"""Safe, combined MDIB snapshots without the profile model's loss of state."""

from __future__ import annotations

import tempfile
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

from .config import ConfigError

if TYPE_CHECKING:
    from .provider_service import ProviderService


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


def mdib_bytes(mdib: ProviderMdib) -> bytes:
    """Serialize one coherent description/state graph, including context history."""
    node = _snapshot_node(mdib)
    response = etree.Element(msg.GetMdibResponse, nsmap=mdib.nsmapper.ns_map)
    for key in ("MdibVersion", "SequenceId", "InstanceId"):
        if key in node.attrib:
            response.set(key, node.get(key))
    response.append(node)
    _schema().assertValid(response)
    return etree.tostring(response, encoding="UTF-8", xml_declaration=True, pretty_print=True)


def save_mdib(service: ProviderService, path: str | Path) -> Path:
    """Atomically export both description and state to one file."""
    target = Path(path)
    try:
        payload = mdib_bytes(service.mdib)
    except (etree.LxmlError, ValueError, TypeError) as exc:
        raise ConfigError(f"cannot serialize native MDIB: {exc}") from exc
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
