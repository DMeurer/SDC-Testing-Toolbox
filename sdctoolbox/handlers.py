"""The generic execute handler.

One handler serves every controllable metric. It does not need to know which metric it is
dealing with, because the target handle travels with the call in
``params.operation_instance.operation_target_handle``.

A handler that refuses a request returns a RefusedExecution. sdc11073 3.0.0 reports only the
state of a result, so ReasonedSetService adds the error code and the text to the report.
VersionedGetService does the same kind of repair for the Get service: the narrower answers
leave out the MDIB versions that GetMdib reports.

The simulated faults (DeviceFaults, MetricFault) live here too: the services and handlers are
where a slow, refusing or silent device differs from a healthy one.
"""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sdc11073.provider import SdcProvider
from sdc11073.provider.operations import ExecuteParameters, ExecuteResult
from sdc11073.provider.porttypes.getserviceimpl import GetService
from sdc11073.provider.porttypes.setserviceimpl import SetService
from sdc11073.provider.subscriptionmgr_async import SubscriptionsManagerPathAsync
from sdc11073.xml_types import msg_types, pm_types
from sdc11073.xml_types import pm_qnames as pm

from .model import RemoteRange, numeric_value_in_ranges

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from sdc11073.mdib import ProviderMdib
    from sdc11073.mdib.mdibbase import MdibVersionGroup

logger = logging.getLogger("sdctoolbox.handlers")

# Keeps a text that echoes a requested value from being repeated, in full, to every subscriber.
MAX_REASON_LENGTH = 400

# The request message each kind of operation accepts.
_REQUESTS_BY_OPERATION = {
    pm.SetValueOperationDescriptor: msg_types.SetValue,
    pm.SetStringOperationDescriptor: msg_types.SetString,
    pm.ActivateOperationDescriptor: msg_types.Activate,
}


@dataclass
class RefusedExecution(ExecuteResult):
    """A FAILED result that also says why."""

    error: msg_types.InvocationError = msg_types.InvocationError.OTHER
    error_message: str = ""


def _failed(
    mdib: ProviderMdib,
    reason: str,
    target: str | None = None,
    error: msg_types.InvocationError = msg_types.InvocationError.OTHER,
) -> RefusedExecution:
    reason = reason if len(reason) <= MAX_REASON_LENGTH else reason[: MAX_REASON_LENGTH - 3] + "..."
    logger.warning("rejecting set operation: %s", reason)
    return RefusedExecution(msg_types.InvocationState.FAILED, mdib.mdib_version_group, target, error, reason)


# -- simulated faults ------------------------------------------------------------------

# What a write is refused with while every write is refused.
SIMULATED_REFUSAL = "simulated fault: this device refuses every write (Faults > Refuse every write)"
# The answer delays the window offers; the service takes any delay up to the maximum.
ANSWER_DELAYS_MS = (0, 250, 500, 1000, 2000, 5000)
MAX_ANSWER_DELAY_MS = 60_000


@dataclass
class DeviceFaults:
    """Device-wide faults a consumer under test should survive. Runtime only, never saved."""

    # Wait this long before answering a Set, Activate or Get request.
    answer_delay_ms: int = 0
    # Answer every set and activate request with Fail.
    refuse_writes: bool = False
    # Send no state or waveform reports.
    pause_reports: bool = False
    # Gone from the network as if crashed: no Bye, no answers, no reports.
    silent: bool = False

    @property
    def count(self) -> int:
        """How many of them are on."""
        return sum((self.answer_delay_ms > 0, self.refuse_writes, self.pause_reports, self.silent))


@dataclass(frozen=True)
class MetricFault:
    """A metric's ActivationState, MetricQuality/@Validity and @Mode, as BICEPS spells them.

    As an override None means "as the toolbox normally publishes it": On (or Off when switched
    off), Vld, and Real (Demo for sample arrays).
    """

    activation: str | None = None
    validity: str | None = None
    mode: str | None = None

    @property
    def parts(self) -> tuple[str, ...]:
        """The values that are set, in column order."""
        return tuple(value for value in (self.activation, self.validity, self.mode) if value is not None)


