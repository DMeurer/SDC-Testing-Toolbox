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
import threading
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acceptance_provider import (  # noqa: E402
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
    WAVE,
    ZOOM,
)
from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import msg_types  # noqa: E402

from sdctoolbox import constants  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.model import MetricKind  # noqa: E402

FINISHED = (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD)


class Report:
    """Collects pass/fail results and prints them as they happen."""

    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, description: str, detail: str = "") -> bool:  # noqa: FBT001
        self.checks += 1
        if not ok:
            self.failures += 1
        status = "PASS" if ok else "FAIL"
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {status}  {description}{suffix}", flush=True)
        return ok

    def summary(self) -> int:
        print("-" * 74)
        if self.failures:
            print(f"RESULT: {self.failures} of {self.checks} checks FAILED")
            return 1
        print(f"RESULT: all {self.checks} checks passed")
        return 0


def wait_for_ready(process: subprocess.Popen, timeout: float) -> bool:
    """Block until the provider prints READY, echoing its output meanwhile."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                return False
            continue
        print(f"    {line.rstrip()}", flush=True)
        if "READY" in line:
            return True
    return False


def drain(process: subprocess.Popen) -> None:
    """Keep echoing provider output in the background so it never blocks on a full pipe."""

    def pump() -> None:
        for line in process.stdout:
            print(f"    {line.rstrip()}", flush=True)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()


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
        text=True,
        bufsize=1,
    )

    try:
        if not wait_for_ready(process, timeout=45):
            print("FAIL: provider never reported READY")
            return 1
        drain(process)

        # ---------------------------------------------------------------- discovery
        print("\n1. Discovery and connection", flush=True)
        with ConsumerService(ip=args.ip) as consumer_service:
            expected_epr = constants.epr_for(PEER_INSTANCE).urn
            deadline = time.monotonic() + 25.0
            devices = []
            device = None
            while device is None and time.monotonic() < deadline:
                devices = consumer_service.scan(
                    timeout=min(5.0, deadline - time.monotonic()),
                    expected=99,
                )
                device = next((candidate for candidate in devices if candidate.epr == expected_epr), None)
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

            # Record runtime descriptor arrivals before the late metric is created.
            new_descriptor_events: list[str] = []
            seen = threading.Event()

            def on_new_descriptors(descriptors_by_handle: dict) -> None:
                for handle in descriptors_by_handle:
                    new_descriptor_events.append(handle)
                    if handle == LATE:
                        seen.set()

            value_events: list[str] = []
            context_events: list[str] = []
            context_changed = threading.Event()

            def on_metrics(metrics_by_handle: dict) -> None:
                value_events.extend(metrics_by_handle)

            def on_contexts(context_by_handle: dict) -> None:
                context_events.extend(context_by_handle)
                context_changed.set()

            remote.bind(
                new_descriptors_by_handle=on_new_descriptors,
                metrics_by_handle=on_metrics,
                context_by_handle=on_contexts,
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
            state = remote.set_value(ZOOM, "7")
            report.check(state in FINISHED, "setting a numeric string finishes", str(state))
            time.sleep(1.5)
            report.check(
                remote.metrics()[ZOOM].value == Decimal("7"),
                "new numeric value observed back on the consumer",
                str(remote.metrics()[ZOOM].value),
            )

            state = remote.set_value(MODE, "RUN")
            report.check(state in FINISHED, "setting a choice value finishes", str(state))
            time.sleep(1.5)
            report.check(
                remote.metrics()[MODE].value == "RUN",
                "new choice value observed back on the consumer",
                str(remote.metrics()[MODE].value),
            )

            state = remote.set_value(NOTE, "checked at 10:00")
            report.check(state in FINISHED, "setting a text value finishes", str(state))

            # ------------------------------------------------------------ rejections
            print("\n4. Rejections", flush=True)
            state = remote.set_value(MODE, "NOT_A_MODE")
            report.check(
                state is msg_types.InvocationState.FAILED,
                "value outside AllowedValue is rejected",
                str(state),
            )
            time.sleep(1.0)
            report.check(
                remote.metrics()[MODE].value == "RUN",
                "rejected write left the value untouched",
                str(remote.metrics()[MODE].value),
            )

            state = remote.set_value(LOCKED, Decimal("9"))
            report.check(
                state is msg_types.InvocationState.FAILED,
                "write to a disabled control is rejected",
                str(state),
            )
            time.sleep(1.0)
            report.check(
                remote.metrics()[LOCKED].value == Decimal("5"),
                "disabled control left the value untouched",
                str(remote.metrics()[LOCKED].value),
            )

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
            time.sleep(1.0)
            report.check(
                remote.metrics()[ZOOM].value == Decimal("7"),
                "out-of-range writes left the value untouched",
                str(remote.metrics()[ZOOM].value),
            )
            state = remote.set_value(ZOOM, Decimal("100"))
            report.check(
                state in FINISHED,
                "the maximum itself is accepted",
                str(state),
            )

            # ------------------------------------------- runtime descriptor creation
            print("\n5. Data source created at runtime", flush=True)
            report.check(
                seen.wait(timeout=30),
                "new_descriptors_by_handle fired for the late metric",
                f"events: {new_descriptor_events}",
            )
            time.sleep(2.0)
            metrics = remote.metrics()
            late_metric = metrics.get(LATE)
            report.check(late_metric is not None, f"{LATE} appeared in the consumer MDIB")
            if late_metric is not None:
                report.check(late_metric.kind is MetricKind.NUMBER, f"{LATE} is a number")
                report.check(
                    late_metric.controllable_now,
                    f"{LATE} is controllable without reconnecting",
                )
                state = remote.set_value(LATE, Decimal("99"))
                report.check(
                    state in FINISHED,
                    "controlling the runtime-created metric finishes",
                    str(state),
                )
                time.sleep(1.5)
                report.check(
                    remote.metrics()[LATE].value == Decimal("99"),
                    "runtime-created metric reflects the new value",
                    str(remote.metrics()[LATE].value),
                )

            report.check(bool(value_events), "metrics_by_handle fired at least once")
            report.check(
                context_changed.wait(timeout=30),
                "context_by_handle fires when the peer replaces its patient",
                str(context_events),
            )
            updated_patient = remote.patient()
            report.check(
                updated_patient.summary().startswith(UPDATED_PATIENT)
                and updated_patient.height is not None
                and updated_patient.height.value == Decimal("1E-7")
                and updated_patient.race is not None
                and updated_patient.race.system == "urn:example:race",
                "a changed patient context arrives without reconnecting",
                updated_patient.summary(),
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
                deadline = time.monotonic() + 30.0
                while not remote.metrics()[WAVE].samples and time.monotonic() < deadline:
                    time.sleep(0.5)
                samples = remote.metrics()[WAVE].samples
                report.check(bool(samples), "blocks of samples arrive", f"{len(samples)} samples")
                report.check(
                    all(Decimal("0") <= s <= Decimal("100") for s in samples),
                    "inside the range the peer declared",
                    f"{min(samples)} to {max(samples)}" if samples else "none",
                )
                first = list(samples)
                deadline = time.monotonic() + 30.0
                while remote.metrics()[WAVE].samples == tuple(first) and time.monotonic() < deadline:
                    time.sleep(0.5)
                report.check(
                    remote.metrics()[WAVE].samples != tuple(first),
                    "and keep arriving, so the stream is live",
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
                seen_blocks: list[tuple] = []

                def collect_blocks(states_by_handle: dict) -> None:
                    state = states_by_handle.get(SAW)
                    value = getattr(state, "MetricValue", None) if state is not None else None
                    block = tuple(getattr(value, "Samples", None) or ())
                    if block:
                        seen_blocks.append(block)

                remote.bind(waveform_by_handle=collect_blocks)
                deadline = time.monotonic() + 25.0
                while len(seen_blocks) < 6 and time.monotonic() < deadline:  # noqa: PLR2004
                    time.sleep(0.5)

                report.check(
                    len(seen_blocks) >= 3,  # noqa: PLR2004
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

            # The zoom metric currently sits at 100, which is above the alarm limit of 90,
            # so the alarm should already have fired from the earlier boundary write.
            state = remote.set_value(ZOOM, Decimal("95"))
            report.check(state in FINISHED, "raise the source above the limit", str(state))
            time.sleep(2.0)
            report.check(
                remote.alerts()[LIMIT_ALARM].present,
                "a remote write raises the alarm on the provider",
            )

            state = remote.set_value(ZOOM, Decimal("20"))
            report.check(state in FINISHED, "bring the source back into range", str(state))
            time.sleep(2.0)
            report.check(
                not remote.alerts()[LIMIT_ALARM].present,
                "and clears it again",
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
                remote.set_value(ZOOM, Decimal("42"))
                remote.set_value(MODE, "RUN")
                time.sleep(1.5)
                state = remote.run_action(HOME_ACTION)
                report.check(state in FINISHED, "invoking it finishes", str(state))
                time.sleep(2.0)
                report.check(
                    remote.metrics()[ZOOM].value == Decimal("1"),
                    "and the device did what the action means, without being told a value",
                    str(remote.metrics()[ZOOM].value),
                )
                report.check(
                    remote.metrics()[MODE].value == "IDLE",
                    "including on a metric of a different kind",
                    str(remote.metrics()[MODE].value),
                )
                report.check(
                    remote.metrics()[NOTE].value == "001",
                    "and numeric-looking text remains text",
                    repr(remote.metrics()[NOTE].value),
                )

                for invalid_action, description in (
                    (INVALID_EFFECT_ACTION, "out-of-range"),
                    (INVALID_CHOICE_ACTION, "invalid-choice"),
                    (MISSING_EFFECT_ACTION, "missing-target"),
                ):
                    remote.set_value(ZOOM, Decimal("42"))
                    remote.set_value(MODE, "RUN")
                    time.sleep(1.0)
                    state = remote.run_action(invalid_action)
                    report.check(
                        state is msg_types.InvocationState.FAILED,
                        f"a remote {description} action fails",
                        str(state),
                    )
                    time.sleep(1.0)
                    current = remote.metrics()
                    report.check(
                        current[ZOOM].value == Decimal("42") and current[MODE].value == "RUN",
                        f"a remote {description} action is all-or-nothing",
                        f"{current[ZOOM].value}, {current[MODE].value}",
                    )

            report.check(
                remote.run_action("act.no_such_thing") is msg_types.InvocationState.FAILED,
                "an unknown action fails rather than raising",
            )

            remote.close()

    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
