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
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sdc11073 import observableproperties
from sdc11073.consumer.consumerimpl import SdcConsumer
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.mdib import ConsumerMdib
from sdc11073.mdib.consumermdibxtra import ConsumerMdibMethods
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types import msg_types, pm_types
from sdc11073.xml_types import pm_qnames as pm

from . import constants
from .model import (
    MetricKind,
    PatientInfo,
    RemoteAction,
    RemoteAlert,
    RemoteMetric,
    fixed_point_decimal,
    patient_info_from_biceps,
    validate_decimal,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("sdctoolbox.consumer")

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
    """Whether an operation is enabled, including BICEPS' default for an absent mode."""
    mode = getattr(state, "OperatingMode", None)
    return mode is None or _enum_value(mode) == _enum_value(pm_types.OperatingMode.ENABLED)


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


def _first_range(ranges: Any) -> tuple[Any, Any]:
    """Lower and upper of the first Range in a list, tolerating an absent or empty list."""
    for item in ranges or []:
        return getattr(item, "Lower", None), getattr(item, "Upper", None)
    return None, None


class _PeriodicConsumerMdibMethods(ConsumerMdibMethods):
    """Apply periodic reports because sdc11073's stock MDIB helper only binds episodic ones."""

    def bind_to_client_observables(self) -> None:
        super().bind_to_client_observables()
        observableproperties.bind(
            self._sdc_client,
            periodic_metric_report=self._on_periodic_metric_report,
            periodic_alert_report=self._on_periodic_alert_report,
            periodic_component_report=self._on_periodic_component_report,
            periodic_operational_state_report=self._on_periodic_operational_state_report,
            periodic_context_report=self._on_periodic_context_report,
        )

    def _on_periodic_metric_report(self, received_message_data: Any) -> None:
        report = self._mdib.data_model.msg_types.PeriodicMetricReport.from_node(received_message_data.p_msg.msg_node)
        self._mdib.process_incoming_metric_states_report(received_message_data.mdib_version_group, report)

    def _on_periodic_alert_report(self, received_message_data: Any) -> None:
        report = self._mdib.data_model.msg_types.PeriodicAlertReport.from_node(received_message_data.p_msg.msg_node)
        self._mdib.process_incoming_alert_states_report(received_message_data.mdib_version_group, report)

    def _on_periodic_component_report(self, received_message_data: Any) -> None:
        report = self._mdib.data_model.msg_types.PeriodicComponentReport.from_node(received_message_data.p_msg.msg_node)
        self._mdib.process_incoming_component_states_report(received_message_data.mdib_version_group, report)

    def _on_periodic_operational_state_report(self, received_message_data: Any) -> None:
        report = self._mdib.data_model.msg_types.PeriodicOperationalStateReport.from_node(received_message_data.p_msg.msg_node)
        self._mdib.process_incoming_operational_states_report(received_message_data.mdib_version_group, report)

    def _on_periodic_context_report(self, received_message_data: Any) -> None:
        report = self._mdib.data_model.msg_types.PeriodicContextReport.from_node(received_message_data.p_msg.msg_node)
        self._mdib.process_incoming_context_states_report(received_message_data.mdib_version_group, report)


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
    allowed_range: tuple[Any, Any] | None


class RemoteDevice:
    """A connected peer. Wraps SdcConsumer plus its ConsumerMdib."""

    def __init__(self, consumer: SdcConsumer, mdib: ConsumerMdib, epr: str) -> None:
        self._consumer = consumer
        self._mdib = mdib
        self.epr = epr
        self._lock = threading.RLock()

    # -- reading -------------------------------------------------------------------

    @property
    def mdib(self) -> ConsumerMdib:
        """The consumer-side MDIB."""
        return self._mdib

    def metrics(self) -> dict[str, RemoteMetric]:
        """Build a defensive snapshot of every metric on the peer.

        Unknown node types are skipped rather than guessed at; callers that want to show
        them can walk ``mdib.entities`` themselves.
        """
        with self._lock:
            operations_by_target = self._operation_index()
            result: dict[str, RemoteMetric] = {}

            for handle, entity in self._mdib.entities.items():
                node_type = getattr(entity, "node_type", None)
                kind = METRIC_NODE_TYPES.get(node_type)
                if kind is None:
                    continue

                descriptor = getattr(entity, "descriptor", None)
                state = getattr(entity, "state", None)

                allowed = getattr(descriptor, "AllowedValue", None) or []
                metric_value = getattr(state, "MetricValue", None)

                technical_lower, technical_upper = _first_range(getattr(descriptor, "TechnicalRange", None))
                operation_type = SET_OPERATION_BY_METRIC_KIND.get(kind)
                operations = [
                    operation
                    for operation in operations_by_target.get(handle, ())
                    if operation.node_type == operation_type
                ]
                selected_operation = next((operation for operation in operations if operation.enabled), None)
                # What we may ask for comes from the selected operation when it says so,
                # otherwise we fall back on what the device says it can produce.
                lower, upper = (
                    selected_operation.allowed_range
                    if selected_operation is not None and selected_operation.allowed_range is not None
                    else (technical_lower, technical_upper)
                )

                result[handle] = RemoteMetric(
                    handle=handle,
                    node_type_name=getattr(node_type, "localname", str(node_type)),
                    kind=kind,
                    label=_first_text(getattr(descriptor, "Type", None)),
                    unit_label=_first_text(getattr(descriptor, "Unit", None)),
                    type_code=getattr(getattr(descriptor, "Type", None), "Code", None),
                    allowed_values=tuple(str(item.Value) for item in allowed),
                    resolution=getattr(descriptor, "Resolution", None),
                    minimum=lower,
                    maximum=upper,
                    technical_minimum=technical_lower,
                    technical_maximum=technical_upper,
                    value=getattr(metric_value, "Value", None),
                    # A sample array carries Samples instead of Value, so a peer's waveform
                    # would otherwise look like a metric that never reports anything.
                    samples=tuple(getattr(metric_value, "Samples", None) or ()),
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

    def _operation_index(self) -> dict[str, list[_SetOperation]]:
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
            if not target:
                continue

            state = getattr(entity, "state", None)
            lower, upper = _first_range(getattr(state, "AllowedRange", None))
            allowed_range = (lower, upper) if lower is not None or upper is not None else None
            by_target.setdefault(target, []).append(
                _SetOperation(
                    handle=handle,
                    node_type=node_type,
                    enabled=_operation_is_enabled(state),
                    allowed_range=allowed_range,
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
                if getattr(entity, "node_type", None) is not pm.AlertSignalDescriptor:
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
                limits = getattr(state, "Limits", None) or getattr(descriptor, "MaxLimits", None)
                if limits is not None:
                    lower = getattr(limits, "Lower", None)
                    upper = getattr(limits, "Upper", None)

                result[handle] = RemoteAlert(
                    handle=handle,
                    node_type_name=getattr(node_type, "localname", str(node_type)),
                    label=_first_text(getattr(descriptor, "Type", None)),
                    kind=_enum_value(getattr(descriptor, "Kind", None)),
                    priority=_enum_value(getattr(descriptor, "Priority", None)),
                    present=bool(getattr(state, "Presence", False)),
                    activation=_enum_value(getattr(state, "ActivationState", None)),
                    source_handles=tuple(getattr(descriptor, "Source", None) or ()),
                    lower_limit=lower,
                    upper_limit=upper,
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

        metrics = self.metrics()
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
            future = client.set_numeric_value(
                operation_handle,
                fixed_point_decimal(
                    validate_decimal(numeric_value, f"value for {metric_handle!r}"),
                    f"value for {metric_handle!r}",
                ),
            )
        else:
            future = client.set_string(operation_handle, str(value))

        report_part = future.result(timeout=timeout)
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
                if getattr(entity, "node_type", None) is not pm.ActivateOperationDescriptor:
                    continue
                descriptor = getattr(entity, "descriptor", None)
                state = getattr(entity, "state", None)
                found[handle] = RemoteAction(
                    handle=handle,
                    label=_first_text(getattr(descriptor, "Type", None)),
                    type_code=getattr(getattr(descriptor, "Type", None), "Code", None),
                    target_handle=getattr(descriptor, "OperationTarget", None),
                    # Same rule as a metric editor: offer it only when the device says it
                    # is enabled, and follow operation_by_handle for changes.
                    enabled=_operation_is_enabled(state),
                )
            return found

    def run_action(self, action_handle: str, timeout: float = 10.0) -> msg_types.InvocationState:
        """Tell the peer to do something, and wait for the final InvocationState.

        Returns FAILED rather than raising when the action is unknown, so a caller has one
        failure mode to handle instead of two.
        """
        action = self.actions().get(action_handle)
        if action is None:
            logger.warning("no action %s on this device", action_handle)
            return msg_types.InvocationState.FAILED

        future = self._consumer.set_service_client.activate(action_handle, arguments=None)
        report_part = future.result(timeout=timeout)
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
        """Unsubscribe and disconnect."""
        try:
            self._consumer.stop_all()
        except Exception:  # noqa: BLE001 - teardown must not mask the original problem
            logger.exception("error while closing consumer for %s", self.epr)


class ConsumerService:
    """Discovery plus connection management for the consumer side."""

    def __init__(self, ip: str = constants.DEFAULT_IP, *, own_epr: str | None = None) -> None:
        self.ip = ip
        self._own_epr = own_epr
        self._discovery: WSDiscovery | None = None

    def start(self) -> None:
        """Start WS-Discovery on the configured interface."""
        if self._discovery is not None:
            msg = "consumer service is already started"
            raise RuntimeError(msg)
        self._discovery = WSDiscovery(self.ip)
        try:
            self._discovery.start()
        except Exception:
            try:
                self.stop()
            except Exception:
                logger.exception("error while rolling back consumer discovery startup")
            raise
        logger.info("discovery up on %s", self.ip)

    def stop(self) -> None:
        """Stop WS-Discovery."""
        discovery = self._discovery
        self._discovery = None
        if discovery is not None:
            discovery.stop()

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

        deadline = time.monotonic() + timeout
        found: list[DiscoveredDevice] = []
        while time.monotonic() < deadline and not (
            cancel_event is not None and cancel_event.is_set()
        ):
            services = self._discovery.search_services(types=SdcV1Definitions.MedicalDeviceTypesFilter)
            if self._own_epr is not None:
                services = [service for service in services if service.epr != self._own_epr]
            found = [
                DiscoveredDevice(
                    epr=service.epr,
                    x_addrs=tuple(getattr(service, "x_addrs", ()) or ()),
                    scopes=_scope_uris(service),
                    service=service,
                )
                for service in services
            ]
            if len(found) >= expected:
                break
            if cancel_event is None:
                time.sleep(1.0)
            elif cancel_event.wait(1.0):
                break

        logger.info("discovered %d provider(s)", len(found))
        return found

    def connect(self, device: DiscoveredDevice) -> RemoteDevice:
        """Connect to a discovered provider and load its MDIB."""
        consumer = SdcConsumer.from_wsd_service(device.service, ssl_context_container=None)
        try:
            consumer.start_all()
            mdib = ConsumerMdib(consumer, extras_cls=_PeriodicConsumerMdibMethods)
            mdib.init_mdib()
        except Exception:
            try:
                consumer.stop_all()
            except Exception:
                logger.exception("error while rolling back consumer connection to %s", device.epr)
            raise
        logger.info("connected to %s, %d entities", device.epr, len(mdib.entities))
        return RemoteDevice(consumer, mdib, device.epr)
