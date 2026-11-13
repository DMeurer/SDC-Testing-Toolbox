"""Round-trip and validation tests for config files.

Nothing here touches the network beyond starting a provider on loopback, so it is quick
compared with the other suites.

Exit code 0 means all checks passed.

Usage:  .venv/Scripts/python.exe tests/config_roundtrip.py
"""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
from dataclasses import fields
from decimal import Decimal
from itertools import pairwise, permutations
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report  # noqa: E402
from sdc11073 import observableproperties  # noqa: E402
from sdc11073.consumer.consumerimpl import SdcConsumer  # noqa: E402
from sdc11073.definitions_sdc import SdcV1Definitions  # noqa: E402
from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.mdib import ConsumerMdib  # noqa: E402
from sdc11073.xml_types import pm_qnames as pm  # noqa: E402
from sdc11073.xml_types import msg_types, pm_types  # noqa: E402

from sdctoolbox import config, constants  # noqa: E402
from sdctoolbox.consumer_service import RemoteDevice  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    ActionSpec,
    AlertKind,
    AlertManifestation,
    AlertPriority,
    AlertSignalSpec,
    AlertSpec,
    Coding,
    DeviceInfo,
    DistributionShape,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    PatientMeasurement,
    WaveformShape,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402

REFERENCE_DEVICE = DeviceInfo(
    friendly_name="Round-trip instrument",
    manufacturer="Example Devices",
    manufacturer_url="https://example.com/devices",
    model_name="Config Exerciser",
    model_number="CFG-18",
    firmware_version="18.4",
)


def coding_snapshot(coding: Coding | None) -> dict | None:
    if coding is None:
        return None
    return {"code": coding.code, "system": coding.system, "label": coding.label}


def snapshot(service: ProviderService) -> dict:
    """Every running-provider field emitted by config.to_dict()."""
    return {
        "metrics": {
            handle: {
                "handle": handle,
                "label": spec.label,
                "kind": spec.kind.value,
                "section": spec.section,
                "unit_label": spec.unit_label,
                "unit_coding": coding_snapshot(spec.unit_coding),
                "type_coding": coding_snapshot(spec.type_coding),
                "allowed_values": spec.allowed_values,
                "resolution": spec.resolution,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "controllable": spec.controllable,
                "initial_value": None if spec.is_sample_array else service.get_value(handle),
                "sample_period": spec.sample_period,
                "shape": spec.shape.value,
                "cycle_seconds": spec.cycle_seconds,
                "domain_unit_label": spec.domain_unit_label,
                "domain_unit_coding": coding_snapshot(spec.domain_unit_coding),
                "domain_minimum": spec.domain_minimum,
                "domain_maximum": spec.domain_maximum,
                "distribution_shape": spec.distribution_shape.value,
                "allowed_range": spec.effective_allowed_range(),
                "operation_values": spec.operation_values,
                "active": spec.active,
            }
            for handle, spec in service.list_metrics().items()
        },
        "alerts": {
            handle: {
                "handle": handle,
                "label": spec.label,
                "source_handle": spec.source_handle,
                "kind": spec.kind.value,
                "priority": spec.priority.value,
                "signals": [
                    {"manifestation": signal.manifestation.value, "latching": signal.latching}
                    for signal in spec.signals
                ],
                "lower_limit": spec.lower_limit,
                "upper_limit": spec.upper_limit,
                "delegable": spec.delegable,
                "max_limits": service._max_limits(spec),  # noqa: SLF001 - what MaxLimits carries
                "actual_priority": spec.actual_priority,
            }
            for handle, spec in service.list_alerts().items()
        },
        "actions": {
            handle: {
                "handle": handle,
                "label": spec.label,
                "target_handle": spec.target_handle,
                "type_coding": coding_snapshot(spec.type_coding),
                "note": spec.note,
                "effects": dict(spec.effects),
            }
            for handle, spec in service.list_actions().items()
        },
        "sections": service.sections(),
        "contexts": {
            "location": {
                "facility": service.get_location().facility,
                "building": service.get_location().building,
                "floor": service.get_location().floor,
                "point_of_care": service.get_location().point_of_care,
                "room": service.get_location().room,
                "bed": service.get_location().bed,
            },
            "patient": {
                "given_name": service.get_patient().given_name,
                "family_name": service.get_patient().family_name,
                "sex": service.get_patient().sex,
                "patient_type": service.get_patient().patient_type,
                "date_of_birth": service.get_patient().date_of_birth,
                "height": (
                    {
                        "value": service.get_patient().height.value,
                        "unit": coding_snapshot(service.get_patient().height.unit),
                    }
                    if service.get_patient().height is not None
                    else None
                ),
                "weight": (
                    {
                        "value": service.get_patient().weight.value,
                        "unit": coding_snapshot(service.get_patient().weight.unit),
                    }
                    if service.get_patient().weight is not None
                    else None
                ),
                "race": coding_snapshot(service.get_patient().race),
            },
        },
    }


def device_snapshot(instance_name: str, device: DeviceInfo | None) -> dict | None:
    if device is None:
        return None
    return {
        "instance_name": instance_name,
        "friendly_name": device.friendly_name,
        "manufacturer": device.manufacturer,
        "manufacturer_url": device.manufacturer_url,
        "model_name": device.model_name,
        "model_number": device.model_number,
        "firmware_version": device.firmware_version,
    }


def device_config_snapshot(device: config.DeviceConfig) -> dict:
    """Order-independent semantics persisted by one profile."""
    return {
        "metrics": {spec.handle: spec for spec in device.metrics},
        "alerts": {spec.handle: spec for spec in device.alerts},
        "actions": {spec.handle: spec for spec in device.actions},
        "location": device.location,
        "patient": device.patient,
        "device": device.device,
        "instance_name": device.instance_name,
    }


def complete_snapshot(service: ProviderService) -> dict:
    """Provider configuration, descriptors, and live bookkeeping changed by import."""
    descriptors = {}
    for handle, entity in service.mdib.entities.items():
        descriptor = entity.descriptor
        descriptors[handle] = (
            descriptor.NODETYPE.localname,
            descriptor.parent_handle,
            descriptor.DescriptorVersion,
            str(getattr(descriptor, "OperationTarget", "")),
            str(getattr(descriptor, "ConditionSignaled", "")),
            tuple(getattr(descriptor, "Source", ()) or ()),
        )
    mdib_node = service.mdib.reconstruct_mdib_with_context_states()[0]
    for node in reversed(list(mdib_node.iter())):
        node[:] = sorted(
            node,
            key=lambda child: (
                child.tag,
                child.get("Handle", ""),
                child.get("DescriptorHandle", ""),
                etree.tostring(child, method="c14n"),
            ),
        )
    return {
        "mdib": etree.tostring(mdib_node, method="c14n"),
        "mdib_versions": (
            service.mdib.mdib_version,
            service.mdib.mdstate_version,
            service.mdib.mddescription_version,
        ),
        "version_lookups": (
            dict(service.mdib.descriptions.handle_version_lookup),
            dict(service.mdib.states.handle_version_lookup),
            dict(service.mdib.context_states.handle_version_lookup),
        ),
        "descriptors": descriptors,
        **snapshot(service),
        "alert_presence": {
            handle: (service.alert_present(handle), tuple(service.signal_states(handle)))
            for handle in service.list_alerts()
        },
        "actions": service.list_actions(),
        "sections": service.sections(),
        "operations": {
            handle: service.operation_handle_for(handle)
            for handle in service.list_metrics()
        },
        "registered_operations": set(service._sco._registered_operations),  # noqa: SLF001
        "operation_targets": {
            handle: (type(operation).__name__, operation.operation_target_handle)
            for handle, operation in service._sco._registered_operations.items()  # noqa: SLF001
        },
        "context_states": {
            state.Handle: (
                state.DescriptorHandle,
                str(state.ContextAssociation),
                state.StateVersion,
            )
            for state in service.mdib.context_states.objects
        },
        "location": service.get_location(),
        "patient": service.get_patient(),
        "provider_location": vars(service._provider._location).copy(),  # noqa: SLF001
        "mds_operating_mode": service.mdib.entities.by_handle(constants.MDS_HANDLE).state.OperatingMode,
        "mds_mode_before_demo": service._mds_mode_before_demo,  # noqa: SLF001
        "pending_alert_sources": set(service._pending_alert_sources),  # noqa: SLF001
        "waveform_phase": dict(service._sample_generator.phases),  # noqa: SLF001
        "pinned_samples": set(service._sample_generator.pinned),  # noqa: SLF001
        "quarantined_samples": set(service._sample_generator.quarantined),  # noqa: SLF001
        "waveform_clocks": dict(service._sample_generator.next_waveform_times),  # noqa: SLF001
        "sample_deadlines": dict(service._sample_generator.deadlines),  # noqa: SLF001
        "generator_running": service.generator_running,
    }


def semantic_mdib(mdib) -> bytes:  # noqa: ANN001 - provider and consumer MDIBs share this API
    """Canonical active graph without transport-maintained history/version attributes."""
    node = mdib.reconstruct_mdib_with_context_states()[0]
    for element in list(node.iter()):
        xsi_type = element.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
        if xsi_type.endswith("ContextState") and element.get("ContextAssociation") == "Dis":
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
    for element in node.iter():
        for name in list(element.attrib):
            if name.endswith("Version"):
                del element.attrib[name]
        xsi_type = element.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
        if xsi_type.endswith("ContextState"):
            for name in ("Handle", "BindingStartTime", "BindingEndTime"):
                element.attrib.pop(name, None)
        element.attrib.pop("SafetyClassification", None)
        if element.get("{http://www.w3.org/2001/XMLSchema-instance}type") == "dom:MdsState":
            element.attrib.pop("Lang", None)
            element.attrib.pop("OperatingMode", None)
    for element in reversed(list(node.iter())):
        element[:] = sorted(
            element,
            key=lambda child: (
                child.tag,
                child.get("Handle", ""),
                child.get("DescriptorHandle", ""),
                etree.tostring(child, method="c14n"),
            ),
        )
    return etree.tostring(node, method="c14n")


def mdib_versions(mdib) -> tuple[int, int, int]:  # noqa: ANN001 - provider and consumer MDIBs share this API
    return mdib.mdib_version, mdib.mdstate_version, mdib.mddescription_version


def wait_until(predicate, timeout: float = 10.0) -> bool:  # noqa: ANN001, ANN201 - test predicate
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def build_reference(service: ProviderService) -> None:
    """A device exercising every field emitted by config.to_dict()."""
    service.add_metric(
        MetricSpec(
            label="Zoom level",
            kind=MetricKind.NUMBER,
            unit_label="steps",
            unit_coding=Coding(code="zoom-steps", system="private", label="steps"),
            type_coding=Coding(code="zoom-level", system="urn:example:metrics", label="Zoom level"),
            section="Optics",
            resolution=Decimal("1"),
            minimum=Decimal("1"),
            maximum=Decimal("100"),
            controllable=True,
            initial_value=Decimal("7"),
        ),
    )
    service.add_metric(
        MetricSpec(
            label="Mode",
            kind=MetricKind.CHOICE,
            allowed_values=("IDLE", "RUN", "PAUSE"),
            controllable=True,
            initial_value="RUN",
        ),
    )
    service.add_metric(MetricSpec(label="Patient note", kind=MetricKind.TEXT, controllable=False))
    service.add_metric(
        MetricSpec(
            label="Pleth",
            kind=MetricKind.WAVEFORM,
            unit_label="%",
            minimum=Decimal("0"),
            maximum=Decimal("100"),
            sample_period=Decimal("0.05"),
            shape=WaveformShape.SQUARE,
            cycle_seconds=Decimal("1.85"),
        ),
    )
    service.add_metric(
        MetricSpec(
            label="Spectrum",
            kind=MetricKind.DISTRIBUTION,
            unit_label="dB",
            domain_unit_label="Hz",
            domain_unit_coding=Coding(code="frequency", system="urn:example:units", label="Hz"),
            domain_minimum=Decimal("0"),
            domain_maximum=Decimal("500"),
            distribution_shape=DistributionShape.SPECTRUM,
        ),
    )
    service.add_alert(
        AlertSpec(
            label="Zoom high",
            source_handle="m.zoom_level",
            kind=AlertKind.PHYSIOLOGICAL,
            priority=AlertPriority.HIGH,
            lower_limit=Decimal("2"),
            upper_limit=Decimal("90"),
            delegable=True,
            signals=(
                AlertSignalSpec(AlertManifestation.VIS, latching=True),
                AlertSignalSpec(AlertManifestation.TAN),
            ),
        ),
    )
    service.add_alert(AlertSpec(label="Service due", source_handle="m.zoom_level"))
    # Runtime states, edited after creation, survive the file too.
    service.set_allowed_range("m.zoom_level", Decimal("5"), Decimal("50"))
    service.set_allowed_values("m.mode", ["IDLE", "RUN"])
    service.set_metric_active("m.patient_note", active=False)
    service.set_alert_limits("al.zoom_high", Decimal("3"), Decimal("80"))
    service.set_alert_priority("al.service_due", AlertPriority.LOW)
    service.add_action(
        ActionSpec(
            label="Set inspection mode",
            target_handle="vmd.optics",
            effects={"m.zoom_level": Decimal("12"), "m.mode": "PAUSE"},
            type_coding=Coding(code="inspection-mode", system="urn:example:actions", label="Inspection mode"),
            note="Prepare optics for inspection",
        ),
    )
    service.set_location(
        LocationInfo(
            facility="HOSP",
            building="North",
            floor="3",
            point_of_care="OR1",
            room="Hybrid",
            bed="A",
        ),
    )
    service.set_patient(
        PatientInfo(
            given_name="Ada",
            family_name="Lovelace",
            sex="F",
            patient_type="Ad",
            date_of_birth="1815-12-10",
            height=PatientMeasurement(
                value=Decimal("170.5"),
                unit=Coding(code="demo-cm", system="private", label="cm"),
            ),
            weight=PatientMeasurement(
                value=Decimal("72.4"),
                unit=Coding(code="demo-kg", system="private", label="kg"),
            ),
            race=Coding(code="demo-race", system="urn:example:race", label="Demo race"),
        ),
    )


