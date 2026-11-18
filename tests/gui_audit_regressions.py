"""Real offscreen Qt regressions for audit findings 5, 7, 11, 12, 24 and 25.

Only the consumer transport is controlled; widgets, signals and worker threads are real.
Run with the shared network-test mutex: the provider-editor case starts a real provider.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
import unittest
import weakref
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import QApplication
from consumer_lifecycle import BlockingCall, EqualResource, FakeBridge, FakeConsumerService, FakeRemote
from mdib_test_support import fake_consumer_mdib
from gui_test_support import WindowFixture, wait_for
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.mdib import ConsumerMdib
from sdc11073.observableproperties.observables import WeakRef
from sdc11073.xml_types import pm_qnames as pm

from sdctoolbox.consumer_service import DiscoveredDevice, RemoteDevice
from sdctoolbox.gui import consumer_pane as cp
from sdctoolbox.gui.async_call import AsyncCall
from sdctoolbox.gui.helpers import NO_VALUE
from sdctoolbox.gui.provider_pane import COL_CONTROL, COL_VALUE
from sdctoolbox.gui.qt_bridge import MdibBridge
from sdctoolbox.gui.widgets import WidgetSpec, build_widget, from_remote_metric, from_spec
from sdctoolbox.model import MetricKind as K, MetricSpec, RemoteMetric, RemoteRange


class GatedCloser:
    """Bound every blocked cleanup, even when the baseline runs it on the GUI thread."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()
        self.count = 0
        self.thread_id = None
        self.expired = False

    def __call__(self) -> None:
        self.count += 1
        self.thread_id = threading.get_ident()
        self.entered.set()
        self.expired = not self.release.wait(2.0)
        self.completed.set()


BRIDGE_SIGNALS = (
    ("metrics_by_handle", "metrics_changed"),
    ("new_descriptors_by_handle", "descriptors_added"),
    ("updated_descriptors_by_handle", "descriptors_updated"),
    ("deleted_descriptors_by_handle", "descriptors_deleted"),
    ("operation_by_handle", "operations_changed"),
    ("alert_by_handle", "alerts_changed"),
    ("context_by_handle", "contexts_changed"),
    ("waveform_by_handle", "waveforms_changed"),
    ("sequence_or_instance_id_changed_event", "peer_restarted"),
)


def consumer_mdib() -> ConsumerMdib:
    """Use the real SDK MDIB and observables without starting a transport."""
    return ConsumerMdib(NS(sdc_definitions=SdcV1Definitions, log_prefix="audit", msg_reader=None))


class GuiAuditRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @contextmanager
    def consumer(self, *, real_bridge=False):
        bridge_patch = nullcontext() if real_bridge else patch.object(cp, "MdibBridge", FakeBridge)
        with patch.object(cp, "ConsumerService", FakeConsumerService), bridge_patch:
            pane = cp.ConsumerPane("127.0.0.1")
            try:
                yield pane
            finally:
                pane.service.scan_call.release.set()
                pane.service.connect_call.release.set()
                pane.shutdown()
                self.assertTrue(pane._worker.wait(2.0), "test cleanup must finish")
                if shiboken6.isValid(pane):
                    shiboken6.delete(pane)
                self.app.processEvents()

    def test_real_bridge_open_signals_preserve_payloads_and_unrelated_failures(self) -> None:
        mdib = consumer_mdib()
        bridge = MdibBridge(mdib)
        received = {}
        try:
            for observable, signal_name in BRIDGE_SIGNALS:
                getattr(bridge, signal_name).connect(
                    lambda *args, name=signal_name: received.setdefault(name, []).append(args),
                    Qt.DirectConnection,
                )
                if signal_name == "peer_restarted":
                    setattr(mdib, observable, False)
                    self.assertNotIn(signal_name, received)
                    setattr(mdib, observable, True)
                    self.assertEqual(received[signal_name], [()])
                elif signal_name == "waveforms_changed":
                    samples = [D(1), D(2)]
                    mdib.waveform_by_handle = {
                        "wave": NS(MetricValue=NS(Samples=samples)), "new": NS(),
                    }
                    samples.clear()
                    self.assertEqual(received[signal_name], [({"wave": [D(1), D(2)], "new": []},)])
                else:
                    state = object()
                    values = {"metric": state}
                    setattr(mdib, observable, values)
                    values.clear()
                    self.assertEqual(received[signal_name], [({"metric": state},)])

            def unrelated_failure(*_args) -> None:
                raise RuntimeError("unrelated emission failure")

            with patch.object(bridge, "metrics_changed", NS(emit=unrelated_failure)):
                with self.assertRaisesRegex(RuntimeError, "unrelated emission failure"):
                    mdib.metrics_by_handle = {}
        finally:
            shiboken6.delete(bridge)

    def test_waveform_activation_change_is_reported_as_a_metric_change(self) -> None:
        mdib = consumer_mdib()
        bridge = MdibBridge(mdib)
        changed = []
        bridge.metrics_changed.connect(lambda values: changed.append(set(values)), Qt.DirectConnection)
        try:
            mdib.waveform_by_handle = {"wave": NS(ActivationState="On", MetricValue=NS(Samples=[D(1)]))}
            mdib.waveform_by_handle = {"wave": NS(ActivationState="On", MetricValue=NS(Samples=[D(2)]))}
            self.assertEqual(changed, [], "blocks of an unchanged activation reread nothing")
            mdib.waveform_by_handle = {"wave": NS(ActivationState="Off", MetricValue=NS(Samples=[D(2)]))}
            self.assertEqual(changed, [{"wave"}], "switching a waveform off is a metric change")
            mdib.waveform_by_handle = {"wave": NS(ActivationState="Off", MetricValue=NS(Samples=[D(2)]))}
            self.assertEqual(changed, [{"wave"}], "only once")
            mdib.waveform_by_handle = {"late": NS(ActivationState="Off", MetricValue=NS(Samples=[]))}
            self.assertEqual(changed[-1], {"late"}, "a waveform first seen Off is reported too")
        finally:
            bridge.close()

    def test_real_bridge_acquired_sdk_callbacks_are_suppressed_before_qt_deletion(self) -> None:
        for mode in ("disconnect", "shutdown"):
            for observable, signal_name in BRIDGE_SIGNALS:
                with self.subTest(mode=mode, signal=signal_name), self.consumer(real_bridge=True) as pane:
                    closer = GatedCloser()
                    mdib = consumer_mdib()
                    remote = RemoteDevice(NS(stop_all=lambda **_: closer()), mdib, "late-mdib")
                    pane._on_connected(remote, pane._generation)
                    bridge, worker = pane.bridge, pane._worker
                    self.assertIsInstance(bridge, MdibBridge)
                    acquired, resume = threading.Event(), threading.Event()
                    errors, emitted, resolved_callbacks = [], [], []
                    getattr(bridge, signal_name).connect(lambda *args: emitted.append(args), Qt.DirectConnection)
                    resolve = WeakRef.get_ref

                    def acquire_before_unbinding(ref):
                        callback = resolve(ref)
                        if getattr(callback, "__self__", None) is bridge:
                            resolved_callbacks.append(callback)
                            acquired.set()
                            if not resume.wait(2.0):
                                raise TimeoutError("test callback gate was not released")
                        return callback

                    def dispatch() -> None:
                        try:
                            # Incoming SDK reports hold this lock while notifying observers.
                            with mdib.mdib_lock:
                                value = True if signal_name == "peer_restarted" else {
                                    "wave": NS(MetricValue=NS(Samples=[D(1)])),
                                }
                                setattr(mdib, observable, value)
                        except Exception as exc:
                            errors.append(exc)

                    receiver = threading.Thread(target=dispatch)
                    try:
                        with patch.object(WeakRef, "get_ref", acquire_before_unbinding):
                            receiver.start()
                            self.assertTrue(acquired.wait(1.0), "SDK must acquire the real bound callback first")
                            started = time.monotonic()
                            if mode == "disconnect":
                                pane._on_disconnect()
                            else:
                                pane.shutdown()
                            self.assertLess(time.monotonic() - started, 0.6, "close cannot wait for transport cleanup")
                            self.assertTrue(closer.entered.wait(1.0))
                            self.assertFalse(closer.completed.is_set())
                            QCoreApplication.sendPostedEvents(bridge, QEvent.DeferredDelete)
                            if mode == "shutdown":
                                shiboken6.delete(pane)
                            self.assertFalse(shiboken6.isValid(bridge), "the real Qt signal source must be deleted")
                            resume.set()
                            receiver.join(1.0)
                            self.assertFalse(receiver.is_alive())
                            self.assertEqual(errors, [], "an acquired SDK callback must not emit through deleted Qt")
                            self.assertEqual(emitted, [], "closed bridges suppress every signal family")
                            setattr(mdib, observable, True if signal_name == "peer_restarted" else {})
                            self.assertEqual(len(resolved_callbacks), 1, "close must also unbind future SDK dispatch")
                            self.assertFalse(worker.wait(0), "the external stop_all must still be blocked")
                    finally:
                        resume.set()
                        closer.release.set()
                        if receiver.ident is not None:
                            receiver.join(2.0)
                        self.assertFalse(receiver.is_alive())
                        self.assertTrue(worker.wait(2.0))
                    self.assertEqual(closer.count, 1)
                    self.assertNotEqual(closer.thread_id, threading.get_ident())
                    self.assertFalse(closer.expired)

    def test_real_bridge_teardown_synchronizes_with_an_inflight_emission(self) -> None:
        with self.consumer(real_bridge=True) as pane:
            closer = GatedCloser()
            mdib = consumer_mdib()
            pane._on_connected(RemoteDevice(NS(stop_all=lambda **_: closer()), mdib, "inflight-mdib"), pane._generation)
            bridge = pane.bridge
            native_signal = bridge.metrics_changed
            emitting, resume, deleted = threading.Event(), threading.Event(), threading.Event()
            emitted_after_delete, errors = [], []

            def paused_emit(*args) -> None:
                emitting.set()
                if not resume.wait(2.0):
                    raise TimeoutError("test emission gate was not released")
                emitted_after_delete.append(deleted.is_set())
                native_signal.emit(*args)

            def dispatch() -> None:
                try:
                    with mdib.mdib_lock:
                        mdib.metrics_by_handle = {}
                except Exception as exc:
                    errors.append(exc)

            def release_after_teardown_attempt() -> None:
                deleted.wait(0.2)
                resume.set()

            receiver = threading.Thread(target=dispatch)
            releaser = threading.Thread(target=release_after_teardown_attempt)
            try:
                with patch.object(bridge, "metrics_changed", NS(emit=paused_emit)):
                    receiver.start()
                    self.assertTrue(emitting.wait(1.0))
                    releaser.start()
                    pane._on_disconnect()
                    QCoreApplication.sendPostedEvents(bridge, QEvent.DeferredDelete)
                    self.assertFalse(shiboken6.isValid(bridge))
                    deleted.set()
                    receiver.join(1.0)
                    releaser.join(1.0)
                self.assertFalse(receiver.is_alive())
                self.assertFalse(releaser.is_alive())
                self.assertEqual(emitted_after_delete, [False], "teardown must synchronize closed check and emission")
                self.assertEqual(errors, [])
                self.assertFalse(closer.completed.is_set())
            finally:
                resume.set()
                deleted.set()
                closer.release.set()
                for thread in (receiver, releaser):
                    if thread.ident is not None:
                        thread.join(2.0)
                    self.assertFalse(thread.is_alive())
                self.assertTrue(pane._worker.wait(2.0))
            self.assertFalse(closer.expired)

    def test_provider_reports_refresh_unfocused_editor_without_overwriting_drafts(self) -> None:
        with WindowFixture("audit-provider-editor") as fixture:
            service, pane = fixture.service, fixture.window.provider_pane
            number = service.add_metric(MetricSpec("Number", K.NUMBER, initial_value=D(7)))
            mode = service.add_metric(MetricSpec("Mode", K.CHOICE, allowed_values=("IDLE", "RUN", "PAUSE"), initial_value="RUN"))
            pane.refresh()
            pane.set_use_widgets(False)
            pane.select_handle(number)
            pane.table.setFocus()
            self.app.processEvents()
            self.assertFalse(pane.value_edit.hasFocus())
            self.assertEqual(pane.value_edit.text(), "7")

            service.set_value(number, D(9))
            row = next(row for row in range(pane.table.rowCount()) if pane.table.item(row, 0).text() == number)
            self.assertTrue(wait_for(self.app, lambda: pane.table.item(row, COL_VALUE).text() == "9"))
            with self.subTest("unfocused refresh"):
                self.assertEqual(pane.value_edit.text(), "9")
            pane.apply_button.click()
            with self.subTest("Set cannot restore stale 7"):
                self.assertEqual(service.get_value(number), D(9))

            pane.value_edit.setFocus()
            self.app.processEvents()
            self.assertTrue(pane.value_edit.hasFocus())
            pane.value_edit.setText("11")
            service.set_value(number, D(8))
            self.assertTrue(wait_for(self.app, lambda: pane.table.item(row, COL_VALUE).text() == "8"))
            self.assertEqual(pane.value_edit.text(), "11", "a focused draft wins over reports")

            pane.select_handle(mode)
            pane.choice_box.showPopup()
            self.app.processEvents()
            self.assertTrue(pane.choice_box.view().isVisible())
            try:
                service.set_value(mode, "PAUSE")
                self.app.processEvents()
                self.assertEqual(pane.choice_box.currentText(), "RUN", "an open choice draft must survive")
            finally:
                pane.choice_box.hidePopup()
            service.set_value(mode, "IDLE")
            self.assertTrue(wait_for(self.app, lambda: pane.choice_box.currentText() == "IDLE", timeout=0.5))
            pane.select_handle(number)
            service.remove_metric(number)
            pane._on_values_changed({number: object()})  # A value report can precede queued deletion.

    def test_missing_card_values_clear_instead_of_showing_old_readings(self) -> None:
        for spec, value, shown in (
            (WidgetSpec("stepper", "Number", K.NUMBER), D(7), lambda c: c.edit.text()),
            (WidgetSpec("slider", "Bounded", K.NUMBER, minimum=D(0), maximum=D(10)), D(7), lambda c: c.readout.text()),
            (WidgetSpec("choice", "Mode", K.CHOICE, allowed_values=("IDLE", "RUN")), "RUN", lambda c: c.box.currentText()),
        ):
            with self.subTest(spec.handle):
                control = build_widget(spec)
                try:
                    requested = []
                    control.value_requested.connect(lambda h, v: requested.append((h, v)))
                    control.show_value(value)
                    self.assertEqual(shown(control), str(value))
                    control.show_value(None)
                    self.assertIn(shown(control), ("", NO_VALUE))
                    if spec.kind is K.CHOICE:
                        self.assertEqual(control.box.currentIndex(), -1)
                        self.assertEqual(control.box.placeholderText(), NO_VALUE)
                    self.assertEqual(requested, [], "refreshing or clearing must not write")
                finally:
                    shiboken6.delete(control)

    def test_provider_rebuild_preserves_focused_same_definition_draft(self) -> None:
        with WindowFixture("followup-provider-draft") as fixture:
            service, pane = fixture.service, fixture.window.provider_pane
            number = service.add_metric(MetricSpec("Number", K.NUMBER, initial_value=D(7), handle="m.zznumber"))
            other = service.add_metric(MetricSpec("Other", K.NUMBER, initial_value=D(3), controllable=True))
            pane.refresh()
            pane.set_use_widgets(False)
            self.app.processEvents()
            pane.select_handle(number)
            pane.value_edit.setFocus()
            self.app.processEvents()
            self.assertTrue(pane.value_edit.hasFocus())
            changes = []
            pane.value_edit.textChanged.connect(changes.append)
            for event in ("descriptor", "operation"):
                pane.value_edit.setText("9")
                changes.clear()
                if event == "descriptor":
                    service.add_metric(MetricSpec("Earlier", K.NUMBER, handle="m.aaa"))
                    self.assertTrue(wait_for(self.app, lambda: pane.table.rowCount() == 3))
                else:
                    service.disable_control(other)
                    row = next(row for row in range(pane.table.rowCount()) if pane.table.item(row, 0).text() == other)
                    self.assertTrue(wait_for(self.app, lambda: pane.table.item(row, COL_CONTROL).checkState() == Qt.Unchecked))
                with self.subTest(event=event):
                    self.assertEqual(pane.selected_handle(), number)
                    self.assertEqual(pane.value_edit.text(), "9", "an unrelated rebuild cannot replace the focused draft with current 7")
                    self.assertEqual(changes, [], "intermediate selection signals cannot reset and then restore a draft")
                    self.assertEqual(service.get_value(number), D(7))

            pane.value_edit.setText("9")
            service.list_metrics()[number].maximum = D(8)
            pane.refresh()
            self.assertEqual(pane.value_edit.text(), "7", "a changed numeric domain resets the old draft")
            pane.value_edit.setText("9")
            pane.select_handle(other)
            self.assertEqual(pane.value_edit.text(), "3", "selecting another metric resets a focused draft")
            pane.select_handle(number)
            pane.value_edit.setText("9")
            service.remove_metric(number)
            self.assertTrue(wait_for(self.app, lambda: number not in pane.board.handles))
            self.assertIsNone(pane.selected_handle())
            self.assertFalse(pane.editor_stack.isEnabled(), "deletion cannot leave the draft writable")

    def test_provider_rebuild_preserves_open_same_definition_choice(self) -> None:
        with WindowFixture("followup-provider-choice") as fixture:
            service, pane = fixture.service, fixture.window.provider_pane
            mode = service.add_metric(MetricSpec("Mode", K.CHOICE, allowed_values=("IDLE", "RUN", "PAUSE"), initial_value="RUN"))
            other = service.add_metric(MetricSpec("Other", K.NUMBER, controllable=True))
            pane.refresh()
            pane.set_use_widgets(False)
            self.app.processEvents()
            pane.select_handle(mode)
            pane.choice_box.setCurrentIndex(2)
            pane.choice_box.showPopup()
            self.app.processEvents()
            self.assertTrue(pane.choice_box.view().isVisible())
            try:
                for event in ("descriptor", "operation"):
                    if event == "descriptor":
                        service.add_metric(MetricSpec("Earlier", K.NUMBER, handle="m.aaa"))
                        self.assertTrue(wait_for(self.app, lambda: pane.table.rowCount() == 3))
                    else:
                        service.disable_control(other)
                        row = next(row for row in range(pane.table.rowCount()) if pane.table.item(row, 0).text() == other)
                        self.assertTrue(wait_for(self.app, lambda: pane.table.item(row, COL_CONTROL).checkState() == Qt.Unchecked))
                    with self.subTest(event=event):
                        self.assertEqual(pane.choice_box.currentText(), "PAUSE")
                        self.assertTrue(pane.choice_box.view().isVisible(), "the open choice must not be rebuilt")
                        self.assertEqual(service.get_value(mode), "RUN", "a preserved draft is not a write")
                service.list_metrics()[mode].allowed_values = ("IDLE", "RUN")
                pane.refresh()
                self.assertEqual(tuple(pane.choice_box.itemText(i) for i in range(pane.choice_box.count())), ("IDLE", "RUN"))
                self.assertEqual(pane.choice_box.currentText(), "RUN", "a changed choice domain drops the old draft")
            finally:
                pane.choice_box.hidePopup()

    def test_consumer_rebuild_preserves_focused_same_definition_draft(self) -> None:
        with self.consumer() as pane:
            remote = FakeRemote("followup-numeric-draft")
            remote.metric_values["zznumber"] = RemoteMetric(
                "zznumber", "NumericMetricDescriptor", kind=K.NUMBER, value=D(7),
                operation_handles=("set.number",), selected_operation_handle="set.number", controllable_now=True,
            )
            remote.metric_values["other"] = RemoteMetric("other", "NumericMetricDescriptor", kind=K.NUMBER, value=D(3))
            pane._on_connected(remote, pane._generation)
            pane.set_use_widgets(False)
            pane.show()
            pane.activateWindow()
            pane.select_handle("zznumber")
            pane.value_edit.setFocus()
            self.app.processEvents()
            self.assertTrue(pane.value_edit.hasFocus())
            changes = []
            pane.value_edit.textChanged.connect(changes.append)
            for event in ("descriptor", "operation"):
                pane.value_edit.setText("9")
                changes.clear()
                if event == "descriptor":
                    remote.metric_values["aaa"] = RemoteMetric("aaa", "NumericMetricDescriptor", kind=K.NUMBER)
                    pane.bridge.descriptors_added.emit({"aaa": object()})
                else:
                    pane.bridge.operations_changed.emit({"set.other": object()})
                with self.subTest(event=event):
                    self.assertEqual(pane.selected_handle(), "zznumber")
                    self.assertEqual(pane.value_edit.text(), "9", "an unrelated rebuild cannot replace the focused draft with current 7")
                    self.assertEqual(changes, [], "intermediate selection signals cannot reset and then restore a draft")
                    self.assertFalse(remote.set_call.entered.is_set())

            for definition in (
                {"maximum": D(8), "allowed_ranges": (RemoteRange(None, D(8)),)},
                {"operation_handles": ("set.replacement",), "selected_operation_handle": "set.replacement"},
            ):
                pane.value_edit.setText("9")
                remote.metric_values["zznumber"] = replace(remote.metric_values["zznumber"], **definition)
                pane.refresh()
                self.assertEqual(pane.value_edit.text(), "7", "a changed domain or selected operation resets the draft")
            pane.value_edit.setText("9")
            pane.select_handle("other")
            self.assertEqual(pane.value_edit.text(), "3", "selection changes cannot keep a different metric's draft")
            pane.select_handle("zznumber")
            remote.metric_values["zznumber"] = replace(remote.metric_values["zznumber"], controllable_now=False)
            pane.refresh()
            self.assertFalse(pane.editor_stack.isEnabled(), "disabled control still wins over focus")
            del remote.metric_values["zznumber"]
            pane.bridge.descriptors_deleted.emit({"zznumber": object()})
            self.assertIsNone(pane.selected_handle())
            self.assertFalse(pane.apply_button.isEnabled(), "deletion cannot keep a writable draft")

    def test_consumer_rebuild_preserves_open_same_definition_choice(self) -> None:
        with self.consumer() as pane:
            remote = FakeRemote("followup-choice-draft")
            remote.metric_values["mode"] = RemoteMetric(
                "mode", "EnumStringMetricDescriptor", kind=K.CHOICE, value="RUN", allowed_values=("IDLE", "RUN", "PAUSE"),
                operation_handles=("set.mode",), selected_operation_handle="set.mode", controllable_now=True,
            )
            pane._on_connected(remote, pane._generation)
            pane.set_use_widgets(False)
            pane.show()
            pane.select_handle("mode")
            pane.choice_box.setCurrentIndex(2)
            pane.choice_box.showPopup()
            self.app.processEvents()
            self.assertTrue(pane.choice_box.view().isVisible())
            try:
                for signal in (pane.bridge.descriptors_added, pane.bridge.operations_changed):
                    signal.emit({"unrelated": object()})
                    with self.subTest(signal=signal):
                        self.assertEqual(pane.choice_box.currentText(), "PAUSE")
                        self.assertTrue(pane.choice_box.view().isVisible())
                        self.assertFalse(remote.set_call.entered.is_set())
                remote.metric_values["mode"] = replace(remote.metric_values["mode"], operation_allowed_values=("IDLE", "RUN"))
                pane.refresh()
                self.assertEqual(tuple(pane.choice_box.itemText(i) for i in range(pane.choice_box.count())), ("IDLE", "RUN"))
                self.assertEqual(pane.choice_box.currentText(), "RUN", "a changed operation choice domain resets the draft")
            finally:
                pane.choice_box.hidePopup()

    def test_missing_reports_preserve_active_card_drafts(self) -> None:
        stepper = build_widget(WidgetSpec("number", "Number", K.NUMBER, editable=True))
        choice = build_widget(WidgetSpec("choice", "Mode", K.CHOICE, allowed_values=("IDLE", "RUN"), editable=True))
        slider = build_widget(WidgetSpec("slider", "Bounded", K.NUMBER, minimum=D(0), maximum=D(10), editable=True))
        try:
            stepper.show_value(D(7))
            stepper.show()
            stepper.edit.setFocus()
            self.app.processEvents()
            self.assertTrue(stepper.edit.hasFocus())
            stepper.edit.setText("9.5")
            stepper.show_value(None)
            self.assertEqual(stepper.edit.text(), "9.5")
            slider.show_value(D(7))
            slider.slider.setSliderDown(True)
            slider.show_value(None)
            self.assertEqual(slider.readout.text(), "7")
            slider.slider.setSliderDown(False)
            choice.show_value("RUN")
            choice.show()
            choice.box.showPopup()
            self.app.processEvents()
            self.assertTrue(choice.box.view().isVisible())
            choice.show_value(None)
            self.assertEqual(choice.box.currentText(), "RUN")
        finally:
            choice.box.hidePopup()
            for control in (stepper, choice, slider):
                shiboken6.delete(control)

    def test_unmatched_and_missing_choices_are_display_only_not_write_candidates(self) -> None:
        with self.consumer() as pane:
            remote = FakeRemote("choice")
            remote.set_call.release.set()
            remote.metric_values["mode"] = RemoteMetric(
                "mode", "EnumStringMetricDescriptor", kind=K.CHOICE,
                allowed_values=("IDLE", "RUN", "PAUSE"), operation_allowed_values=("IDLE", "RUN"),
                operation_handles=("set.mode",), selected_operation_handle="set.mode",
                value="PAUSE", controllable_now=True,
            )
            pane._on_connected(remote, pane._generation)
            pane.select_handle("mode")
            card = pane.board.card("mode").control
            for name, box in (("card", card.box), ("table", pane.choice_box)):
                with self.subTest("unmatched current", control=name):
                    self.assertEqual(tuple(box.itemText(i) for i in range(box.count())), ("IDLE", "RUN"))
                    self.assertEqual(box.currentIndex(), -1)
                    self.assertEqual(box.placeholderText(), "PAUSE")
            pane._on_apply()
            with self.subTest("no placeholder write"):
                self.assertFalse(remote.set_call.entered.is_set())
            self.assertTrue(pane._worker.wait(1.0))
            self.app.processEvents()
            remote.metric_values["mode"].value = "RUN"
            pane.bridge.metrics_changed.emit({"mode": object()})
            self.assertEqual(card.box.currentText(), "RUN")
            remote.metric_values["mode"].value = None
            pane.bridge.metrics_changed.emit({"mode": object()})
            for name, box in (("card", card.box), ("table", pane.choice_box)):
                with self.subTest("missing current", control=name):
                    self.assertEqual(box.currentIndex(), -1)
                    self.assertEqual(box.placeholderText(), NO_VALUE)
            pane._restore_device_value("mode")
            self.assertEqual(pane.choice_box.currentIndex(), -1, "completion cannot restore a stale choice")

            remote.metric_values["number"] = RemoteMetric("number", "NumericMetricDescriptor", kind=K.NUMBER, value=D(7))
            pane.refresh()
            remote.metric_values["number"].value = None
            pane.bridge.metrics_changed.emit({"number": object()})
            self.assertEqual(pane.board.card("number").control.edit.text(), "")
            row = next(row for row in range(pane.table.rowCount()) if pane.table.item(row, 0).text() == "number")
            self.assertEqual(pane.table.item(row, cp.COL_VALUE).text(), NO_VALUE)

    def test_remote_sample_technical_bounds_match_local_plots(self) -> None:
        for kind, node_type in ((K.WAVEFORM, pm.RealTimeSampleArrayMetricDescriptor), (K.DISTRIBUTION, pm.DistributionSampleArrayMetricDescriptor)):
            with self.subTest(kind.value):
                entity = NS(node_type=node_type, descriptor=NS(TechnicalRange=[NS(Lower=D(0), Upper=D(100))], SamplePeriod=0.01), state=NS(MetricValue=NS(Samples=[D(50), D("50.01")])))
                remote = RemoteDevice(NS(), fake_consumer_mdib({"wave": entity}), "peer")
                metric = remote.metrics()["wave"]
                control = build_widget(from_remote_metric(metric))
                local = build_widget(from_spec("wave", MetricSpec("Wave", kind, minimum=D(0), maximum=D(100)), editable=False))
                try:
                    for widget in (control, local):
                        widget.append_samples(metric.samples)
                        widget.plot.flush()
                    self.assertEqual(local.plot._bounds(), (0.0, 100.0))
                    self.assertEqual(control.plot._bounds(), (0.0, 100.0))
                    self.assertEqual(metric.allowed_ranges, (), "technical bounds are not operation limits")
                finally:
                    shiboken6.delete(control)
                    shiboken6.delete(local)

    def test_location_only_discovery_update_preserves_selection_and_connect_next(self) -> None:
        with self.consumer() as pane:
            service = pane.service
            other = DiscoveredDevice("other", ("http://127.0.0.1/other",), ())
            before = DiscoveredDevice("peer", ("http://127.0.0.1/peer",), ("sdc.ctxt.loc:OR1",))
            after = DiscoveredDevice("peer", before.x_addrs, ("sdc.ctxt.loc:OR2",))
            service.announce(other, before)
            self.assertTrue(wait_for(self.app, lambda: len(pane.devices) == 2))
            pane.device_list.setCurrentRow(1)
            pane.connect_next_box.setChecked(True)
            service.announce(other, after)
            self.assertTrue(wait_for(self.app, lambda: pane.devices[1].location_scope == "sdc.ctxt.loc:OR2", timeout=0.5))
            self.assertIs(pane._selected_device(), after)
            self.assertEqual(pane.device_list.item(1).toolTip(), "sdc.ctxt.loc:OR2")
            self.assertTrue(pane.connect_next_box.isChecked())
            self.assertFalse(service.connect_call.entered.is_set())
            fresh = DiscoveredDevice("fresh", ("http://127.0.0.1/fresh",), ())
            connected = FakeRemote("fresh")
            service.connect_call.result = connected
            service.connect_call.release.set()
            service.announce(other, after, fresh)
            self.assertTrue(wait_for(self.app, lambda: pane.remote is connected))
            self.assertFalse(pane.connect_next_box.isChecked())
            self.assertIs(pane._selected_device(), fresh)

    def test_blocked_disconnect_cleanup_does_not_block_reconnect_or_shutdown(self) -> None:
        with self.consumer() as pane:
            closer = GatedCloser()
            remote = FakeRemote("old")
            remote.close = closer
            pane._on_connected(remote, pane._generation)
            try:
                pane._on_disconnect()
                self.assertTrue(closer.entered.wait(1.0))
                with self.subTest("disconnect is nonblocking"):
                    self.assertFalse(closer.completed.is_set())
                    self.assertNotEqual(closer.thread_id, threading.get_ident())
                    self.assertFalse(pane._worker.wait(0))
                replacement = FakeRemote("new")
                pane.service.connect_call.result = replacement
                pane.service.connect_call.release.set()
                pane.service.announce(DiscoveredDevice("new", ("http://127.0.0.1/new",), ()))
                self.assertTrue(wait_for(self.app, lambda: bool(pane.devices)))
                pane._on_connect()
                self.assertTrue(wait_for(self.app, lambda: pane.remote is replacement))
                ticked = threading.Event()
                QTimer.singleShot(0, ticked.set)
                self.assertTrue(wait_for(self.app, ticked.is_set, timeout=0.5))
                started = time.monotonic()
                pane.shutdown()
                self.assertLess(time.monotonic() - started, 0.6)
                with self.subTest("shutdown still tracks the blocked closer"):
                    self.assertFalse(closer.completed.is_set())
                    self.assertFalse(pane._worker.wait(0))
            finally:
                closer.release.set()
                self.assertTrue(pane._worker.wait(2.0))
            self.assertEqual(closer.count, 1)
            self.assertFalse(closer.expired)

    def test_stale_connection_results_retire_once_off_the_gui_thread(self) -> None:
        with self.consumer() as pane:
            closer = GatedCloser()
            remote = FakeRemote("stale")
            remote.close = closer
            try:
                pane._on_connected(remote, pane._generation - 1)
                self.assertTrue(closer.entered.wait(1.0))
                pane._on_connected(remote, pane._generation - 1)
                self.assertFalse(closer.completed.is_set())
                self.assertNotEqual(closer.thread_id, threading.get_ident())
                self.assertIsNone(pane.remote)
                self.assertFalse(pane._worker.wait(0))
            finally:
                closer.release.set()
                pane._worker.wait(2.0)
            self.assertEqual(closer.count, 1)
            self.assertFalse(closer.expired)

    def test_retired_invocation_cleanup_is_tracked_and_cannot_emit_after_owner_deletion(self) -> None:
        for error in (None, RuntimeError("controlled invocation failure")):
            with self.subTest(error=error):
                owner = QObject()
                worker = AsyncCall(owner, expected_errors=(RuntimeError,))
                resource = EqualResource("invoked")
                resource_ref = weakref.ref(resource)
                call, closer = BlockingCall(42), GatedCloser()
                call.error = error
                emitted, errors = [], []
                thread = None
                worker.managed_finished.connect(lambda *args: emitted.append(args), Qt.DirectConnection)
                worker.managed_failed.connect(lambda *args: emitted.append(args), Qt.DirectConnection)
                with patch.object(threading, "excepthook", lambda args: errors.append(args.exc_value)):
                    try:
                        self.assertTrue(worker.start_managed("invoke", resource, call))
                        self.assertTrue(call.entered.wait(1.0))
                        thread = worker._threads["invoke"]
                        worker.retire(resource, closer)
                        worker.retire(resource, closer)
                        call.release.set()
                        self.assertTrue(closer.entered.wait(1.0))
                        with self.subTest("cleanup is still work"):
                            self.assertFalse(worker.wait(0))
                            self.assertTrue(worker.busy_for("invoke"))
                            self.assertFalse(worker.start_managed("invoke", None, lambda: None))
                        with self.subTest("bounded close is incomplete"):
                            self.assertFalse(worker.close(0))
                        shiboken6.delete(owner)
                    finally:
                        worker.close(0)
                        call.release.set()
                        closer.release.set()
                        if thread is not None:
                            thread.join(2.0)
                        if shiboken6.isValid(owner):
                            shiboken6.delete(owner)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [], "cleanup must not emit through a deleted QObject")
                self.assertEqual(emitted, [])
                self.assertEqual(closer.count, 1)
                # The test owns this exception; its traceback otherwise retains the call frame.
                if error is not None:
                    error.__traceback__ = None
                del resource
                gc.collect()
                self.assertIsNone(resource_ref(), "completed cleanup must release its resource")

    def test_queued_result_discard_is_nonblocking_tracked_and_single_close(self) -> None:
        owner = QObject()
        worker = AsyncCall(owner)
        result, closer = EqualResource("queued"), GatedCloser()
        try:
            self.assertTrue(worker.start_managed("connect", None, lambda: result, discard_result=lambda _: closer()))
            self.assertTrue(worker.wait(1.0))  # Keep the Qt result queued, not claimed.
            with self.subTest("close cannot finish during queued-result discard"):
                self.assertFalse(worker.close(0))
            self.assertTrue(closer.entered.wait(1.0))
            self.assertNotEqual(closer.thread_id, threading.get_ident())
            self.assertFalse(worker.close(0))
            self.assertFalse(worker.claim_result(result))
            shiboken6.delete(owner)
        finally:
            closer.release.set()
            worker.close(0)
            self.assertTrue(worker.wait(2.0))
            if shiboken6.isValid(owner):
                shiboken6.delete(owner)
        self.assertEqual(closer.count, 1)
        self.assertFalse(closer.expired)

    def test_idle_retirement_can_retry_after_cleanup_thread_start_failure(self) -> None:
        worker, resource = AsyncCall(), EqualResource("retry-close")
        closed = []
        try:
            with patch.object(threading.Thread, "start", side_effect=RuntimeError("forced start failure")):
                with self.assertRaisesRegex(RuntimeError, "forced start failure"):
                    worker.retire(resource, lambda: closed.append(resource))
            self.assertEqual(closed, [])
            worker.retire(resource, lambda: closed.append(resource))
            self.assertTrue(worker.wait(1.0))
            self.assertEqual(closed, [resource])
        finally:
            worker.close()
            shiboken6.delete(worker)

    def test_idle_resource_is_retained_until_cleanup_finishes_then_collected(self) -> None:
        worker, resource, closer = AsyncCall(), EqualResource("held-during-close"), GatedCloser()
        resource_ref = weakref.ref(resource)
        try:
            worker.retire(resource, closer)
            self.assertTrue(closer.entered.wait(1.0))
            del resource
            gc.collect()
            self.assertIsNotNone(resource_ref(), "cleanup owns the resource even if its closer uses a weak reference")
            self.assertFalse(worker.wait(0))
        finally:
            closer.release.set()
            self.assertTrue(worker.wait(2.0))
            worker.close()
            shiboken6.delete(worker)
        gc.collect()
        self.assertIsNone(resource_ref())
        self.assertEqual(closer.count, 1)

    def test_late_connection_discard_stays_tracked_after_owner_deletion(self) -> None:
        owner, closer = QObject(), GatedCloser()
        worker, result = AsyncCall(owner), EqualResource("late-result")
        call, thread = BlockingCall(result), None
        errors = []
        with patch.object(threading, "excepthook", lambda args: errors.append(args.exc_value)):
            try:
                self.assertTrue(worker.start_managed("connect", None, call, discard_result=lambda _: closer()))
                self.assertTrue(call.entered.wait(1.0))
                thread = worker._threads["connect"]
                self.assertFalse(worker.close(0))
                shiboken6.delete(owner)
                call.release.set()
                self.assertTrue(closer.entered.wait(1.0))
                self.assertFalse(worker.wait(0))
                self.assertNotEqual(closer.thread_id, threading.get_ident())
            finally:
                worker.close(0)
                call.release.set()
                closer.release.set()
                if thread is not None:
                    thread.join(2.0)
                if shiboken6.isValid(owner):
                    shiboken6.delete(owner)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(closer.count, 1)
        self.assertFalse(closer.expired)

    def test_queued_discard_survives_cleanup_thread_start_failure(self) -> None:
        worker, result = AsyncCall(), EqualResource("retry-discard")
        closed = []
        try:
            self.assertTrue(worker.start_managed("connect", None, lambda: result, discard_result=closed.append))
            self.assertTrue(worker.wait(1.0))
            with patch.object(threading.Thread, "start", side_effect=RuntimeError("forced start failure")):
                with self.assertRaisesRegex(RuntimeError, "forced start failure"):
                    worker.close(0)
            self.assertEqual(closed, [])
            self.assertTrue(worker.close(1.0))
            self.assertEqual(closed, [result])
            self.assertFalse(worker.claim_result(result))
        finally:
            worker.close()
            shiboken6.delete(worker)

    def test_retiring_real_remote_cannot_emit_health_after_pane_deletion(self) -> None:
        with self.consumer() as pane:
            closer = GatedCloser()
            remote = RemoteDevice(
                NS(stop_all=lambda **_: closer()), fake_consumer_mdib(), "late-health",
            )
            pane._on_connected(remote, pane._generation)
            worker = pane._worker
            health = threading.Thread(target=remote.mark_connection_lost, args=("loss during blocked cleanup",))
            try:
                pane.shutdown()
                self.assertTrue(closer.entered.wait(1.0))
                self.assertFalse(worker.wait(0))
                shiboken6.delete(pane)
                with self.assertLogs("sdctoolbox.consumer", level="WARNING") as records:
                    health.start()
                    health.join(1.0)
                self.assertFalse(health.is_alive())
                errors = [record for record in records.records if record.levelno >= logging.ERROR]
                self.assertEqual(errors, [], records.output)
                self.assertFalse(worker.wait(0), "health notification must not release blocked cleanup")
            finally:
                closer.release.set()
                if health.ident is not None:
                    health.join(2.0)
                self.assertTrue(worker.wait(2.0))
            self.assertEqual(closer.count, 1)
            self.assertNotEqual(closer.thread_id, threading.get_ident())
            self.assertFalse(closer.expired)

    def test_already_lost_remote_cannot_overwrite_synchronous_attachment_retirement(self) -> None:
        with self.consumer() as pane:
            closer = GatedCloser()
            closer.release.set()
            remote = RemoteDevice(
                NS(stop_all=lambda **_: closer()), fake_consumer_mdib(), "already-lost",
            )
            with self.assertLogs("sdctoolbox.consumer", level="WARNING"):
                remote.mark_connection_lost("advertised metric feeds were rejected")
            generation = pane._generation
            pane._on_connected(remote, generation)
            self.assertIsNone(pane.remote, "the already-lost callback retires attachment synchronously")
            self.assertIsNone(pane.bridge)
            self.assertEqual(pane._generation, generation + 1)
            self.assertTrue(pane.status_label.text().startswith("Connection lost:"), pane.status_label.text())
            self.assertIn("advertised metric feeds were rejected", pane.status_label.text())
            self.assertFalse(pane.disconnect_button.isEnabled())
            self.assertFalse(pane.apply_button.isEnabled())
            self.assertTrue(pane._worker.wait(1.0))
            self.assertEqual(closer.count, 1)
            self.assertNotEqual(closer.thread_id, threading.get_ident())

    def test_shutdown_waits_for_an_inflight_health_emission_before_pane_deletion(self) -> None:
        with self.consumer() as pane:
            closer = GatedCloser()
            remote = RemoteDevice(
                NS(stop_all=lambda **_: closer()), fake_consumer_mdib(), "inflight-health",
            )
            pane._on_connected(remote, pane._generation)
            native_signal = pane.connection_lost
            emitting, release_emit, deleted = threading.Event(), threading.Event(), threading.Event()
            emitted_after_delete = []

            def paused_emit(*args) -> None:
                emitting.set()
                if not release_emit.wait(2.0):
                    raise TimeoutError("test emission gate was not released")
                emitted_after_delete.append(deleted.is_set())
                native_signal.emit(*args)

            def release_after_shutdown_attempt() -> None:
                deleted.wait(1.0)
                release_emit.set()

            health = threading.Thread(target=remote.mark_connection_lost, args=("inflight health",))
            releaser = threading.Thread(target=release_after_shutdown_attempt)
            try:
                with (
                    patch.object(pane, "connection_lost", NS(emit=paused_emit)),
                    self.assertLogs("sdctoolbox.consumer", level="WARNING") as records,
                ):
                    health.start()
                    self.assertTrue(emitting.wait(1.0))
                    releaser.start()
                    pane.shutdown()
                    shiboken6.delete(pane)
                    deleted.set()
                    health.join(2.0)
                    releaser.join(2.0)
                self.assertFalse(health.is_alive())
                self.assertFalse(releaser.is_alive())
                self.assertEqual(emitted_after_delete, [False], "shutdown must synchronize with check + emission")
                self.assertEqual(
                    [record for record in records.records if record.levelno >= logging.ERROR], [], records.output,
                )
            finally:
                release_emit.set()
                closer.release.set()
                deleted.set()
                if health.ident is not None:
                    health.join(2.0)
                if releaser.ident is not None:
                    releaser.join(2.0)
                self.assertTrue(pane._worker.wait(2.0))
            self.assertEqual(closer.count, 1)
            self.assertFalse(closer.expired)

    def test_shutdown_retains_ownership_and_retries_remote_or_discovery_start_failure(self) -> None:
        for failing_start in (1, 2):
            with self.subTest(failing_start=failing_start), self.consumer() as pane:
                closer = GatedCloser()
                remote = FakeRemote("retry-shutdown")
                remote.close = closer
                pane._on_connected(remote, pane._generation)
                original_start = threading.Thread.start
                starts = 0

                def fail_cleanup_start(thread) -> None:
                    nonlocal starts
                    starts += 1
                    if starts == failing_start:
                        raise RuntimeError("forced cleanup start failure")
                    original_start(thread)

                try:
                    with patch.object(threading.Thread, "start", fail_cleanup_start):
                        with self.assertRaisesRegex(RuntimeError, "forced cleanup start failure"):
                            pane.shutdown()
                    with self.subTest("retirement ownership"):
                        self.assertIs(pane.remote, remote if failing_start == 1 else None)
                    with self.subTest("shutdown remains suppressed"):
                        self.assertTrue(pane._shutdown)
                        self.assertTrue(pane._worker.cancellation_event.is_set())
                        self.assertFalse(pane._worker.start_managed(("scan", pane._generation, None), None, lambda: None))
                        self.assertIsNone(pane.service.listener)
                        self.assertIsNone(pane.service.departure_listener)
                        self.assertFalse(pane._refresh_timer.isActive())
                        self.assertFalse(pane._probe_timer.isActive())
                    generation, status = pane._generation, pane.status_label.text()
                    pane.shutdown()
                    self.assertTrue(closer.entered.wait(1.0), "retry must schedule the retained remote")
                    self.assertIsNone(pane.remote)
                    self.assertEqual(pane._generation, generation, "retry must not start a new session")
                    self.assertTrue(wait_for(self.app, lambda: pane.service.stop_count == 1, timeout=0.5))
                    self.assertFalse(pane._worker.wait(0))
                    self.assertEqual(pane.status_label.text(), status)
                    self.assertNotEqual(closer.thread_id, threading.get_ident())
                finally:
                    closer.release.set()
                    self.assertTrue(pane._worker.wait(2.0))
                pane.shutdown()
                self.assertEqual(closer.count, 1)
                self.assertEqual(pane.service.stop_count, 1)
                self.assertFalse(closer.expired)

    def test_shutdown_retries_queued_disposal_without_claiming_after_start_failure(self) -> None:
        with self.consumer() as pane:
            closer = GatedCloser()
            remote = FakeRemote("retry-queued")
            remote.close = closer
            pane.service.connect_call.result = remote
            pane.service.connect_call.release.set()
            pane.service.known = [DiscoveredDevice("retry-queued", ("http://127.0.0.1/peer",), ())]
            pane._refresh_devices()
            pane._on_connect()
            self.assertTrue(pane._worker.wait(1.0))  # Leave the actual Qt completion queued.
            original_start = threading.Thread.start
            starts, errors = 0, []

            def fail_disposal_start(thread) -> None:
                nonlocal starts
                starts += 1
                if starts >= 2:
                    raise RuntimeError("forced queued disposal start failure")
                original_start(thread)

            try:
                with (
                    patch.object(threading.Thread, "start", fail_disposal_start),
                    patch.object(sys, "excepthook", lambda _type, value, _trace: errors.append(value)),
                ):
                    with self.assertRaisesRegex(RuntimeError, "forced queued disposal start failure"):
                        pane.shutdown()
                    self.app.processEvents()
                self.assertEqual(errors, [], "a queued callback cannot steal ownership during failed shutdown")
                self.assertIn(id(remote), pane._worker._pending_results)
                self.assertEqual(closer.count, 0)
                self.assertTrue(pane._worker.cancellation_event.is_set())
                self.assertIsNone(pane.remote)
                pane.shutdown()
                self.assertTrue(closer.entered.wait(1.0))
                self.assertFalse(pane._worker.wait(0))
                self.assertNotEqual(closer.thread_id, threading.get_ident())
            finally:
                closer.release.set()
                self.assertTrue(pane._worker.close(2.0))
            pane.shutdown()
            self.assertEqual(closer.count, 1)
            self.assertEqual(pane.service.stop_count, 1)
            self.assertFalse(closer.expired)

    def test_stale_queued_connection_remains_owned_when_retirement_start_fails(self) -> None:
        with self.consumer() as pane:
            closer = GatedCloser()
            remote = FakeRemote("retry-stale-queued")
            remote.close = closer
            pane.service.connect_call.result = remote
            pane.service.connect_call.release.set()
            pane.service.known = [DiscoveredDevice("retry-stale-queued", ("http://127.0.0.1/peer",), ())]
            pane._refresh_devices()
            pane._on_connect()
            self.assertTrue(pane._worker.wait(1.0))
            pane._on_disconnect()
            errors = []
            try:
                with (
                    patch.object(threading.Thread, "start", side_effect=RuntimeError("forced stale disposal start failure")),
                    patch.object(sys, "excepthook", lambda _type, value, _trace: errors.append(value)),
                ):
                    self.app.processEvents()
                self.assertEqual([str(error) for error in errors], ["forced stale disposal start failure"])
                self.assertIn(id(remote), pane._worker._pending_results, "failed handoff must leave worker ownership intact")
                self.assertIsNone(pane.remote)
                pane.shutdown()
                self.assertTrue(closer.entered.wait(1.0))
                self.assertNotEqual(closer.thread_id, threading.get_ident())
            finally:
                closer.release.set()
                self.assertTrue(pane._worker.close(2.0))
            self.assertEqual(closer.count, 1)
            self.assertFalse(closer.expired)


if __name__ == "__main__":
    unittest.main(verbosity=2)
