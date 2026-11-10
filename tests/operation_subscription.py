"""Network acceptance: operations need an active OperationInvokedReport subscription.

Some providers refuse set and activate requests from a consumer that is not subscribed to
OperationInvokedReport. sdc11073 only logs a rejected subscription, so the toolbox checks
it after connecting, retries with a dedicated subscription, and refuses to invoke without it.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from script_support import CallbackRecorder, Report, wait_until  # noqa: E402
from sdc11073.consumer.consumerimpl import SdcConsumer  # noqa: E402
from sdc11073.definitions_sdc import SdcV1Definitions  # noqa: E402
from sdc11073.provider.periodicreports import PeriodicStates  # noqa: E402
from sdc11073.provider.porttypes.contextserviceimpl import ContextService  # noqa: E402
from sdc11073.provider.porttypes.porttypebase import (  # noqa: E402
    WSDLMessageDescription,
    WSDLOperationBinding,
    mk_wsdl_one_way_operation,
    msg_prefix,
)
from sdc11073.provider.porttypes.stateeventserviceimpl import StateEventService  # noqa: E402
from sdc11073.provider.porttypes.waveformserviceimpl import WaveformService  # noqa: E402
from sdc11073.provider.subscriptionmgr_base import SubscriptionsManagerBase  # noqa: E402
from sdc11073.xml_types import eventing_types, msg_types, pm_types  # noqa: E402
from sdc11073.xml_types.actions import Actions  # noqa: E402

from sdctoolbox.consumer_service import ConsumerService, DiscoveredDevice  # noqa: E402
from sdctoolbox.model import AlertSpec, MetricKind, MetricSpec, PatientInfo  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402
from sdctoolbox.sample_generation import SamplePublisher  # noqa: E402

OPERATION_INVOKED = Actions.OperationInvokedReport.value
EPISODIC_METRIC = Actions.EpisodicMetricReport.value
PERIODIC_METRIC = Actions.PeriodicMetricReport.value
WAVEFORM = Actions.Waveform.value


def start_all_without_operation_reports(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
    """Stand-in for a provider that rejected the combined subscription containing the report."""
    kwargs["not_subscribed_actions"] = [OPERATION_INVOKED]
    return ORIGINAL_START_ALL(self, *args, **kwargs)


ORIGINAL_START_ALL = SdcConsumer.start_all


def start_all_without_metric_reports(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
    """Stand-in for a connection with the metric action missing from its subscriptions."""
    kwargs["not_subscribed_actions"] = [EPISODIC_METRIC]
    return ORIGINAL_START_ALL(self, *args, **kwargs)


@contextmanager
def capture_warnings() -> Iterator[list[str]]:
    messages: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = Collect(level=logging.WARNING)
    logger = logging.getLogger("sdctoolbox.consumer")
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)


def check_restricted_provider(report: Report) -> None:
    """A real SOAP peer rejects whole filters containing unadvertised reports."""
    unsupported = {
        action.value for action in Actions
        if "Periodic" in action.value or action in (Actions.SystemErrorReport, Actions.EpisodicComponentReport)
    }
    rejected: list[str] = []
    original_subscribe = SubscriptionsManagerBase.on_subscribe_request

    def restricted_subscribe(self, request):
        subscription = eventing_types.Subscribe.from_node(request.message_data.p_msg.msg_node)
        if unsupported.intersection(subscription.Filter.text.split()):
            rejected.append(subscription.Filter.text)
            raise ValueError("unsupported report in subscription filter")
        return original_subscribe(self, request)

    def restrict_wsdl(original):
        def add_port_type(self, parent):
            original(self, parent)
            port_type = parent[-1]
            for operation in list(port_type):
                if any(action.rsplit("/", 1)[-1] == operation.get("name") for action in unsupported):
                    port_type.remove(operation)
        return add_port_type

    provider = ProviderService(instance_name="restricted-report-provider")
    consumer = ConsumerService()
    remote = None
    try:
        with patch.object(StateEventService, "add_wsdl_port_type", restrict_wsdl(StateEventService.add_wsdl_port_type)), \
             patch.object(ContextService, "add_wsdl_port_type", restrict_wsdl(ContextService.add_wsdl_port_type)), \
             patch.object(SubscriptionsManagerBase, "on_subscribe_request", restricted_subscribe):
            provider.start()
            metric = provider.add_metric(MetricSpec(
                label="Report source", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal("1"),
            ))
            alert = provider.add_alert(AlertSpec(label="Report alarm", source_handle=metric))
            consumer.start()
            device = next((d for d in consumer.scan(timeout=10, expected=99) if d.epr == provider.epr.urn), None)
            report.require(device is not None, "restricted provider is discoverable")
            remote = consumer.connect(device)
            report.check(not rejected, "connect requests only WSDL-advertised reports", str(rejected))
            report.check(remote.operation_reports_subscribed(), "restricted peer has operation-result reports")
            provider.set_value(metric, Decimal("7"))
            report.check(wait_until(lambda: remote.metrics()[metric].value == Decimal("7"), timeout=5),
                         "metric notifications reach the consumer MDIB")
            for present in (True, False):
                provider.set_alert_presence(alert, present)
                report.check(wait_until(lambda: remote.alerts()[alert].present == present, timeout=5),
                             f"alert notification reaches the consumer MDIB: present={present}")
                signals = provider.signal_handles_for(alert)
                expected = pm_types.AlertSignalPresence.ON if present else pm_types.AlertSignalPresence.OFF
                report.check(all(remote.mdib.entities.by_handle(h).state.Presence == expected for h in signals),
                             f"visual and audible signals follow the alert: present={present}")
            operation = provider.operation_handle_for(metric)
            provider._set_operating_mode(operation, pm_types.OperatingMode.DISABLED)
            report.check(wait_until(lambda: not remote.metrics()[metric].controllable_now, timeout=5),
                         "operation enabled-state notification reaches the consumer MDIB")
            patient = PatientInfo(given_name="Subscription", family_name="Regression")
            provider.set_patient(patient)
            report.check(wait_until(lambda: remote.patient() == patient, timeout=5),
                         "context notification survives a peer without periodic reports")
    finally:
        if remote is not None:
            remote.close()
        consumer.stop()
        provider.stop()


def check_partial_metric_feed_loss(report: Report) -> None:
    """Use actual SubscriptionEnd messages and a surviving periodic SOAP notification."""
    provider = ProviderService(instance_name="partial-metric-feed-provider")
    service = ConsumerService()
    remote = None

    def separate_metric_subscriptions(self, *args, **kwargs):
        kwargs["not_subscribed_actions"] = [EPISODIC_METRIC, PERIODIC_METRIC]
        return ORIGINAL_START_ALL(self, *args, **kwargs)

    def end(action: str) -> None:
        subscriptions = [
            (manager, subscription)
            for manager in provider._provider._subscriptions_managers.values()
            for subscription in manager._subscriptions.objects
            if subscription.filter_type.text == action
            and subscription.notify_to_address.startswith(remote._consumer.base_url)
        ]
        assert len(subscriptions) == 1, "expected a dedicated metric subscription"
        manager, subscription = subscriptions[0]
        manager._async_send_thread.run_coro(subscription.async_send_notification_end_message())
        assert wait_until(lambda: remote._consumer.subscription_status.get(action) is False, timeout=5)

    try:
        provider.start()
        handle = provider.add_metric(MetricSpec(label="Live metric feed", kind=MetricKind.NUMBER, initial_value=Decimal("1")))
        service.start()
        device = DiscoveredDevice(provider.epr.urn, (provider._provider.get_xaddrs()[0],), (), service=object())
        # HTTP discovery construction needs the same immutable direct-address snapshot.
        device.service = SimpleNamespace(x_addrs=list(device.x_addrs), types=SdcV1Definitions.MedicalDeviceTypesFilter)
        with patch.object(SdcConsumer, "start_all", separate_metric_subscriptions):
            remote = service.connect(device)
        reasons = []
        remote.on_connection_lost(reasons.append)
        end(PERIODIC_METRIC)
        report.check(remote.connection_lost_reason is None, "loss of an optional periodic action keeps episodic metrics live")
        end(EPISODIC_METRIC)
        report.check(
            remote.operation_reports_subscribed() and bool(reasons) and remote.connection_lost_reason is not None,
            "ending the metric feed reports data loss even while OperationInvokedReport remains active",
        )
        remote._consumer.stop_all()  # this peer is reachable; explicitly clean up its remaining subscriptions
        remote = None

        with patch.object(SdcConsumer, "start_all", separate_metric_subscriptions):
            remote = service.connect(device)
        end(EPISODIC_METRIC)
        provider.set_value(handle, Decimal("9"))
        state = provider.mdib.states.descriptor_handle.get_one(handle).mk_copy()
        provider._provider.hosted_services.state_event_service.send_periodic_metric_report(
            [PeriodicStates(provider.mdib.mdib_version, [state])], provider.mdib.mdib_version_group,
        )
        report.check(
            remote.connection_lost_reason is None
            and wait_until(lambda: remote.metrics()[handle].value == Decimal("9"), timeout=5),
            "a surviving periodic metric subscription remains usable after the episodic subscription ends",
        )
    finally:
        if remote is not None:
            remote._consumer.stop_all()
        service.stop()
        provider.stop()


def check_waveform_operation_naming(report: Report) -> None:
    """The waveform operation is called Waveform by sdc11073 and WaveformStream by other peers.

    Both declare the same action, so the subscription must follow the action and not the name.
    """

    def waveform_wsdl(name: str) -> list:
        def add_port_type(self, parent):
            mk_wsdl_one_way_operation(self._mk_port_type_node(parent, True), operation_name=name)

        return [
            patch.object(WaveformService, "add_wsdl_port_type", add_port_type),
            patch.object(
                WaveformService, "WSDLMessageDescriptions",
                (WSDLMessageDescription(name, (f"{msg_prefix}:WaveformStreamReport",)),),
            ),
            patch.object(WaveformService, "WSDLOperationBindings", (WSDLOperationBinding(name, None, "literal"),)),
        ]

    for name in ("Waveform", "WaveformStream"):
        provider = ProviderService(instance_name=f"waveform-{name.lower()}-provider")
        service = ConsumerService()
        remote = None
        try:
            with ExitStack() as stack:
                for patcher in waveform_wsdl(name):
                    stack.enter_context(patcher)
                provider.start()
                handle = provider.add_metric(MetricSpec(label="Named wave", kind=MetricKind.WAVEFORM))
                service.start()
                device = next((d for d in service.scan(timeout=10, expected=99) if d.epr == provider.epr.urn), None)
                report.require(device is not None, f"provider naming the operation {name} is discoverable")
                remote = service.connect(device)
            report.check(
                any(active and WAVEFORM in subscription_filter.split()
                    for subscription_filter, active in remote._consumer.subscription_status.items()),  # noqa: SLF001
                f"the waveform action is subscribed when the WSDL names the operation {name}",
            )
            blocks = CallbackRecorder(dict)
            remote.bind(waveform_by_handle=blocks)
            cursor = blocks.cursor()
            provider.set_samples(handle, [Decimal(1), Decimal(2), Decimal(3)])
            report.check(
                blocks.wait_for(lambda payload: handle in payload, after=cursor, timeout=5.0) is not None,
                f"waveform blocks reach the consumer when the WSDL names the operation {name}",
            )
        finally:
            if remote is not None:
                remote.close()
            service.stop()
            provider.stop()


def check_waveform_without_determination_time(report: Report) -> None:
    """BICEPS leaves DeterminationTime optional, and some peers omit it from their sample arrays."""
    original_prepare = SamplePublisher.prepare

    def prepare_without_time(self, handle, samples, *, determination_time):  # noqa: ANN001, ANN202
        prepared = original_prepare(self, handle, samples, determination_time=determination_time)
        prepared.entity.state.MetricValue.DeterminationTime = None
        return prepared

    block = [Decimal(1), Decimal(2), Decimal(3)]
    provider = ProviderService(instance_name="untimed-waveform-provider")
    service = ConsumerService()
    remote = None
    try:
        provider.start()
        handle = provider.add_metric(MetricSpec(label="Untimed wave", kind=MetricKind.WAVEFORM))
        service.start()
        device = next((d for d in service.scan(timeout=10, expected=99) if d.epr == provider.epr.urn), None)
        report.require(device is not None, "provider is discoverable")
        remote = service.connect(device)
        blocks = CallbackRecorder(lambda payload: {key: list(state.MetricValue.Samples) for key, state in payload.items()})
        remote.bind(waveform_by_handle=blocks)
        cursor = blocks.cursor()
        with patch.object(SamplePublisher, "prepare", prepare_without_time):
            provider.set_samples(handle, block)
        report.require(
            provider.mdib.entities.by_handle(handle).state.MetricValue.DeterminationTime is None,
            "the published block carries no DeterminationTime",
        )
        event = blocks.wait_for(lambda payload: handle in payload, after=cursor, timeout=5.0)
        report.check(event is not None, "a waveform block without DeterminationTime reaches the consumer")
        report.check(
            event is not None and event.payload[handle] == block,
            "its samples arrive intact",
            str(event.payload if event is not None else None),
        )
    finally:
        if remote is not None:
            remote.close()
        service.stop()
        provider.stop()


def check_deliberate_close_is_not_a_loss(report: Report) -> None:
    """Ending our own subscriptions must not look like the peer going away."""
    provider = ProviderService(instance_name="deliberate-close-provider")
    service = ConsumerService()
    remote = None
    try:
        provider.start()
        service.start()
        device = next((d for d in service.scan(timeout=10, expected=99) if d.epr == provider.epr.urn), None)
        report.require(device is not None, "provider is discoverable")
        remote = service.connect(device)
        reasons: list[str] = []
        remote.on_connection_lost(reasons.append)
        with capture_warnings() as warnings:
            remote.close()
        report.check(
            not reasons and remote.connection_lost_reason is None,
            "closing a healthy connection does not notify the connection-lost listeners",
            str(reasons),
        )
        report.check(not warnings, "closing a healthy connection logs no warning", "; ".join(warnings))
        remote = None
    finally:
        if remote is not None:
            remote.close()
        service.stop()
        provider.stop()


def main() -> int:
    report = Report()
    print("OperationInvokedReport subscription")
    provider = ProviderService(instance_name="operation-subscription-provider")
    consumer = ConsumerService()
    remote = None
    try:
        provider.start()
        handle = provider.add_metric(
            MetricSpec(label="Setting", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal("1")),
        )
        choice = provider.add_metric(
            MetricSpec(label="Choice", kind=MetricKind.CHOICE, allowed_values=("OFF", "ON"),
                       controllable=True, initial_value="OFF"),
        )
        consumer.start()
        device = next(
            (candidate for candidate in consumer.scan(timeout=10.0, expected=99) if candidate.epr == provider.epr.urn),
            None,
        )
        report.check(device is not None, "provider is discoverable")
        if device is None:
            return report.summary()

        with capture_warnings() as warnings:
            healthy = consumer.connect(device)
        report.check(
            healthy.operation_reports_subscribed() and not warnings,
            "a normal connection is recognized as subscribed without a retry",
        )
        healthy.close()

        with patch.object(SdcConsumer, "start_all", start_all_without_metric_reports):
            missing_metrics = consumer.connect(device)
        report.check(
            any(active and EPISODIC_METRIC in subscription_filter.split()
                for subscription_filter, active in missing_metrics._consumer.subscription_status.items()),  # noqa: SLF001
            "connecting restores a missing metric-report subscription",
        )
        missing_metrics.close()

        # A peer can still accept sets without delivering metric notifications. GetMdState
        # must reconcile both kinds of controls from the provider rather than guessing that
        # the requested value became the reported one.
        with patch.object(SdcConsumer, "start_all", start_all_without_metric_reports), patch(
            "sdctoolbox.consumer_service._ensure_metric_report_subscription",
        ):
            unreported = consumer.connect(device)
        try:
            number_result = unreported.set_value(handle, Decimal("4"))
            choice_result = unreported.set_value(choice, "ON")
            report.check(
                number_result in (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD)
                and choice_result in (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD)
                and unreported.metrics((handle,))[handle].value == Decimal("4")
                and unreported.metrics((choice,))[choice].value == "ON",
                "number and choice read back correctly when metric notifications are missing",
            )
        finally:
            unreported.close()

        with capture_warnings() as warnings, patch.object(SdcConsumer, "start_all", start_all_without_operation_reports):
            remote = consumer.connect(device)
        report.check(
            any("no active OperationInvokedReport subscription" in message for message in warnings),
            "the missing subscription is detected after connecting",
        )
        report.check(
            remote.operation_reports_subscribed()
            and remote._consumer.subscription_status.get(OPERATION_INVOKED) is True,  # noqa: SLF001
            "a missing OperationInvokedReport subscription is restored with a dedicated one",
        )
        report.check(
            remote.set_value(handle, Decimal("2")) in (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD),
            "set operations work over the dedicated subscription",
        )

        # Simulate the provider ending that subscription (SubscriptionEnd or a failed renew).
        sdc_consumer = remote._consumer  # noqa: SLF001
        sdc_consumer.subscription_status = {
            subscription_filter: active and OPERATION_INVOKED not in subscription_filter.split()
            for subscription_filter, active in sdc_consumer.subscription_status.items()
        }
        report.check(not remote.operation_reports_subscribed(), "an ended subscription is noticed")
        try:
            remote.set_value(handle, Decimal("3"))
        except RuntimeError as exc:
            refusal = str(exc)
        else:
            refusal = ""
        report.check(
            "not subscribed to OperationInvokedReport" in refusal,
            "a set without the subscription fails locally with the reason",
        )
    finally:
        if remote is not None:
            remote.close()
        consumer.stop()
        provider.stop()
    check_restricted_provider(report)
    check_partial_metric_feed_loss(report)
    check_waveform_operation_naming(report)
    check_waveform_without_determination_time(report)
    check_deliberate_close_is_not_a_loss(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