def check_preflight_preserves_device(report: Report, service: ProviderService) -> None:
    """Reference failures must be reported before replace=True removes live descriptors."""
    service.add_metric(MetricSpec(label="Existing", kind=MetricKind.NUMBER, initial_value=Decimal("7")))
    before = set(service.list_metrics())
    invalid = config.parse(
        {
            "metrics": [{"label": "New", "kind": "number"}],
            "actions": [{"label": "Broken", "target": "missing", "effects": {"m.new": "1"}}],
        },
    )
    try:
        config.apply_to(service, invalid)
    except config.ConfigError as exc:
        report.check("target" in str(exc), "an invalid action target is rejected before import", str(exc))
    else:
        report.check(False, "an invalid action target is rejected before import", "it was accepted")  # noqa: FBT003
    report.check(set(service.list_metrics()) == before, "a rejected import leaves existing metrics intact")

    missing_effect = config.parse(
        {
            "metrics": [{"label": "New", "kind": "number"}],
            "actions": [{"label": "Broken", "target": "mds0", "effects": {"m.missing": "1"}}],
        },
    )
    try:
        config.apply_to(service, missing_effect)
    except config.ConfigError as exc:
        report.check("effects" in str(exc), "a missing effect metric is rejected before import", str(exc))
    else:
        report.check(False, "a missing effect metric is rejected before import", "it was accepted")  # noqa: FBT003
    report.check(set(service.list_metrics()) == before, "a rejected effect import leaves existing metrics intact")
    empty_location = config.parse({"contexts": {"location": {}}})
    try:
        config.apply_to(service, empty_location)
    except config.ConfigError as exc:
        report.check("location" in str(exc), "an empty location is rejected before import", str(exc))
    else:
        report.check(False, "an empty location is rejected before import", "it was accepted")  # noqa: FBT003
    report.check(set(service.list_metrics()) == before, "a rejected location import leaves existing metrics intact")
    service.remove_metric("m.existing")


def check_section_handle_collisions(report: Report, service: ProviderService) -> None:
    """Section handles are reserved before any explicit profile descriptor is claimed."""
    service.add_metric(
        MetricSpec(label="Existing", kind=MetricKind.NUMBER, section="Existing section", initial_value=Decimal("7")),
    )
    for generated_handle in ("vmd.generated_section", "ch.generated_section"):
        for collision_first in (True, False):
            colliding = {"label": "Collision", "kind": "number", "handle": generated_handle}
            sectioned = {"label": "Sectioned", "kind": "number", "section": "Generated section"}
            before = complete_snapshot(service)
            try:
                profile = config.parse(
                    {"metrics": [colliding, sectioned] if collision_first else [sectioned, colliding]},
                )
                config.apply_to(service, profile)
            except config.ConfigError as exc:
                report.check(
                    generated_handle in str(exc),
                    f"{generated_handle} collision is rejected with the metric "
                    f"{'before' if collision_first else 'after'} the section",
                    str(exc),
                )
            else:
                report.check(False, f"{generated_handle} collision is rejected", "it was accepted")
            report.check(
                complete_snapshot(service) == before,
                f"rejected {generated_handle} collision does not mutate the MDIB or service",
            )

    before = complete_snapshot(service)
    try:
        config.parse({"metrics": [{"label": "Fixed collision", "kind": "text", "handle": "mds0"}]})
    except config.ConfigError as exc:
        report.check(
            "mds0" in str(exc) and "already exists" in str(exc),
            "an explicit metric handle cannot claim a fixed descriptor",
            str(exc),
        )
    else:
        report.check(False, "an explicit metric handle cannot claim a fixed descriptor", "it was accepted")
    report.check(
        complete_snapshot(service) == before,
        "a fixed descriptor collision does not mutate the MDIB or service",
    )
    service.remove_metric("m.existing")


def check_append_section_type_preflight(report: Report, service: ProviderService) -> None:
    """A late section collision is rejected before an earlier metric is appended."""
    colliding_handle = "vmd.append_collision"
    witness = service.mdib.entities.new_entity(
        pm.ChannelDescriptor,
        colliding_handle,
        "vmd0",
    )
    service._create_entities([witness])  # noqa: SLF001 - construct a live foreign descriptor
    profile = config.parse(
        {
            "metrics": [
                {"label": "First append", "kind": "number"},
                {"label": "Second append", "kind": "number", "section": "Append collision"},
            ],
        },
    )
    before = complete_snapshot(service)
    try:
        config.apply_to(service, profile, replace=False)
    except config.ConfigError as exc:
        report.check(
            colliding_handle in str(exc) and "VmdDescriptor" in str(exc),
            "append preflight rejects a generated VMD handle occupied by the wrong live type",
            str(exc),
        )
    else:
        report.check(False, "append preflight rejects a wrong-type generated VMD", "it was reused")
    report.check(
        complete_snapshot(service) == before
        and service.mdib.entities.by_handle("m.first_append") is None,
        "a second metric's section collision is rejected before the first metric mutates the provider",
    )
    with service.mdib.descriptor_transaction() as mgr:
        mgr.remove_entity(service.mdib.entities.by_handle(colliding_handle))


def check_operational_failure_rolls_back(report: Report, service: ProviderService) -> None:
    """A failure after one successful creation restores the complete live provider."""
    metric = service.add_metric(
        MetricSpec(
            label="Existing controlled",
            kind=MetricKind.NUMBER,
            section="Existing section",
            controllable=True,
            initial_value=Decimal("20"),
        ),
    )
    alert = service.add_alert(
        AlertSpec(
            label="Existing alert",
            source_handle=metric,
            upper_limit=Decimal("10"),
            delegable=True,
        ),
    )
    service.set_signal_delegated(service.signal_handles_for(alert)[0], delegated=True)
    service.add_action(ActionSpec(label="Existing action", target_handle=metric, effects={metric: Decimal("5")}))
    service.set_location(LocationInfo(facility="OLD", point_of_care="OR", bed="7"))
    service.set_patient(PatientInfo(given_name="Before", family_name="Import"))
    before = complete_snapshot(service)
    before_graph = semantic_mdib(service.mdib)

    replacement = config.parse(
        {
            "metrics": [
                {"label": "First replacement", "kind": "number", "section": "New section"},
                {"label": "Second replacement", "kind": "number"},
            ],
            "contexts": {
                "location": {"facility": "NEW", "bed": "1"},
                "patient": {"given_name": "After"},
            },
        },
    )
    original_set_patient = service.set_patient
    replacement_created = False

    def fail_after_patient(info: PatientInfo) -> None:
        nonlocal replacement_created
        replacement_created = service.mdib.entities.by_handle("m.first_replacement") is not None
        original_set_patient(info)
        raise RuntimeError("injected context creation failure")

    service.set_patient = fail_after_patient
    try:
        try:
            config.apply_to(service, replacement, replace=True)
        except config.ConfigError as exc:
            report.check(
                "contexts.patient" in str(exc) and "injected" in str(exc),
                "an operational import failure reports profile field context",
                str(exc),
            )
        else:
            report.check(False, "an operational import failure reports profile field context", "it was accepted")  # noqa: FBT003
    finally:
        service.set_patient = original_set_patient

    after = complete_snapshot(service)
    report.check(replacement_created, "fault injection runs after a replacement descriptor succeeds")
    versioned = {"mdib", "mdib_versions", "version_lookups", "descriptors", "context_states"}
    changed = [name for name in before if name not in versioned and before[name] != after[name]]
    versions_advance = all(new >= old for old, new in zip(before["mdib_versions"], after["mdib_versions"], strict=True))
    report.check(
        not changed and semantic_mdib(service.mdib) == before_graph,
        "a failed replacement restores the complete provider state",
        str(changed),
    )
    report.check(versions_advance, "provider rollback never rewinds MDIB version counters")


def check_profile_demo_mode_lifecycle(report: Report, service: ProviderService) -> None:
    """Replacement and compensation preserve the mode displaced by demo samples."""
    service._set_mds_operating_mode(pm_types.MdsOperatingMode.SERVICE)  # noqa: SLF001
    sample_profile = config.parse(
        {"metrics": [{"label": "Profile waveform", "kind": "waveform"}]},
    )
    scalar_profile = config.parse(
        {"metrics": [{"label": "Profile scalar", "kind": "number"}]},
    )
    config.apply_to(service, sample_profile)
    config.apply_to(service, scalar_profile)
    report.check(
        service.mdib.entities.by_handle(constants.MDS_HANDLE).state.OperatingMode
        is pm_types.MdsOperatingMode.SERVICE
        and service._mds_mode_before_demo is None,  # noqa: SLF001
        "replacing generated sample metrics with scalar metrics restores the prior MDS mode",
    )

    config.apply_to(service, sample_profile)
    before = complete_snapshot(service)
    before_graph = semantic_mdib(service.mdib)
    failing_profile = config.parse(
        {
            "metrics": [{"label": "Failed scalar", "kind": "number"}],
            "contexts": {"patient": {"given_name": "Failure"}},
        },
    )
    original_set_patient = service.set_patient

    def fail_patient(_info: PatientInfo) -> None:
        raise RuntimeError("injected demo mode rollback failure")

    service.set_patient = fail_patient
    try:
        try:
            config.apply_to(service, failing_profile)
        except config.ConfigError as exc:
            report.check(
                "injected demo mode rollback failure" in str(exc),
                "sample-to-scalar replacement failure reaches profile compensation",
                str(exc),
            )
        else:
            report.check(False, "sample-to-scalar replacement failure reaches profile compensation", "accepted")
    finally:
        service.set_patient = original_set_patient

    after = complete_snapshot(service)
    versioned = {"mdib", "mdib_versions", "version_lookups", "descriptors", "context_states"}
    changed = [name for name in before if name not in versioned and before[name] != after[name]]
    report.check(
        not changed
        and semantic_mdib(service.mdib) == before_graph
        and service.mdib.entities.by_handle(constants.MDS_HANDLE).state.OperatingMode
        is pm_types.MdsOperatingMode.DEMO
        and service._mds_mode_before_demo is pm_types.MdsOperatingMode.SERVICE,  # noqa: SLF001
        "rollback restores both Demo mode and the non-demo mode it displaced",
        str(changed),
    )
    service.remove_metric("m.profile_waveform")
    report.check(
        service.mdib.entities.by_handle(constants.MDS_HANDLE).state.OperatingMode
        is pm_types.MdsOperatingMode.SERVICE,
        "the rolled-back sample source still restores its original prior mode on removal",
    )


