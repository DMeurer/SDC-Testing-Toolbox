"""The generic execute handler.

One handler serves every controllable metric. It does not need to know which metric it is
dealing with, because the target handle travels with the call in
``params.operation_instance.operation_target_handle``.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
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


def make_set_handler(mdib: ProviderMdib) -> Callable[[ExecuteParameters], ExecuteResult]:
    """Build the execute handler bound to one provider MDIB.

    The handler applies the requested value to the operation's target metric and reports
    FINISHED, or reports FAILED without touching the MDIB.

    It rejects a request when:

    * the target metric no longer exists,
    * the operation is currently disabled (``OperatingMode`` other than ``En``),
    * the value does not parse as a number for a numeric target,
    * the value is not among ``AllowedValue`` for a choice target.

    The OperatingMode check is deliberate: ``ScoOperationsRegistry.handle_operation_request``
    does not look at it, so without this check a disabled control would still take effect.
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

        descriptor = target_entity.descriptor

        allowed = getattr(descriptor, "AllowedValue", None)
        if allowed:
            permitted = [item.Value for item in allowed]
            if str(requested) not in permitted:
                return _failed(
                    mdib,
                    f"{requested!r} is not among the allowed values {permitted} of {target_handle!r}",
                    target_handle,
                )
            value: object = str(requested)
        elif hasattr(descriptor, "Resolution"):
            # Numeric target. Only Integers, no Floats, because of precision issues.
            # The Resolution attribute is basically "how many decimal places are allowed", so it is present on all numeric metrics.
            try:
                value = requested if isinstance(requested, Decimal) else Decimal(str(requested))
            except (InvalidOperation, ValueError):
                return _failed(mdib, f"{requested!r} is not a number for {target_handle!r}", target_handle)

            # AllowedRange on the operation state is what a remote caller must respect.
            # The library does not police it, just as it does not police OperatingMode.
            for permitted_range in getattr(operation_entity.state, "AllowedRange", None) or []:
                lower = getattr(permitted_range, "Lower", None)
                upper = getattr(permitted_range, "Upper", None)
                if lower is not None and value < lower:
                    return _failed(
                        mdib,
                        f"{value} is below the allowed minimum {lower} of {target_handle!r}",
                        target_handle,
                    )
                if upper is not None and value > upper:
                    return _failed(
                        mdib,
                        f"{value} is above the allowed maximum {upper} of {target_handle!r}",
                        target_handle,
                    )
        else:
            value = str(requested)

        apply_metric_value(target_entity.state, value)

        with mdib.metric_state_transaction() as mgr:
            mgr.write_entity(target_entity)

        logger.info("set %s = %r via %s", target_handle, value, operation_handle)
        return ExecuteResult(
            msg_types.InvocationState.FINISHED,
            mdib.mdib_version_group,
            target_handle,
        )

    return handler
