"""The redesigned interface: colour tokens, button kinds, sections and their defaults."""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QStatusBar, QVBoxLayout, QWidget

from gui_test_support import WindowFixture, pump, wait_for
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from sdctoolbox.gui.collapsible import CollapsibleSection
from sdctoolbox.gui.icons import app_icon
from sdctoolbox.gui.provider_pane import ACOL_SIGNALS, ACOL_STATE
from sdctoolbox.model import AlertSpec, MetricKind, MetricSpec
from sdctoolbox.gui.styling import apply_theme_style, set_button_kind, set_dark_mode, token


def grab_pixel(widget: QWidget, x: int, y: int) -> QColor:
    return widget.grab().toImage().pixelColor(x, y)


class TokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        set_dark_mode(False)

    def test_tokens_follow_the_mode(self) -> None:
        set_dark_mode(False)
        self.assertEqual(token("accent").name(), "#0078d4")
        self.assertEqual(token("highlight").name(), "#7b3fe4")
        self.assertEqual(token("kind/mdib").name(), "#2e7d32")
        set_dark_mode(True)
        self.assertEqual(token("accent").name(), "#4cc2ff")
        self.assertEqual(token("highlight").name(), "#b99cff")
        self.assertEqual(token("kind/toolbox").name(), "#f5c842")

    def test_dark_palette_uses_native_values_and_nothing_is_black(self) -> None:
        set_dark_mode(True)
        palette = self.app.palette()
        self.assertEqual(palette.color(QPalette.Window).name(), "#1e1e1e")
        self.assertEqual(palette.color(QPalette.Base).name(), "#2d2d2d")
        self.assertEqual(palette.color(QPalette.Highlight).name(), "#4cc2ff")
        for role in (QPalette.Window, QPalette.Base, QPalette.AlternateBase, QPalette.Button):
            self.assertNotEqual(palette.color(role).name(), "#000000", role)

    def test_button_kinds_paint_in_their_colour(self) -> None:
        for dark in (False, True):
            set_dark_mode(dark)
            host = QWidget()
            layout = QVBoxLayout(host)
            filled, outlined = QPushButton("New"), QPushButton("Edit")
            set_button_kind(filled, "highlight")
            set_button_kind(outlined, "design-outline")
            layout.addWidget(filled)
            layout.addWidget(outlined)
            host.show()
            self.app.processEvents()
            middle = filled.height() // 2
            self.assertEqual(grab_pixel(filled, 6, middle).name(), token("highlight").name())
            self.assertEqual(grab_pixel(outlined, 0, middle).name(), token("accent").name())
            outlined.setEnabled(False)
            self.app.processEvents()
            self.assertEqual(grab_pixel(outlined, 0, middle).name(), token("border/default").name())
            host.close()

    def test_selected_rows_are_tinted_in_both_modes(self) -> None:
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem

        from sdctoolbox.gui.styling import apply_row_selection_style
        table = QTableWidget(2, 2)
        table.setItem(0, 0, QTableWidgetItem("row"))
        apply_row_selection_style(table)
        table.resize(300, 200)
        table.show()
        for dark in (False, True):
            set_dark_mode(dark)
            table.selectRow(0)
            self.app.processEvents()
            rect = table.visualRect(table.model().index(0, 1))
            self.assertEqual(grab_pixel(table.viewport(), rect.center().x(), rect.center().y()).name(),
                             token("accent/tint").name())
        table.close()

    def test_checked_outlined_button_fills_like_a_selected_segment(self) -> None:
        apply_theme_style()
        button = QPushButton("Release")
        set_button_kind(button, "design-outline")
        button.setCheckable(True)
        button.setChecked(True)
        button.resize(100, 30)
        button.show()
        self.app.processEvents()
        self.assertEqual(grab_pixel(button, 6, 15).name(), token("accent").name())
        button.close()

    def test_status_bar_is_design_coloured(self) -> None:
        apply_theme_style()
        bar = QStatusBar()
        bar.resize(200, 24)
        bar.show()
        self.app.processEvents()
        self.assertEqual(grab_pixel(bar, 100, 12).name(), token("accent").name())
        bar.close()


class AppIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_icon_ships_in_every_size(self) -> None:
        icon = app_icon()
        sizes = {size.width() for size in icon.availableSizes()}
        self.assertTrue({16, 32, 48, 256} <= sizes, sizes)
        image = icon.pixmap(256, 256).toImage()
        top, wanted = image.pixelColor(128, 2), QColor("#0a5fc2")
        self.assertLessEqual(max(abs(top.red() - wanted.red()), abs(top.green() - wanted.green()),
                                 abs(top.blue() - wanted.blue())), 3, top.name())

    def test_windows_icon_file_exists(self) -> None:
        ico = Path(__file__).resolve().parent.parent / "sdctoolbox" / "gui" / "icons" / "app_icon.ico"
        self.assertEqual(ico.read_bytes()[:4], bytes([0, 0, 1, 0]))


def has_colour(image, colour: QColor) -> bool:
    return any(image.pixelColor(x, y).name() == colour.name()
               for x in range(image.width()) for y in range(image.height()))


class CollapsibleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_header_click_toggles_content_and_keeps_header(self) -> None:
        content = QWidget()
        section = CollapsibleSection("Alarms", content, expanded=False)
        section.set_chips(["4", "none active"])
        section.show()
        self.app.processEvents()
        self.assertFalse(content.isVisible())
        self.assertEqual(section.chip_texts(), ["4", "none active"])
        QTest.mouseClick(section.header, Qt.LeftButton)
        self.assertTrue(section.expanded and content.isVisible())
        QTest.keyClick(section.header, Qt.Key_Space)
        self.assertFalse(section.expanded or content.isVisible())
        self.assertTrue(section.header.isVisible())
        section.close()

    def test_header_band_is_tinted(self) -> None:
        apply_theme_style()
        section = CollapsibleSection("Metrics", QWidget())
        section.resize(300, 120)
        section.show()
        self.app.processEvents()
        self.assertEqual(grab_pixel(section.header, section.header.width() - 6, 6).name(),
                         token("accent/tint").name())
        section.close()


