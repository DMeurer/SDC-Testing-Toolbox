"""Demo sample generation and publication for provider sample-array metrics."""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from typing import TYPE_CHECKING

from sdc11073.xml_types import msg_qnames, pm_types
from sdc11073.xml_types.dataconverters import DecimalConverter

from . import constants
from .model import (
    MAX_DECIMAL_WIRE_CHARS,
    DistributionShape,
    MetricKind,
    MetricSpec,
    WaveformShape,
    fixed_point_decimal,
    validate_decimal,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from sdc11073.mdib import ProviderMdib

logger = logging.getLogger("sdctoolbox.provider")

WAVEFORM_BLOCK_SECONDS = constants.WAVEFORM_BLOCK_SECONDS
DISTRIBUTION_BINS = 32
DISTRIBUTION_WIDTH = 0.12
DISTRIBUTION_DRIFT = 0.02


@dataclass
class SampleGeneratorSnapshot:
    """Mutable per-source state needed by provider configuration rollback."""

    phases: dict[str, float]
    pinned: set[str]
    quarantined: set[str]
    next_waveform_times: dict[str, float]
    deadlines: dict[str, float]


@dataclass(frozen=True)
class _PreparedSamples:
    handle: str
    entity: object
    old_state_version: int


class SamplePublisher:
    """Prepare sample states and dispatch them through their distinct report paths."""

    def __init__(self, mdib: Callable[[], ProviderMdib]) -> None:
        self._mdib = mdib

    def prepare(
        self,
        handle: str,
        samples: Sequence[Decimal],
        *,
        determination_time: float,
    ) -> _PreparedSamples:
        prepared = [fixed_point_decimal(validate_decimal(sample, "sample"), "sample") for sample in samples]
        entity = self._mdib().entities.by_handle(handle)
        if entity is None:
            msg = f"no metric with handle {handle!r}"
            raise KeyError(msg)
        if entity.state.MetricValue is None:
            entity.state.mk_metric_value()
        entity.state.MetricValue.Samples = prepared
        entity.state.MetricValue.DeterminationTime = determination_time
        entity.state.MetricValue.Validity = pm_types.MeasurementValidity.VALID
        entity.state.MetricValue.MetricQuality.Mode = pm_types.GenerationMode.DEMO
        entity.state.ActivationState = pm_types.ComponentActivation.ON

        # Catch conversion failures before entering a transaction. sdc11073 commits its
        # copied state before report serialization, at which point rollback is impossible.
        entity.state.mk_state_node(msg_qnames.State, self._mdib().nsmapper)
        return _PreparedSamples(handle, entity, entity.state.StateVersion)

    def publish_waveforms(self, prepared: Sequence[_PreparedSamples]) -> set[str]:
        """Commit real-time samples to the Waveform Service transaction family."""
        return self._publish(
            prepared,
            lambda: self._mdib().rt_sample_state_transaction(set_determination_time=False),
        )

    def publish_distributions(self, prepared: Sequence[_PreparedSamples]) -> set[str]:
        """Commit distribution samples to the Metric report transaction family."""
        return self._publish(
            prepared,
            lambda: self._mdib().metric_state_transaction(set_determination_time=False),
        )

    def _publish(
        self,
        prepared: Sequence[_PreparedSamples],
        transaction: Callable[[], AbstractContextManager],
    ) -> set[str]:
        if not prepared:
            return set()
        try:
            with transaction() as manager:
                for item in prepared:
                    manager.write_entity(item.entity)
        except Exception:
            committed = self._committed(prepared)
            if committed:
                logger.exception("sample transaction committed but report publication raised")
                return committed
            raise
        return {item.handle for item in prepared}

    def _committed(self, prepared: Sequence[_PreparedSamples]) -> set[str]:
        committed = set()
        for item in prepared:
            entity = self._mdib().entities.by_handle(item.handle)
            if entity is not None and entity.state.StateVersion > item.old_state_version:
                committed.add(item.handle)
        return committed


class DemoSampleGenerator:
    """Own demo formulas, source clocks, pinning, quarantine, and its worker thread."""

    def __init__(
        self,
        mdib: Callable[[], ProviderMdib],
        specs: Callable[[], Mapping[str, MetricSpec]],
        lock: AbstractContextManager,
        instance_name: str,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mdib = mdib
        self._specs = specs
        self._lock = lock
        self._instance_name = instance_name
        self._clock = clock
        self._monotonic = monotonic
        self._publisher = SamplePublisher(mdib)
        self.phases: dict[str, float] = {}
        self.pinned: set[str] = set()
        self.quarantined: set[str] = set()
        self.next_waveform_times: dict[str, float] = {}
        self.deadlines: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def snapshot(self) -> SampleGeneratorSnapshot:
        return SampleGeneratorSnapshot(
            phases=dict(self.phases),
            pinned=set(self.pinned),
            quarantined=set(self.quarantined),
            next_waveform_times=dict(self.next_waveform_times),
            deadlines=dict(self.deadlines),
        )

    def restore(self, snapshot: SampleGeneratorSnapshot) -> None:
        self.phases = dict(snapshot.phases)
        self.pinned = set(snapshot.pinned)
        self.quarantined = set(snapshot.quarantined)
        self.next_waveform_times = dict(snapshot.next_waveform_times)
        self.deadlines = dict(snapshot.deadlines)

    def clear(self) -> None:
        self.phases.clear()
        self.pinned.clear()
        self.quarantined.clear()
        self.next_waveform_times.clear()
        self.deadlines.clear()
        self._thread = None

    def forget(self, handle: str) -> None:
        self.phases.pop(handle, None)
        self.pinned.discard(handle)
        self.quarantined.discard(handle)
        self.next_waveform_times.pop(handle, None)
        self.deadlines.pop(handle, None)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                name=f"samples-{self._instance_name}",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:  # noqa: BLE001 - isolate one malformed source
                self._thread = None
                self._stop.set()
                raise
            logger.info("sample generator started")

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        try:
            thread.join(timeout=5.0)
            if thread.is_alive():
                msg = "sample generator did not stop within 5 seconds"
                raise RuntimeError(msg)
        finally:
            self._thread = None
        logger.info("sample generator stopped")

    def publish_manual(self, handle: str, spec: MetricSpec, samples: Sequence[Decimal]) -> None:
        if spec.kind is MetricKind.DISTRIBUTION and len(samples) != DISTRIBUTION_BINS:
            msg = f"a distribution needs exactly {DISTRIBUTION_BINS} samples, not {len(samples)}"
            raise ValueError(msg)
        timestamp = self._clock()
        if spec.kind is MetricKind.WAVEFORM:
            timestamp -= max(0, len(samples) - 1) * float(spec.sample_period)
        prepared = self._publisher.prepare(
            handle,
            samples,
            determination_time=timestamp,
        )
        publisher = (
            self._publisher.publish_waveforms
            if spec.kind is MetricKind.WAVEFORM
            else self._publisher.publish_distributions
        )
        committed = publisher([prepared])
        if handle in committed:
            self.pinned.add(handle)
            self.quarantined.discard(handle)

    def publish_once(self, *, force: bool = True) -> None:
        """Generate and publish each due source, independently by report family."""
        with self._lock:
            now = self._monotonic()
            sources = [
                (handle, spec)
                for handle, spec in self._specs().items()
                if spec.is_sample_array
                and handle not in self.pinned
                and handle not in self.quarantined
                and (force or self.deadlines.get(handle, now) <= now)
            ]
            waveforms = [(handle, spec) for handle, spec in sources if spec.kind is MetricKind.WAVEFORM]
            distributions = [(handle, spec) for handle, spec in sources if spec.kind is MetricKind.DISTRIBUTION]
            self._publish_waveform_sources(waveforms, now)
            self._publish_distribution_sources(distributions, now)

    def next_block(self, handle: str, spec: MetricSpec) -> tuple[list[Decimal], float]:
        cycle = spec.waveform_cycle_sample_count()
        count = spec.generated_waveform_block_sample_count()
        sample_range = spec.generated_sample_range()
        phase = self.phases.get(handle, 0.0)
        block = [
            _generated_sample(spec, _shape_fraction(spec.shape, (phase + index / cycle) % 1.0), sample_range)
            for index in range(count)
        ]
        return block, (phase + count / cycle) % 1.0

    def _publish_waveform_sources(self, sources: Sequence[tuple[str, MetricSpec]], now: float) -> None:
        prepared = []
        updates: dict[str, tuple[float, float, float]] = {}
        wall_time = self._clock()
        for handle, spec in sources:
            try:
                block, next_phase = self.next_block(handle, spec)
                period = float(spec.sample_period)
                first_time = self.next_waveform_times.get(
                    handle,
                    wall_time - max(0, len(block) - 1) * period,
                )
                item = self._publisher.prepare(
                    handle,
                    block,
                    determination_time=first_time,
                )
            except KeyError:
                logger.debug("skipped a block for %s", handle, exc_info=True)
                continue
            except Exception:  # noqa: BLE001 - isolate one malformed source
                self._quarantine(handle)
                continue
            prepared.append(item)
            updates[handle] = (
                next_phase,
                first_time + len(block) * period,
                now + len(block) * period,
            )

        committed = self._publish_family(
            prepared,
            self._publisher.publish_waveforms,
            "waveform",
        )
        for handle in committed:
            phase, next_time, deadline = updates[handle]
            self.phases[handle] = phase
            self.next_waveform_times[handle] = next_time
            self.deadlines[handle] = deadline

    def _publish_distribution_sources(self, sources: Sequence[tuple[str, MetricSpec]], now: float) -> None:
        prepared = []
        updates: dict[str, float] = {}
        for handle, spec in sources:
            phase = self.phases.get(handle, 0.0)
            try:
                item = self._publisher.prepare(
                    handle,
                    distribution_samples(spec, phase),
                    determination_time=self._clock(),
                )
            except KeyError:
                logger.debug("skipped a block for %s", handle, exc_info=True)
                continue
            except Exception:  # noqa: BLE001 - isolate one malformed source
                self._quarantine(handle)
                continue
            prepared.append(item)
            updates[handle] = (phase + DISTRIBUTION_DRIFT) % 1.0

        committed = self._publish_family(
            prepared,
            self._publisher.publish_distributions,
            "distribution",
        )
        for handle in committed:
            self.phases[handle] = updates[handle]
            self.deadlines[handle] = now + WAVEFORM_BLOCK_SECONDS

    def _publish_family(
        self,
        prepared: Sequence[_PreparedSamples],
        publish: Callable[[Sequence[_PreparedSamples]], set[str]],
        family: str,
    ) -> set[str]:
        if not prepared:
            return set()
        try:
            return publish(prepared)
        except Exception:
            logger.exception("%s sample transaction failed; isolating sources", family)

        committed = set()
        failed = []
        for item in prepared:
            try:
                committed.update(publish([item]))
            except Exception:  # noqa: BLE001 - identify a source that cannot commit
                failed.append(item.handle)
                logger.debug("isolated sample publication failure for %s", item.handle, exc_info=True)
        for handle in failed:
            self._quarantine(handle, exc_info=False)
        return committed

    def _quarantine(self, handle: str, *, exc_info: bool = True) -> None:
        if handle in self.quarantined:
            return
        self.quarantined.add(handle)
        logger.error(
            "sample generation failed for %s; generation disabled",
            handle,
            exc_info=exc_info,
        )

    def _run(self) -> None:
        failure_logged = False
        while not self._stop.is_set():
            try:
                self.publish_once(force=False)
                failure_logged = False
            except Exception:
                if not failure_logged:
                    logger.exception("sample generator failed; retrying")
                    failure_logged = True
            with self._lock:
                active = [
                    self.deadlines.get(handle, self._monotonic())
                    for handle, spec in self._specs().items()
                    if spec.is_sample_array and handle not in self.pinned and handle not in self.quarantined
                ]
            delay = max(0.0, min(active) - self._monotonic()) if active else WAVEFORM_BLOCK_SECONDS
            self._stop.wait(delay)


def domain_step(spec: MetricSpec) -> Decimal:
    """Return spacing along a distribution domain, independently of sample values."""
    upper = spec.domain_maximum if spec.domain_maximum is not None else Decimal("1")
    lower = spec.domain_minimum if spec.domain_minimum is not None else Decimal("0")
    span = upper - lower
    if span <= 0:
        msg = f"distribution domain must increase, not {lower} to {upper}"
        raise ValueError(msg)
    step = span / Decimal(DISTRIBUTION_BINS - 1)
    quantum = Decimal(1).scaleb(step.adjusted() - 5)
    step = step.quantize(quantum)
    if step.is_zero():
        msg = f"distribution domain {lower} to {upper} produces a zero StepWidth"
        raise ValueError(msg)
    wire_step = fixed_point_decimal(step.normalize(), "distribution StepWidth")
    if Decimal(DecimalConverter.to_xml(wire_step)).is_zero():
        msg = f"distribution domain {lower} to {upper} produces a zero StepWidth on the wire"
        raise ValueError(msg)
    return wire_step


def distribution_samples(spec: MetricSpec, phase: float) -> list[Decimal]:
    """Generate one sample-value block across a coded distribution domain."""
    sample_range = spec.generated_sample_range()
    return [
        _generated_sample(
            spec,
            _bin_weight(spec.distribution_shape, index / (DISTRIBUTION_BINS - 1), phase),
            sample_range,
        )
        for index in range(DISTRIBUTION_BINS)
    ]


def _bin_weight(shape: DistributionShape, position: float, phase: float) -> float:
    if shape is DistributionShape.BELL:
        centre = 0.5 + 0.3 * math.sin(phase * 2.0 * math.pi)
        return _bump(position, centre, DISTRIBUTION_WIDTH)
    if shape is DistributionShape.SPECTRUM:
        fundamental = 0.12 + 0.02 * math.sin(phase * 2.0 * math.pi)
        total = sum(
            (1.0 / harmonic**1.6) * _bump(position, fundamental * harmonic, 0.022)
            for harmonic in range(1, 6)
        )
        return min(1.0, total)
    if shape is DistributionShape.BIMODAL:
        drift = 0.06 * math.sin(phase * 2.0 * math.pi)
        return min(1.0, _bump(position, 0.28 + drift, 0.075) + 0.72 * _bump(position, 0.68 - drift, 0.09))
    if shape is DistributionShape.DECAY:
        wobble = 1.0 + 0.15 * math.sin(phase * 2.0 * math.pi)
        return min(1.0, math.exp(-position * 3.2 * wobble))
    return min(1.0, max(0.0, 0.5 + 0.05 * math.sin(phase * 2.0 * math.pi) + random.uniform(-0.04, 0.04)))  # noqa: S311


def _bump(position: float, centre: float, width: float) -> float:
    offset = position - centre
    return math.exp(-(offset * offset) / (2.0 * width * width))


def _shape_fraction(shape: WaveformShape, position: float) -> float:  # noqa: PLR0911
    if shape is WaveformShape.SINE:
        return (math.sin(position * 2.0 * math.pi) + 1.0) / 2.0
    if shape is WaveformShape.SAWTOOTH:
        return position
    if shape is WaveformShape.SQUARE:
        return 1.0 if position < 0.5 else 0.0  # noqa: PLR2004
    if shape is WaveformShape.NOISE:
        return random.random()  # noqa: S311
    if shape is WaveformShape.PULSE:
        if position < 0.15:  # noqa: PLR2004
            return _ease(position / 0.15)
        if position < 0.30:  # noqa: PLR2004
            return 1.0 - 0.45 * _ease((position - 0.15) / 0.15)
        if position < 0.38:  # noqa: PLR2004
            return 0.55 + 0.12 * math.sin((position - 0.30) / 0.08 * math.pi)
        return 0.67 * math.exp(-(position - 0.38) * 4.0)
    if shape is WaveformShape.ARTERIAL:
        return 0.35 + 0.65 * _shape_fraction(WaveformShape.PULSE, position)
    if shape is WaveformShape.ECG:
        return _ecg_fraction(position)
    if shape is WaveformShape.RESPIRATION:
        if position < 0.30:  # noqa: PLR2004
            return _ease(position / 0.30)
        if position < 0.40:  # noqa: PLR2004
            return 1.0
        if position < 0.75:  # noqa: PLR2004
            return 1.0 - _ease((position - 0.40) / 0.35)
        return 0.0
    if position < 0.30:  # noqa: PLR2004
        return 0.5 + 0.5 * math.sin(position / 0.30 * math.pi)
    if position < 0.75:  # noqa: PLR2004
        return 0.5 - 0.4 * math.sin((position - 0.30) / 0.45 * math.pi)
    return 0.5


def _ease(fraction: float) -> float:
    clamped = min(1.0, max(0.0, fraction))
    return (1.0 - math.cos(clamped * math.pi)) / 2.0


_ECG_FEATURES = (
    (0.16, 0.13, 0.035),
    (0.36, -0.10, 0.012),
    (0.40, 0.85, 0.012),
    (0.44, -0.22, 0.014),
    (0.62, 0.22, 0.055),
)
_ECG_BASELINE = 0.25


def _ecg_fraction(position: float) -> float:
    value = _ECG_BASELINE
    for centre, height, width in _ECG_FEATURES:
        offset = position - centre
        value += height * math.exp(-(offset * offset) / (2.0 * width * width))
    return min(1.0, max(0.0, value))


def _generated_sample(
    spec: MetricSpec,
    fraction: float,
    sample_range: tuple[Decimal, Decimal],
) -> Decimal:
    low, high = sample_range
    if low == high:
        return low
    if not math.isfinite(fraction):
        msg = f"generated sample fraction must be finite, not {fraction}"
        raise ValueError(msg)
    anchor_at_high = spec.minimum is None and spec.maximum is not None
    normalized = Decimal(str(min(1.0, max(0.0, fraction))))
    with localcontext() as context:
        context.prec = MAX_DECIMAL_WIRE_CHARS * 2 + 32
        step_count = ((high - low) / spec.resolution).to_integral_value(rounding=ROUND_FLOOR)
        if step_count == 0:
            return high if anchor_at_high else low
        distance = (Decimal(1) - normalized) if anchor_at_high else normalized
        step_index = (distance * step_count).to_integral_value(rounding=ROUND_HALF_UP)
        sample = high - step_index * spec.resolution if anchor_at_high else low + step_index * spec.resolution
    if not low <= sample <= high:
        msg = f"generated sample {sample} escaped range {low} to {high}"
        raise ArithmeticError(msg)
    return sample
