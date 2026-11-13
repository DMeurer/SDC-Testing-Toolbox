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
from script_support import wait_until
from sdc11073.xml_types import pm_types
from sdc11073.xml_types import msg_types

from sdctoolbox.consumer_service import ConsumerService
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

    def assert_discoverable(self) -> None:
        epr = self.service.epr.urn
        consumer = ConsumerService()
        self.addCleanup(consumer.stop)
        consumer.start()

        def probed() -> bool:
            consumer.probe()
            return any(device.epr == epr for device in consumer.known_devices())

        self.assertTrue(wait_until(probed, timeout=8.0, interval=1.0), "the provider must answer a Probe")

    def test_native_start_is_discoverable(self) -> None:
        self.start_native()
        self.assert_discoverable()

    def test_native_replacement_is_discoverable_again(self) -> None:
        self.service.start()
        self.service.replace_mdib(self.io.NativeMdibConfig(original_mdib()))
        self.assertTrue(self.service.from_mdib_file)
        self.assert_discoverable()

    def test_native_start_preserves_graph_state_contexts_and_does_not_generate(self) -> None:
        self.start_native()
        service = self.service
        self.assertTrue(service.from_mdib_file)
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
        self.assertFalse(self.service.from_mdib_file)
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
        self.assertTrue(self.service.from_mdib_file)
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
        self.service.import_profile(profile)
        self.assertFalse(self.service.from_mdib_file)
        self.assertEqual(len(self.service.list_metrics()), 1)
        self.assertIn("m.back_to_json", self.service.list_metrics())


RUNTIME_COUNTERS = ("MdibVersion", "SequenceId", "InstanceId", "DescriptionVersion", "StateVersion")


def canonical(payload: bytes) -> str:
    """An exported MDIB without the runtime counters on its four aggregate elements.

    Sibling order is not data here (the library writes entities in dictionary order), so
    every element's children are compared sorted.
    """
    root = etree.fromstring(payload)
    mdib = root[0]
    for node in (root, mdib, mdib.find(f"{{{PM}}}MdDescription"), mdib.find(f"{{{PM}}}MdState")):
        for name in RUNTIME_COUNTERS:
            node.attrib.pop(name, None)

    def text(node) -> str:
        children = sorted(text(child) for child in node if isinstance(child.tag, str))
        return f"<{node.tag} {sorted(node.attrib.items())} {(node.text or '').strip()!r}>{''.join(children)}</>"

    return text(root)


