"""Consumer side: discover SDC providers and work with their MDIBs.

This module must survive MDIBs it did not create - the goal is to be usable against any SDC
provider, not only against our own. Nothing here may assume our handle scheme, our coding
system, or even that a metric carries a value, a unit or a type. Foreign devices routinely
omit all of those.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from concurrent.futures import wait as wait_for_futures
from copy import copy, deepcopy
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from functools import partial
from http.client import BadStatusLine, HTTPException
from typing import TYPE_CHECKING, Any

from sdc11073 import observableproperties
from sdc11073.consumer.consumerimpl import SdcConsumer, default_components_factory
from sdc11073.consumer.request_handler_deferred import DispatchKeyRegistryDeferred
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.exceptions import ValidationError
from sdc11073.mdib import ConsumerMdib, mdibbase
from sdc11073.mdib.consumermdibxtra import ConsumerMdibMethods
from sdc11073.pysoap.soapclient import HTTPReturnCodeError
from sdc11073.xml_types import eventing_types, msg_types, pm_types
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types.dpws_types import DeviceEventingFilterDialectURI

from . import constants, ports
from .http_server import configure_request_handler
from .mdib_io import live_entities
from .model import (
    MetricKind,
    PatientInfo,
    RemoteAction,
    RemoteAlert,
    RemoteMetric,
    RemoteRange,
    fixed_point_decimal,
    patient_info_from_biceps,
    validate_decimal,
)
from .ports import RangeWSDiscovery
from .security import CertificateInfo, TlsConfig, VerifiedSoapClient, endpoint_host

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger("sdctoolbox.consumer")

ports.install()

# Discovery pacing. Providers answer a probe within about half a second; re-probing covers
# lost multicast datagrams without flooding the segment.
SCAN_POLL_SECONDS = 0.2
PROBE_REPEAT_SECONDS = 1.0
PROBE_SEND_SECONDS = 0.01
# Continuous discovery: a background probe this often refreshes every provider that is
# still there, and one not heard from for STALE_AFTER_SECONDS (crashed, no Bye) drops off.
BACKGROUND_PROBE_SECONDS = 30.0
STALE_AFTER_SECONDS = 2.5 * BACKGROUND_PROBE_SECONDS
# An operation that waits for its result looks this often at whether the peer was declared lost.
LOST_CHECK_SECONDS = 0.25
# Every consumer compiles its XML schema validators when it is built. When two compile at the
# same moment, libxml2 can skip an import without an error and leave one validator without any
# declarations, so every request of that consumer then fails validation. Builds take turns.
_CONSUMER_BUILD_LOCK = threading.Lock()
_EMPTY_SCHEMA_MESSAGE = "No matching global declaration"

# Descriptor node types we recognize as metrics, mapped to our own kind enum.
METRIC_NODE_TYPES = {
    pm.NumericMetricDescriptor: MetricKind.NUMBER,
    pm.StringMetricDescriptor: MetricKind.TEXT,
    pm.EnumStringMetricDescriptor: MetricKind.CHOICE,
    pm.RealTimeSampleArrayMetricDescriptor: MetricKind.WAVEFORM,
    pm.DistributionSampleArrayMetricDescriptor: MetricKind.DISTRIBUTION,
}

# Operation node types that set a metric value. Other operation kinds exist but do not
# produce an editor widget for a metric.
SET_OPERATION_NODE_TYPES = frozenset(
    {
        pm.SetValueOperationDescriptor,
        pm.SetStringOperationDescriptor,
    },
)

SET_OPERATION_BY_METRIC_KIND = {
    MetricKind.NUMBER: pm.SetValueOperationDescriptor,
    MetricKind.TEXT: pm.SetStringOperationDescriptor,
    MetricKind.CHOICE: pm.SetStringOperationDescriptor,
}


def _first_text(coded_value: Any) -> str | None:
    """Best-effort human label from a CodedValue, tolerating every field being absent."""
    if coded_value is None:
        return None
    descriptions = getattr(coded_value, "ConceptDescription", None) or []
    for description in descriptions:
        text = getattr(description, "text", None)
        if text:
            return str(text)
    return None


def _scope_uris(service: Any) -> tuple[str, ...]:
    """Extract scope URIs from a discovery hit.

    ``Service.scopes`` is a ``ScopesType`` whose URI list lives in ``.text``, not an
    iterable of strings. Fall back to plain iteration so a future change does not break us.
    """
    scopes = getattr(service, "scopes", None)
    if scopes is None:
        return ()
    text = getattr(scopes, "text", None)
    if text is not None:
        return tuple(str(uri) for uri in text)
    try:
        return tuple(str(uri) for uri in scopes)
    except TypeError:
        return (str(scopes),)


# Descriptor node types that carry an alarm condition.
ALERT_CONDITION_NODE_TYPES = frozenset(
    {
        pm.AlertConditionDescriptor,
        pm.LimitAlertConditionDescriptor,
    },
)


def _enum_value(value: Any) -> str | None:
    """The wire value of a BICEPS enum, tolerating None and plain strings."""
    if value is None:
        return None
    return getattr(value, "value", None) or str(value)


def _operation_is_enabled(state: Any) -> bool:
    """Whether an operation explicitly has the mandatory enabled mode."""
    mode = getattr(state, "OperatingMode", None)
    return isinstance(mode, str) and mode == pm_types.OperatingMode.ENABLED


def _failure_reason(info: Any) -> str:
    """The error code and texts a peer gave for an invocation, empty when it gave none."""
    code = _enum_value(getattr(info, "InvocationError", None))
    texts = ", ".join(text.text for text in info.InvocationErrorMessage or () if text.text)
    return ": ".join(part for part in (code, texts) if part)


def _seconds(duration: Any) -> Decimal | None:
    """A SamplePeriod as a number of seconds.

    sdc11073 hands back a float for an xsd:duration. A foreign device may publish something
    this cannot read, in which case the waveform is still shown, just without its rate.
    """
    if duration is None:
        return None
    try:
        return Decimal(str(float(duration)))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _sequence(value: Any) -> tuple[Any, ...]:
    """Read an optional BICEPS sequence without treating text as characters."""
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return ()


def _string_values(items: Any, attribute: str | None = None) -> tuple[str, ...]:
    """Read string values while isolating malformed members of a foreign collection."""
    values = []
    for item in _sequence(items):
        value = item if attribute is None else getattr(item, attribute, None)
        if value is not None:
            values.append(str(value))
    return tuple(values)


def _ranges(value: Any) -> tuple[RemoteRange, ...]:
    """Copy every readable range, including StepWidth, into an immutable snapshot."""
    return tuple(
        RemoteRange(
            lower=getattr(item, "Lower", None),
            upper=getattr(item, "Upper", None),
            step_width=getattr(item, "StepWidth", None),
        )
        for item in _sequence(value)
        if item is not None
    )


def _first_bounds(ranges: tuple[RemoteRange, ...]) -> tuple[Any, Any]:
    """Return the first range's bounds for the legacy single-range display fields."""
    if not ranges:
        return None, None
    return ranges[0].lower, ranges[0].upper


