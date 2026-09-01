"""Verify that the installed sdc11073 still provides the API this project is built on.

Every entry in CHECKS was read from the sdc11073 sources while designing the project. This
script confirms each one exists in the *installed* package, so an upgrade that moves or
removes something fails here loudly rather than somewhere deep in a callback.

Output: OK / MISSING per entry.

Usage:  .venv/Scripts/python.exe diagnostics/check_api.py
"""

from __future__ import annotations

import importlib
import inspect
import sys

# (module, attribute path, role in the design)
CHECKS: list[tuple[str, str, str]] = [
    # --- Provider MDIB and transactions ---
    ("sdc11073.mdib", "ProviderMdib", "provider MDIB"),
    ("sdc11073.mdib", "ProviderMdib.from_mdib_file", "bootstrap from XML"),
    ("sdc11073.mdib", "ProviderMdib.from_string", "bootstrap from string"),
    ("sdc11073.mdib", "ProviderMdib.descriptor_transaction", "create/modify descriptors"),
    ("sdc11073.mdib", "ProviderMdib.metric_state_transaction", "set metric values"),
    ("sdc11073.mdib", "ProviderMdib.operational_state_transaction", "set OperatingMode"),
    ("sdc11073.mdib", "ProviderMdib.context_state_transaction", "contexts"),
    ("sdc11073.mdib", "ProviderMdib.reconstruct_mdib_with_context_states", "context serialization"),
    ("sdc11073.mdib", "ProviderMdib.alert_state_transaction", "alerts (stage 4)"),
    ("sdc11073.mdib", "ProviderMdib.rt_sample_state_transaction", "waveforms (stage 4)"),
    # --- Entity API (used throughout) ---
    ("sdc11073.mdib.providermdib", "ProviderEntityGetter.new_entity", "create metric at runtime"),
    ("sdc11073.mdib.mdibbase", "EntityGetter.by_handle", "entity by handle"),
    ("sdc11073.mdib.mdibbase", "EntityGetter.by_node_type", "entities by type"),
    ("sdc11073.mdib.mdibbase", "EntityGetter.by_parent_handle", "walk the containment tree"),
    # --- Operations / external control ---
    ("sdc11073.provider.operations", "SetValueOperation", "remote control for numeric"),
    ("sdc11073.provider.operations", "SetStringOperation", "remote control for string/enum"),
    ("sdc11073.provider.operations", "ActivateOperation", "command button (stage 4)"),
    ("sdc11073.provider.operations", "ExecuteParameters", "handler argument"),
    ("sdc11073.provider.operations", "ExecuteResult", "handler return value"),
    ("sdc11073.provider.operations", "OperationDefinitionBase.set_mdib", "creates the op descriptor"),
    ("sdc11073.provider.operations", "OperationDefinitionBase.set_operating_mode", "control on/off"),
    ("sdc11073.provider.sco", "ScoOperationsRegistry.register_operation", "register an operation"),
    ("sdc11073.provider.sco", "ScoOperationsRegistry.unregister_operation_by_handle", "unregister"),
    # --- Descriptor types (the five metric kinds) ---
    ("sdc11073.mdib.descriptorcontainers", "NumericMetricDescriptorContainer", "number"),
    ("sdc11073.mdib.descriptorcontainers", "StringMetricDescriptorContainer", "text"),
    ("sdc11073.mdib.descriptorcontainers", "EnumStringMetricDescriptorContainer", "dropdown"),
    ("sdc11073.mdib.descriptorcontainers", "RealTimeSampleArrayMetricDescriptorContainer", "waveform"),
    ("sdc11073.mdib.descriptorcontainers", "DistributionSampleArrayMetricDescriptorContainer", "distribution"),
    ("sdc11073.mdib.descriptorcontainers", "ScoDescriptorContainer", "SCO"),
    ("sdc11073.mdib.descriptorcontainers", "SetValueOperationDescriptorContainer", "op descriptor, numeric"),
    ("sdc11073.mdib.descriptorcontainers", "SetStringOperationDescriptorContainer", "op descriptor, text"),
    # --- Enums ---
    ("sdc11073.xml_types.pm_types", "MetricCategory.SETTING", "MetricCategory=Set"),
    ("sdc11073.xml_types.pm_types", "MetricCategory.MEASUREMENT", "MetricCategory=Msrmt"),
    ("sdc11073.xml_types.pm_types", "OperatingMode.ENABLED", "control enabled"),
    ("sdc11073.xml_types.pm_types", "OperatingMode.DISABLED", "control disabled"),
    ("sdc11073.xml_types.pm_types", "AllowedValue", "enum values"),
    ("sdc11073.xml_types.pm_types", "ComponentActivation.ON", "metric active"),
    ("sdc11073.xml_types.pm_types", "MeasurementValidity.VALID", "value valid"),
    ("sdc11073.xml_types.pm_types", "Measurement", "patient height and weight"),
    ("sdc11073.xml_types.pm_types", "CodedValue", "patient race and measurement units"),
    ("sdc11073.xml_types.pm_types", "PatientDemographicsCoreData", "patient demographics"),
    ("sdc11073.xml_types.dataconverters", "DecimalConverter", "lossless demographic decimal check"),
    # --- Consumer ---
    # Note: SdcConsumer is NOT re-exported from the sdc11073.consumer package,
    # it must be imported from sdc11073.consumer.consumerimpl.
    ("sdc11073.consumer.consumerimpl", "SdcConsumer", "consumer"),
    ("sdc11073.consumer.consumerimpl", "SdcConsumer.from_wsd_service", "from a discovery hit"),
    ("sdc11073.consumer.consumerimpl", "SdcConsumer.start_all", "subscribe to all reports"),
    ("sdc11073.consumer.consumerimpl", "SdcConsumer.periodic_metric_report", "periodic metric report"),
    ("sdc11073.consumer.consumerimpl", "SdcConsumer.periodic_alert_report", "periodic alert report"),
    ("sdc11073.consumer.consumerimpl", "SdcConsumer.periodic_component_report", "periodic component report"),
    ("sdc11073.consumer.consumerimpl", "SdcConsumer.periodic_operational_state_report", "periodic operational report"),
    ("sdc11073.consumer.consumerimpl", "SdcConsumer.periodic_context_report", "periodic context report"),
    ("sdc11073.mdib", "ConsumerMdib", "consumer MDIB"),
    ("sdc11073.mdib", "ConsumerMdib.init_mdib", "load the MDIB"),
    ("sdc11073.mdib.consumermdibxtra", "ConsumerMdibMethods", "extend MDIB report handling"),
    # --- Discovery ---
    ("sdc11073.wsdiscovery", "WSDiscovery", "discovery bound to an IP"),
    ("sdc11073.wsdiscovery", "WSDiscoverySingleAdapter", "discovery bound to an adapter name"),
    ("sdc11073.definitions_sdc", "SdcV1Definitions.MedicalDeviceTypesFilter", "search filter"),
    # --- Provider ---
    ("sdc11073.provider", "SdcProvider", "provider"),
    ("sdc11073.provider.providerimpl", "RoleProviderComponents", "role configuration"),
    ("sdc11073.location", "SdcLocation", "location context"),
    ("sdc11073.xml_types.dpws_types", "ThisModelType", "device model"),
    ("sdc11073.xml_types.dpws_types", "ThisDeviceType", "device instance"),
]

