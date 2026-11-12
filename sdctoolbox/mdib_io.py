"""Safe, combined MDIB snapshots without the profile model's loss of state."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from lxml import etree
from sdc11073 import loghelper
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.mdib import ProviderMdib
from sdc11073.namespaces import default_ns_helper as ns
from sdc11073.pysoap.msgreader import MessageReader
from sdc11073.schema_resolver import mk_schema_validator
from sdc11073.xml_types import msg_qnames as msg
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.dataconverters import TimestampConverter

from .config import ConfigError, write_atomically
from .model import ActionSpec, DistributionShape, MetricKind, MetricSpec, WaveformShape

if TYPE_CHECKING:
    from .provider_service import ProviderService

# Toolbox behaviour BICEPS has no place for: how a sample array is generated and what an
# action does. Written only into an export that asks for it, never into the live MDIB.
BEHAVIOUR_NS = "urn:sdc-testing-toolbox:behaviour"
_MUST_UNDERSTAND = ns.EXT.tag("MustUnderstand")


def _tb(name: str) -> str:
    return f"{{{BEHAVIOUR_NS}}}{name}"


def _read_behaviour(element, handle: str) -> dict:
    """One toolbox element as plain values; anything malformed names the descriptor."""
    name = etree.QName(element).localname
    try:
        if name == "Generator":
            result = {}
            if element.get("Shape") is not None:
                result["shape"] = WaveformShape(element.get("Shape"))
            if element.get("CycleSamples") is not None:
                result["cycle_samples"] = int(element.get("CycleSamples"))
                if result["cycle_samples"] < 1:
                    raise ValueError("CycleSamples must be positive")
            if element.get("DistributionShape") is not None:
                result["distribution_shape"] = DistributionShape(element.get("DistributionShape"))
            return result
        if name == "Action":
            effects = {}
            for effect in element:
                if not isinstance(effect.tag, str):
                    continue
                target, value = effect.get("Target"), effect.get("Value")
                if effect.tag != _tb("Effect") or target is None or value is None:
                    raise ValueError("every effect needs a Target and a Value")
                effects[target] = value
            return {"note": element.get("Note", ""), "effects": effects}
    except ValueError as exc:
        raise ValueError(f"toolbox behaviour of {handle!r}: {exc}") from exc
    raise ValueError(f"toolbox behaviour of {handle!r}: unknown element {name!r}")


def _strip_behaviour(mdib_node) -> dict[str, dict]:
    """Take toolbox behaviour out of a graph before it goes live; other extensions stay."""
    result: dict[str, dict] = {}
    for element in list(mdib_node.iter(_tb("*"))):
        extension = element.getparent()
        if extension is None or extension.tag != ns.EXT.tag("Extension"):
            continue
        owner = extension.getparent()
        handle = owner.get("Handle") if owner is not None else None
        if handle is None:
            raise ValueError("toolbox behaviour may only extend a descriptor")
        result.setdefault(handle, {}).update(_read_behaviour(element, handle))
        extension.remove(element)
        if not any(isinstance(child.tag, str) for child in extension):
            owner.remove(extension)
    return result


def behaviour_elements(specs: dict[str, MetricSpec], actions: dict[str, ActionSpec]) -> dict[str, list]:
    """The toolbox elements to attach per descriptor handle; defaults are left out."""
    defaults = MetricSpec("defaults", MetricKind.NUMBER)
    result: dict[str, list] = {}
    for handle, spec in specs.items():
        attributes = {}
        if spec.kind is MetricKind.WAVEFORM:
            if spec.shape is not defaults.shape:
                attributes["Shape"] = spec.shape.value
            if spec.cycle_samples != defaults.cycle_samples:
                attributes["CycleSamples"] = str(spec.cycle_samples)
        elif spec.kind is MetricKind.DISTRIBUTION and spec.distribution_shape is not defaults.distribution_shape:
            attributes["DistributionShape"] = spec.distribution_shape.value
        if attributes:
            result[handle] = [etree.Element(_tb("Generator"), attributes)]
    for handle, action in actions.items():
        # Written even without effects, so a reload knows the toolbox may run this action.
        element = etree.Element(_tb("Action"), {"Note": action.note} if action.note else {})
        for target, value in action.effects.items():
            etree.SubElement(element, _tb("Effect"), Target=target, Value=str(value))
        result[handle] = [element]
    return result


def _attach_behaviour(node, behaviour: dict[str, list]) -> None:
    extension_tag = ns.EXT.tag("Extension")
    for descriptor in node.find(pm.MdDescription).iter():
        elements = behaviour.get(descriptor.get("Handle")) if isinstance(descriptor.tag, str) else None
        if not elements:
            continue
        first = descriptor[0] if len(descriptor) else None
        if first is None or first.tag != extension_tag:
            first = etree.Element(extension_tag)
            descriptor.insert(0, first)
        for element in elements:
            marked = deepcopy(element)
            marked.set(_MUST_UNDERSTAND, "false")
            first.append(marked)


@dataclass(frozen=True)
class NativeMdibConfig:
    """Immutable input bytes; activation validates and materializes its own live MDIB."""

    xml: bytes


@lru_cache(maxsize=1)
def _schema() -> etree.XMLSchema:
    return mk_schema_validator([entry.value for entry in ns.prefix_enum], ns)


@lru_cache(maxsize=1)
def _reader() -> MessageReader:
    return MessageReader(SdcV1Definitions, None, loghelper.get_logger_adapter("sdctoolbox.mdib"), validate=False)


def _combined_root(data: bytes):
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, remove_comments=True)
    root = etree.fromstring(data, parser)
    if root.getroottree().docinfo.doctype:
        raise ValueError("DTDs and entities are not permitted in MDIB files")
    if root.tag == msg.GetMdibResponse:
        if len(root) != 1 or root[0].tag != msg.Mdib:
            raise ValueError("GetMdibResponse must contain exactly one Mdib")
        response, mdib = root, root[0]
    elif root.tag in (msg.Mdib, ns.PM.tag("MdibContainer")):
        mdib = root
        mdib.tag = str(msg.Mdib)
        response = etree.Element(msg.GetMdibResponse, nsmap=mdib.nsmap)
        for key in ("MdibVersion", "SequenceId", "InstanceId"):
            if key in mdib.attrib:
                response.set(key, mdib.get(key))
        response.append(mdib)
    else:
        raise ValueError("expected a combined MDIB XML file, not a split description/state document or unknown root")
    description = mdib.find(pm.MdDescription)
    states = mdib.find(pm.MdState)
    if description is None or states is None:
        raise ValueError("MDIB files must contain both MdDescription and MdState in one file")
    # The pinned library models extensions on entities, but not on aggregate containers.
    if any(node.find(ns.EXT.tag("Extension")) is not None for node in (mdib, description, states)):
        raise ValueError("aggregate MDIB/description/state extensions cannot be preserved by sdc11073 3.0.0")
    _schema().assertValid(response)
    return response, mdib, description, states


def _validate_graph(descriptors: list, states: list) -> None:
    by_handle = {}
    for descriptor in descriptors:
        if descriptor.Handle in by_handle:
            raise ValueError(f"duplicate descriptor handle {descriptor.Handle!r}")
        by_handle[descriptor.Handle] = descriptor
    if not any(d.NODETYPE == pm.MdsDescriptor for d in descriptors):
        raise ValueError("MDIB must contain at least one Mds")
    state_keys = set()
    context_handles = set(by_handle)
    associated = set()
    for state in states:
        descriptor = by_handle.get(state.DescriptorHandle)
        if descriptor is None:
            raise ValueError(f"state references missing descriptor {state.DescriptorHandle!r}")
        if state.NODETYPE != descriptor.STATE_QNAME:
            raise ValueError(f"state type does not match descriptor {state.DescriptorHandle!r}")
        if state.DescriptorVersion != descriptor.DescriptorVersion:
            raise ValueError(f"state DescriptorVersion does not match {state.DescriptorHandle!r}")
        if state.is_multi_state:
            if state.Handle in context_handles:
                raise ValueError(f"duplicate context/descriptor handle {state.Handle!r}")
            context_handles.add(state.Handle)
            if state.ContextAssociation == pm_types.ContextAssociation.ASSOCIATED:
                if state.DescriptorHandle in associated:
                    raise ValueError(f"multiple associated states for {state.DescriptorHandle!r}")
                associated.add(state.DescriptorHandle)
        elif state.DescriptorHandle in state_keys:
            raise ValueError(f"duplicate state for {state.DescriptorHandle!r}")
        else:
            state_keys.add(state.DescriptorHandle)
    for descriptor in descriptors:
        if descriptor.parent_handle is not None and descriptor.parent_handle not in by_handle:
            raise ValueError(f"missing parent for {descriptor.Handle!r}")
        if descriptor.is_operational_descriptor and descriptor.OperationTarget not in context_handles:
            raise ValueError(f"operation {descriptor.Handle!r} references a missing target")
        if descriptor.is_alert_signal_descriptor:
            condition = by_handle.get(descriptor.ConditionSignaled)
            if condition is None or not condition.is_alert_condition_descriptor:
                raise ValueError(f"signal {descriptor.Handle!r} references a missing alert condition")
        for source in getattr(descriptor, "Source", ()):
            if source not in by_handle:
                raise ValueError(f"alert {descriptor.Handle!r} references missing source {source!r}")
        for relation in getattr(descriptor, "Relation", ()):
            for entry in relation.Entries:
                if entry not in by_handle:
                    raise ValueError(f"metric relation references missing entry {entry!r}")
    for state in states:
        operations = [*getattr(state, "InvocationRequested", ()), *getattr(state, "InvocationRequired", ())]
        for group in getattr(state, "OperationGroup", ()):
            operations.extend(group.Operations)
        for handle in operations:
            if handle not in by_handle or not by_handle[handle].is_operational_descriptor:
                raise ValueError(f"operation list references missing/non-operation {handle!r}")
        for field, kind in (("PresentTechnicalAlarmConditions", pm_types.AlertConditionKind.TECHNICAL),
                            ("PresentPhysiologicalAlarmConditions", pm_types.AlertConditionKind.PHYSIOLOGICAL)):
            for handle in getattr(state, field, ()):
                condition = by_handle.get(handle)
                if (condition is None or not condition.is_alert_condition_descriptor
                        or condition.Kind != kind or condition.parent_handle != state.DescriptorHandle):
                    raise ValueError(f"{field} references an invalid alarm condition {handle!r}")


def _extension_signatures(root) -> Counter:
    """Locate opaque extensions by entity and nested field, ignoring XML formatting."""
    def content(node):
        text = node.text if node.text and node.text.strip() else None
        tail = node.tail if node.tail and node.tail.strip() else None
        return (node.tag, tuple(sorted(node.attrib.items())), text, tail,
                tuple(content(child) for child in node if isinstance(child.tag, str)))

    result = Counter()
    for extension in root.iter(ns.EXT.tag("Extension")):
        if any(parent.tag == ns.EXT.tag("Extension") for parent in extension.iterancestors()):
            continue
        path = []
        for parent in extension.iterancestors():
            if parent.tag == pm.State:
                owner = ("state", parent.get("DescriptorHandle"), parent.get("Handle"))
                break
            if parent.get("Handle") is not None:
                owner = ("descriptor", parent.get("Handle"))
                break
            siblings = list(parent.itersiblings(preceding=True))
            path.append((parent.tag, sum(sibling.tag == parent.tag for sibling in siblings)))
        else:
            owner = ("aggregate",)
        result[(owner, tuple(reversed(path)), content(extension))] += 1
    return result


def parse_mdib(data: bytes) -> ProviderMdib:
    """Validate a combined snapshot and retain its supplied states, including history."""
    try:
        _, node, description, states_node = _combined_root(data)
        behaviour = _strip_behaviour(node)
        descriptors, states = _reader().read_get_mdib_payload(node)
        _validate_graph(descriptors, states)
        # from_string() resets alarm presence and operation modes even for supplied states.
        mdib = ProviderMdib()
        mdib.add_description_containers(descriptors)
        mdib.add_state_containers(states)
        mdib.xtra.mk_state_containers_for_all_descriptors()
        mdib.xtra.update_retrievability_lists()
        mdib.xtra.set_all_source_mds()
        mdib.sequence_id = node.get("SequenceId")
        mdib.instance_id = int(node.get("InstanceId")) if node.get("InstanceId") is not None else None
        mdib.mdib_version = int(node.get("MdibVersion", "0"))
        mdib.mddescription_version = int(description.get("DescriptionVersion", "0"))
        mdib.mdstate_version = int(states_node.get("StateVersion", "0"))
        if next(node.iter(ns.EXT.tag("Extension")), None) is not None:
            if _extension_signatures(node) != _extension_signatures(_snapshot_node(mdib)):
                raise ValueError("a nested extension cannot be preserved by sdc11073 3.0.0")
        # shortcut: carried on the library object so every start(parse_mdib(...)) caller gets it
        # without a second return value; move to NativeMdibConfig if more metadata ever rides along.
        mdib.toolbox_behaviour = behaviour
        return mdib
    except (etree.LxmlError, ValueError, TypeError, LookupError, AttributeError) as exc:
        raise ConfigError(f"cannot load native MDIB: {exc}") from exc


def load_mdib_file(path: str | Path) -> NativeMdibConfig:
    """Read and validate once, retaining bytes rather than a mutable live graph."""
    source = Path(path)
    try:
        data = source.read_bytes()
        parse_mdib(data)
    except (OSError, ConfigError) as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    return NativeMdibConfig(data)


def _snapshot_node(mdib: ProviderMdib):
    """Serialize copies: the library's ClockState serializer writes the current time."""
    with mdib.mdib_lock:
        snapshot = ProviderMdib(mdib.sdc_definitions)
        snapshot.add_description_containers(deepcopy(list(mdib.descriptions.objects)))
        snapshot.add_state_containers(deepcopy([*mdib.states.objects, *mdib.context_states.objects]))
        for name in ("sequence_id", "instance_id", "mdib_version", "mddescription_version", "mdstate_version"):
            setattr(snapshot, name, getattr(mdib, name))
    clocks = {state.DescriptorHandle: state.DateAndTime for state in snapshot.states.objects
              if state.NODETYPE == pm.ClockState}
    node, _ = snapshot.reconstruct_mdib_with_context_states()
    for state_node in node.find(pm.MdState):
        handle = state_node.get("DescriptorHandle")
        if handle in clocks:
            if clocks[handle] is None:
                state_node.attrib.pop("DateAndTime", None)
            else:
                state_node.set("DateAndTime", TimestampConverter.to_xml(clocks[handle]))
    return node


