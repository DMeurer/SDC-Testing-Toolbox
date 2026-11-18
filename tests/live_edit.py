"""Editing a running provider: only states and toolbox behaviour change, never descriptors.

BICEPS keeps what is technically possible in the descriptor and what is active now in the
state. Every edit here must leave the descriptor and its DescriptorVersion alone and must
refuse a state that would leave its descriptor's bounds.

Exit code 0 means all checks passed.

Usage:  .venv/Scripts/python.exe tests/live_edit.py
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report  # noqa: E402
from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import msg_types  # noqa: E402

from sdctoolbox.model import (  # noqa: E402
    AlertPriority,
    AlertSpec,
    DistributionShape,
    MetricKind,
    MetricSpec,
    WaveformShape,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402


def raises(call, *args) -> bool:  # noqa: ANN001
    try:
        call(*args)
    except ValueError:
        return True
    return False


def remote_set(service: ProviderService, metric: str, value: object) -> bool:
    """Run the registered operation handler the way a consumer's request would."""
    operation_handle = service.operation_handle_for(metric)
    operation = service._sco_for_edits()._registered_operations[operation_handle]  # noqa: SLF001
    params = SimpleNamespace(operation_instance=operation, operation_request=SimpleNamespace(argument=value))
    return service._handler(params).invocation_state is msg_types.InvocationState.FINISHED  # noqa: SLF001


def descriptor_version(service: ProviderService, handle: str) -> int:
    return service.mdib.entities.by_handle(handle).descriptor.DescriptorVersion


def operation_state(service: ProviderService, metric: str):  # noqa: ANN201 - an sdc11073 state
    return service.mdib.entities.by_handle(service.operation_handle_for(metric)).state


def check_allowed_range(report: Report, service: ProviderService) -> None:
    metric = service.add_metric(
        MetricSpec("Live range", MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(100),
                   controllable=True, initial_value=Decimal(10)),
    )
    service.enable_control(metric)
    metric_version = descriptor_version(service, metric)
    operation_version = descriptor_version(service, service.operation_handle_for(metric))

    service.set_allowed_range(metric, Decimal(20), Decimal(40))
    allowed = operation_state(service, metric).AllowedRange[0]
    report.check((allowed.Lower, allowed.Upper) == (20, 40), "the operation's AllowedRange becomes 20..40",
                 f"{allowed.Lower}..{allowed.Upper}")
    technical = service.mdib.entities.by_handle(metric).descriptor.TechnicalRange[0]
    report.check((technical.Lower, technical.Upper) == (0, 100), "the TechnicalRange stays 0..100")
    report.check(
        descriptor_version(service, metric) == metric_version
        and descriptor_version(service, service.operation_handle_for(metric)) == operation_version,
        "no descriptor version changes",
    )
    report.check(service.list_metrics()[metric].effective_allowed_range() == (20, 40),
                 "the spec remembers the allowed range")
    report.check(not remote_set(service, metric, Decimal(50)), "a remote 50 is refused")
    report.check(remote_set(service, metric, Decimal(30)), "a remote 30 is accepted")
    service.set_value(metric, Decimal(50))
    report.check(service.get_value(metric) == 50, "the local operator may still set 50")

    for lower, upper in ((Decimal(-1), Decimal(40)), (Decimal(40), Decimal(20)), (None, Decimal(101))):
        report.check(raises(service.set_allowed_range, metric, lower, upper),
                     f"an allowed range {lower}..{upper} is refused")
    allowed = operation_state(service, metric).AllowedRange[0]
    report.check((allowed.Lower, allowed.Upper) == (20, 40), "a refused range leaves the state alone")

    service.disable_control(metric)
    service.enable_control(metric)
    allowed = operation_state(service, metric).AllowedRange[0]
    report.check((allowed.Lower, allowed.Upper) == (20, 40), "switching control off and on keeps 20..40")

    open_metric = service.add_metric(
        MetricSpec("Live open range", MetricKind.NUMBER, minimum=Decimal(0), controllable=True),
    )
    service.enable_control(open_metric)
    service.set_allowed_range(open_metric, Decimal(5), Decimal(500))
    report.check(service.list_metrics()[open_metric].effective_allowed_range() == (5, 500),
                 "an open technical side may be narrowed to a finite bound")
    report.check(raises(service.set_allowed_range, open_metric, Decimal(-5), None),
                 "the closed technical side may not be widened")

    unbounded = service.add_metric(MetricSpec("Live unbounded", MetricKind.NUMBER, controllable=True))
    service.enable_control(unbounded)
    service.set_allowed_range(unbounded, Decimal(20), Decimal(40))
    service.set_allowed_range(unbounded, None, None)
    report.check(not operation_state(service, unbounded).AllowedRange and remote_set(service, unbounded, Decimal(50)),
                 "clearing the allowed range of an unbounded metric removes it from the state")
    service.remove_metric(unbounded)

    measured = service.add_metric(MetricSpec("Live measured", MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(9)))
    report.check(raises(service.set_allowed_range, measured, Decimal(1), Decimal(2)),
                 "a metric without remote control has no allowed range to edit")
    for handle in (metric, open_metric, measured):
        service.remove_metric(handle)


