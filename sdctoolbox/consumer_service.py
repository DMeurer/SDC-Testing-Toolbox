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
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import partial
from http.client import HTTPException
from typing import TYPE_CHECKING, Any

from sdc11073 import observableproperties
from sdc11073.consumer.consumerimpl import SdcConsumer
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.mdib import ConsumerMdib
from sdc11073.mdib.consumermdibxtra import ConsumerMdibMethods
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types import eventing_types, msg_types, pm_types
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types.dpws_types import DeviceEventingFilterDialectURI

from . import constants
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
from .security import CertificateInfo, TlsConfig, endpoint_host

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

logger = logging.getLogger("sdctoolbox.consumer")

# Discovery pacing. Providers answer a probe within about half a second; re-probing covers
# lost multicast datagrams without flooding the segment.
SCAN_POLL_SECONDS = 0.2
PROBE_REPEAT_SECONDS = 1.0
PROBE_SEND_SECONDS = 0.01
# Continuous discovery: a background probe this often refreshes every provider that is
# still there, and one not heard from for STALE_AFTER_SECONDS (crashed, no Bye) drops off.
BACKGROUND_PROBE_SECONDS = 30.0
STALE_AFTER_SECONDS = 2.5 * BACKGROUND_PROBE_SECONDS

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


def _error_text(exc: BaseException) -> str:
    """Name the underlying socket error; sdc11073 wraps it in a message-less NotConnected."""
    while not str(exc) and (exc.__cause__ or exc.__context__) is not None:
        exc = exc.__cause__ or exc.__context__
    text = str(exc.args[1]) if isinstance(exc, OSError) and len(exc.args) > 1 else str(exc)
    return f"{exc.__class__.__name__}: {text}" if text else exc.__class__.__name__


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


def _subscription_summary(consumer: SdcConsumer) -> str:
    """One readable line per subscription: its state and the short action names it covers."""
    parts = []
    for subscription_filter, active in consumer.subscription_status.items():
        names = ", ".join(action.rsplit("/", 1)[-1] for action in subscription_filter.split())
        parts.append(f"{'active' if active else 'inactive'}: {names}")
    return "; ".join(parts) or "no subscriptions"