class MyDeviceTests(unittest.TestCase):
    def test_sections_buttons_and_alarm_toolbar(self) -> None:
        with WindowFixture("redesign-my-device") as fixture:
            pane = fixture.window.provider_pane
            service = fixture.service
            self.assertTrue(pane.metrics_section.expanded)
            self.assertFalse(pane.alarms_section.expanded)
            self.assertEqual(pane.new_button.property("kind"), "highlight")
            self.assertEqual(pane.context_button.property("kind"), "highlight-outline")
            order = [pane.new_alert_button, pane.edit_alert_button, pane.remove_alert_button,
                     pane.toggle_alert_button, pane.acknowledge_button, pane.stop_latched_button,
                     pane.delegate_button]
            layout = pane.alert_toolbar
            found = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget()]
            self.assertEqual(found, order)
            self.assertEqual([b.property("kind") for b in order],
                             ["highlight", "highlight-outline", "highlight-outline"] + ["design-outline"] * 4)

            from sdctoolbox import constants
            from sdctoolbox.model import ActionSpec
            action = service.add_action(ActionSpec(label="Home", target_handle=constants.MDS_HANDLE))
            pane.refresh_actions()
            self.assertEqual(pane.action_buttons[action].property("kind"), "design-outline")

            metric = service.add_metric(MetricSpec(label="Rate", kind=MetricKind.NUMBER))
            alarm = service.add_alert(AlertSpec(label="Manual", source_handle=metric))
            self.assertTrue(wait_for(fixture.app, lambda: pane.alert_table.rowCount() == 1))
            self.assertEqual(pane.metrics_section.chip_texts(), ["1"])
            self.assertEqual(pane.alarms_section.chip_texts(), ["1", "none active"])
            pane.alarms_section.set_expanded(True)
            fixture.window.resize(1200, 700)
            pump(fixture.app)
            red = token("text/error")
            cell = pane.alert_table.visualRect(pane.alert_table.model().index(0, ACOL_SIGNALS))
            self.assertFalse(has_colour(pane.alert_table.viewport().grab(cell).toImage(), red))
            self.assertEqual(pane.alert_table.item(0, ACOL_SIGNALS).text(), "")
            self.assertIn("Visible: Off", pane.alert_table.item(0, ACOL_SIGNALS).toolTip())

            from unittest.mock import patch

            from sdctoolbox.gui.edit_dialogs import EditSourceDialog

            def choose_value_only(dialog) -> int:
                index = dialog.card_editor.type_box.findText("Value only")
                dialog.card_editor.type_box.setCurrentIndex(index)
                return 1
            with patch.object(EditSourceDialog, "exec", choose_value_only):
                pane.board.card(metric).edit_button.click()
            self.assertEqual(type(pane.board.card(metric).control).__name__, "ReadoutWidget")

            service.set_alert_presence(alarm, True)
            self.assertTrue(wait_for(fixture.app, lambda: pane.alert_table.item(0, ACOL_STATE).text() == "active"))
            self.assertEqual(pane.alarms_section.chip_texts(), ["1", "1 active"])
            state = pane.alert_table.item(0, ACOL_STATE)
            self.assertEqual(state.foreground().color().name(), red.name())
            self.assertTrue(state.font().bold())
            pump(fixture.app)
            cell = pane.alert_table.visualRect(pane.alert_table.model().index(0, ACOL_SIGNALS))
            self.assertTrue(has_colour(pane.alert_table.viewport().grab(cell).toImage(), red))


class NetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from sdctoolbox.gui.consumer_tabs import ConsumerTabs
        from sdctoolbox.gui.main_window import network_view
        self.tabs = ConsumerTabs("127.0.0.1")
        self.view = network_view(self.tabs)
        self.view.resize(900, 600)
        self.view.show()
        pump(self.app)

    def tearDown(self) -> None:
        self.tabs.shutdown()
        self.view.close()
        self.view.deleteLater()
        pump(self.app, 0.05)

    def test_nothing_connected_shows_discovery_and_hint_only(self) -> None:
        pane = self.tabs.current_pane
        self.assertIsNone(self.tabs.cornerWidget())
        self.assertFalse(self.tabs.tabBar().isVisible())
        self.assertTrue(pane.discovery_section.isVisible() and pane.discovery_section.expanded)
        self.assertTrue(pane.empty_hint.isVisible())
        for section in (pane.structure_section, pane.metrics_section, pane.alarms_section):
            self.assertFalse(section.isVisible())
        self.assertTrue(pane.metrics_section.expanded)
        self.assertFalse(pane.alarms_section.expanded or pane.structure_section.expanded)
        self.assertEqual(pane.structure_section.hint.text(), "Descriptor tree with handles")

    def test_discovery_row_order_and_kinds(self) -> None:
        pane = self.tabs.current_pane
        row = pane.discovery_row
        found = [row.itemAt(i).widget() for i in range(row.count()) if row.itemAt(i).widget()]
        self.assertEqual(found, [pane.connect_button, pane.disconnect_button, pane.scan_button,
                                 pane.connect_next_box])
        self.assertEqual([b.property("kind") for b in found[:3]],
                         ["highlight", "design-outline", "design-outline"])

    def test_device_rows_carry_transport_and_tab_chips(self) -> None:
        from sdctoolbox.consumer_service import DiscoveredDevice
        from sdctoolbox.gui.consumer_pane import DEVICE_TAB_ROLE, DEVICE_TRANSPORT_ROLE
        pane = self.tabs.current_pane
        devices = [DiscoveredDevice("urn:uuid:a", ("http://10.0.0.1/a",), ()),
                   DiscoveredDevice("urn:uuid:b", ("https://10.0.0.2/b",), ())]
        pane.service.known_devices = lambda: devices
        pane._refresh_devices()
        self.assertEqual(pane.device_list.item(0).text(), "urn:uuid:a")
        self.assertEqual(pane.device_list.item(1).data(DEVICE_TRANSPORT_ROLE), "HTTPS")
        pane.set_device_tabs({"urn:uuid:b": 2})
        self.assertIsNone(pane.device_list.item(0).data(DEVICE_TAB_ROLE))
        self.assertEqual(pane.device_list.item(1).data(DEVICE_TAB_ROLE), 2)

    def test_connect_from_a_connected_tab_opens_a_new_tab(self) -> None:
        from unittest.mock import patch

        from sdctoolbox.consumer_service import DiscoveredDevice
        from sdctoolbox.gui.consumer_pane import ConsumerPane
        first = self.tabs.current_pane
        device = DiscoveredDevice("urn:uuid:c", ("http://10.0.0.3/c",), ())
        first.devices = [device]
        first.device_list.addItem("urn:uuid:c")
        first.device_list.setCurrentRow(0)
        first.remote = object()
        started = []
        with patch.object(ConsumerPane, "connect_to", lambda pane, dev: started.append((pane, dev))):
            first._on_connect()
        self.assertEqual(self.tabs.count(), 2)
        self.assertIs(self.tabs.current_pane, started[0][0])
        self.assertIsNot(started[0][0], first)
        self.assertEqual(started[0][1].epr, "urn:uuid:c")
        self.assertTrue(self.tabs.tabBar().isVisible())
        first.remote = None


