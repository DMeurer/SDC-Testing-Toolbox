"""The generic execute handler.

One handler serves every controllable metric. It does not need to know which metric it is
dealing with, because the target handle travels with the call in
``params.operation_instance.operation_target_handle``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from sdc11073.provider.operations import ExecuteParameters, ExecuteResult
from sdc11073.xml_types import msg_types, pm_types

if TYPE_CHECKING:
    from collections.abc import Callable

    from sdc11073.mdib import ProviderMdib

logger = logging.getLogger("sdctoolbox.handlers")


def _failed(mdib: ProviderMdib, reason: str, target: str | None = None) -> ExecuteResult:
    logger.warning("rejecting set operation: %s", reason)
    return ExecuteResult(msg_types.InvocationState.FAILED, mdib.mdib_version_group, target)


def apply_metric_value(state: object, value: object) -> None:
    """Write a value into a metric state, creating the MetricValue only when absent.

    ``mk_metric_value()`` raises ValueError if the state already carries a value, so it must
    not be called on every update - only the first time.
    """
    if getattr(state, "MetricValue", None) is None:
        state.mk_metric_value()
    state.MetricValue.Value = value
    state.MetricValue.Validity = pm_types.MeasurementValidity.VALID
    state.ActivationState = pm_types.ComponentActivation.ON


def make_set_handler(
    mdib: ProviderMdib,
    coerce_value: Callable[[str, object], Decimal | str],
    on_applied: Callable[[str], None] | None = None,
) -> Callable[[ExecuteParameters], ExecuteResult]:
    """Build the execute handler bound to one provider MDIB.

    The handler applies the requested value to the operation's target metric and reports
    FINISHED, or reports FAILED without touching the MDIB.

    It rejects a request when:

    * the target metric no longer exists,
    * the operation is currently disabled (``OperatingMode`` other than ``En``),
    * the value does not parse as a number for a numeric target,
    * the value is not among ``AllowedValue`` for a choice target,
    * the value falls outside the operation's ``AllowedRange``.

    The OperatingMode check is deliberate: ``ScoOperationsRegistry.handle_operation_request``
    does not look at it, so without this check a disabled control would still take effect.

    Numeric targets accept finite ``Decimal`` values and decimal strings. Their BICEPS
    ``Resolution`` describes measurement granularity; it is not a decimal-place limit.

    :param on_applied: called with the target handle after the write has been committed.
        It runs outside the transaction, because sdc11073 holds a non-reentrant lock for the
        whole of it and anything wanting a transaction of its own would deadlock.
    """

    def handler(params: ExecuteParameters) -> ExecuteResult:
        operation_handle = params.operation_instance.handle
        target_handle = params.operation_instance.operation_target_handle
        requested = params.operation_request.argument

        operation_entity = mdib.entities.by_handle(operation_handle)
        if operation_entity is None:
            return _failed(mdib, f"operation {operation_handle!r} vanished from the mdib")

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
            return _failed(mdib, str(exc), target_handle)

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

    return handler


def make_activate_handler(
    mdib: ProviderMdib,
    effects_for: Callable[[str], dict[str, object]],
    coerce_value: Callable[[str, object], Decimal | str],
    on_applied: Callable[[str], None] | None = None,
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
    handle_operation_request never looks at it.

    :param effects_for: given an operation handle, the metric values that operation sets.
    :param on_applied: called once per touched metric, after the transaction closes.
    """

    def handler(params: ExecuteParameters) -> ExecuteResult:
        operation_handle = params.operation_instance.handle
        target_handle = params.operation_instance.operation_target_handle

        operation_entity = mdib.entities.by_handle(operation_handle)
        if operation_entity is None:
            return _failed(mdib, f"operation {operation_handle!r} vanished from the mdib")

        operating_mode = getattr(operation_entity.state, "OperatingMode", None)
        if operating_mode != pm_types.OperatingMode.ENABLED:
            return _failed(
                mdib,
                f"action {operation_handle!r} is {operating_mode}, not {pm_types.OperatingMode.ENABLED}",
                target_handle,
            )

        effects = effects_for(operation_handle)
        prepared = []
        for handle, value in effects.items():
            entity = mdib.entities.by_handle(handle)
            if entity is None:
                return _failed(mdib, f"effect target {handle!r} does not exist", target_handle)
            try:
                prepared.append((entity, coerce_value(handle, value)))
            except (KeyError, TypeError, ValueError) as exc:
                return _failed(mdib, str(exc), target_handle)

        for entity, value in prepared:
            apply_metric_value(entity.state, value)

        if prepared:
            with mdib.metric_state_transaction() as mgr:
                for entity, _ in prepared:
                    mgr.write_entity(entity)

        if on_applied is not None:
            for entity, _ in prepared:
                on_applied(entity.handle)

        logger.info("action %s ran, changing %d metric(s)", operation_handle, len(prepared))
        return ExecuteResult(
            msg_types.InvocationState.FINISHED,
            mdib.mdib_version_group,
            target_handle,
        )

    return handler