def check_connected_consumer_rollback(report: Report) -> None:  # noqa: PLR0915 - linear integration check
    """A subscribed consumer sees replacement and compensating rollback reports."""
    service = ProviderService(instance_name="config-live-rollback")
    service.start()
    remote = None
    try:
        metric = service.add_metric(
            MetricSpec(
                label="Original metric",
                kind=MetricKind.NUMBER,
                section="Original section",
                controllable=True,
                initial_value=Decimal("20"),
            ),
        )
        alert = service.add_alert(
            AlertSpec(label="High", source_handle=metric, upper_limit=Decimal("10"), delegable=True),
        )
        duplicate_alert = service.add_alert(AlertSpec(label="High", source_handle=metric))
        service.set_signal_delegated(service.signal_handles_for(alert)[0], delegated=True)
        service.add_action(ActionSpec(label="Original action", target_handle=metric, effects={metric: Decimal("5")}))
        service.set_location(LocationInfo(facility="OLD", point_of_care="OR", bed="7"))
        service.set_patient(PatientInfo(given_name="Before", family_name="Import"))

        consumer = SdcConsumer(
            provider_address=service._provider.get_xaddrs()[0],  # noqa: SLF001 - deterministic loopback fixture
            sdc_definitions=SdcV1Definitions,
            ssl_context_container=None,
        )
        consumer.start_all()
        consumer_mdib = ConsumerMdib(consumer)
        consumer_mdib.init_mdib()
        remote = RemoteDevice(consumer, consumer_mdib, service.epr.urn)

        original_graph = semantic_mdib(remote.mdib)
        original_versions = mdib_versions(remote.mdib)
        original_descriptor_versions = {
            handle: entity.descriptor.DescriptorVersion
            for handle, entity in remote.mdib.entities.items()
        }
        original_state_versions = {
            state.Handle if state.is_context_state else state.DescriptorHandle: state.StateVersion
            for state in [*remote.mdib.states.objects, *remote.mdib.context_states.objects]
        }
        observed_versions = [original_versions]
        provider_versions = [mdib_versions(service.mdib)]
        replacement_seen = threading.Event()
        description_reports = []

        def on_description(description_report) -> None:  # noqa: ANN001 - observable payload
            observed_versions.append(mdib_versions(remote.mdib))
            description_reports.append(
                tuple(
                    (
                        str(part.ModificationType),
                        part.ParentDescriptor,
                        tuple(descriptor.Handle for descriptor in part.Descriptor),
                        tuple(state.DescriptorHandle for state in part.State),
                    )
                    for part in description_report.ReportPart
                ),
            )

        def on_created(descriptors: dict) -> None:
            if "m.replacement_metric" in descriptors:
                replacement_seen.set()

        remote.bind(description_modifications=on_description, new_descriptors_by_handle=on_created)
        replacement = config.parse(
            {
                "metrics": [
                    {
                        "label": "Replacement metric",
                        "kind": "number",
                        "section": "Replacement section",
                        "controllable": True,
                        "initial_value": "3",
                    },
                ],
                "alerts": [
                    {
                        "handle": "al.high", "label": "High", "watches": "m.replacement_metric",
                        "upper_limit": "10", "delegable": True,
                    },
                    {"handle": "al.high.2", "label": "High", "watches": "m.replacement_metric"},
                ],
                "actions": [
                    {
                        "label": "Replacement action",
                        "target": "m.replacement_metric",
                        "effects": {"m.replacement_metric": "4"},
                    },
                ],
                "contexts": {
                    "location": {"facility": "NEW", "bed": "1"},
                    "patient": {"given_name": "After"},
                },
            },
        )
        original_set_patient = service.set_patient

        def fail_after_consumer_observes_replacement(info: PatientInfo) -> None:
            if not replacement_seen.wait(10):
                raise RuntimeError("consumer did not observe replacement descriptor")
            provider_versions.append(mdib_versions(service.mdib))
            original_set_patient(info)
            raise RuntimeError("injected failure after consumer observation")

        service.set_patient = fail_after_consumer_observes_replacement
        try:
            try:
                config.apply_to(service, replacement)
            except config.ConfigError as exc:
                report.check(
                    "injected failure after consumer observation" in str(exc),
                    "failure is injected only after the consumer observes the replacement",
                    str(exc),
                )
            else:
                report.check(False, "live replacement failure reaches the caller", "it was accepted")  # noqa: FBT003
        finally:
            service.set_patient = original_set_patient

        provider_versions.append(mdib_versions(service.mdib))
        complete = wait_until(lambda: semantic_mdib(remote.mdib) == original_graph)
        report.check(
            complete,
            "subscribed consumer completes the original descriptor and state graph",
            "consumer graph remained different" if not complete else "",
        )
        report.check(
            set(remote.metrics()) == {metric}
            and set(remote.alerts()) == {alert, duplicate_alert}
            and set(remote.actions()) == {"act.original_action"}
            and remote.patient().given_name == "Before",
            "consumer restores metrics, alerts, actions, and associated context",
        )
        consumer_versions = observed_versions + [mdib_versions(remote.mdib)]
        report.check(
            all(
                all(new_part >= old_part for old_part, new_part in zip(old, new, strict=True))
                for old, new in pairwise(consumer_versions)
            ),
            "consumer-observed MDIB version counters never decrease",
            str(consumer_versions),
        )
        report.check(
            all(
                all(new_part >= old_part for old_part, new_part in zip(old, new, strict=True))
                for old, new in pairwise(provider_versions)
            ),
            "provider MDIB version counters never decrease during rollback",
            str(provider_versions),
        )
        report.check(
            all(
                remote.mdib.entities.by_handle(handle).descriptor.DescriptorVersion >= version
                for handle, version in original_descriptor_versions.items()
            ),
            "restored descriptor versions do not rewind",
        )
        current_state_versions = {
            state.Handle if state.is_context_state else state.DescriptorHandle: state.StateVersion
            for state in [*remote.mdib.states.objects, *remote.mdib.context_states.objects]
        }
        report.check(
            all(current_state_versions[handle] >= version for handle, version in original_state_versions.items()),
            "restored state and context versions do not rewind",
        )
        report_parts = [part for description_report in description_reports for part in description_report]
        replacement_parts = [part for part in report_parts if "m.replacement_metric" in part[2]]
        report.check(
            replacement_parts
            and replacement_parts[0][1] == "ch.replacement_section"
            and replacement_parts[0][3] == ("m.replacement_metric",)
            and all(part[1] is None for part in report_parts if part[0] == "Del"),
            "wire description reports carry create parent/state and omit delete ParentDescriptor",
            repr(report_parts),
        )
    finally:
        if remote is not None:
            remote.close()
        service.stop()


def check_generator_start_rollback(report: Report) -> None:  # noqa: PLR0915 - linear integration check
    """A post-apply generator start failure compensates before import returns."""
    service = ProviderService(instance_name="config-generator-rollback")
    service.start()
    remote = None
    try:
        original = service.add_metric(
            MetricSpec(
                label="Original waveform",
                kind=MetricKind.WAVEFORM,
                section="Original section",
            ),
        )
        service.add_alert(AlertSpec(label="Original alert", source_handle=original))
        service.set_samples(original, [Decimal("1"), Decimal("2"), Decimal("3")])
        service.set_location(LocationInfo(facility="OLD", point_of_care="ICU", bed="8"))
        service.set_patient(PatientInfo(given_name="Before", family_name="Generator"))

        consumer = SdcConsumer(
            provider_address=service._provider.get_xaddrs()[0],  # noqa: SLF001 - deterministic loopback fixture
            sdc_definitions=SdcV1Definitions,
            ssl_context_container=None,
        )
        consumer.start_all()
        consumer_mdib = ConsumerMdib(consumer)
        consumer_mdib.init_mdib()
        remote = RemoteDevice(consumer, consumer_mdib, service.epr.urn)

        before = complete_snapshot(service)
        before_graph = semantic_mdib(service.mdib)
        consumer_graph = semantic_mdib(remote.mdib)
        original_descriptor_version = service.mdib.entities.by_handle(original).descriptor.DescriptorVersion
        original_state_version = service.mdib.entities.by_handle(original).state.StateVersion
        provider_versions = [mdib_versions(service.mdib)]
        consumer_versions = [mdib_versions(remote.mdib)]
        replacement_seen = threading.Event()

        def on_description(_report) -> None:  # noqa: ANN001 - observable payload
            consumer_versions.append(mdib_versions(remote.mdib))

        def on_created(descriptors: dict) -> None:
            if "m.replacement_waveform" in descriptors:
                replacement_seen.set()

        remote.bind(description_modifications=on_description, new_descriptors_by_handle=on_created)
        replacement = config.parse(
            {
                "metrics": [
                    {
                        "handle": "m.replacement_waveform",
                        "label": "Replacement waveform",
                        "kind": "waveform",
                        "section": "Replacement section",
                    },
                ],
                "contexts": {
                    "location": {"facility": "NEW", "bed": "1"},
                    "patient": {"given_name": "After"},
                },
            },
        )
        original_thread_start = threading.Thread.start
        failed_start = False

        def fail_first_generator_start(thread: threading.Thread) -> None:
            nonlocal failed_start
            if thread.name == "samples-config-generator-rollback" and not failed_start:
                failed_start = True
                if not replacement_seen.wait(10):
                    raise RuntimeError("consumer did not observe replacement waveform")
                provider_versions.append(mdib_versions(service.mdib))
                raise RuntimeError("forced generator thread start failure")
            original_thread_start(thread)

        with patch.object(threading.Thread, "start", fail_first_generator_start):
            try:
                config.apply_to(service, replacement)
            except config.ConfigError as exc:
                report.check(
                    "profile.generator" in str(exc) and "forced generator thread start failure" in str(exc),
                    "a replacement waveform's generator start failure reaches the caller",
                    str(exc),
                )
            else:
                report.check(False, "a replacement waveform's generator start failure reaches the caller")

        provider_versions.append(mdib_versions(service.mdib))
        consumer_complete = wait_until(lambda: semantic_mdib(remote.mdib) == consumer_graph)
        after = complete_snapshot(service)
        versioned = {"mdib", "mdib_versions", "version_lookups", "descriptors", "context_states"}
        changed = [name for name in before if name not in versioned and before[name] != after[name]]
        report.check(
            failed_start
            and not changed
            and semantic_mdib(service.mdib) == before_graph
            and service.get_location() == before["location"]
            and service.get_patient() == before["patient"]
            and service.generator_running,
            "start failure restores the old graph, contexts, location, and generator state",
            f"changed={changed}, graph={semantic_mdib(service.mdib) == before_graph}, "
            f"location={service.get_location() == before['location']}, "
            f"patient={service.get_patient() == before['patient']}, running={service.generator_running}",
        )
        report.check(
            all(
                all(new_part >= old_part for old_part, new_part in zip(old, new, strict=True))
                for old, new in pairwise(provider_versions)
            )
            and all(
                all(new_part >= old_part for old_part, new_part in zip(old, new, strict=True))
                for old, new in pairwise([*consumer_versions, mdib_versions(remote.mdib)])
            )
            and service.mdib.entities.by_handle(original).descriptor.DescriptorVersion
            >= original_descriptor_version
            and service.mdib.entities.by_handle(original).state.StateVersion >= original_state_version,
            "generator-start compensation preserves provider and consumer version monotonicity",
            f"provider={provider_versions}, consumer={consumer_versions}",
        )
        report.check(
            consumer_complete
            and set(remote.metrics()) == {original}
            and remote.patient().given_name == "Before",
            "the connected consumer converges on the original graph and context",
            f"complete={consumer_complete}, metrics={set(remote.metrics())}, patient={remote.patient().given_name!r}",
        )
    finally:
        if remote is not None:
            remote.close()
        service.stop()


def check_connected_consumer_append_rollback(report: Report) -> None:  # noqa: PLR0915 - linear integration check
    """A consumer-observed partial append is compensated without replacing originals."""
    service = ProviderService(instance_name="config-live-append-rollback")
    service.start()
    remote = None
    try:
        original = service.add_metric(
            MetricSpec(label="Append original", kind=MetricKind.NUMBER, initial_value=Decimal("9")),
        )
        service.set_location(LocationInfo(facility="OLD", bed="4"))
        service.set_patient(PatientInfo(given_name="Before", family_name="Append"))

        consumer = SdcConsumer(
            provider_address=service._provider.get_xaddrs()[0],  # noqa: SLF001 - deterministic loopback fixture
            sdc_definitions=SdcV1Definitions,
            ssl_context_container=None,
        )
        consumer.start_all()
        consumer_mdib = ConsumerMdib(consumer)
        consumer_mdib.init_mdib()
        remote = RemoteDevice(consumer, consumer_mdib, service.epr.urn)

        before = complete_snapshot(service)
        before_graph = semantic_mdib(service.mdib)
        consumer_graph = semantic_mdib(remote.mdib)
        original_descriptor_version = service.mdib.entities.by_handle(original).descriptor.DescriptorVersion
        provider_versions = [mdib_versions(service.mdib)]
        consumer_versions = [mdib_versions(remote.mdib)]
        first_seen = threading.Event()
        original_churn = []

        def on_description(_report) -> None:  # noqa: ANN001 - observable payload
            consumer_versions.append(mdib_versions(remote.mdib))

        def on_created(descriptors: dict) -> None:
            if "m.first_append" in descriptors:
                first_seen.set()
            if original in descriptors:
                original_churn.append("created")

        def on_deleted(descriptors: dict) -> None:
            if original in descriptors:
                original_churn.append("deleted")

        remote.bind(
            description_modifications=on_description,
            new_descriptors_by_handle=on_created,
            deleted_descriptors_by_handle=on_deleted,
        )
        profile = config.parse(
            {
                "metrics": [
                    {"label": "First append", "kind": "number", "initial_value": "1"},
                    {"label": "Second append", "kind": "number"},
                ],
                "contexts": {
                    "location": {"facility": "NEW", "bed": "1"},
                    "patient": {"given_name": "After"},
                },
            },
        )
        original_set_patient = service.set_patient
        first_seen_locally = False

        def fail_after_consumer_observes_append(info: PatientInfo) -> None:
            nonlocal first_seen_locally
            first_seen_locally = service.mdib.entities.by_handle("m.first_append") is not None
            if not first_seen.wait(10):
                raise RuntimeError("consumer did not observe first appended descriptor")
            provider_versions.append(mdib_versions(service.mdib))
            original_set_patient(info)
            raise RuntimeError("injected append failure after consumer observation")

        service.set_patient = fail_after_consumer_observes_append
        try:
            try:
                config.apply_to(service, profile, replace=False)
            except config.ConfigError as exc:
                report.check(
                    "injected append failure after consumer observation" in str(exc),
                    "append failure is injected after provider and consumer observe the first metric",
                    str(exc),
                )
            else:
                report.check(False, "partial append failure reaches the caller", "it was accepted")
        finally:
            service.set_patient = original_set_patient

        provider_versions.append(mdib_versions(service.mdib))
        consumer_complete = wait_until(lambda: semantic_mdib(remote.mdib) == consumer_graph)
        after = complete_snapshot(service)
        versioned = {"mdib", "mdib_versions", "version_lookups", "descriptors", "context_states"}
        changed = [name for name in before if name not in versioned and before[name] != after[name]]
        report.check(
            first_seen_locally
            and not changed
            and semantic_mdib(service.mdib) == before_graph
            and service.mdib.entities.by_handle("m.first_append") is None,
            "failed append restores complete provider state and removes the first metric",
            str(changed),
        )
        report.check(
            not original_churn
            and service.mdib.entities.by_handle(original).descriptor.DescriptorVersion
            == original_descriptor_version,
            "append compensation does not recreate an unchanged pre-existing descriptor",
            str(original_churn),
        )
        report.check(
            consumer_complete
            and set(remote.metrics()) == {original}
            and remote.mdib.entities.by_handle("m.first_append") is None,
            "subscribed consumer returns to the complete pre-append graph",
        )
        report.check(
            all(
                all(new_part >= old_part for old_part, new_part in zip(old, new, strict=True))
                for old, new in pairwise(provider_versions)
            )
            and all(
                all(new_part >= old_part for old_part, new_part in zip(old, new, strict=True))
                for old, new in pairwise([*consumer_versions, mdib_versions(remote.mdib)])
            ),
            "append rollback keeps provider and consumer MDIB versions monotonic",
            f"provider={provider_versions}, consumer={consumer_versions}",
        )
    finally:
        if remote is not None:
            remote.close()
        service.stop()


