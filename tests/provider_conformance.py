"""Provider conformance: what the provider's MDIB says must match what the provider does.

Each check reads the provider's own MDIB and compares it with the BICEPS meaning of an
attribute, rather than with whatever the toolbox happens to write.
"""

from __future__ import annotations

import logging
import socket
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from script_support import Report, wait_until  # noqa: E402
from sdc11073.consumer.consumerimpl import SdcConsumer  # noqa: E402
from sdc11073.definitions_sdc import SdcV1Definitions  # noqa: E402
from sdc11073.xml_types import msg_types, pm_types  # noqa: E402

from sdctoolbox import constants  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService, DiscoveredDevice  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    ActionSpec,
    AlertSpec,
    MetricKind,
    MetricSpec,
    PatientInfo,
    WaveformShape,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402
from sdctoolbox.sample_generation import DemoSampleGenerator  # noqa: E402

MONITORED = pm_types.AlertConditionMonitoredLimits
ASSOCIATED = pm_types.ContextAssociation.ASSOCIATED
LATE = 0.011  # how far a timed wait typically overshoots on a desktop OS
FAILED = msg_types.InvocationState.FAILED
FINISHED = msg_types.InvocationState.FINISHED
EVENTING = "http://schemas.xmlsoap.org/ws/2004/08/eventing"
EPISODIC_METRIC_REPORT = "http://standards.ieee.org/downloads/11073/11073-20701-2018/StateEventService/EpisodicMetricReport"
MAX_STALL = 6.0  # seconds; the provider's default is 18


@contextmanager
def _remote_set_client(service: ProviderService):
    """The Set service client of a plain sdc11073 consumer that listens for operation reports."""
    consumer = SdcConsumer(
        provider_address=service._provider.get_xaddrs()[0],  # noqa: SLF001
        sdc_definitions=SdcV1Definitions,
        ssl_context_container=None,
    )
    consumer.start_all()
    try:
        yield consumer.set_service_client
    finally:
        consumer.stop_all()


def _final_outcome(future) -> tuple[msg_types.InvocationState, msg_types.InvocationError | None, str]:
    """The last InvocationState of one remote invocation, with the error code and text it carried."""
    info = future.result(timeout=10).InvocationInfo
    return info.InvocationState, info.InvocationError, " ".join(text.text for text in info.InvocationErrorMessage)


def _connect(consumer: ConsumerService, service: ProviderService):
    """Connect the toolbox consumer to a provider of this process without a network scan."""
    addresses = list(service._provider.get_xaddrs())  # noqa: SLF001
    announced = SimpleNamespace(epr=service.epr.urn, x_addrs=addresses, types=[SdcV1Definitions.MedicalDeviceType])
    return consumer.connect(DiscoveredDevice(service.epr.urn, tuple(addresses), (), service=announced))


class _SilentEndpoint:
    """A TCP endpoint that accepts connections and never answers, like a frozen consumer process."""

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}/notify"
        self._held: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            self._held.append(connection)

    def close(self) -> None:
        self._listener.close()
        for connection in self._held:
            connection.close()


