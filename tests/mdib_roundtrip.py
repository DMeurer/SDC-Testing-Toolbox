"""Native combined MDIB persistence and provider regression checks."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lxml import etree
from mdib_test_support import EXT, MSG, PM, XSI, original_mdib
from sdc11073.xml_types import pm_types
from sdc11073.xml_types import msg_types

from sdctoolbox.provider_service import ProviderService
from sdctoolbox.model import MetricKind, MetricSpec


class MdibIoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("sdctoolbox.mdib_io"), "native MDIB I/O is not implemented")
        self.io = importlib.import_module("sdctoolbox.mdib_io")

    def test_unrepresentable_nested_extension_is_rejected_not_silently_removed(self) -> None:
        from sdctoolbox.config import ConfigError

        root = etree.fromstring(original_mdib())
        quality = root.find(f".//{{{PM}}}MetricQuality")
        extension = etree.SubElement(quality, f"{{{EXT}}}Extension")
        etree.SubElement(extension, "{urn:original-test}Metadata").text = "must not disappear"
        with self.assertRaisesRegex(ConfigError, "preserv|extension"):
            self.io.parse_mdib(etree.tostring(root))

    def test_clock_snapshot_retains_timestamp_without_mutating_live_state(self) -> None:
        root = etree.fromstring(original_mdib())
        mds = root[0][0][0]
        clock = etree.Element(f"{{{PM}}}Clock", Handle="demo.clock")
        mds.insert(mds.index(mds.find(f"{{{PM}}}Vmd")), clock)
        etree.SubElement(root[0][1], f"{{{PM}}}State", DescriptorHandle="demo.clock",
                         attrib={f"{{{XSI}}}type": "pm:ClockState", "DateAndTime": "1234567890000", "RemoteSync": "false"})
        mdib = self.io.parse_mdib(etree.tostring(root))
        self.assertEqual(mdib.entities.by_handle("demo.clock").state.DateAndTime, 1234567890.0)
        exported = self.io.mdib_bytes(mdib)
        self.assertEqual(mdib.entities.by_handle("demo.clock").state.DateAndTime, 1234567890.0)
        restored = self.io.parse_mdib(exported)
        self.assertEqual(restored.entities.by_handle("demo.clock").state.DateAndTime, 1234567890.0)

    def test_relationship_alarm_summary_and_operation_list_references_are_validated(self) -> None:
        from sdctoolbox.config import ConfigError

        for field in ("relation", "technical", "physiological", "invocation"):
            with self.subTest(field=field):
                root = etree.fromstring(original_mdib())
                if field == "relation":
                    metric = root.find(f".//{{{PM}}}Metric[@Handle='demo.rate']")
                    relation = etree.Element(f"{{{PM}}}Relation", Kind="DCE", Entries="missing.metric")
                    metric.insert(metric.index(metric.find(f"{{{PM}}}TechnicalRange")), relation)
                elif field == "invocation":
                    etree.SubElement(root[0][1], f"{{{PM}}}State", DescriptorHandle="demo.sco",
                                     attrib={f"{{{XSI}}}type": "pm:ScoState", "InvocationRequired": "missing.operation"})
                else:
                    state = root.find(f".//{{{PM}}}State[@DescriptorHandle='demo.alerts']")
                    state.set("PresentTechnicalAlarmConditions" if field == "technical" else "PresentPhysiologicalAlarmConditions", "missing.alarm")
                with self.assertRaises(ConfigError):
                    self.io.parse_mdib(etree.tostring(root))

    def test_context_state_operation_target_is_a_valid_reference(self) -> None:
        root = etree.fromstring(original_mdib())
        operation = root.find(f".//{{{PM}}}Operation[@Handle='demo.action']")
        operation.set(f"{{{XSI}}}type", "pm:SetContextStateOperationDescriptor")
        operation.set("OperationTarget", "demo.current")
        state = root.find(f".//{{{PM}}}State[@DescriptorHandle='demo.action']")
        state.set(f"{{{XSI}}}type", "pm:SetContextStateOperationState")
        mdib = self.io.parse_mdib(etree.tostring(root))
        self.assertEqual(mdib.entities.by_handle("demo.action").descriptor.OperationTarget, "demo.current")

    def test_existing_typed_relationship_alarm_and_operation_references_round_trip(self) -> None:
        root = etree.fromstring(original_mdib())
        metric = root.find(f".//{{{PM}}}Metric[@Handle='demo.rate']")
        metric.insert(metric.index(metric.find(f"{{{PM}}}TechnicalRange")),
                      etree.Element(f"{{{PM}}}Relation", Kind="DCE", Entries="demo.wave"))
        root.find(f".//{{{PM}}}State[@DescriptorHandle='demo.alerts']").set("PresentTechnicalAlarmConditions", "demo.alarm")
        state = etree.SubElement(root[0][1], f"{{{PM}}}State", DescriptorHandle="demo.sco",
                                 attrib={f"{{{XSI}}}type": "pm:ScoState", "InvocationRequired": "demo.set"})
        group = etree.SubElement(state, f"{{{PM}}}OperationGroup", Operations="demo.set demo.disabled")
        etree.SubElement(group, f"{{{PM}}}Type", Code="demo-operation-group")
        mdib = self.io.parse_mdib(self.io.mdib_bytes(self.io.parse_mdib(etree.tostring(root))))
        self.assertEqual(mdib.entities.by_handle("demo.rate").descriptor.Relation[0].Entries, ["demo.wave"])
        self.assertEqual(mdib.entities.by_handle("demo.alerts").state.PresentTechnicalAlarmConditions, ["demo.alarm"])
        self.assertEqual(mdib.entities.by_handle("demo.sco").state.InvocationRequired, ["demo.set"])

    def test_import_retains_supplied_states_instead_of_factory_defaults(self) -> None:
        mdib = self.io.parse_mdib(original_mdib())
        self.assertEqual(mdib.mdib_version, 17)
        self.assertEqual(mdib.mddescription_version, 7)
        self.assertEqual(mdib.mdstate_version, 11)
        self.assertEqual(mdib.entities.by_handle("demo.rate").state.MetricValue.Value, Decimal("12.5"))
        self.assertEqual(mdib.entities.by_handle("demo.root").state.ActivationState, pm_types.ComponentActivation.STANDBY)
        self.assertEqual(mdib.entities.by_handle("demo.disabled").state.OperatingMode, pm_types.OperatingMode.DISABLED)
        self.assertTrue(mdib.entities.by_handle("demo.alarm").state.Presence)
        self.assertEqual(mdib.entities.by_handle("demo.alerts").state.ActivationState, pm_types.AlertActivation.PAUSED)

    def test_export_contains_description_all_states_samples_history_and_extensions(self) -> None:
        payload = self.io.mdib_bytes(self.io.parse_mdib(original_mdib()))
        root = etree.fromstring(payload)
        self.assertEqual(root.tag, f"{{{MSG}}}GetMdibResponse")
        self.assertEqual(len(root.findall(f"{{{MSG}}}Mdib/{{{PM}}}MdDescription")), 1)
        self.assertEqual(len(root.findall(f"{{{MSG}}}Mdib/{{{PM}}}MdState")), 1)
        self.assertEqual(root.find(".//{urn:sdc-testing-toolbox:test-extension}Note").text, "original fixture")
        restored = self.io.parse_mdib(payload)
        self.assertEqual(restored.entities.by_handle("demo.wave").state.MetricValue.Samples,
                         [Decimal("0"), Decimal("1.5"), Decimal("-0.5")])
        patient = restored.entities.by_handle("demo.patient")
        self.assertEqual(set(patient.states), {"demo.history", "demo.current"})
        self.assertEqual(patient.states["demo.history"].StateVersion, 2)
        self.assertEqual(patient.states["demo.current"].CoreData.Givenname, "Synthetic current")
        self.assertTrue(restored.entities.by_handle("demo.alarm").state.Presence)
        self.assertEqual(restored.entities.by_handle("demo.rate").parent_handle, "demo.channel")

    def test_combined_mdib_and_participant_container_wrappers(self) -> None:
        for participant in (False, True):
            root = etree.fromstring(original_mdib())[0]
            if participant:
                root.tag = f"{{{PM}}}MdibContainer"
            with self.subTest(participant=participant):
                self.assertEqual(self.io.parse_mdib(etree.tostring(root)).entities.by_handle("demo.rate").state.MetricValue.Value,
                                 Decimal("12.5"))

    def test_invalid_xml_or_graph_is_rejected(self) -> None:
        from sdctoolbox.config import ConfigError

        cases = {
            "malformed": b"<broken>",
            "doctype": b'<!DOCTYPE x [<!ENTITY probe SYSTEM "file:///not-to-be-read">]>' + original_mdib(),
            "wrong root": b"<not-a-mdib/>",
            "duplicate descriptor": original_mdib().replace(b'Handle="demo.mode" xsi:type="pm:EnumStringMetricDescriptor"', b'Handle="demo.rate" xsi:type="pm:EnumStringMetricDescriptor"'),
            "orphan state": original_mdib().replace(b'DescriptorHandle="demo.rate" xsi:type="pm:NumericMetricState"', b'DescriptorHandle="missing" xsi:type="pm:NumericMetricState"'),
            "bad target": original_mdib().replace(b'OperationTarget="demo.rate"', b'OperationTarget="missing"'),
            "bad signal": original_mdib().replace(b'ConditionSignaled="demo.alarm"', b'ConditionSignaled="missing"'),
            "unknown type": original_mdib().replace(b'pm:NumericMetricDescriptor', b'pm:UnknownMetricDescriptor'),
            "mismatched state": original_mdib().replace(b'DescriptorHandle="demo.mode" xsi:type="pm:EnumStringMetricState"', b'DescriptorHandle="demo.rate" xsi:type="pm:EnumStringMetricState"'),
        }
        split = etree.fromstring(original_mdib())[0][0]
        cases["split description"] = etree.tostring(split)
        for name, payload in cases.items():
            with self.subTest(name=name), self.assertRaises(ConfigError):
                self.io.parse_mdib(payload)

    def test_oversized_duration_is_a_config_error_not_an_uncaught_overflow(self) -> None:
        from sdctoolbox.config import ConfigError

        data = original_mdib().replace(b'SelfCheckPeriod="PT30S"', b'SelfCheckPeriod="PT999999999999999999999999H"')
        with self.assertRaises(ConfigError):
            self.io.parse_mdib(data)

    def test_atomic_export_keeps_existing_destination_after_replace_failure(self) -> None:
        mdib = self.io.parse_mdib(original_mdib())
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot.xml"
            target.write_bytes(b"previous file")
            with patch.object(Path, "replace", side_effect=OSError("injected replace failure")):
                with self.assertRaises(OSError):
                    self.io.save_mdib(SimpleNamespace(mdib=mdib), target)
            self.assertEqual(target.read_bytes(), b"previous file")
            self.assertEqual(list(Path(directory).iterdir()), [target])
            self.io.save_mdib(SimpleNamespace(mdib=mdib), target)
            self.assertEqual(self.io.load_mdib_file(target).xml, target.read_bytes())


class NativeProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(hasattr(ProviderService, "replace_mdib"), "native provider activation is not implemented")
        self.io = importlib.import_module("sdctoolbox.mdib_io")
        self.service = ProviderService(instance_name=f"native-test-{self._testMethodName}")
        self.addCleanup(self.service.stop)

    def start_native(self, data: bytes | None = None) -> None:
        self.service.start(self.io.parse_mdib(data or original_mdib()))

    def test_native_start_preserves_graph_state_contexts_and_does_not_generate(self) -> None:
        self.start_native()
        service = self.service
        self.assertTrue(service.native_mode)
        self.assertNotEqual(service.mdib.sequence_id, "urn:uuid:11111111-1111-1111-1111-111111111111")
        self.assertEqual(len(service.list_metrics()), 3)
        self.assertEqual(len(service.list_alerts()), 1)
        self.assertEqual(service.get_patient().given_name, "Synthetic current")
        self.assertEqual(service.get_location().facility, "DEMO")
        self.assertEqual(len(service.mdib.context_states.objects), 3)
        self.assertTrue(service.alert_present("demo.alarm"))
        self.assertFalse(service.generator_running)
        service.set_value("demo.rate", Decimal("45"))
        self.assertEqual(service.get_value("demo.rate"), Decimal("45"))
        self.assertTrue(service.alert_present("demo.alarm"), "native alarms must not acquire invented rules")
        self.assertEqual(service.get_samples("demo.wave"), [Decimal("0"), Decimal("1.5"), Decimal("-0.5")])
        with self.assertRaises(ValueError):
            service.add_metric(MetricSpec("not permitted", MetricKind.NUMBER))
        with self.assertRaises(ValueError):
            service.enable_control("demo.rate")

    def test_context_state_target_is_published_but_unsupported_execution_fails(self) -> None:
        root = etree.fromstring(original_mdib())
        operation = root.find(f".//{{{PM}}}Operation[@Handle='demo.action']")
        operation.set(f"{{{XSI}}}type", "pm:SetContextStateOperationDescriptor")
        operation.set("OperationTarget", "demo.current")
        root.find(f".//{{{PM}}}State[@DescriptorHandle='demo.action']").set(
            f"{{{XSI}}}type", "pm:SetContextStateOperationState")
        self.start_native(etree.tostring(root))
        registered = self.service._provider.get_operation_by_handle("demo.action")
        self.assertIsNotNone(registered)
        self.assertEqual(registered.execute_operation(None, SimpleNamespace(argument=None)).invocation_state,
                         msg_types.InvocationState.FAILED)
        self.assertEqual(self.service.get_patient().given_name, "Synthetic current")

    def test_operations_enforce_each_imported_range_mode_and_string_constraints(self) -> None:
        self.start_native()
        def invoke(handle, value):
            operation = self.service._provider.get_operation_by_handle(handle)
            self.assertIsNotNone(operation)
            return operation.execute_operation(None, SimpleNamespace(argument=value)).invocation_state

        self.assertEqual(invoke("demo.set", Decimal("15")), msg_types.InvocationState.FINISHED)
        for value in (Decimal("4"), Decimal("31"), Decimal("15.1")):
            self.assertEqual(invoke("demo.set", value), msg_types.InvocationState.FAILED)
        self.assertEqual(invoke("demo.disabled", Decimal("35")), msg_types.InvocationState.FAILED)
        self.assertEqual(invoke("demo.mode.set", "RUN"), msg_types.InvocationState.FAILED)
        self.assertEqual(invoke("demo.mode.set", "IDLE"), msg_types.InvocationState.FINISHED)
        self.assertEqual(invoke("demo.action", None), msg_types.InvocationState.FAILED)
        self.assertEqual(self.service.get_value("demo.rate"), Decimal("15"))

    def test_multiple_mds_and_scos_register_their_own_operations(self) -> None:
        root = etree.fromstring(original_mdib())
        description, states = root[0]
        description.append(etree.fromstring(etree.tostring(description[0]).replace(b"demo.", b"other.")))
        for state in list(states):
            states.append(etree.fromstring(etree.tostring(state).replace(b"demo.", b"other.")))
        self.start_native(etree.tostring(root))
        operation = self.service._provider.get_operation_by_handle("other.set")
        self.assertIsNotNone(operation)
        self.assertEqual(operation.execute_operation(None, SimpleNamespace(argument=Decimal("20"))).invocation_state,
                         msg_types.InvocationState.FINISHED)
        self.assertEqual(self.service.get_value("other.rate"), Decimal("20"))
        self.assertEqual(self.service.get_value("demo.rate"), Decimal("12.5"))
        self.assertEqual(self.service.mdib.entities.by_handle("other.set").parent_handle, "other.sco")

    def test_read_only_mdib_without_sco_does_not_gain_bootstrap_content(self) -> None:
        root = etree.fromstring(original_mdib())
        description, states = root[0]
        mds = description[0]
        for child in list(mds):
            mds.remove(child)
        for state in list(states):
            states.remove(state)
        self.start_native(etree.tostring(root))
        self.assertEqual({handle for handle, _ in self.service.mdib.entities.items()}, {"demo.root"})
        self.assertEqual(self.service.list_metrics(), {})
        self.assertFalse(self.service.mdib.context_states.objects)

    def test_failed_activation_restores_managed_values_contexts_and_operations(self) -> None:
        self.service.start()
        handle = self.service.add_metric(MetricSpec("Kept", MetricKind.NUMBER, controllable=True, initial_value=Decimal("8")))
        self.service.add_metric(MetricSpec("Kept wave", MetricKind.WAVEFORM))
        previous_sequence = self.service.mdib.sequence_id
        previous_patient = self.service.get_patient()
        original_start = self.service._start_graph
        attempts = 0
        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("injected activation failure")
            return original_start(*args, **kwargs)
        with patch.object(self.service, "_start_graph", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "injected activation failure"):
                self.service.replace_mdib(self.io.NativeMdibConfig(original_mdib()))
        self.assertFalse(self.service.native_mode)
        self.assertEqual(self.service.get_value(handle), Decimal("8"))
        self.assertEqual(self.service.get_patient(), previous_patient)
        self.assertNotEqual(self.service.mdib.sequence_id, previous_sequence)
        operation = self.service._provider.get_operation_by_handle(self.service.operation_handle_for(handle))
        self.assertEqual(operation.execute_operation(None, SimpleNamespace(argument=Decimal("9"))).invocation_state,
                         msg_types.InvocationState.FINISHED)
        self.assertEqual(self.service.get_value(handle), Decimal("9"))
        self.assertTrue(self.service.generator_running)

    def test_native_replacement_and_failed_recovery_are_explicit(self) -> None:
        self.service.start()
        self.service.replace_mdib(self.io.NativeMdibConfig(original_mdib()))
        self.assertTrue(self.service.native_mode)
        self.assertEqual(self.service.get_value("demo.rate"), Decimal("12.5"))
        with patch.object(self.service, "_start_graph", side_effect=RuntimeError("injected startup failure")):
            with self.assertRaisesRegex(RuntimeError, "import failed:.*recovery also failed:"):
                self.service.replace_mdib(self.io.NativeMdibConfig(original_mdib()))

    def test_invalid_replacement_never_stops_provider_and_json_replacement_exits_native(self) -> None:
        from sdctoolbox import config

        self.start_native()
        original = self.service.mdib
        with self.assertRaises(config.ConfigError):
            self.service.replace_mdib(self.io.NativeMdibConfig(b"<broken/>"))
        self.assertIs(self.service.mdib, original)
        profile = config.parse({"version": 3, "metrics": [{"label": "Back to JSON", "kind": "number"}]})
        with self.assertRaises(ValueError):
            self.service.import_profile(profile, replace=False)
        self.service.import_profile(profile)
        self.assertFalse(self.service.native_mode)
        self.assertEqual(len(self.service.list_metrics()), 1)
        self.assertIn("m.back_to_json", self.service.list_metrics())


class ConfigMdibTests(unittest.TestCase):
    def test_two_original_native_presets_are_shipped_and_include_state(self) -> None:
        from sdctoolbox import config, constants
        from sdctoolbox.mdib_io import parse_mdib

        expected = {"mdib-demo-monitor.xml", "mdib-demo-infusion-pump.xml"}
        for name in expected:
            path = Path(__file__).resolve().parent.parent / "presets" / name
            self.assertTrue(path.is_file(), f"missing original native preset: {name}")
            self.assertIn(name, constants.SHIPPED_PRESET_FILES)
            snapshot = config.load_file(path)
            mdib = parse_mdib(snapshot.xml)
            self.assertFalse(mdib.context_states.objects, "shipped demos must not include patient data")
            if name == "mdib-demo-monitor.xml":
                self.assertEqual(mdib.entities.by_handle("monitor.pulse").state.MetricValue.Value, Decimal("72"))
                self.assertEqual(len(mdib.entities.by_handle("monitor.trace").state.MetricValue.Samples), 8)
            else:
                self.assertEqual(mdib.entities.by_handle("pump.rate").state.MetricValue.Value, Decimal("2.5"))
                self.assertEqual(mdib.entities.by_handle("pump.mode").state.MetricValue.Value, "IDLE")

    def test_native_files_appear_alongside_json_presets_and_dispatch_without_conversion(self) -> None:
        from sdctoolbox import config
        from sdctoolbox.mdib_io import NativeMdibConfig

        with tempfile.TemporaryDirectory() as directory:
            native = Path(directory) / "native.xml"
            native.write_bytes(original_mdib())
            (Path(directory) / "broken.xml").write_bytes(b"<broken/>")
            (Path(directory) / "json.json").write_text('{"version":3,"name":"JSON demo","metrics":[]}', encoding="utf-8")
            self.assertIsInstance(config.load_file(native), NativeMdibConfig)
            presets = config.list_presets(directory)
            self.assertEqual(len(presets), 2)
            chosen = next(preset for preset in presets if preset.path.suffix == ".xml")
            self.assertIn("MDIB", chosen.summary())
            self.assertEqual((chosen.metrics, chosen.alerts), (3, 1))

    def test_explicit_export_filter_and_suffix_cannot_disagree(self) -> None:
        from sdctoolbox import config

        self.assertTrue(hasattr(config, "export_path"), "file-format selection is not implemented")
        self.assertEqual(config.export_path("snapshot", config.MDIB_FILE_FILTER), Path("snapshot.xml"))
        self.assertEqual(config.export_path("snapshot.xml", config.MDIB_FILE_FILTER), Path("snapshot.xml"))
        with self.assertRaises(config.ConfigError):
            config.export_path("snapshot.json", config.MDIB_FILE_FILTER)
        with self.assertRaises(config.ConfigError):
            config.export_path("snapshot.xml", config.JSON_FILE_FILTER)

    def test_native_api_export_is_combined_and_cannot_silently_reduce_to_json(self) -> None:
        from sdctoolbox import config
        from sdctoolbox.mdib_io import NativeMdibConfig

        service = ProviderService(instance_name="native-config-integration")
        service.start()
        try:
            snapshot = NativeMdibConfig(original_mdib())
            with self.assertRaises(config.ConfigError):
                config.apply_to(service, snapshot, replace=False)
            self.assertEqual(config.apply_to(service, snapshot), (3, 1))
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "combined.xml"
                config.save(service, target)
                self.assertIsInstance(config.load_file(target), NativeMdibConfig)
                with self.assertRaises(config.ConfigError):
                    config.save(service, Path(directory) / "lossy.json")
                self.assertFalse((Path(directory) / "lossy.json").exists())
                with self.assertRaises(config.ConfigError):
                    config.to_dict(service)
        finally:
            service.stop()


if __name__ == "__main__":
    unittest.main()
