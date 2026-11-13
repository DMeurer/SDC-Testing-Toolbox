"""The redesigned interface: colour tokens, button kinds, sections and their defaults."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QPushButton, QStatusBar, QVBoxLayout, QWidget

from sdctoolbox.gui.icons import app_icon
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


if __name__ == "__main__":
    unittest.main()