def check_profile_boundary_semantics(report: Report) -> None:  # noqa: PLR0915 - focused integration matrix
    """The provider boundary owns history, versions, reports, concurrency, and failure policy."""
    service = ProviderService(instance_name="config-boundary")
    service.start()
    try:
        transactions = []

        def capture_transaction(result) -> None:  # noqa: ANN001
            transactions.append(result)

        observableproperties.bind(service.mdib, transaction=capture_transaction)
        before = mdib_versions(service.mdib)
        profile = config.parse(
            {
                "metrics": [
                    {
                        "handle": "m.history",
                        "label": "History",
                        "kind": "number",
                        "section": "Versioned",
                        "initial_value": "4",
                    },
                ],
            },
        )
        config.apply_to(service, profile)
        after = mdib_versions(service.mdib)
        description_transactions = sum(
            bool(result.descr_created or result.descr_updated or result.descr_deleted)
            for result in transactions
        )
        state_transactions = sum(
            bool(
                result.descr_created
                or result.descr_updated
                or result.descr_deleted
                or result.metric_updates
                or result.alert_updates
                or result.comp_updates
                or result.ctxt_updates
                or result.op_updates
                or result.rt_updates
            )
            for result in transactions
        )
        report.check(
            after[0] - before[0] == len(transactions)
            and after[1] - before[1] == state_transactions
            and after[2] - before[2] == description_transactions,
            "profile transactions advance aggregate versions by their exact report counts",
            f"before={before}, after={after}, transactions={len(transactions)}",
        )

        created = [descriptor for result in transactions for descriptor in result.descr_created]
        created_handles = [descriptor.Handle for descriptor in created]
        report_states = [
            state
            for result in transactions
            for states in (
                result.metric_updates,
                result.alert_updates,
                result.comp_updates,
                result.op_updates,
                result.rt_updates,
            )
            for state in states
        ]
        report.check(
            created_handles.index("vmd.versioned") < created_handles.index("ch.versioned")
            < created_handles.index("m.history")
            and any(state.DescriptorHandle == "m.history" for state in report_states),
            "create reports are parent-before-child and include the created metric state",
            str(created_handles),
        )

        descriptor_version = service.mdib.entities.by_handle("m.history").descriptor.DescriptorVersion
        state_version = service.mdib.entities.by_handle("m.history").state.StateVersion
        before_rejected = mdib_versions(service.mdib)
        incompatible = config.parse(
            {"metrics": [{"handle": "m.history", "label": "History text", "kind": "text"}]},
        )
        try:
            config.apply_to(service, incompatible)
        except config.ConfigError as exc:
            report.check(
                "previously" in str(exc) and "m.history" in str(exc),
                "same-sequence handle reuse rejects a different XML datatype",
                str(exc),
            )
        else:
            report.check(False, "same-sequence handle reuse rejects a different XML datatype", "it was accepted")
        report.check(
            mdib_versions(service.mdib) == before_rejected
            and service.mdib.entities.by_handle("m.history").descriptor.DescriptorVersion == descriptor_version
            and service.mdib.entities.by_handle("m.history").state.StateVersion == state_version,
            "datatype rejection leaves aggregate and per-handle versions exact",
        )

        transactions.clear()
        replacement = config.parse(
            {"metrics": [{"handle": "m.other", "label": "Other", "kind": "number"}]},
        )
        config.apply_to(service, replacement)
        deleted = [descriptor for result in transactions for descriptor in result.descr_deleted]
        report.check(
            any(descriptor.Handle == "m.history" for descriptor in deleted)
            and all(descriptor.parent_handle is None for descriptor in deleted),
            "delete report parts omit ParentDescriptor",
        )
        historical_descriptor = service.mdib.descriptions.handle_version_lookup["m.history"]
        historical_state = service.mdib.states.handle_version_lookup["m.history"]
        config.apply_to(service, profile)
        reinserted = service.mdib.entities.by_handle("m.history")
        report.check(
            reinserted.descriptor.DescriptorVersion == historical_descriptor + 1
            and reinserted.state.StateVersion == historical_state + 2,
            "same-type reinsertion and its initial value advance exact per-handle maxima",
            f"descriptor={reinserted.descriptor.DescriptorVersion}, state={reinserted.state.StateVersion}",
        )

        service.set_patient(PatientInfo(given_name="Stable", family_name="Patient"))
        old_associated = service._associated_context_state(constants.PATIENT_CONTEXT_HANDLE)  # noqa: SLF001
        failed_handle = [None]
        original_set_patient = service.set_patient

        def fail_context(info: PatientInfo) -> None:
            original_set_patient(info)
            failed_handle[0] = service._associated_context_state(constants.PATIENT_CONTEXT_HANDLE).Handle  # noqa: SLF001
            raise RuntimeError("injected context rollback probe")

        service.set_patient = fail_context
        try:
            context_profile = config.parse({"contexts": {"patient": {"given_name": "Transient"}}})
            try:
                config.apply_to(service, context_profile, replace=False)
            except config.ConfigError:
                pass
        finally:
            service.set_patient = original_set_patient
        current = service._associated_context_state(constants.PATIENT_CONTEXT_HANDLE)  # noqa: SLF001
        failed_state = service.mdib.context_states.handle.get_one(failed_handle[0])
        report.check(
            current is not None
            and current.Handle not in {old_associated.Handle, failed_handle[0]}
            and service.get_patient().given_name == "Stable"
            and failed_state.ContextAssociation.name == "DISASSOCIATED",
            "context compensation retains and disassociates the failed association then creates a new valid one",
        )

        action_metric = service.add_metric(
            MetricSpec(label="Concurrent action metric", kind=MetricKind.NUMBER, initial_value=Decimal("1")),
        )
        action = service.add_action(
            ActionSpec(label="Concurrent action", target_handle=action_metric, effects={action_metric: Decimal("2")}),
        )
        sample_metric = service.add_metric(
            MetricSpec(label="Concurrent waveform", kind=MetricKind.WAVEFORM),
        )
        entered = threading.Event()
        release = threading.Event()
        mutation_done = threading.Event()
        action_done = threading.Event()
        sample_done = threading.Event()
        original_add_metric = service.add_metric

        def blocked_add(spec: MetricSpec) -> str:
            if spec.handle == "m.gated":
                entered.set()
                release.wait(10)
            return original_add_metric(spec)

        service.add_metric = blocked_add
        gated = config.parse({"metrics": [{"handle": "m.gated", "label": "Gated", "kind": "number"}]})
        import_thread = threading.Thread(
            target=lambda: config.apply_to(service, gated, replace=False),
            daemon=True,
        )
        import_thread.start()
        entered.wait(10)

        def concurrent_mutation() -> None:
            service.set_location(LocationInfo(facility="AFTER-GATE"))
            mutation_done.set()

        mutation_thread = threading.Thread(target=concurrent_mutation, daemon=True)
        action_thread = threading.Thread(
            target=lambda: (service.run_action(action), action_done.set()),
            daemon=True,
        )
        sample_thread = threading.Thread(
            target=lambda: (service.set_samples(sample_metric, [Decimal("3")]), sample_done.set()),
            daemon=True,
        )
        mutation_thread.start()
        action_thread.start()
        sample_thread.start()
        time.sleep(0.1)
        blocked = not mutation_done.is_set() and not action_done.is_set() and not sample_done.is_set()
        release.set()
        import_thread.join(10)
        mutation_thread.join(10)
        action_thread.join(10)
        sample_thread.join(10)
        service.add_metric = original_add_metric
        report.check(
            blocked
            and mutation_done.is_set()
            and action_done.is_set()
            and sample_done.is_set()
            and service.get_location().facility == "AFTER-GATE"
            and service.get_value(action_metric) == Decimal("2")
            and service.get_samples(sample_metric) == [Decimal("3")],
            "one import gate serializes planning through compensation against action, sample, and context updates",
        )
    finally:
        observableproperties.unbind(service.mdib, transaction=capture_transaction)
        service.stop()

    failed = ProviderService(instance_name="config-fail-closed")
    failed.start()
    try:
        failed.add_metric(MetricSpec(label="Existing", kind=MetricKind.NUMBER))
        profile = config.parse({"metrics": [{"label": "Replacement", "kind": "number"}]})
        with (
            patch.object(failed, "add_metric", side_effect=RuntimeError("injected apply failure")),
            patch.object(failed, "_restore_configuration", side_effect=RuntimeError("injected rollback failure")),
        ):
            try:
                config.apply_to(failed, profile)
            except config.ConfigError as exc:
                report.check(
                    "rollback failed" in str(exc) and "provider stopped" in str(exc),
                    "rollback failure is reported and fails the provider closed",
                    str(exc),
                )
            else:
                report.check(False, "rollback failure is reported and fails the provider closed", "it was accepted")
        report.check(
            failed._provider is None and failed._mdib is None,  # noqa: SLF001
            "a provider with failed compensation is no longer advertised or usable",
        )
    finally:
        if failed._mdib is not None:  # noqa: SLF001
            failed.stop()


def check_replace_section_lifecycle(report: Report, service: ProviderService) -> None:
    old = config.parse(
        {
            "metrics": [
                {"label": "Old first", "kind": "number", "section": "Old section"},
                {"label": "Old second", "kind": "number", "section": "Old section"},
            ],
            "actions": [{"label": "Old action", "target": "vmd.old_section"}],
        },
    )
    config.apply_to(service, old)
    replacement = config.parse(
        {"metrics": [{"label": "New metric", "kind": "number", "section": "New section"}]},
    )
    config.apply_to(service, replacement)

    old_handles = ("m.old_first", "m.old_second", "ch.old_section", "vmd.old_section", "act.old_action")
    report.check(
        all(service.mdib.entities.by_handle(handle) is None for handle in old_handles),
        "replacement removes every descriptor from an unrelated old section",
        str([handle for handle in old_handles if service.mdib.entities.by_handle(handle) is not None]),
    )
    report.check(
        service.sections() == {"New section": "ch.new_section"}
        and set(service.list_metrics()) == {"m.new_metric"}
        and not service.list_actions(),
        "replacement leaves only new section bookkeeping",
    )
    mdib_xml = etree.tostring(service.mdib.reconstruct_mdib_with_context_states()[0])
    report.check(
        all(handle.encode() not in mdib_xml for handle in old_handles),
        "old section descriptors are absent from the replacement MDIB",
    )

    before = complete_snapshot(service)
    stale_target = config.parse(
        {
            "metrics": [{"label": "Later metric", "kind": "number", "section": "Later section"}],
            "actions": [{"label": "Stale target", "target": "vmd.new_section"}],
        },
    )
    try:
        config.apply_to(service, stale_target)
    except config.ConfigError as exc:
        report.check(
            "target" in str(exc) and "not available after import" in str(exc),
            "an action cannot target a section removed by its replacement",
            str(exc),
        )
    else:
        report.check(False, "an action cannot target a section removed by its replacement", "it was accepted")  # noqa: FBT003
    after = complete_snapshot(service)
    report.check(
        after == before,
        "stale section target rejection preserves MDIB and bookkeeping",
        str([name for name in before if before[name] != after[name]]),
    )


def check_action_effect_types(report: Report) -> None:
    device = config.parse(
        {
            "metrics": [
                {"handle": "m.number", "label": "Number", "kind": "number", "minimum": "1", "maximum": "9"},
                {"handle": "m.text", "label": "Text", "kind": "text"},
                {"handle": "m.choice", "label": "Choice", "kind": "choice", "allowed_values": ["7", "RUN"]},
            ],
            "actions": [
                {
                    "label": "Mixed",
                    "target": "mds0",
                    "effects": {"m.number": "7", "m.text": "007", "m.choice": "7"},
                },
            ],
        },
    )
    effects = device.actions[0].effects
    report.check(
        effects == {"m.number": Decimal("7"), "m.text": "007", "m.choice": "7"},
        "action effect syntax is resolved against each target metric kind",
        repr(effects),
    )

    try:
        config.parse(
            {
                "metrics": [
                    {"handle": "m.number", "label": "Number", "kind": "number", "minimum": "1", "maximum": "9"},
                ],
                "actions": [
                    {"label": "Invalid", "target": "mds0", "effects": {"m.number": "10"}},
                ],
            },
        )
    except config.ConfigError as exc:
        report.check("maximum" in str(exc), "an out-of-range config effect is rejected", str(exc))
    else:
        report.check(False, "an out-of-range config effect is rejected", "it was accepted")  # noqa: FBT003