class AdoptedMdibTests(unittest.TestCase):
    """A file MDIB joins the normal bookkeeping without any of its data changing."""

    def setUp(self) -> None:
        self.io = importlib.import_module("sdctoolbox.mdib_io")
        self.service = ProviderService(instance_name=f"adopt-{self._testMethodName}")
        self.addCleanup(self.service.stop)

    def start(self, data: bytes | None = None) -> None:
        from mdib_test_support import adoption_mdib

        self.data = data or adoption_mdib()
        self.service.start(self.io.parse_mdib(self.data))

    def test_loading_changes_nothing_but_runtime_counters(self) -> None:
        from mdib_test_support import adoption_mdib

        for data in (original_mdib(), adoption_mdib(), (Path(__file__).resolve().parent.parent / "presets" / "mdib-demo-monitor.xml").read_bytes()):
            with self.subTest(size=len(data)):
                service = ProviderService(instance_name=f"adopt-unchanged-{len(data)}")
                service.start(self.io.parse_mdib(data))
                try:
                    self.assertEqual(canonical(self.io.mdib_bytes(service.mdib)),
                                     canonical(self.io.mdib_bytes(self.io.parse_mdib(data))))
                finally:
                    service.stop()

    def test_origin_flag(self) -> None:
        self.start()
        self.assertTrue(self.service.from_mdib_file)
        bootstrap = ProviderService(instance_name="adopt-bootstrap")
        bootstrap.start()
        self.addCleanup(bootstrap.stop)
        self.assertFalse(bootstrap.from_mdib_file)

    def test_imported_metrics_and_alarms_join_the_bookkeeping(self) -> None:
        self.start()
        metrics = self.service.list_metrics()
        self.assertEqual(metrics["demo.rate"].section, "demo.channel")
        self.assertEqual(self.service.sections()["demo.channel"], "demo.channel")
        alerts = self.service.list_alerts()
        self.assertEqual((alerts["demo.limit"].source_handle, alerts["demo.limit"].lower_limit,
                          alerts["demo.limit"].upper_limit), ("demo.rate", Decimal("5"), Decimal("40")))
        self.assertFalse(alerts["demo.alarm"].has_limits)
        self.assertEqual(self.service.signal_handles_for("demo.limit"), ["demo.limit.signal"])

    def test_imported_limit_alarm_follows_its_source_only_after_a_change(self) -> None:
        self.start()
        self.assertFalse(self.service.alert_present("demo.limit"))
        self.service.set_value("demo.rate", Decimal("45"))
        self.assertTrue(self.service.alert_present("demo.limit"))
        self.assertEqual(self.service.signal_states("demo.limit")[0].presence, str(pm_types.AlertSignalPresence.ON))
        self.service.set_value("demo.rate", Decimal("20"))
        self.assertFalse(self.service.alert_present("demo.limit"))

    def test_other_imported_alarms_are_not_rederived_by_unrelated_changes(self) -> None:
        self.start()
        self.service.set_value("demo.rate", Decimal("45"))
        self.service.set_alert_presence("demo.alarm", False)
        self.assertTrue(self.service.alert_present("demo.paused"), "a paused but present imported alarm was rewritten")
        self.assertFalse(self.service.alert_present("demo.alarm"))
        self.service.set_alert_presence("demo.alarm", True)
        self.assertTrue(self.service.alert_present("demo.alarm"))
        self.assertEqual(self.service.signal_states("demo.alarm")[0].presence, str(pm_types.AlertSignalPresence.ON))
        present = self.service.mdib.entities.by_handle("demo.alerts").state
        self.assertIn("demo.limit", present.PresentPhysiologicalAlarmConditions)
        self.assertIn("demo.alarm", present.PresentTechnicalAlarmConditions)

    def test_imported_signal_acknowledge_and_delegation(self) -> None:
        self.start()
        self.service.set_value("demo.rate", Decimal("45"))
        self.assertEqual(self.service.acknowledge_alert("demo.limit"), 1)
        self.assertEqual(self.service.signal_states("demo.limit")[0].presence, str(pm_types.AlertSignalPresence.ACK))


MONITOR = Path(__file__).resolve().parent.parent / "presets" / "mdib-demo-monitor.xml"


def bare_mds() -> bytes:
    """Only an MDS: no channel, alert system, SCO or context to put anything in."""
    root = etree.fromstring(original_mdib())
    description, states = root[0]
    mds = description[0]
    for child in list(mds):
        if child.tag != f"{{{PM}}}Type":
            mds.remove(child)
    for state in list(states):
        if state.get("DescriptorHandle") != "demo.root":
            states.remove(state)
    return etree.tostring(root)


