"""Simulated faults: per-metric activation, validity and mode, and the device-wide faults.

A consumer under test must cope with a sensor in standby, a questionable value, a slow or
refusing device, missing reports and a device that vanishes. These checks drive the provider
service directly and through a real consumer, and the Advanced mode that shows the controls.

Usage:  .venv/Scripts/python.exe tests/faults.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from script_support import wait_until  # noqa: E402
from sdc11073.definitions_sdc import SdcV1Definitions  # noqa: E402
from sdc11073.xml_types import msg_types  # noqa: E402

from sdctoolbox import constants  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService, DiscoveredDevice  # noqa: E402
from sdctoolbox.handlers import SIMULATED_REFUSAL, MetricFault, what_consumers_see  # noqa: E402
from sdctoolbox.mdib_io import mdib_bytes, parse_mdib  # noqa: E402
from sdctoolbox.model import ActionSpec, MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402


def state(service: ProviderService, handle: str):  # noqa: ANN201 - an sdc11073 state
    return service.mdib.entities.by_handle(handle).state


def quality(service: ProviderService, handle: str) -> tuple[str, str, str]:
    current = state(service, handle)
    metric_quality = current.MetricValue.MetricQuality
    return current.ActivationState, metric_quality.Validity, metric_quality.Mode


def remote_set(service: ProviderService, metric: str, value: object):  # noqa: ANN201 - an ExecuteResult
    """Run the registered set handler the way a consumer's request would."""
    operation = service._sco._registered_operations[service.operation_handle_for(metric)]  # noqa: SLF001
    params = SimpleNamespace(operation_instance=operation, operation_request=SimpleNamespace(argument=value))
    return service._handler(params)  # noqa: SLF001


def connect(consumer: ConsumerService, service: ProviderService):  # noqa: ANN201 - a RemoteDevice
    """Connect without a network scan, as tests/provider_conformance.py does."""
    addresses = list(service._provider.get_xaddrs())  # noqa: SLF001
    announced = SimpleNamespace(epr=service.epr.urn, x_addrs=addresses, types=[SdcV1Definitions.MedicalDeviceType])
    return consumer.connect(DiscoveredDevice(service.epr.urn, tuple(addresses), (), service=announced))