def check_allowed_values(report: Report, service: ProviderService) -> None:
    choice = service.add_metric(
        MetricSpec("Live choice", MetricKind.CHOICE, allowed_values=("A", "B", "C"), controllable=True,
                   initial_value="A"),
    )
    service.enable_control(choice)
    version = descriptor_version(service, choice)
    service.set_allowed_values(choice, ["A", "B"])
    values = operation_state(service, choice).AllowedValues
    report.check(values is not None and list(values.Value) == ["A", "B"],
                 "the operation's AllowedValues become A, B", str(values))
    report.check([v.Value for v in service.mdib.entities.by_handle(choice).descriptor.AllowedValue] == ["A", "B", "C"]
                 and descriptor_version(service, choice) == version, "the metric still offers A, B, C")
    report.check(not remote_set(service, choice, "C"), "a remote C is refused")
    report.check(remote_set(service, choice, "B"), "a remote B is accepted")
    report.check(raises(service.set_allowed_values, choice, ["D"]), "a value the metric lacks is refused")
    report.check(raises(service.set_allowed_values, choice, []), "an empty set is refused")
    service.disable_control(choice)
    service.enable_control(choice)
    report.check(list(operation_state(service, choice).AllowedValues.Value) == ["A", "B"],
                 "switching control off and on keeps A, B")
    service.remove_metric(choice)


def check_alert_limits(report: Report, service: ProviderService) -> None:
    rate = service.add_metric(MetricSpec("Live rate", MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(300),
                                         initial_value=Decimal(100)))
    alarm = service.add_alert(AlertSpec("Live rate alarm", rate, lower_limit=Decimal(60), upper_limit=Decimal(120),
                                        max_lower_limit=Decimal(0), max_upper_limit=Decimal(300)))
    entity = service.mdib.entities.by_handle(alarm)
    report.check((entity.descriptor.MaxLimits.Lower, entity.descriptor.MaxLimits.Upper) == (0, 300),
                 "MaxLimits come from the max limits, not the limits")
    version = entity.descriptor.DescriptorVersion
    report.check(not service.alert_present(alarm), "100 is inside 60..120")

    service.set_alert_limits(alarm, Decimal(60), Decimal(90))
    entity = service.mdib.entities.by_handle(alarm)
    report.check(service.alert_present(alarm), "narrowing to 60..90 raises the alarm at once")
    report.check((entity.state.Limits.Lower, entity.state.Limits.Upper) == (60, 90), "the state Limits are 60..90")
    report.check((entity.descriptor.MaxLimits.Lower, entity.descriptor.MaxLimits.Upper) == (0, 300)
                 and entity.descriptor.DescriptorVersion == version, "MaxLimits and the descriptor stay")

    service.set_alert_limits(alarm, None, Decimal(150))
    entity = service.mdib.entities.by_handle(alarm)
    report.check(str(entity.state.MonitoredAlertLimits) == "LoOff" or entity.state.MonitoredAlertLimits.value == "LoOff",
                 "an upper-only limit switches the low limit off", str(entity.state.MonitoredAlertLimits))
    report.check(not service.alert_present(alarm), "widening to ..150 clears it again")
    for lower, upper in ((Decimal(-5), Decimal(90)), (None, None), (Decimal(90), Decimal(60)), (None, Decimal(301))):
        report.check(raises(service.set_alert_limits, alarm, lower, upper), f"limits {lower}..{upper} are refused")
    report.check(service.list_alerts()[alarm].upper_limit == 150, "a refused change keeps the last limits")

    made_in_dialog = service.add_alert(AlertSpec("Live dialog alarm", rate, lower_limit=Decimal(60), upper_limit=Decimal(120)))
    max_limits = service.mdib.entities.by_handle(made_in_dialog).descriptor.MaxLimits
    report.check((max_limits.Lower, max_limits.Upper) == (0, 300),
                 "without max limits, MaxLimits default to the source's technical range")
    service.set_alert_limits(made_in_dialog, Decimal(50), Decimal(200))
    report.check(service.list_alerts()[made_in_dialog].lower_limit == 50, "so its limits can widen, too")

    plain = service.add_alert(AlertSpec("Live plain alarm", rate))
    report.check(raises(service.set_alert_limits, plain, Decimal(1), Decimal(2)), "a plain alarm has no limits to edit")

    service.set_alert_priority(alarm, AlertPriority.HIGH)
    entity = service.mdib.entities.by_handle(alarm)
    report.check(entity.state.ActualPriority == AlertPriority.HIGH, "the state ActualPriority becomes Hi",
                 str(entity.state.ActualPriority))
    report.check(entity.descriptor.Priority == AlertPriority.MEDIUM and entity.descriptor.DescriptorVersion == version,
                 "the descriptor Priority stays Me")
    report.check(service.list_alerts()[alarm].actual_priority is AlertPriority.HIGH, "the spec remembers it")
    service.remove_metric(rate)


