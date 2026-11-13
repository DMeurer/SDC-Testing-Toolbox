"""The generic execute handler.

One handler serves every controllable metric. It does not need to know which metric it is
dealing with, because the target handle travels with the call in
``params.operation_instance.operation_target_handle``.

A handler that refuses a request returns a RefusedExecution. sdc11073 3.0.0 reports only the
state of a result, so ReasonedSetService adds the error code and the text to the report.
VersionedGetService does the same kind of repair for the Get service: the narrower answers
leave out the MDIB versions that GetMdib reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

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
    """A SetService whose failed OperationInvokedReports carry InvocationError and InvocationErrorMessage."""

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
    """

    def mk_get_mddescription_response_message(self, request_data, mdib, requested_handles):  # noqa: ANN001, ANN201
        with mdib.mdib_lock:
            response = super().mk_get_mddescription_response_message(request_data, mdib, requested_handles)
            response.p_msg.payload_element[0].set("DescriptionVersion", str(mdib.mddescription_version))
        return response

    def _on_get_md_state(self, request_data):  # noqa: ANN001, ANN202
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


def apply_metric_value(state: object, value: object) -> None:
    """Write a value into a metric state, creating the MetricValue only when absent.

    ``mk_metric_value()`` raises ValueError if the state already carries a value, so it must
    not be called on every update - only the first time.
    """
    if getattr(state, "MetricValue", None) is None:
        state.mk_metric_value()
    state.MetricValue.Value = value
    state.MetricValue.MetricQuality.Validity = pm_types.MeasurementValidity.VALID
    state.ActivationState = pm_types.ComponentActivation.ON


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
    * the value falls outside the operation's ``AllowedRange``.

    Every rejection is a RefusedExecution: the report carries the InvocationError named
    above, or ``Oth`` where none is, and a text that names the cause.

    The OperatingMode check is deliberate: ``ScoOperationsRegistry.handle_operation_request``
    does not look at it, so without this check a disabled control would still take effect.

    Numeric targets accept finite ``Decimal`` values and decimal strings. Their BICEPS
    ``Resolution`` describes measurement granularity; it is not a decimal-place limit.

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

        apply_metric_value(target_entity.state, value)

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