def check_metric_handle_resolution(report: Report, service: ProviderService) -> None:
    existing = service.add_metric(
        MetricSpec(label="Existing", kind=MetricKind.NUMBER, initial_value=Decimal("7")),
    )
    metric_entries = {
        MetricKind.NUMBER: {"label": "Duplicate", "kind": "number"},
        MetricKind.TEXT: {"label": "Duplicate", "kind": "text"},
        MetricKind.CHOICE: {
            "label": "Duplicate",
            "kind": "choice",
            "allowed_values": ["001", "RUN"],
        },
    }
    for first, second in permutations(metric_entries, 2):
        before = complete_snapshot(service)
        try:
            profile = config.parse(
                {
                    "metrics": [metric_entries[first], metric_entries[second]],
                    "alerts": [{"label": "Duplicate source", "watches": "m.duplicate"}],
                    "actions": [
                        {
                            "label": "Duplicate effect",
                            "target": "m.duplicate",
                            "effects": {"m.duplicate": "001"},
                        },
                    ],
                },
            )
            config.apply_to(service, profile)
        except config.ConfigError as exc:
            report.check(
                "implicit handle 'm.duplicate'" in str(exc) and "explicit unique handles" in str(exc),
                f"duplicate implicit {first.value}/{second.value} handles require explicit unique handles",
                str(exc),
            )
        else:
            report.check(
                False,
                f"duplicate implicit {first.value}/{second.value} handles are rejected",
                "they were accepted",
            )
        report.check(
            complete_snapshot(service) == before,
            f"rejected implicit {first.value}/{second.value} references do not mutate the provider",
        )

    explicit = config.parse(
        {
            "metrics": [
                {"handle": "m.duplicate.number", "label": "Duplicate", "kind": "number"},
                {"handle": "m.duplicate.text", "label": "Duplicate", "kind": "text"},
                {
                    "handle": "m.duplicate.choice",
                    "label": "Duplicate",
                    "kind": "choice",
                    "allowed_values": ["001", "RUN"],
                },
            ],
            "alerts": [
                {"label": "Number source", "watches": "m.duplicate.number"},
                {"label": "Text source", "watches": "m.duplicate.text"},
                {"label": "Choice source", "watches": "m.duplicate.choice"},
            ],
            "actions": [
                {
                    "label": "Set duplicate number",
                    "target": "m.duplicate.number",
                    "effects": {"m.duplicate.number": "001"},
                },
                {
                    "label": "Set duplicate text",
                    "target": "m.duplicate.text",
                    "effects": {"m.duplicate.text": "001"},
                },
                {
                    "label": "Set duplicate choice",
                    "target": "m.duplicate.choice",
                    "effects": {"m.duplicate.choice": "001"},
                },
            ],
        },
    )
    expected_handles = ["m.duplicate.number", "m.duplicate.text", "m.duplicate.choice"]
    expected_effects = [
        {"m.duplicate.number": Decimal("1")},
        {"m.duplicate.text": "001"},
        {"m.duplicate.choice": "001"},
    ]
    report.check(
        [spec.handle for spec in explicit.metrics] == expected_handles,
        "explicit metric handles preserve declaration order for duplicate labels",
    )
    report.check(
        [action.effects for action in explicit.actions] == expected_effects,
        "duplicate-label action effects use the exact referenced metric kind",
        repr([action.effects for action in explicit.actions]),
    )
    config.apply_to(service, explicit)
    report.check(
        list(service.list_metrics()) == expected_handles
        and [alert.source_handle for alert in service.list_alerts().values()] == expected_handles
        and [action.target_handle for action in service.list_actions().values()] == expected_handles
        and [action.effects for action in service.list_actions().values()] == expected_effects,
        "preflight and creation use the parsed metric handles for every reference",
    )
    round_tripped = config.parse(config.to_dict(service))
    report.check(
        set(spec.handle for spec in round_tripped.metrics) == set(expected_handles)
        and {action.label: action.effects for action in round_tripped.actions}
        == {action.label: action.effects for action in explicit.actions},
        "explicit duplicate-label handles and typed effects round-trip",
    )
    report.check(existing not in service.list_metrics(), "successful replacement removes the prior metric")


def check_atomic_save(report: Report, service: ProviderService, workdir: Path) -> None:
    """A partial ENOSPC write or failed replacement must not damage the old profile."""
    target = workdir / "atomic.sdcprofile.json"
    old_bytes = b'{"version": 1, "name": "previous"}\n'
    target.write_bytes(old_bytes)
    before_files = set(workdir.iterdir())
    real_open = io.open
    written_paths = []

    class FullDiskFile:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.stream.__exit__(*exc)

        def write(self, text):
            written_paths.append(Path(self.name))
            self.stream.write(text[:12])
            self.stream.flush()
            raise OSError(errno.ENOSPC, "injected disk full after JSON prefix")

    with patch.object(io, "open", lambda *args, **kwargs: FullDiskFile(real_open(*args, **kwargs))):
        try:
            config.save(service, target)
        except OSError as exc:
            report.check(exc.errno == errno.ENOSPC, "partial save exposes ENOSPC")
        else:
            report.check(False, "partial save exposes ENOSPC")
    report.check(target.read_bytes() == old_bytes, "a partial save preserves the old target bytes")
    report.check(set(workdir.iterdir()) == before_files, "a failed partial save removes its temporary output")
    report.check(
        bool(written_paths) and all(path != target and path.parent == target.parent for path in written_paths),
        "profile output is staged in the target directory, never in the target itself",
        str(written_paths),
    )

    target.write_bytes(old_bytes)
    with patch.object(Path, "replace", side_effect=PermissionError("injected replace failure")):
        try:
            config.save(service, target)
        except PermissionError:
            rejected = True
        else:
            rejected = False
    report.check(
        rejected and target.read_bytes() == old_bytes and set(workdir.iterdir()) == before_files,
        "a failed atomic replacement preserves the old profile and cleans its temporary file",
    )
    result = config.save(service, target)
    report.check(
        result == target and json.loads(target.read_text(encoding="utf-8")) == config.to_dict(service)
        and set(workdir.iterdir()) == before_files,
        "a successful atomic save returns the requested Path and valid JSON without temporary files",
    )


def check_xml_profile_preflight(report: Report, service: ProviderService) -> None:
    """Parsing and mutable-spec preflight reject invalid effects before replacing live state."""
    broken = "broken\x01"
    service.add_metric(MetricSpec(label="XML existing", kind=MetricKind.TEXT, initial_value="old"))
    original = config.parse(config.to_dict(service))
    payload = {
        "metrics": [
            {"handle": "m.xml_import_number", "label": "XML import number", "kind": "number", "initial_value": "2"},
            {"handle": "m.xml_import_text", "label": "XML import text", "kind": "text", "initial_value": "old"},
        ],
        "actions": [{
            "label": "XML import", "target": "m.xml_import_number",
            "effects": {"m.xml_import_number": "3", "m.xml_import_text": broken},
        }],
    }
    before = complete_snapshot(service)
    try:
        config.parse(payload)
    except config.ConfigError as exc:
        rejected = "XML" in str(exc)
    else:
        rejected = False
    report.check(
        rejected and complete_snapshot(service) == before,
        "parsing an XML-invalid action effect rejects it without provider mutation",
    )

    payload["actions"][0]["effects"]["m.xml_import_text"] = "valid"
    profile = config.parse(payload)
    profile.actions[0].effects["m.xml_import_text"] = broken
    try:
        config.apply_to(service, profile)
    except config.ConfigError as exc:
        rejected = "XML" in str(exc)
    else:
        rejected = False
    report.check(
        rejected and complete_snapshot(service) == before
        and profile.actions[0].target_handle == "m.xml_import_number",
        "mutable action preflight rejects XML before replacing descriptors or normalizing the action target",
    )
    if complete_snapshot(service) != before:
        config.apply_to(service, original)

    append = config.parse({"actions": [{
        "label": "XML append", "target": "m.xml_existing", "effects": {"m.xml_existing": broken},
    }]})
    before = complete_snapshot(service)
    try:
        config.apply_to(service, append, replace=False)
    except config.ConfigError as exc:
        rejected = "XML" in str(exc)
    else:
        rejected = False
    report.check(
        rejected and complete_snapshot(service) == before,
        "append preflight resolves existing text targets and rejects XML without storing an action",
    )
    service.remove_action("act.xml_append")
    for kind, field, value in (
        (MetricKind.TEXT, "initial_value", broken),
        *[(MetricKind.NUMBER, field, Decimal("1E-19")) for field in ("initial_value", "resolution", "minimum", "maximum")],
    ):
        profile = config.parse(payload)
        profile.actions.clear()
        spec = next(spec for spec in profile.metrics if spec.kind is kind)
        setattr(spec, field, value)
        before = complete_snapshot(service)
        try:
            config.apply_to(service, profile)
        except config.ConfigError:
            rejected = True
        else:
            rejected = False
        report.check(
            rejected and complete_snapshot(service) == before,
            f"a mutated {kind.value} {field} is rejected before profile replacement changes state or versions",
        )
        if complete_snapshot(service) != before:
            config.apply_to(service, original)
    service.remove_metric("m.xml_existing")


def check_rejected_unchanged(
    report: Report, service: ProviderService, operation, description: str, expected_text: str = "",
) -> None:
    """A validation failure must precede even a compensating/versioned write."""
    before = complete_snapshot(service)
    try:
        operation()
    except (config.ConfigError, TypeError, ValueError, RuntimeError) as exc:
        rejected = True
        detail = str(exc)
    else:
        rejected = False
        detail = "accepted"
    report.check(rejected and expected_text in detail, f"{description} is rejected", detail)
    try:
        after = complete_snapshot(service)
    except (TypeError, ValueError, RuntimeError) as exc:
        after = {}
        detail = f"snapshot is no longer readable: {exc}"
    else:
        detail = str([name for name in before if before[name] != after[name]])
    report.require(after == before, f"{description} leaves the complete provider and wire state unchanged", detail)


def check_location_xml_validation(report: Report, service: ProviderService) -> None:
    """Invalid location details cannot disassociate the current context or poison MDIB XML."""
    service.stop_generator()
    original = service.get_location()
    fields = ("facility", "building", "floor", "point_of_care", "room", "bed")
    for field in fields:
        payload = {"contexts": {"location": {field: "Bad\x01facility"}}}
        check_rejected_unchanged(
            report, service, lambda: config.parse(json.loads(json.dumps(payload))),
            f"JSON location {field} with XML-invalid text", "XML",
        )
        check_rejected_unchanged(
            report, service,
            lambda: service.set_location(LocationInfo(**{field: "Bad\x01facility"})),
            f"direct location {field} with XML-invalid text", "XML",
        )
        invalid = LocationInfo(**{field: "Bad\x01facility"})
        for replace in (True, False):
            profile = config.DeviceConfig(
                metrics=[MetricSpec(label="Location first", kind=MetricKind.NUMBER, handle="m.location_first")],
                location=invalid,
            )
            check_rejected_unchanged(
                report, service, lambda: config.apply_to(service, profile, replace=replace),
                f"{'replace' if replace else 'append'} invalid location {field}", "XML",
            )

    for field in fields:
        check_rejected_unchanged(
            report, service, lambda: service.set_location(LocationInfo(**{field: 1})),
            f"non-string location {field}",
        )
    permitted = LocationInfo(**{field: "Klinik München 東京\t\n\r" for field in fields})
    try:
        service.set_location(permitted)
        node = service.mdib.reconstruct_mdib_with_context_states()[0]
        details = node.xpath(
            "//*[@DescriptorHandle=$handle and @ContextAssociation='Assoc']/*[local-name()='LocationDetail']",
            handle=constants.LOCATION_CONTEXT_HANDLE,
        )
        report.check(
            service.get_location() == permitted and len(details) == 1
            and dict(details[0].attrib) == {
                name: "Klinik München 東京\t\n\r" for name in ("Facility", "Building", "Floor", "PoC", "Room", "Bed")
            },
            "all location fields retain ordinary Unicode and XML-permitted whitespace on the wire",
        )
        report.check(
            config.parse(config.to_dict(service)).location == permitted,
            "permitted location text survives export and JSON/profile parsing",
        )
        config.apply_to(service, config.parse({}), replace=False)
        report.check(service.get_location() == permitted, "an omitted location preserves the current association")
        check_rejected_unchanged(
            report, service, lambda: config.apply_to(service, config.parse({"contexts": {"location": {}}})),
            "an explicit empty location",
        )
    finally:
        service.set_location(original)


def check_control_capability_roundtrip(report: Report, service: ProviderService) -> None:
    """A late-created set operation is profile capability, even while disabled."""
    handles = []
    try:
        for kind, initial, requested in (
            (MetricKind.NUMBER, Decimal("1"), Decimal("2")),
            (MetricKind.TEXT, "before", "after"),
        ):
            handle = service.add_metric(MetricSpec(
                label=f"Late control {kind.value}", kind=kind, initial_value=initial,
            ))
            handles.append(handle)
            report.check(service.operation_handle_for(handle) is None, f"{kind.value} starts without a set operation")
            service.enable_control(handle)
            for disabled in (False, True):
                if disabled:
                    service.disable_control(handle)
                payload = config.to_dict(service)
                entry = next(item for item in payload["metrics"] if item["handle"] == handle)
                report.check(entry["controllable"] is True, f"{'disabled' if disabled else 'enabled'} late {kind.value} control exports capability")
                config.apply_to(service, config.parse(json.loads(json.dumps(payload))))
                operation_handle = service.operation_handle_for(handle)
                operation = service._sco._registered_operations.get(operation_handle)  # noqa: SLF001
                report.check(
                    operation is not None and operation.operation_target_handle == handle,
                    f"{'disabled' if disabled else 'enabled'} late {kind.value} control reloads a real registered set operation",
                )
                if operation is not None:
                    params = SimpleNamespace(
                        operation_instance=operation, operation_request=SimpleNamespace(argument=requested),
                    )
                    result = service._handler(params)  # noqa: SLF001 - exercise the restored registered boundary
                    report.check(
                        result.invocation_state is msg_types.InvocationState.FINISHED
                        and service.get_value(handle) == requested,
                        f"reloaded {kind.value} capability accepts a real set (OperatingMode is not persisted)",
                    )
                else:
                    service.enable_control(handle)
    finally:
        for handle in handles:
            service.remove_metric(handle)