NO_FAULT = MetricFault()
# The values each part can take, in the order the window offers them.
ACTIVATIONS = ("On", "StndBy", "NotRdy", "Fail", "Off", "Shtdn")
VALIDITIES = tuple(member.value for member in pm_types.MeasurementValidity)
MODES = tuple(member.value for member in pm_types.GenerationMode)

_SEEN_WITHOUT_VALUE = {
    "Off": "switched off: no value",
    "StndBy": "the sensor in standby: no value",
    "NotRdy": "not ready yet: no value",
    "Fail": "a failed measurement: no value",
    "Shtdn": "shutting down: no value",
}
_SEEN_VALIDITY = {
    "Vldated": "a value a clinician has validated",
    "Ong": "a value whose measurement is still going on",
    "Qst": "a value marked questionable",
    "Calib": "a value taken while calibrating",
    "Inv": "a value marked invalid: do not use it",
    "Oflw": "a value above the measuring range",
    "Uflw": "a value below the measuring range",
    "NA": "a value marked not available",
}
_SEEN_MODE = {"Real": "from a real measurement", "Test": "test data, not from a patient",
              "Demo": "demo data, not from a patient"}


def what_consumers_see(fault: MetricFault, *, sample_array: bool = False) -> str:
    """In plain words, what a consumer is told by a metric's faulted values."""
    if fault.activation is not None:
        text = _SEEN_WITHOUT_VALUE.get(fault.activation, fault.activation)
        return text.replace("no value", "no samples") if sample_array else text
    parts = [_SEEN_VALIDITY.get(fault.validity, fault.validity)] if fault.validity is not None else []
    if fault.mode is not None:
        parts.append(_SEEN_MODE.get(fault.mode, fault.mode))
    return ", ".join(parts) or "normal"


def apply_fault(state: object, fault: MetricFault) -> None:
    """Write a metric's faulted values over what the toolbox would publish."""
    if fault.activation is not None:
        state.ActivationState = pm_types.ComponentActivation(fault.activation)
    if fault.validity is None and fault.mode is None:
        return
    if getattr(state, "MetricValue", None) is None:
        state.mk_metric_value()
    quality = state.MetricValue.MetricQuality
    if fault.validity is not None:
        quality.Validity = pm_types.MeasurementValidity(fault.validity)
        # An ongoing measurement says since when (10701 TR0216), as the MDIB check asks.
        if fault.validity == "Ong" and state.MetricValue.StartTime is None:
            state.MetricValue.StartTime = state.MetricValue.DeterminationTime or time.time()
    if fault.mode is not None:
        quality.Mode = pm_types.GenerationMode(fault.mode)


def _answer_delay(provider: object) -> None:
    """Hold an answer back while the device simulates a slow answer."""
    faults = getattr(provider, "faults", None)
    if faults is not None and faults.answer_delay_ms > 0:
        time.sleep(faults.answer_delay_ms / 1000)


