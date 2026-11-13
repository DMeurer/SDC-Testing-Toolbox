"""Network acceptance: a consumer sees a device started from an MDIB file, and every edit to it.

The in-process MDIB tests call operations directly. This one goes through discovery,
metadata, the Set service and the reports, which is where a Sco or operation created after
loading has to be found by the library's own registries.
"""

from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from device_identity import with_metadata  # noqa: E402
from lxml import etree  # noqa: E402
from mdib_test_support import PM, original_mdib  # noqa: E402
from script_support import Report, wait_until  # noqa: E402
from sdc11073.xml_types import msg_types  # noqa: E402

import run_toolbox  # noqa: E402
from sdctoolbox import config  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.mdib_io import parse_mdib  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    ActionSpec,
    AlertPriority,
    AlertSpec,
    MetricKind,
    MetricSpec,
    PatientInfo,
)

FINISHED = msg_types.InvocationState.FINISHED


def without_sco(data: bytes) -> bytes:
    """The fixture with no Sco, so every operation has to be created after loading.

    Its alert system is switched on: the fixture's paused one would rightly keep a new alarm absent.
    """
    root = etree.fromstring(data.replace(b'xsi:type="pm:AlertSystemState" ActivationState="Psd"',
                                         b'xsi:type="pm:AlertSystemState" ActivationState="On"'))
    sco = root.find(f".//{{{PM}}}Sco")
    operations = {operation.get("Handle") for operation in sco}
    sco.getparent().remove(sco)
    states = root.find(f".//{{{PM}}}MdState")
    for state in list(states):
        if state.get("DescriptorHandle") in operations:
            states.remove(state)
    return etree.tostring(root)


def first_text(values) -> str | None:  # noqa: ANN001 - LocalizedText list or plain string
    if isinstance(values, str):
        return values
    return next((value.text for value in values or () if getattr(value, "text", None)), None)


def connect(report: Report, consumer: ConsumerService, epr: str, what: str):  # noqa: ANN201
    device = next((d for d in consumer.scan(timeout=15, expected=99) if d.epr == epr), None)
    report.require(device is not None, f"{what}: the device is discoverable")
    return consumer.connect(device)


def check_identity(report: Report, remote) -> None:  # noqa: ANN001
    host = remote._consumer.host_description  # noqa: SLF001 - the metadata the consumer fetched
    model, device = host.this_model, host.this_device
    report.check(remote.friendly_name == "Bed 4", "--friendly-name reaches the consumer", str(remote.friendly_name))
    report.check(first_text(model.Manufacturer) == "Example Medical", "manufacturer comes from the file's MetaData")
    report.check(first_text(model.ModelName) == "Demo 3000", "model name comes from the file's MetaData")
    report.check(model.ModelNumber == "D3K", "model number comes from the file's MetaData", str(model.ModelNumber))
    report.check(device.SerialNumber == "SN-42", "serial number comes from the file's MetaData", str(device.SerialNumber))


def check_operations(report: Report, service, remote) -> None:  # noqa: ANN001
    report.check(not remote.metrics()["demo.rate"].controllable_now, "without a Sco the metric is not controllable")
    service.enable_control("demo.rate")
    report.check(wait_until(lambda: remote.metrics()["demo.rate"].controllable_now, timeout=10),
                 "a control added to a file without a Sco is offered remotely")
    state = remote.set_value("demo.rate", Decimal("20"))
    report.check(state == FINISHED, "a remote set reaches the Sco created after loading",
                 f"{state} {remote.last_invocation_detail}")
    report.check(service.get_value("demo.rate") == Decimal("20"), "the provider took the remote value")
    report.check(wait_until(lambda: remote.metrics()["demo.rate"].value == Decimal("20"), timeout=10),
                 "the new value is reported back")
    service.disable_control("demo.rate")
    report.check(wait_until(lambda: not remote.metrics()["demo.rate"].controllable_now, timeout=10),
                 "switching the control off is reported")

    action = service.add_action(ActionSpec("Go idle", "demo.root", effects={"demo.mode": "IDLE"}))
    report.check(wait_until(lambda: action in remote.actions(), timeout=10), "an action added after loading is offered")
    state = remote.run_action(action)
    report.check(state == FINISHED, "the action runs remotely", f"{state} {remote.last_invocation_detail}")
    report.check(wait_until(lambda: remote.metrics()["demo.mode"].value == "IDLE", timeout=10),
                 "the action's effect is reported")
    service.remove_action(action)
    report.check(wait_until(lambda: action not in remote.actions(), timeout=10), "a removed action disappears remotely")