def _subscribe(address: str, action: str, notify_to: str) -> bool:
    """Send a WS-Eventing Subscribe for one action with an arbitrary delivery endpoint."""
    envelope = (
        '<s12:Envelope xmlns:s12="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:wsa="http://www.w3.org/2005/08/addressing" '
        f'xmlns:wse="{EVENTING}">'
        f"<s12:Header><wsa:To>{address}</wsa:To><wsa:Action>{EVENTING}/Subscribe</wsa:Action>"
        f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID></s12:Header>"
        f'<s12:Body><wse:Subscribe><wse:Delivery Mode="{EVENTING}/DeliveryModes/Push">'
        f"<wse:NotifyTo><wsa:Address>{notify_to}</wsa:Address></wse:NotifyTo></wse:Delivery>"
        "<wse:Expires>PT15S</wse:Expires>"
        f'<wse:Filter Dialect="http://docs.oasis-open.org/ws-dd/ns/dpws/2009/01/Action">{action}</wse:Filter>'
        "</wse:Subscribe></s12:Body></s12:Envelope>"
    )
    request = urllib.request.Request(
        address, data=envelope.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - a loopback test endpoint
        return b"SubscribeResponse" in response.read()


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


def check_requests_of_the_wrong_kind_are_refused(report: Report) -> None:
    """BICEPS: a request that does not fit the addressed operation gets InvocationError Inv and changes nothing."""
    service = ProviderService(instance_name="request-kind-conformance")
    try:
        service.start()
        number = service.add_metric(
            MetricSpec(
                label="Level",
                kind=MetricKind.NUMBER,
                controllable=True,
                minimum=Decimal(0),
                maximum=Decimal(100),
                initial_value=Decimal(10),
            ),
        )
        text = service.add_metric(
            MetricSpec(label="Note", kind=MetricKind.TEXT, controllable=True, initial_value="before"),
        )
        action = service.add_action(
            ActionSpec(label="Raise", target_handle=constants.MDS_HANDLE, effects={number: Decimal(20)}),
        )
        number_op = service.operation_handle_for(number)
        text_op = service.operation_handle_for(text)
        proposed_state = service.mdib.entities.by_handle(number).state.mk_copy()
        before = service.get_value(number), service.get_value(text)
        with _remote_set_client(service) as client:
            wrong_kinds = (
                ("SetString to a SetValue operation", number_op, lambda: client.set_string(number_op, "7")),
                ("SetValue to a SetString operation", text_op, lambda: client.set_numeric_value(text_op, 3)),
                ("Activate to a SetValue operation", number_op, lambda: client.activate(number_op)),
                ("Activate to a SetString operation", text_op, lambda: client.activate(text_op)),
                ("SetValue to an Activate operation", action, lambda: client.set_numeric_value(action, 3)),
                ("SetString to an Activate operation", action, lambda: client.set_string(action, "x")),
                (
                    "SetMetricState to a SetValue operation",
                    number_op,
                    lambda: client.set_metric_state(number_op, [proposed_state]),
                ),
            )
            for name, operation, send in wrong_kinds:
                state, error, message = _final_outcome(send())
                report.check(state is FAILED, f"{name} fails", str(state))
                report.check(
                    error is msg_types.InvocationError.INVALID_VALUE,
                    f"{name} is answered with InvocationError Inv",
                    str(error),
                )
                report.check(operation in message, f"{name}: the error text names the operation", message)
            report.check(
                (service.get_value(number), service.get_value(text)) == before,
                "refused requests of the wrong kind leave every value unchanged",
                f"{before} -> {(service.get_value(number), service.get_value(text))}",
            )

            for name, send, expected in (
                ("SetValue to a SetValue operation", lambda: client.set_numeric_value(number_op, 30), Decimal(30)),
                ("SetString to a SetString operation", lambda: client.set_string(text_op, "after"), "after"),
            ):
                state, error, _ = _final_outcome(send())
                report.check(state is FINISHED and error is None, f"{name} still succeeds", f"{state} {error}")
            report.check(
                (service.get_value(number), service.get_value(text)) == (Decimal(30), "after"),
                "matching requests still write their values",
            )
            state, error, _ = _final_outcome(client.activate(action))
            report.check(state is FINISHED and error is None, "Activate to an Activate operation still succeeds")
            report.check(service.get_value(number) == Decimal(20), "the activation applied its effect")
    finally:
        service.stop()


def check_failed_operations_say_why(report: Report) -> None:
    """A consumer must be able to tell a disabled control from a refused value from a vanished operation."""
    service = ProviderService(instance_name="failure-reason-conformance")
    try:
        service.start()
        number = service.add_metric(
            MetricSpec(
                label="Dose",
                kind=MetricKind.NUMBER,
                controllable=True,
                minimum=Decimal(0),
                maximum=Decimal(100),
                initial_value=Decimal(10),
            ),
        )
        choice = service.add_metric(
            MetricSpec(
                label="Mode",
                kind=MetricKind.CHOICE,
                controllable=True,
                allowed_values=("IDLE", "RUN"),
                initial_value="IDLE",
            ),
        )
        gone = service.add_metric(
            MetricSpec(label="Gone", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal(1)),
        )
        number_op = service.operation_handle_for(number)
        choice_op = service.operation_handle_for(choice)
        gone_op = service.operation_handle_for(gone)
        broken_action = service.add_action(
            ActionSpec(label="Broken", target_handle=constants.MDS_HANDLE, effects={number: "not a number"}),
        )
        other = msg_types.InvocationError.OTHER
        with _remote_set_client(service) as client:
            refused = (
                ("a value above the maximum", number_op, "maximum", lambda: client.set_numeric_value(number_op, 500)),
                ("a value outside the choices", choice_op, "allowed", lambda: client.set_string(choice_op, "FLY")),
                (
                    "an action whose effects are invalid",
                    broken_action,
                    "not a number",
                    lambda: client.activate(broken_action),
                ),
            )
            for name, operation, cause, send in refused:
                state, error, message = _final_outcome(send())
                report.check(state is FAILED, f"{name} fails", str(state))
                report.check(error is other, f"{name} is answered with InvocationError Oth", str(error))
                report.check(cause in message, f"{name}: the error text names the cause", message)
            report.check(
                (service.get_value(number), service.get_value(choice)) == (Decimal(10), "IDLE"),
                "refused values are not written",
            )

            service.disable_control(number)
            state, error, message = _final_outcome(client.set_numeric_value(number_op, 20))
            report.check(
                state is FAILED and error is other and number_op in message,
                "a disabled operation fails with Oth and its handle in the text",
                f"{state} {error} {message}",
            )
            report.check(service.get_value(number) == Decimal(10), "a disabled operation writes nothing")

            with service.mdib.descriptor_transaction() as mgr:
                mgr.remove_entity(service.mdib.entities.by_handle(gone_op))
            state, error, message = _final_outcome(client.set_numeric_value(gone_op, 2))
            report.check(
                state is FAILED and error is msg_types.InvocationError.UNKNOWN_OPERATION and gone_op in message,
                "an operation that left the MDIB fails with Unkn and its handle in the text",
                f"{state} {error} {message}",
            )
    finally:
        service.stop()


def check_the_consumer_logs_why_a_peer_refused(report: Report) -> None:
    """The toolbox consumer's own log line names the code and the text, not a list of objects."""
    service = ProviderService(instance_name="refusal-log-conformance")
    consumer = ConsumerService()
    remote = None
    lines: list[str] = []
    capture = logging.Handler()
    capture.emit = lambda record: lines.append(record.getMessage())
    consumer_log = logging.getLogger("sdctoolbox.consumer")
    previous_level = consumer_log.level
    try:
        service.start()
        number = service.add_metric(
            MetricSpec(
                label="Dose",
                kind=MetricKind.NUMBER,
                controllable=True,
                minimum=Decimal(0),
                maximum=Decimal(100),
                initial_value=Decimal(10),
            ),
        )
        broken_action = service.add_action(
            ActionSpec(label="Broken", target_handle=constants.MDS_HANDLE, effects={number: "not a number"}),
        )
        consumer.start()
        remote = _connect(consumer, service)
        consumer_log.addHandler(capture)
        consumer_log.setLevel(logging.INFO)
        refused_set = remote.set_value(number, Decimal(500))
        set_reason = remote.last_invocation_detail
        refused_action = remote.run_action(broken_action)
        action_reason = remote.last_invocation_detail
        report.check(refused_set is FAILED and refused_action is FAILED, "the toolbox consumer sees both refusals")
        report.check(
            set_reason.startswith("Oth: ") and "maximum" in set_reason,
            "the consumer keeps the peer's reason for a refused set",
            set_reason,
        )
        report.check(
            action_reason.startswith("Oth: ") and "not a number" in action_reason,
            "the consumer keeps the peer's reason for a refused action",
            action_reason,
        )
        report.check(
            remote.set_value(number, Decimal(20)) is not FAILED and remote.last_invocation_detail == "",
            "an accepted set leaves no reason behind",
            remote.last_invocation_detail,
        )
        report.check(
            remote.set_value("no-such-metric", Decimal(1)) is FAILED
            and "no-such-metric" in remote.last_invocation_detail,
            "a request the consumer never sends says why it was refused",
            remote.last_invocation_detail,
        )
        set_line = next((line for line in lines if line.startswith("set ")), "")
        action_line = next((line for line in lines if line.startswith("ran ")), "")
        report.check(
            "(Oth: " in set_line and "maximum" in set_line and "LocalizedText" not in set_line,
            "the log line of a refused set carries the error code and the text",
            set_line,
        )
        report.check(
            "(Oth: " in action_line and "not a number" in action_line and "LocalizedText" not in action_line,
            "the log line of a refused action carries the error code and the text",
            action_line,
        )
    finally:
        consumer_log.removeHandler(capture)
        consumer_log.setLevel(previous_level)
        if remote is not None:
            remote.close()
        consumer.stop()
        service.stop()


def check_a_silent_subscriber_cannot_hold_up_the_provider(report: Report) -> None:
    """A consumer that accepts a notification and never answers must cost seconds, not a subscription lifetime.

    sdc11073 delivers notifications while it holds the MDIB lock, so every Get and Set waits for the
    delivery timeout of the silent endpoint.
    """
    service = ProviderService(instance_name="silent-subscriber-conformance")
    consumer = ConsumerService()
    silent = _SilentEndpoint()
    remote = None
    try:
        service.start()
        metric = service.add_metric(MetricSpec(label="Level", kind=MetricKind.NUMBER, initial_value=Decimal(1)))
        consumer.start()
        remote = _connect(consumer, service)
        base = service._provider.get_xaddrs()[0].rstrip("/")  # noqa: SLF001
        report.require(
            _subscribe(f"{base}/StateEvent", EPISODIC_METRIC_REPORT, silent.url),
            "the silent endpoint is subscribed to metric reports",
        )

        updater = threading.Thread(target=lambda: service.set_value(metric, Decimal(2)))
        updater.start()
        time.sleep(0.3)  # the update is now waiting for the silent endpoint
        started = time.monotonic()
        try:
            remote._consumer.client("Get").get_md_state([metric])  # noqa: SLF001
            outcome = "answered"
        except Exception as exc:  # noqa: BLE001 - the consumer giving up on the request is the symptom
            outcome = type(exc).__name__
        answered_after = time.monotonic() - started
        updater.join(timeout=30)
        report.check(
            outcome == "answered" and answered_after < MAX_STALL,
            "a GetMdState issued during the stall is answered within the delivery timeout",
            f"{outcome} after {answered_after:.1f}s",
        )
        report.check(
            wait_until(lambda: remote.metrics()[metric].value == Decimal(2), timeout=10),
            "a healthy consumer still receives the update",
        )

        started = time.monotonic()
        service.set_value(metric, Decimal(3))
        report.check(time.monotonic() - started < 1.0, "the silent subscriber is dropped, so the next update is not held up")
        report.check(
            wait_until(lambda: remote.metrics()[metric].value == Decimal(3), timeout=10),
            "a healthy consumer still receives the next update",
        )
    finally:
        if remote is not None:
            remote.close()
        consumer.stop()
        silent.close()
        service.stop()


def check_the_consumer_is_told_about_descriptors_that_disappear(report: Report) -> None:
    """A peer that creates or removes a descriptor while connected is reported to the consumer, once."""
    service = ProviderService(instance_name="deleted-descriptor-conformance")
    consumer = ConsumerService()
    remote = None
    try:
        service.start()
        keep = service.add_metric(MetricSpec(label="Keep", kind=MetricKind.NUMBER, initial_value=Decimal(1)))
        gone = service.add_metric(MetricSpec(label="Gone", kind=MetricKind.NUMBER, initial_value=Decimal(2)))
        consumer.start()
        remote = _connect(consumer, service)
        added: list[set[str]] = []
        deleted: list[set[str]] = []
        remote.bind(
            new_descriptors_by_handle=lambda handles: added.append(set(handles)),
            deleted_descriptors_by_handle=lambda handles: deleted.append(set(handles)),
        )

        late = service.add_metric(MetricSpec(label="Late", kind=MetricKind.NUMBER, initial_value=Decimal(3)))
        report.check(
            wait_until(lambda: any(late in handles for handles in added), timeout=10),
            "the consumer is told about a descriptor the peer created",
            str(added),
        )
        service.remove_metric(gone)
        report.check(
            wait_until(lambda: any(gone in handles for handles in deleted), timeout=10),
            "the consumer is told about a descriptor the peer removed",
            str(deleted),
        )
        report.check(
            sum(gone in handles for handles in deleted) == 1 and not any(keep in handles for handles in deleted),
            "the removal is reported once, and only for the descriptor that went away",
            str(deleted),
        )
        metrics = remote.metrics()
        report.check(
            keep in metrics and late in metrics and gone not in metrics,
            "the consumer's own view dropped the removed metric and kept the others",
            str(sorted(metrics)),
        )
        service.set_value(keep, Decimal(5))
        report.check(
            wait_until(lambda: remote.metrics()[keep].value == Decimal(5), timeout=10)
            and len(deleted) == 1,
            "a later value report is not mistaken for another removal",
            str(deleted),
        )
    finally:
        if remote is not None:
            remote.close()
        consumer.stop()
        service.stop()


def check_partial_reads_carry_the_mdib_versions(report: Report) -> None:
    """An absent DescriptionVersion or StateVersion means 0, so a partial read has to repeat the MDIB's own."""
    service = ProviderService(instance_name="partial-read-conformance")
    consumer = None
    try:
        service.start()
        metric = service.add_metric(MetricSpec(label="Pressure", kind=MetricKind.NUMBER, initial_value=Decimal(50)))
        service.set_value(metric, Decimal(51))
        consumer = SdcConsumer(
            provider_address=service._provider.get_xaddrs()[0],  # noqa: SLF001
            sdc_definitions=SdcV1Definitions,
            ssl_context_container=None,
        )
        consumer.start_all()
        get = consumer.get_service_client

        def versions(result, element: str, attribute: str) -> list[str | None]:
            return [
                node.get(attribute)
                for node in result.p_msg.msg_node.iter()
                if isinstance(node.tag, str) and node.tag.rpartition("}")[2] == element
            ]

        whole = get.get_mdib()
        description_version = versions(whole, "MdDescription", "DescriptionVersion")
        state_version = versions(whole, "MdState", "StateVersion")
        report.check(
            description_version not in ([], ["0"]) and state_version not in ([], ["0"]),
            "the whole MDIB has moved past version 0, so a missing version would be told apart",
            f"description {description_version}, state {state_version}",
        )
        for handles in (None, [metric]):
            scope = "everything" if handles is None else "one handle"
            from_description = versions(get.get_md_description(handles), "MdDescription", "DescriptionVersion")
            report.check(
                from_description == description_version,
                f"GetMdDescription for {scope} names the description version of GetMdib",
                f"answer {from_description}, GetMdib {description_version}",
            )
            from_state = versions(get.get_md_state(handles), "MdState", "StateVersion")
            report.check(
                from_state == state_version,
                f"GetMdState for {scope} names the state version of GetMdib",
                f"answer {from_state}, GetMdib {state_version}",
            )
    finally:
        if consumer is not None:
            consumer.stop_all()
        service.stop()


def check_only_services_with_an_implementation_are_advertised(report: Report) -> None:
    """sdc11073 hosts a ContainmentTree service whose operations all answer a 500 "not implemented"."""
    service = ProviderService(instance_name="advertised-services-conformance")
    consumer = None
    try:
        service.start()
        consumer = SdcConsumer(
            provider_address=service._provider.get_xaddrs()[0],  # noqa: SLF001
            sdc_definitions=SdcV1Definitions,
            ssl_context_container=None,
        )
        consumer.start_all()
        offered = sorted(
            port_type.localname
            for hosted in consumer.host_description.relationship.Hosted
            for port_type in hosted.Types or ()
        )
        report.check(
            "GetService" in offered and "ContainmentTreeService" not in offered,
            "the provider offers its Get service but no ContainmentTree service it cannot answer",
            str(offered),
        )
    finally:
        if consumer is not None:
            consumer.stop_all()
        service.stop()


def check_a_closed_consumer_leaves_no_thread_behind(report: Report) -> None:
    """Closing a connection, or failing to make one, must end everything the consumer started.

    sdc11073 starts a worker thread per consumer that waits on its notification queue for ever. Left
    running, it kept the closed consumer, its MDIB and its sockets alive: a thread, about twenty
    handles and a few megabytes per reconnect.
    """

    def settled_thread_count() -> int:
        count = threading.active_count()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.3)
            if threading.active_count() == count:
                break
            count = threading.active_count()
        return count

    service = ProviderService(instance_name="reconnect-conformance")
    consumer = ConsumerService()
    try:
        service.start()
        consumer.start()
        _connect(consumer, service).close()  # whatever the first connection starts for good is not a leak
        baseline = settled_thread_count()

        for _ in range(4):
            _connect(consumer, service).close()
        report.check(
            wait_until(lambda: threading.active_count() <= baseline, timeout=5),
            "four connections that are closed again leave no thread running",
            f"{threading.active_count()} threads, {baseline} before: {sorted(t.name for t in threading.enumerate())}",
        )

        with patch.object(SdcConsumer, "start_all", side_effect=RuntimeError("the peer refused")):
            for _ in range(4):
                try:
                    _connect(consumer, service)
                except RuntimeError:
                    pass
        report.check(
            wait_until(lambda: threading.active_count() <= baseline, timeout=5),
            "four connections that fail while starting leave no thread running",
            f"{threading.active_count()} threads, {baseline} before: {sorted(t.name for t in threading.enumerate())}",
        )
    finally:
        consumer.stop()
        service.stop()


def main() -> int:
    report = Report()
    check_a_closed_consumer_leaves_no_thread_behind(report)
    check_limit_alarms_declare_the_limits_they_monitor(report)
    check_patient_is_bound_to_the_version_that_introduced_it(report)
    check_sample_streams_keep_their_declared_rate(report)
    check_requests_of_the_wrong_kind_are_refused(report)
    check_failed_operations_say_why(report)
    check_the_consumer_logs_why_a_peer_refused(report)
    check_a_silent_subscriber_cannot_hold_up_the_provider(report)
    check_the_consumer_is_told_about_descriptors_that_disappear(report)
    check_partial_reads_carry_the_mdib_versions(report)
    check_only_services_with_an_implementation_are_advertised(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
