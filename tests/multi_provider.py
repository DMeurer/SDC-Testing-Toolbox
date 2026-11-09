"""Independent provider tabs: real Qt lifecycle and loopback SOAP subscriptions.

Run under the shared network-test mutex. Hidden backend state, not hidden painting,
is the continuity contract. Only slow boundaries and deliberate failures are controlled.
"""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QTextDocument, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTabBar
from consumer_lifecycle import BlockingCall, FakeBridge, FakeConsumerService, controllable_peer, http_device
from gui_test_support import application, pump, wait_for
from sdc11073.xml_types.actions import Actions
from sdc11073.xml_types import msg_types

import run_toolbox
from sdctoolbox import config
from sdctoolbox.gui import consumer_pane as cp
from sdctoolbox.gui.async_call import AsyncCall
from sdctoolbox.gui.main_window import MainWindow
from sdctoolbox.gui.no_wheel import NoWheelTabBar
from sdctoolbox.gui.startup_dialog import StartupSettings
from sdctoolbox.gui.styling import is_dark
from sdctoolbox.model import MetricKind, MetricSpec, RemoteMetric, RemoteRange
from sdctoolbox.provider_service import ProviderService


class MultiProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = application()

    @contextmanager
    def window(self):
        provider = ProviderService(ip="127.0.0.1", instance_name="multi-provider-local")
        window = None
        panes = []
        workers = []
        try:
            provider.start()
            window = MainWindow(provider)
            window.show()
            self.assertTrue(
                hasattr(window, "network_tabs"),
                "Network must host independent connection tabs, not a single ConsumerPane",
            )
            tabs = window.network_tabs
            panes = tabs.panes
            workers.extend(pane._worker for pane in panes)
            add_connection = tabs.add_connection

            def tracked_add():
                pane = add_connection()
                panes.append(pane)
                workers.append(pane._worker)
                return pane

            with patch.object(tabs, "add_connection", side_effect=tracked_add):
                yield window, tabs, panes
        finally:
            try:
                if window is not None:
                    try:
                        for pane in dict.fromkeys(panes):
                            if shiboken6.isValid(pane):
                                if isinstance(pane.service, FakeConsumerService):
                                    pane.service.scan_call.release.set()
                                    pane.service.connect_call.release.set()
                                if pane.remote is not None and hasattr(pane.remote, "set_call"):
                                    pane.remote.set_call.release.set()
                        window.close()
                    finally:
                        try:
                            completed = [worker.wait(5.0) for worker in workers]
                            self.assertTrue(all(completed), "every created worker must finish retiring, even after Qt deletion")
                        finally:
                            try:
                                window.provider_pane.bridge.close()
                            finally:
                                try:
                                    shiboken6.delete(window)
                                finally:
                                    self.app.processEvents()
            finally:
                provider.stop()

    def setUp(self) -> None:
        self.fake_service = patch.object(cp, "ConsumerService", FakeConsumerService)
        self.fake_bridge = patch.object(cp, "MdibBridge", FakeBridge)
        self.fake_service.start()
        self.fake_bridge.start()
        self.addCleanup(self.fake_service.stop)
        self.addCleanup(self.fake_bridge.stop)

    def attach(self, pane, epr="peer"):
        remote = controllable_peer(epr)
        pane._on_connected(remote, pane._generation)
        return remote

    def type_draft(self, field, text):
        pump(self.app, 0.02)
        field.window().activateWindow()
        field.setFocus()
        pump(self.app, 0.02)
        self.assertTrue(field.hasFocus(), f"regression must exercise real Qt typing (visible={field.isVisible()}, enabled={field.isEnabled()})")
        QTest.keyClick(field, Qt.Key_A, Qt.ControlModifier)
        QTest.keyClicks(field, text)
        self.assertTrue(field.isModified())

    def edit_field(self, pane, *, cards):
        pane.select_handle("metric")
        return pane.board.card("metric").control.edit if cards else pane.value_edit

    def set_metric_kind(self, pane, kind):
        remote = pane.remote
        remote.metric_values["metric"] = replace(
            remote.metric_values["metric"], kind=kind,
            node_type_name="NumericMetricDescriptor" if kind is MetricKind.NUMBER else "StringMetricDescriptor",
            value=Decimal(7) if kind is MetricKind.NUMBER else "initial",
            selected_operation_handle="set.metric",
        )
        pane.refresh()

    def check_hidden_drafts(self, *, cards):
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            remote = self.attach(first, "first")
            second = tabs.add_connection()
            owned.append(second)
            self.attach(second, "second")
            window.set_use_widgets(cards)
            for kind, draft, other_draft, reported, clean in (
                (MetricKind.NUMBER, "123", "456", Decimal(99), Decimal(88)),
                (MetricKind.TEXT, "first draft", "second draft", "reported", "clean report"),
            ):
                with self.subTest(kind=kind, cards=cards):
                    for pane in (first, second):
                        self.set_metric_kind(pane, kind)
                    tabs.setCurrentIndex(0)
                    field = self.edit_field(first, cards=cards)
                    self.type_draft(field, draft)
                    tabs.setCurrentIndex(1)
                    other_field = self.edit_field(second, cards=cards)
                    self.type_draft(other_field, other_draft)
                    self.assertFalse(first.isVisible())
                    self.assertFalse(field.hasFocus())
                    remote.metric_values["metric"].value = reported
                    first.bridge.metrics_changed.emit({"metric": object()})
                    first.bridge.descriptors_added.emit({"unrelated": object()})
                    first.bridge.operations_changed.emit({"unrelated": object()})
                    tabs.setCurrentIndex(0)
                    self.assertEqual(field.text(), draft, "own-peer reports/rebuilds must not erase a hidden draft")
                    self.assertEqual(other_field.text(), other_draft)
                    self.assertIs(first.remote, remote)
                    first._restore_device_value("metric")
                    self.assertEqual(field.text(), str(reported), "explicit reconciliation overrides a draft")
                    self.assertFalse(field.isModified())
                    tabs.setCurrentIndex(1)
                    remote.metric_values["metric"].value = clean
                    first.bridge.metrics_changed.emit({"metric": object()})
                    tabs.setCurrentIndex(0)
                    self.assertEqual(field.text(), str(clean), "clean editors must keep following reports")
                    second._restore_device_value("metric")

    def test_table_drafts_survive_hidden_own_peer_reports(self) -> None:
        self.check_hidden_drafts(cards=False)

    def test_card_drafts_survive_hidden_own_peer_reports(self) -> None:
        self.check_hidden_drafts(cards=True)

    def test_pending_table_choice_survives_switch_until_reset(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            remote = self.attach(first)
            remote.metric_values["metric"] = RemoteMetric(
                "metric", "EnumStringMetricDescriptor", kind=MetricKind.CHOICE, value="IDLE",
                allowed_values=("IDLE", "RUN", "PAUSE"), operation_handles=("set.metric",),
                selected_operation_handle="set.metric", controllable_now=True,
            )
            first.refresh()
            second = tabs.add_connection()
            owned.append(second)
            window.set_use_widgets(False)
            tabs.setCurrentIndex(0)
            first.select_handle("metric")
            first.choice_box.setFocus()
            pump(self.app, 0.02)
            QTest.keyClick(first.choice_box, Qt.Key_End)
            self.assertEqual(first.choice_box.currentText(), "PAUSE")
            self.assertFalse(remote.set_call.entered.is_set())
            tabs.setCurrentIndex(1)
            remote.metric_values["metric"].value = "RUN"
            first.bridge.metrics_changed.emit({"metric": object()})
            first.bridge.operations_changed.emit({"unrelated": object()})
            tabs.setCurrentIndex(0)
            self.assertEqual(first.choice_box.currentText(), "PAUSE", "unsubmitted choice belongs to its pane")
            first._restore_device_value("metric")
            self.assertEqual(first.choice_box.currentText(), "RUN")
            tabs.setCurrentIndex(1)
            remote.metric_values["metric"].value = "IDLE"
            first.bridge.metrics_changed.emit({"metric": object()})
            tabs.setCurrentIndex(0)
            self.assertEqual(first.choice_box.currentText(), "IDLE", "reconciled choices follow reports again")
            first.choice_box.setFocus()
            QTest.keyClick(first.choice_box, Qt.Key_End)
            remote.metric_values["metric"] = replace(remote.metric_values["metric"], operation_allowed_values=("IDLE", "RUN"))
            first.refresh()
            self.assertEqual(first.choice_box.currentText(), "IDLE", "domain changes discard pending choices")
            first.choice_box.setFocus()
            QTest.keyClick(first.choice_box, Qt.Key_End)
            remote.metric_values["metric"] = replace(remote.metric_values["metric"], selected_operation_handle="replacement")
            first.refresh()
            self.assertEqual(first.choice_box.currentText(), "IDLE", "operation changes discard pending choices")
            first.choice_box.setFocus()
            QTest.keyClick(first.choice_box, Qt.Key_End)
            first._on_disconnect()
            self.assertEqual(first.choice_box.count(), 0)

    def test_dirty_fields_reset_on_source_domain_operation_and_selection(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            remote = self.attach(first)
            second = tabs.add_connection()
            owned.append(second)
            for cards in (False, True):
                window.set_use_widgets(cards)
                for kind in (MetricKind.NUMBER, MetricKind.TEXT):
                    self.set_metric_kind(first, kind)
                    for change in ("operation", "domain", "disabled"):
                        if kind is MetricKind.TEXT and change == "domain":
                            continue
                        with self.subTest(cards=cards, kind=kind, change=change):
                            tabs.setCurrentIndex(0)
                            field = self.edit_field(first, cards=cards)
                            self.type_draft(field, "123" if kind is MetricKind.NUMBER else "unfinished")
                            tabs.setCurrentIndex(1)
                            metric = remote.metric_values["metric"]
                            if change == "operation":
                                metric = replace(metric, selected_operation_handle="replacement")
                            elif change == "domain":
                                metric = replace(metric, allowed_ranges=(RemoteRange(None, Decimal(200)),))
                            else:
                                metric = replace(metric, controllable_now=False)
                            remote.metric_values["metric"] = metric
                            first.bridge.operations_changed.emit({"changed": object()})
                            tabs.setCurrentIndex(0)
                            reset = self.edit_field(first, cards=cards)
                            self.assertEqual(reset.text(), str(metric.value))
                            self.assertFalse(reset.isModified())
                            self.set_metric_kind(first, kind)
                            remote.metric_values["metric"] = replace(remote.metric_values["metric"], controllable_now=True, allowed_ranges=())
                            first.refresh()
                # Deleting/recreating the same handle must not resurrect its card/table draft.
                tabs.setCurrentIndex(0)
                field = self.edit_field(first, cards=cards)
                self.type_draft(field, "old source draft")
                first._on_disconnect()
                remote = self.attach(first, "replacement source")
                tabs.setCurrentIndex(1)
                remote.metric_values["metric"].value = Decimal(17)
                first.bridge.metrics_changed.emit({"metric": object()})
                tabs.setCurrentIndex(0)
                reset = self.edit_field(first, cards=cards)
                self.assertEqual(reset.text(), "17")
                self.assertFalse(reset.isModified())
            window.set_use_widgets(False)
            remote.metric_values["other"] = replace(remote.metric_values["metric"], handle="other", value=Decimal(3))
            first.refresh()
            first.select_handle("metric")
            self.type_draft(first.value_edit, "123")
            first.select_handle("other")
            self.assertEqual(first.value_edit.text(), "3")
            self.assertFalse(first.value_edit.isModified())

    def test_dirty_invocation_results_reconcile_table_and_cards(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            remote = self.attach(first)
            second = tabs.add_connection()
            owned.append(second)
            for cards in (False, True):
                window.set_use_widgets(cards)
                for kind in (MetricKind.NUMBER, MetricKind.TEXT):
                    self.set_metric_kind(first, kind)
                    for outcome in (msg_types.InvocationState.FINISHED_MOD, msg_types.InvocationState.FAILED, "error"):
                        with self.subTest(cards=cards, kind=kind, outcome=outcome):
                            tabs.setCurrentIndex(0)
                            field = self.edit_field(first, cards=cards)
                            self.type_draft(field, "123" if kind is MetricKind.NUMBER else "requested")
                            call = remote.set_call = BlockingCall(outcome)
                            if outcome == "error":
                                call.error = cp.ConsumerError("injected draft write failure")
                            try:
                                QTest.keyClick(field, Qt.Key_Return)
                                self.assertTrue(call.entered.wait(1.0))
                                tabs.setCurrentIndex(1)
                                actual = Decimal(99) if kind is MetricKind.NUMBER else "device answer"
                                remote.metric_values["metric"].value = actual
                                first.bridge.metrics_changed.emit({"metric": object()})
                                call.release.set()
                                self.assertTrue(wait_for(self.app, lambda: first.invocation_label.text().startswith(("accepted", "refused", "failed"))))
                                tabs.setCurrentIndex(0)
                                self.assertEqual(field.text(), str(actual))
                                self.assertFalse(field.isModified())
                                tabs.setCurrentIndex(1)
                                following = Decimal(88) if kind is MetricKind.NUMBER else "next answer"
                                remote.metric_values["metric"].value = following
                                first.bridge.metrics_changed.emit({"metric": object()})
                                tabs.setCurrentIndex(0)
                                self.assertEqual(field.text(), str(following), "completed requests cannot leave clean fields frozen")
                            finally:
                                call.release.set()

    def test_fixture_awaits_worker_after_closed_page_is_deleted(self) -> None:
        seen = []
        original_wait = AsyncCall.wait

        def wait(worker, timeout=None):
            seen.append(worker)
            return original_wait(worker, timeout)

        with patch.object(AsyncCall, "wait", wait):
            with self.window() as (window, tabs, owned):
                closing = window.network_pane
                worker = closing._worker
                owned.append(tabs.add_connection())
                tabs.close_connection(0)
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
                self.assertFalse(shiboken6.isValid(closing))
                seen.clear()
            self.assertIn(worker, seen, "fixture must await retired workers independently of Qt page validity")

    def test_fixture_cleanup_assertion_still_stops_provider_and_deletes_qt(self) -> None:
        original_wait = AsyncCall.wait

        def wait(worker, timeout=None):
            completed = original_wait(worker, timeout)
            return False if timeout == 5.0 else completed

        with patch.object(AsyncCall, "wait", wait):
            with self.assertRaisesRegex(AssertionError, "every created worker"):
                with self.window() as (window, _tabs, _owned):
                    provider = window.service
        self.assertIsNone(provider._provider, "a cleanup assertion must not skip stopping its real provider")
        self.assertFalse(shiboken6.isValid(window), "Qt cleanup must run even after a cleanup assertion")

    def test_add_switch_close_and_last_tab_stay_usable(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            self.assertEqual(tabs.panes, [first])
            self.assertIs(window.network_panel.content, tabs)
            self.assertIsInstance(tabs.tabBar(), NoWheelTabBar)
            add = tabs.cornerWidget()
            self.assertTrue(add.accessibleName())
            add.setFocus()
            QTest.keyClick(add, Qt.Key_Space)
            second = window.network_pane
            owned.append(second)
            self.assertIsNot(first, second)
            third = tabs.add_connection()
            owned.append(third)
            self.assertEqual(tabs.panes, [first, second, third], "there is no two-provider ceiling")
            remotes = [self.attach(pane) for pane in tabs.panes]
            for index in (0, 2, 1, 0, 1):
                tabs.setCurrentIndex(index)
                self.assertIs(window.network_pane, tabs.panes[index])
                self.assertIs(window.network_pane.remote, remotes[index])
            bar = tabs.tabBar()
            event = QWheelEvent(QPointF(5, 5), QPointF(5, 5), QPoint(), QPoint(0, -120),
                                Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
            QApplication.sendEvent(bar, event)
            self.assertEqual(tabs.currentIndex(), 1, "wheel scrolling must not switch connections")
            close = bar.tabButton(1, QTabBar.RightSide) or bar.tabButton(1, QTabBar.LeftSide)
            self.assertTrue(close.accessibleName())
            close.setFocus()
            QTest.keyClick(close, Qt.Key_Space)
            self.assertEqual(tabs.panes, [first, third])
            self.assertTrue(second._shutdown)
            self.assertIs(first.remote, remotes[0])
            self.assertIs(third.remote, remotes[2])
            tabs.close_connection(-1)
            tabs.close_connection(99)
            self.assertEqual(tabs.count(), 2)
            tabs.close_connection(0)
            tabs.close_connection(0)
            replacement = window.network_pane
            self.assertEqual(tabs.count(), 1)
            self.assertNotIn(replacement, (first, second, third))
            self.assertIsNone(replacement.remote)
            self.assertTrue(replacement.scan_button.isEnabled())
            window.close()
            self.assertEqual(tabs.count(), 1, "shutdown must not manufacture a new session")
            self.assertTrue(replacement._shutdown)
            with self.assertRaises(RuntimeError):
                tabs.add_connection()

    def test_widget_theme_and_layout_changes_reach_existing_and_new_tabs(self) -> None:
        palette = self.app.palette()
        scheme = self.app.styleHints().colorScheme()
        try:
            with self.window() as (window, tabs, owned):
                first = window.network_pane
                self.attach(first)
                first.value_edit.setText("unsaved first")
                for dark in (True, False):
                    window.dark_mode_action.setChecked(dark)
                    window.set_use_widgets(False)
                    added = tabs.add_connection()
                    owned.append(added)
                    self.attach(added)
                    pump(self.app, 0.05)
                    self.assertTrue(all(not p.use_widgets for p in tabs.panes))
                    self.assertTrue(all(is_dark(p) == dark for p in tabs.panes))
                    self.assertTrue(all(is_dark(p.context_label) == dark for p in tabs.panes))
                    window.set_split_view(False)
                    window.tabs.setCurrentIndex(0)
                    window.set_use_widgets(True)
                    self.assertTrue(all(p.use_widgets for p in tabs.panes))
                    window.set_split_view(True)
                    self.assertIs(window.network_panel.content, tabs)
                self.assertEqual(first.value_edit.text(), "unsaved first")
                self.assertEqual(tabs.count(), 3)
        finally:
            self.app.styleHints().setColorScheme(scheme)
            self.app.setPalette(palette)

    def test_connection_identity_is_distinct_bounded_and_follows_disconnect(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            second = tabs.add_connection()
            owned.append(second)
            epr = "urn:" + "long\nidentity &" * 1000
            self.attach(first, epr)
            self.attach(second, epr)
            self.assertNotEqual(tabs.tabText(0), tabs.tabText(1))
            for index in (0, 1):
                self.assertLessEqual(len(tabs.tabText(index)), 80)
                self.assertNotIn("\n", tabs.tabText(index))
                self.assertIn("urn:", tabs.tabToolTip(index))
                document = QTextDocument()
                document.setHtml(tabs.tabToolTip(index))
                self.assertLessEqual(len(document.toPlainText()), 1100)
            before = tabs.tabToolTip(1)
            first._on_disconnect()
            self.assertIn("Not connected", tabs.tabToolTip(0))
            self.assertEqual(tabs.tabToolTip(1), before)

    def test_tab_tooltip_displays_peer_markup_literally(self) -> None:
        with self.window() as (window, tabs, _owned):
            self.attach(window.network_pane, "urn:<b>literal peer</b>&identity")
            document = QTextDocument()
            document.setHtml(tabs.tabToolTip(0))
            self.assertEqual(document.toPlainText(), "Connection 1: Connected to urn:<b>literal peer</b>&identity")

    def test_scanning_a_connected_tab_keeps_its_peer_identity(self) -> None:
        with self.window() as (window, tabs, _owned):
            remote = self.attach(window.network_pane, "urn:peer-one")
            window.network_pane._on_scan_finished([])
            self.assertIn("urn:peer-one", tabs.tabText(0))
            self.assertIn("urn:peer-one", tabs.tabToolTip(0))
            self.assertIn("No devices found", tabs.tabToolTip(0))
            self.assertIs(window.network_pane.remote, remote)

    def test_close_failure_retains_page_for_retry(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            remote = self.attach(first)
            second = tabs.add_connection()
            owned.append(second)
            with patch.object(first._worker, "retire", side_effect=RuntimeError("injected close handoff failure")), \
                 patch("sdctoolbox.gui.consumer_tabs.QMessageBox.warning") as warning:
                tabs.close_connection(0)
            self.assertTrue(warning.called)
            self.assertEqual(tabs.panes, [first, second])
            self.assertIs(first.remote, remote)
            self.assertFalse(second._shutdown)
            tabs.close_connection(0)
            self.assertEqual(tabs.panes, [second])
            self.assertTrue(wait_for(self.app, lambda: remote.close_count == 1))

    def test_close_during_late_connect_only_retires_that_session(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            surviving = self.attach(first, "surviving")
            second = tabs.add_connection()
            owned.append(second)
            service = second.service
            late = controllable_peer("late")
            service.connect_call.result = late
            second.devices = [http_device("late")]
            second.device_list.addItem("late")
            second.device_list.setCurrentRow(0)
            second._on_connect()
            self.assertTrue(service.connect_call.entered.wait(1.0))
            tabs.close_connection(1)
            self.assertEqual(service.stop_count, 0)
            self.assertIs(first.remote, surviving)
            service.connect_call.release.set()
            self.assertTrue(wait_for(self.app, lambda: late.close_count == service.stop_count == 1))
            self.assertIs(first.remote, surviving)
            self.assertEqual(surviving.close_count, 0)
            self.assertEqual(tabs.count(), 1)

    def test_close_during_invoke_only_retires_after_its_own_work(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            surviving = self.attach(first, "surviving")
            second = tabs.add_connection()
            owned.append(second)
            closing = self.attach(second, "closing")
            second._on_widget_value_requested("metric", Decimal(8))
            self.assertTrue(closing.set_call.entered.wait(1.0))
            before = first.invocation_label.text()
            tabs.close_connection(1)
            self.assertEqual(closing.close_count, 0)
            closing.set_call.release.set()
            self.assertTrue(wait_for(self.app, lambda: closing.close_count == 1))
            self.assertIs(first.remote, surviving)
            self.assertEqual(first.invocation_label.text(), before)
            self.assertEqual(surviving.close_count, 0)

    def test_hidden_loss_restart_and_failed_connect_do_not_touch_other_tabs(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            healthy = self.attach(first, "healthy")
            second = tabs.add_connection()
            owned.append(second)
            lost = self.attach(second, "lost")
            tabs.setCurrentIndex(0)
            lost.mark_connection_lost("injected lost session")
            self.assertTrue(wait_for(self.app, lambda: second.remote is None))
            self.assertIn("injected lost session", tabs.tabToolTip(1))
            restarted = self.attach(second, "restart")
            second.bridge.peer_restarted.emit()
            self.assertIsNone(second.remote)
            self.assertIn("restarted", tabs.tabToolTip(1))
            self.assertTrue(wait_for(self.app, lambda: restarted.close_count == 1))
            second.devices = [http_device("bad")]
            second.device_list.addItem("bad")
            second.device_list.setCurrentRow(0)
            second.service.connect_call.error = cp.ConsumerError("injected connect refusal")
            second.service.connect_call.release.set()
            second._on_connect()
            self.assertTrue(wait_for(self.app, lambda: "Could not connect" in tabs.tabToolTip(1)))
            self.assertIs(first.remote, healthy)
            self.assertEqual(healthy.close_count, 0)

    def test_bad_add_preserves_sessions_and_last_close_failure_is_usable(self) -> None:
        with self.window() as (window, tabs, _owned):
            first = window.network_pane
            healthy = self.attach(first)
            warnings = []
            # Service startup owns its rollback; the notebook must preserve its existing pages.
            with patch.object(FakeConsumerService, "start", side_effect=OSError("injected start failure")), \
                 patch("sdctoolbox.gui.consumer_tabs.QMessageBox.warning", side_effect=lambda *a: warnings.append(a)):
                QTest.mouseClick(tabs.cornerWidget(), Qt.LeftButton)
                self.assertEqual(tabs.panes, [first])
                self.assertIs(first.remote, healthy)
                self.assertTrue(warnings)
                tabs.close_connection(0)
                self.assertEqual(tabs.panes, [first])
                self.assertFalse(first._shutdown)
                self.assertIs(first.remote, healthy)
                self.assertEqual(len(warnings), 2)
            added = tabs.add_connection()
            self.assertIs(window.network_pane, added)

    def test_window_close_reaches_every_session_and_is_idempotent(self) -> None:
        with self.window() as (window, tabs, owned):
            second = tabs.add_connection()
            owned.append(second)
            remotes = [self.attach(pane) for pane in tabs.panes]
            services = [pane.service for pane in tabs.panes]
            window.close()
            tabs.shutdown()
            self.assertTrue(all(p._shutdown for p in tabs.panes))
            self.assertTrue(wait_for(self.app, lambda: all(r.close_count == 1 for r in remotes)))
            self.assertTrue(wait_for(self.app, lambda: all(s.stop_count == 1 for s in services)))
            self.assertEqual(tabs.count(), 2)

    def test_shutdown_failure_does_not_skip_other_sessions_and_can_retry(self) -> None:
        with self.window() as (window, tabs, owned):
            first = window.network_pane
            second = tabs.add_connection()
            owned.append(second)
            remote = self.attach(first)
            original_retire = first._worker.retire

            def fail_remote_retirement(resource, closer):
                if resource is remote:
                    raise RuntimeError("injected retirement start failure")
                return original_retire(resource, closer)

            with patch.object(first._worker, "retire", side_effect=fail_remote_retirement):
                with self.assertRaisesRegex(RuntimeError, "injected retirement start failure"):
                    tabs.shutdown()
            self.assertTrue(second._shutdown)
            self.assertTrue(wait_for(self.app, lambda: second.service.stop_count == 1))
            self.assertIs(first.remote, remote, "failed handoff must retain ownership for retry")
            tabs.shutdown()
            self.assertTrue(wait_for(self.app, lambda: remote.close_count == first.service.stop_count == 1))
            self.assertEqual(tabs.count(), 2)

    def test_new_tabs_keep_interface_identity_and_tls_policy(self) -> None:
        received = []

        class ObservedService(FakeConsumerService):
            def __init__(inner, **kwargs):
                super().__init__(**kwargs)
                received.append(kwargs)

        policy = object()
        provider = ProviderService(ip="127.0.0.1", instance_name="multi-tls-policy")
        window = None
        try:
            provider.start()
            provider.tls_config = policy
            with patch.object(cp, "ConsumerService", ObservedService):
                window = MainWindow(provider)
                self.assertTrue(hasattr(window, "network_tabs"), "all new tabs must inherit TLS policy")
                window.network_tabs.add_connection()
                window.network_tabs.add_connection()
            self.assertEqual(len(received), 3)
            for arguments in received:
                self.assertEqual(arguments["ip"], "127.0.0.1")
                self.assertEqual(arguments["own_epr"], provider.epr.urn)
                self.assertIs(arguments["tls_config"], policy)
        finally:
            try:
                if window is not None:
                    try:
                        window.close()
                    finally:
                        try:
                            window.provider_pane.bridge.close()
                        finally:
                            shiboken6.delete(window)
            finally:
                provider.stop()

    def test_failed_profile_startup_closes_all_created_tabs(self) -> None:
        app = self.app
        windows = []

        class ObservedWindow(MainWindow):
            def __init__(inner, service):
                super().__init__(service)
                windows.append(inner)
                if hasattr(inner, "network_tabs"):
                    inner.network_tabs.add_connection()
                    inner.network_tabs.add_connection()

        class AppBoundary:
            def exec(self):
                raise AssertionError("failed profile startup must not enter the application event loop")

        parsed = config.parse({"version": 3, "actions": [{"label": "Bad", "target": "missing.target"}]})
        settings = StartupSettings("multi-startup", "127.0.0.1", "snapshot.json", False, device_config=parsed)
        try:
            with patch.object(run_toolbox, "QApplication", return_value=AppBoundary()), \
                 patch.object(run_toolbox, "ask_how_to_start", return_value=settings), \
                 patch.object(run_toolbox, "MainWindow", ObservedWindow), \
                 patch("sdctoolbox.gui.main_window.QMessageBox.warning"), \
                 patch("sdctoolbox.gui.main_window.QMessageBox.critical"), \
                 patch.object(run_toolbox, "basic_logging_setup"):
                result = run_toolbox.main([])
            self.assertEqual(result, 2)
            self.assertEqual(len(windows), 1)
            self.assertTrue(hasattr(windows[0], "network_tabs"), "startup must own every session")
            self.assertEqual(windows[0].network_tabs.count(), 3)
            for pane in windows[0].network_tabs.panes:
                self.assertTrue(pane._shutdown)
                self.assertTrue(pane._worker.wait(3.0))
                self.assertEqual(pane.service.stop_count, 1)
            self.assertIsNone(windows[0].service._provider)
        finally:
            for window in windows:
                window.close()
                window.provider_pane.bridge.close()
                shiboken6.delete(window)
            app.processEvents()

    def test_two_real_providers_hidden_backend_and_writes_are_independent(self) -> None:
        self.fake_service.stop()
        self.fake_bridge.stop()
        peer = ProviderService(ip="127.0.0.1", instance_name="multi-provider-peer")
        other = ProviderService(ip="127.0.0.1", instance_name="multi-provider-other")
        try:
            peer.start()
            other.start()
            with self.window() as (window, tabs, owned):
                providers = [other, peer]
                handles = [p.add_metric(MetricSpec(label="Shared setting", handle="shared.setting",
                    kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal(i + 1)))
                    for i, p in enumerate(providers)]
                self.assertEqual(handles, ["shared.setting", "shared.setting"])
                for provider in providers:
                    provider.add_metric(MetricSpec(label="Shared note", handle="shared.note", kind=MetricKind.TEXT,
                                                   controllable=True, initial_value="initial"))
                second = tabs.add_connection()
                owned.append(second)
                panes = tabs.panes
                for pane, provider in zip(panes, reversed(providers)):
                    self.assertTrue(wait_for(self.app, lambda: any(d.epr == provider.epr.urn for d in pane.devices), 20))
                    pane.device_list.setCurrentRow(next(i for i, d in enumerate(pane.devices) if d.epr == provider.epr.urn))
                    pane.connect_button.click()
                    self.assertTrue(wait_for(self.app, lambda: pane.remote is not None, 30))
                remotes = [pane.remote for pane in panes]
                self.assertIsNot(remotes[0], remotes[1])
                for pane in panes:
                    self.assertTrue(pane.remote.operation_reports_subscribed())
                    self.assertTrue(any(active and Actions.EpisodicMetricReport.value in actions.split()
                                        for actions, active in pane.remote._consumer.subscription_status.items()))
                for index in (0, 1, 0, 1, 0):
                    tabs.setCurrentIndex(index)
                    self.assertIs(window.network_pane.remote, remotes[index])
                # Exercise a hidden connection and then the whole hidden Network panel.
                for hidden in (1, 0):
                    tabs.setCurrentIndex(1 - hidden)
                    window.set_split_view(hidden == 1)
                    if hidden == 0:
                        window.tabs.setCurrentIndex(0)
                    pump(self.app, 0.05)
                    self.assertFalse(panes[hidden].isVisible())
                    provider = list(reversed(providers))[hidden]
                    value = Decimal(10 + hidden)
                    provider.set_value("shared.setting", value)
                    self.assertTrue(wait_for(self.app, lambda: remotes[hidden].metrics()["shared.setting"].value == value))
                    self.assertIs(panes[hidden].remote, remotes[hidden])
                window.set_split_view(True)
                tabs.setCurrentIndex(0)
                panes[0].board.card("shared.setting").control.request(Decimal(31))
                self.assertTrue(wait_for(self.app, lambda: peer.get_value("shared.setting") == Decimal(31)))
                self.assertEqual(other.get_value("shared.setting"), Decimal(11))
                tabs.setCurrentIndex(1)
                window.set_use_widgets(False)
                panes[1].select_handle("shared.setting")
                panes[1].value_edit.setText("42")
                panes[1].apply_button.click()
                self.assertTrue(wait_for(self.app, lambda: other.get_value("shared.setting") == Decimal(42)))
                self.assertEqual(peer.get_value("shared.setting"), Decimal(31))
                # Actual SOAP reports must advance the hidden MDIB without discarding user input.
                for cards in (False, True):
                    window.set_use_widgets(cards)
                    for handle, draft, other_draft, reported in (
                        ("shared.setting", "123", "456", Decimal(99)),
                        ("shared.note", "first pending", "second pending", "wire report"),
                    ):
                        with self.subTest(real_transport=True, cards=cards, handle=handle):
                            tabs.setCurrentIndex(0)
                            panes[0].select_handle(handle)
                            field = panes[0].board.card(handle).control.edit if cards else panes[0].value_edit
                            self.type_draft(field, draft)
                            tabs.setCurrentIndex(1)
                            panes[1].select_handle(handle)
                            other_field = panes[1].board.card(handle).control.edit if cards else panes[1].value_edit
                            self.type_draft(other_field, other_draft)
                            self.assertFalse(panes[0].isVisible())
                            peer.set_value(handle, reported)
                            self.assertTrue(wait_for(self.app, lambda: remotes[0].metrics()[handle].value == reported))
                            tabs.setCurrentIndex(0)
                            self.assertEqual(field.text(), draft)
                            self.assertEqual(other_field.text(), other_draft)
                            self.assertIs(panes[0].remote, remotes[0])
                            panes[0]._restore_device_value(handle)
                            panes[1]._restore_device_value(handle)
                closed_worker = panes[1]._worker
                closed_address = remotes[1]._consumer.base_url
                tabs.close_connection(1)
                self.assertTrue(closed_worker.wait(5.0), "closed real connection must complete its retirement")
                self.assertFalse(any(remotes[1]._consumer.subscription_status.values()), "closed consumer has no active subscriptions")
                self.assertFalse(any(
                    subscription.is_valid and subscription.unsubscribed_at is None
                    and subscription.notify_to_address.startswith(closed_address)
                    for manager in other._provider._subscriptions_managers.values()
                    for subscription in list(manager._subscriptions.objects)
                ), "provider no longer has a live subscription for the closed connection")
                self.assertIs(panes[0].remote, remotes[0])
                peer.set_value("shared.setting", Decimal(53))
                self.assertTrue(wait_for(self.app, lambda: remotes[0].metrics()["shared.setting"].value == Decimal(53)))
                self.assertTrue(remotes[0].operation_reports_subscribed())
                peer.stop()
                self.assertTrue(wait_for(self.app, lambda: panes[0].remote is None, 10))
                self.assertIn("Connection lost", tabs.tabToolTip(0))
        finally:
            try:
                peer.stop()
            finally:
                other.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
