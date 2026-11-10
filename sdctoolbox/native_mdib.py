"""Read-only UI projections and explicit operation capabilities for native MDIBs.

These projections never rebuild the imported graph or define simulation behaviour.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from urllib.parse import quote, urlencode

from sdc11073.provider.baseproduct import BaseProduct
from sdc11073.provider.operations import ExecuteResult
from sdc11073.provider.scopesfactory import KEY_PURPOSE_SERVICE_PROVIDER
from sdc11073.xml_types import msg_types
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.wsd_types import ScopesType

from .model import (
    AlertSpec, MetricKind, MetricSpec, RemoteRange, coerce_metric_value, numeric_value_in_ranges,
)

logger = logging.getLogger("sdctoolbox.native_mdib")
KINDS = {kind.descriptor_qname: kind for kind in MetricKind}


def label(descriptor) -> str:
    """Use a supplied concept description, otherwise the descriptor handle."""
    texts = getattr(getattr(descriptor, "Type", None), "ConceptDescription", ())
    return next((text.text for text in texts if text.text), descriptor.Handle)


def metric_specs(mdib) -> dict[str, MetricSpec]:
    """Project only metrics the existing cards can accurately represent."""
    result = {}
    for handle, entity in mdib.entities.items():
        kind = KINDS.get(entity.node_type)
        if kind is None:
            continue
        descriptor = entity.descriptor
        ranges = getattr(descriptor, "TechnicalRange", ())
        bounds = ranges[0] if len(ranges) == 1 else None
        units = getattr(getattr(descriptor, "Unit", None), "ConceptDescription", ())
        fields = {
            "handle": handle, "label": label(descriptor), "kind": kind,
            "unit_label": next((text.text for text in units if text.text), ""),
            "resolution": getattr(descriptor, "Resolution", None),
            "minimum": getattr(bounds, "Lower", None), "maximum": getattr(bounds, "Upper", None),
        }
        if kind is MetricKind.CHOICE:
            fields["allowed_values"] = tuple(value.Value for value in descriptor.AllowedValue)
        elif kind is MetricKind.WAVEFORM:
            fields["sample_period"] = Decimal(str(descriptor.SamplePeriod))
        elif kind is MetricKind.DISTRIBUTION:
            domain = descriptor.DistributionRange
            fields.update(domain_minimum=domain.Lower, domain_maximum=domain.Upper)
        try:
            result[handle] = MetricSpec(**fields)
        except (ValueError, TypeError) as exc:
            logger.info("native metric %s is containment-view only: %s", handle, exc)
    return result


def alert_specs(mdib) -> dict[str, AlertSpec]:
    """Project alarm labels and limits for the existing, read-only alarm table."""
    result = {}
    for handle, entity in mdib.entities.items():
        descriptor = entity.descriptor
        if not descriptor.is_alert_condition_descriptor:
            continue
        limits = getattr(entity.state, "Limits", None)
        result[handle] = AlertSpec(
            handle=handle, label=label(descriptor), source_handle=", ".join(descriptor.Source),
            kind=descriptor.Kind, priority=getattr(entity.state, "ActualPriority", None) or descriptor.Priority,
            lower_limit=getattr(limits, "Lower", None), upper_limit=getattr(limits, "Upper", None),
        )
    return result


def coerce_value(mdib, handle: str, value: object, spec: MetricSpec | None) -> Decimal | str:
    """Validate local/remote scalar writes against the actual native descriptor."""
    if spec is None:
        raise ValueError(f"native metric {handle!r} has no supported scalar editor")
    normalized = coerce_metric_value(spec, value, handle)
    if isinstance(normalized, Decimal):
        descriptor = mdib.entities.by_handle(handle).descriptor
        ranges = tuple(RemoteRange(lower=r.Lower, upper=r.Upper, step_width=r.StepWidth)
                       for r in descriptor.TechnicalRange)
        if not numeric_value_in_ranges(normalized, ranges):
            raise ValueError(f"value is outside the TechnicalRange of {handle!r}")
    return normalized


class SnapshotProduct(BaseProduct):
    """Register existing operations without creating descriptors or resetting state."""

    def __init__(self, mdib, sco, log_prefix, set_handler, activate_handler=None):
        super().__init__(mdib, sco, log_prefix)
        self._set_handler = set_handler
        self._activate_handler = activate_handler

    def make_operation_instance(self, descriptor, operation_cls_getter):
        target = self._mdib.entities.by_handle(descriptor.OperationTarget)
        handler = self._unsupported
        if target is not None and (
            descriptor.NODETYPE == pm.SetValueOperationDescriptor and target.node_type == pm.NumericMetricDescriptor
            or descriptor.NODETYPE == pm.SetStringOperationDescriptor
            and target.node_type in (pm.StringMetricDescriptor, pm.EnumStringMetricDescriptor)
        ):
            handler = self._set_handler
        elif descriptor.NODETYPE == pm.ActivateOperationDescriptor and self._activate_handler is not None:
            handler = self._activate_handler
        operation_class = operation_cls_getter(descriptor.NODETYPE)
        if operation_class is None:
            return None
        return operation_class(handle=descriptor.Handle, operation_target_handle=descriptor.OperationTarget,
                               operation_handler=handler)

    def _unsupported(self, params):
        logger.warning("native operation %s has no executable business logic", params.operation_instance.handle)
        return ExecuteResult(msg_types.InvocationState.FAILED, self._mdib.mdib_version_group,
                             params.operation_instance.operation_target_handle)


def scopes(mdib) -> ScopesType:
    """Publish type scopes and an unambiguous, fully identified imported location."""
    result = ScopesType()
    result.text.append(KEY_PURPOSE_SERVICE_PROVIDER)
    for entity in mdib.entities.by_node_type(pm.MdsDescriptor):
        coding = entity.descriptor.Type
        if coding is not None and coding.Code:
            values = (coding.CodingSystem, coding.CodingSystemVersion, coding.Code)
            result.text.append("sdc.cdc.type:/" + "/".join(quote(value or "", safe="") for value in values))
    locations = [state for entity in mdib.entities.by_node_type(pm.LocationContextDescriptor)
                 for state in entity.states.values() if state.ContextAssociation == pm_types.ContextAssociation.ASSOCIATED]
    if len(locations) == 1 and locations[0].LocationDetail is not None:
        state = locations[0]
        detail = state.LocationDetail
        query = urlencode({key: value for key, value in (
            ("fac", detail.Facility), ("bldng", detail.Building), ("flr", detail.Floor),
            ("poc", detail.PoC), ("rm", detail.Room), ("bed", detail.Bed),
        ) if value is not None}, quote_via=quote)
        for identification in state.Identification:
            value = "sdc.ctxt.loc:/" + quote(identification.Root or "biceps.uri.unk", safe="")
            if identification.Extension:
                value += "/" + quote(identification.Extension, safe="")
            result.text.append(value + ("?" + query if query else ""))
    return result