class EditableMdibTests(unittest.TestCase):
    """Everything a JSON preset can do also works on a graph loaded from a file."""

    def setUp(self) -> None:
        self.io = importlib.import_module("sdctoolbox.mdib_io")
        self.service = ProviderService(instance_name=f"edit-{self._testMethodName}")
        self.addCleanup(self.service.stop)

    def start(self, data: bytes) -> None:
        self.service.start(self.io.parse_mdib(data))

    def invoke(self, handle: str, value):
        operation = self.service._provider.get_operation_by_handle(handle)
        self.assertIsNotNone(operation, handle)
        return operation.execute_operation(None, SimpleNamespace(argument=value)).invocation_state

    def assert_exportable(self) -> None:
        self.io.parse_mdib(self.io.mdib_bytes(self.service.mdib))

    def test_metrics_go_into_existing_and_new_sections(self) -> None:
        self.start(MONITOR.read_bytes())
        into_existing = self.service.add_metric(MetricSpec("SpO2", MetricKind.NUMBER, section="monitor.vitals.channel",
                                                           initial_value=Decimal("97")))
        self.assertEqual(self.service.mdib.entities.by_handle(into_existing).parent_handle, "monitor.vitals.channel")
        into_new = self.service.add_metric(MetricSpec("Mode", MetricKind.TEXT, section="Settings"))
        channel = self.service.mdib.entities.by_handle(self.service.mdib.entities.by_handle(into_new).parent_handle)
        self.assertEqual(self.service.mdib.entities.by_handle(channel.parent_handle).parent_handle, "monitor")
        unsectioned = self.service.add_metric(MetricSpec("Loose", MetricKind.NUMBER))
        self.assertIsNotNone(self.service.mdib.entities.by_handle(unsectioned))
        for kind in (MetricKind.CHOICE, MetricKind.WAVEFORM, MetricKind.DISTRIBUTION):
            extra = {"allowed_values": ("A", "B")} if kind is MetricKind.CHOICE else {}
            self.service.add_metric(MetricSpec(f"New {kind.value}", kind, **extra))
        self.assertEqual(self.service.get_value(into_existing), Decimal("97"))
        self.assert_exportable()

    def test_control_creates_a_sco_when_the_file_has_none(self) -> None:
        self.start(MONITOR.read_bytes())
        operation = self.service.enable_control("monitor.pulse")
        self.assertEqual(self.service.mdib.entities.by_handle(
            self.service.mdib.entities.by_handle(operation).parent_handle).node_type, pm_types_qname("ScoDescriptor"))
        self.assertEqual(self.invoke(operation, Decimal("80")), msg_types.InvocationState.FINISHED)
        self.assertEqual(self.service.get_value("monitor.pulse"), Decimal("80"))
        self.service.disable_control("monitor.pulse")
        self.assertEqual(self.invoke(operation, Decimal("81")), msg_types.InvocationState.FAILED)
        self.assert_exportable()

    def test_imported_operation_can_be_switched_off_and_on(self) -> None:
        self.start(original_mdib())
        self.service.disable_control("demo.rate")
        self.assertEqual(self.invoke("demo.set", Decimal("15")), msg_types.InvocationState.FAILED)
        self.service.enable_control("demo.rate")
        self.assertEqual(self.invoke("demo.set", Decimal("15")), msg_types.InvocationState.FINISHED)

    def test_alarms_on_a_graph_without_alert_system(self) -> None:
        self.start(bare_mds())
        source = self.service.add_metric(MetricSpec("Pressure", MetricKind.NUMBER, initial_value=Decimal("10")))
        from sdctoolbox.model import AlertSpec
        alarm = self.service.add_alert(AlertSpec("Too high", source, upper_limit=Decimal("20"), delegable=True))
        self.service.set_value(source, Decimal("25"))
        self.assertTrue(self.service.alert_present(alarm))
        signal = self.service.signal_handles_for(alarm)[0]
        self.service.set_signal_delegated(signal, delegated=True)
        self.service.remove_alert(alarm)
        self.assertNotIn(alarm, self.service.list_alerts())
        self.assert_exportable()

    def test_actions_with_effects(self) -> None:
        self.start(MONITOR.read_bytes())
        from sdctoolbox.model import ActionSpec
        action = self.service.add_action(ActionSpec("Reset pulse", "monitor", effects={"monitor.pulse": Decimal("60")}))
        self.assertEqual(self.invoke(action, None), msg_types.InvocationState.FINISHED)
        self.assertEqual(self.service.get_value("monitor.pulse"), Decimal("60"))
        self.service.set_value("monitor.pulse", Decimal("70"))
        self.service.run_action(action)
        self.assertEqual(self.service.get_value("monitor.pulse"), Decimal("60"))
        self.service.remove_action(action)
        self.assert_exportable()

    def test_contexts_are_created_when_missing_and_reuse_imported_ones(self) -> None:
        from sdctoolbox.model import LocationInfo, PatientInfo
        self.start(MONITOR.read_bytes())
        self.service.set_location(LocationInfo(facility="NEW", bed="7"))
        self.assertEqual(self.service.get_location().bed, "7")
        self.service.set_patient(PatientInfo(given_name="Synthetic"))
        self.assertEqual(self.service.get_patient().given_name, "Synthetic")
        self.service.clear_patient()
        self.assertEqual(self.service.get_patient().given_name, "")
        self.assert_exportable()

        imported = ProviderService(instance_name="edit-contexts-imported")
        imported.start(self.io.parse_mdib(original_mdib()))
        self.addCleanup(imported.stop)
        imported.set_patient(PatientInfo(given_name="Next"))
        patient = imported.mdib.entities.by_handle("demo.patient")
        self.assertEqual(len(patient.states), 3, "history is kept and the imported descriptor reused")
        imported.set_location(LocationInfo(facility="OTHER"))
        self.assertEqual(imported.get_location().facility, "OTHER")

    def test_runtime_states_reload_from_an_mdib_file(self) -> None:
        from sdctoolbox import config
        from sdctoolbox.model import AlertPriority, AlertSpec

        self.start(bare_mds())
        rate = self.service.add_metric(MetricSpec("Rate", MetricKind.NUMBER, controllable=True,
                                                  minimum=Decimal("0"), maximum=Decimal("100"), initial_value=Decimal("50")))
        mode = self.service.add_metric(MetricSpec("Mode", MetricKind.CHOICE, controllable=True,
                                                  allowed_values=("A", "B", "C"), initial_value="A"))
        alarm = self.service.add_alert(AlertSpec("Rate high", rate, upper_limit=Decimal("80")))
        self.service.set_allowed_range(rate, Decimal("20"), Decimal("40"))
        self.service.set_allowed_values(mode, ["A", "B"])
        self.service.set_metric_active(mode, active=False)
        self.service.set_alert_limits(alarm, Decimal("10"), Decimal("90"))
        self.service.set_alert_priority(alarm, AlertPriority.HIGH)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "states.xml"
            config.save(self.service, target)
            reloaded = ProviderService(instance_name="states-reloaded")
            reloaded.start(self.io.parse_mdib(target.read_bytes()))
        self.addCleanup(reloaded.stop)

        metrics, alert = reloaded.list_metrics(), reloaded.list_alerts()[alarm]
        self.assertEqual(metrics[rate].effective_allowed_range(), (Decimal("20"), Decimal("40")))
        self.assertEqual((metrics[rate].minimum, metrics[rate].maximum), (Decimal("0"), Decimal("100")))
        self.assertEqual(metrics[mode].operation_values, ("A", "B"))
        self.assertFalse(metrics[mode].active)
        self.assertEqual((alert.lower_limit, alert.upper_limit), (Decimal("10"), Decimal("90")))
        self.assertEqual(alert.effective_max_limits(), (Decimal("0"), Decimal("100")))
        self.assertIs(alert.actual_priority, AlertPriority.HIGH)
        self.assertIs(alert.priority, AlertPriority.MEDIUM)
        # Still editable after the reload.
        reloaded.set_allowed_range(rate, Decimal("10"), Decimal("60"))
        reloaded.set_alert_limits(alarm, Decimal("0"), Decimal("100"))

    def test_an_edited_graph_reloads_unchanged(self) -> None:
        from sdctoolbox import config
        from sdctoolbox.model import ActionSpec, AlertSpec, LocationInfo, PatientInfo

        self.start(bare_mds())
        pulse = self.service.add_metric(MetricSpec("Pulse", MetricKind.NUMBER, section="Vitals", controllable=True,
                                                   minimum=Decimal("0"), maximum=Decimal("250"), initial_value=Decimal("70")))
        alarm = self.service.add_alert(AlertSpec("Pulse high", pulse, upper_limit=Decimal("120")))
        action = self.service.add_action(ActionSpec("Reset", "demo.root", effects={pulse: Decimal("60")}, note="back to rest"))
        self.service.set_patient(PatientInfo(given_name="Synthetic"))
        self.service.set_location(LocationInfo(facility="F", bed="2"))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "edited.xml"
            config.save(self.service, target, behaviour=True)
            reloaded = ProviderService(instance_name="edit-reloaded")
            reloaded.start(self.io.parse_mdib(target.read_bytes()))
        self.addCleanup(reloaded.stop)

        self.assertEqual(canonical(self.io.mdib_bytes(reloaded.mdib)), canonical(self.io.mdib_bytes(self.service.mdib)))
        self.assertEqual(set(reloaded.list_metrics()), set(self.service.list_metrics()))
        self.assertEqual(reloaded.list_alerts()[alarm].upper_limit, Decimal("120"))
        self.assertEqual((reloaded.list_actions()[action].effects, reloaded.list_actions()[action].note),
                         ({pulse: Decimal("60")}, "back to rest"))
        self.assertEqual(reloaded.get_patient().given_name, "Synthetic")
        self.assertEqual(reloaded.get_location().bed, "2")
        operation = reloaded.operation_handle_for(pulse)
        self.assertIsNotNone(operation)
        reloaded_invoke = lambda handle, value: reloaded._provider.get_operation_by_handle(handle).execute_operation(
            None, SimpleNamespace(argument=value)).invocation_state
        self.assertEqual(reloaded_invoke(operation, Decimal("130")), msg_types.InvocationState.FINISHED)
        self.assertTrue(reloaded.alert_present(alarm), "the reloaded alarm follows its source")
        self.assertEqual(reloaded_invoke(action, None), msg_types.InvocationState.FINISHED)
        self.assertEqual(reloaded.get_value(pulse), Decimal("60"))

    def test_removing_a_metric_unregisters_its_operation_from_a_second_sco(self) -> None:
        root = etree.fromstring(original_mdib())
        description, states = root[0]
        description.append(etree.fromstring(etree.tostring(description[0]).replace(b"demo.", b"other.")))
        for state in list(states):
            states.append(etree.fromstring(etree.tostring(state).replace(b"demo.", b"other.")))
        self.start(etree.tostring(root))
        self.assertIsNotNone(self.service._provider.get_operation_by_handle("other.set"))
        self.service.remove_metric("other.rate")
        self.assertIsNone(self.service._provider.get_operation_by_handle("other.set"))
        self.assertIsNotNone(self.service._provider.get_operation_by_handle("demo.set"), "the first Sco keeps its own")

    def test_removing_an_imported_metric_takes_its_dependents(self) -> None:
        self.start(original_mdib())
        self.service.remove_metric("demo.rate")
        for handle in ("demo.rate", "demo.alarm", "demo.signal", "demo.set", "demo.disabled"):
            self.assertIsNone(self.service.mdib.entities.by_handle(handle), handle)
        self.assertNotIn("demo.alarm", self.service.list_alerts())
        self.assert_exportable()

    def test_generator_drives_new_and_imported_waveforms_only_when_started(self) -> None:
        self.start(MONITOR.read_bytes())
        before = self.service.get_samples("monitor.trace")
        self.assertFalse(self.service.generator_running)
        self.service.start_generator()
        self.assertTrue(wait_until(lambda: self.service.get_samples("monitor.trace") != before, timeout=5.0))
        self.service.stop_generator()

    def test_json_profile_appends_and_replaces(self) -> None:
        from sdctoolbox import config

        self.start(original_mdib())
        profile = config.parse({"version": 3, "metrics": [{"label": "Appended", "kind": "number", "section": "demo.channel"}],
                                "alerts": [{"label": "Appended alarm", "watches": "m.appended", "upper_limit": "3"}]})
        self.service.import_profile(profile, replace=False)
        self.assertIn("m.appended", self.service.list_metrics())
        self.assertIn("demo.rate", self.service.list_metrics())
        self.assertEqual(self.service.mdib.entities.by_handle("m.appended").parent_handle, "demo.channel")
        self.assertTrue(self.service.from_mdib_file)
        self.service.import_profile(profile)
        self.assertFalse(self.service.from_mdib_file)
        self.assertNotIn("demo.rate", self.service.list_metrics())


