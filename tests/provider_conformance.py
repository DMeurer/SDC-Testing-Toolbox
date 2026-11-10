"""Provider conformance: what the provider's MDIB says must match what the provider does.

Each check reads the provider's own MDIB and compares it with the BICEPS meaning of an
attribute, rather than with whatever the toolbox happens to write.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from script_support import Report  # noqa: E402
from sdc11073.xml_types import pm_types  # noqa: E402

from sdctoolbox import constants  # noqa: E402
from sdctoolbox.model import AlertSpec, MetricKind, MetricSpec, PatientInfo, WaveformShape  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402
from sdctoolbox.sample_generation import DemoSampleGenerator  # noqa: E402

MONITORED = pm_types.AlertConditionMonitoredLimits
ASSOCIATED = pm_types.ContextAssociation.ASSOCIATED
LATE = 0.011  # how far a timed wait typically overshoots on a desktop OS


def check_limit_alarms_declare_the_limits_they_monitor(report: Report) -> None:
    """``None`` means that no limit is monitored, which is the opposite of what a limit alarm does."""
    cases = (
        ("two-sided", Decimal(10), Decimal(90), MONITORED.ALL_ON),
        ("upper-only", None, Decimal(90), MONITORED.LOW_OFF),
        ("lower-only", Decimal(10), None, MONITORED.HIGH_OFF),
    )
    service = ProviderService(instance_name="limit-alarm-conformance")
    try:
        service.start()
        metric = service.add_metric(MetricSpec(label="Pressure", kind=MetricKind.NUMBER, initial_value=Decimal(50)))
        for name, lower, upper, expected in cases:
            handle = service.add_alert(
                AlertSpec(label=f"{name} limit", source_handle=metric, lower_limit=lower, upper_limit=upper),
            )
            state = service.mdib.entities.by_handle(handle).state
            report.check(
                state.MonitoredAlertLimits == expected,
                f"a {name} limit alarm declares MonitoredAlertLimits {expected.value}",
                str(state.MonitoredAlertLimits),
            )
    finally:
        service.stop()


def check_patient_is_bound_to_the_version_that_introduced_it(report: Report) -> None:
    """A consumer tells from BindingMdibVersion since which report a patient is associated."""
    service = ProviderService(instance_name="patient-binding-conformance")
    try:
        service.start()
        previous: str | None = None
        for given in ("Ada", "Grace"):
            service.set_patient(PatientInfo(given_name=given, family_name="Tester"))
            introduced_in = service.mdib.mdib_version
            states = list(service.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE).states.values())
            current = next(state for state in states if state.ContextAssociation == ASSOCIATED)
            report.check(
                current.BindingMdibVersion == introduced_in,
                f"{given} is bound to the MDIB version that introduced the association",
                f"binding {current.BindingMdibVersion}, introduced in {introduced_in}",
            )
            if previous is not None:
                old = next(state for state in states if state.Handle == previous)
                report.check(
                    old.UnbindingMdibVersion == introduced_in,
                    "the replaced patient is unbound by the same version",
                    f"unbinding {old.UnbindingMdibVersion}, introduced in {introduced_in}",
                )
            previous = current.Handle
    finally:
        service.stop()


def check_sample_streams_keep_their_declared_rate(report: Report) -> None:
    """A wake-up that is a few milliseconds late must not push every later block back."""
    clock = [1_000.0]
    monotonic = [50.0]
    service = ProviderService(instance_name="sample-rate-conformance")
    try:
        service.start()
        service.stop_generator()
        generator = DemoSampleGenerator(
            lambda: service.mdib,
            lambda: service._specs,  # noqa: SLF001
            service._lock,  # noqa: SLF001
            "sample-rate",
            clock=lambda: clock[0],
            monotonic=lambda: monotonic[0],
        )
        service._sample_generator = generator  # noqa: SLF001
        with patch.object(service, "start_generator", lambda: None):
            wave = service.add_metric(
                MetricSpec(
                    label="Declared rate",
                    kind=MetricKind.WAVEFORM,
                    sample_period=Decimal("0.1"),
                    shape=WaveformShape.SAWTOOTH,
                ),
            )
            frames = service.add_metric(
                MetricSpec(
                    label="Declared cadence",
                    kind=MetricKind.DISTRIBUTION,
                    minimum=Decimal("-10"),
                    maximum=Decimal("10"),
                    domain_minimum=Decimal("100"),
                    domain_maximum=Decimal("410"),
                ),
            )
        spec = service._specs[wave]  # noqa: SLF001
        block, _ = generator.next_block(wave, spec)
        block_seconds = len(block) * float(spec.sample_period)
        frame_seconds = constants.WAVEFORM_BLOCK_SECONDS

        def versions(handle: str) -> int:
            return service.mdib.entities.by_handle(handle).state.StateVersion

        def run(seconds: float) -> tuple[int, int]:
            """Wake the generator as its worker does, each time a little late; count what it sent."""
            started = monotonic[0]
            first = versions(wave), versions(frames)
            while monotonic[0] - started < seconds:
                generator.publish_once(force=False)
                step = generator.seconds_until_next_block() + LATE
                monotonic[0] += step
                clock[0] += step
            return versions(wave) - first[0], versions(frames) - first[1]

        elapsed = 120.0
        blocks, frame_count = run(elapsed)
        delivered = blocks * block_seconds
        report.check(
            abs(delivered - elapsed) <= 2 * block_seconds,
            "the waveform samples delivered in two minutes cover two minutes",
            f"{delivered:.1f} s of samples in {elapsed:.0f} s",
        )
        report.check(
            abs(frame_count * frame_seconds - elapsed) <= 2 * frame_seconds,
            "a distribution is refreshed at its declared cadence for two minutes",
            f"{frame_count} frames in {elapsed:.0f} s, {elapsed / frame_seconds:.0f} expected",
        )
        behind = clock[0] - generator.next_waveform_times[wave]
        report.check(
            abs(behind) <= 1.0,
            "the sample timestamps still follow the wall clock after two minutes",
            f"{behind:+.2f} s",
        )

        clock[0] += 30.0
        monotonic[0] += 30.0
        burst, _ = run(1.0)
        report.check(
            burst <= 2 * round(1.0 / block_seconds),
            "a long stall is not made up for with a burst of blocks",
            f"{burst} blocks in the second after the stall",
        )
        behind = clock[0] - generator.next_waveform_times[wave]
        report.check(
            abs(behind) <= 1.0,
            "the sample timestamps are back on the wall clock after the stall",
            f"{behind:+.2f} s",
        )
    finally:
        service.stop()


def main() -> int:
    report = Report()
    check_limit_alarms_declare_the_limits_they_monitor(report)
    check_patient_is_bound_to_the_version_that_introduced_it(report)
    check_sample_streams_keep_their_declared_rate(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