def check_metric_activation(report: Report, service: ProviderService) -> None:
    rate = service.add_metric(MetricSpec("Live switch", MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(300),
                                         controllable=True, initial_value=Decimal(200)))
    service.enable_control(rate)
    alarm = service.add_alert(AlertSpec("Live switch alarm", rate, upper_limit=Decimal(120)))
    report.check(service.alert_present(alarm), "200 raises the alarm")
    version = descriptor_version(service, rate)

    service.set_metric_active(rate, active=False)
    state = service.mdib.entities.by_handle(rate).state
    report.check(state.ActivationState == "Off", "the metric's ActivationState becomes Off", str(state.ActivationState))
    report.check(descriptor_version(service, rate) == version, "its descriptor stays")
    report.check(not service.list_metrics()[rate].active, "the spec remembers it")
    report.check(not service.alert_present(alarm), "an alarm on a switched-off metric is not present")
    report.check(not remote_set(service, rate, Decimal(10)), "a remote set on a switched-off metric is refused")
    service.set_value(rate, Decimal(250))
    report.check(service.get_value(rate) == 250 and not service.alert_present(alarm),
                 "the local operator may still set it, without raising the alarm")
    report.check(service.mdib.entities.by_handle(rate).state.ActivationState == "Off"
                 and not remote_set(service, rate, Decimal(10)),
                 "a local set leaves it Off, so remote sets stay refused")

    service.set_metric_active(rate, active=True)
    report.check(service.mdib.entities.by_handle(rate).state.ActivationState == "On", "On again")
    from sdctoolbox import native_mdib  # noqa: PLC0415
    from sdc11073.xml_types import pm_types  # noqa: PLC0415
    entity = service.mdib.entities.by_handle(rate)
    entity.state.ActivationState = pm_types.ComponentActivation.STANDBY
    with service.mdib.metric_state_transaction() as manager:
        manager.write_entity(entity)
    report.check(remote_set(service, rate, Decimal(250)), "a StndBy metric (as files may have) still takes remote sets")
    report.check(native_mdib.metric_specs(service.mdib)[rate].active, "and loads as switched on, as before")
    entity = service.mdib.entities.by_handle(rate)
    entity.state.ActivationState = pm_types.ComponentActivation.ON
    with service.mdib.metric_state_transaction() as manager:
        manager.write_entity(entity)
    report.check(service.alert_present(alarm), "switched on, the alarm follows the value again")
    report.check(remote_set(service, rate, Decimal(10)), "and remote sets work again")
    service.remove_metric(rate)

    wave = service.add_metric(MetricSpec("Live switch wave", MetricKind.WAVEFORM, minimum=Decimal(0), maximum=Decimal(1)))
    other = service.add_metric(MetricSpec("Live other wave", MetricKind.WAVEFORM, minimum=Decimal(0), maximum=Decimal(1)))
    service.stop_generator()
    service.set_metric_active(wave, active=False)
    report.check(service.mdib.entities.by_handle(wave).state.ActivationState == "Off", "a waveform can be switched off")
    before = (service.get_samples(wave), service.get_samples(other))
    service._sample_generator.publish_once()  # noqa: SLF001 - one block, deterministically
    report.check(service.get_samples(wave) == before[0], "a switched-off waveform gets no new samples")
    service.set_samples(wave, [Decimal("0.5")] * 5)
    report.check(service.mdib.entities.by_handle(wave).state.ActivationState == "Off",
                 "samples set by hand leave a switched-off waveform Off")
    report.check(service.get_samples(other) != before[1], "the other waveform still does")
    service.set_metric_active(wave, active=True)
    service._sample_generator.publish_once()  # noqa: SLF001
    report.check(service.get_samples(wave) != before[0], "switched on, it is generated again")
    service.remove_metric(wave)
    service.remove_metric(other)


