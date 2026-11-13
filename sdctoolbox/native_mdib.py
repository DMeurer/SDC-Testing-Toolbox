"""Adopting an MDIB from a file into the provider's bookkeeping, and its operation capabilities.

Adoption only reads the graph. It never rebuilds or corrects it: whatever the file says is
what the provider publishes until the user changes something.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from decimal import Decimal
from urllib.parse import quote, urlencode

from sdc11073.provider.baseproduct import BaseProduct
from sdc11073.provider.scopesfactory import KEY_PURPOSE_SERVICE_PROVIDER
from sdc11073.xml_types import msg_types
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.wsd_types import ScopesType

from .handlers import RefusedExecution, reporting_refusals
from .mdib_io import live_entities
from .model import (
    ActionSpec, AlertSpec, DeviceInfo, MetricKind, MetricSpec, RemoteRange, coerce_metric_value, numeric_value_in_ranges,
)

logger = logging.getLogger("sdctoolbox.native_mdib")
KINDS = {kind.descriptor_qname: kind for kind in MetricKind}


def label(descriptor) -> str:
    """Use a supplied concept description, otherwise the descriptor handle."""
    texts = getattr(getattr(descriptor, "Type", None), "ConceptDescription", ())
    return next((text.text for text in texts if text.text), descriptor.Handle)


def device_identity(mdib) -> tuple[DeviceInfo, str | None]:
    """ThisModel fields and serial number from the first MDS MetaData; toolbox defaults otherwise."""
    mds = next(iter(mdib.entities.by_node_type(pm.MdsDescriptor)), None)
    meta = getattr(getattr(mds, "descriptor", None), "MetaData", None)
    if meta is None:
        return DeviceInfo(), None

    def first(values) -> str | None:
        return next((getattr(value, "text", value) for value in values or () if getattr(value, "text", value)), None)

    defaults = DeviceInfo()
    return DeviceInfo(
        manufacturer=first(meta.Manufacturer) or defaults.manufacturer,
        model_name=first(meta.ModelName) or defaults.model_name,
        model_number=meta.ModelNumber or defaults.model_number,
    ), first(meta.SerialNumber)


@dataclass
class Adopted:
    """The bookkeeping ProviderService keeps for a graph it built itself, derived from a file."""

    specs: dict[str, MetricSpec] = field(default_factory=dict)
    alerts: dict[str, AlertSpec] = field(default_factory=dict)
    alert_truths: dict[str, bool] = field(default_factory=dict)
    alert_signals: dict[str, list[str]] = field(default_factory=dict)
    operations: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)


def adopt(mdib) -> Adopted:
    """Derive specs, alarms, signals, set operations and sections from an imported graph."""
    with mdib.mdib_lock:
        return _adopt(live_entities(mdib))


def _adopt(entities: list) -> Adopted:
    by_handle = {entity.handle: entity for entity in entities}
    result = Adopted(specs=_metric_specs(entities, by_handle), alerts=_alert_specs(entities))
    for channel in entities:
        if channel.node_type == pm.ChannelDescriptor:
            result.sections.setdefault(label(channel.descriptor), channel.handle)
    for handle, spec in result.alerts.items():
        source = result.specs.get(spec.source_handle)
        if spec.has_limits and (source is None or source.kind is not MetricKind.NUMBER):
            # Nothing numeric to compare against, so only a manual change can move it.
            spec.lower_limit = spec.upper_limit = spec.max_lower_limit = spec.max_upper_limit = None
        # The truth is what the file says; it is re-derived only when its own source changes.
        result.alert_truths[handle] = bool(by_handle[handle].state.Presence)
        result.alert_signals[handle] = []
    for entity in entities:
        if entity.node_type != pm.AlertSignalDescriptor:
            continue
        signals = result.alert_signals.get(entity.descriptor.ConditionSignaled)
        if signals is not None:
            signals.append(entity.handle)
    for entity in entities:
        handle = entity.handle
        if entity.node_type in (pm.SetValueOperationDescriptor, pm.SetStringOperationDescriptor):
            target = entity.descriptor.OperationTarget
            if target not in result.operations or entity.state.OperatingMode == pm_types.OperatingMode.ENABLED:
                result.operations[target] = handle
    for target, handle in result.operations.items():
        spec = result.specs.get(target)
        if spec is not None:
            result.specs[target] = _with_operation_state(spec, by_handle[handle].state)
    return result


def _with_operation_state(spec: MetricSpec, state) -> MetricSpec:  # noqa: ANN001 - an sdc11073 state
    """Take over the operation's AllowedRange or AllowedValues where the spec can hold them."""
    changes = {}
    ranges = getattr(state, "AllowedRange", None) or ()
    if spec.kind is MetricKind.NUMBER and len(ranges) == 1:
        changes = {"allowed_minimum": ranges[0].Lower, "allowed_maximum": ranges[0].Upper}
    values = list(getattr(getattr(state, "AllowedValues", None), "Value", None) or ())
    if spec.kind is MetricKind.CHOICE and values and values != list(spec.allowed_values):
        changes = {"operation_values": tuple(values)}
    try:
        return replace(spec, **changes)
    except (TypeError, ValueError) as exc:
        logger.info("operation state of %s stays as the file has it: %s", spec.handle, exc)
        return spec


def _coerced_or_raw(coerce, target: str, value: str):
    # A live device accepts an action whose effect it would refuse and refuses it on activation;
    # a reloaded export must behave the same instead of failing to load.
    try:
        return coerce(target, value)
    except (KeyError, TypeError, ValueError):
        return value


