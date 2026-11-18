"""Work per edit must not grow with the size of the MDIB.

sdc11073 deep-copies every entity it hands out, so one whole-MDIB scan per metric turns a
large import quadratic. These tests count those copies instead of timing, so they are stable.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from perf_stress import big_device
from PySide6.QtWidgets import QApplication
from script_support import wait_until
from sdc11073.consumer.consumerimpl import SdcConsumer
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.mdib import ConsumerMdib
from sdc11073.httpserver.compression import GzipCompressionHandler
from sdc11073.mdib.mdibbase import EntityGetter
from sdc11073.provider.subscriptionmgr_async import AsyncioEventLoopThread

from sdctoolbox import config
from sdctoolbox.consumer_service import RemoteDevice
from sdctoolbox.gui.consumer_pane import ConsumerPane
from sdctoolbox.gui.main_window import MainWindow
from sdctoolbox.gui.provider_pane import ProviderPane
from sdctoolbox.model import MetricKind, MetricSpec
from sdctoolbox.mdib_io import parse_mdib
from sdctoolbox.native_mdib import adopt
from sdctoolbox.provider_service import ProviderService


def entity_copies(action) -> int:  # noqa: ANN001
    """Entities copied by this thread; the in-process provider and consumer copy on their own threads."""
    original = EntityGetter._mk_entity  # noqa: SLF001
    caller = threading.get_ident()
    count = 0

    def counting(self, descriptor):  # noqa: ANN001, ANN202
        nonlocal count
        count += threading.get_ident() == caller
        return original(self, descriptor)

    with patch.object(EntityGetter, "_mk_entity", counting):
        action()
    return count


class ScalingTests(unittest.TestCase):
    def per_metric(self, size: int, *, reset: bool) -> float:
        with ProviderService(ip="127.0.0.1", instance_name=f"scaling-{size}") as service:
            if reset:
                service.import_profile(big_device(size))
            return entity_copies(lambda: service.import_profile(big_device(size))) / size

    def assert_linear(self, *, reset: bool) -> None:
        small, large = self.per_metric(40, reset=reset), self.per_metric(160, reset=reset)
        # Quadratic work would make the large import copy about four times as much per metric.
        self.assertLess(large, small * 1.5, f"entity copies per metric: {small:.1f} at 40, {large:.1f} at 160")

    def test_import_copies_a_constant_number_of_entities_per_metric(self) -> None:
        self.assert_linear(reset=False)

    def test_reset_copies_a_constant_number_of_entities_per_metric(self) -> None:
        self.assert_linear(reset=True)

    def test_reports_without_subscribers_do_not_wake_the_send_thread(self) -> None:
        # A generated waveform reports four times a second; with hundreds of providers per
        # host the empty hand-offs to each provider's send thread were most of the CPU time.
        calls = []
        original = AsyncioEventLoopThread.run_coro

        def counting(self, coro):  # noqa: ANN001, ANN202
            calls.append(coro)
            return original(self, coro)

        with ProviderService(ip="127.0.0.1", instance_name="scaling-quiet") as service:
            handle = service.add_metric(MetricSpec("Quiet", MetricKind.NUMBER, initial_value=Decimal("1")))
            with patch.object(AsyncioEventLoopThread, "run_coro", counting):
                for value in range(10):
                    service.set_value(handle, Decimal(value))
            self.assertEqual(service.get_value(handle), Decimal("9"))
        self.assertEqual(len(calls), 0)

    def test_consumer_reads_do_not_copy_the_remote_mdib(self) -> None:
        # The GUI polls these; each used to deep-copy every entity, even for one metric.
        with ProviderService(ip="127.0.0.1", instance_name="scaling-consumer") as service:
            service.import_profile(big_device(200))
            handle = next(iter(service.list_metrics()))
            consumer = SdcConsumer(service._provider.get_xaddrs()[0], SdcV1Definitions, None)  # noqa: SLF001
            consumer.start_all()
            try:
                mdib = ConsumerMdib(consumer)
                mdib.init_mdib()
                remote = RemoteDevice(consumer, mdib, service.epr.urn)
                self.assertEqual(remote.metrics((handle,))[handle].value, service.get_value(handle))
                self.assertEqual(len(remote.alerts()), len(service.list_alerts()))
                for name, read in (("one metric", lambda: remote.metrics((handle,))), ("metrics", remote.metrics),
                                   ("alerts", remote.alerts), ("actions", remote.actions)):
                    self.assertEqual(entity_copies(read), 0, name)
            finally:
                consumer.stop_all()

    def test_empty_replies_to_notifications_are_not_compressed(self) -> None:
        # A consumer answers every report with an empty body; gzipping it only adds a header,
        # and with 50 subscriptions the compressor setup was a seventh of the consumer's CPU.
        compressed = []
        original = GzipCompressionHandler.compress_payload

        def recording(payload):  # noqa: ANN001, ANN202
            compressed.append(len(payload))
            return original(payload)

        with ProviderService(ip="127.0.0.1", instance_name="scaling-gzip") as service:
            handle = service.add_metric(MetricSpec("Gzip", MetricKind.NUMBER, initial_value=Decimal("1")))
            consumer = SdcConsumer(service._provider.get_xaddrs()[0], SdcV1Definitions, None)  # noqa: SLF001
            consumer.start_all()
            try:
                mdib = ConsumerMdib(consumer)
                mdib.init_mdib()
                remote = RemoteDevice(consumer, mdib, service.epr.urn)
                with patch.object(GzipCompressionHandler, "compress_payload", staticmethod(recording)):
                    for value in range(5):
                        service.set_value(handle, Decimal(value))
                    self.assertTrue(wait_until(lambda: remote.metrics((handle,))[handle].value == Decimal("4"),
                                               timeout=10), "the reports still arrive")
            finally:
                consumer.stop_all()
        self.assertNotIn(0, compressed)


    def test_provider_pane_rebuilds_once_per_burst_and_without_copies(self) -> None:
        # A profile import sends one operation change per controllable metric; rebuilding the
        # whole pane for each, with a copied entity per row, left the window "Not Responding".
        app = QApplication.instance() or QApplication([])
        with ProviderService(ip="127.0.0.1", instance_name="scaling-pane") as service:
            service.import_profile(big_device(40))
            pane = ProviderPane(service)
            try:
                operations = [h for m in service.list_metrics() if (h := service.operation_handle_for(m))]
                rebuilds = []
                original = ProviderPane.refresh
                with patch.object(ProviderPane, "refresh", lambda self: (rebuilds.append(1), original(self))):
                    for operation in operations:
                        pane.bridge.operations_changed.emit({operation: None})
                    for _ in range(5):
                        app.processEvents()
                self.assertEqual(len(rebuilds), 1, f"{len(operations)} operation changes")
                self.assertEqual(entity_copies(pane.refresh), 0)

                # Adding alarms emits alerts_changed on the GUI thread itself, once per alarm.
                alarm_rebuilds = []
                original_alerts = ProviderPane.refresh_alerts
                with patch.object(ProviderPane, "refresh_alerts",
                                  lambda self: (alarm_rebuilds.append(1), original_alerts(self))):
                    for alarm in service.list_alerts():
                        pane.bridge.alerts_changed.emit({alarm: None})
                    for _ in range(5):
                        app.processEvents()
                self.assertEqual(len(alarm_rebuilds), 1, f"{len(service.list_alerts())} alarm changes")
            finally:
                pane.bridge.close()
                pane.deleteLater()
                app.processEvents()


    def test_a_loaded_mdib_is_adopted_and_shown_without_copies(self) -> None:
        # Adopting the file and drawing its containment tree each copied every entity.
        app = QApplication.instance() or QApplication([])
        with ProviderService(ip="127.0.0.1", instance_name="scaling-native") as service:
            service.import_profile(big_device(60))
            path = config.save(service, Path(tempfile.mkdtemp()) / "big.xml", behaviour=True)
            native = config.load_file(path)
            mdib = parse_mdib(native.xml)
            self.assertEqual(entity_copies(lambda: adopt(mdib)), 0, "adopt")
            window = MainWindow(service)
            try:
                rebuilds = []
                original = ProviderPane.refresh
                with patch.object(ProviderPane, "refresh", lambda self: (rebuilds.append(1), original(self))):
                    self.assertTrue(window.apply_config(native, path))
                self.assertEqual(len(rebuilds), 1, "a replaced pane is drawn once")
                self.assertIsNotNone(window.provider_pane.native_tree)
                self.assertEqual(entity_copies(window.provider_pane.refresh), 0, "pane with native tree")
            finally:
                window.close()
                app.processEvents()


    def test_a_burst_of_discovery_answers_refreshes_the_device_list_once(self) -> None:
        # 450 providers answer a probe within half a second; listing them again for every
        # answer, in every connection tab, saturated the GUI thread.
        app = QApplication.instance() or QApplication([])
        pane = ConsumerPane("127.0.0.1")
        try:
            refreshes = []
            original = ConsumerPane._refresh_devices
            with patch.object(ConsumerPane, "_refresh_devices", lambda self: (refreshes.append(1), original(self))):
                for _ in range(50):
                    pane.discovery_changed.emit()
                    app.processEvents()
                self.assertTrue(wait_until(lambda: refreshes, timeout=2, pump=app.processEvents))
                wait_until(lambda: False, timeout=0.5, pump=app.processEvents, check_boundary=False)
            self.assertLessEqual(len(refreshes), 2, "50 discovery events")
        finally:
            pane.shutdown()
            pane.deleteLater()
            app.processEvents()


    def test_generated_samples_are_published_without_copying_entities(self) -> None:
        # 80 generated waveforms at four blocks a second copied 320 whole entities a second.
        with ProviderService(ip="127.0.0.1", instance_name="scaling-samples") as service:
            service.import_profile(big_device(40))
            service.stop_generator()
            waves = [h for h, spec in service.list_metrics().items() if spec.is_sample_array]
            before = {h: service.get_samples(h) for h in waves}
            self.assertEqual(entity_copies(service._sample_generator.publish_once), 0)  # noqa: SLF001
            self.assertTrue(all(service.get_samples(h) != before[h] for h in waves), "new blocks were published")

    def test_consumers_on_one_interface_share_a_single_discovery(self) -> None:
        # One discovery per connection tab: each tab parsed all traffic, and a new tab
        # listed nothing until hundreds of providers had answered its own probe.
        from service_lifecycle import AnnouncingDiscovery
        from sdc11073.wsdiscovery.service import Service

        from sdctoolbox import consumer_service as consumer_module

        built = []
        shared = consumer_module.SharedDiscovery("127.0.0.1")
        first = consumer_module.ConsumerService(discovery=shared)
        second = consumer_module.ConsumerService(discovery=shared)
        with patch.object(consumer_module, "RangeWSDiscovery", lambda _ip: built.append(AnnouncingDiscovery()) or built[-1]):
            first.start()
            built[0].receive("hello", Service(None, None, ["http://127.0.0.1:21080/a"], "urn:a", 1))
            heard = []
            second.set_discovery_listener(lambda: heard.append(1))
            second.start()
        self.assertEqual(len(built), 1, "one discovery for both consumers")
        self.assertEqual([d.epr for d in second.known_devices()], ["urn:a"], "a new consumer lists known devices at once")
        built[0].receive("hello", Service(None, None, ["http://127.0.0.1:21081/b"], "urn:b", 1))
        self.assertTrue(heard, "every consumer hears new traffic")
        time.sleep(0.05)  # past the ~16 ms tick of the monotonic clock on Windows
        self.assertEqual(len(first.scan(timeout=0, expected=99)), 0, "a scan reports only providers that answered it")
        self.assertEqual(len(second.known_devices()), 2, "a scan in one tab does not empty the others")
        first.stop()
        self.assertEqual(built[0].stop_count, 0, "the remaining consumer keeps discovery running")
        self.assertEqual(len(second.known_devices()), 2)
        second.stop()
        self.assertEqual(built[0].stop_count, 1, "the last consumer stops it")

    def test_connection_tabs_share_their_discovery(self) -> None:
        from sdctoolbox.gui import consumer_pane as cp
        from consumer_lifecycle import FakeConsumerService

        received = []

        class Observed(FakeConsumerService):
            def __init__(self, **kwargs) -> None:  # noqa: ANN003
                super().__init__(**kwargs)
                received.append(kwargs.get("discovery"))

        app = QApplication.instance() or QApplication([])
        with ProviderService(ip="127.0.0.1", instance_name="scaling-tabs") as provider, \
             patch.object(cp, "ConsumerService", Observed):
            window = MainWindow(provider)
            try:
                window.network_tabs.add_connection()
                window.network_tabs.add_connection()
                self.assertEqual(len(received), 3)
                self.assertIsNotNone(received[0])
                self.assertTrue(all(d is received[0] for d in received), "every tab uses the same discovery")
            finally:
                window.close()
                window.provider_pane.bridge.close()
                window.deleteLater()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
