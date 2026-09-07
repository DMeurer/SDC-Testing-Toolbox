"""End-to-end acceptance test for the headless core.

Spawns acceptance_provider.py as a separate process, connects to it as a consumer and checks
that the whole loop works:

* a provider is discovered, carries an XAddr and publishes its location as a scope
* metrics created before we connected arrive with kind, unit and label intact
* all three controllable kinds can be set remotely and the new value comes back
* a value outside AllowedValue is refused and leaves the old value in place
* a control with OperatingMode Dis is refused and leaves the old value in place
* a metric created at runtime shows up via new_descriptors_by_handle and is immediately
  controllable, without reconnecting

Two processes rather than two threads on purpose: WS-Discovery, the HTTP servers and the
subscription machinery all behave differently in-process, and the tool is meant to be run
twice on one machine anyway.

Exit code 0 means all checks passed.

Usage:  .venv/Scripts/python.exe tests/acceptance_core.py
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acceptance_provider import (  # noqa: E402
    ADD_LATE_COMMAND,
    COMMAND_DONE_PREFIX,
    DIST,
    HOME_ACTION,
    INVALID_CHOICE_ACTION,
    INVALID_EFFECT_ACTION,
    LATE,
    LIMIT_ALARM,
    LOCKED,
    MANUAL_ALARM,
    MISSING_EFFECT_ACTION,
    MODE,
    NOTE,
    PEER_INSTANCE,
    SAW,
    SAW_CYCLE,
    UPDATED_PATIENT,
    UPDATE_CONTEXT_COMMAND,
    WAVE,
    ZOOM,
)
from script_support import (  # noqa: E402
    CallbackRecorder,
    ProcessOutput,
    Report,
    stop_process,
    wait_for_output_line,
    wait_for_ready,
)
from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import msg_types  # noqa: E402

from sdctoolbox import constants  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.model import MetricKind, patient_info_from_biceps  # noqa: E402

FINISHED = (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD)
REPORT_TIMEOUT = 20.0


def copy_metric_values(values: object) -> dict[str, object]:
    return {
        handle: getattr(getattr(state, "MetricValue", None), "Value", None)
        for handle, state in dict(values).items()
    }


def copy_waveform_blocks(values: object) -> dict[str, tuple[Decimal, ...]]:
    return {
        handle: tuple(getattr(getattr(state, "MetricValue", None), "Samples", None) or ())
        for handle, state in dict(values).items()
    }


def copy_descriptor_types(values: object) -> dict[str, str]:
    return {
        handle: getattr(getattr(descriptor, "NODETYPE", None), "localname", "")
        for handle, descriptor in dict(values).items()
    }


def copy_operation_modes(values: object) -> dict[str, str]:
    return {
        handle: str(getattr(state, "OperatingMode", ""))
        for handle, state in dict(values).items()
    }


def copy_contexts(values: object) -> dict[str, object]:
    return {
        handle: patient_info_from_biceps(getattr(state, "CoreData", None))
        for handle, state in dict(values).items()
        if getattr(state, "CoreData", None) is not None
    }


def copy_alert_states(values: object) -> dict[str, dict[str, object]]:
    copied = {}
    for handle, state in dict(values).items():
        copied[handle] = {
            "type": getattr(getattr(state, "NODETYPE", None), "localname", ""),
            "presence": str(getattr(state, "Presence", "")),
            "technical": tuple(getattr(state, "PresentTechnicalAlarmConditions", None) or ()),
            "physiological": tuple(getattr(state, "PresentPhysiologicalAlarmConditions", None) or ()),
        }
    return copied


def send_provider_command(process: subprocess.Popen[str], command: str) -> None:
    if process.stdin is None or process.poll() is not None:
        raise RuntimeError(f"provider unavailable while sending {command!r}")
    process.stdin.write(f"{command}\n")
    process.stdin.flush()


def main() -> int:  # noqa: PLR0915 - a linear test script reads better in one piece
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", default=constants.DEFAULT_IP)
    args = parser.parse_args()

    basic_logging_setup(level=logging.WARNING)
    report = Report()

    print("=" * 74)
    print("Core acceptance")
    print("=" * 74)
    print("Starting provider process ...", flush=True)

    process = subprocess.Popen(  # noqa: S603
        [sys.executable, str(ROOT / "tests" / "acceptance_provider.py"), "--ip", args.ip],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    output = ProcessOutput(process)

    try:
        if not wait_for_ready(output, timeout=45):
            print("FAIL: provider never reported READY")
            return 1

        # ---------------------------------------------------------------- discovery
        print("\n1. Discovery and connection", flush=True)
        with ConsumerService(ip=args.ip) as consumer_service:
            expected_epr = constants.epr_for(PEER_INSTANCE).urn
            devices = []
            device = None
            for _attempt in range(5):
                devices = consumer_service.scan(
                    timeout=5.0,
                    expected=99,
                )
                device = next((candidate for candidate in devices if candidate.epr == expected_epr), None)
                if device is not None:
                    break
            if not report.check(bool(devices), "provider discovered"):
                return report.summary()
            if not report.check(device is not None, "the acceptance provider is selected", expected_epr):
                return report.summary()
            report.check(bool(device.x_addrs), "discovery hit carries an XAddr", device.epr)
            report.check(
                device.location_scope is not None,
                "location context published as a discovery scope",
                device.location_scope or "none",
            )

            remote = consumer_service.connect(device)

            patient = remote.patient()
            report.check(
                patient.given_name == "Ada"
                and patient.family_name == "Lovelace"
                and patient.height is not None
                and patient.height.value == Decimal("170.5")
                and patient.height.unit.code == "demo-cm"
                and patient.weight is not None
                and patient.weight.value == Decimal("72.4")
                and patient.weight.unit.code == "demo-kg"
                and patient.race is not None
                and patient.race.code == "demo-race",
                "patient demographics arrive as BICEPS measurements and coded race",
                patient.summary(),
            )

            metric_reports = CallbackRecorder(copy_metric_values)
            descriptor_reports = CallbackRecorder(copy_descriptor_types)
            operation_reports = CallbackRecorder(copy_operation_modes)
            context_reports = CallbackRecorder(copy_contexts)
            alert_reports = CallbackRecorder(copy_alert_states)
            waveform_reports = CallbackRecorder(copy_waveform_blocks)

            remote.bind(
                new_descriptors_by_handle=descriptor_reports,
                metrics_by_handle=metric_reports,
                operation_by_handle=operation_reports,
                context_by_handle=context_reports,
                alert_by_handle=alert_reports,
                waveform_by_handle=waveform_reports,
            )

            # ------------------------------------------------------ initial metrics
            print("\n2. Metrics created before we connected", flush=True)
            metrics = remote.metrics()
            for handle, expected_kind in [
                (ZOOM, MetricKind.NUMBER),
                (MODE, MetricKind.CHOICE),
                (NOTE, MetricKind.TEXT),
                (LOCKED, MetricKind.NUMBER),
            ]:
                metric = metrics.get(handle)
                report.check(metric is not None, f"{handle} present")
                if metric is not None:
                    report.check(metric.kind is expected_kind, f"{handle} is {expected_kind.value}")

            mode_metric = metrics.get(MODE)
            if mode_metric is not None:
                report.check(
                    mode_metric.allowed_values == ("IDLE", "RUN", "PAUSE"),
                    "choice metric exposes its AllowedValue list",
                    str(mode_metric.allowed_values),
                )

            zoom_metric = metrics.get(ZOOM)
            if zoom_metric is not None:
                report.check(zoom_metric.controllable, "zoom has a set operation")
                report.check(zoom_metric.controllable_now, "zoom control is enabled")
                report.check(
                    zoom_metric.unit_label == "steps",
                    "unit label survived the round trip",
                    str(zoom_metric.unit_label),
                )
                report.check(
                    zoom_metric.label == "Zoom level",
                    "concept description survived the round trip",
                    str(zoom_metric.label),
                )
                report.check(
                    (zoom_metric.minimum, zoom_metric.maximum) == (Decimal("1"), Decimal("100")),
                    "the peer publishes the range a caller must respect",
                    f"{zoom_metric.minimum} to {zoom_metric.maximum}",
                )
                report.check(
                    (zoom_metric.technical_minimum, zoom_metric.technical_maximum)
                    == (Decimal("1"), Decimal("100")),
                    "and the metric's own TechnicalRange",
                    f"{zoom_metric.technical_minimum} to {zoom_metric.technical_maximum}",
                )

            locked_metric = metrics.get(LOCKED)
            if locked_metric is not None:
                report.check(locked_metric.controllable, "locked setting has a set operation")
                report.check(
                    not locked_metric.controllable_now,
                    "locked setting reports OperatingMode Dis",
                )

            # ------------------------------------------------------- remote control
            print("\n3. Remote control", flush=True)
            cursor = metric_reports.cursor()
            state = remote.set_value(ZOOM, "7")
            report.check(state in FINISHED, "setting a numeric string finishes", str(state))
            metric_event = metric_reports.wait_for(
                lambda payload: payload.get(ZOOM) == Decimal("7"),
                after=cursor,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                metric_event is not None,
                "the numeric value arrives in a metric report",
                str(metric_reports.history()),
            )

            cursor = metric_reports.cursor()
            state = remote.set_value(MODE, "RUN")
            report.check(state in FINISHED, "setting a choice value finishes", str(state))
            metric_event = metric_reports.wait_for(
                lambda payload: payload.get(MODE) == "RUN",
                after=cursor,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                metric_event is not None,
                "the choice value arrives in a metric report",
                str(metric_reports.history()),
            )

            cursor = metric_reports.cursor()
            state = remote.set_value(NOTE, "checked at 10:00")
            report.check(state in FINISHED, "setting a text value finishes", str(state))
            report.check(
                metric_reports.wait_for(
                    lambda payload: payload.get(NOTE) == "checked at 10:00",
                    after=cursor,
                    timeout=REPORT_TIMEOUT,
                )
                is not None,
                "the text value arrives in a metric report",
                str(metric_reports.history()),
            )

            # ------------------------------------------------------------ rejections
            print("\n4. Rejections", flush=True)
            cursor = metric_reports.cursor()
            state = remote.set_value(MODE, "NOT_A_MODE")
            report.check(
                state is msg_types.InvocationState.FAILED,
                "value outside AllowedValue is rejected",
                str(state),
            )
            barrier = metric_reports.cursor()
            barrier_state = remote.set_value(NOTE, "barrier-invalid-mode")
            barrier_event = metric_reports.wait_for(
                lambda payload: payload.get(NOTE) == "barrier-invalid-mode",
                after=barrier,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                barrier_state in FINISHED and barrier_event is not None,
                "a later known-good metric report forms the rejection barrier",
                str(metric_reports.history()),
            )
            report.check(
                not any(event.payload.get(MODE) == "NOT_A_MODE" for event in metric_reports.events_after(cursor)),
                "rejected write delivered no prohibited choice value",
                str(metric_reports.events_after(cursor)),
            )

            cursor = metric_reports.cursor()
            state = remote.set_value(LOCKED, Decimal("9"))
            report.check(
                state is msg_types.InvocationState.FAILED,
                "write to a disabled control is rejected",
                str(state),
            )
            barrier = metric_reports.cursor()
            barrier_state = remote.set_value(NOTE, "barrier-disabled")
            barrier_event = metric_reports.wait_for(
                lambda payload: payload.get(NOTE) == "barrier-disabled",
                after=barrier,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                barrier_state in FINISHED and barrier_event is not None,
                "a later report forms the disabled-control barrier",
            )
            report.check(
                not any(event.payload.get(LOCKED) == Decimal("9") for event in metric_reports.events_after(cursor)),
                "disabled control delivered no prohibited value",
                str(metric_reports.events_after(cursor)),
            )

            cursor = metric_reports.cursor()
            state = remote.set_value(ZOOM, Decimal("500"))
            report.check(
                state is msg_types.InvocationState.FAILED,
                "a value above the maximum is rejected",
                str(state),
            )
            state = remote.set_value(ZOOM, Decimal("0"))
            report.check(
                state is msg_types.InvocationState.FAILED,
                "a value below the minimum is rejected",
                str(state),
            )
            barrier = metric_reports.cursor()
            barrier_state = remote.set_value(NOTE, "barrier-range")
            barrier_event = metric_reports.wait_for(
                lambda payload: payload.get(NOTE) == "barrier-range",
                after=barrier,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                barrier_state in FINISHED and barrier_event is not None,
                "a later report forms the out-of-range barrier",
            )
            report.check(
                not any(
                    event.payload.get(ZOOM) in {Decimal("500"), Decimal("0")}
                    for event in metric_reports.events_after(cursor)
                ),
                "out-of-range writes delivered no prohibited value",
                str(metric_reports.events_after(cursor)),
            )
            metric_cursor = metric_reports.cursor()
            alert_cursor = alert_reports.cursor()
            state = remote.set_value(ZOOM, Decimal("100"))
            report.check(
                state in FINISHED,
                "the maximum itself is accepted",
                str(state),
            )
            report.check(
                metric_reports.wait_for(
                    lambda payload: payload.get(ZOOM) == Decimal("100"),
                    after=metric_cursor,
                    timeout=REPORT_TIMEOUT,
                )
                is not None,
                "the accepted boundary value arrives in a metric report",
                str(metric_reports.history()),
            )
            raised_alert_event = alert_reports.wait_for(
                lambda payload: payload.get(LIMIT_ALARM, {}).get("presence") == "True"
                and payload.get("sig.zoom_out_of_range.vis", {}).get("presence") == "On"
                and payload.get("sig.zoom_out_of_range.aud", {}).get("presence") == "On",
                after=alert_cursor,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                raised_alert_event is not None,
                "the condition and both signals are synchronized in one report",
                str(alert_reports.history()),
            )

            # ------------------------------------------- runtime descriptor creation
            print("\n5. Data source created at runtime", flush=True)
            descriptor_cursor = descriptor_reports.cursor()
            metric_cursor = metric_reports.cursor()
            operation_cursor = operation_reports.cursor()
            send_provider_command(process, ADD_LATE_COMMAND)
            late_operation = constants.OPERATION_HANDLE_PREFIX + LATE.removeprefix(
                constants.METRIC_HANDLE_PREFIX,
            )
            descriptor_event = descriptor_reports.wait_for(
                lambda payload: LATE in payload,
                after=descriptor_cursor,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                descriptor_event is not None,
                "new_descriptors_by_handle fired for the late metric",
                str(descriptor_reports.history()),
            )
            report.check(
                descriptor_reports.wait_for(
                    lambda payload: late_operation in payload,
                    after=descriptor_cursor,
                    timeout=REPORT_TIMEOUT,
                )
                is not None,
                "the late metric's set operation arrives by descriptor report",
                str(descriptor_reports.history()),
            )
            report.check(
                metric_reports.wait_for(
                    lambda payload: payload.get(LATE) == Decimal("42"),
                    after=metric_cursor,
                    timeout=REPORT_TIMEOUT,
                )
                is not None,
                "the late metric's initial state arrives by metric report",
                str(metric_reports.history()),
            )
            report.check(
                operation_reports.wait_for(
                    lambda payload: late_operation in payload,
                    after=operation_cursor,
                    timeout=REPORT_TIMEOUT,
                )
                is not None,
                "the late metric's operation state arrives by operation report",
                str(operation_reports.history()),
            )
            report.check(
                wait_for_output_line(
                    output,
                    f"{COMMAND_DONE_PREFIX} {ADD_LATE_COMMAND}",
                    REPORT_TIMEOUT,
                ),
                "the provider confirms completion of the late-metric command",
                output.buffered_output,
            )
            metrics = remote.metrics()
            late_metric = metrics.get(LATE)
            report.check(late_metric is not None, f"{LATE} appeared in the consumer MDIB")
            if late_metric is not None:
                report.check(late_metric.kind is MetricKind.NUMBER, f"{LATE} is a number")
                report.check(
                    late_metric.controllable_now,
                    f"{LATE} is controllable without reconnecting",
                )
                cursor = metric_reports.cursor()
                state = remote.set_value(LATE, Decimal("99"))
                report.check(
                    state in FINISHED,
                    "controlling the runtime-created metric finishes",
                    str(state),
                )
                report.check(
                    metric_reports.wait_for(
                        lambda payload: payload.get(LATE) == Decimal("99"),
                        after=cursor,
                        timeout=REPORT_TIMEOUT,
                    )
                    is not None,
                    "runtime-created metric reports the new value",
                    str(metric_reports.history()),
                )

            context_cursor = context_reports.cursor()
            send_provider_command(process, UPDATE_CONTEXT_COMMAND)
            context_event = context_reports.wait_for(
                lambda payload: any(patient.summary().startswith(UPDATED_PATIENT) for patient in payload.values()),
                after=context_cursor,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                context_event is not None,
                "context_by_handle fires when the peer replaces its patient",
                str(context_reports.history()),
            )
            report.check(
                wait_for_output_line(
                    output,
                    f"{COMMAND_DONE_PREFIX} {UPDATE_CONTEXT_COMMAND}",
                    REPORT_TIMEOUT,
                ),
                "the provider confirms completion of the context command",
                output.buffered_output,
            )
            updated_patient = next(
                (
                    patient
                    for patient in (context_event.payload.values() if context_event is not None else ())
                    if patient.summary().startswith(UPDATED_PATIENT)
                ),
                None,
            )
            report.check(
                updated_patient is not None
                and updated_patient.summary().startswith(UPDATED_PATIENT)
                and updated_patient.height is not None
                and updated_patient.height.value == Decimal("1E-7")
                and updated_patient.race is not None
                and updated_patient.race.system == "urn:example:race",
                "a changed patient context arrives without reconnecting",
                updated_patient.summary() if updated_patient is not None else "no matching context report",
            )

            # ------------------------------------------------------------ sample arrays
            print("\n5b. Waveforms and distributions over the wire", flush=True)
            wave = remote.metrics().get(WAVE)
            report.check(wave is not None, f"{WAVE} present")
            if wave is not None:
                report.check(wave.kind is MetricKind.WAVEFORM, "it is a waveform", str(wave.kind))
                report.check(
                    wave.sample_period == Decimal("0.1"),
                    "SamplePeriod survives the round trip",
                    str(wave.sample_period),
                )
                # A waveform arrives as a WaveformStream rather than an EpisodicMetricReport,
                # so this also proves that path is wired up at both ends.
                waveform_cursor = waveform_reports.cursor()
                first_wave_event = waveform_reports.wait_for(
                    lambda payload: bool(payload.get(WAVE)),
                    after=waveform_cursor,
                    timeout=30.0,
                )
                samples = first_wave_event.payload.get(WAVE, ()) if first_wave_event is not None else ()
                report.check(bool(samples), "blocks of samples arrive", f"{len(samples)} samples")
                report.check(
                    all(Decimal("0") <= s <= Decimal("100") for s in samples),
                    "inside the range the peer declared",
                    f"{min(samples)} to {max(samples)}" if samples else "none",
                )
                next_wave_event = waveform_reports.wait_for(
                    lambda payload: bool(payload.get(WAVE)) and payload[WAVE] != samples,
                    after=first_wave_event.sequence if first_wave_event is not None else waveform_cursor,
                    timeout=30.0,
                )
                report.check(
                    next_wave_event is not None,
                    "and keep arriving, so the stream is live",
                    str(waveform_reports.history()),
                )
                report.check(
                    not wave.controllable,
                    "no operation targets it: BICEPS has none that writes a sample array",
                )

                # The blocks have to join up. A sawtooth is used at the far end precisely
                # because a break is arithmetic rather than a matter of opinion: consecutive
                # samples rise by a fixed step until the wrap, so a duplicated, dropped or
                # reordered block shows up as a delta that is neither.
                print("\n5c. The stream joins up", flush=True)
                received: list[Decimal] = []
                waveform_cursor = waveform_reports.cursor()
                sixth_block = waveform_reports.wait_for(
                    lambda _payload: sum(
                        bool(event.payload.get(SAW))
                        for event in waveform_reports.events_after(waveform_cursor)
                    )
                    >= 6,  # noqa: PLR2004
                    after=waveform_cursor,
                    timeout=25.0,
                )
                seen_blocks = [
                    event.payload[SAW]
                    for event in waveform_reports.events_after(waveform_cursor)
                    if event.payload.get(SAW)
                ]

                report.check(
                    sixth_block is not None and len(seen_blocks) >= 3,  # noqa: PLR2004
                    "several blocks arrive",
                    f"{len(seen_blocks)} blocks",
                )
                for block in seen_blocks:
                    received.extend(block)

                step = Decimal("100") / Decimal(SAW_CYCLE)
                breaks = []
                for index in range(1, len(received)):
                    delta = received[index] - received[index - 1]
                    wrapped = delta < 0 and received[index - 1] > Decimal("100") - step * 2
                    if not wrapped and abs(delta - step) > Decimal("0.05"):
                        breaks.append(f"{received[index - 1]}->{received[index]}")
                report.check(
                    not breaks,
                    "and every block continues the one before it",
                    f"{len(received)} samples, breaks at {breaks[:4]}" if breaks else f"{len(received)} samples",
                )
                report.check(
                    len(set(seen_blocks)) == len(seen_blocks) or len(received) < 2,  # noqa: PLR2004
                    "no block is delivered twice",
                    f"{len(seen_blocks)} blocks, {len(set(seen_blocks))} distinct",
                )

            dist = remote.metrics().get(DIST)
            report.check(dist is not None, f"{DIST} present")
            if dist is not None:
                report.check(dist.kind is MetricKind.DISTRIBUTION, "it is a distribution")
                report.check(
                    dist.samples == tuple(Decimal(index) for index in range(32)),
                    "its samples arrive exactly as sent",
                    str(dist.samples),
                )
                report.check(
                    dist.domain_unit_label == "Hz",
                    "DomainUnit survives, and is not the same as Unit",
                    f"domain {dist.domain_unit_label!r}, unit {dist.unit_label!r}",
                )
                report.check(
                    (dist.domain_minimum, dist.domain_maximum) == (Decimal("0"), Decimal("500")),
                    "and so does DistributionRange",
                    dist.domain_text(),
                )

            # ------------------------------------------------------------- alarms
            print("\n6. Alarms", flush=True)
            alerts = remote.alerts()
            report.check(len(alerts) == 2, "both alarms are visible", str(sorted(alerts)))  # noqa: PLR2004

            limit_alarm = alerts.get(LIMIT_ALARM)
            report.check(limit_alarm is not None, f"{LIMIT_ALARM} present")
            if limit_alarm is not None:
                report.check(
                    limit_alarm.node_type_name == "LimitAlertConditionDescriptor",
                    "an alarm with limits is a LimitAlertCondition",
                    limit_alarm.node_type_name,
                )
                report.check(
                    limit_alarm.source_handles == (ZOOM,),
                    "it names the metric it watches",
                    str(limit_alarm.source_handles),
                )
                report.check(
                    limit_alarm.upper_limit == Decimal("90"),
                    "and the limit it watches for",
                    str(limit_alarm.upper_limit),
                )
                report.check(
                    (limit_alarm.kind, limit_alarm.priority) == ("Tec", "Hi"),
                    "kind and priority survive the round trip",
                    f"{limit_alarm.kind}/{limit_alarm.priority}",
                )
                report.check(
                    sorted(limit_alarm.signals.values()) == ["Aud", "Vis"],
                    "one condition, two signals",
                    str(sorted(limit_alarm.signals.values())),
                )
                report.check(
                    limit_alarm.present,
                    "it already fired: the boundary write earlier left zoom at 100, above 90",
                )

            manual_alarm = alerts.get(MANUAL_ALARM)
            if manual_alarm is not None:
                report.check(
                    manual_alarm.node_type_name == "AlertConditionDescriptor",
                    "an alarm without limits is a plain AlertCondition",
                    manual_alarm.node_type_name,
                )

            # Cross the threshold in each direction so every check has a causal transition.
            metric_cursor = metric_reports.cursor()
            alert_cursor = alert_reports.cursor()
            state = remote.set_value(ZOOM, Decimal("20"))
            report.check(state in FINISHED, "bring the source back into range", str(state))
            report.check(
                metric_reports.wait_for(
                    lambda payload: payload.get(ZOOM) == Decimal("20"),
                    after=metric_cursor,
                    timeout=REPORT_TIMEOUT,
                )
                is not None,
                "the in-range value arrives independently in a metric report",
                str(metric_reports.history()),
            )
            cleared_event = alert_reports.wait_for(
                lambda payload: payload.get(LIMIT_ALARM, {}).get("presence") == "False"
                and payload.get("sig.zoom_out_of_range.vis", {}).get("presence") == "Off"
                and payload.get("sig.zoom_out_of_range.aud", {}).get("presence") == "Off",
                after=alert_cursor,
                timeout=REPORT_TIMEOUT,
            )
            report.check(
                cleared_event is not None,
                "the condition and both signals clear in one report",
                str(alert_reports.history()),
            )

            # --------------------------------------------------------------- actions
            print("\n7. Actions", flush=True)
            actions = remote.actions()
            report.check(HOME_ACTION in actions, "the peer's action is discovered", str(sorted(actions)))
            action = actions.get(HOME_ACTION)
            if action is not None:
                report.check(action.enabled, "and reports itself enabled")
                report.check(
                    action.caption == "Home axes",
                    "its concept description survives the round trip",
                    action.caption,
                )
                report.check(
                    action.target_handle == constants.MDS_HANDLE,
                    "and it names what it acts on",
                    str(action.target_handle),
                )

                # An action is not a set: nothing here says what value anything takes. The
                # device decides, and the proof is in the metrics it moves.
                setup_cursor = metric_reports.cursor()
                setup_zoom = remote.set_value(ZOOM, Decimal("42"))
                setup_mode = remote.set_value(MODE, "RUN")
                setup_event = metric_reports.wait_for(
                    lambda _payload: any(
                        event.payload.get(ZOOM) == Decimal("42")
                        for event in metric_reports.events_after(setup_cursor)
                    )
                    and any(
                        event.payload.get(MODE) == "RUN"
                        for event in metric_reports.events_after(setup_cursor)
                    ),
                    after=setup_cursor,
                    timeout=REPORT_TIMEOUT,
                )
                report.check(
                    setup_zoom in FINISHED and setup_mode in FINISHED and setup_event is not None,
                    "action preconditions arrive by metric report",
                    str(metric_reports.events_after(setup_cursor)),
                )
                action_cursor = metric_reports.cursor()
                state = remote.run_action(HOME_ACTION)
                report.check(state in FINISHED, "invoking it finishes", str(state))
                action_event = metric_reports.wait_for(
                    lambda payload: payload.get(ZOOM) == Decimal("1")
                    and payload.get(MODE) == "IDLE"
                    and payload.get(NOTE) == "001",
                    after=action_cursor,
                    timeout=REPORT_TIMEOUT,
                )
                report.check(
                    action_event is not None,
                    "and the device did what the action means, without being told a value",
                    str(metric_reports.events_after(action_cursor)),
                )
                report.check(
                    action_event is not None and action_event.payload.get(MODE) == "IDLE",
                    "including on a metric of a different kind",
                    str(action_event.payload if action_event is not None else None),
                )
                report.check(
                    action_event is not None and action_event.payload.get(NOTE) == "001",
                    "and numeric-looking text remains text",
                    repr(action_event.payload.get(NOTE) if action_event is not None else None),
                )

                for invalid_action, description in (
                    (INVALID_EFFECT_ACTION, "out-of-range"),
                    (INVALID_CHOICE_ACTION, "invalid-choice"),
                    (MISSING_EFFECT_ACTION, "missing-target"),
                ):
                    setup_cursor = metric_reports.cursor()
                    setup_zoom = remote.set_value(ZOOM, Decimal("42"))
                    setup_mode = remote.set_value(MODE, "RUN")
                    setup_event = metric_reports.wait_for(
                        lambda _payload: any(
                            event.payload.get(ZOOM) == Decimal("42")
                            for event in metric_reports.events_after(setup_cursor)
                        )
                        and any(
                            event.payload.get(MODE) == "RUN"
                            for event in metric_reports.events_after(setup_cursor)
                        ),
                        after=setup_cursor,
                        timeout=REPORT_TIMEOUT,
                    )
                    report.check(
                        setup_zoom in FINISHED and setup_mode in FINISHED and setup_event is not None,
                        f"the {description} action preconditions arrive",
                        str(metric_reports.events_after(setup_cursor)),
                    )
                    action_cursor = metric_reports.cursor()
                    state = remote.run_action(invalid_action)
                    report.check(
                        state is msg_types.InvocationState.FAILED,
                        f"a remote {description} action fails",
                        str(state),
                    )
                    barrier_cursor = metric_reports.cursor()
                    barrier_state = remote.set_value(NOTE, f"barrier-{description}")
                    barrier_event = metric_reports.wait_for(
                        lambda payload: payload.get(NOTE) == f"barrier-{description}",
                        after=barrier_cursor,
                        timeout=REPORT_TIMEOUT,
                    )
                    report.check(
                        barrier_state in FINISHED and barrier_event is not None,
                        f"a later report forms the {description} action barrier",
                    )
                    action_events = metric_reports.events_after(action_cursor)
                    report.check(
                        not any(
                            event.payload.get(ZOOM) not in {None, Decimal("42")}
                            or event.payload.get(MODE) not in {None, "RUN"}
                            for event in action_events
                        ),
                        f"a remote {description} action is all-or-nothing",
                        str(action_events),
                    )

            report.check(
                remote.run_action("act.no_such_thing") is msg_types.InvocationState.FAILED,
                "an unknown action fails rather than raising",
            )

            remote.close()

    finally:
        stop_process(process, output)

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