def apply_behaviour(mdib, behaviour: dict[str, dict], specs: dict[str, MetricSpec], coerce) -> dict[str, ActionSpec]:
    """Put stored toolbox behaviour back onto adopted specs, and return the actions it defines."""
    actions = {}
    for handle, values in behaviour.items():
        entity = mdib.entities.by_handle(handle)
        if "effects" in values:
            if entity is None or entity.node_type != pm.ActivateOperationDescriptor:
                raise ValueError(f"toolbox action behaviour on {handle!r}, which is not an ActivateOperation")
            effects = {target: _coerced_or_raw(coerce, target, value) for target, value in values["effects"].items()}
            actions[handle] = ActionSpec(label=label(entity.descriptor), target_handle=entity.descriptor.OperationTarget,
                                         effects=effects, handle=handle, note=values["note"])
            continue
        spec = specs.get(handle)
        if spec is None or not spec.is_sample_array:
            raise ValueError(f"toolbox generator behaviour on {handle!r}, which is not a sample array metric")
        for name in ("shape", "cycle_samples", "distribution_shape"):
            if name in values:
                setattr(spec, name, values[name])
    return actions


def metric_specs(mdib) -> dict[str, MetricSpec]:
    """Project only metrics the existing cards can accurately represent."""
    with mdib.mdib_lock:
        entities = live_entities(mdib)
        return _metric_specs(entities, {entity.handle: entity for entity in entities})


def _metric_specs(entities: list, by_handle: dict) -> dict[str, MetricSpec]:
    result = {}
    for entity in entities:
        handle = entity.handle
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
        parent = by_handle.get(entity.parent_handle)
        if parent is not None and parent.node_type == pm.ChannelDescriptor:
            fields["section"] = label(parent.descriptor)
        if kind is MetricKind.CHOICE:
            fields["allowed_values"] = tuple(value.Value for value in descriptor.AllowedValue)
        elif kind is MetricKind.WAVEFORM:
            fields["sample_period"] = Decimal(str(descriptor.SamplePeriod))
        elif kind is MetricKind.DISTRIBUTION:
            domain = descriptor.DistributionRange
            fields.update(domain_minimum=domain.Lower, domain_maximum=domain.Upper)
        activation = getattr(entity.state, "ActivationState", None)
        fields["active"] = activation != pm_types.ComponentActivation.OFF
        try:
            result[handle] = MetricSpec(**fields)
        except (ValueError, TypeError) as exc:
            logger.info("native metric %s is containment-view only: %s", handle, exc)
    return result


def alert_specs(mdib) -> dict[str, AlertSpec]:
    """Project alarm labels and limits for the existing, read-only alarm table."""
    with mdib.mdib_lock:
        return _alert_specs(live_entities(mdib))


def _alert_specs(entities: list) -> dict[str, AlertSpec]:
    result = {}
    for entity in entities:
        handle = entity.handle
        descriptor = entity.descriptor
        if not descriptor.is_alert_condition_descriptor:
            continue
        limits = getattr(entity.state, "Limits", None)
        monitored = getattr(entity.state, "MonitoredAlertLimits", None)
        off = pm_types.AlertConditionMonitoredLimits
        if monitored in (off.LOW_OFF, off.NONE):
            limits = pm_types.Range(upper=limits.Upper) if monitored is off.LOW_OFF else None
        elif monitored is off.HIGH_OFF:
            limits = pm_types.Range(lower=limits.Lower)
        source = descriptor.Source[0] if len(descriptor.Source) == 1 else ", ".join(descriptor.Source)
        spec = AlertSpec(
            handle=handle, label=label(descriptor), source_handle=source,
            kind=descriptor.Kind, priority=descriptor.Priority,
            actual_priority=getattr(entity.state, "ActualPriority", None),
            lower_limit=getattr(limits, "Lower", None), upper_limit=getattr(limits, "Upper", None),
        )
        max_limits = getattr(descriptor, "MaxLimits", None)
        if spec.has_limits and max_limits is not None:
            try:
                spec = replace(spec, max_lower_limit=max_limits.Lower, max_upper_limit=max_limits.Upper)
            except (TypeError, ValueError) as exc:
                logger.info("MaxLimits of %s do not enclose its limits: %s", handle, exc)
        result[handle] = spec
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

    def __init__(self, mdib, sco, log_prefix, set_handler, activate_handler=None, runs_action=lambda _handle: False):
        super().__init__(mdib, sco, log_prefix)
        self._set_handler = set_handler
        self._activate_handler = activate_handler
        # Only actions whose effects the toolbox knows are run; any other Activate is refused.
        self._runs_action = runs_action

    def make_operation_instance(self, descriptor, operation_cls_getter):
        target = self._mdib.entities.by_handle(descriptor.OperationTarget)
        handler = reporting_refusals(self._unsupported)
        if target is not None and (
            descriptor.NODETYPE == pm.SetValueOperationDescriptor and target.node_type == pm.NumericMetricDescriptor
            or descriptor.NODETYPE == pm.SetStringOperationDescriptor
            and target.node_type in (pm.StringMetricDescriptor, pm.EnumStringMetricDescriptor)
        ):
            handler = self._set_handler
        elif (descriptor.NODETYPE == pm.ActivateOperationDescriptor and self._activate_handler is not None
              and self._runs_action(descriptor.Handle)):
            handler = self._activate_handler
        operation_class = operation_cls_getter(descriptor.NODETYPE)
        if operation_class is None:
            return None
        return operation_class(handle=descriptor.Handle, operation_target_handle=descriptor.OperationTarget,
                               operation_handler=handler)

    def _unsupported(self, params):
        handle = params.operation_instance.handle
        logger.warning("native operation %s has no executable business logic", handle)
        return RefusedExecution(
            msg_types.InvocationState.FAILED, self._mdib.mdib_version_group,
            params.operation_instance.operation_target_handle,
            msg_types.InvocationError.OTHER, f"operation {handle!r} has no executable business logic here",
        )


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
