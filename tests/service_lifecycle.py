"""Fault-injection checks for provider and consumer startup cleanup."""

from __future__ import annotations

import gc
import sys
from contextlib import ExitStack
from http.client import BadStatusLine, NotConnected
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report  # noqa: E402
from sdc11073.definitions_sdc import SdcV1Definitions  # noqa: E402
from lxml import etree  # noqa: E402
from sdc11073.observableproperties import ObservableProperty  # noqa: E402
from sdc11073.pysoap.soapclient import HTTPReturnCodeError  # noqa: E402
from sdc11073.pysoap.soapenvelope import Fault  # noqa: E402
from sdc11073.xml_types.actions import Actions  # noqa: E402

from sdctoolbox import consumer_service as consumer_module  # noqa: E402
from sdctoolbox import provider_service as provider_module  # noqa: E402
from sdctoolbox import sample_generation as sample_module  # noqa: E402
from sdctoolbox.consumer_service import (  # noqa: E402
    ConsumerError,
    ConsumerService,
    DiscoveredDevice,
    PeerConnectionLost,
    RemoteDevice,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402

REPORT = Report()


class Tracker:
    def __init__(
        self,
        *,
        fail_start: str | None = None,
        fail_stop: str | None = None,
        stop_event: str | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.stop_event = stop_event
        self.events = events
        self.active = False
        self.start_count = 0
        self.stop_count = 0

    def start(self, *_args, **_kwargs) -> None:
        self.start_count += 1
        self.active = True
        if self.fail_start:
            raise RuntimeError(self.fail_start)

    def stop(self) -> None:
        self.stop_count += 1
        self.active = False
        if self.events is not None and self.stop_event is not None:
            self.events.append(self.stop_event)
        if self.fail_stop:
            raise RuntimeError(self.fail_stop)

    start_all = start
    stop_all = stop


class FakeMdib:
    pass


def check(condition: bool, message: str) -> None:
    REPORT.require(condition, message)


def raises(message: str, function) -> RuntimeError:
    try:
        function()
    except RuntimeError as exc:
        check(str(exc) == message, f"preserves {message!r} as the primary error")
        return exc
    raise AssertionError(f"expected {message!r}")


def assert_provider_reset(service: ProviderService, discovery: Tracker, provider: Tracker | None = None) -> None:
    check(not discovery.active, "provider failure leaves discovery stopped")
    if provider is not None:
        check(not provider.active, "provider failure leaves the provider stopped")
    check(not service.generator_running, "provider failure leaves no sample generator")
    check(
        all(
            getattr(service, name) is None
            for name in (
                "_discovery",
                "_provider",
                "_mdib",
                "_sco",
                "_handler",
                "_activate_handler",
                "_adapter",
            )
        ),
        "provider failure resets lifecycle references",
    )
    check(
        all(
            not getattr(service, name)
            for name in (
                "_operations",
                "_specs",
                "_alerts",
                "_alert_signals",
                "_pending_alert_sources",
                "_actions",
                "_sections",
            )
        ),
        "provider failure resets mutable lifecycle state",
    )


def provider_dependencies(discovery: Tracker, provider: Tracker | None = None):
    patches = [
        patch.object(provider_module, "WSDiscovery", lambda _ip: discovery),
        patch.object(provider_module, "make_set_handler", lambda *_args, **_kwargs: object()),
        patch.object(provider_module, "make_activate_handler", lambda *_args, **_kwargs: object()),
    ]
    if provider is not None:
        patches.append(patch.object(provider_module, "SdcProvider", lambda **_kwargs: provider))
    return patches


def run_patched(patchers: list, function) -> None:  # noqa: ANN001
    with ExitStack() as stack:
        for patcher in patchers:
            stack.enter_context(patcher)
        function()


def mdib_load_failure() -> None:
    service = ProviderService(instance_name="mdib-load-failure")
    discovery = Tracker()

    class FailingProviderMdib:
        @staticmethod
        def from_mdib_file(_path: str) -> None:
            raise RuntimeError("mdib load failed")

    def exercise() -> None:
        raises("mdib load failed", service.start)

    run_patched(
        [*provider_dependencies(discovery), patch.object(provider_module, "ProviderMdib", FailingProviderMdib)],
        exercise,
    )
    check(discovery.stop_count == 1, "MDIB load failure stops discovery exactly once")
    assert_provider_reset(service, discovery)


def provider_start_failure() -> None:
    service = ProviderService(instance_name="provider-start-failure")
    discovery = Tracker()
    provider = Tracker(fail_start="provider start failed")

    def exercise() -> None:
        raises("provider start failed", service.start)

    run_patched(
        [
            *provider_dependencies(discovery, provider),
            patch.object(provider_module.ProviderMdib, "from_mdib_file", lambda _path: FakeMdib()),
        ],
        exercise,
    )
    check(provider.stop_count == 1, "provider start failure stops the partial provider exactly once")
    check(discovery.stop_count == 1, "provider start failure stops discovery exactly once")
    assert_provider_reset(service, discovery, provider)


def default_context_failure() -> None:
    service = ProviderService(instance_name="context-failure")
    events: list[str] = []
    discovery = Tracker(stop_event="discovery", events=events)
    provider = Tracker(stop_event="provider", events=events)
    stop_generator = service.stop_generator

    def fail_patient(_patient) -> None:
        service.start_generator()
        raise RuntimeError("default context failed")

    def record_generator_stop() -> None:
        stop_generator()
        events.append("generator")

    def exercise() -> None:
        service.set_location = lambda _location: None
        service.set_patient = fail_patient
        service.stop_generator = record_generator_stop
        raises("default context failed", service.start)

    run_patched(
        [
            *provider_dependencies(discovery, provider),
            patch.object(provider_module.ProviderMdib, "from_mdib_file", lambda _path: FakeMdib()),
            patch.object(service, "_publish_one_block", lambda: None),
        ],
        exercise,
    )
    check(provider.stop_count == 1, "default-context failure stops the provider exactly once")
    check(discovery.stop_count == 1, "default-context failure stops discovery exactly once")
    check(events == ["generator", "provider", "discovery"], "provider startup rolls back in reverse order")
    assert_provider_reset(service, discovery, provider)


def generator_start_failure() -> None:
    service = ProviderService(instance_name="generator-failure")

    class FailingThread:
        def __init__(self, **_kwargs) -> None:
            self.active = False

        def start(self) -> None:
            raise RuntimeError("generator start failed")

    with patch.object(sample_module.threading, "Thread", FailingThread):
        raises("generator start failed", service.start_generator)
    check(service._sample_generator.thread is None, "generator start failure resets its thread reference")  # noqa: SLF001
    check(not service.generator_running, "generator start failure leaves no active generator")


def provider_stop_failures() -> None:
    service = ProviderService(instance_name="stop-failure")
    provider = Tracker(fail_stop="provider stop failed")
    discovery = Tracker(fail_stop="discovery stop failed")
    provider.active = True
    discovery.active = True
    service._provider = provider
    service._discovery = discovery
    service._mdib = FakeMdib()

    def fail_generator_stop() -> None:
        raise RuntimeError("generator stop failed")

    service.stop_generator = fail_generator_stop

    raises("generator stop failed", service.stop)
    check(not service.generator_running, "shutdown preserves the first cleanup error")
    check(provider.stop_count == 1, "shutdown attempts provider cleanup")
    check(discovery.stop_count == 1, "shutdown continues through discovery cleanup")
    assert_provider_reset(service, discovery, provider)


def consumer_discovery_start_failure() -> None:
    service = ConsumerService()
    discovery = Tracker(fail_start="discovery start failed")
    with patch.object(consumer_module, "WSDiscovery", lambda _ip: discovery):
        raises("discovery start failed", service.start)
    check(discovery.stop_count == 1, "consumer discovery start failure rolls back exactly once")
    check(not discovery.active and service._discovery is None, "consumer discovery failure resets state")


def consumer_connection_failure(*, stage: str, tls: bool = False) -> None:
    service = ConsumerService(tls_config=object() if tls else None)
    consumer = Tracker(fail_start="consumer start failed" if stage == "start" else None)
    device = DiscoveredDevice("peer", (), (), service=SimpleNamespace(x_addrs=[]))

    class ConsumerFactory:
        def __new__(cls, *_args, **_kwargs) -> Tracker:
            return consumer

        @staticmethod
        def from_wsd_service(*_args, **_kwargs) -> Tracker:
            return consumer

    class FakeConsumerMdib:
        def __init__(self, *_args, **_kwargs) -> None:
            if stage == "construct":
                raise RuntimeError("consumer MDIB construction failed")

        def init_mdib(self) -> None:
            raise RuntimeError("consumer MDIB init failed")

    message = {
        "start": "consumer start failed",
        "construct": "consumer MDIB construction failed",
        "init": "consumer MDIB init failed",
    }[stage]
    patchers = [patch.object(consumer_module, "_WsdlSdcConsumer", ConsumerFactory)]
    if tls:
        patchers.append(patch.object(service.tls_config, "create_contexts", lambda: object()))
    with ExitStack() as stack:
        for patcher in patchers:
            stack.enter_context(patcher)
        with patch.object(
        consumer_module,
        "ConsumerMdib",
        FakeConsumerMdib,
        ):
            raises(message, lambda: service.connect(device))
    check(consumer.stop_count == 1, f"consumer {stage} failure closes the consumer exactly once")
    check(not consumer.active, f"consumer {stage} failure leaves no active consumer")


def http_consumer_rejects_https_endpoint() -> None:
    service = ConsumerService()
    http = "http://127.0.0.1:1/device"
    https = "https://127.0.0.1:1/device"
    for addresses, live_addresses in (
        ((https,), (https,)),
        ((https, http), (https, http)),
        ((http,), (http,)),
        ((http, https), (http, https)),
        ((), ()),
        ((http,), (https, http)),
        ((https,), (http, https)),
    ):
        discovered_service = SimpleNamespace(x_addrs=list(addresses))
        device = DiscoveredDevice("peer", addresses, (), service=discovered_service)
        discovered_service.x_addrs = list(live_addresses)

        def construct(selected_service, *, ssl_context_container) -> None:  # noqa: ANN001
            discovered_service.x_addrs[:] = [https]
            check(
                tuple(selected_service.x_addrs) == live_addresses,
                "construction uses the checked addresses despite a later discovery update",
            )
            raise RuntimeError("consumer construction reached")

        with patch.object(
            consumer_module._WsdlSdcConsumer,
            "from_wsd_service",
            side_effect=construct,
        ) as factory:
            try:
                service.connect(device)
            except consumer_module.ConsumerError as exc:
                check(bool(live_addresses) and live_addresses[0] == https, "only the selected HTTPS endpoint is rejected")
                check(
                    "requires HTTPS" in str(exc) and "configured for HTTP" in str(exc)
                    and "Enable TLS" in str(exc) and "certificates" in str(exc),
                    "transport mismatch explains the cause and how to enable secure connections",
                )
                factory.assert_not_called()
            except RuntimeError as exc:
                check(not live_addresses or live_addresses[0] == http, "an HTTPS endpoint is rejected before construction")
                check(str(exc) == "consumer construction reached", "other endpoints keep their existing connection path")
            else:
                raise AssertionError("expected connection rejection or consumer construction")


def tls_alert_connection_diagnostics() -> None:
    alert = "\x15\x03\x03\x00\x02\x02\x16"
    unrelated = RuntimeError("unrelated startup failure")
    unrelated.__cause__ = BadStatusLine(alert)
    cases = (
        (BadStatusLine(alert), True, False, True),
        (BadStatusLine("\x15\x03\x01\x00\x02\x02\x16"), True, False, True),
        (BadStatusLine("\x15\x03\x02\x00\x02\x02\x16"), True, False, True),
        (BadStatusLine("\x15\x03\x03\x00\x02\x01\x00"), True, False, True),
        (BadStatusLine(alert), False, False, True),
        (BadStatusLine("not an HTTP response"), True, False, False),
        (BadStatusLine("\x15\x03\x03"), True, False, False),
        (BadStatusLine(alert + "\x00"), True, False, False),
        (BadStatusLine("\x15\x02\x03\x00\x02\x02\x16"), True, False, False),
        (BadStatusLine("\x15\x03\x04\x00\x02\x02\x16"), True, False, False),
        (BadStatusLine("\x15\x03\x03\x00\x01\x02\x16"), True, False, False),
        (BadStatusLine("\x15\x03\x03\x00\x02\x00\x16"), True, False, False),
        (BadStatusLine("\x14\x03\x03\x00\x02\x02\x16"), True, False, False),
        (ConnectionResetError("connection reset by peer"), True, False, False),
        (BadStatusLine(alert), True, True, False),
        (None, True, False, False),
        (unrelated, False, False, False),
    )
    for cause, wrapped, tls, expected in cases:
        failure = NotConnected() if wrapped else cause
        if wrapped:
            failure.__cause__ = cause
        service = ConsumerService(tls_config=SimpleNamespace(server_name=None) if tls else None)
        address = "https://127.0.0.1:1/device" if tls else "http://127.0.0.1:1/device"
        device = DiscoveredDevice("peer", (address,), (), service=SimpleNamespace(x_addrs=[address]))
        consumer = Tracker()
        with (
            patch.object(consumer, "start_all", side_effect=failure),
            patch.object(consumer_module, "_WsdlSdcConsumer", return_value=consumer) as factory,
        ):
            factory.from_wsd_service.return_value = consumer
            try:
                service.connect(device)
            except consumer_module.ConsumerError as exc:
                check(expected, "only a confirmed TLS alert in HTTP mode gets a transport diagnosis")
                check(
                    "TLS alert" in str(exc) and "HTTP request" in str(exc)
                    and "advertised" in str(exc) and "Enable TLS" in str(exc),
                    "misadvertisement diagnosis explains the TLS response and configuration to check",
                )
                check(exc.__cause__ is failure, "transport diagnosis retains the original error chain")
            except Exception as exc:
                check(not expected, "unconfirmed responses and TLS-mode failures are not misdiagnosed")
                check(exc is failure, "unconfirmed and TLS-mode failures retain their original error")
            else:
                raise AssertionError("failed startup unexpectedly connected")
            check(factory.call_count + factory.from_wsd_service.call_count == 1, "connection failure never retries another transport")
        check(consumer.stop_count == 1, "transport failure rolls back the consumer once")


def metric_subscription_health() -> None:
    """Subscription health follows measurement feeds, not operation-result traffic."""
    episodic = Actions.EpisodicMetricReport.value
    periodic = Actions.PeriodicMetricReport.value
    operation = Actions.OperationInvokedReport.value
    optional = Actions.EpisodicAlertReport.value
    cases = (
        ({episodic: True, operation: True}, {episodic: False, operation: True}, True, "lost metric feed with live operation results"),
        ({episodic: True, periodic: True, operation: True}, {episodic: False, periodic: True, operation: True}, False, "surviving periodic metric alternative"),
        ({episodic: True, optional: True}, {episodic: True, optional: False}, False, "missing optional action with a live metric feed"),
        ({episodic: False, operation: True}, None, True, "initially rejected metric feed"),
        ({}, None, True, "peer with no report subscriptions"),
    )
    for initial, updated, lost, description in cases:
        consumer = consumer_module._WsdlSdcConsumer("http://127.0.0.1:1/device", SdcV1Definitions, None)
        consumer.subscription_status = initial
        consumer._service_clients["StateEventService"] = SimpleNamespace(  # peer-advertised capabilities
            get_available_subscriptions=lambda: [SimpleNamespace(action=episodic), SimpleNamespace(action=periodic)],
        )
        remote = RemoteDevice(consumer, object(), description)
        reasons = []
        remote.on_connection_lost(reasons.append)
        if updated is not None:
            consumer.subscription_status = updated
        REPORT.check((remote.connection_lost_reason is not None) is lost, description)
        REPORT.check(bool(reasons) is lost, f"health callback: {description}")
        if lost:
            consumer.subscription_status = {**consumer.subscription_status, optional: False}
            REPORT.check(len(reasons) == 1, f"connection/data-loss callback is one-shot: {description}")
    consumer = consumer_module._WsdlSdcConsumer("http://127.0.0.1:1/device", SdcV1Definitions, None)
    consumer.subscription_status = {operation: True}
    remote = RemoteDevice(consumer, object(), "operations-only peer")
    REPORT.check(remote.connection_lost_reason is None, "no invented metric-feed requirement for an operations-only peer")


class ObservableMdib:
    metrics_by_handle = ObservableProperty()


def bound_callbacks_stay_alive() -> None:
    """The library holds observers weakly, so an unreferenced lambda would be collected before its first report."""
    mdib = ObservableMdib()
    remote = RemoteDevice(SimpleNamespace(), mdib, "weakly bound peer")
    seen: list[object] = []
    remote.bind(metrics_by_handle=lambda payload: seen.append(payload))
    gc.collect()
    mdib.metrics_by_handle = {"metric": 1}
    REPORT.check(seen == [{"metric": 1}], "a callback that nothing else references still receives notifications")


SOAP_FAULT = (
    '<s12:Fault xmlns:s12="http://www.w3.org/2003/05/soap-envelope"'
    ' xmlns:wse="http://schemas.xmlsoap.org/ws/2004/08/eventing">'
    "<s12:Code><s12:Value>s12:Sender</s12:Value>"
    "<s12:Subcode><s12:Value>wse:InvalidMessage</s12:Value></s12:Subcode></s12:Code>"
    '<s12:Reason><s12:Text xml:lang="en">The message is not valid and cannot be processed</s12:Text></s12:Reason>'
    "</s12:Fault>"
)


def http_error(status: int, reason: str, *, with_fault: bool) -> HTTPReturnCodeError:
    fault = Fault.from_node(etree.fromstring(SOAP_FAULT)) if with_fault else None
    return HTTPReturnCodeError(status, reason, fault)


def http_error_answers() -> None:
    """A peer that answers with an HTTP error is reachable: say what it answered, never call it gone."""
    check = REPORT.check  # record every failed expectation instead of stopping at the first
    for with_fault in (True, False):
        error = http_error(400, "Bad Request", with_fault=with_fault)
        label = "with a SOAP fault" if with_fault else "without a SOAP fault"

        consumer = Tracker()

        class FakeConsumerMdib:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def init_mdib(self) -> None:
                raise error

        with (
            patch.object(consumer_module, "_WsdlSdcConsumer") as factory,
            patch.object(consumer_module, "ConsumerMdib", FakeConsumerMdib),
        ):
            factory.from_wsd_service.return_value = consumer
            try:
                ConsumerService().connect(DiscoveredDevice("peer", (), (), service=SimpleNamespace(x_addrs=[])))
            except ConsumerError as exc:
                text = str(exc)
                check("400" in text and "Bad Request" in text, f"connect names the HTTP status {label}")
                check(
                    ("The message is not valid" in text and "InvalidMessage" in text) is with_fault,
                    f"connect repeats the fault's reason and code only when there is one {label}",
                )
                check(exc.__cause__ is error, f"connect keeps the original error as the cause {label}")
            except Exception as exc:  # noqa: BLE001
                check(False, f"connect raised {type(exc).__name__} instead of ConsumerError {label}")
            else:
                check(False, f"connect succeeded despite the HTTP error {label}")
        check(consumer.stop_count == 1, f"connect rolls the consumer back once {label}")

        remote = RemoteDevice(SimpleNamespace(), object(), f"answering peer {label}")

        def refuse(error: HTTPReturnCodeError = error) -> None:
            raise error

        try:
            remote._send(refuse, 1.0)
        except PeerConnectionLost:
            check(False, f"an HTTP error answer is not a lost connection {label}")
        except ConsumerError as exc:
            check("400" in str(exc) and "Bad Request" in str(exc), f"the request error names the HTTP status {label}")
        else:
            check(False, f"the request error is raised {label}")
        check(remote.connection_lost_reason is None, f"an HTTP error answer leaves the connection alive {label}")

    remote = RemoteDevice(SimpleNamespace(), object(), "unreachable peer")

    def unreachable() -> None:
        raise ConnectionResetError("connection reset by peer")

    try:
        remote._send(unreachable, 1.0)
    except PeerConnectionLost:
        check(remote.connection_lost_reason is not None, "a reset connection still marks the peer as lost")
    else:
        check(False, "a reset connection raises PeerConnectionLost")


def main() -> int:
    print("Service lifecycle fault injection")
    mdib_load_failure()
    provider_start_failure()
    default_context_failure()
    generator_start_failure()
    provider_stop_failures()
    consumer_discovery_start_failure()
    consumer_connection_failure(stage="start")
    consumer_connection_failure(stage="construct")
    consumer_connection_failure(stage="init")
    http_consumer_rejects_https_endpoint()
    tls_alert_connection_diagnostics()
    metric_subscription_health()
    bound_callbacks_stay_alive()
    http_error_answers()
    return REPORT.summary()


if __name__ == "__main__":
    raise SystemExit(main())