def _ensure_operation_report_subscription(consumer: SdcConsumer) -> None:
    """Make sure OperationInvokedReports are subscribed, with a dedicated subscription if needed.

    start_all() subscribes once per hosted service with every action that service offers.
    When the provider groups SetService with other services, one action it dislikes (often
    a periodic report) gets the whole combined subscription rejected, and sdc11073 only
    logs that. Subscribing to OperationInvokedReport alone usually succeeds.
    """
    if _operation_reports_subscribed(consumer):
        return
    client = consumer.set_service_client
    hosted = next(
        (
            candidate
            for candidate in consumer.host_description.relationship.Hosted
            if any(port_type.localname == "SetService" for port_type in candidate.Types or ())
        ),
        None,
    )
    logger.warning(
        "no active OperationInvokedReport subscription after connecting (%s); "
        "retrying with a dedicated subscription",
        _subscription_summary(consumer),
    )
    if hosted is None:
        logger.warning("provider metadata names no hosted SetService to subscribe at")
        return
    keys = list(client.get_available_subscriptions())
    filter_type = eventing_types.FilterType()
    filter_type.text = " ".join(_action_uri(key.action) for key in keys)
    filter_type.Dialect = DeviceEventingFilterDialectURI.ACTION
    try:
        consumer.do_subscribe(hosted, filter_type, keys)
    except Exception:
        logger.exception("dedicated OperationInvokedReport subscription failed")
        return
    if _operation_reports_subscribed(consumer):
        logger.info("dedicated OperationInvokedReport subscription is active")


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
        self._lost_reason: str | None = None
        self._lost_listeners: list[Callable[[str], None]] = []
        status = getattr(consumer, "subscription_status", None)
        self._had_active_subscription = bool(status) and any(status.values())
        if status is not None:
            # A clean shutdown ends every subscription (SubscriptionEnd); a crashed or
            # unreachable peer shows up when the renewals fail.
            observableproperties.bind(consumer, subscription_status=self._on_subscription_status)

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
        if any(status.values()):
            self._had_active_subscription = True
        elif self._had_active_subscription:
            self.mark_connection_lost(
                "the device ended all subscriptions (it shut down, restarted or became unreachable)",
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
        except (OSError, HTTPException) as exc:
            reason = f"the device could not be reached ({_error_text(exc)})"
            self.mark_connection_lost(reason)
            raise PeerConnectionLost(f"connection lost: {reason}") from exc
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            if self._lost_reason is not None:
                raise PeerConnectionLost(f"connection lost: {self._lost_reason}") from exc
            msg = f"the device accepted the request but sent no final result within {timeout:g} s"
            raise ConsumerError(msg) from exc

    def operation_reports_subscribed(self) -> bool:
        """Whether an active subscription delivers this peer's OperationInvokedReports.

        Providers may refuse set and activate requests from a consumer without one
        (SDPi requires it), and sdc11073 only logs a failed or ended subscription.
        """
        return _operation_reports_subscribed(self._consumer)

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

    def metrics(self, handles: Iterable[str] | None = None) -> dict[str, RemoteMetric]:
        """Build defensive snapshots of the requested metrics on the peer.

        Unknown node types are skipped rather than guessed at; callers that want to show
        them can walk ``mdib.entities`` themselves. With no handles, snapshot every metric.
        """
        wanted = None if handles is None else frozenset(handles)
        if wanted == frozenset():
            return {}
        with self._lock:
            operations_by_target = self._operation_index(wanted)
            result: dict[str, RemoteMetric] = {}

            for handle, entity in self._mdib.entities.items():
                if wanted is not None and handle not in wanted:
                    continue
                node_type = getattr(entity, "node_type", None)
                kind = METRIC_NODE_TYPES.get(node_type)
                if kind is None:
                    continue

                descriptor = getattr(entity, "descriptor", None)
                state = getattr(entity, "state", None)

                metric_allowed_values = _string_values(getattr(descriptor, "AllowedValue", None), "Value")
                metric_value = getattr(state, "MetricValue", None)

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
                    unit_label=_first_text(getattr(descriptor, "Unit", None)),
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

        for handle, entity in self._mdib.entities.items():
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
        with self._lock:
            signals_by_condition: dict[str, dict[str, str]] = {}
            for handle, entity in self._mdib.entities.items():
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
            for handle, entity in self._mdib.entities.items():
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

        metrics = self.metrics((metric_handle,))
        metric = metrics.get(metric_handle)
        if metric is None or not metric.operation_handles:
            logger.warning("no set operation targets %s", metric_handle)
            return msg_types.InvocationState.FAILED

        operation_handle = metric.selected_operation_handle
        if operation_handle is None:
            logger.warning("no enabled set operation targets %s", metric_handle)
            return msg_types.InvocationState.FAILED
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
                logger.warning(
                    "%r is not allowed by set operation %s",
                    string_value,
                    operation_handle,
                )
                return msg_types.InvocationState.FAILED
            report_part = self._send(lambda: client.set_string(operation_handle, string_value), timeout)

        info = report_part.InvocationInfo
        logger.info(
            "set %s via %s -> %s%s",
            metric_handle,
            operation_handle,
            info.InvocationState,
            f" ({info.InvocationErrorMessage})" if info.InvocationErrorMessage else "",
        )
        return info.InvocationState

    def actions(self) -> dict[str, RemoteAction]:
        """The things the peer says it can be told to do.

        ActivateOperations, as opposed to the set operations behind metrics/. A device with
        no metric for "home the axes" may still be able to do it, and this is where that
        shows up.
        """
        with self._lock:
            found: dict[str, RemoteAction] = {}
            for handle, entity in self._mdib.entities.items():
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
        action = self.actions().get(action_handle)
        if action is None:
            logger.warning("no action %s on this device", action_handle)
            return msg_types.InvocationState.FAILED
        if not action.enabled:
            logger.warning("action %s is disabled", action_handle)
            return msg_types.InvocationState.FAILED

        client = self._consumer.set_service_client
        report_part = self._send(lambda: client.activate(action_handle, arguments=None), timeout)
        info = report_part.InvocationInfo
        logger.info(
            "ran %s -> %s%s",
            action_handle,
            info.InvocationState,
            f" ({info.InvocationErrorMessage})" if info.InvocationErrorMessage else "",
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

        Callbacks run on sdc11073 threads. Never touch Qt widgets from them.
        """
        observableproperties.bind(self._mdib, **callbacks)

    def close(self) -> None:
        """Unsubscribe and disconnect.

        A lost peer is not sent Unsubscribe requests: they could only fail or wait for a
        timeout, and would leave a traceback per subscription in the log.
        """
        try:
            self._consumer.stop_all(unsubscribe=self._lost_reason is None)
        except Exception:  # noqa: BLE001 - teardown must not mask the original problem
            logger.exception("error while closing consumer for %s", self.epr)


class ConsumerService:
    """Discovery plus connection management for the consumer side."""

    def __init__(
        self,
        ip: str = constants.DEFAULT_IP,
        *,
        own_epr: str | None = None,
        tls_config: TlsConfig | None = None,
    ) -> None:
        self.ip = ip
        self._own_epr = own_epr
        self.tls_config = tls_config
        self._tls_contexts = None
        self._discovery: WSDiscovery | None = None
        # When each provider was last heard from, by EPR. Written from sdc11073's
        # networking thread, read from whichever thread asks for known_devices().
        self._last_seen: dict[str, float] = {}
        self._last_seen_lock = threading.Lock()
        self._discovery_listener: Callable[[], None] | None = None
        self._departure_listener: Callable[[str], None] | None = None

    def start(self) -> None:
        """Start WS-Discovery on the configured interface."""
        if self._discovery is not None:
            msg = "consumer service is already started"
            raise RuntimeError(msg)
        # The consumer hosts the HTTPS endpoint for provider event callbacks, so validate
        # its full mTLS identity before discovery starts as well.
        self._tls_contexts = self.tls_config.create_contexts() if self.tls_config is not None else None
        self._discovery = WSDiscovery(self.ip)
        try:
            self._discovery.start()
        except Exception:
            try:
                self.stop()
            except Exception:
                logger.exception("error while rolling back consumer discovery startup")
            raise
        # Passive discovery: providers announce themselves with Hello on start and Bye on
        # a clean shutdown. Probe and resolve answers count as "heard from" as well.
        types = SdcV1Definitions.MedicalDeviceTypesFilter
        self._discovery.set_remote_service_hello_callback(lambda _addr, service: self._heard(service.epr), types=types)
        self._discovery.set_on_probe_matches_callback(lambda services: self._heard(*(s.epr for s in services)))
        self._discovery.set_remote_service_resolve_match_callback(lambda service: self._heard(service.epr))
        self._discovery.set_remote_service_bye_callback(lambda _addr, epr: self._gone(epr))
        logger.info("discovery up on %s", self.ip)

    def stop(self) -> None:
        """Stop WS-Discovery."""
        discovery = self._discovery
        self._discovery = None
        self._tls_contexts = None
        with self._last_seen_lock:
            self._last_seen.clear()
        if discovery is not None:
            discovery.stop()

    def set_discovery_listener(self, listener: Callable[[], None] | None) -> None:
        """Call `listener` whenever the set of known providers may have changed.

        It runs on sdc11073's networking thread and takes no arguments; read the new state
        with known_devices(). Never touch Qt widgets from it.
        """
        self._discovery_listener = listener

    def _heard(self, *eprs: str) -> None:
        now = time.monotonic()
        with self._last_seen_lock:
            for epr in eprs:
                if epr:
                    self._last_seen[epr] = now
        self._notify_listener()

    def set_departure_listener(self, listener: Callable[[str], None] | None) -> None:
        """Call `listener(epr)` when a provider announces it is leaving (WS-Discovery Bye).

        Runs on sdc11073's networking thread. Never touch Qt widgets from it.
        """
        self._departure_listener = listener

    def _gone(self, epr: str) -> None:
        with self._last_seen_lock:
            self._last_seen.pop(epr, None)
        departure = self._departure_listener
        if departure is not None:
            try:
                departure(epr)
            except Exception:  # noqa: BLE001 - a listener bug must not kill the networking thread
                logger.exception("departure listener failed")
        self._notify_listener()

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
        cutoff = time.monotonic() - max_age
        with self._last_seen_lock:
            return [device for device in devices if self._last_seen.get(device.epr, cutoff - 1) >= cutoff]

    def _devices_from(self, services: Iterable[Any]) -> list[DiscoveredDevice]:
        return [
            DiscoveredDevice(
                epr=service.epr,
                x_addrs=tuple(getattr(service, "x_addrs", ()) or ()),
                scopes=_scope_uris(service),
                service=service,
            )
            for service in services
            if self._own_epr is None or service.epr != self._own_epr
        ]

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
        # Forget earlier results first, so providers that left without a Bye drop off.
        types = SdcV1Definitions.MedicalDeviceTypesFilter
        self._discovery.clear_remote_services()
        start = time.monotonic()
        deadline = start + timeout
        next_probe = start
        found: list[DiscoveredDevice] = []
        while not (cancel_event is not None and cancel_event.is_set()):
            now = time.monotonic()
            if now >= next_probe:
                self._discovery.search_services(types=types, timeout=PROBE_SEND_SECONDS)
                next_probe = now + PROBE_REPEAT_SECONDS
            found = self._devices_from(self._discovery.search_services(types=types, timeout=0))
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
        if self.tls_config is not None:
            provider_address = next((address for address in device.x_addrs if address.startswith("https://")), None)
            if provider_address is None:
                msg = "TLS is required, but the discovered provider does not advertise an HTTPS endpoint"
                raise RuntimeError(msg)
        else:
            provider_address = None
        if self.tls_config is None:
            consumer = SdcConsumer.from_wsd_service(device.service, ssl_context_container=None)
        else:
            # The factory does not expose force_ssl_connect in sdc11073 3.0.0. Constructing
            # the same SDC-v1 consumer directly keeps a failed TLS handshake from retrying HTTP.
            consumer = SdcConsumer(
                provider_address,
                SdcV1Definitions,
                self._tls_contexts,
                force_ssl_connect=True,
                alternative_hostname=self.tls_config.server_name,
            )
        try:
            consumer.start_all()
            _ensure_operation_report_subscription(consumer)
            peer_certificate = (
                self.tls_config.verify_peer_certificate(
                    consumer.binary_peer_certificate,
                    endpoint_host(provider_address),
                )
                if self.tls_config is not None
                else None
            )
            mdib = ConsumerMdib(consumer, extras_cls=_PeriodicConsumerMdibMethods)
            mdib.init_mdib()
        except Exception:
            try:
                consumer.stop_all()
            except Exception:
                logger.exception("error while rolling back consumer connection to %s", device.epr)
            raise
        logger.info("connected to %s, %d entities", device.epr, len(mdib.entities))
        return RemoteDevice(consumer, mdib, device.epr, peer_certificate)