def check_descriptor_text_preflight(report: Report, service: ProviderService) -> None:
    """Descriptor text must validate even with explicit codings and mutable parsed specs."""
    service.stop_generator()
    witness = service.add_metric(MetricSpec(label="Descriptor witness", kind=MetricKind.NUMBER))
    payload = {
        "metrics": [
            {"label": "Text first", "kind": "number", "section": "Text section"},
            {
                "label": "Text second", "kind": "choice", "allowed_values": ["A", "B"],
                "type": {"code": "type", "label": "Type"}, "unit": {"code": "unit", "label": "Unit"},
            },
            {"label": "Text domain", "kind": "distribution", "domain_unit": {"code": "domain", "label": "Domain"}},
        ],
        "alerts": [{"label": "Text alert", "watches": "m.text_first"}],
        "actions": [{"label": "Text action", "target": "m.text_first"}],
    }
    cases = (
        ("metrics", 1, "label", "label"),
        ("metrics", 1, "unit", "unit_label"),
        ("metrics", 1, "allowed_values", "allowed_values"),
        ("metrics", 1, "section", "section"),
        ("metrics", 1, "handle", "handle"),
        ("metrics", 2, "domain_unit", "domain_unit_label"),
        ("alerts", 0, "label", "label"),
        ("alerts", 0, "handle", "handle"),
        ("actions", 0, "label", "label"),
        ("actions", 0, "handle", "handle"),
        ("actions", 0, "note", "note"),
    )
    for group, index, key, field in cases:
        broken = json.loads(json.dumps(payload))
        broken[group][index][key] = ["A", "Bad\x01text"] if key == "allowed_values" else "Bad\x01text"
        check_rejected_unchanged(
            report, service, lambda: config.parse(broken), f"JSON {group} {field} with XML-invalid text", "XML",
        )
        for replace in (True, False):
            profile = config.parse(payload)
            spec = getattr(profile, group)[index]
            setattr(spec, field, ("A", "Bad\x01text") if field == "allowed_values" else "Bad\x01text")
            check_rejected_unchanged(
                report, service, lambda: config.apply_to(service, profile, replace=replace),
                f"{'replace' if replace else 'append'} mutated {group} {field}", "XML",
            )
        if group == "alerts":
            spec.source_handle = witness
        elif group == "actions":
            spec.target_handle = constants.MDS_HANDLE
        check_rejected_unchanged(
            report, service,
            lambda: getattr(service, {"metrics": "add_metric", "alerts": "add_alert", "actions": "add_action"}[group])(spec),
            f"direct mutated {group} {field}", "XML",
        )
    service.remove_metric(witness)


def check_mutated_waveform_preflight(report: Report, service: ProviderService) -> None:
    """Import repeats the geometry checks before removals or an earlier valid addition."""
    witness = service.add_metric(MetricSpec(label="Preflight witness", kind=MetricKind.WAVEFORM))
    service.stop_generator()
    service._publish_one_block()  # noqa: SLF001 - deterministic samples and generator bookkeeping
    try:
        cases = (
            *[("sample_period", value) for value in (None, Decimal("0"), Decimal("-0.1"), Decimal("NaN"), Decimal("1E-100"), 0.1)],
            *[("cycle_seconds", value) for value in (None, 40, True, Decimal("0.01"))],
        )
        for replace in (True, False):
            for field, invalid in cases:
                profile = config.parse({"metrics": [
                    {"label": "Geometry first", "kind": "number", "initial_value": "2", "section": "Geometry new section"},
                    {"label": "Geometry second", "kind": "waveform"},
                ]})
                setattr(profile.metrics[1], field, invalid)
                check_rejected_unchanged(
                    report, service, lambda: config.apply_to(service, profile, replace=replace),
                    f"{'replace' if replace else 'append'} waveform {field}={invalid!r}", field,
                )
    finally:
        service.remove_metric(witness)


def check_cycle_seconds(report: Report) -> None:
    """A cycle is a duration; old profiles that counted samples keep their speed."""
    legacy = config.parse({"metrics": [
        {"label": "Old wave", "kind": "waveform", "sample_period": "0.02", "cycle_samples": 50},
        {"label": "Old default", "kind": "waveform", "cycle_samples": 30},
    ]})
    report.check([spec.cycle_seconds for spec in legacy.metrics] == [Decimal("1.00"), Decimal("3.0")],
                 "cycle_samples from an older profile becomes the same duration",
                 str([spec.cycle_seconds for spec in legacy.metrics]))
    entry = config.metric_to_dict("m.old_wave", legacy.metrics[0])
    report.check(entry.get("cycle_seconds") == "1.00" and "cycle_samples" not in entry,
                 "profiles are written with cycle_seconds", str(entry))
    try:
        config.parse({"metrics": [{"label": "Both", "kind": "waveform", "cycle_samples": 50, "cycle_seconds": "1"}]})
        both = False
    except config.ConfigError:
        both = True
    report.check(both, "a profile may not give the cycle both ways")


def check_derived_step_preflight(report: Report, service: ProviderService) -> None:
    """Representable domain endpoints do not make a lossy calculated step safe to import."""
    existing = service.add_metric(
        MetricSpec(label="Step preflight original", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal("7")),
    )
    try:
        for replace in (True, False):
            mode = "replace" if replace else "append"
            profile = config.parse({"metrics": [
                {"label": "Step preflight first", "kind": "number", "initial_value": "2"},
                {
                    "label": "Tiny distribution", "kind": "distribution",
                    "domain_minimum": "0", "domain_maximum": "1E-17",
                },
            ]})
            before = complete_snapshot(service)
            try:
                config.apply_to(service, profile, replace=replace)
            except config.ConfigError as exc:
                rejected = "distribution StepWidth" in str(exc) and "losing precision" in str(exc)
            else:
                rejected = False
            report.check(rejected, f"{mode} rejects a lossy calculated distribution step")
            after = complete_snapshot(service)
            report.check(
                after == before,
                f"{mode} rejects the derived step before the preceding valid metric changes any state or versions",
                f"versions {before['mdib_versions']} -> {after['mdib_versions']}; "
                f"changed {[name for name in before if before[name] != after[name]]}",
            )
    finally:
        service.remove_metric(existing)


def check_derived_sample_range_preflight(report: Report, service: ProviderService) -> None:
    """One-sided wire-safe endpoints can derive a lossy bound; reject before any live writes."""
    existing = service.add_metric(
        MetricSpec(label="Range preflight original", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal("7")),
    )
    try:
        for replace in (True, False):
            mode = "replace" if replace else "append"
            for kind in (MetricKind.WAVEFORM, MetricKind.DISTRIBUTION):
                for field, initial, derived_field in (("maximum", "100", "minimum"), ("minimum", "0", "maximum")):
                    profile = config.parse({"metrics": [
                        {"label": "Range preflight first", "kind": "number", "initial_value": "2"},
                        {"label": "One-sided sample", "kind": kind.value, field: initial},
                    ]})
                    setattr(profile.metrics[1], field, Decimal("1E-17"))
                    before = complete_snapshot(service)
                    try:
                        config.apply_to(service, profile, replace=replace)
                    except config.ConfigError as exc:
                        rejected = f"{derived_field} used for sample generation" in str(exc) and "losing precision" in str(exc)
                    else:
                        rejected = False
                    case = f"{mode} {kind.value} with only {field}"
                    report.check(rejected, f"{case} rejects a lossy derived {derived_field}")
                    after = complete_snapshot(service)
                    report.check(
                        after == before,
                        f"{case} rejects the derived sample range before the preceding valid metric changes state or versions",
                        f"versions {before['mdib_versions']} -> {after['mdib_versions']}; "
                        f"changed {[name for name in before if before[name] != after[name]]}",
                    )
    finally:
        service.remove_metric(existing)


def check_mutable_alert_limit_preflight(report: Report, service: ProviderService) -> None:
    """Mutable alarm limits must fail preflight, not trigger live import compensation."""
    existing = service.add_metric(
        MetricSpec(label="Alert preflight original", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal("7")),
    )
    try:
        for replace in (True, False):
            mode = "replace" if replace else "append"
            for field in ("lower_limit", "upper_limit"):
                profile = config.parse({
                    "metrics": [{"label": "Alert preflight first", "kind": "number", "initial_value": "2"}],
                    "alerts": [{
                        "label": "Mutable limit", "watches": "m.alert_preflight_first",
                        "lower_limit": "0", "upper_limit": "10",
                    }],
                })
                setattr(profile.alerts[0], field, Decimal("1E-19"))
                before = complete_snapshot(service)
                try:
                    config.apply_to(service, profile, replace=replace)
                except config.ConfigError as exc:
                    rejected = field in str(exc) and "losing precision" in str(exc)
                else:
                    rejected = False
                report.check(rejected, f"{mode} rejects a mutable lossy alarm {field}")
                after = complete_snapshot(service)
                report.check(
                    after == before,
                    f"{mode} rejects the mutable alarm {field} before any state or versions change",
                    f"versions {before['mdib_versions']} -> {after['mdib_versions']}; "
                    f"changed {[name for name in before if before[name] != after[name]]}",
                )
    finally:
        service.remove_metric(existing)


def check_duplicate_alarm_roundtrip(report: Report, service: ProviderService) -> None:
    """Identical labels allocate the same unique generated signals in preflight and creation."""
    metric = service.add_metric(MetricSpec(label="Duplicate alarm source", kind=MetricKind.NUMBER, initial_value=Decimal("7")))
    alerts = [service.add_alert(AlertSpec(label="High", source_handle=metric)) for _ in range(2)]
    expected_signals = {
        "al.high": ["sig.high.vis", "sig.high.aud"],
        "al.high.2": ["sig.high.vis.2", "sig.high.aud.2"],
    }
    payload = config.to_dict(service)
    try:
        config.apply_to(service, config.parse(payload))
    except config.ConfigError as exc:
        report.check(False, "two High alarms can be exported and reimported", str(exc))
    else:
        report.check(
            {handle: service.signal_handles_for(handle) for handle in alerts} == expected_signals,
            "two High alarms round-trip with the original unique suffix allocation",
        )
        implicit = {
            **payload,
            "alerts": [{key: value for key, value in alert.items() if key != "handle"} for alert in payload["alerts"]],
        }
        config.apply_to(service, config.parse(implicit))
        report.check(
            {handle: service.signal_handles_for(handle) for handle in alerts} == expected_signals,
            "implicit duplicate alarm handles use the same suffix allocation as add_alert",
        )
        before_graph = semantic_mdib(service.mdib)
        before_version = service.mdib.mdib_version
        original_set_patient = service.set_patient
        created_signals = {}

        def fail_after_alarms(info: PatientInfo) -> None:
            created_signals.update({handle: service.signal_handles_for(handle) for handle in alerts})
            original_set_patient(info)
            raise RuntimeError("injected failure after duplicate alarms")

        with patch.object(service, "set_patient", fail_after_alarms):
            try:
                config.apply_to(service, config.parse(payload))
            except config.ConfigError as exc:
                rejected = "injected failure after duplicate alarms" in str(exc)
            else:
                rejected = False
        report.check(
            rejected and created_signals == expected_signals
            and semantic_mdib(service.mdib) == before_graph
            and service.mdib.mdib_version > before_version,
            "failure after creating duplicate alarms compensates the complete graph without rewinding versions",
        )
    for handle in alerts:
        service.remove_alert(handle)
    repeated = AlertSpec(
        label="Repeated manifestation", source_handle=metric,
        signals=(AlertSignalSpec(AlertManifestation.VIS), AlertSignalSpec(AlertManifestation.VIS, latching=True)),
    )
    try:
        handle = service.add_alert(repeated)
    except ValueError as exc:
        report.check(False, "pending signals with the same manifestation get unique handles", str(exc))
    else:
        report.check(
            service.signal_handles_for(handle) == ["sig.repeated_manifestation.vis", "sig.repeated_manifestation.vis.2"],
            "pending signals with the same manifestation get unique handles",
        )
        service.remove_alert(handle)
    service.remove_metric(metric)


def check_detached_patient_export(report: Report, service: ProviderService) -> None:
    """An exported detach is an instruction; an omitted patient remains no instruction."""
    service.clear_patient()
    payload = config.to_dict(service)
    report.check(
        payload.get("contexts", {}).get("patient") == {}, "a detached patient exports an explicit patient: {}",
    )
    fresh = ProviderService(instance_name="config-detached-patient")
    fresh.start()
    try:
        for version in (1, 2, 3):
            if fresh.get_patient().is_empty():
                fresh.set_patient(PatientInfo(given_name="Default associated"))
            payload["version"] = version
            config.apply_to(fresh, config.parse(payload))
            entity = fresh.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE)
            report.check(
                fresh.get_patient().is_empty()
                and not any(
                    state.ContextAssociation == pm_types.ContextAssociation.ASSOCIATED for state in entity.states.values()
                ),
                f"version {version} detached-patient export clears a fresh associated provider",
            )
            fresh.set_patient(PatientInfo(given_name="Keep this patient"))
            before = complete_snapshot(fresh)
            config.apply_to(fresh, config.parse({"version": version, "contexts": {}}))
            report.check(
                complete_snapshot(fresh) == before,
                f"version {version} omitted patient leaves the associated context unchanged",
            )
    finally:
        fresh.stop()