class FaultableProvider(SdcProvider):
    """An SdcProvider that can pause its reports, drop its subscriptions and go silent.

    shortcut: overrides sdc11073 3.0.0's private report and server hooks; recheck on upgrade.
    """

    def __init__(self, *args, faults: DeviceFaults, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.faults = faults
        self._hidden_service = None
        super().__init__(*args, **kwargs)

    def _send_episodic_reports(self, transaction_result) -> None:  # noqa: ANN001
        if not (self.faults.pause_reports or self.faults.silent):
            super()._send_episodic_reports(transaction_result)
        elif transaction_result.has_descriptor_updates:
            # Kept even while paused: later states would name descriptors a consumer never saw.
            self.hosted_services.description_event_service.send_descriptor_updates(
                transaction_result.descr_updated, transaction_result.descr_created, transaction_result.descr_deleted,
                transaction_result.all_states(), self._mdib.mdib_version_group,
            )

    def _send_rt_notifications(self, rt_states) -> None:  # noqa: ANN001
        if not (self.faults.pause_reports or self.faults.silent):
            super()._send_rt_notifications(rt_states)

    def publish(self) -> None:
        """Send Hello, unless the device is pretending to be gone."""
        if not self.faults.silent:
            super().publish()

    def catch_up(self) -> None:
        """Report every current state once, so subscribers missing reports are up to date again."""
        with self._mdib.mdib_lock:
            states = [deepcopy(state) for state in self._mdib.states.objects]
            contexts = [deepcopy(state) for state in self._mdib.context_states.objects]
            version = self._mdib.mdib_version_group
        events = self.hosted_services.state_event_service
        for wanted, send in (
            (lambda s: s.is_metric_state and not s.is_realtime_sample_array_metric_state,
             events.send_episodic_metric_report),
            (lambda s: s.is_alert_state, events.send_episodic_alert_report),
            (lambda s: s.is_component_state, events.send_episodic_component_state_report),
            (lambda s: s.is_operational_state, events.send_episodic_operational_state_report),
        ):
            chosen = [state for state in states if wanted(state)]
            if chosen:
                send(chosen, version)
        if contexts:
            self.hosted_services.context_service.send_episodic_context_report(contexts, version)

    def drop_subscriptions(self) -> int:
        """Forget every subscription without telling the subscriber, and say how many there were."""
        dropped = 0
        for manager in self._subscriptions_managers.values():
            dropped += len(manager._subscriptions.objects)  # noqa: SLF001
            manager._end_all_subscriptions(send_subscription_end=False)  # noqa: SLF001
        return dropped

    def set_silent(self, silent: bool) -> None:  # noqa: FBT001
        """Vanish as a crashed device does (no Bye, no answers), or come back with a Hello."""
        if silent == self.faults.silent:
            return
        self.faults.silent = silent
        self._http_server.httpd.silent = silent
        local_services = self._wsdiscovery._local_services  # noqa: SLF001
        if silent:
            # Off the list, a Probe or Resolve finds nothing, and stopping later sends no Bye for it.
            self._hidden_service = local_services.pop(self.epr_urn, None)
            self.drop_subscriptions()
            return
        if self._hidden_service is not None:
            # Back on the list first, so the Hello carries the next MetadataVersion.
            local_services[self.epr_urn] = self._hidden_service
            self._hidden_service = None
        self.publish()


def reporting_refusals(
    handler: Callable[[ExecuteParameters], ExecuteResult],
) -> Callable[[ExecuteParameters], ExecuteResult]:
    """Leave a refusal on the operation, where ReasonedSetService finds it for the final report.

    The operation worker runs one operation at a time and notifies right after the handler
    returns, so the last refusal of an operation is the one its failed report is about.
    """

    def wrapped(params: ExecuteParameters) -> ExecuteResult:
        params.operation_instance.refusal = None
        result = handler(params)
        if isinstance(result, RefusedExecution):
            params.operation_instance.refusal = result
        return result

    return wrapped


# What a SubscriptionEnd ends with when the subscriber is gone: its host does not answer, or drops the connection.
_SUBSCRIBER_GONE = ("TimeoutError", "ServerDisconnectedError", "ClientOSError", "ConnectionResetError")


class UnansweredSubscriptionEndFilter(logging.Filter):
    """Note an undeliverable SubscriptionEnd in one info line instead of an error with a traceback.

    A stopping provider says goodbye to every subscriber, best effort. sdc11073 3.0.0 already
    logs a refused connection as info, but a timed-out or dropped one falls into its catch-all
    and is logged as an error, although the subscription is closed either way.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            text = record.getMessage().strip()
            last = text.splitlines()[-1] if text else ""
            if "async_send_notification_end_message" in text and last.split(":")[0].endswith(_SUBSCRIBER_GONE):
                record.levelno, record.levelname = logging.INFO, "INFO"
                record.msg, record.args = f"could not send subscription end, the subscriber did not answer ({last})", ()
        return True


# shortcut: matched on sdc11073 3.0.0's traceback text; tests/provider_core.py says when an upgrade changes it.
logging.getLogger("sdc.device.subscrMgr").addFilter(UnansweredSubscriptionEndFilter())


class QuietSubscriptionsManager(SubscriptionsManagerPathAsync):
    """Skip reports nobody subscribed to.

    The library builds every report and waits for its send thread even when there is no one
    to send it to. A generated waveform reports four times a second, so on a host with many
    providers those empty hand-offs were most of the CPU time.
    """

    def send_to_subscribers(self, payload, action: str, mdib_version_group) -> None:  # noqa: ANN001
        if not self._get_subscriptions_for_action(action):
            return
        super().send_to_subscribers(payload, action, mdib_version_group)


class ReasonedSetService(SetService):
    """A SetService whose failed OperationInvokedReports carry InvocationError and InvocationErrorMessage.

    It also holds every set and activate request back while the device simulates a slow answer.
    """

    def _handle_operation_request(self, request_data, request, set_response):  # noqa: ANN001, ANN202
        _answer_delay(self._sdc_device)
        return super()._handle_operation_request(request_data, request, set_response)

    def notify_operation(  # noqa: PLR0913 - the signature is sdc11073's
        self,
        operation,  # noqa: ANN001
        transaction_id,  # noqa: ANN001
        invocation_state,  # noqa: ANN001
        mdib_version_group,  # noqa: ANN001
        operation_target=None,  # noqa: ANN001
        error=None,  # noqa: ANN001
        error_message=None,  # noqa: ANN001
    ):
        refusal = getattr(operation, "refusal", None)
        if refusal is not None and error is None and invocation_state == msg_types.InvocationState.FAILED:
            error, error_message = refusal.error, refusal.error_message
        super().notify_operation(
            operation, transaction_id, invocation_state, mdib_version_group, operation_target, error, error_message
        )


class VersionedGetService(GetService):
    """A GetService whose partial reads carry MdDescription/@DescriptionVersion and MdState/@StateVersion.

    GetMdib reports both, but sdc11073 copies only the children into the narrower answers, and an
    absent version means 0. The MDIB lock is held so that the version is the one the answer shows.

    Its three requests are held back while the device simulates a slow answer.
    """

    def _on_get_mdib(self, request_data):  # noqa: ANN001, ANN202
        _answer_delay(self._sdc_device)
        return super()._on_get_mdib(request_data)

    def _on_get_md_description(self, request_data):  # noqa: ANN001, ANN202
        _answer_delay(self._sdc_device)
        return super()._on_get_md_description(request_data)

    def mk_get_mddescription_response_message(self, request_data, mdib, requested_handles):  # noqa: ANN001, ANN201
        with mdib.mdib_lock:
            response = super().mk_get_mddescription_response_message(request_data, mdib, requested_handles)
            response.p_msg.payload_element[0].set("DescriptionVersion", str(mdib.mddescription_version))
        return response

    def _on_get_md_state(self, request_data):  # noqa: ANN001, ANN202
        _answer_delay(self._sdc_device)
        with self._mdib.mdib_lock:
            response = super()._on_get_md_state(request_data)
            response.p_msg.payload_element[0].set("StateVersion", str(self._mdib.mdstate_version))
        return response


def _wrong_request_kind(operation_entity: object, request: object) -> str | None:
    """Say how a request does not fit the operation it addresses, or None when it does.

    Only real request messages are judged; the handlers are also called with a bare argument.
    """
    expected = _REQUESTS_BY_OPERATION.get(operation_entity.node_type)
    if expected is None or not isinstance(request, msg_types.AbstractSet) or isinstance(request, expected):
        return None
    return (
        f"operation {operation_entity.handle!r} takes {expected.__name__} requests, "
        f"not {type(request).__name__} requests"
    )


def apply_metric_value(state: object, value: object, fault: MetricFault = NO_FAULT) -> None:
    """Write a value into a metric state, creating the MetricValue only when absent.

    ``mk_metric_value()`` raises ValueError if the state already carries a value, so it must
    not be called on every update - only the first time. A simulated fault stays on the value.
    """
    if getattr(state, "MetricValue", None) is None:
        state.mk_metric_value()
    state.MetricValue.Value = value
    state.MetricValue.DeterminationTime = time.time()  # 10701 TR0214: a value says how old it is
    state.MetricValue.MetricQuality.Validity = pm_types.MeasurementValidity.VALID
    # A value makes a metric On, except one deliberately switched Off: that stays Off.
    if state.ActivationState != pm_types.ComponentActivation.OFF:
        state.ActivationState = pm_types.ComponentActivation.ON
    apply_fault(state, fault)


def _reason(error: Exception) -> str:
    """The text of an exception; a KeyError would otherwise show its message in quotes."""
    if isinstance(error, KeyError) and len(error.args) == 1 and isinstance(error.args[0], str):
        return error.args[0]
    return str(error)


def make_set_handler(
    mdib: ProviderMdib,
    coerce_value: Callable[[str, object], Decimal | str],
    on_applied: Callable[[str], None] | None = None,
    execution_lock: AbstractContextManager | None = None,
    faults: DeviceFaults | None = None,
    fault_for: Callable[[str], MetricFault] = lambda _handle: NO_FAULT,
) -> Callable[[ExecuteParameters], ExecuteResult]:
    """Build the execute handler bound to one provider MDIB.

    The handler applies the requested value to the operation's target metric and reports
    FINISHED, or reports FAILED without touching the MDIB.

    It rejects a request when:

    * the operation is no longer in the MDIB (``Unkn``),
    * the request is not the message the operation takes, for example a ``SetString`` sent
      to a ``SetValue`` operation (``Inv``),
    * the target metric no longer exists,
    * the operation is currently disabled (``OperatingMode`` other than ``En``),
    * the value does not parse as a number for a numeric target,
    * the value is not among ``AllowedValue`` for a choice target,
    * the value falls outside the operation's ``AllowedRange``,
    * the device simulates refusing every write (``faults.refuse_writes``).

    Every rejection is a RefusedExecution: the report carries the InvocationError named
    above, or ``Oth`` where none is, and a text that names the cause.

    The OperatingMode check is deliberate: ``ScoOperationsRegistry.handle_operation_request``
    does not look at it, so without this check a disabled control would still take effect.

    Numeric targets accept finite ``Decimal`` values and decimal strings. Their BICEPS
    ``Resolution`` describes measurement granularity; it is not a decimal-place limit.

    :param fault_for: the simulated fault of a metric, kept on the written value.
    :param on_applied: called with the target handle after the write has been committed.
        It runs outside the transaction, because sdc11073 holds a non-reentrant lock for the
        whole of it and anything wanting a transaction of its own would deadlock.
    """

    def handler(params: ExecuteParameters) -> ExecuteResult:
        if execution_lock is not None:
            with execution_lock:
                return execute(params)
        return execute(params)

    def execute(params: ExecuteParameters) -> ExecuteResult:
        operation_handle = params.operation_instance.handle
        target_handle = params.operation_instance.operation_target_handle
        requested = params.operation_request.argument
        if faults is not None and faults.refuse_writes:
            return _failed(mdib, SIMULATED_REFUSAL, target_handle)

        operation_entity = mdib.entities.by_handle(operation_handle)
        if operation_entity is None:
            return _failed(
                mdib,
                f"operation {operation_handle!r} vanished from the mdib",
                error=msg_types.InvocationError.UNKNOWN_OPERATION,
            )

        mismatch = _wrong_request_kind(operation_entity, params.operation_request)
        if mismatch is not None:
            return _failed(mdib, mismatch, target_handle, msg_types.InvocationError.INVALID_VALUE)

        operating_mode = getattr(operation_entity.state, "OperatingMode", None)
        if operating_mode != pm_types.OperatingMode.ENABLED:
            return _failed(
                mdib,
                f"operation {operation_handle!r} is {operating_mode}, not {pm_types.OperatingMode.ENABLED}",
                target_handle,
            )

        target_entity = mdib.entities.by_handle(target_handle)
        if target_entity is None:
            return _failed(mdib, f"target {target_handle!r} does not exist", target_handle)
        # Only a metric switched Off refuses; other states (e.g. StndBy in a file) behave as before.
        if getattr(target_entity.state, "ActivationState", None) == pm_types.ComponentActivation.OFF:
            return _failed(mdib, f"metric {target_handle!r} is switched off", target_handle)

        try:
            value = coerce_value(target_handle, requested)
        except (KeyError, TypeError, ValueError) as exc:
            return _failed(mdib, _reason(exc), target_handle)

        if isinstance(value, Decimal):
            ranges = tuple(
                RemoteRange(lower=item.Lower, upper=item.Upper, step_width=item.StepWidth)
                for item in operation_entity.state.AllowedRange
            )
            if not numeric_value_in_ranges(value, ranges):
                return _failed(mdib, f"{value} is outside the AllowedRange of {operation_handle!r}", target_handle)
        else:
            allowed = getattr(getattr(operation_entity.state, "AllowedValues", None), "Value", ())
            if allowed and value not in allowed:
                return _failed(mdib, f"value is not in the AllowedValues of {operation_handle!r}", target_handle)
            maximum = getattr(operation_entity.descriptor, "MaxLength", None)
            if maximum is not None and len(value) > maximum:
                return _failed(mdib, f"value exceeds MaxLength of {operation_handle!r}", target_handle)

        apply_metric_value(target_entity.state, value, fault_for(target_handle))

        with mdib.metric_state_transaction() as mgr:
            mgr.write_entity(target_entity)

        if on_applied is not None:
            on_applied(target_handle)

        logger.info("set %s = %r via %s", target_handle, value, operation_handle)
        return ExecuteResult(
            msg_types.InvocationState.FINISHED,
            mdib.mdib_version_group,
            target_handle,
        )

    return reporting_refusals(handler)


def make_activate_handler(
    mdib: ProviderMdib,
    execute_effects: Callable[[str], tuple[MdibVersionGroup, Exception | None]],
    faults: DeviceFaults | None = None,
) -> Callable[[ExecuteParameters], ExecuteResult]:
    """Build the handler shared by every ActivateOperation.

    An activate operation does not carry a value. It means "do the thing", and what the
    thing involves is the device's business - which is precisely why BICEPS has a separate
    operation kind for it rather than making everything a set.

    What this build does is apply the action's declared effects to metrics. That stands in
    for machinery a real device would have behind homing an axis, and it is done through
    metrics on purpose: an action whose result is invisible cannot be checked, and here the
    result is exactly the values it moved.

    OperatingMode is enforced here for the same reason as in the set handler:
    handle_operation_request never looks at it. Refusals are reported the same way too:
    ``Inv`` for a request that is not an ``Activate``, ``Unkn`` for an operation that left the
    MDIB, ``Oth`` with a text for anything else.

    :param execute_effects: provider-owned preparation, commit, and alert processing.
        A returned exception means the effects committed but reporting or subsequent alert
        processing failed; the remote invocation remains successful in that case.
    """

    def handler(params: ExecuteParameters) -> ExecuteResult:
        operation_handle = params.operation_instance.handle
        target_handle = params.operation_instance.operation_target_handle
        if faults is not None and faults.refuse_writes:
            return _failed(mdib, SIMULATED_REFUSAL, target_handle)

        operation_entity = mdib.entities.by_handle(operation_handle)
        if operation_entity is None:
            return _failed(
                mdib,
                f"operation {operation_handle!r} vanished from the mdib",
                error=msg_types.InvocationError.UNKNOWN_OPERATION,
            )

        mismatch = _wrong_request_kind(operation_entity, params.operation_request)
        if mismatch is not None:
            return _failed(mdib, mismatch, target_handle, msg_types.InvocationError.INVALID_VALUE)

        operating_mode = getattr(operation_entity.state, "OperatingMode", None)
        if operating_mode != pm_types.OperatingMode.ENABLED:
            return _failed(
                mdib,
                f"action {operation_handle!r} is {operating_mode}, not {pm_types.OperatingMode.ENABLED}",
                target_handle,
            )

        try:
            version_group, alert_error = execute_effects(operation_handle)
        except (KeyError, TypeError, ValueError) as exc:
            return _failed(mdib, _reason(exc), target_handle)

        if alert_error is not None:
            logger.error(
                "action %s committed its effects, but post-commit processing failed",
                operation_handle,
                exc_info=(type(alert_error), alert_error, alert_error.__traceback__),
            )

        logger.info("action %s ran", operation_handle)
        return ExecuteResult(
            msg_types.InvocationState.FINISHED,
            version_group,
            target_handle,
        )

    return reporting_refusals(handler)