class _PeriodicConsumerMdibMethods(ConsumerMdibMethods):
    """Apply periodic reports because sdc11073's stock MDIB helper only binds episodic ones."""

    _PERIODIC_REPORTS = {
        "periodic_metric_report": ("PeriodicMetricReport", "process_incoming_metric_states_report"),
        "periodic_alert_report": ("PeriodicAlertReport", "process_incoming_alert_states_report"),
        "periodic_component_report": ("PeriodicComponentReport", "process_incoming_component_states_report"),
        "periodic_operational_state_report": (
            "PeriodicOperationalStateReport",
            "process_incoming_operational_states_report",
        ),
        "periodic_context_report": ("PeriodicContextReport", "process_incoming_context_states_report"),
    }

    def bind_to_client_observables(self) -> None:
        super().bind_to_client_observables()
        self._periodic_report_callbacks = {
            observable_name: partial(self._on_periodic_report, parser_name, processor_name)
            for observable_name, (parser_name, processor_name) in self._PERIODIC_REPORTS.items()
        }
        # observableproperties keeps weak references, so the generated partials must live
        # on this helper for as long as the MDIB does.
        observableproperties.bind(self._sdc_client, **self._periodic_report_callbacks)

    def _on_waveform_report(self, received_message_data: Any) -> None:
        # BICEPS leaves @DeterminationTime optional, but sdc11073 adds the sample offsets to it and
        # drops the whole block when it is missing, so a peer that omits it gets the arrival time.
        arrived = str(int(time.time() * 1000))
        for value in received_message_data.p_msg.msg_node.iter(pm.MetricValue.text):
            if value.get("DeterminationTime") is None:
                value.set("DeterminationTime", arrived)
        super()._on_waveform_report(received_message_data)

    def _on_periodic_report(
        self,
        parser_name: str,
        processor_name: str,
        received_message_data: Any,
    ) -> None:
        parser = getattr(self._mdib.data_model.msg_types, parser_name)
        report = parser.from_node(received_message_data.p_msg.msg_node)
        processor = getattr(self._mdib, processor_name)
        processor(received_message_data.mdib_version_group, report)


class _TolerantEntityGetter(mdibbase.EntityGetter):
    """Return a stateless entity when the peer omitted a single-state descriptor's state.

    BICEPS requires exactly one state per single-state descriptor, but a peer can still
    leave one out. sdc11073 then raises KeyError, which aborts every walk over the MDIB
    and hides the whole peer. The readers here already treat a None state as unknown.
    """

    def _mk_entity(self, descriptor: Any) -> mdibbase.Entity | mdibbase.MultiStateEntity:
        if descriptor.is_context_descriptor or self._mdib.states.descriptor_handle.get(descriptor.Handle):
            return super()._mk_entity(descriptor)
        return mdibbase.Entity(self._mdib, deepcopy(descriptor), None)


class _TolerantConsumerMdib(ConsumerMdib):
    """ConsumerMdib that stays readable when the peer's MDIB lacks single states."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.entities = _TolerantEntityGetter(self)

    def reload_all(self) -> None:
        super().reload_all()
        with self.mdib_lock:
            missing = sorted(
                descriptor.Handle
                for descriptor in self.descriptions.objects
                if not descriptor.is_context_descriptor
                and not self.states.descriptor_handle.get(descriptor.Handle)
            )
        if missing:
            logger.warning(
                "peer MDIB has no state for %d descriptor(s); BICEPS requires exactly one each, "
                "so they are shown with unknown state: %s",
                len(missing),
                ", ".join(missing),
            )


@dataclass
class DiscoveredDevice:
    """A provider seen on the network, before connecting to it."""

    epr: str
    x_addrs: tuple[str, ...]
    scopes: tuple[str, ...]
    service: Any = field(repr=False, default=None)

    @property
    def location_scope(self) -> str | None:
        """The sdc.ctxt.loc scope, if the provider published one."""
        for scope in self.scopes:
            if scope.startswith("sdc.ctxt.loc:"):
                return scope
        return None


@dataclass(frozen=True)
class _SetOperation:
    """The state relevant to selecting one set operation from a foreign MDIB."""

    handle: str
    node_type: Any
    enabled: bool
    allowed_ranges: tuple[RemoteRange, ...]
    allowed_values: tuple[str, ...]


class ConsumerError(RuntimeError):
    """An expected failure while talking to a peer. The message is written for the user."""


class PeerConnectionLost(ConsumerError):
    """The peer is gone: shut down, restarted, or unreachable."""


class OperationReportsMissing(ConsumerError):
    """The peer is there, but without an OperationInvokedReport subscription it refuses operations."""


_WSDL_NS = "{http://schemas.xmlsoap.org/wsdl/}"


def _declared_notifications(wsdl: Any, port_type_name: str) -> dict[str, str | None] | None:
    """The notifications a port type declares, each with the soapAction of its binding.

    A notification is a one-way operation with an output and no input. None means the WSDL
    has no such port type.
    """
    port_type = wsdl.find(f"{_WSDL_NS}portType[@name='{port_type_name}']")
    if port_type is None:
        return None
    soap_actions: dict[str | None, str] = {}
    for binding in wsdl.findall(f"{_WSDL_NS}binding"):
        if binding.get("type", "").rpartition(":")[2] != port_type_name:
            continue
        for operation in binding.findall(f"{_WSDL_NS}operation"):
            for child in operation:
                if child.get("soapAction"):
                    soap_actions[operation.get("name")] = child.get("soapAction")
    return {
        operation.get("name"): soap_actions.get(operation.get("name"))
        for operation in port_type.findall(f"{_WSDL_NS}operation")
        if operation.get("name")
        and operation.find(f"{_WSDL_NS}input") is None
        and operation.find(f"{_WSDL_NS}output") is not None
    }


class _StoppableDispatcher(DispatchKeyRegistryDeferred):
    """sdc11073's deferred request dispatcher, with a worker thread that can be ended.

    The library starts one worker per consumer that waits on its queue for ever and offers no way
    to stop it. Every connection, even a failed one, then left a thread behind that kept the closed
    consumer, its MDIB and its sockets alive.
    """

    def _read_queue(self) -> None:
        while (queued := self._queue.get()) is not None:
            func, request, action = queued
            try:
                func(request)
            except Exception:  # noqa: BLE001 - one failing handler must not end the worker
                self._logger.error(  # noqa: PLE1205
                    'method {} for action "{}" failed:{}', func.__name__, action, traceback.format_exc(),
                )

    def stop(self) -> None:
        """End the worker once it has handled the requests it already accepted."""
        self._queue.put(None)


class _WsdlSdcConsumer(SdcConsumer):
    """Limit subscriptions before start_all groups actions by hosted service."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        components = kwargs.get("components") or default_components_factory()
        kwargs["components"] = replace(components, action_dispatcher_class=_StoppableDispatcher)
        super().__init__(*args, **kwargs)
        self.peer_end_statements: list[str] = []

    def stop_all(self, unsubscribe: bool = True) -> None:
        """Stop everything, the notification worker included: a stopped consumer is gone for good."""
        try:
            super().stop_all(unsubscribe)
        finally:
            self._services_dispatcher.stop()

    def _on_subscription_end(self, request_data: Any) -> Any:
        # sdc11073 flags the subscription as ended before it stores the peer's status and reason,
        # and the connection watcher reacts to that flag, so the statement is written down first.
        statement = _subscription_end_statement(request_data)
        if statement and statement not in self.peer_end_statements:
            self.peer_end_statements.append(statement)
        return super()._on_subscription_end(request_data)

    def _start_event_sink(self, shared_http_server: Any, http_server_start_timeout: float = 60.0) -> None:
        super()._start_event_sink(shared_http_server, http_server_start_timeout)
        if shared_http_server is None:
            configure_request_handler(self._http_server)

    def _mk_hosted_services(self, host_description: Any) -> None:
        super()._mk_hosted_services(host_description)
        for hosted in self.hosted_services.values():
            if hosted.wsdl_node is None:
                continue
            for name, client in hosted.services.items():
                declared = _declared_notifications(hosted.wsdl_node, name)
                if declared is None:
                    continue
                # get_available_subscriptions() lists library capabilities, not peer support.
                # Operation names differ between implementations (sdc11073 declares Waveform
                # where others declare WaveformStream), so the binding's soapAction counts too.
                declared_actions = {
                    action
                    for operation, soap_action in declared.items()
                    for action in (_action_uri(getattr(self.sdc_definitions.Actions, operation, "")), soap_action)
                    if action
                }
                client.notifications = tuple(
                    key for key in client.get_available_subscriptions()
                    if _action_uri(key.action) in declared_actions
                )
                subscribed = {_action_uri(key.action) for key in client.notifications}
                for operation, soap_action in declared.items():
                    named = _action_uri(getattr(self.sdc_definitions.Actions, operation, ""))
                    if named not in subscribed and soap_action not in subscribed:
                        logger.warning(
                            "the peer declares the notification %s.%s, which this consumer cannot subscribe to",
                            name,
                            operation,
                        )