def mdib_bytes(mdib: ProviderMdib, behaviour: dict[str, list] | None = None) -> bytes:
    """Serialize one coherent description/state graph, including context history.

    :param behaviour: toolbox elements per descriptor handle, from behaviour_elements(), to
        add as extensions. None writes plain BICEPS.
    """
    node = _snapshot_node(mdib)
    if behaviour:
        _attach_behaviour(node, behaviour)
    response = etree.Element(msg.GetMdibResponse, nsmap=mdib.nsmapper.ns_map)
    for key in ("MdibVersion", "SequenceId", "InstanceId"):
        if key in node.attrib:
            response.set(key, node.get(key))
    response.append(node)
    _schema().assertValid(response)
    return etree.tostring(response, encoding="UTF-8", xml_declaration=True, pretty_print=True)


def save_mdib(service: ProviderService, path: str | Path, *, behaviour: bool = False) -> Path:
    """Atomically export both description and state to one file, with toolbox behaviour on request."""
    target = Path(path)
    try:
        payload = mdib_bytes(service.mdib, service.behaviour_extensions() if behaviour else None)
    except (etree.LxmlError, ValueError, TypeError) as exc:
        raise ConfigError(f"cannot serialize native MDIB: {exc}") from exc
    write_atomically(target, payload)
    return target