BEHAVIOUR = "urn:sdc-testing-toolbox:behaviour"


class BehaviourExtensionTests(unittest.TestCase):
    """Toolbox behaviour travels in the exported file on request, and never on the network."""

    def setUp(self) -> None:
        self.io = importlib.import_module("sdctoolbox.mdib_io")
        self.service = ProviderService(instance_name=f"behaviour-{self._testMethodName}")
        self.addCleanup(self.service.stop)
        self.service.start(self.io.parse_mdib(original_mdib()))

    def configure(self) -> tuple[str, str]:
        from sdctoolbox.model import ActionSpec, WaveformShape

        wave = self.service.add_metric(MetricSpec("Pleth", MetricKind.WAVEFORM, shape=WaveformShape.SQUARE,
                                                  cycle_seconds=Decimal("2.5"), minimum=Decimal("0"), maximum=Decimal("10")))
        self.service.stop_generator()
        action = self.service.add_action(ActionSpec("Run", "demo.root", effects={"demo.mode": "RUN", "demo.rate": Decimal("20")},
                                                    note="starts the demo"))
        return wave, action

    def export(self, behaviour: bool) -> bytes:
        from sdctoolbox import config

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "out.xml"
            config.save(self.service, target, behaviour=behaviour)
            return target.read_bytes()

    def test_plain_export_has_no_toolbox_extensions(self) -> None:
        self.configure()
        payload = self.export(behaviour=False)
        self.assertNotIn(BEHAVIOUR.encode(), payload)
        self.assertIn(b"urn:sdc-testing-toolbox:test-extension", payload, "foreign extensions must survive")

    def test_behaviour_round_trips_and_stays_off_the_live_mdib(self) -> None:
        wave, action = self.configure()
        payload = self.export(behaviour=True)
        root = etree.fromstring(payload)
        marked = root.findall(f".//{{{EXT}}}Extension/{{{BEHAVIOUR}}}*")
        self.assertTrue(marked)
        self.assertTrue(all(node.get(f"{{{EXT}}}MustUnderstand") == "false" for node in marked))
        self.assertIn(b"urn:sdc-testing-toolbox:test-extension", payload)

        reloaded = ProviderService(instance_name="behaviour-reloaded")
        reloaded.start(self.io.parse_mdib(payload))
        self.addCleanup(reloaded.stop)
        live = self.io.mdib_bytes(reloaded.mdib)
        self.assertNotIn(BEHAVIOUR.encode(), live, "toolbox behaviour must not be published")
        self.assertIn(b"urn:sdc-testing-toolbox:test-extension", live)
        spec = reloaded.list_metrics()[wave]
        self.assertEqual((spec.shape.value, spec.cycle_seconds), ("square", Decimal("2.5")))
        self.assertIn(b'CycleSeconds="2.5"', payload)

        # An export from before cycles were durations counted samples at its own period.
        older = payload.replace(b'CycleSeconds="2.5"', b'CycleSamples="25"')
        legacy = ProviderService(instance_name="behaviour-legacy")
        legacy.start(self.io.parse_mdib(older))
        self.addCleanup(legacy.stop)
        self.assertEqual(legacy.list_metrics()[wave].cycle_seconds, Decimal("2.5"))
        restored = reloaded.list_actions()[action]
        self.assertEqual(restored.effects, {"demo.mode": "RUN", "demo.rate": Decimal("20")})
        self.assertEqual(restored.note, "starts the demo")
        operation = reloaded._provider.get_operation_by_handle(action)
        self.assertEqual(operation.execute_operation(None, SimpleNamespace(argument=None)).invocation_state,
                         msg_types.InvocationState.FINISHED)
        self.assertEqual(reloaded.get_value("demo.mode"), "RUN")
        self.assertEqual(reloaded.get_value("demo.rate"), Decimal("20"))
        self.assertIn(BEHAVIOUR.encode(), self.export_from(reloaded), "behaviour survives a second round trip")

    def export_from(self, service) -> bytes:
        from sdctoolbox import config

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "again.xml"
            config.save(service, target, behaviour=True)
            return target.read_bytes()

    def reload(self, payload: bytes) -> ProviderService:
        service = ProviderService(instance_name=f"reloaded-{self._testMethodName}")
        self.addCleanup(service.stop)
        service.start(self.io.parse_mdib(payload))
        return service

    def test_generated_waveforms_are_marked_and_generate_again_after_reload(self) -> None:
        sine = self.service.add_metric(MetricSpec("Sine", MetricKind.WAVEFORM, minimum=Decimal("0"), maximum=Decimal("10")))
        self.assertTrue(self.service.generator_running)
        payload = self.export(behaviour=True)
        markers = {node.getparent().getparent().get("Handle")
                   for node in etree.fromstring(payload).iter(f"{{{BEHAVIOUR}}}Generator")}
        self.assertIn(sine, markers, "a default sine is generated too, so it needs a marker")
        self.assertIn("demo.wave", markers, "an imported waveform the running generator drives is marked")
        self.assertTrue(self.reload(payload).generator_running)
        self.assertFalse(self.reload(self.export(behaviour=False)).generator_running, "a plain file stays static")

    def test_unmarked_waveforms_keep_their_file_samples_while_others_generate(self) -> None:
        wave, _ = self.configure()  # generator stopped: the imported demo.wave is not driven
        exported = self.service.get_samples("demo.wave")
        payload = self.export(behaviour=True)
        markers = {node.getparent().getparent().get("Handle")
                   for node in etree.fromstring(payload).iter(f"{{{BEHAVIOUR}}}Generator")}
        self.assertEqual(markers, {wave})
        reloaded = self.reload(payload)
        self.assertTrue(reloaded.generator_running)
        reloaded.stop_generator()
        reloaded._sample_generator.publish_once(force=True)
        self.assertEqual(reloaded.get_samples("demo.wave"), exported)
        self.assertTrue(reloaded.get_samples(wave))

    def test_action_with_a_refused_effect_reloads_and_is_still_refused(self) -> None:
        from sdctoolbox.model import ActionSpec

        action = self.service.add_action(ActionSpec("Bad", "demo.root", effects={"demo.mode": "INVALID"}))
        reloaded = ProviderService(instance_name="behaviour-refused")
        reloaded.start(self.io.parse_mdib(self.export(behaviour=True)))
        self.addCleanup(reloaded.stop)
        self.assertEqual(reloaded.list_actions()[action].effects, {"demo.mode": "INVALID"})
        operation = reloaded._provider.get_operation_by_handle(action)
        self.assertEqual(operation.execute_operation(None, SimpleNamespace(argument=None)).invocation_state,
                         msg_types.InvocationState.FAILED)
        self.assertEqual(reloaded.get_value("demo.mode"), "RUN")

    def test_malformed_toolbox_extension_names_the_item(self) -> None:
        from sdctoolbox.config import ConfigError

        bad = original_mdib().replace(
            b'<pm:Operation Handle="demo.action" xsi:type="pm:ActivateOperationDescriptor" OperationTarget="demo.root"/>',
            b'<pm:Operation Handle="demo.action" xsi:type="pm:ActivateOperationDescriptor" OperationTarget="demo.root">'
            b'<ext:Extension><tb:Action xmlns:tb="urn:sdc-testing-toolbox:behaviour" ext:MustUnderstand="false">'
            b'<tb:Effect Value="1"/></tb:Action></ext:Extension></pm:Operation>',
        )
        with self.assertRaisesRegex(ConfigError, "demo.action"):
            self.io.parse_mdib(bad)

    def test_export_filters(self) -> None:
        from sdctoolbox import config

        self.assertEqual(config.export_path("x", config.MDIB_BEHAVIOUR_FILE_FILTER), Path("x.xml"))
        self.assertIn(config.MDIB_BEHAVIOUR_FILE_FILTER, config.FILE_FILTER)


def pm_types_qname(name: str):
    from sdc11073.xml_types import pm_qnames
    return getattr(pm_qnames, name)


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