def _error_text(exc: BaseException) -> str:
    """Name the underlying socket error; sdc11073 wraps it in a message-less NotConnected."""
    while not str(exc) and (exc.__cause__ or exc.__context__) is not None:
        exc = exc.__cause__ or exc.__context__
    text = str(exc.args[1]) if isinstance(exc, OSError) and len(exc.args) > 1 else str(exc)
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


def _http_error_text(error: HTTPReturnCodeError) -> str:
    """Say what a peer's HTTP error answer contained; the library's exception has an empty str()."""
    text = f"HTTP {error.status} {error.reason}".strip()
    try:
        fault = error.soap_fault  # None when the peer sent no parsable fault
        reason = next((item.text for item in fault.Reason.Text if item.text), None)
        codes = [
            str(getattr(code, "value", code)).rpartition("}")[2]
            for code in (fault.Code.Value, getattr(fault.Code.Subcode, "Value", None))
            if code is not None
        ]
    except Exception:  # noqa: BLE001 - a malformed fault must not hide the status line
        return text
    if reason:
        text = f"{text}: {reason}"
    return f"{text} ({', '.join(codes)})" if codes else text


def _action_uri(action: object) -> str:
    # sdc11073's Actions is a str-mixin Enum; str() of one yields "Actions.X", not the URI.
    return str(getattr(action, "value", action))


def _operation_report_actions(consumer: SdcConsumer) -> set[str]:
    client = getattr(consumer, "set_service_client", None)
    if client is None:
        return set()
    return {_action_uri(key.action) for key in client.get_available_subscriptions()}


def _operation_reports_subscribed(consumer: SdcConsumer) -> bool:
    actions = _operation_report_actions(consumer)
    if not actions:
        return True  # no SetService, so nothing to invoke and nothing to require
    return any(active and actions & set(subscription_filter.split())
               for subscription_filter, active in consumer.subscription_status.items())


def _metric_report_actions(consumer: SdcConsumer) -> set[str]:
    client = getattr(consumer, "state_event_service_client", None)
    actions = {_action_uri(key.action) for key in client.get_available_subscriptions()} if client is not None else set()
    actions.update(action for subscription_filter in consumer.subscription_status for action in subscription_filter.split())
    return {action for action in actions if action.rsplit("/", 1)[-1] in ("EpisodicMetricReport", "PeriodicMetricReport")}


def _subscription_summary(consumer: SdcConsumer) -> str:
    """One readable line per subscription: its state and the short action names it covers."""
    parts = []
    for subscription_filter, active in consumer.subscription_status.items():
        names = ", ".join(action.rsplit("/", 1)[-1] for action in subscription_filter.split())
        parts.append(f"{'active' if active else 'inactive'}: {names}")
    return "; ".join(parts) or "no subscriptions"


def _short_action(action: str) -> str:
    return action.rsplit("/", 1)[-1]


def _active_report_actions(consumer: SdcConsumer) -> set[str]:
    return {
        action
        for subscription_filter, active in (getattr(consumer, "subscription_status", None) or {}).items()
        if active
        for action in subscription_filter.split()
    }


def _declared_reports(consumer: SdcConsumer) -> list[tuple[Any, Any]]:
    """Every report the peer declares, each with the hosted service it is subscribed at."""
    description = getattr(consumer, "host_description", None)
    reports = []
    for hosted in description.relationship.Hosted if description is not None else ():
        for port_type in hosted.Types or ():
            client = consumer.client(port_type.localname)
            if client is not None:
                reports.extend((hosted, key) for key in client.get_available_subscriptions())
    return reports


def _missing_reports(consumer: SdcConsumer) -> list[str]:
    """Short names of the declared reports that no active subscription delivers."""
    active = _active_report_actions(consumer)
    return sorted(
        {
            _short_action(_action_uri(key.action))
            for _hosted, key in _declared_reports(consumer)
            if _action_uri(key.action) not in active
        },
    )


def _ensure_report_subscriptions(consumer: SdcConsumer) -> None:
    """Subscribe, one report at a time, to every declared report the combined subscriptions missed.

    start_all() subscribes once per hosted service with every action that service offers. One
    action the peer dislikes, or a limit on how many subscribers it serves, gets the whole
    combined request refused, and sdc11073 only logs that. Requested alone, the reports are
    usually accepted. Without OperationInvokedReport a provider may refuse every operation,
    without EpisodicOperationalStateReport action buttons go stale, and without the waveform
    or description reports the consumer silently shows an incomplete device.
    """
    for hosted, key in _declared_reports(consumer):
        action = _action_uri(key.action)
        if action in _active_report_actions(consumer):
            continue
        logger.warning(
            "no active %s subscription (%s); retrying alone",
            _short_action(action),
            _subscription_summary(consumer),
        )
        filter_type = eventing_types.FilterType()
        filter_type.text = action
        filter_type.Dialect = DeviceEventingFilterDialectURI.ACTION
        try:
            consumer.do_subscribe(hosted, filter_type, [key])
        except Exception:
            logger.exception("dedicated %s subscription failed", _short_action(action))
    missing = _missing_reports(consumer)
    if missing:
        logger.warning("the device still refuses: %s", ", ".join(missing))