# Observables on MdibBase that the consumer pane depends on.
OBSERVABLES = [
    "metrics_by_handle",
    "waveform_by_handle",
    "alert_by_handle",
    "context_by_handle",
    "component_by_handle",
    "operation_by_handle",
    "new_descriptors_by_handle",
    "updated_descriptors_by_handle",
    "deleted_descriptors_by_handle",
    "description_modifications",
    "sequence_id",
    "instance_id",
]


def resolve(module_name: str, dotted: str):
    """Resolve 'Class.method' inside a module. Raises if any part is absent."""
    obj = importlib.import_module(module_name)
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def main() -> int:
    from importlib.metadata import version

    print(f"sdc11073 version : {version('sdc11073')}")
    print(f"Python           : {sys.version.split()[0]}")
    print("=" * 78)

    missing: list[str] = []
    for module_name, dotted, role in CHECKS:
        try:
            resolve(module_name, dotted)
            status = "OK     "
        except Exception as exc:  # noqa: BLE001 - every failure is interesting here
            status = "MISSING"
            missing.append(f"{module_name}.{dotted}  ({type(exc).__name__}: {exc})")
        print(f"{status}  {module_name}.{dotted:<52} {role}")

    print("-" * 78)
    print("Observables on MdibBase:")
    from sdc11073.mdib.mdibbase import MdibBase

    for name in OBSERVABLES:
        if hasattr(MdibBase, name):
            print(f"OK       MdibBase.{name}")
        else:
            print(f"MISSING  MdibBase.{name}")
            missing.append(f"MdibBase.{name}")

    print("-" * 78)
    print("Signatures the design relies on:")
    from sdc11073.mdib.providermdib import ProviderEntityGetter
    from sdc11073.provider.operations import OperationDefinitionBase
    from sdc11073.provider.sco import ScoOperationsRegistry

    for label, func in [
        ("ProviderEntityGetter.new_entity", ProviderEntityGetter.new_entity),
        ("OperationDefinitionBase.__init__", OperationDefinitionBase.__init__),
        ("OperationDefinitionBase.set_mdib", OperationDefinitionBase.set_mdib),
        ("ScoOperationsRegistry.register_operation", ScoOperationsRegistry.register_operation),
    ]:
        try:
            print(f"  {label}{inspect.signature(func)}")
        except (TypeError, ValueError) as exc:
            print(f"  {label}: signature unreadable ({exc})")

    print("=" * 78)
    if missing:
        print(f"RESULT: {len(missing)} deviation(s) from the design:")
        for item in missing:
            print(f"  - {item}")
        return 1
    print("RESULT: installed API matches the design completely.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