def check_config_versions(report: Report, workdir: Path) -> None:
    """All supported versions upgrade on export; rejected versions never reach apply."""
    cases = [
        ("missing version", False, None, True),
        ("version 0", True, 0, False),
        ("version -1", True, -1, False),
        *[
            (f"version {version}", True, version, True)
            for version in range(config.LEGACY_CONFIG_VERSION, config.CONFIG_VERSION + 1)
        ],
        ("future version", True, config.CONFIG_VERSION + 1, False),
        ("boolean true version", True, True, False),
        ("boolean false version", True, False, False),
        ("fractional version", True, 1.5, False),
    ]
    service = ProviderService(instance_name="config-versions")
    service.start()
    accepted = {}
    try:
        service.add_metric(MetricSpec(label="Existing", kind=MetricKind.NUMBER, initial_value=Decimal("9")))
        for description, explicit, version, should_accept in cases:
            payload = {
                "metrics": [
                    {
                        "handle": "m.version_test",
                        "label": "Version test",
                        "kind": "number",
                        "initial_value": "7",
                    },
                ],
            }
            if explicit:
                payload["version"] = version
            case_path = workdir / f"version-{len(accepted)}-{description.replace(' ', '-')}.json"
            case_path.write_text(json.dumps(payload), encoding="utf-8")
            before = complete_snapshot(service)
            try:
                config.load_into(service, case_path)
            except config.ConfigError as exc:
                report.check(
                    not should_accept and "top level.version" in str(exc),
                    f"rejects {description} with version field context",
                    str(exc),
                )
                report.check(
                    complete_snapshot(service) == before,
                    f"rejected {description} does not mutate the provider",
                )
                continue

            report.check(should_accept, f"accepts {description}")
            serialized = config.to_dict(service)
            round_tripped = config.parse(serialized)
            accepted[description] = device_config_snapshot(round_tripped)
            report.check(
                serialized.get("version") == config.CONFIG_VERSION
                and len(round_tripped.metrics) == 1
                and round_tripped.metrics[0].initial_value == Decimal("7"),
                f"{description} round-trips at current version {config.CONFIG_VERSION}",
                str(serialized.get("version")),
            )
    finally:
        service.stop()

    report.check(
        accepted.get("missing version") == accepted.get(f"version {config.LEGACY_CONFIG_VERSION}"),
        f"a missing version uses legacy version {config.LEGACY_CONFIG_VERSION} semantics",
    )


BAD_FILES = [
    ('{"metrics": [{"label": "x"}]}', "a metric with no kind"),
    ('{"metrics": [{"label": "x", "kind": "nope"}]}', "an unknown kind"),
    ('{"metrics": [{"kind": "number"}]}', "a metric with no label"),
    ('{"metrics": [{"label": "x", "kind": "number", "minimum": "abc"}]}', "a non-numeric limit"),
    ('{"metrics": [{"label": "x", "kind": "number", "minimum": "NaN"}]}', "a NaN metric limit"),
    ('{"metrics": [{"label": "x", "kind": "number", "maximum": "Infinity"}]}', "an infinite metric limit"),
    ('{"metrics": [{"label": "x", "kind": "number", "resolution": "0"}]}', "a zero resolution"),
    ('{"metrics": [{"label": "x", "kind": "number", "resolution": "-1"}]}', "a negative resolution"),
    (
        '{"metrics": [{"label": "wave", "kind": "waveform", "sample_period": "1e-324"}]}',
        "a float-underflowing sample period",
    ),
    (
        '{"metrics": [{"label": "wave", "kind": "waveform", "maximum": "1e400"}]}',
        "a float-overflowing waveform range",
    ),
    (
        '{"metrics": [{"label": "distribution", "kind": "distribution", "minimum": "-1e400"}]}',
        "a negative float-overflowing distribution range",
    ),
    (
        '{"metrics": [{"label": "x", "kind": "number", "minimum": "10", "maximum": "1"}]}',
        "a minimum above its maximum",
    ),
    (
        '{"metrics": [{"label": "x", "kind": "distribution", "domain_minimum": "0", "domain_maximum": "0"}]}',
        "a zero-width distribution domain",
    ),
    (
        '{"metrics": [{"label": "x", "kind": "distribution", "domain_minimum": "-Infinity"}]}',
        "an infinite distribution domain",
    ),
    ('{"metrics": [{"label": "x", "kind": "choice"}]}', "a choice with no values"),
    ('{"alerts": [{"label": "a", "watches": "m.nothing"}]}', "an alarm watching nothing"),
    ('{"alerts": [{"label": "a"}]}', "an alarm with no source"),
    (
        '{"metrics": [{"handle": "m.x", "label": "x", "kind": "number"}], "alerts": [{"label": "a", "watches": "m.x", "signals": []}]}',
        "an alarm with no signals",
    ),
    (
        '{"metrics": [{"handle": "m.x", "label": "x", "kind": "number"}], "alerts": [{"label": "a", "watches": "m.x", "upper_limit": "NaN"}]}',
        "a NaN alert limit",
    ),
    (
        '{"metrics": [{"handle": "m.x", "label": "x", "kind": "text"}], "alerts": [{"label": "a", "watches": "m.x", "upper_limit": "1"}]}',
        "a limit alarm on a nonnumeric scalar source",
    ),
    (
        '{"metrics": [{"handle": "m.x", "label": "x", "kind": "number"}], "actions": [{"label": "a", "target": "mds0", "effects": {"m.x": "-Infinity"}}]}',
        "an infinite numeric action effect",
    ),
    (
        '{"metrics": [{"handle": "m.x", "label": "x", "kind": "number"}], "alerts": [{"label": "a", "watches": "m.x", "signals": [{"manifestation": "Vis", "latching": "false"}]}]}',
        "a non-boolean signal latching value",
    ),
    (
        '{"contexts": {"patient": {"height": {"value": "170"}}}}',
        "a patient height without a unit",
    ),
    (
        '{"contexts": {"patient": {"weight": {"unit": {"code": "kg", "system": "private"}}}}}',
        "a patient weight without a value",
    ),
    (
        '{"contexts": {"patient": {"race": {"code": "demo-race"}}}}',
        "a patient race without a coding system",
    ),
    (
        '{"contexts": {"patient": {"height": {"value": "1e999999", "unit": {"code": "m", "system": "private"}}}}}',
        "an impractical patient measurement exponent",
    ),
    (
        '{"contexts": {"patient": {"height": {"value": 1e-1000, "unit": {"code": "m", "system": "private"}}}}}',
        "an underflowing patient measurement literal",
    ),
    (
        '{"contexts": {"patient": {"race": {"code": "bad\\u0001", "system": "private"}}}}',
        "an XML-invalid patient race code",
    ),
    (
        '{"contexts": {"patient": {"race": {"code": "demo-race", "system": "urn:%"}}}}',
        "a malformed patient race coding-system URI",
    ),
    (
        '{"contexts": {"patient": {"given_name": "bad\\u0001"}}}',
        "an XML-invalid patient name",
    ),
    ("not json at all", "a file that is not JSON"),
    ('{"version": "2"}', "a non-integer version"),
    ('{"version": 99}', "a file from a newer build"),
    ("[]", "a top level list"),
]

SCHEMA_BAD_FILES = [
    ({"metric": []}, "top level", "a misspelled top-level key"),
    (
        {"metrics": [{"label": "x", "kind": "number", "controlable": True}]},
        "metrics[x]",
        "a misspelled controllable key",
    ),
    (
        {"metrics": [{"label": "x", "kind": "number", "minumum": "0"}]},
        "metrics[x]",
        "a misspelled minimum key",
    ),
    (
        {
            "metrics": [{"handle": "m.x", "label": "x", "kind": "number"}],
            "alerts": [{"label": "a", "watches": "m.x", "prioritty": "Hi"}],
        },
        "alerts[a]",
        "an unknown alert key",
    ),
    (
        {"actions": [{"label": "a", "target": "mds0", "effect": {}}]},
        "actions[a]",
        "an unknown action key",
    ),
    ({"metrics": {}}, "top level.metrics", "an object metrics collection"),
    ({"alerts": "none"}, "top level.alerts", "a string alerts collection"),
    ({"actions": 1}, "top level.actions", "a numeric actions collection"),
    (
        {"metrics": [{"label": "x", "kind": "choice", "allowed_values": "A"}]},
        "metrics[x].allowed_values",
        "a scalar allowed-values collection",
    ),
    (
        {
            "metrics": [{"handle": "m.x", "label": "x", "kind": "number"}],
            "alerts": [{"label": "a", "watches": "m.x", "signals": {}}],
        },
        "alerts[a].signals",
        "an object signal collection",
    ),
    (
        {"actions": [{"label": "a", "target": "mds0", "effects": []}]},
        "actions[a].effects",
        "an array action-effects object",
    ),
    (
        {"metrics": [{"label": "x", "kind": "number", "controllable": "false"}]},
        "metrics[x].controllable",
        "a string metric boolean",
    ),
    (
        {
            "metrics": [{"handle": "m.x", "label": "x", "kind": "number"}],
            "alerts": [{"label": "a", "watches": "m.x", "delegable": "false"}],
        },
        "alerts[a].delegable",
        "a string alert boolean",
    ),
    (
        {"metrics": [{"label": "wave", "kind": "waveform", "cycle_samples": 2.5}]},
        "metrics[wave].cycle_samples",
        "a fractional waveform cycle count",
    ),
    (
        {"metrics": [{"label": 7, "kind": "number"}]},
        "metrics.label",
        "a non-string metric label",
    ),
    (
        {"metrics": [{"label": "x", "kind": "number", "unit": {"label": "u", "cod": "x"}}]},
        "metrics[x].unit",
        "an unknown coding key",
    ),
    (
        {"contexts": {"patient": {"given_name": 7}}},
        "contexts.patient.given_name",
        "a non-string patient field",
    ),
    (
        {"contexts": {"patient": {"given_name": "x", "surname": "y"}}},
        "contexts.patient",
        "an unknown patient key",
    ),
    (
        {"contexts": {"location": "ward"}},
        "contexts.location",
        "a scalar location object",
    ),
]


UNENCODABLE_TEXT = {
    "a control character": "bad\x0btext",
    "a NUL": "bad\x00text",
    "a lone surrogate": "bad\ud800text",
    "a non-character": "bad\ufffetext",
}


def check_profile_text_xml_preflight(report: Report) -> None:
    """The device block is announced as ThisModel and ThisDevice, so it must be text XML can carry.

    It used to be accepted: the provider started and answered discovery, then failed every connect.
    """
    names = [field.name for field in fields(DeviceInfo)] + ["instance_name"]
    targets = [(f"device.{name}", name, lambda text, name=name: {"device": {name: text}}) for name in names]
    targets += [(key, key, lambda text, key=key: {key: text}) for key in ("name", "description")]
    for label, key, payload in targets:
        problems = []
        for kind, text in UNENCODABLE_TEXT.items():
            try:
                config.parse(payload(text))
            except config.ConfigError as exc:
                if key not in str(exc):
                    problems.append(f"{kind}: {exc}")
            except Exception as exc:  # noqa: BLE001 - a malformed file must only expose ConfigError
                problems.append(f"{kind}: {type(exc).__name__}")
            else:
                problems.append(f"{kind}: accepted")
        report.check(not problems, f"{label} refuses text that XML cannot carry", "; ".join(problems))

    ordinary = "Klinik M\u00fcnchen \u6771\u4eac \u202e <a&b]]>\"' " + "x" * 5000
    accepted = config.parse({
        "name": ordinary,
        "description": ordinary,
        "device": {name: ordinary for name in names},
    })
    report.check(
        accepted.device is not None
        and accepted.device.friendly_name == ordinary
        and accepted.instance_name == ordinary,
        "text that XML can carry is kept as it is",
    )

    for description, build in (
        ("an instance name", lambda: ProviderService(instance_name="bad\x0btext")),
        ("an instance name with a lone surrogate", lambda: ProviderService(instance_name="bad\ud800text")),
        ("a friendly name", lambda: ProviderService(friendly_name="bad\x0btext")),
        ("a model name", lambda: DeviceInfo(model_name="bad\x00text")),
    ):
        try:
            build()
        except ValueError as exc:
            report.check(
                "XML" in str(exc),
                f"{description} that XML cannot carry is refused before a provider exists",
                str(exc)[:80],
            )
        else:
            report.check(False, f"{description} that XML cannot carry is refused", "it was accepted")  # noqa: FBT003


def check_import_edge_cases(report: Report, workdir: Path) -> None:
    """Files that editors and shells write must not end in an uncaught exception or a baffling refusal."""
    deep = workdir / "deep.json"
    deep.write_text("[" * 200_000, encoding="utf-8")
    try:
        config.load_file(deep)
    except config.ConfigError as exc:
        report.check("nested" in str(exc), "a deeply nested file is refused with a usable message", str(exc)[:80])
    except Exception as exc:  # noqa: BLE001 - the GUI and the console only handle ConfigError
        report.check(False, "a deeply nested file is refused with ConfigError", type(exc).__name__)  # noqa: FBT003
    else:
        report.check(False, "a deeply nested file is refused", "it was accepted")  # noqa: FBT003

    document = {"name": "From a BOM file", "metrics": [{"label": "From a BOM file", "kind": "number"}]}
    signed = workdir / "bom.json"
    signed.write_bytes(b"\xef\xbb\xbf" + json.dumps(document).encode("utf-8"))
    try:
        loaded = config.load_file(signed)
    except config.ConfigError as exc:
        report.check(False, "a file that starts with a UTF-8 byte order mark is read", str(exc)[:80])  # noqa: FBT003
    else:
        report.check(
            [spec.label for spec in loaded.metrics] == ["From a BOM file"],
            "a file that starts with a UTF-8 byte order mark is read",
        )

    presets = workdir / "bom-presets"
    presets.mkdir()
    (presets / "bom.json").write_bytes(signed.read_bytes())
    report.check(
        [preset.name for preset in config.list_presets(presets)] == ["From a BOM file"],
        "a preset that starts with a UTF-8 byte order mark is listed",
    )