def _subscription_end_statement(request_data: Any) -> str:
    """The status and reason a SubscriptionEnd message carries, e.g. "SourceCancelling: too many subscribers"."""
    try:
        message = eventing_types.SubscriptionEnd.from_node(request_data.message_data.p_msg.msg_node)
    except Exception:  # sdc11073 reports a malformed message itself
        return ""
    reasons = (str(getattr(reason, "text", reason) or "") for reason in message.Reason)
    return ": ".join(
        part for part in ((message.Status or "").rpartition("/")[2], " / ".join(r for r in reasons if r)) if part
    )


def _ended_by_device(consumer: SdcConsumer) -> str:
    """What the peer itself said when it ended its subscriptions; empty when it said nothing."""
    return "; ".join(getattr(consumer, "peer_end_statements", ()))


class RemoteDevice:
    """A connected peer. Wraps SdcConsumer plus its ConsumerMdib."""

    def __init__(
        self,
        consumer: SdcConsumer,
        mdib: ConsumerMdib,
        epr: str,
        peer_certificate: CertificateInfo | None = None,
    ) -> None:
        self._consumer = consumer
        self._mdib = mdib
        self.epr = epr
        self.peer_certificate = peer_certificate
        self._lock = threading.RLock()
        self._closing = False
        self._lost_reason: str | None = None
        self._lost_listeners: list[Callable[[str], None]] = []
        self._bound_callbacks: list[Callable[[Any], None]] = []
        # Why the latest set_value or run_action was refused. Empty after an accepted one.
        self.last_invocation_detail = ""
        status = getattr(consumer, "subscription_status", None)
        self._metric_report_actions = _metric_report_actions(consumer) if status is not None else set()
        if status is not None:
            # A clean shutdown ends every subscription (SubscriptionEnd); a crashed or
            # unreachable peer shows up when the renewals fail.
            observableproperties.bind(consumer, subscription_status=self._on_subscription_status)
            self._on_subscription_status(consumer.subscription_status)

    # -- connection health ---------------------------------------------------------

    @property
    def connection_lost_reason(self) -> str | None:
        """Why the peer is considered gone, or None while the connection looks healthy."""
        return self._lost_reason

    def on_connection_lost(self, listener: Callable[[str], None]) -> None:
        """Call `listener(reason)` once when the connection is lost.

        It may run on an sdc11073 thread. Never touch Qt widgets from it.
        """
        with self._lock:
            reason = self._lost_reason
            if reason is None:
                self._lost_listeners.append(listener)
                return
        listener(reason)

    def mark_connection_lost(self, reason: str) -> None:
        """Record that the peer is gone and tell the listeners, once."""
        with self._lock:
            if self._lost_reason is not None:
                return
            self._lost_reason = reason
            listeners, self._lost_listeners = self._lost_listeners, []
        logger.warning("connection to %s lost: %s", self.epr, reason)
        for listener in listeners:
            try:
                listener(reason)
            except Exception:
                logger.exception("connection-lost listener failed")

    def _on_subscription_status(self, status: dict[str, bool]) -> None:
        if self._closing:
            return  # our own unsubscribe ends every subscription; that is not the peer going away
        if not any(status.values()):
            said = _ended_by_device(self._consumer)
            self.mark_connection_lost(
                f"the device ended its subscriptions ({said})"
                if said
                else "the device has no active report subscriptions (it shut down, restarted or became unreachable)",
            )
        elif self._metric_report_actions and not any(
            active and self._metric_report_actions.intersection(subscription_filter.split())
            for subscription_filter, active in status.items()
        ):
            self.mark_connection_lost(
                "the device has no active metric-report subscription; measurement data is no longer live",
            )

    def _send(self, request: Callable[[], Any], timeout: float) -> Any:
        """Send one operation request and wait for its final report part.

        A request that cannot be delivered means the peer is gone. A delivered request whose
        report does not arrive in time is only reported, since a slow device is still there.
        """
        if self._lost_reason is not None:
            raise PeerConnectionLost(f"connection lost: {self._lost_reason}")
        self._require_operation_reports()
        try:
            future = request()
        except ConsumerError:
            raise
        except HTTPReturnCodeError as exc:
            # The peer answered, so it is still there; it only refused this request.
            msg = f"the device refused the request: {_http_error_text(exc)}"
            raise ConsumerError(msg) from exc
        except (OSError, HTTPException) as exc:
            reason = f"the device could not be reached ({_error_text(exc)})"
            self.mark_connection_lost(reason)
            raise PeerConnectionLost(f"connection lost: {reason}") from exc
        deadline = time.monotonic() + timeout
        while True:
            done, _ = wait_for_futures([future], timeout=max(0.0, min(deadline - time.monotonic(), LOST_CHECK_SECONDS)))
            if done:
                return future.result()
            if self._lost_reason is not None:
                raise PeerConnectionLost(f"connection lost: {self._lost_reason}")
            if time.monotonic() >= deadline:
                msg = f"the device accepted the request but sent no final result within {timeout:g} s"
                raise ConsumerError(msg)

    def operation_reports_subscribed(self) -> bool:
        """Whether an active subscription delivers this peer's OperationInvokedReports.

        Providers may refuse set and activate requests from a consumer without one
        (SDPi requires it), and sdc11073 only logs a failed or ended subscription.
        """
        return _operation_reports_subscribed(self._consumer)

    def missing_reports(self) -> list[str]:
        """Short names of the reports the peer declares but no active subscription delivers.

        A peer can refuse a subscription without ending the connection, and the device then
        shows up incomplete (for example without waveforms) with nothing to say why.
        """
        if getattr(self._consumer, "subscription_status", None) is None:
            return []
        return _missing_reports(self._consumer)

    def _require_operation_reports(self) -> None:
        if not self.operation_reports_subscribed():
            msg = (
                "not subscribed to OperationInvokedReport: the provider rejected or ended the "
                "subscription, and will refuse operations. Reconnect, or see the log for the reason."
            )
            raise OperationReportsMissing(msg)

    # -- reading -------------------------------------------------------------------

    @property
    def mdib(self) -> ConsumerMdib:
        """The consumer-side MDIB."""
        return self._mdib

    @property
    def friendly_name(self) -> str | None:
        """The first usable localized DPWS device name, when the peer advertises one."""
        host = getattr(self._consumer, "host_description", None)
        device = getattr(host, "this_device", None)
        for name in _sequence(getattr(device, "FriendlyName", None)):
            text = getattr(name, "text", None)
            if isinstance(text, str) and text.strip():
                return text.strip()
        return None

    def metrics(self, handles: Iterable[str] | None = None) -> dict[str, RemoteMetric]:
        """Build defensive snapshots of the requested metrics on the peer.

        Unknown node types are skipped rather than guessed at; callers that want to show
        them can walk ``mdib.entities`` themselves. With no handles, snapshot every metric.
        """
        wanted = None if handles is None else frozenset(handles)
        if wanted == frozenset():
            return {}
        with self._lock, self._mdib.mdib_lock:
            operations_by_target = self._operation_index(wanted)
            result: dict[str, RemoteMetric] = {}

            for entity in live_entities(self._mdib, wanted):
                handle = entity.handle
                node_type = getattr(entity, "node_type", None)
                kind = METRIC_NODE_TYPES.get(node_type)
                if kind is None:
                    continue

                descriptor = getattr(entity, "descriptor", None)
                state = getattr(entity, "state", None)

                metric_allowed_values = _string_values(getattr(descriptor, "AllowedValue", None), "Value")
                metric_value = getattr(state, "MetricValue", None)
                unit = getattr(descriptor, "Unit", None)
                unit_system = getattr(unit, "CodingSystem", None)

                technical_ranges = _ranges(getattr(descriptor, "TechnicalRange", None))
                technical_lower, technical_upper = _first_bounds(technical_ranges)
                operation_type = SET_OPERATION_BY_METRIC_KIND.get(kind)
                operations = [
                    operation
                    for operation in operations_by_target.get(handle, ())
                    if operation.node_type == operation_type
                ]
                selected_operation = next((operation for operation in operations if operation.enabled), None)
                allowed_ranges = (
                    selected_operation.allowed_ranges if selected_operation is not None else ()
                )
                lower, upper = _first_bounds(allowed_ranges)

                result[handle] = RemoteMetric(
                    handle=handle,
                    node_type_name=getattr(node_type, "localname", str(node_type)),
                    kind=kind,
                    label=_first_text(getattr(descriptor, "Type", None)),
                    unit_label=_first_text(unit),
                    type_code=getattr(getattr(descriptor, "Type", None), "Code", None),
                    allowed_values=metric_allowed_values,
                    operation_allowed_values=(
                        selected_operation.allowed_values if selected_operation is not None else ()
                    ),
                    resolution=getattr(descriptor, "Resolution", None),
                    minimum=lower,
                    maximum=upper,
                    allowed_ranges=allowed_ranges,
                    technical_minimum=technical_lower,
                    technical_maximum=technical_upper,
                    technical_ranges=technical_ranges,
                    value=getattr(metric_value, "Value", None),
                    # A sample array carries Samples instead of Value, so a peer's waveform
                    # would otherwise look like a metric that never reports anything.
                    samples=_sequence(getattr(metric_value, "Samples", None)),
                    sample_period=_seconds(getattr(descriptor, "SamplePeriod", None)),
                    domain_unit_label=_first_text(getattr(descriptor, "DomainUnit", None)),
                    domain_minimum=getattr(getattr(descriptor, "DistributionRange", None), "Lower", None),
                    domain_maximum=getattr(getattr(descriptor, "DistributionRange", None), "Upper", None),
                    parent_handle=getattr(entity, "parent_handle", None),
                    operation_handles=tuple(operation.handle for operation in operations),
                    selected_operation_handle=(
                        selected_operation.handle if selected_operation is not None else None
                    ),
                    controllable_now=selected_operation is not None,
                    validity=_enum_value(getattr(getattr(metric_value, "MetricQuality", None), "Validity", None)),
                    activation=_enum_value(getattr(state, "ActivationState", None)),
                    unit_code=getattr(unit, "Code", None),
                    unit_coding_system=(
                        constants.CODING_SYSTEM_MDC if unit is not None and unit_system is None else unit_system
                    ),
                )
            return result

    def patient_contexts(self) -> dict[str, PatientInfo]:
        """Associated peer patients keyed by PatientContext descriptor handle.

        A multi-MDS device can expose more than one PatientContext. Entity getters already
        return MDIB-locked copies, so do not update those copies again while reading them.
        """
        with self._lock:
            entities = self._mdib.entities.by_node_type(pm.PatientContextDescriptor)
        patients: dict[str, PatientInfo] = {}
        for entity in sorted(entities, key=lambda candidate: candidate.handle):
            associated = [
                state
                for state in entity.states.values()
                if getattr(state, "ContextAssociation", None) == pm_types.ContextAssociation.ASSOCIATED
            ]
            if len(associated) == 1:
                patients[entity.handle] = patient_info_from_biceps(getattr(associated[0], "CoreData", None))
            elif len(associated) > 1:
                logger.warning("peer has %d associated states for patient context %s", len(associated), entity.handle)
        return patients

    def patient(self) -> PatientInfo:
        """The associated peer patient when the peer exposes exactly one context."""
        patients = self.patient_contexts()
        if len(patients) == 1:
            return next(iter(patients.values()))
        return PatientInfo()

    def _operation_index(self, targets: frozenset[str] | None = None) -> dict[str, list[_SetOperation]]:
        """Index the peer's set operations by the metric they target.

        State and AllowedRange stay attached to their operation handle so selecting an
        enabled operation cannot accidentally borrow another operation's range.
        """
        by_target: dict[str, list[_SetOperation]] = {}

        for entity in live_entities(self._mdib):
            handle = entity.handle
            node_type = getattr(entity, "node_type", None)
            if node_type not in SET_OPERATION_NODE_TYPES:
                continue
            target = getattr(getattr(entity, "descriptor", None), "OperationTarget", None)
            if not target or targets is not None and target not in targets:
                continue

            state = getattr(entity, "state", None)
            allowed_values = getattr(getattr(state, "AllowedValues", None), "Value", None)
            by_target.setdefault(target, []).append(
                _SetOperation(
                    handle=handle,
                    node_type=node_type,
                    enabled=_operation_is_enabled(state),
                    allowed_ranges=_ranges(getattr(state, "AllowedRange", None)),
                    allowed_values=_string_values(allowed_values),
                ),
            )

        return by_target

    def alerts(self) -> dict[str, RemoteAlert]:
        """Build a defensive snapshot of every alarm condition on the peer.

        Signals are matched back to their condition through ConditionSignaled, so a
        condition announced three different ways still appears once, with its three signals
        listed against it.
        """
        with self._lock, self._mdib.mdib_lock:
            entities = live_entities(self._mdib)
            signals_by_condition: dict[str, dict[str, str]] = {}
            for entity in entities:
                handle = entity.handle
                if getattr(entity, "node_type", None) != pm.AlertSignalDescriptor:
                    continue
                descriptor = getattr(entity, "descriptor", None)
                condition = getattr(descriptor, "ConditionSignaled", None)
                if not condition:
                    continue
                manifestation = getattr(descriptor, "Manifestation", None)
                signals_by_condition.setdefault(condition, {})[handle] = (
                    getattr(manifestation, "value", None) or str(manifestation)
                )

            result: dict[str, RemoteAlert] = {}
            for entity in entities:
                handle = entity.handle
                node_type = getattr(entity, "node_type", None)
                if node_type not in ALERT_CONDITION_NODE_TYPES:
                    continue

                descriptor = getattr(entity, "descriptor", None)
                state = getattr(entity, "state", None)
                lower, upper = None, None
                limits = getattr(state, "Limits", None)
                if limits is not None:
                    lower = getattr(limits, "Lower", None)
                    upper = getattr(limits, "Upper", None)
                max_lower, max_upper = None, None
                max_limits = getattr(descriptor, "MaxLimits", None)
                if max_limits is not None:
                    max_lower = getattr(max_limits, "Lower", None)
                    max_upper = getattr(max_limits, "Upper", None)
                actual_priority = getattr(state, "ActualPriority", None)

                result[handle] = RemoteAlert(
                    handle=handle,
                    node_type_name=getattr(node_type, "localname", str(node_type)),
                    label=_first_text(getattr(descriptor, "Type", None)),
                    kind=_enum_value(getattr(descriptor, "Kind", None)),
                    priority=_enum_value(
                        actual_priority if actual_priority is not None else getattr(descriptor, "Priority", None),
                    ),
                    present=bool(getattr(state, "Presence", False)),
                    activation=_enum_value(getattr(state, "ActivationState", None)),
                    source_handles=_string_values(getattr(descriptor, "Source", None)),
                    lower_limit=lower,
                    upper_limit=upper,
                    max_lower_limit=max_lower,
                    max_upper_limit=max_upper,
                    signals=signals_by_condition.get(handle, {}),
                )
            return result

    # -- writing -------------------------------------------------------------------

    def set_value(
            self,
            metric_handle: str,
            value: Decimal | str,
            timeout: float = 10.0,
    ) -> msg_types.InvocationState:
        """Remote-control a metric on the peer and wait for the final InvocationState.

        Returns FAILED when the metric has no usable operation, so callers get one
        consistent failure mode instead of an exception for some cases and a state for others.
        """
        if isinstance(value, float):
            msg = "use Decimal, never float"
            raise TypeError(msg)

        self.last_invocation_detail = ""
        metrics = self.metrics((metric_handle,))
        metric = metrics.get(metric_handle)
        if metric is None or not metric.operation_handles:
            return self._not_sent("no set operation targets %s", metric_handle)

        operation_handle = metric.selected_operation_handle
        if operation_handle is None:
            return self._not_sent("no enabled set operation targets %s", metric_handle)
        client = self._consumer.set_service_client

        if metric.kind is MetricKind.NUMBER:
            try:
                numeric_value = value if isinstance(value, Decimal) else Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                msg = f"{value!r} is not a number for {metric_handle!r}"
                raise ValueError(msg) from exc
            wire_value = fixed_point_decimal(
                validate_decimal(numeric_value, f"value for {metric_handle!r}"),
                f"value for {metric_handle!r}",
            )
            report_part = self._send(lambda: client.set_numeric_value(operation_handle, wire_value), timeout)
        else:
            string_value = str(value)
            if metric.operation_allowed_values and string_value not in metric.operation_allowed_values:
                return self._not_sent("%r is not allowed by set operation %s", string_value, operation_handle)
            report_part = self._send(lambda: client.set_string(operation_handle, string_value), timeout)

        info = report_part.InvocationInfo
        self.last_invocation_detail = _failure_reason(info)
        logger.info(
            "set %s via %s -> %s%s",
            metric_handle,
            operation_handle,
            info.InvocationState,
            f" ({self.last_invocation_detail})" if self.last_invocation_detail else "",
        )
        if info.InvocationState in (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD):
            self._reconcile_metric(metric_handle, value)
        return info.InvocationState

    def _not_sent(self, message: str, *args: object) -> msg_types.InvocationState:
        """FAILED for a request that never left this consumer, with the reason kept for the caller."""
        logger.warning(message, *args)
        self.last_invocation_detail = message % args
        return msg_types.InvocationState.FAILED

    def _reconcile_metric(self, handle: str, requested: Decimal | str) -> None:
        """Read the actual value when a peer finishes a set without a metric notification.

        OperationInvokedReport describes the invocation, not the resulting metric state.
        A targeted GetMdState also covers peers whose metric subscription is missing or
        whose metric report is delayed. Never overwrite a newer notification with an older
        state version, and do not turn a successful invocation into a failure if Get fails.
        """
        get_client = self._consumer.client("Get")
        if get_client is None:
            logger.warning("cannot read back metric %s: peer has no GetService", handle)
            return
        try:
            response = get_client.get_md_state([handle])
            group = response.mdib_version_group
            updated = {}
            found = False
            with self._mdib.mdib_lock:
                if (
                    group.sequence_id != self._mdib.sequence_id
                    or group.instance_id != self._mdib.instance_id
                ):
                    logger.debug("ignoring stale GetMdState response for %s", handle)
                    return
                for state in response.result.MdState.State:
                    if state.DescriptorHandle != handle:
                        continue
                    current = self._mdib.states.descriptor_handle.get_one(handle, allow_none=True)
                    if current is None or state.StateVersion < current.StateVersion:
                        continue
                    found = True
                    previous = getattr(getattr(current, "MetricValue", None), "Value", None)
                    current.update_from_other_container(state)
                    self._mdib.states.update_object(current)
                    actual = getattr(getattr(current, "MetricValue", None), "Value", None)
                    if previous != actual:
                        updated[handle] = current
                if updated:
                    self._mdib.metrics_by_handle = updated
            if not found:
                logger.warning("GetMdState returned no usable metric state for %s", handle)
            else:
                actual = self.metrics((handle,)).get(handle)
                if actual is None:
                    logger.warning("GetMdState returned no usable metric state for %s", handle)
                elif actual.value != requested:
                    logger.warning("set %s finished: requested %r, device reports %r", handle, requested, actual.value)
                else:
                    logger.info("read back %s from GetMdState: %r", handle, actual.value)
        except Exception:  # noqa: BLE001 - readback cannot change a completed invocation's result
            logger.exception("could not read back metric %s after set", handle)

    def actions(self) -> dict[str, RemoteAction]:
        """The things the peer says it can be told to do.

        ActivateOperations, as opposed to the set operations behind metrics/. A device with
        no metric for "home the axes" may still be able to do it, and this is where that
        shows up.
        """
        with self._lock, self._mdib.mdib_lock:
            found: dict[str, RemoteAction] = {}
            for entity in live_entities(self._mdib):
                handle = entity.handle
                if getattr(entity, "node_type", None) != pm.ActivateOperationDescriptor:
                    continue
                descriptor = getattr(entity, "descriptor", None)
                state = getattr(entity, "state", None)
                arguments = _sequence(getattr(descriptor, "Argument", None))
                found[handle] = RemoteAction(
                    handle=handle,
                    label=_first_text(getattr(descriptor, "Type", None)),
                    type_code=getattr(getattr(descriptor, "Type", None), "Code", None),
                    target_handle=getattr(descriptor, "OperationTarget", None),
                    # Same rule as a metric editor: offer it only when the device says it
                    # is enabled, and follow operation_by_handle for changes.
                    enabled=_operation_is_enabled(state) and not arguments,
                    argument_count=len(arguments),
                )
            return found

    def run_action(self, action_handle: str, timeout: float = 10.0) -> msg_types.InvocationState:
        """Tell the peer to do something, and wait for the final InvocationState.

        Returns FAILED rather than raising when the action is unknown or disabled, so a caller
        has one failure mode to handle instead of two.
        """
        self.last_invocation_detail = ""
        action = self.actions().get(action_handle)
        if action is None:
            return self._not_sent("no action %s on this device", action_handle)
        if not action.enabled:
            return self._not_sent("action %s is disabled", action_handle)

        client = self._consumer.set_service_client
        report_part = self._send(lambda: client.activate(action_handle, arguments=None), timeout)
        info = report_part.InvocationInfo
        self.last_invocation_detail = _failure_reason(info)
        logger.info(
            "ran %s -> %s%s",
            action_handle,
            info.InvocationState,
            f" ({self.last_invocation_detail})" if self.last_invocation_detail else "",
        )
        return info.InvocationState

    # -- notifications -------------------------------------------------------------

    def bind(self, **callbacks: Callable[[Any], None]) -> None:
        """Subscribe to MDIB observables.

        Useful observable names:

        ``metrics_by_handle``
            values changed
        ``new_descriptors_by_handle``, ``updated_descriptors_by_handle``,
        ``deleted_descriptors_by_handle``
            the peer's description changed at runtime
        ``operation_by_handle``
            an operation's OperatingMode changed, so a control became usable or unusable
        ``description_modifications``
            the full report, as a fallback
        ``sequence_id``, ``instance_id``
            the peer restarted and the cached MDIB is worthless

        Example::

            device.bind(
                metrics_by_handle=on_values,
                new_descriptors_by_handle=on_new_metric,
                operation_by_handle=on_operation_state,
            )

        Callbacks run on sdc11073 threads. Never touch Qt widgets from them. The device keeps
        every callback alive until it is closed and released, because the library alone would
        drop a lambda or a local function immediately and silently.
        """
        self._bound_callbacks.extend(callbacks.values())
        observableproperties.bind(self._mdib, **callbacks)

    def close(self) -> None:
        """Unsubscribe and disconnect.

        A lost peer is not sent Unsubscribe requests: they could only fail or wait for a
        timeout, and would leave a traceback per subscription in the log.
        """
        self._closing = True
        try:
            self._consumer.stop_all(unsubscribe=self._lost_reason is None)
        except Exception:  # noqa: BLE001 - teardown must not mask the original problem
            logger.exception("error while closing consumer for %s", self.epr)


