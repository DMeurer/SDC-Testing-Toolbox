"""Theme switches update styled item views without rebuilding their data."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QLineEdit, QStyleFactory, QTableWidget, QTableWidgetItem,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from sdctoolbox.gui.styling import apply_row_selection_style, muted_colour, set_dark_mode, token
from sdctoolbox.gui.widgets.controls import _RangeSlider


class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_ticked_checkbox_is_filled_with_the_accent_in_both_modes(self) -> None:
        # The Windows 11 style fills a ticked box with the Accent role; unset, it came out black.
        if "windows11" not in QStyleFactory.keys():
            self.skipTest("the Windows 11 style exists only on Windows")
        self.app.setStyle("windows11")
        for dark in (False, True):
            with self.subTest(dark=dark):
                set_dark_mode(dark)
                box = QCheckBox("x")
                box.setChecked(True)
                box.resize(40, 24)
                self.assertEqual(self.app.palette().color(QPalette.Accent), token("accent", dark))
                self.assertEqual(box.grab().toImage().pixelColor(7, 12), token("accent", dark))

    def test_headers_use_the_header_token_before_and_after_a_switch(self) -> None:
        existing = QTableWidget(1, 1)
        for dark in (True, False):
            with self.subTest(dark=dark):
                set_dark_mode(dark)
                for table in (existing, QTableWidget(1, 1)):
                    self.assertEqual(table.horizontalHeader().palette().color(QPalette.Button), token("bg/header", dark))

    def test_blocked_slider_parts_cover_the_track_exactly(self) -> None:
        # They used to sit two pixels above the track and let the blue fill show through.
        for dark in (False, True):
            with self.subTest(dark=dark):
                set_dark_mode(dark)
                slider = _RangeSlider(Qt.Horizontal)
                slider.setRange(0, 100)
                slider.setValue(60)
                slider.blocked = [(0.0, 0.25)]
                slider.resize(300, 24)
                image = slider.grab().toImage()
                background = image.pixelColor(150, 0)

                def rows(x: int) -> list[int]:
                    return [y for y in range(image.height()) if image.pixelColor(x, y) != background]

                self.assertEqual(rows(30), rows(280), "blocked part and rail must cover the same rows")
                middle = rows(30)[len(rows(30)) // 2]
                self.assertNotEqual(image.pixelColor(30, middle), token("accent", dark))

    def test_existing_tables_headers_and_muted_cells_follow_both_themes(self) -> None:
        for style in QStyleFactory.keys():
            with self.subTest(style=style):
                self.app.setStyle(style)
                set_dark_mode(True)
                window = QWidget()
                layout = QVBoxLayout(window)
                editor = QLineEdit("unsaved draft")
                layout.addWidget(editor)
                table = QTableWidget(1, 2)
                item = QTableWidgetItem("unchanged value")
                item.setForeground(muted_colour(table))
                table.setItem(0, 0, item)
                table.setItem(0, 1, QTableWidgetItem("selected"))
                table.selectRow(0)
                tree = QTreeWidget()
                tree.setColumnCount(1)
                tree.addTopLevelItem(QTreeWidgetItem(["unchanged descriptor"]))
                hidden = QTableWidget(1, 1, window)
                for view in (table, tree, hidden):
                    apply_row_selection_style(view)
                layout.addWidget(table)
                layout.addWidget(tree)
                hidden.hide()
                window.resize(600, 500)
                window.show()
                changes = []
                table.itemChanged.connect(changes.append)
                try:
                    for dark in (True, False, True, False):
                        set_dark_mode(dark)
                        self.app.processEvents()
                        for view in (table, tree, hidden):
                            self.assertEqual(view.palette().color(QPalette.Base).lightness() < 128, dark)
                            self.assertEqual(view.viewport().palette().color(QPalette.Base).lightness() < 128, dark)
                            header = view.header() if view is tree else view.horizontalHeader()
                            self.assertEqual(header.palette().color(QPalette.Window).lightness() < 128, dark,
                                             f"{style}: header retained its old theme")
                        image = table.viewport().grab().toImage()
                        self.assertEqual(image.pixelColor(image.width() - 3, image.height() - 3),
                                         table.palette().color(QPalette.Base))
                        self.assertEqual(item.foreground().color(), muted_colour(table),
                                         f"{style}: existing muted cell retained its old colour")
                        self.assertIs(table.item(0, 0), item)
                        self.assertEqual(item.text(), "unchanged value")
                        self.assertEqual(table.currentRow(), 0)
                        self.assertEqual(editor.text(), "unsaved draft")
                        self.assertEqual(changes, [], "recolouring must not invoke data-edit handlers")
                        self.assertEqual(tree.topLevelItem(0).text(0), "unchanged descriptor")
                finally:
                    window.close()
                    window.deleteLater()
                    self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