def check_refused_create_is_not_retried(report: Report, service: ProviderService, workdir: Path) -> None:
    """Saving where files cannot be created must fail at once, not retry for days.

    Windows reports a refused create like a name clash, so tempfile tries about two billion
    names; an export into a drive root froze the window that asked for it.
    """
    attempts: list[str] = []

    def refusing(real):  # noqa: ANN001, ANN202
        def opener(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            if isinstance(file, str | bytes | os.PathLike) and Path(os.fsdecode(file)).parent == workdir:
                attempts.append(os.fsdecode(file))
                raise PermissionError(errno.EACCES, "injected: files cannot be created here", os.fsdecode(file))
            return real(file, *args, **kwargs)

        return opener

    before = set(workdir.iterdir())
    for suffix in (config.FILE_SUFFIX, ".xml"):
        target = workdir / f"refused{suffix}"
        attempts.clear()
        # bounded, or the unfixed code would go on for about 2 billion attempts
        with (
            patch.object(os, "open", refusing(os.open)),
            patch.object(io, "open", refusing(io.open)),
            patch.object(tempfile, "TMP_MAX", 20),
        ):
            try:
                config.save(service, target)
            except OSError as exc:
                outcome = type(exc).__name__
            else:
                outcome = "saved"
        report.check(
            outcome == "PermissionError" and len(attempts) == 1,
            f"a {suffix} save into a folder that refuses new files fails at once with the real reason",
            f"{outcome} after {len(attempts)} attempt(s)",
        )
        report.check(set(workdir.iterdir()) == before, f"a refused {suffix} save leaves nothing behind")


def run_checks(report: Report, workdir: Path) -> None:
    path = workdir / f"reference{config.FILE_SUFFIX}"

    print("=" * 74)
    print("Config round trip")
    print("=" * 74)

    print("\n1. Export")
    source = ProviderService(instance_name="config-source", device=REFERENCE_DEVICE)
    source.start()
    try:
        build_reference(source)
        before = snapshot(source)
        config.save(source, path)
        report.check(path.exists(), "the file is written", f"{path.stat().st_size} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
        report.check(
            data.get("version") == config.CONFIG_VERSION,
            f"new profiles use current version {config.CONFIG_VERSION}",
        )
        report.check(
            data.get("device") == device_snapshot(source.instance_name, REFERENCE_DEVICE),
            "construction-time device metadata is exported",
            str(data.get("device")),
        )
        report.check(len(data.get("metrics", [])) == 5, "all data sources are in it")  # noqa: PLR2004
        report.check(len(data.get("alerts", [])) == 2, "and both alarms")  # noqa: PLR2004
        report.check(
            all("handle" in entry for entry in data["metrics"]),
            "handles are recorded for stable metric references",
        )
        zoom_alert = next(alert for alert in data["alerts"] if alert["handle"] == "al.zoom_high")
        report.check(
            zoom_alert["signals"] == [
                {"manifestation": "Vis", "latching": True},
                {"manifestation": "Tan", "latching": False},
            ],
            "signal manifestation and latching settings are exported",
            str(zoom_alert["signals"]),
        )
        service_due = next(alert for alert in data["alerts"] if alert["handle"] == "al.service_due")
        report.check(
            service_due["signals"] == [
                {"manifestation": "Vis", "latching": False},
                {"manifestation": "Aud", "latching": False},
            ],
            "default version-3 signals are exported explicitly",
            str(service_due["signals"]),
        )
        patient_data = data.get("contexts", {}).get("patient", {})
        report.check(
            patient_data.get("height", {}).get("value") == "170.5"
            and patient_data.get("height", {}).get("unit", {}).get("code") == "demo-cm"
            and patient_data.get("weight", {}).get("value") == "72.4"
            and patient_data.get("race", {}).get("code") == "demo-race"
            and patient_data.get("race", {}).get("system") == "urn:example:race",
            "patient measurements and race are exported as coded structures",
            str(patient_data),
        )
        precise = workdir / "precise-patient.json"
        precise.write_text(
            '{"contexts": {"patient": {"height": {"value": 0.12345678901234567, '
            '"unit": {"code": "demo-unit", "system": "private"}}}}}',
            encoding="utf-8",
        )
        parsed_patient = config.load_file(precise).patient
        report.check(
            parsed_patient is not None
            and parsed_patient.height is not None
            and parsed_patient.height.value == Decimal("0.12345678901234567"),
            "JSON measurement literals retain Decimal precision",
            str(parsed_patient.height.value) if parsed_patient and parsed_patient.height else "none",
        )
        try:
            config.parse(
                {
                    "contexts": {
                        "patient": {
                            "height": {
                                "value": 0.12345678901234567,
                                "unit": {"code": "demo-unit", "system": "private"},
                            },
                        },
                    },
                },
            )
        except config.ConfigError as exc:
            report.check("never float" in str(exc), "programmatic float measurements are refused", str(exc))
        else:
            report.check(False, "programmatic float measurements are refused", "it was accepted")  # noqa: FBT003
        source.set_value("m.zoom_level", Decimal("42"))
        config.save(source, workdir / "with-values.json")
        with_values = json.loads((workdir / "with-values.json").read_text(encoding="utf-8"))
        zoom = next(m for m in with_values["metrics"] if m["handle"] == "m.zoom_level")
        report.check(
            zoom.get("initial_value") == "42",
            "the current value is captured as the initial value",
            str(zoom.get("initial_value")),
        )
    finally:
        source.stop()

    print("\n2. Import into a fresh device")
    target = ProviderService(instance_name="config-target")
    target.start()
    try:
        metrics, alerts = config.load_into(target, path)
        report.check(metrics == 5, "five data sources created", str(metrics))  # noqa: PLR2004
        report.check(alerts == 2, "two alarms created", str(alerts))  # noqa: PLR2004
        after = snapshot(target)
        report.check(after == before, "everything matches the original")
        if after != before:
            for field in before:
                if before[field] != after[field]:
                    print(f"      {field}\n        before {before[field]}")
                    print(f"        after  {after[field]}")

        imported = config.load_file(path)
        report.check(
            device_snapshot(imported.instance_name, imported.device)
            == device_snapshot(source.instance_name, REFERENCE_DEVICE),
            "device metadata is parsed separately from running-provider import",
            repr(imported.device),
        )

        report.check(
            len(target.signal_handles_for("al.zoom_high")) == 2,  # noqa: PLR2004
            "the alarm's signals are rebuilt too",
            str(target.signal_handles_for("al.zoom_high")),
        )
        report.check(
            target.mdib.entities.by_handle("al.zoom_high").descriptor.NODETYPE.localname
            == "LimitAlertConditionDescriptor",
            "an alarm with limits comes back as a LimitAlertCondition",
        )

        check_config_versions(report, workdir)

        preset_round_trips = []
        preset_paths = sorted((ROOT / "presets").glob("*.json"))
        preset_files = {preset_path.name for preset_path in preset_paths}
        json_preset_files = {name for name in constants.SHIPPED_PRESET_FILES if name.endswith(".json")}
        for preset_path in preset_paths:
            preset_data = json.loads(preset_path.read_text(encoding="utf-8"))
            preset = config.parse(preset_data)
            preset_service = ProviderService(instance_name=preset.instance_name, device=preset.device)
            preset_service.start()
            try:
                config.apply_to(preset_service, preset)
                serialized = config.to_dict(preset_service)
                source_contexts = preset_data.get("contexts", {})
                current_contexts = serialized.get("contexts", {})
                retained_contexts = {
                    name: current_contexts[name]
                    for name in ("location", "patient")
                    if name in source_contexts
                }
                if retained_contexts:
                    serialized["contexts"] = retained_contexts
                else:
                    serialized.pop("contexts", None)
                round_tripped = config.parse(serialized)
                signals_preserved = all(
                    serialized_alert["signals"]
                    == [
                        {"manifestation": signal.manifestation.value, "latching": signal.latching}
                        for signal in alert.signals
                    ]
                    for alert, serialized_alert in zip(
                        sorted(preset.alerts, key=lambda item: item.handle or ""),
                        serialized["alerts"],
                        strict=True,
                    )
                )
                preset_round_trips.append(
                    preset_data.get("version") == config.CONFIG_VERSION
                    and serialized.get("version") == config.CONFIG_VERSION
                    and signals_preserved
                    and device_config_snapshot(round_tripped) == device_config_snapshot(preset)
                )
            finally:
                preset_service.stop()
        report.check(
            preset_files == json_preset_files and all(preset_round_trips),
            "canonical presets round-trip at the current version with version-3 signals intact",
            f"{sum(preset_round_trips)} of {len(json_preset_files)}; "
            f"files {sorted(preset_files)}",
        )

        print("\n3. Import replaces rather than appends")
        metrics, _ = config.load_into(target, path)
        report.check(
            len(target.list_metrics()) == 5,  # noqa: PLR2004
            "loading twice does not duplicate anything",
            str(sorted(target.list_metrics())),
        )
        report.check(
            sorted(target.list_metrics()) == sorted(before["metrics"]),
            "and the handles are unchanged",
        )
    finally:
        target.stop()

    print("\n4. Invalid imports do not mutate the device")
    guarded = ProviderService(instance_name="config-preflight")
    guarded.start()
    try:
        check_preflight_preserves_device(report, guarded)
        check_section_handle_collisions(report, guarded)
        check_append_section_type_preflight(report, guarded)
    finally:
        guarded.stop()

    print("\n5. Operational failures roll back replacements")
    transactional = ProviderService(instance_name="config-transaction")
    transactional.start()
    try:
        check_operational_failure_rolls_back(report, transactional)
        check_profile_demo_mode_lifecycle(report, transactional)
    finally:
        transactional.stop()

    print("\n6. Connected consumers observe compensating rollback")
    check_connected_consumer_rollback(report)

    print("\n6a. Generator start failures compensate the applied replacement")
    check_generator_start_rollback(report)

    print("\n6b. Append failures compensate consumer-observed partial changes")
    check_connected_consumer_append_rollback(report)

    print("\n7. Replacement updates section containment")
    sections = ProviderService(instance_name="config-sections")
    sections.start()
    try:
        check_replace_section_lifecycle(report, sections)
    finally:
        sections.stop()

    print("\n7a. Provider import boundary preserves protocol history and serialization")
    check_profile_boundary_semantics(report)

    print("\n8. Action effects follow target metric kinds")
    check_action_effect_types(report)

    print("\n9. Metric handles are stable across parsing, preflight, and creation")
    handles = ProviderService(instance_name="config-metric-handles")
    handles.start()
    try:
        check_metric_handle_resolution(report, handles)
    finally:
        handles.stop()

    print("\n10. Bad files are refused with a usable message")
    for text, description in BAD_FILES:
        bad = workdir / "bad.json"
        bad.write_text(text, encoding="utf-8")
        try:
            config.load_file(bad)
        except config.ConfigError as exc:
            report.check(bool(str(exc)), f"refuses {description}", str(exc)[:70])
        else:
            report.check(False, f"refuses {description}", "it was accepted")  # noqa: FBT003

    for payload, field, description in SCHEMA_BAD_FILES:
        bad = workdir / "bad-shape.json"
        bad.write_text(json.dumps(payload), encoding="utf-8")
        try:
            config.load_file(bad)
        except config.ConfigError as exc:
            report.check(field in str(exc), f"refuses {description} with field context", str(exc)[:90])
        except Exception as exc:  # noqa: BLE001 - malformed files must only expose ConfigError
            report.check(False, f"refuses {description} with ConfigError", type(exc).__name__)  # noqa: FBT003
        else:
            report.check(False, f"refuses {description}", "it was accepted")  # noqa: FBT003

    print("\n11. A missing file says so")
    try:
        config.load_file(workdir / "does-not-exist.json")
    except config.ConfigError as exc:
        report.check("cannot read" in str(exc), "missing file reported clearly", str(exc)[:60])
    else:
        report.check(False, "missing file reported clearly", "it was accepted")  # noqa: FBT003
    check_import_edge_cases(report, workdir)

    print("\n12. Audit regressions: atomic saves, XML preflight, duplicate alarms, and patient detach")
    check_profile_text_xml_preflight(report)
    regressions = ProviderService(instance_name="config-write-regressions")
    regressions.start()
    try:
        check_atomic_save(report, regressions, workdir)
        check_refused_create_is_not_retried(report, regressions, workdir)
        check_location_xml_validation(report, regressions)
        check_control_capability_roundtrip(report, regressions)
        check_descriptor_text_preflight(report, regressions)
        check_mutated_waveform_preflight(report, regressions)
        check_cycle_seconds(report)
        check_xml_profile_preflight(report, regressions)
        check_derived_step_preflight(report, regressions)
        check_derived_sample_range_preflight(report, regressions)
        check_mutable_alert_limit_preflight(report, regressions)
        check_duplicate_alarm_roundtrip(report, regressions)
        check_detached_patient_export(report, regressions)
    finally:
        regressions.stop()


def check_owned_temp_cleanup(report: Report) -> None:
    with tempfile.TemporaryDirectory(prefix="sdctoolbox-cleanup-root-") as raw_root:
        root = Path(raw_root)
        with tempfile.TemporaryDirectory(prefix="success-", dir=root) as raw_successful:
            successful = Path(raw_successful)
            (successful / "marker").write_text("closed", encoding="utf-8")
        report.check(not successful.exists(), "owned temporary directories are removed after success")

        try:
            with tempfile.TemporaryDirectory(prefix="failure-", dir=root) as raw_failed:
                failed = Path(raw_failed)
                (failed / "marker").write_text("closed", encoding="utf-8")
                raise AssertionError("forced temporary-directory failure")
        except AssertionError:
            pass
        report.check(not failed.exists(), "owned temporary directories are removed after exceptions")


def main() -> int:
    basic_logging_setup(level=logging.WARNING)
    report = Report()
    check_owned_temp_cleanup(report)
    with tempfile.TemporaryDirectory(prefix="sdctoolbox-config-") as raw_workdir:
        workdir = Path(raw_workdir)
        run_checks(report, workdir)
    report.check(not workdir.exists(), "the config test directory is removed after all services stop")
    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
