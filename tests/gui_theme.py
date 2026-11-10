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
    QApplication, QLineEdit, QStyleFactory, QTableWidget, QTableWidgetItem,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from sdctoolbox.gui.styling import apply_row_selection_style, muted_colour, set_dark_mode


class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

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