class SharedDiscovery:
    """One WS-Discovery for every ConsumerService on an interface.

    Each connection tab used to run its own: every tab parsed all discovery traffic, and a new
    tab listed nothing until every provider had answered its own probe. The first consumer to
    start starts it, and the last one to stop stops it.
    """

    def __init__(self, ip: str = constants.DEFAULT_IP) -> None:
        self.ip = ip
        # Guards the consumers and both maps. The maps are written from sdc11073's networking
        # thread and read from whichever thread asks for known_devices().
        self.lock = threading.Lock()
        self._consumers: list[ConsumerService] = []
        self._discovery: RangeWSDiscovery | None = None
        # When each provider was last heard from, by EPR.
        self.last_seen: dict[str, float] = {}
        # The newest announcement of each provider, by EPR. WSDiscovery keeps its first entry
        # for an EPR that repeats its metadata version, so a provider that restarted on another
        # port without a Bye would stay listed at the address it left.
        self.announced: dict[str, Any] = {}

    def attach(self, consumer: ConsumerService) -> RangeWSDiscovery:
        with self.lock:
            if self._discovery is None:
                discovery = RangeWSDiscovery(self.ip)
                try:
                    discovery.start()
                except Exception:
                    try:
                        discovery.stop()
                    except Exception:
                        logger.exception("error while rolling back consumer discovery startup")
                    raise
                # Passive discovery: providers announce themselves with Hello on start and Bye on
                # a clean shutdown. Probe and resolve answers count as "heard from" as well.
                types = SdcV1Definitions.MedicalDeviceTypesFilter
                discovery.set_remote_service_hello_callback(lambda _addr, service: self._heard(service), types=types)
                discovery.set_on_probe_matches_callback(lambda services: self._heard(*services))
                discovery.set_remote_service_resolve_match_callback(lambda service: self._heard(service))
                discovery.set_remote_service_bye_callback(lambda _addr, epr: self._gone(epr))
                self._discovery = discovery
                logger.info("discovery up on %s", self.ip)
            self._consumers.append(consumer)
            return self._discovery

    def detach(self, consumer: ConsumerService) -> None:
        with self.lock:
            if consumer in self._consumers:
                self._consumers.remove(consumer)
            if self._consumers or self._discovery is None:
                return
            discovery, self._discovery = self._discovery, None
            self.last_seen.clear()
            self.announced.clear()
        # Outside the lock: stopping joins the networking thread, which may be waiting for it.
        discovery.stop()

    def _heard(self, *services: Any) -> None:
        now = time.monotonic()
        with self.lock:
            for service in services:
                if service.epr:
                    self.last_seen[service.epr] = now
                    if service.x_addrs:
                        self.announced[service.epr] = service
            consumers = list(self._consumers)
        for consumer in consumers:
            consumer._notify_listener()  # noqa: SLF001

    def _gone(self, epr: str) -> None:
        with self.lock:
            self.last_seen.pop(epr, None)
            self.announced.pop(epr, None)
            consumers = list(self._consumers)
        for consumer in consumers:
            consumer._notify_departure(epr)  # noqa: SLF001
            consumer._notify_listener()  # noqa: SLF001