def check_generator(report: Report, service: ProviderService) -> None:
    wave = service.add_metric(MetricSpec("Live curve", MetricKind.WAVEFORM, minimum=Decimal(0), maximum=Decimal(10)))
    version = descriptor_version(service, wave)
    period = service.mdib.entities.by_handle(wave).descriptor.SamplePeriod
    service.set_generator(wave, shape=WaveformShape.SQUARE)
    spec = service.list_metrics()[wave]
    block, _ = service._next_block(wave, spec)  # noqa: SLF001
    report.check(spec.shape is WaveformShape.SQUARE and set(block) <= {Decimal(0), Decimal(10)},
                 "a square curve only reaches the two extremes", str(sorted(set(block))))
    service.set_generator(wave, cycle_seconds=Decimal(8))
    report.check(service.list_metrics()[wave].cycle_seconds == 8
                 and service.list_metrics()[wave].shape is WaveformShape.SQUARE, "the cycle changes, the curve stays")
    report.check(_raises_kw(service, wave, cycle_seconds=Decimal("0.1")),
                 "a cycle shorter than two samples is refused")
    report.check(_raises_kw(service, wave, distribution_shape=DistributionShape.FLAT),
                 "a distribution curve on a waveform is refused")
    report.check(descriptor_version(service, wave) == version
                 and service.mdib.entities.by_handle(wave).descriptor.SamplePeriod == period,
                 "the descriptor and its SamplePeriod stay")
    service.remove_metric(wave)


def check_cycle_is_time_based(report: Report, service: ProviderService) -> None:
    """The sample period sets the resolution of a curve, never its speed."""
    slow = service.add_metric(MetricSpec("Live slow", MetricKind.WAVEFORM, minimum=Decimal(0), maximum=Decimal(10),
                                         sample_period=Decimal("0.1"), cycle_seconds=Decimal(2)))
    fast = service.add_metric(MetricSpec("Live fast", MetricKind.WAVEFORM, minimum=Decimal(0), maximum=Decimal(10),
                                         sample_period=Decimal("0.05"), cycle_seconds=Decimal(2)))
    slow_block, _ = service._next_block(slow, service.list_metrics()[slow])  # noqa: SLF001
    fast_block, _ = service._next_block(fast, service.list_metrics()[fast])  # noqa: SLF001
    same_moments = all(abs(fast_block[2 * index] - sample) <= Decimal("0.1") for index, sample in enumerate(slow_block))
    report.check(same_moments, "a sine at 0.05 s per sample matches the 0.1 s one at the same moments",
                 f"{slow_block} vs {fast_block[::2]}")
    service.remove_metric(slow)
    service.remove_metric(fast)


def _raises_kw(service: ProviderService, handle: str, **changes: object) -> bool:
    try:
        service.set_generator(handle, **changes)
    except ValueError:
        return True
    return False


def main() -> int:
    basic_logging_setup(level=logging.ERROR)
    report = Report()
    print("=" * 74)
    print("Live provider edits")
    print("=" * 74)
    service = ProviderService(instance_name="live-edit")
    service.start()
    try:
        check_allowed_range(report, service)
        check_allowed_values(report, service)
        check_alert_limits(report, service)
        check_metric_activation(report, service)
        check_generator(report, service)
        check_cycle_is_time_based(report, service)
    finally:
        service.stop()
    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
