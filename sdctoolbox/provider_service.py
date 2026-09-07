"""Provider side: publish an SDC device whose data sources are created at runtime.

This module owns everything that turns a MetricSpec into BICEPS descriptors and keeps the
SCO in sync. It has no GUI dependency; stage 2 puts a PySide6 skin on top of it.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from typing import TYPE_CHECKING

from sdc11073.location import SdcLocation
from sdc11073.mdib import ProviderMdib, mdibbase
from sdc11073.provider import SdcProvider
from sdc11073.provider.baseproduct import BaseProduct
from sdc11073.provider.operations import ActivateOperation
from sdc11073.provider.providerimpl import RoleProviderComponents
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.dataconverters import DecimalConverter
from sdc11073.xml_types.dpws_types import ThisDeviceType, ThisModelType

from . import constants
from .handlers import apply_metric_value, make_activate_handler, make_set_handler
from .model import (
    DEFAULT_PATIENT,
    MAX_DECIMAL_WIRE_CHARS,
    ActionSpec,
    AlertSpec,
    Coding,
    DeviceInfo,
    DistributionShape,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    SignalInfo,
    WaveformShape,
    coerce_metric_value,
    fixed_point_decimal,
    patient_info_from_biceps,
    patient_measurement_wire_value,
    slugify,
    validate_decimal,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sdc11073.provider.sco import AbstractScoOperationsRegistry

logger = logging.getLogger("sdctoolbox.provider")


@dataclass
class _ConfigurationSnapshot:
    descriptors: list
    states: list
    context_states: list
    operations: dict[str, str]
    specs: dict[str, MetricSpec]
    alerts: dict[str, AlertSpec]
    alert_signals: dict[str, list[str]]
    actions: dict[str, ActionSpec]
    sections: dict[str, str]
    registered_operations: dict
    provider_location: SdcLocation
    pending_alert_sources: set[str]
    waveform_phase: dict[str, float]
    pinned_samples: set[str]
    generator_running: bool


# How much waveform data goes out per report. Samples are generated in blocks rather than
# one at a time, because a report per sample would be all overhead: a 0.1s sample period
# would mean ten SOAP messages a second per waveform.
#
# This sets latency, not smoothness. The consumer paces what it draws from SamplePeriod
# (see widgets/plot.py), so a block only decides how stale the newest sample is when it
# arrives. A quarter second is a reasonable trade against four SOAP messages per second
# per waveform - and a preset with three of them is twelve.
WAVEFORM_BLOCK_SECONDS = constants.WAVEFORM_BLOCK_SECONDS

# How many samples a generated distribution spreads across its domain. This is what fixes
# DistributionRange/StepWidth, because the two have to agree: a descriptor saying the
# samples are 0.1 Hz apart while five arrive for a 50 Hz domain describes nothing real.
DISTRIBUTION_BINS = 32

# How wide the generated bell is, as a fraction of the domain.
DISTRIBUTION_WIDTH = 0.12

# How far the bell's peak moves per tick, as a fraction of a full sweep.
DISTRIBUTION_DRIFT = 0.02


def _coded_value(coding: Coding, label: str = "") -> pm_types.CodedValue:
    """Turn one of our Codings into the BICEPS element.

    The label rides along as a ConceptDescription, which is documentation. The code and its
    coding system are the part another device can act on.
    """
    text = label or coding.label
    return pm_types.CodedValue(
        code=coding.code,
        coding_system=coding.coding_system,
        concept_descriptions=[pm_types.LocalizedText(text, lang="en-US")] if text else None,
    )


def _domain_step(spec: MetricSpec) -> Decimal:
    """The gap between two samples along a distribution's domain.

    Quantised to six significant digits, because a domain that does not divide evenly by the
    bin count produces a repeating decimal and that would go on the wire in full. Precision
    follows the domain span so small valid domains do not collapse to a zero step.
    """
    # `or` would be wrong here: Decimal("0") is falsy, so a domain ending at zero - which
    # -60..0 dB is - would silently become 1 and the step would be computed over 61.
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


def _distribution_samples(spec: MetricSpec, phase: float) -> list[Decimal]:
    """A block across the domain, in the shape the spec asks for.

    A distribution is a shape over a domain rather than a signal in time, so the generated
    one is whatever makes the device recognisable. It still moves, because a card that never
    changes tells you nothing about whether reports are arriving.
    """
    sample_range = spec.generated_sample_range()
    shape = spec.distribution_shape

    samples = []
    for index in range(DISTRIBUTION_BINS):
        position = index / (DISTRIBUTION_BINS - 1)
        samples.append(_generated_sample(spec, _bin_weight(shape, position, phase), sample_range))
    return samples


def _bin_weight(shape: DistributionShape, position: float, phase: float) -> float:
    """How full one bin is, 0..1, at a position across the domain."""
    if shape is DistributionShape.BELL:
        centre = 0.5 + 0.3 * math.sin(phase * 2.0 * math.pi)
        return _bump(position, centre, DISTRIBUTION_WIDTH)

    if shape is DistributionShape.SPECTRUM:
        # A fundamental near the low end with harmonics above it, each smaller than the
        # last: what a power spectrum looks like, and why a spectrum is a distribution
        # rather than a waveform.
        fundamental = 0.12 + 0.02 * math.sin(phase * 2.0 * math.pi)
        total = 0.0
        for harmonic in range(1, 6):
            total += (1.0 / harmonic**1.6) * _bump(position, fundamental * harmonic, 0.022)
        return min(1.0, total)

    if shape is DistributionShape.BIMODAL:
        drift = 0.06 * math.sin(phase * 2.0 * math.pi)
        return min(1.0, _bump(position, 0.28 + drift, 0.075) + 0.72 * _bump(position, 0.68 - drift, 0.09))

    if shape is DistributionShape.DECAY:
        wobble = 1.0 + 0.15 * math.sin(phase * 2.0 * math.pi)
        return min(1.0, math.exp(-position * 3.2 * wobble))

    # FLAT: a baseline with a little noise, for a reference channel.
    return min(1.0, max(0.0, 0.5 + 0.05 * math.sin(phase * 2.0 * math.pi) + random.uniform(-0.04, 0.04)))  # noqa: S311


def _bump(position: float, centre: float, width: float) -> float:
    """A gaussian bump, clamped to something a bin can hold."""
    offset = position - centre
    return math.exp(-(offset * offset) / (2.0 * width * width))


def _shape_fraction(shape: WaveformShape, position: float) -> float:  # noqa: PLR0911
    """Where the curve sits between its ends, at a position through one cycle.

    Returns 0..1. The physiological ones are caricatures - drawn so a person recognises the
    signal and a consumer has something with structure to render - not clinical models.
    """
    if shape is WaveformShape.SINE:
        return (math.sin(position * 2.0 * math.pi) + 1.0) / 2.0
    if shape is WaveformShape.SAWTOOTH:
        return position
    if shape is WaveformShape.SQUARE:
        return 1.0 if position < 0.5 else 0.0  # noqa: PLR2004
    if shape is WaveformShape.NOISE:
        return random.random()  # noqa: S311 - a test signal, not a secret

    if shape is WaveformShape.PULSE:
        # Steep systolic upstroke, dicrotic notch, slow diastolic decay.
        if position < 0.15:  # noqa: PLR2004
            return _ease(position / 0.15)
        if position < 0.30:  # noqa: PLR2004
            return 1.0 - 0.45 * _ease((position - 0.15) / 0.15)
        if position < 0.38:  # noqa: PLR2004
            # The notch: the aortic valve closing puts a bump in the downslope.
            return 0.55 + 0.12 * math.sin((position - 0.30) / 0.08 * math.pi)
        return 0.67 * math.exp(-(position - 0.38) * 4.0)

    if shape is WaveformShape.ARTERIAL:
        # The same beat, but blood pressure never returns to zero.
        return 0.35 + 0.65 * _shape_fraction(WaveformShape.PULSE, position)

    if shape is WaveformShape.ECG:
        return _ecg_fraction(position)

    if shape is WaveformShape.RESPIRATION:
        # Rise, plateau, passive fall, pause. Inspiration is shorter than expiration.
        if position < 0.30:  # noqa: PLR2004
            return _ease(position / 0.30)
        if position < 0.40:  # noqa: PLR2004
            return 1.0
        if position < 0.75:  # noqa: PLR2004
            return 1.0 - _ease((position - 0.40) / 0.35)
        return 0.0

    # FLOW: inspiratory limb positive, expiratory negative, so it crosses the middle.
    if position < 0.30:  # noqa: PLR2004
        return 0.5 + 0.5 * math.sin(position / 0.30 * math.pi)
    if position < 0.75:  # noqa: PLR2004
        return 0.5 - 0.4 * math.sin((position - 0.30) / 0.45 * math.pi)
    return 0.5


def _ease(fraction: float) -> float:
    """A smooth 0..1 ramp, so a caricature does not look like it was drawn with a ruler."""
    clamped = min(1.0, max(0.0, fraction))
    return (1.0 - math.cos(clamped * math.pi)) / 2.0


# Where each feature of the ECG sits in a beat, how tall it is, and how wide.
# The QRS is narrow and large; P and T are broad and small. Baseline sits low so the
# S wave has somewhere to go.
_ECG_FEATURES = (
    (0.16, 0.13, 0.035),  # P wave
    (0.36, -0.10, 0.012),  # Q
    (0.40, 0.85, 0.012),  # R
    (0.44, -0.22, 0.014),  # S
    (0.62, 0.22, 0.055),  # T wave
)
_ECG_BASELINE = 0.25


def _ecg_fraction(position: float) -> float:
    """A recognisable PQRST complex, as a sum of bumps on a baseline."""
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
    """Map a normalized shape value to the metric's bounded resolution grid."""
    low, high = sample_range
    if low == high:
        return low
    if not math.isfinite(fraction):
        msg = f"generated sample fraction must be finite, not {fraction}"
        raise ValueError(msg)

    # A maximum-only range has no lower descriptor endpoint to define its grid, so count
    # down from the declared maximum. Every other range counts up from its lower endpoint.
    anchor_at_high = spec.minimum is None and spec.maximum is not None
    normalized = Decimal(str(min(1.0, max(0.0, fraction))))
    with localcontext() as context:
        # Bounds and resolution can each occupy the full permitted fixed-point width.
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