def check_edits(report: Report, service, remote) -> None:  # noqa: ANN001
    metric = service.add_metric(MetricSpec("SpO2", MetricKind.NUMBER, section="Vitals", initial_value=Decimal("97")))
    report.check(wait_until(lambda: metric in remote.metrics() and remote.metrics()[metric].value == Decimal("97"),
                            timeout=10), "a metric added to a loaded MDIB reaches the consumer with its value")
    alarm = service.add_alert(AlertSpec("SpO2 low", metric, lower_limit=Decimal("90")))
    report.check(wait_until(lambda: alarm in remote.alerts(), timeout=10), "an alarm added to a loaded MDIB is reported")
    service.set_value(metric, Decimal("85"))
    report.check(wait_until(lambda: remote.alerts()[alarm].present, timeout=10), "the new alarm's presence is reported")
    service.set_patient(PatientInfo(given_name="Synthetic next"))
    report.check(wait_until(lambda: remote.patient().given_name == "Synthetic next", timeout=10),
                 "a patient change on the imported context is reported")
    service.remove_metric(metric)
    report.check(wait_until(lambda: metric not in remote.metrics() and alarm not in remote.alerts(), timeout=10),
                 "removing the metric removes it and its alarm remotely")


def check_live_edits(report: Report, service, remote) -> None:  # noqa: ANN001
    """Runtime states change on the running device and reach the consumer without a reconnect."""
    rate = service.add_metric(MetricSpec("Live rate", MetricKind.NUMBER, minimum=Decimal("0"), maximum=Decimal("100"),
                                         controllable=True, initial_value=Decimal("30")))
    alarm = service.add_alert(AlertSpec("Live rate high", rate, upper_limit=Decimal("90")))
    report.check(wait_until(lambda: alarm in remote.alerts() and rate in remote.metrics(), timeout=10),
                 "the live-edit metric and alarm reach the consumer")

    def allowed() -> tuple:
        ranges = remote.metrics()[rate].allowed_ranges
        return (ranges[0].lower, ranges[0].upper) if ranges else ()

    service.set_allowed_range(rate, Decimal("20"), Decimal("40"))
    report.check(wait_until(lambda: allowed() == (Decimal("20"), Decimal("40")), timeout=2),
                 "a narrowed allowed range is reported within 2 s", str(allowed()))
    state = remote.set_value(rate, Decimal("50"))
    report.check(state != FINISHED and service.get_value(rate) == Decimal("30"),
                 "the consumer cannot set 50 any more", f"{state} {remote.last_invocation_detail}")

    service.set_alert_limits(alarm, Decimal("10"), Decimal("25"))
    service.set_alert_priority(alarm, AlertPriority.HIGH)
    report.check(wait_until(lambda: (remote.alerts()[alarm].lower_limit, remote.alerts()[alarm].upper_limit)
                            == (Decimal("10"), Decimal("25")) and remote.alerts()[alarm].priority == "Hi", timeout=2),
                 "moved limits and the actual priority are reported within 2 s")
    report.check(wait_until(lambda: remote.alerts()[alarm].present, timeout=2), "30 is above 25, so the alarm is present")

    service.set_metric_active(rate, active=False)
    report.check(wait_until(lambda: remote.metrics()[rate].activation == "Off", timeout=2),
                 "a switched-off metric is reported Off within 2 s")
    report.check(wait_until(lambda: not remote.alerts()[alarm].present, timeout=2), "its alarm is no longer present")
    service.remove_metric(rate)


def check_reload_generates(report: Report, service, consumer, directory: Path):  # noqa: ANN001, ANN201
    """A behaviour export reloaded in place, as the window's import does, keeps its waveforms moving."""
    wave = service.add_metric(MetricSpec("Pleth", MetricKind.WAVEFORM, minimum=Decimal("0"), maximum=Decimal("10")))
    service.start_generator()
    target = directory / "with behaviour.xml"
    config.save(service, target, behaviour=True)
    service.stop_generator()
    service.replace_mdib(config.load_file(target))
    report.check(service.generator_running, "loading a file that marks waveforms starts the generator")
    remote = connect(report, consumer, service.epr.urn, "after the reload")
    first = {}

    def moving(handle: str) -> bool:
        samples = remote.metrics()[handle].samples
        first.setdefault(handle, samples)
        return bool(samples) and samples != first[handle]

    for handle in (wave, "demo.wave"):
        report.check(wait_until(lambda handle=handle: moving(handle), timeout=10),
                     f"{handle} keeps generating after the reload")
    return remote


def main() -> int:
    report = Report()
    consumer = ConsumerService()
    remotes = []
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        source = directory / "no sco.xml"
        source.write_bytes(without_sco(with_metadata(original_mdib())))
        settings = run_toolbox.settings_from_args(run_toolbox.parse_args(
            ["--name", "mdib-network", "--config", str(source), "--friendly-name", "Bed 4"]))
        device_config = config.load_file(source)
        service = run_toolbox.make_service(settings, device_config)
        try:
            service.start(parse_mdib(device_config.xml))
            consumer.start()
            remotes.append(connect(report, consumer, service.epr.urn, "loaded MDIB"))
            check_identity(report, remotes[0])
            check_operations(report, service, remotes[0])
            check_edits(report, service, remotes[0])
            check_live_edits(report, service, remotes[0])
            remotes.append(check_reload_generates(report, service, consumer, directory))
        except AssertionError:
            pass
        finally:
            for remote in remotes:
                remote.close()
            consumer.stop()
            service.stop()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