class MetricFaultTests(unittest.TestCase):
    """Activation, validity and mode are MDIB state that every later write keeps."""

    def setUp(self) -> None:
        self.service = ProviderService(instance_name="faults-metric")
        self.service.start()
        self.rate = self.service.add_metric(MetricSpec("Rate", MetricKind.NUMBER, controllable=True,
                                                       initial_value=Decimal(60)))

    def tearDown(self) -> None:
        self.service.stop()

    def test_a_fault_survives_local_remote_and_action_writes(self) -> None:
        service, rate = self.service, self.rate
        told = service.set_metric_fault(rate, activation="StndBy", validity="Qst", mode="Test")
        self.assertEqual(told, MetricFault("StndBy", "Qst", "Test"))
        self.assertEqual(quality(service, rate), ("StndBy", "Qst", "Test"))
        service.set_value(rate, Decimal(61))
        self.assertEqual(quality(service, rate), ("StndBy", "Qst", "Test"), "a local edit keeps the fault")
        result = remote_set(service, rate, Decimal(62))
        self.assertIs(result.invocation_state, msg_types.InvocationState.FINISHED)
        self.assertEqual(quality(service, rate), ("StndBy", "Qst", "Test"), "a remote write keeps the fault")
        action = service.add_action(ActionSpec("Reset", constants.MDS_HANDLE, effects={rate: Decimal(63)}))
        service.run_action(action)
        self.assertEqual(state(service, rate).MetricValue.Value, Decimal(63))
        self.assertEqual(quality(service, rate), ("StndBy", "Qst", "Test"), "an action's effect keeps the fault")

    def test_defaults_end_a_fault_and_on_off_is_the_ordinary_switch(self) -> None:
        service, rate = self.service, self.rate
        service.set_metric_fault(rate, activation="Fail", validity="Inv")
        self.assertEqual(service.faults_on, 1)
        service.set_metric_fault(rate, validity="Vld")
        self.assertEqual(service.metric_faults()[rate], MetricFault(activation="Fail"))
        service.set_metric_fault(rate, activation="Off")
        self.assertEqual(service.metric_faults(), {}, "Off is not a fault")
        self.assertFalse(service.list_metrics()[rate].active, "Off switches the metric off as before")
        service.set_metric_fault(rate, activation="NotRdy")
        self.assertTrue(service.list_metrics()[rate].active, "a fault other than Off is not switched off")
        service.set_metric_active(rate, active=True)
        self.assertEqual(service.metric_faults(), {}, "switching on ends an activation fault")
        self.assertEqual(state(service, rate).ActivationState, "On")
        with self.assertRaises(ValueError) as caught:
            service.set_metric_fault(rate, validity="Wobbly")
        self.assertIn("Qst", str(caught.exception))
        with self.assertRaises(KeyError):
            service.set_metric_fault("m.nothing", validity="Qst")

    def test_the_generator_keeps_a_mode_and_stops_for_a_sensor_in_standby(self) -> None:
        service = self.service
        wave = service.add_metric(MetricSpec("Wave", MetricKind.WAVEFORM))
        self.assertEqual(service.metric_fault(wave).mode, "Demo", "generated samples are Demo data")
        service.set_metric_fault(wave, mode="Test", validity="Calib")
        service._publish_one_block()  # noqa: SLF001
        self.assertEqual(quality(service, wave)[1:], ("Calib", "Test"))
        service.set_metric_fault(wave, activation="StndBy")
        before = state(service, wave).StateVersion
        service._publish_one_block()  # noqa: SLF001
        self.assertEqual(state(service, wave).StateVersion, before, "no samples while in standby")
        service.set_metric_fault(wave, mode="Demo")
        self.assertEqual(service.metric_faults()[wave], MetricFault("StndBy", "Calib"), "Demo is a waveform's usual mode")

    def test_an_ongoing_measurement_passes_the_mdib_check(self) -> None:
        from sdctoolbox import mdib_check  # noqa: PLC0415

        self.service.set_metric_fault(self.rate, validity="Ong")
        rules = [finding.rule for finding in mdib_check.check(self.service.mdib) if finding.handle == self.rate]
        self.assertNotIn("10701 TR0216", rules, "an Ong value says since when")

    def test_reset_ends_every_fault(self) -> None:
        service, rate = self.service, self.rate
        service.set_metric_fault(rate, activation="Shtdn", validity="Oflw", mode="Demo")
        service.set_device_faults(answer_delay_ms=250, refuse_writes=True, pause_reports=True)
        self.assertEqual(service.faults_on, 4)
        service.reset_faults()
        self.assertEqual((service.faults_on, service.metric_faults()), (0, {}))
        self.assertEqual(quality(service, rate), ("On", "Vld", "Real"))
        self.assertEqual(service.faults.answer_delay_ms, 0)
        with self.assertRaises(ValueError):
            service.set_device_faults(answer_delay_ms=-1)

    def test_a_fault_comes_back_with_an_exported_mdib(self) -> None:
        self.service.set_metric_fault(self.rate, activation="StndBy", validity="Qst")
        xml = mdib_bytes(self.service.mdib, self.service.behaviour_extensions())
        loaded = ProviderService(instance_name="faults-loaded")
        loaded.start(parse_mdib(xml))
        try:
            self.assertEqual(loaded.metric_faults(), {self.rate: MetricFault("StndBy", "Qst")})
            self.assertEqual(loaded.faults_on, 1, "device-wide faults are never saved")
        finally:
            loaded.stop()

    def test_refused_writes_say_why(self) -> None:
        self.service.set_device_faults(refuse_writes=True)
        result = remote_set(self.service, self.rate, Decimal(70))
        self.assertIs(result.invocation_state, msg_types.InvocationState.FAILED)
        self.assertEqual(result.error_message, SIMULATED_REFUSAL)
        self.assertEqual(state(self.service, self.rate).MetricValue.Value, Decimal(60))
        self.service.set_device_faults(refuse_writes=False)
        self.assertIs(remote_set(self.service, self.rate, Decimal(70)).invocation_state,
                      msg_types.InvocationState.FINISHED)

    def test_what_consumers_see(self) -> None:
        self.assertEqual(what_consumers_see(MetricFault()), "normal")
        self.assertEqual(what_consumers_see(MetricFault("StndBy", "Inv")), "the sensor in standby: no value")
        self.assertEqual(what_consumers_see(MetricFault("Fail"), sample_array=True), "a failed measurement: no samples")
        self.assertEqual(what_consumers_see(MetricFault(validity="Qst")), "a value marked questionable")
        self.assertIn("test data", what_consumers_see(MetricFault(validity="Qst", mode="Test")))


