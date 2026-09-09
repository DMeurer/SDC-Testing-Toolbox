"""Fault-injection checks for provider and consumer startup cleanup."""

from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report  # noqa: E402

from sdctoolbox import consumer_service as consumer_module  # noqa: E402
from sdctoolbox import provider_service as provider_module  # noqa: E402
from sdctoolbox import sample_generation as sample_module  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService, DiscoveredDevice  # noqa: E402
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
    device = DiscoveredDevice("peer", (), (), service=object())

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
    patchers = [patch.object(consumer_module, "SdcConsumer", ConsumerFactory)]
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
    return REPORT.summary()


if __name__ == "__main__":
    raise SystemExit(main())
