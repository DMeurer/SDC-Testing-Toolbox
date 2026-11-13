"""The redesigned interface: colour tokens, button kinds, sections and their defaults."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QPushButton, QStatusBar, QVBoxLayout, QWidget

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

            service.set_alert_presence(alarm, True)
            self.assertTrue(wait_for(fixture.app, lambda: pane.alert_table.item(0, ACOL_STATE).text() == "active"))
            self.assertEqual(pane.alarms_section.chip_texts(), ["1", "1 active"])
            state = pane.alert_table.item(0, ACOL_STATE)
            self.assertEqual(state.foreground().color().name(), red.name())
            self.assertTrue(state.font().bold())
            pump(fixture.app)
            cell = pane.alert_table.visualRect(pane.alert_table.model().index(0, ACOL_SIGNALS))
            self.assertTrue(has_colour(pane.alert_table.viewport().grab(cell).toImage(), red))


if __name__ == "__main__":
    unittest.main()