class ProviderService:
    """An SDC provider whose MDIB grows while it is running.

    Usage::

        service = ProviderService(instance_name="alpha")
        service.start()
        handle = service.add_metric(MetricSpec("Zoom level", MetricKind.NUMBER, controllable=True))
        service.set_value(handle, Decimal("3"))
        service.stop()
    """

    def __init__(
            self,
            ip: str = constants.DEFAULT_IP,
            instance_name: str = constants.DEFAULT_INSTANCE_NAME,
            friendly_name: str | None = None,
            device: DeviceInfo | None = None,
    ) -> None:
        self.ip = ip
        self.instance_name = instance_name
        # A preset describing a ventilator should announce itself as one. sdc11073 takes
        # ThisModel and ThisDevice when the provider is constructed, so this cannot be
        # changed later - run_toolbox reads the config before starting for that reason.
        self.device = device or DeviceInfo()
        self.friendly_name = friendly_name or self.device.friendly_name or f"Toolbox {instance_name}"
        self.epr = constants.epr_for(instance_name)

        self._discovery: WSDiscovery | None = None
        self._provider: SdcProvider | None = None
        self._mdib: ProviderMdib | None = None
        self._sco: AbstractScoOperationsRegistry | None = None
        self._handler = None
        self._activate_handler = None
        # metric handle -> operation handle, for metrics that have a set operation
        self._operations: dict[str, str] = {}
        # metric handle -> the spec it was created from
        self._specs: dict[str, MetricSpec] = {}
        # alarm handle -> the spec it was created from
        self._alerts: dict[str, AlertSpec] = {}
        # alarm handle -> the signals announcing it
        self._alert_signals: dict[str, list[str]] = {}
        # action handle -> the spec it was created from
        self._actions: dict[str, ActionSpec] = {}
        # section name -> the Channel handle its metrics were put in
        self._sections: dict[str, str] = {}
        # Sources arriving during an alert evaluation are retained for its next pass. The
        # service lock protects the set; the non-blocking lock elects exactly one drainer.
        self._pending_alert_sources: set[str] = set()
        self._alert_evaluator_lock = threading.Lock()
        # Waveform generation: one thread for every waveform, and where each curve had
        # got to, so a new block continues rather than restarting.
        self._waveform_thread: threading.Thread | None = None
        self._waveform_stop = threading.Event()
        self._waveform_phase: dict[str, float] = {}
        # Sample arrays whose block was set by hand, and which the generator leaves alone.
        self._pinned_samples: set[str] = set()
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------------------

    @property
    def mdib(self) -> ProviderMdib:
        """The live provider MDIB. Raises if the service is not started."""
        if self._mdib is None:
            msg = "provider is not started"
            raise RuntimeError(msg)
        return self._mdib

    def start(self) -> None:
        """Bring the provider up and announce it on the network."""
        if self._discovery is not None or self._provider is not None or self._mdib is not None:
            msg = "provider is already started"
            raise RuntimeError(msg)

        try:
            self._discovery = WSDiscovery(self.ip)
            self._discovery.start()

            self._mdib = ProviderMdib.from_mdib_file(str(constants.BOOTSTRAP_MDIB_PATH))
            # The handler tells us which metric it touched once its transaction has closed, so a
            # value written by a remote consumer raises a limit alarm exactly as a local edit
            # does. It cannot be done from a metrics observable: sdc11073 fires those while
            # holding the transaction lock, and opening the alert transaction there deadlocks.
            self._handler = make_set_handler(
                self._mdib,
                coerce_value=self._coerce_value,
                on_applied=self._on_metric_applied,
            )
            self._activate_handler = make_activate_handler(
                self._mdib,
                effects_for=self._effects_for,
                coerce_value=self._coerce_value,
                on_applied=self._on_metric_applied,
            )

            this_model = ThisModelType(
                manufacturer=self.device.manufacturer,
                manufacturer_url=self.device.manufacturer_url,
                model_name=self.device.model_name,
                model_number=self.device.model_number,
                model_url=self.device.manufacturer_url,
                presentation_url=self.device.manufacturer_url,
            )
            this_device = ThisDeviceType(
                friendly_name=self.friendly_name,
                firmware_version=self.device.firmware_version,
                serial_number=self.instance_name,
            )

            # SdcProvider only builds SCO registries when a role_provider_class is present
            # (see providerimpl._setup_components). We therefore always supply one, and use the
            # factory to capture the registry instance instead of reaching into private state.
            def role_provider_factory(mdib, sco, log_prefix):  # noqa: ANN001, ANN202
                self._sco = sco
                return BaseProduct(mdib, sco, log_prefix)

            self._provider = SdcProvider(
                ws_discovery=self._discovery,
                epr=self.epr,
                this_model=this_model,
                this_device=this_device,
                device_mdib_container=self._mdib,
                role_provider_components=RoleProviderComponents(role_provider_class=role_provider_factory),
            )

            # No waveform provider configured, so the real-time sample loop must stay off.
            self._provider.start_all(start_rtsample_loop=False)
            self.set_location(LocationInfo(**constants.DEFAULT_LOCATION))
            self.set_patient(DEFAULT_PATIENT)

            if self._sco is None:
                msg = "no SCO registry was created - the bootstrap MDIB is missing its Sco element"
                raise RuntimeError(msg)
        except Exception:
            try:
                self.stop()
            except Exception:
                logger.exception("error while rolling back provider startup")
            raise

        logger.info("provider %r up on %s, EPR %s", self.instance_name, self.ip, self.epr.urn)

    def stop(self) -> None:
        """Take the provider off the network."""
        provider = self._provider
        discovery = self._discovery
        first_error: Exception | None = None
        first_traceback = None

        cleanups = [("sample generator", self.stop_generator)]
        if provider is not None:
            cleanups.append(("provider", provider.stop_all))
        if discovery is not None:
            cleanups.append(("discovery", discovery.stop))

        for name, cleanup in cleanups:
            try:
                cleanup()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                    first_traceback = exc.__traceback__
                logger.exception("error while stopping provider %s", name)

        with self._lock:
            self._discovery = None
            self._provider = None
            self._mdib = None
            self._sco = None
            self._handler = None
            self._activate_handler = None
            self._operations.clear()
            self._specs.clear()
            self._alerts.clear()
            self._alert_signals.clear()
            self._pending_alert_sources.clear()
            self._actions.clear()
            self._sections.clear()
            self._waveform_phase.clear()
            self._pinned_samples.clear()
            self._waveform_thread = None
        logger.info("provider %r stopped", self.instance_name)
        if first_error is not None:
            raise first_error.with_traceback(first_traceback)

    def _snapshot_configuration(self) -> _ConfigurationSnapshot:
        """Clone all mutable provider state needed to undo a profile import."""
        with self._lock:
            mdib = self.mdib
            return _ConfigurationSnapshot(
                descriptors=deepcopy(list(mdib.descriptions.objects)),
                states=deepcopy(list(mdib.states.objects)),
                context_states=deepcopy(list(mdib.context_states.objects)),
                operations=dict(self._operations),
                specs=dict(self._specs),
                alerts=dict(self._alerts),
                alert_signals={handle: list(signals) for handle, signals in self._alert_signals.items()},
                actions=dict(self._actions),
                sections=dict(self._sections),
                registered_operations=dict(self._sco._registered_operations),  # noqa: SLF001
                provider_location=deepcopy(self._provider._location),  # noqa: SLF001
                pending_alert_sources=set(self._pending_alert_sources),
                waveform_phase=dict(self._waveform_phase),
                pinned_samples=set(self._pinned_samples),
                generator_running=self.generator_running,
            )

    def _restore_configuration(self, snapshot: _ConfigurationSnapshot) -> None:
        """Restore a snapshot with compensating reports for connected consumers."""
        if not snapshot.generator_running:
            self.stop_generator()
        with self._lock:
            mdib = self.mdib
            location_changed = self._provider._location != snapshot.provider_location  # noqa: SLF001
            self._operations = snapshot.operations
            self._specs = snapshot.specs
            self._alerts = snapshot.alerts
            self._alert_signals = snapshot.alert_signals
            self._actions = snapshot.actions
            self._sections = snapshot.sections
            self._pending_alert_sources = snapshot.pending_alert_sources
            self._waveform_phase = snapshot.waveform_phase
            self._pinned_samples = snapshot.pinned_samples
            self._sco._registered_operations = snapshot.registered_operations  # noqa: SLF001
            self._provider._location = snapshot.provider_location  # noqa: SLF001

            saved_descriptors = {descriptor.Handle: descriptor for descriptor in snapshot.descriptors}
            managed_handles = (
                set(snapshot.specs)
                | set(snapshot.operations.values())
                | set(snapshot.alerts)
                | set(snapshot.actions)
                | {signal for signals in snapshot.alert_signals.values() for signal in signals}
                | set(snapshot.sections.values())
            )
            managed_handles.update(
                saved_descriptors[channel].parent_handle
                for channel in snapshot.sections.values()
            )
            while descendants := {
                descriptor.Handle
                for descriptor in snapshot.descriptors
                if descriptor.parent_handle in managed_handles and descriptor.Handle not in managed_handles
            }:
                managed_handles.update(descendants)
            context_handles = {
                descriptor.Handle
                for descriptor in snapshot.descriptors
                if descriptor.is_context_descriptor
            }
            current = {handle: entity for handle, entity in mdib.entities.items()}
            removal_candidates = (set(current) - set(saved_descriptors)) | managed_handles | context_handles
            removal_handles = {
                handle
                for handle in removal_candidates
                if handle in current and current[handle].parent_handle not in removal_candidates
            }

            with mdib.descriptor_transaction() as mgr:
                for handle in removal_handles:
                    mgr.remove_entity(current[handle])

            states = {state.DescriptorHandle: state for state in snapshot.states}
            context_states: dict[str, list] = {}
            for state in snapshot.context_states:
                context_states.setdefault(state.DescriptorHandle, []).append(state)

            managed_entities = []
            context_entities = []
            for handle in managed_handles | context_handles:
                saved_descriptor = saved_descriptors[handle]
                descriptor = deepcopy(saved_descriptor)
                if descriptor.is_context_descriptor:
                    saved_states = deepcopy(context_states.get(descriptor.Handle, []))
                    entity = mdibbase.MultiStateEntity(mdib, descriptor, saved_states)
                    context_entities.append(entity)
                else:
                    entity = mdibbase.Entity(mdib, descriptor, deepcopy(states[descriptor.Handle]))
                    managed_entities.append(entity)

            with mdib.descriptor_transaction() as mgr:
                mgr.write_entities(managed_entities)
            with mdib.descriptor_transaction() as mgr:
                mgr.write_entities(context_entities)

            for operation in snapshot.registered_operations.values():
                operation._operation_entity = mdib.entities.by_handle(operation.handle)  # noqa: SLF001

        if location_changed:
            self._provider.publish()
        if snapshot.generator_running:
            self.start_generator()

    def _restore_appended_configuration(
            self,
            snapshot: _ConfigurationSnapshot,
            touched_contexts: set[str],
    ) -> None:
        """Remove an interrupted append without recreating unchanged live descriptors."""
        if not snapshot.generator_running:
            self.stop_generator()
        with self._lock:
            mdib = self.mdib
            saved_descriptors = {descriptor.Handle: descriptor for descriptor in snapshot.descriptors}
            added_handles = {handle for handle, _ in mdib.entities.items()} - set(saved_descriptors)
            added_roots = {
                handle
                for handle in added_handles
                if mdib.entities.by_handle(handle).parent_handle not in added_handles
            }

            self._operations = snapshot.operations
            self._specs = snapshot.specs
            self._alerts = snapshot.alerts
            self._alert_signals = snapshot.alert_signals
            self._actions = snapshot.actions
            self._sections = snapshot.sections
            self._pending_alert_sources = snapshot.pending_alert_sources
            self._waveform_phase = snapshot.waveform_phase
            self._pinned_samples = snapshot.pinned_samples
            self._sco._registered_operations = snapshot.registered_operations  # noqa: SLF001

            if added_roots:
                with mdib.descriptor_transaction() as mgr:
                    for handle in added_roots:
                        mgr.remove_entity(mdib.entities.by_handle(handle))

            context_states: dict[str, list] = {}
            for state in snapshot.context_states:
                context_states.setdefault(state.DescriptorHandle, []).append(state)
            restored_contexts = []
            for handle in touched_contexts:
                current = mdib.entities.by_handle(handle)
                if current is not None:
                    with mdib.descriptor_transaction() as mgr:
                        mgr.remove_entity(current)
                descriptor = deepcopy(saved_descriptors[handle])
                restored_contexts.append(
                    mdibbase.MultiStateEntity(
                        mdib,
                        descriptor,
                        deepcopy(context_states.get(handle, [])),
                    ),
                )
            if restored_contexts:
                with mdib.descriptor_transaction() as mgr:
                    mgr.write_entities(restored_contexts)

            location_changed = (
                constants.LOCATION_CONTEXT_HANDLE in touched_contexts
                and self._provider._location != snapshot.provider_location  # noqa: SLF001
            )
            self._provider._location = snapshot.provider_location  # noqa: SLF001

        if location_changed:
            self._provider.publish()
        if snapshot.generator_running:
            self.start_generator()

    def __enter__(self) -> ProviderService:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- data sources --------------------------------------------------------------

    def add_metric(self, spec: MetricSpec) -> str:
        """Create a data source at runtime and return its handle.

        The descriptor is written inside a descriptor transaction, which makes sdc11073 emit
        a DescriptionModificationReport, so connected consumers see the new metric appear.
        """
        with self._lock:
            if spec.is_sample_array:
                # MetricSpec is mutable, so repeat this preflight immediately before any
                # lazy section, descriptor, bookkeeping, or generator mutation.
                if spec.kind is MetricKind.WAVEFORM:
                    spec.waveform_cycle_sample_count()
                    spec.generated_waveform_block_sample_count()
                spec.generated_sample_range()
            if spec.kind is MetricKind.DISTRIBUTION:
                lower = spec.domain_minimum if spec.domain_minimum is not None else Decimal("0")
                upper = spec.domain_maximum if spec.domain_maximum is not None else Decimal("1")
                if lower >= upper:
                    msg = f"distribution domain must increase, not {lower} to {upper}"
                    raise ValueError(msg)
                # Compute this before lazily creating a section or any metric entity.
                _domain_step(spec)
            if spec.is_sample_array and spec.initial_value is not None:
                msg = f"initial_value is not meaningful for {spec.kind.value} metrics; use samples instead"
                raise ValueError(msg)

            handle = spec.handle or self._unique_handle(constants.METRIC_HANDLE_PREFIX + spec.slug)
            if self.mdib.entities.by_handle(handle) is not None:
                msg = f"handle {handle!r} already exists"
                raise ValueError(msg)

            entity = self.mdib.entities.new_entity(
                spec.kind.descriptor_qname,
                handle,
                self._channel_for(spec.section),
            )
            self._apply_spec_to_descriptor(entity.descriptor, spec)

            self._create_entities([entity])

            self._specs[handle] = spec
            logger.info("added %s metric %r (%s)", spec.kind.value, spec.label, handle)

            if spec.initial_value is not None:
                self.set_value(handle, spec.initial_value)
            if spec.controllable:
                self.enable_control(handle)
            if spec.is_sample_array:
                # A sample array with nothing driving it publishes a descriptor and never a
                # sample, which looks like a broken device rather than an idle one. That is
                # what a distribution used to do: there was no way to fill one from the
                # window at all, so its card said 'waiting for samples' for ever.
                self.start_generator()
            return handle

    def remove_metric(self, handle: str) -> None:
        """Delete a data source and everything that depends on it."""
        with self._lock:
            metric_entity = self.mdib.entities.by_handle(handle)
            section_entities = []
            dependent_handles = {handle}
            if metric_entity is not None and metric_entity.parent_handle in self._section_channels():
                channel_handle = metric_entity.parent_handle
                section_entities, section_handles = self._empty_section_entities(
                    channel_handle,
                    excluding_metric=handle,
                )
                dependent_handles.update(section_handles)

            alert_handles = sorted(
                alert_handle
                for alert_handle, spec in self._alerts.items()
                if spec.source_handle == handle
            )
            signal_handles = sorted(
                signal_handle
                for alert_handle in alert_handles
                for signal_handle in self._alert_signals.get(alert_handle, [])
            )
            dependent_handles.update(alert_handles)
            dependent_handles.update(signal_handles)

            operation_handle = self._operations.get(handle)
            if operation_handle is not None:
                dependent_handles.add(operation_handle)

            action_handles = self._dependent_action_handles(
                dependent_handles,
                effect_metric=handle,
            )
            for action_handle in action_handles:
                self._sco.unregister_operation_by_handle(action_handle)
            if operation_handle is not None:
                self._sco.unregister_operation_by_handle(operation_handle)

            # Drop the bookkeeping before committing. Leaving the transaction fires
            # deleted_descriptors_by_handle synchronously, and any observer that reacts by
            # listing our metrics would otherwise see a handle whose entity is already gone.
            for action_handle in action_handles:
                self._actions.pop(action_handle, None)
            self._operations.pop(handle, None)
            for alert_handle in alert_handles:
                self._alerts.pop(alert_handle, None)
                self._alert_signals.pop(alert_handle, None)
            self._specs.pop(handle, None)
            self._pending_alert_sources.discard(handle)
            self._waveform_phase.pop(handle, None)
            self._pinned_samples.discard(handle)
            for entity in section_entities:
                if entity.node_type == pm.ChannelDescriptor:
                    self._forget_section(entity.handle)

            removal_handles = [
                *action_handles,
                *([operation_handle] if operation_handle is not None else []),
                *signal_handles,
                *alert_handles,
                handle,
                *(entity.handle for entity in section_entities),
            ]
            with self.mdib.descriptor_transaction() as mgr:
                for removal_handle in removal_handles:
                    entity = self.mdib.entities.by_handle(removal_handle)
                    if entity is not None:
                        mgr.remove_entity(entity)

            logger.info("removed metric %s", handle)

    def _remove_empty_sections(self) -> None:
        """Delete section containment left without any owned metrics."""
        with self._lock:
            for channel_handle in self._section_channels():
                if self.mdib.entities.by_handle(channel_handle) is None:
                    self._forget_section(channel_handle)
                    continue
                entities, handles = self._empty_section_entities(channel_handle)
                if not entities:
                    continue
                for action_handle, spec in list(self._actions.items()):
                    if spec.target_handle in handles:
                        self.remove_action(action_handle)
                self._forget_section(channel_handle)
                with self.mdib.descriptor_transaction() as mgr:
                    for entity in entities:
                        mgr.remove_entity(entity)

    def set_value(self, handle: str, value: Decimal | str) -> None:
        """Set the current value of one of our own metrics."""
        with self._lock:
            spec = self._specs.get(handle)
            entity = self.mdib.entities.by_handle(handle)
            if spec is None or entity is None:
                msg = f"no metric with handle {handle!r}"
                raise KeyError(msg)
            normalized = coerce_metric_value(spec, value, handle)
            apply_metric_value(entity.state, normalized)
            with self.mdib.metric_state_transaction() as mgr:
                mgr.write_entity(entity)

        # Outside the transaction on purpose: writing the alarm state needs a transaction of
        # its own, and the metric one still holds the lock until the with block closes.
        self._evaluate_alerts({handle})

    def get_value(self, handle: str):  # noqa: ANN201 - the value type depends on the metric kind
        """Read back the current value of one of our own metrics.

        Returns None both when the metric has no value yet and when it no longer exists, so
        that observers reacting to a deletion do not have to guard against a race.
        """
        entity = self.mdib.entities.by_handle(handle)
        if entity is None:
            return None
        return getattr(getattr(entity.state, "MetricValue", None), "Value", None)

    def list_metrics(self) -> dict[str, MetricSpec]:
        """Return the specs of all data sources we created, keyed by handle."""
        return dict(self._specs)

    # -- remote control ------------------------------------------------------------

    def enable_control(self, handle: str) -> str:
        """Make a metric remote-controllable and return the operation handle.

        On first call this registers a new operation with the SCO, which creates the
        operation descriptor and emits a DescriptionModificationReport. On later calls it
        merely flips OperatingMode back to En.
        """
        with self._lock:
            spec = self._specs.get(handle)
            if spec is None:
                msg = f"no metric with handle {handle!r}"
                raise KeyError(msg)
            operation_class = spec.kind.operation_class
            if operation_class is None:
                msg = f"{spec.kind.value} metrics cannot be remote-controlled"
                raise ValueError(msg)

            operation_handle = self._operations.get(handle)
            if operation_handle is None:
                operation_handle = constants.OPERATION_HANDLE_PREFIX + handle.removeprefix(
                    constants.METRIC_HANDLE_PREFIX,
                )
                operation_handle = self._unique_handle(operation_handle)
                operation = operation_class(
                    handle=operation_handle,
                    operation_target_handle=handle,
                    operation_handler=self._handler,
                    # The operation is our own construct, not a term from anywhere, so it
                    # stays in the private system even when its target carries an MDC code.
                    coded_value=pm_types.CodedValue(
                        code=f"set_{spec.effective_type().code}",
                        coding_system=constants.CODING_SYSTEM_PRIVATE,
                    ),
                )
                # register_operation calls set_mdib, which creates the descriptor for us.
                self._sco.register_operation(operation)
                self._operations[handle] = operation_handle
                logger.info("registered operation %s targeting %s", operation_handle, handle)

            self._set_operating_mode(operation_handle, pm_types.OperatingMode.ENABLED)
            self._set_allowed_range(operation_handle, spec)
            self._set_metric_category(handle, pm_types.MetricCategory.SETTING)
            return operation_handle

    def disable_control(self, handle: str) -> None:
        """Switch remote control off without deleting the operation.

        Deliberately not unregister_operation_by_handle: that only drops the operation from
        an internal dict and leaves its descriptor advertised to consumers, which would show
        an editor with nothing behind it.
        """
        with self._lock:
            operation_handle = self._operations.get(handle)
            if operation_handle is None:
                return
            self._set_operating_mode(operation_handle, pm_types.OperatingMode.DISABLED)

    def operation_handle_for(self, handle: str) -> str | None:
        """Return the operation handle controlling a metric, if there is one."""
        return self._operations.get(handle)

    # -- alarms --------------------------------------------------------------------

    def add_alert(self, spec: AlertSpec) -> str:
        """Create an alarm condition and the signals that announce it.

        With limits, the condition becomes a LimitAlertCondition and its presence follows
        the source metric from then on. Without them it stays a plain AlertCondition that
        only ``set_alert_presence`` moves.
        """
        with self._lock:
            if self.mdib.entities.by_handle(spec.source_handle) is None:
                msg = f"no metric with handle {spec.source_handle!r} to watch"
                raise KeyError(msg)

            handle = spec.handle or self._unique_handle(constants.ALERT_HANDLE_PREFIX + spec.slug)
            node_type = pm.LimitAlertConditionDescriptor if spec.has_limits else pm.AlertConditionDescriptor
            entity = self.mdib.entities.new_entity(node_type, handle, constants.ALERT_SYSTEM_HANDLE)

            descriptor = entity.descriptor
            descriptor.Type = pm_types.CodedValue(
                code=spec.slug,
                coding_system=constants.CODING_SYSTEM_PRIVATE,
                concept_descriptions=[pm_types.LocalizedText(spec.label, lang="en-US")],
            )
            descriptor.Source = [spec.source_handle]
            descriptor.Kind = spec.kind
            descriptor.Priority = spec.priority
            if spec.has_limits:
                descriptor.MaxLimits = pm_types.Range(
                    lower=(
                        fixed_point_decimal(spec.lower_limit, "lower_limit")
                        if spec.lower_limit is not None
                        else None
                    ),
                    upper=(
                        fixed_point_decimal(spec.upper_limit, "upper_limit")
                        if spec.upper_limit is not None
                        else None
                    ),
                )

            entity.state.ActivationState = pm_types.AlertActivation.ON
            entity.state.Presence = False
            if spec.has_limits:
                entity.state.Limits = pm_types.Range(
                    lower=(
                        fixed_point_decimal(spec.lower_limit, "lower_limit")
                        if spec.lower_limit is not None
                        else None
                    ),
                    upper=(
                        fixed_point_decimal(spec.upper_limit, "upper_limit")
                        if spec.upper_limit is not None
                        else None
                    ),
                )

            signal_entities = []
            for signal_spec in spec.signals:
                manifestation = signal_spec.manifestation
                signal_handle = self._unique_handle(
                    f"{constants.SIGNAL_HANDLE_PREFIX}{spec.slug}.{manifestation.value.lower()}",
                )
                signal = self.mdib.entities.new_entity(
                    pm.AlertSignalDescriptor,
                    signal_handle,
                    constants.ALERT_SYSTEM_HANDLE,
                )
                signal.descriptor.ConditionSignaled = handle
                signal.descriptor.Manifestation = manifestation
                signal.descriptor.Latching = signal_spec.latching
                signal.descriptor.SignalDelegationSupported = spec.delegable
                signal.state.ActivationState = pm_types.AlertActivation.ON
                signal.state.Presence = pm_types.AlertSignalPresence.OFF
                signal.state.Location = pm_types.AlertSignalPrimaryLocation.LOCAL
                signal_entities.append(signal)

            self._create_entities([entity, *signal_entities])

            self._alerts[handle] = spec
            self._alert_signals[handle] = [s.handle for s in signal_entities]
            logger.info(
                "added alarm %r watching %s (%s)",
                spec.label,
                spec.source_handle,
                spec.limit_text() or "manual",
            )

            if spec.has_limits:
                self._evaluate_alerts({spec.source_handle})
            return handle

    def remove_alert(self, handle: str) -> None:
        """Delete an alarm condition together with its signals."""
        with self._lock:
            signal_handles = sorted(self._alert_signals.get(handle, []))
            dependent_handles = {handle, *signal_handles}
            action_handles = self._dependent_action_handles(dependent_handles)
            for action_handle in action_handles:
                self._sco.unregister_operation_by_handle(action_handle)
                self._actions.pop(action_handle, None)

            self._alert_signals.pop(handle, None)
            self._alerts.pop(handle, None)
            entities = [
                entity
                for entity in (
                    self.mdib.entities.by_handle(h)
                    for h in [*action_handles, *signal_handles, handle]
                )
                if entity is not None
            ]
            with self.mdib.descriptor_transaction() as mgr:
                for entity in entities:
                    mgr.remove_entity(entity)
            logger.info("removed alarm %s", handle)

    def set_alert_presence(self, handle: str, present: bool) -> None:  # noqa: FBT001
        """Raise or clear an alarm by hand.

        Only meaningful for a condition without limits; one with limits is recomputed from
        its source metric and would overwrite this on the next change.
        """
        with self._lock:
            self._write_alert_presence(handle, present=present)

    def alert_present(self, handle: str) -> bool:
        """Whether the condition is currently raised."""
        entity = self.mdib.entities.by_handle(handle)
        return bool(getattr(getattr(entity, "state", None), "Presence", False))

    def list_alerts(self) -> dict[str, AlertSpec]:
        """The specs of every alarm we created, keyed by handle."""
        return dict(self._alerts)

    def signal_handles_for(self, handle: str) -> list[str]:
        """Handles of the signals announcing one condition."""
        return list(self._alert_signals.get(handle, []))

    def signal_states(self, handle: str) -> list[SignalInfo]:
        """How each signal of one condition is currently announcing it."""
        infos = []
        for signal_handle in self._alert_signals.get(handle, []):
            entity = self.mdib.entities.by_handle(signal_handle)
            if entity is None:
                continue
            infos.append(
                SignalInfo(
                    handle=signal_handle,
                    manifestation=str(entity.descriptor.Manifestation),
                    presence=str(entity.state.Presence),
                    location=str(entity.state.Location),
                    delegable=bool(entity.descriptor.SignalDelegationSupported),
                    latching=bool(entity.descriptor.Latching),
                ),
            )
        return infos

    def acknowledge_alert(self, handle: str) -> int:
        """Acknowledge every signal of a condition. Returns how many were acknowledged.

        Acknowledging changes how the alarm is announced, not whether it is true: the
        condition keeps its Presence, and only the signals move to Ack. That distinction is
        the reason conditions and signals are separate objects, so the tool has to honour it
        rather than quietly clear the condition.
        """
        with self._lock:
            if handle not in self._alert_signals:
                msg = f"no alarm with handle {handle!r}"
                raise KeyError(msg)
            if not self.alert_present(handle):
                msg = f"{handle!r} is not raised, so there is nothing to acknowledge"
                raise ValueError(msg)
            acknowledged = [
                entity
                for entity in (
                    self.mdib.entities.by_handle(h) for h in self._alert_signals[handle]
                )
                if entity is not None and entity.state.Presence != pm_types.AlertSignalPresence.ACK
            ]
            if not acknowledged:
                return 0
            for entity in acknowledged:
                entity.state.Presence = pm_types.AlertSignalPresence.ACK
            with self.mdib.alert_state_transaction() as mgr:
                for entity in acknowledged:
                    mgr.write_entity(entity)
            logger.info("acknowledged %d signal(s) of %s", len(acknowledged), handle)
            return len(acknowledged)

    def stop_latched_signals(self, handle: str) -> int:
        """Deliberately stop signals that are currently latching after a cleared condition."""
        with self._lock:
            if handle not in self._alert_signals:
                msg = f"no alarm with handle {handle!r}"
                raise KeyError(msg)
            latched = [
                entity
                for entity in (self.mdib.entities.by_handle(h) for h in self._alert_signals[handle])
                if entity is not None and entity.state.Presence == pm_types.AlertSignalPresence.LATCH
            ]
            if not latched:
                return 0
            for entity in latched:
                entity.state.Presence = pm_types.AlertSignalPresence.OFF
            with self.mdib.alert_state_transaction() as mgr:
                for entity in latched:
                    mgr.write_entity(entity)
            logger.info("stopped %d latched signal(s) of %s", len(latched), handle)
            return len(latched)

    def set_signal_delegated(self, signal_handle: str, *, delegated: bool) -> None:
        """Hand a signal over to another device, or take it back.

        Delegation moves the signal's Location from Loc to Rem. BICEPS only allows it where
        the descriptor says SignalDelegationSupported, and nothing in sdc11073 enforces that,
        so this does.

        Note what this is and is not. It records that the announcement now belongs somewhere
        else. It does not arrange for anybody to pick it up: that needs a device offering a
        delegable signal of its own and an operation to drive it, which is beyond a two-role
        toolbox talking to itself.
        """
        with self._lock:
            entity = self.mdib.entities.by_handle(signal_handle)
            if entity is None:
                msg = f"no alarm signal with handle {signal_handle!r}"
                raise KeyError(msg)
            if delegated and not entity.descriptor.SignalDelegationSupported:
                msg = (
                    f"{signal_handle!r} is not delegable: its descriptor does not set "
                    f"SignalDelegationSupported"
                )
                raise ValueError(msg)
            location = (
                pm_types.AlertSignalPrimaryLocation.REMOTE
                if delegated
                else pm_types.AlertSignalPrimaryLocation.LOCAL
            )
            if entity.state.Location == location:
                return
            entity.state.Location = location
            with self.mdib.alert_state_transaction() as mgr:
                mgr.write_entity(entity)
            logger.info("signal %s is now announced %s", signal_handle, location)

    def _on_metric_applied(self, metric_handle: str) -> None:
        """A metric changed, by whatever route. Recompute the alarms watching it."""
        self._evaluate_alerts({metric_handle})

    def _evaluate_alerts(self, source_handles: set[str]) -> None:
        if not source_handles:
            return

        with self._lock:
            if self._mdib is None:
                return
            self._pending_alert_sources.update(source_handles)
            if not self._alert_evaluator_lock.acquire(blocking=False):
                return

        owns_evaluator = True
        first_error: Exception | None = None
        try:
            while True:
                with self._lock:
                    if self._mdib is None:
                        self._pending_alert_sources.clear()
                    if not self._pending_alert_sources:
                        # Release while holding the queue lock. A later caller therefore
                        # either sees this evaluator or becomes the next one itself.
                        self._alert_evaluator_lock.release()
                        owns_evaluator = False
                        break
                    pending = set(self._pending_alert_sources)
                    self._pending_alert_sources.clear()
                    alerts = [
                        (handle, spec)
                        for handle, spec in self._alerts.items()
                        if spec.has_limits and spec.source_handle in pending
                    ]

                for handle, spec in alerts:
                    try:
                        value = self.get_value(spec.source_handle)
                        self._write_alert_presence(handle, present=spec.breached_by(value))
                    except Exception as exc:  # noqa: BLE001 - drain other queued sources first
                        first_error = first_error or exc

            if first_error is not None:
                raise first_error
        finally:
            if owns_evaluator:
                with self._lock:
                    self._alert_evaluator_lock.release()

    def _write_alert_presence(self, handle: str, *, present: bool) -> None:
        entity = self.mdib.entities.by_handle(handle)
        if entity is None:
            msg = f"no alarm with handle {handle!r}"
            raise KeyError(msg)
        if bool(getattr(entity.state, "Presence", False)) == present:
            # Also what keeps an acknowledgement alive: alarms are re-evaluated on every
            # change to the source metric, and without this an Ack would be overwritten with
            # On by the very next value that is still out of range.
            return

        entity.state.Presence = present
        entity.state.DeterminationTime = time.time()
        signals = [
            signal
            for signal in (
                self.mdib.entities.by_handle(h) for h in self._alert_signals.get(handle, [])
            )
            if signal is not None
        ]
        for signal in signals:
            # A fresh occurrence has not been acknowledged, so Ack does not survive the
            # condition going away and coming back.
            if present:
                signal.state.Presence = pm_types.AlertSignalPresence.ON
            elif signal.state.Presence == pm_types.AlertSignalPresence.ACK:
                signal.state.Presence = pm_types.AlertSignalPresence.OFF
            elif signal.descriptor.Latching:
                signal.state.Presence = pm_types.AlertSignalPresence.LATCH
            else:
                signal.state.Presence = pm_types.AlertSignalPresence.OFF

        with self.mdib.alert_state_transaction() as mgr:
            mgr.write_entity(entity)
            for signal in signals:
                mgr.write_entity(signal)
        logger.info("alarm %s is now %s", handle, "present" if present else "clear")

    # -- contexts ------------------------------------------------------------------

    def get_location(self) -> LocationInfo:
        """Where this device says it is."""
        state = self._associated_context_state(constants.LOCATION_CONTEXT_HANDLE)
        detail = getattr(state, "LocationDetail", None) if state is not None else None
        if detail is None:
            return LocationInfo()
        return LocationInfo(
            facility=detail.Facility or "",
            building=detail.Building or "",
            floor=detail.Floor or "",
            point_of_care=detail.PoC or "",
            room=detail.Room or "",
            bed=detail.Bed or "",
        )

    def set_location(self, info: LocationInfo) -> None:
        """Move the device, and tell the network.

        Deliberately routed through SdcProvider.set_location rather than written as a context
        state by hand: a location is also a WS-Discovery scope, so changing it has to
        re-announce the device or consumers would keep filtering on the old one.
        """
        with self._lock:
            if self._provider is None:
                msg = "provider is not started"
                raise RuntimeError(msg)
            self._provider.set_location(
                SdcLocation(
                    fac=info.facility or None,
                    bldng=info.building or None,
                    flr=info.floor or None,
                    poc=info.point_of_care or None,
                    rm=info.room or None,
                    bed=info.bed or None,
                ),
            )
            logger.info("location is now %s", info.summary() or "unset")

    def get_patient(self) -> PatientInfo:
        """Who the device says it is attached to."""
        state = self._associated_context_state(constants.PATIENT_CONTEXT_HANDLE)
        core = getattr(state, "CoreData", None) if state is not None else None
        return patient_info_from_biceps(core)

    def set_patient(self, info: PatientInfo) -> None:
        """Associate a patient with the device, replacing whoever was there before.

        A context is a multi-state entity: the old state is disassociated rather than
        overwritten, so the MDIB keeps the history of who was attached when. That is why
        this cannot simply write over one state the way a metric does.
        """
        if info.is_empty():
            self.clear_patient()
            return
        with self._lock:
            entity = self.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE)
            if entity is None:
                msg = f"the bootstrap MDIB has no {constants.PATIENT_CONTEXT_HANDLE}"
                raise RuntimeError(msg)

            core = pm_types.PatientDemographicsCoreData()
            core.Givenname = info.given_name or None
            core.Familyname = info.family_name or None
            core.Sex = pm_types.Sex(info.sex) if info.sex else None
            core.PatientType = pm_types.PatientType(info.patient_type) if info.patient_type else None
            if info.date_of_birth:
                # Raises ValueError on anything xsd:date cannot express, which is what we want:
                # a silently dropped birth date would look like the field does not work.
                core.set_birthdate(info.date_of_birth)
            if info.height is not None:
                core.Height = pm_types.Measurement(
                    patient_measurement_wire_value(info.height.value),
                    _coded_value(info.height.unit),
                )
            if info.weight is not None:
                core.Weight = pm_types.Measurement(
                    patient_measurement_wire_value(info.weight.value),
                    _coded_value(info.weight.unit),
                )
            if info.race is not None:
                core.Race = _coded_value(info.race)

            # Context transactions commit before report serialization. Serialize the complete
            # demographics first so malformed user/profile data cannot disassociate the prior
            # patient and leave an unpublishable replacement in the MDIB.
            try:
                core.as_etree_node(pm.CoreData, {})
            except (TypeError, ValueError) as exc:
                msg = f"patient demographics cannot be serialized: {exc}"
                raise ValueError(msg) from exc

            state = entity.new_state()
            state.CoreData = core
            state.ContextAssociation = pm_types.ContextAssociation.ASSOCIATED
            state.BindingStartTime = time.time()

            with self.mdib.context_state_transaction() as mgr:
                mgr.disassociate_all(entity.handle, ignored_handle=state.Handle)
                mgr.write_entity(entity, [state.Handle])
            logger.info("patient is now %s", info.summary() or "unset")

    def clear_patient(self) -> None:
        """Detach whoever is currently associated, without putting anyone in their place."""
        with self._lock:
            entity = self.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE)
            if entity is None:
                return
            with self.mdib.context_state_transaction() as mgr:
                disassociated = mgr.disassociate_all(entity.handle)
            logger.info("detached %d patient context state(s)", len(disassociated))

    def _associated_context_state(self, descriptor_handle: str):  # noqa: ANN202 - an sdc11073 state
        """The currently associated state of a context, or None when there is none.

        A context descriptor holds many states and at most one of them is associated. The
        rest are the history, which is exactly what makes a context different from a metric.
        """
        entity = self.mdib.entities.by_handle(descriptor_handle)
        if entity is None:
            return None
        entity.update()
        for state in entity.states.values():
            if state.ContextAssociation == pm_types.ContextAssociation.ASSOCIATED:
                return state
        return None

    # -- sample arrays -------------------------------------------------------------

    def set_samples(self, handle: str, samples: Sequence[Decimal]) -> None:
        """Publish a fresh block of samples for a waveform or a distribution.

        A sample array state carries many values where an ordinary metric carries one, so
        this is the sample-array equivalent of set_value rather than an addition to it.

        The transaction picked here decides which report goes out, and sdc11073 chooses it
        from the *state* rather than from anything we pass: a RealTimeSampleArrayMetricState
        lands in TransactionResult.rt_updates and leaves as a WaveformStream, everything
        else lands in metric_updates and leaves as an EpisodicMetricReport. So a waveform
        needs the rt transaction and a distribution must not use it.
        """
        with self._lock:
            spec = self._specs.get(handle)
            if spec is None:
                msg = f"no metric with handle {handle!r}"
                raise KeyError(msg)
            if not spec.is_sample_array:
                msg = f"{handle!r} is a {spec.kind.value}, which holds one value; use set_value"
                raise ValueError(msg)
            if spec.kind is MetricKind.DISTRIBUTION and len(samples) != DISTRIBUTION_BINS:
                msg = f"a distribution needs exactly {DISTRIBUTION_BINS} samples, not {len(samples)}"
                raise ValueError(msg)

            entity = self._apply_samples(handle, samples)
            # Setting a block by hand takes the metric off the generator. Without this the
            # next tick would overwrite it, which makes set_samples look broken.
            self._pinned_samples.add(handle)
            if spec.kind is MetricKind.WAVEFORM:
                with self.mdib.rt_sample_state_transaction() as mgr:
                    mgr.write_entity(entity)
            else:
                with self.mdib.metric_state_transaction() as mgr:
                    mgr.write_entity(entity)

    def _apply_samples(self, handle: str, samples: Sequence[Decimal]):  # noqa: ANN202 - an Entity
        """Put a block on a state without committing it. Caller opens the transaction."""
        prepared = [fixed_point_decimal(validate_decimal(sample, "sample"), "sample") for sample in samples]
        entity = self.mdib.entities.by_handle(handle)
        if entity is None:
            msg = f"no metric with handle {handle!r}"
            raise KeyError(msg)
        # mk_metric_value raises if there already is one, so only ever call it once.
        if entity.state.MetricValue is None:
            entity.state.mk_metric_value()
        entity.state.MetricValue.Samples = prepared
        entity.state.MetricValue.DeterminationTime = time.time()
        return entity

    def get_samples(self, handle: str) -> list[Decimal]:
        """The samples currently published for a sample-array metric."""
        entity = self.mdib.entities.by_handle(handle)
        if entity is None:
            return []
        value = getattr(entity.state, "MetricValue", None)
        return list(getattr(value, "Samples", None) or [])

    def start_generator(self) -> None:
        """Begin generating samples for every sample array this device publishes.

        Both kinds, not just waveforms: a distribution with nothing driving it shows an
        empty card for ever, and until this existed there was no way to fill one from the
        window at all. Pushing a block with set_samples takes that metric off the generator,
        so your own data is not overwritten on the next tick.

        One thread drives all of them. sdc11073 ships a WaveformProviderProtocol and no
        implementation of it, and a provider configured with none must be started with
        start_rtsample_loop=False - so rather than write that protocol, this walks the
        waveforms itself and writes their states. The report comes out either way, because
        the provider builds it from the transaction result.
        """
        with self._lock:
            if self._waveform_thread is not None:
                return
            self._waveform_stop.clear()
            thread = threading.Thread(
                target=self._run_waveforms,
                name=f"samples-{self.instance_name}",
                daemon=True,
            )
            self._waveform_thread = thread
            try:
                thread.start()
            except Exception:
                self._waveform_thread = None
                self._waveform_stop.set()
                raise
            logger.info("sample generator started")

    def stop_generator(self) -> None:
        """Stop generating samples and wait for the thread to notice."""
        thread = self._waveform_thread
        if thread is None:
            return
        self._waveform_stop.set()
        try:
            thread.join(timeout=5.0)
            if thread.is_alive():
                msg = "sample generator did not stop within 5 seconds"
                raise RuntimeError(msg)
        finally:
            self._waveform_thread = None
        logger.info("sample generator stopped")

    @property
    def generator_running(self) -> bool:
        """Whether the generator thread is alive."""
        return self._waveform_thread is not None and self._waveform_thread.is_alive()

    def _run_waveforms(self) -> None:
        """Push one block per sample array, per tick, until asked to stop.

        Every waveform goes into **one** transaction, so the tick produces one
        WaveformStream carrying all of them rather than one report each. sdc11073 builds
        that report from TransactionResult.rt_updates, which is already a list.
        """
        failure_logged = False
        while not self._waveform_stop.is_set():
            started = time.monotonic()
            try:
                self._publish_one_block()
                failure_logged = False
            except Exception:
                if not failure_logged:
                    logger.exception("sample generator failed; retrying")
                    failure_logged = True
            # Sleep the remainder of the block, so generation keeps real time rather than
            # drifting by however long the writes took.
            self._waveform_stop.wait(max(0.0, WAVEFORM_BLOCK_SECONDS - (time.monotonic() - started)))

    def _publish_one_block(self) -> None:
        """Advance every generated sample array by one block and send them.

        Two transactions, not one, and that is forced by the standard rather than chosen:
        sdc11073 sorts states into buckets by type, and a distribution state is an ordinary
        metric state. Putting it in the rt transaction would not make it a waveform, it
        would just be in the wrong place.
        """
        with self._lock:
            waveforms = []
            distributions = []
            for handle, spec in self._specs.items():
                if not spec.is_sample_array or handle in self._pinned_samples:
                    continue
                if spec.kind is MetricKind.WAVEFORM:
                    waveforms.append((handle, spec))
                else:
                    distributions.append((handle, spec))
            if not waveforms and not distributions:
                return

            advanced: dict[str, float] = {}
            wave_entities = []
            for handle, spec in waveforms:
                try:
                    block, next_phase = self._next_block(handle, spec)
                    entity = self._try_apply(handle, block)
                except Exception:  # noqa: BLE001 - quarantine a permanently broken metric
                    self._quarantine_generation(handle)
                    continue
                if entity is not None:
                    wave_entities.append(entity)
                    advanced[handle] = next_phase

            dist_entities = []
            for handle, spec in distributions:
                phase = self._waveform_phase.get(handle, 0.0)
                try:
                    entity = self._try_apply(handle, _distribution_samples(spec, phase))
                except Exception:  # noqa: BLE001 - quarantine a permanently broken metric
                    self._quarantine_generation(handle)
                    continue
                if entity is not None:
                    dist_entities.append(entity)
                    advanced[handle] = (phase + DISTRIBUTION_DRIFT) % 1.0

            if wave_entities:
                with self.mdib.rt_sample_state_transaction() as mgr:
                    for entity in wave_entities:
                        mgr.write_entity(entity)
            if dist_entities:
                with self.mdib.metric_state_transaction() as mgr:
                    for entity in dist_entities:
                        mgr.write_entity(entity)

            # Only once the blocks are out. Advancing a phase for samples that were never
            # published would leave a step in the curve the size of the lost block.
            self._waveform_phase.update(advanced)

    def _try_apply(self, handle: str, block: list[Decimal]):  # noqa: ANN202 - an Entity or None
        """Stage a block, or None if the metric went away between listing and writing."""
        try:
            return self._apply_samples(handle, block)
        except KeyError:
            logger.debug("skipped a block for %s", handle, exc_info=True)
            return None

    def _quarantine_generation(self, handle: str) -> None:
        """Disable one broken metric after logging its generation failure once."""
        self._pinned_samples.add(handle)
        logger.exception("sample generation failed for %s; generation disabled", handle)

    def _next_block(self, handle: str, spec: MetricSpec) -> tuple[list[Decimal], float]:
        """The next block for one waveform, and the phase it leaves off at.

        Deliberately does not store the phase: see _publish_one_block.
        """
        cycle = spec.waveform_cycle_sample_count()
        count = spec.generated_waveform_block_sample_count()
        sample_range = spec.generated_sample_range()

        phase = self._waveform_phase.get(handle, 0.0)
        block = [
            _generated_sample(spec, _shape_fraction(spec.shape, (phase + index / cycle) % 1.0), sample_range)
            for index in range(count)
        ]
        return block, (phase + count / cycle) % 1.0

    # -- actions -------------------------------------------------------------------

    def add_action(self, spec: ActionSpec) -> str:
        """Publish something the device can be told to do, and return its handle.

        An ActivateOperation, which is the BICEPS way of saying "do the thing" as opposed
        to "take this value". Registering it with the SCO creates the descriptor and emits
        the report, the same route a set operation takes.
        """
        with self._lock:
            if self.mdib.entities.by_handle(spec.target_handle) is None:
                msg = f"no descriptor with handle {spec.target_handle!r} for action {spec.label!r} to act on"
                raise KeyError(msg)

            handle = spec.handle or self._unique_handle(constants.ACTION_HANDLE_PREFIX + spec.slug)
            operation = ActivateOperation(
                handle=handle,
                operation_target_handle=spec.target_handle,
                operation_handler=self._activate_handler,
                coded_value=_coded_value(spec.effective_type(), spec.label),
            )
            self._sco.register_operation(operation)
            self._actions[handle] = spec
            self._set_operating_mode(handle, pm_types.OperatingMode.ENABLED)
            logger.info("added action %r (%s) on %s", spec.label, handle, spec.target_handle)
            return handle

    def remove_action(self, handle: str) -> None:
        """Delete an action and every action that transitively targets it."""
        with self._lock:
            if handle not in self._actions:
                return

            action_handles = self._dependent_action_handles({handle})
            if handle not in action_handles:
                action_handles.append(handle)

            for action_handle in action_handles:
                self._sco.unregister_operation_by_handle(action_handle)
                self._actions.pop(action_handle, None)

            with self.mdib.descriptor_transaction() as mgr:
                for action_handle in action_handles:
                    entity = self.mdib.entities.by_handle(action_handle)
                    if entity is not None:
                        mgr.remove_entity(entity)
            logger.info("removed action %s and %d dependent action(s)", handle, len(action_handles) - 1)

    def list_actions(self) -> dict[str, ActionSpec]:
        """The specs of every action we published, keyed by handle."""
        return dict(self._actions)

    def run_action(self, handle: str) -> None:
        """Invoke one of our own actions locally, as a remote consumer would.

        Deliberately routed through the same effects the remote path uses, so a button here
        and an invocation over the network cannot drift apart.
        """
        with self._lock:
            spec = self._actions.get(handle)
            if spec is None:
                msg = f"no action with handle {handle!r}"
                raise KeyError(msg)
            prepared = []
            for target, value in spec.effects.items():
                entity = self.mdib.entities.by_handle(target)
                if entity is None:
                    msg = f"effect target {target!r} does not exist"
                    raise KeyError(msg)
                prepared.append((entity, self._coerce_value(target, value)))

            for entity, value in prepared:
                apply_metric_value(entity.state, value)
            if prepared:
                with self.mdib.metric_state_transaction() as mgr:
                    for entity, _ in prepared:
                        mgr.write_entity(entity)

        touched = {entity.handle for entity, _ in prepared}
        self._evaluate_alerts(touched)
        logger.info("action %s ran locally, changing %d metric(s)", handle, len(touched))

    def _effects_for(self, operation_handle: str) -> dict[str, object]:
        spec = self._actions.get(operation_handle)
        return dict(spec.effects) if spec is not None else {}

    def _coerce_value(self, handle: str, value: object) -> Decimal | str:
        spec = self._specs.get(handle)
        if spec is None:
            msg = f"no metric with handle {handle!r}"
            raise KeyError(msg)
        return coerce_metric_value(spec, value, handle)

    # -- internals -----------------------------------------------------------------

    def _dependent_action_handles(
        self,
        descriptor_handles: set[str],
        *,
        effect_metric: str | None = None,
    ) -> list[str]:
        """Find actions depending on a descriptor closure, dependents before targets."""
        waves = []
        found: set[str] = set()
        while True:
            wave = []
            for action_handle, spec in sorted(self._actions.items()):
                if action_handle in found:
                    continue
                entity = self.mdib.entities.by_handle(action_handle)
                target = getattr(getattr(entity, "descriptor", None), "OperationTarget", spec.target_handle)
                if target in descriptor_handles or effect_metric is not None and effect_metric in spec.effects:
                    wave.append(action_handle)
            if not wave:
                break
            waves.append(wave)
            found.update(wave)
            descriptor_handles.update(wave)
        return [action_handle for wave in reversed(waves) for action_handle in wave]

    def _create_entities(self, entities: list) -> None:
        """Write brand new entities in one descriptor transaction, undoing them if it fails.

        A descriptor BICEPS considers incomplete is not rejected by the transaction. The
        transaction commits it, and only then does sdc11073 serialise the
        DescriptionModificationReport from an observer of mdib.transaction - which is where
        the complaint comes from. By that point descriptions.add_object_no_lock has already
        run, so letting the exception out unhandled leaves a handle in the MDIB that is
        advertised to consumers while this service has no record of it.

        Undoing it is best effort by design: see _discard_entities.
        """
        handles = [entity.handle for entity in entities]
        try:
            with self.mdib.descriptor_transaction() as mgr:
                for entity in entities:
                    mgr.write_entity(entity)
        except Exception:
            logger.warning("creating %s failed, undoing it", ", ".join(handles))
            self._discard_entities(handles)
            raise

    def _discard_entities(self, handles: list[str]) -> None:
        """Remove entities that were committed but could not be announced.

        One transaction per handle, so that one that will not come out does not strand the
        rest. The removal report serialises the same descriptor that could not be serialised
        on the way in and therefore fails in the same place - but process_transaction has
        already dropped the descriptor by then, which is the part that matters. Swallow that
        second failure and verify the outcome instead of trusting it.
        """
        for handle in handles:
            entity = self.mdib.entities.by_handle(handle)
            if entity is None:
                continue
            try:
                with self.mdib.descriptor_transaction() as mgr:
                    mgr.remove_entity(entity)
            except Exception:  # noqa: BLE001 - the removal is committed before this fires
                logger.debug("announcing the removal of %s failed too", handle, exc_info=True)
            if self.mdib.entities.by_handle(handle) is None:
                logger.info("undid %s", handle)
            else:
                logger.error("could not undo %s: it is still in the mdib", handle)

    def _channel_for(self, section: str) -> str:
        """The Channel a metric belongs in, creating its subsystem if this is the first.

        A device is not a flat list of readings. A microscope has motion, optics and
        illumination; a monitor has a channel per parameter group. BICEPS models that with
        a Vmd per subsystem and a Channel inside it, and a consumer browsing the containment
        tree sees the difference immediately - which is the whole reason the tree is part of
        the standard rather than an afterthought.

        Sections are created lazily rather than declared, so a preset only has to name one
        against each metric.
        """
        if not section:
            return constants.CHANNEL_HANDLE

        slug = slugify(section)
        vmd_handle = f"{constants.VMD_HANDLE_PREFIX}{slug}"
        channel_handle = f"{constants.CHANNEL_HANDLE_PREFIX}{slug}"
        vmd = self.mdib.entities.by_handle(vmd_handle)
        channel = self.mdib.entities.by_handle(channel_handle)
        if vmd is not None and (vmd.node_type != pm.VmdDescriptor or vmd.parent_handle != constants.MDS_HANDLE):
            msg = f"section {section!r} requires {vmd_handle!r} to be a VmdDescriptor under {constants.MDS_HANDLE!r}"
            raise ValueError(msg)
        if channel is not None and (
            channel.node_type != pm.ChannelDescriptor or channel.parent_handle != vmd_handle
        ):
            msg = f"section {section!r} requires {channel_handle!r} to be a ChannelDescriptor under {vmd_handle!r}"
            raise ValueError(msg)
        if channel is not None:
            if vmd is None:
                msg = f"section {section!r} requires parent {vmd_handle!r} for {channel_handle!r}"
                raise ValueError(msg)
            self._sections[section] = channel_handle
            return channel_handle

        coding = Coding(code=slug, system="private", label=section)
        # The Vmd has to be committed before the Channel can name it: new_entity looks the
        # parent up in mdib.descriptions, so an uncommitted one is not there yet.
        if vmd is None:
            vmd = self.mdib.entities.new_entity(pm.VmdDescriptor, vmd_handle, constants.MDS_HANDLE)
            vmd.descriptor.Type = _coded_value(coding, section)
            self._create_entities([vmd])

        channel = self.mdib.entities.new_entity(pm.ChannelDescriptor, channel_handle, vmd_handle)
        channel.descriptor.Type = _coded_value(coding, section)
        self._create_entities([channel])

        self._sections[section] = channel_handle
        logger.info("created section %r as %s / %s", section, vmd_handle, channel_handle)
        return channel_handle

    def sections(self) -> dict[str, str]:
        """Section name -> the Channel handle its metrics live in."""
        return dict(self._sections)

    def _section_channels(self) -> set[str]:
        """Derive managed section Channels from bookkeeping and live metric parents."""
        channels = set(self._sections.values())
        for metric_handle, spec in self._specs.items():
            if not spec.section:
                continue
            entity = self.mdib.entities.by_handle(metric_handle)
            if entity is not None:
                channels.add(entity.parent_handle)
        return channels

    def _section_descriptor_handles(self) -> set[str]:
        """Section descriptors that removing every managed metric will empty."""
        channels = self._section_channels()
        metrics = set(self._specs)
        removable_channels = {
            channel_handle
            for channel_handle in channels
            if all(
                entity.handle in metrics or entity.parent_handle != channel_handle
                for _, entity in self.mdib.entities.items()
            )
        }
        handles = set(removable_channels)
        for channel_handle in removable_channels:
            channel = self.mdib.entities.by_handle(channel_handle)
            if channel is None:
                continue
            vmd_handle = channel.parent_handle
            if all(
                entity.handle in removable_channels or entity.parent_handle != vmd_handle
                for _, entity in self.mdib.entities.items()
            ):
                handles.add(vmd_handle)
        return handles

    def _forget_section(self, channel_handle: str) -> None:
        self._sections = {
            section: existing_handle
            for section, existing_handle in self._sections.items()
            if existing_handle != channel_handle
        }

    def _empty_section_entities(
        self,
        channel_handle: str,
        *,
        excluding_metric: str | None = None,
    ) -> tuple[list, set[str]]:
        """Return an unowned Channel then its VMD, plus their handles."""
        channel = self.mdib.entities.by_handle(channel_handle)
        if channel is None or channel.node_type != pm.ChannelDescriptor:
            return [], set()
        if any(
            entity.handle != excluding_metric and entity.parent_handle == channel_handle
            for _, entity in self.mdib.entities.items()
        ):
            return [], set()
        entities = [channel]
        handles = {channel_handle}
        vmd_handle = channel.parent_handle
        vmd = self.mdib.entities.by_handle(vmd_handle)
        if vmd is not None and vmd.node_type == pm.VmdDescriptor:
            other_children = any(
                entity.handle != channel_handle and entity.parent_handle == vmd_handle
                for _, entity in self.mdib.entities.items()
            )
            if not other_children:
                entities.append(vmd)
                handles.add(vmd_handle)
        return entities, handles

    def _unique_handle(self, candidate: str) -> str:
        if self.mdib.entities.by_handle(candidate) is None:
            return candidate
        counter = 2
        while self.mdib.entities.by_handle(f"{candidate}.{counter}") is not None:
            counter += 1
        return f"{candidate}.{counter}"

    def _apply_spec_to_descriptor(self, descriptor, spec: MetricSpec) -> None:  # noqa: ANN001
        descriptor.Type = _coded_value(spec.effective_type(), spec.label)
        # A dimensionless metric still needs a Unit; it just gets no concept description,
        # rather than a made-up one such as "no unit".
        descriptor.Unit = _coded_value(spec.effective_unit(), spec.unit_label)
        descriptor.MetricCategory = (
            pm_types.MetricCategory.SETTING if spec.controllable else pm_types.MetricCategory.MEASUREMENT
        )
        descriptor.MetricAvailability = pm_types.MetricAvailability.INTERMITTENT

        if spec.kind is MetricKind.NUMBER or spec.is_sample_array:
            descriptor.Resolution = fixed_point_decimal(spec.resolution, "resolution")
            if spec.has_range:
                # What the metric itself can produce. Distinct from the AllowedRange we put
                # on the set operation, which is what a remote caller may ask for.
                descriptor.TechnicalRange = [
                    pm_types.Range(
                        lower=(fixed_point_decimal(spec.minimum, "minimum") if spec.minimum is not None else None),
                        upper=(fixed_point_decimal(spec.maximum, "maximum") if spec.maximum is not None else None),
                        step_width=fixed_point_decimal(spec.resolution, "resolution"),
                    ),
                ]
        if spec.kind is MetricKind.CHOICE:
            descriptor.AllowedValue = [pm_types.AllowedValue(value=value) for value in spec.allowed_values]

        if spec.kind is MetricKind.WAVEFORM:
            # An xsd:duration in seconds. sdc11073 takes a float here and renders it.
            descriptor.SamplePeriod = float(spec.sample_period)

        if spec.kind is MetricKind.DISTRIBUTION:
            # What the samples are spread *over*, as opposed to Unit, which is what each
            # sample measures. Mandatory, and the field this kind fails on first.
            descriptor.DomainUnit = _coded_value(spec.effective_domain_unit(), spec.domain_unit_label)
            descriptor.DistributionRange = pm_types.Range(
                lower=fixed_point_decimal(spec.domain_minimum, "domain_minimum"),
                upper=fixed_point_decimal(spec.domain_maximum, "domain_maximum"),
                # Not Resolution. Resolution is how finely a *sample value* is measured;
                # StepWidth is how far apart two samples sit along the domain, so it is
                # fixed by how many we send. Passing Resolution here made the descriptor
                # claim 501 samples across a 0..50 Hz domain while five were being sent.
                step_width=_domain_step(spec),
            )

    def _set_operating_mode(self, operation_handle: str, mode: pm_types.OperatingMode) -> None:
        entity = self.mdib.entities.by_handle(operation_handle)
        if entity is None:
            msg = f"operation {operation_handle!r} is not in the mdib"
            raise KeyError(msg)
        entity.state.OperatingMode = mode
        with self.mdib.operational_state_transaction() as mgr:
            mgr.write_entity(entity)
        logger.info("operation %s is now %s", operation_handle, mode)

    def _set_metric_category(self, handle: str, category: pm_types.MetricCategory) -> None:
        entity = self.mdib.entities.by_handle(handle)
        if entity is None or entity.descriptor.MetricCategory == category:
            return
        entity.descriptor.MetricCategory = category
        with self.mdib.descriptor_transaction() as mgr:
            mgr.write_entity(entity)

    def _set_allowed_range(self, operation_handle: str, spec: MetricSpec) -> None:
        """Publish the limits a remote caller must respect.

        AllowedRange lives on the operation *state*, not its descriptor, so the permitted
        window can be narrowed at runtime without a description change - the same trick
        OperatingMode uses.
        """
        if not spec.has_range:
            return
        entity = self.mdib.entities.by_handle(operation_handle)
        if entity is None:
            return
        entity.state.AllowedRange = [
            pm_types.Range(
                lower=(fixed_point_decimal(spec.minimum, "minimum") if spec.minimum is not None else None),
                upper=(fixed_point_decimal(spec.maximum, "maximum") if spec.maximum is not None else None),
                step_width=(fixed_point_decimal(spec.resolution, "resolution") if spec.resolution is not None else None),
            ),
        ]
        with self.mdib.operational_state_transaction() as mgr:
            mgr.write_entity(entity)