class ConsumerTests(unittest.TestCase):
    """What a real consumer sees of the faults, over the network."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.service = ProviderService(instance_name="faults-consumer")
        cls.service.start()
        cls.rate = cls.service.add_metric(MetricSpec("Rate", MetricKind.NUMBER, controllable=True,
                                                     minimum=Decimal(0), maximum=Decimal(300),
                                                     initial_value=Decimal(60)))
        cls.consumer = ConsumerService(ip=cls.service.ip)
        cls.consumer.start()
        cls.remote = connect(cls.consumer, cls.service)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.remote.close()
        cls.consumer.stop()
        cls.service.stop()

    def tearDown(self) -> None:
        self.service.reset_faults()

    def seen(self) -> object:
        return self.remote.metrics((self.rate,))[self.rate]

    def test_the_consumer_sees_standby_and_questionable(self) -> None:
        self.service.set_metric_fault(self.rate, activation="StndBy", validity="Qst")
        self.assertTrue(wait_until(lambda: (self.seen().activation, self.seen().validity) == ("StndBy", "Qst"),
                                   timeout=10))
        self.service.set_value(self.rate, Decimal(65))
        self.assertTrue(wait_until(lambda: self.seen().value == Decimal(65), timeout=10))
        self.assertEqual((self.seen().activation, self.seen().validity), ("StndBy", "Qst"))

    def test_a_refused_write_reaches_the_consumer_with_its_reason(self) -> None:
        self.service.set_device_faults(refuse_writes=True)
        answer = self.remote.set_value(self.rate, Decimal(80))
        self.assertIs(answer, msg_types.InvocationState.FAILED)
        self.assertIn("simulated fault", self.remote.last_invocation_detail)

    def test_answers_are_delayed(self) -> None:
        self.service.set_device_faults(answer_delay_ms=500)
        started = time.monotonic()
        self.assertIs(self.remote.set_value(self.rate, Decimal(81)), msg_types.InvocationState.FINISHED)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        started = time.monotonic()
        self.remote._consumer.get_service_client.get_md_state()  # noqa: SLF001
        self.assertGreaterEqual(time.monotonic() - started, 0.5, "Get requests wait too")

    def test_paused_reports_catch_up(self) -> None:
        self.service.set_device_faults(pause_reports=True)
        self.service.set_value(self.rate, Decimal(90))
        time.sleep(1.0)
        self.assertNotEqual(self.seen().value, Decimal(90), "no report while paused")
        self.service.set_device_faults(pause_reports=False)
        self.assertTrue(wait_until(lambda: self.seen().value == Decimal(90), timeout=10), "caught up on resume")


class DeviceLifeTests(unittest.TestCase):
    """Dropping subscriptions and leaving without Bye; each test gets a fresh device."""

    def setUp(self) -> None:
        self.service = ProviderService(instance_name="faults-life")
        self.service.start()
        self.consumer = ConsumerService(ip=self.service.ip)
        self.consumer.start()
        self.remote = connect(self.consumer, self.service)
        self.address = self.service._provider.get_xaddrs()[0]  # noqa: SLF001

    def tearDown(self) -> None:
        self.remote.close()
        self.consumer.stop()
        self.service.stop()

    def test_drop_subscriptions_forgets_them(self) -> None:
        self.assertGreater(self.service.drop_subscriptions(), 0)
        self.assertEqual(self.service.drop_subscriptions(), 0)

    def test_leave_without_bye_and_come_back(self) -> None:
        epr = self.service.epr.urn
        discovery = self.service._discovery  # noqa: SLF001
        self.service.set_device_faults(silent=True)
        self.assertNotIn(epr, discovery._local_services, "no Probe or Resolve answer")  # noqa: SLF001
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(self.address, data=b"<x/>", timeout=5)  # noqa: S310 - a local test URL
        self.assertEqual(self.service.drop_subscriptions(), 0, "a crashed device forgets its subscribers")
        version = self.service._provider._hidden_service.metadata_version  # noqa: SLF001
        self.service.set_device_faults(silent=False)
        self.assertGreater(discovery._local_services[epr].metadata_version, version, "Hello with a new version")  # noqa: SLF001
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.address, data=b"<x/>", timeout=5)  # noqa: S310 - answered again, as a fault
        self.service.set_device_faults(silent=True)
        self.service.stop()  # stopping while gone must not fail, and sends no Bye
        self.service.start()


class AdvancedModeTests(unittest.TestCase):
    """View > Advanced mode and the Simple | Advanced switch show the Faults section and chips."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtCore import QSettings  # noqa: PLC0415
        from PySide6.QtWidgets import QApplication  # noqa: PLC0415

        from sdctoolbox.gui.main_window import MainWindow, remember_view  # noqa: PLC0415

        cls.app = QApplication.instance() or QApplication([])
        cls.service = ProviderService(instance_name="faults-gui")
        cls.service.start()
        cls.rate = cls.service.add_metric(MetricSpec("Rate", MetricKind.NUMBER, initial_value=Decimal(60)))
        cls.directory = tempfile.TemporaryDirectory()
        cls.settings_path = str(Path(cls.directory.name) / "settings.ini")
        cls.QSettings, cls.MainWindow, cls.remember_view = QSettings, MainWindow, staticmethod(remember_view)
        cls.window = MainWindow(cls.service)
        remember_view(cls.window, QSettings(cls.settings_path, QSettings.IniFormat))
        cls.window.show()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()
        cls.service.stop()
        cls.directory.cleanup()

    def tearDown(self) -> None:
        self.service.reset_faults()
        self.window.set_advanced_mode(False)

    def test_simple_by_default_and_menu_and_switch_agree(self) -> None:
        window, pane = self.window, self.window.provider_pane
        self.assertFalse(window.advanced_mode)
        self.assertFalse(pane.faults_section.isVisible(), "Simple mode hides the Faults section")
        window.mode_switch.advanced.click()
        self.assertTrue(window.advanced_mode_action.isChecked())
        self.assertTrue(pane.faults_section.isVisible())
        window.advanced_mode_action.setChecked(False)
        self.assertTrue(window.mode_switch.simple.isChecked())
        self.assertFalse(pane.faults_section.isVisible())

    def test_advanced_mode_is_remembered(self) -> None:
        self.window.set_advanced_mode(True)
        other = self.MainWindow(self.service)
        try:
            self.remember_view(other, self.QSettings(self.settings_path, self.QSettings.IniFormat))
            self.assertTrue(other.advanced_mode, "a new window starts in the mode the last one left")
        finally:
            other.close()
            # Closing the second window unhooked the shared service; give it back to the first.
            self.service.on_remote_control = self.window.provider_pane._remote_control.emit  # noqa: SLF001

    def test_faults_show_in_the_section_and_on_the_card_only_in_advanced_mode(self) -> None:
        pane = self.window.provider_pane
        self.service.set_metric_fault(self.rate, activation="StndBy")
        pane.refresh()
        self.assertIsNone(pane.board.card(self.rate).fault_chip, "no chip in Simple mode")
        self.window.set_advanced_mode(True)
        self.assertEqual(pane.board.card(self.rate).fault_chip.text(), "StndBy")
        self.assertEqual(pane.faults_section.chip_texts(), ["Advanced", "1 fault on"])
        self.assertIn("standby", pane.fault_table.item(0, 5).text())
        pane.refuse_button.click()
        self.assertTrue(self.service.faults.refuse_writes)
        self.assertEqual(pane.faults_section.chip_texts()[1], "2 faults on")
        pane.reset_faults_button.click()
        self.assertEqual(pane.faults_section.chip_texts()[1], "none on")


if __name__ == "__main__":
    unittest.main(verbosity=2)