class ConsumerService:
    """Discovery plus connection management for the consumer side."""

    def __init__(
        self,
        ip: str = constants.DEFAULT_IP,
        *,
        own_epr: str | None = None,
        tls_config: TlsConfig | None = None,
        discovery: SharedDiscovery | None = None,
    ) -> None:
        self.ip = ip
        self._own_epr = own_epr
        self.tls_config = tls_config
        self._tls_contexts = None
        self._shared = discovery if discovery is not None else SharedDiscovery(ip)
        self._discovery: RangeWSDiscovery | None = None
        self._discovery_listener: Callable[[], None] | None = None
        self._departure_listener: Callable[[str], None] | None = None

    def start(self) -> None:
        """Start WS-Discovery on the configured interface, or join the one already running."""
        if self._discovery is not None:
            msg = "consumer service is already started"
            raise RuntimeError(msg)
        # The consumer hosts the HTTPS endpoint for provider event callbacks, so validate
        # its full mTLS identity before discovery starts as well.
        self._tls_contexts = self.tls_config.create_contexts() if self.tls_config is not None else None
        try:
            self._discovery = self._shared.attach(self)
        except Exception:
            self._tls_contexts = None
            raise

    def stop(self) -> None:
        """Leave WS-Discovery; the last consumer on the interface stops it."""
        discovery = self._discovery
        self._discovery = None
        self._tls_contexts = None
        if discovery is not None:
            self._shared.detach(self)

    def set_discovery_listener(self, listener: Callable[[], None] | None) -> None:
        """Call `listener` whenever the set of known providers may have changed.

        It runs on sdc11073's networking thread and takes no arguments; read the new state
        with known_devices(). Never touch Qt widgets from it.
        """
        self._discovery_listener = listener

    def set_departure_listener(self, listener: Callable[[str], None] | None) -> None:
        """Call `listener(epr)` when a provider announces it is leaving (WS-Discovery Bye).

        Runs on sdc11073's networking thread. Never touch Qt widgets from it.
        """
        self._departure_listener = listener

    def _notify_departure(self, epr: str) -> None:
        departure = self._departure_listener
        if departure is not None:
            try:
                departure(epr)
            except Exception:  # noqa: BLE001 - a listener bug must not kill the networking thread
                logger.exception("departure listener failed")

    def _notify_listener(self) -> None:
        listener = self._discovery_listener
        if listener is None:
            return
        try:
            listener()
        except Exception:  # noqa: BLE001 - a listener bug must not kill the networking thread
            logger.exception("discovery listener failed")

    def probe(self) -> None:
        """Send one multicast probe without waiting. Answers arrive through the listener."""
        if self._discovery is None:
            msg = "consumer service is not started"
            raise RuntimeError(msg)
        self._discovery.search_services(types=SdcV1Definitions.MedicalDeviceTypesFilter, timeout=PROBE_SEND_SECONDS)

    def known_devices(self, max_age: float | None = STALE_AFTER_SECONDS) -> list[DiscoveredDevice]:
        """Providers discovery currently knows of, without sending anything.

        With `max_age`, a provider not heard from for that many seconds is left out; that is
        how one that vanished without a Bye eventually disappears.
        """
        if self._discovery is None:
            return []
        devices = self._devices_from(
            self._discovery.search_services(types=SdcV1Definitions.MedicalDeviceTypesFilter, timeout=0),
        )
        if max_age is None:
            return devices
        return self._heard_since(devices, time.monotonic() - max_age)

    def _heard_since(self, devices: list[DiscoveredDevice], cutoff: float) -> list[DiscoveredDevice]:
        with self._shared.lock:
            last_seen = self._shared.last_seen
            return [device for device in devices if last_seen.get(device.epr, cutoff - 1) >= cutoff]

    def _devices_from(self, services: Iterable[Any]) -> list[DiscoveredDevice]:
        with self._shared.lock:
            announced = dict(self._shared.announced)
        devices = []
        for cached in services:
            if self._own_epr is not None and cached.epr == self._own_epr:
                continue
            # The cache and the newest announcement differ only after a provider moved.
            latest = announced.get(cached.epr)
            service = latest if latest is not None and list(latest.x_addrs) != list(cached.x_addrs) else cached
            devices.append(
                DiscoveredDevice(
                    epr=service.epr,
                    x_addrs=tuple(getattr(service, "x_addrs", ()) or ()),
                    scopes=_scope_uris(service),
                    service=service,
                ),
            )
        return devices

    def __enter__(self) -> ConsumerService:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def scan(
        self,
        timeout: float = 10.0,
        expected: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> list[DiscoveredDevice]:
        """Search for SDC providers until `expected` are found or `timeout` expires."""
        if self._discovery is None:
            msg = "consumer service is not started"
            raise RuntimeError(msg)

        # search_services() blocks for its whole timeout before returning anything. Instead,
        # send probes with a near-zero timeout and read the cache that ProbeMatches fill,
        # so a scan ends as soon as enough answers are in and a cancel takes effect at once.
        # Only providers heard during this scan count, so those that left without a Bye drop
        # off; clearing the cache instead would empty every other tab sharing it.
        types = SdcV1Definitions.MedicalDeviceTypesFilter
        start = time.monotonic()
        deadline = start + timeout
        next_probe = start
        found: list[DiscoveredDevice] = []
        while not (cancel_event is not None and cancel_event.is_set()):
            now = time.monotonic()
            if now >= next_probe:
                self._discovery.search_services(types=types, timeout=PROBE_SEND_SECONDS)
                next_probe = now + PROBE_REPEAT_SECONDS
            found = self._heard_since(self._devices_from(self._discovery.search_services(types=types, timeout=0)), start)
            if len(found) >= expected or now >= deadline:
                break
            if cancel_event is None:
                time.sleep(SCAN_POLL_SECONDS)
            elif cancel_event.wait(SCAN_POLL_SECONDS):
                break

        logger.info("discovered %d provider(s)", len(found))
        return found

    def connect(self, device: DiscoveredDevice) -> RemoteDevice:
        """Connect to a discovered provider and load its MDIB."""
        try:
            try:
                return self._connect_once(device)
            except ValidationError as exc:
                if _EMPTY_SCHEMA_MESSAGE not in _error_text(exc):
                    raise
                logger.warning("a consumer for %s was built without usable schemas, building another", device.epr)
                return self._connect_once(device)
        except ValidationError as exc:
            # sdc11073's ValidationError carries no text; the reason is on its lxml cause.
            msg = f"a message was rejected by the XML schema validation ({_error_text(exc)})"
            raise ConsumerError(msg) from exc

    def _connect_once(self, device: DiscoveredDevice) -> RemoteDevice:
        if self.tls_config is not None:
            provider_address = next((address for address in device.x_addrs if address.startswith("https://")), None)
            if provider_address is None:
                msg = "TLS is required, but the discovered provider does not advertise an HTTPS endpoint"
                raise RuntimeError(msg)
        else:
            provider_address = None
        if self.tls_config is None:
            # Keep validation and construction on the same discovery snapshot.
            service = copy(device.service)
            service.x_addrs = list(service.x_addrs)
            # The discovery factory uses the first advertised address.
            if service.x_addrs and service.x_addrs[0].startswith("https://"):
                msg = (
                    "This provider requires HTTPS, but the Toolbox is configured for HTTP. "
                    "Enable TLS and configure certificates in the startup settings."
                )
                raise ConsumerError(msg)
            with _CONSUMER_BUILD_LOCK:
                consumer = _WsdlSdcConsumer.from_wsd_service(service, ssl_context_container=None)
        else:
            # The factory does not expose force_ssl_connect in sdc11073 3.0.0. Constructing
            # the same SDC-v1 consumer directly keeps a failed TLS handshake from retrying HTTP.
            components = default_components_factory()
            components.soap_client_class = partial(VerifiedSoapClient, tls_config=self.tls_config)
            with _CONSUMER_BUILD_LOCK:
                consumer = _WsdlSdcConsumer(
                    provider_address,
                    SdcV1Definitions,
                    self._tls_contexts,
                    force_ssl_connect=True,
                    alternative_hostname=self.tls_config.server_name,
                    components=components,
                )
        try:
            consumer.start_all()
            _ensure_report_subscriptions(consumer)
            peer_certificate = (
                self.tls_config.verify_peer_certificate(
                    consumer.binary_peer_certificate,
                    endpoint_host(provider_address),
                )
                if self.tls_config is not None
                else None
            )
            mdib = _TolerantConsumerMdib(consumer, extras_cls=_PeriodicConsumerMdibMethods)
            mdib.init_mdib()
        except Exception as exc:
            try:
                consumer.stop_all()
            except Exception:
                logger.exception("error while rolling back consumer connection to %s", device.epr)
            cause = exc.__cause__ or exc
            if isinstance(exc, HTTPReturnCodeError):
                msg = f"the device answered with an error: {_http_error_text(exc)}"
                raise ConsumerError(msg) from exc
            if self.tls_config is None and isinstance(exc, HTTPException) and isinstance(cause, BadStatusLine):
                # A complete TLS alert identifies the protocol; a connection reset does not.
                line = cause.line
                if (
                    len(line) == 7
                    and line[:2] == "\x15\x03"
                    and line[2] in "\x01\x02\x03"
                    and line[3:5] == "\x00\x02"
                    and line[5] in "\x01\x02"
                ):
                    msg = (
                        "This provider returned a TLS alert to an HTTP request. "
                        "Check the provider's advertised HTTP/HTTPS address. "
                        "Enable TLS and configure certificates in the startup settings."
                    )
                    raise ConsumerError(msg) from exc
            raise
        logger.info("connected to %s, %d entities", device.epr, len(mdib.entities))
        return RemoteDevice(consumer, mdib, device.epr, peer_certificate)