def owner(widget: QWidget) -> str | None:
    """Which kind of section (mdib or toolbox) a dialog field sits in, if any."""
    parent = widget.parentWidget()
    while parent is not None:
        role = parent.property("role")
        if isinstance(role, str) and role.startswith("sect-"):
            return role.removeprefix("sect-")
        parent = parent.parentWidget()
    return None


def ok_cancel_kinds(buttons) -> tuple[str, str]:
    from PySide6.QtWidgets import QDialogButtonBox
    return (buttons.button(QDialogButtonBox.Ok).property("kind"),
            buttons.button(QDialogButtonBox.Cancel).property("kind"))


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_start_dialog_browse_waits_for_tls(self) -> None:
        from sdctoolbox.gui.startup_dialog import StartupDialog
        dialog = StartupDialog(name="redesign")
        self.assertEqual(len(dialog.tls_browse_buttons), 4)
        self.assertFalse(any(b.isEnabled() for b in dialog.tls_browse_buttons))
        self.assertTrue(dialog.config_browse_button.isEnabled())
        dialog.tls_box.setChecked(True)
        self.assertTrue(all(b.isEnabled() for b in dialog.tls_browse_buttons))
        self.assertEqual({b.property("kind") for b in [*dialog.tls_browse_buttons, dialog.config_browse_button]},
                         {"design-outline"})
        self.assertEqual(ok_cancel_kinds(dialog.buttons), ("highlight", "highlight-outline"))
        dialog.deleteLater()

    def test_new_data_source_sections(self) -> None:
        from sdctoolbox.gui.new_metric_dialog import NewMetricDialog
        dialog = NewMetricDialog()
        for widget in (dialog.kind_box, dialog.label_edit, dialog.unit_edit, dialog.resolution_edit,
                       dialog.limits_widget, dialog.controllable_box, dialog.handle_preview,
                       dialog.sample_period_edit, dialog.domain_widget):
            self.assertEqual(owner(widget), "mdib", widget)
        for widget in (dialog.shape_box, dialog.cycle_widget):
            self.assertEqual(owner(widget), "toolbox", widget)
        self.assertEqual(ok_cancel_kinds(dialog.buttons), ("highlight", "highlight-outline"))
        dialog.deleteLater()

    def test_new_alarm_is_one_mdib_section_with_the_hint_outside(self) -> None:
        from sdctoolbox.gui.new_alert_dialog import NewAlertDialog
        dialog = NewAlertDialog({"m.rate": MetricSpec(label="Rate", kind=MetricKind.NUMBER)})
        for widget in (dialog.source_box, dialog.label_edit, dialog.priority_box, dialog.handle_preview):
            self.assertEqual(owner(widget), "mdib")
        self.assertIsNone(owner(dialog.hint))
        self.assertEqual(ok_cancel_kinds(dialog.buttons), ("highlight", "highlight-outline"))
        dialog.deleteLater()

    def test_edit_dialogs_split_description_state_and_simulation(self) -> None:
        from types import SimpleNamespace

        from sdctoolbox.gui.edit_dialogs import EditAlertDialog, EditSourceDialog
        metric = MetricSpec(label="Rate", kind=MetricKind.NUMBER)
        alert = AlertSpec(label="High", source_handle="m.rate", upper_limit=Decimal(9))
        service = SimpleNamespace(list_metrics=lambda: {"m.rate": metric}, list_alerts=lambda: {"al.high": alert},
                                  operation_handle_for=lambda _h: "op", _max_limits=lambda _s: (None, None))
        source = EditSourceDialog(service, "m.rate")
        self.assertEqual(owner(source.technical_label), "mdib")
        self.assertEqual(owner(source.allowed_minimum_edit), "mdib")
        self.assertEqual(owner(source.active_box), "toolbox")
        self.assertEqual(owner(source.shape_box), "toolbox")
        self.assertGreaterEqual(source.minimumWidth(), 340)
        self.assertEqual(ok_cancel_kinds(source.buttons), ("highlight", "highlight-outline"))
        alarm = EditAlertDialog(service, "al.high")
        self.assertEqual(owner(alarm.lower_edit), "mdib")
        self.assertEqual(owner(alarm.priority_box), "mdib")
        self.assertIsNot(alarm.lower_edit.parentWidget(), None)
        notes = [label.text() for label in alarm.findChildren(QLabel)]
        self.assertIn("read only", notes)
        self.assertIn("sent to consumers", notes)
        for dialog in (source, alarm):
            dialog.deleteLater()

    def test_card_sections_in_data_source_dialogs(self) -> None:
        from types import SimpleNamespace

        from sdctoolbox.gui.edit_dialogs import EditSourceDialog
        from sdctoolbox.gui.new_metric_dialog import NewMetricDialog
        from sdctoolbox.gui.widgets.base import CardSettings, from_spec
        metric = MetricSpec(label="Rate", kind=MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(100))
        service = SimpleNamespace(list_metrics=lambda: {"m.rate": metric}, operation_handle_for=lambda _h: None)
        dialog = EditSourceDialog(service, "m.rate",
                                  card=(from_spec("m.rate", metric, editable=True), CardSettings()))
        self.assertEqual(owner(dialog.card_editor.type_box), "toolbox")
        dialog.card_editor.type_box.setCurrentIndex(1)
        self.assertEqual(dialog.card_settings().control, "StepperWidget")

        new = NewMetricDialog()
        self.assertEqual(owner(new.card_type_box), "toolbox")
        new.minimum_edit.setText("0")
        new.maximum_edit.setText("100")
        texts = [new.card_type_box.itemText(i) for i in range(new.card_type_box.count())]
        self.assertEqual(texts, ["Slider", "Number input", "Value only"])
        new.card_type_box.setCurrentIndex(1)
        self.assertEqual(new.card_control(), "StepperWidget")
        for dialog_ in (dialog, new):
            dialog_.deleteLater()

    def test_patient_and_location_buttons(self) -> None:
        from sdctoolbox.gui.context_dialog import ContextDialog
        from sdctoolbox.model import LocationInfo, PatientInfo
        dialog = ContextDialog(LocationInfo(), PatientInfo())
        self.assertEqual(ok_cancel_kinds(dialog.buttons), ("highlight", "highlight-outline"))
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
