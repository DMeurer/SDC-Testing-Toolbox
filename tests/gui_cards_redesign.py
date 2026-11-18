"""Cards in the redesign: the edit pen everywhere, card settings and the options of each type."""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from sdctoolbox.gui.styling import token
from sdctoolbox.gui.widgets import WidgetBoard
from sdctoolbox.gui.widgets.base import CardSettings, WidgetSpec
from sdctoolbox.gui.widgets.board import CardSettingsEditor, MetricCard
from sdctoolbox.gui.widgets.controls import (
    ReadoutWidget,
    SampleArrayWidget,
    SliderWidget,
    StepperWidget,
    TextWidget,
)
from sdctoolbox.gui.widgets.factory import build_widget, matching_controls, pick_control
from sdctoolbox.model import MetricKind, RemoteRange


def number(**extra) -> WidgetSpec:
    values = {"handle": "m.n", "label": "N", "kind": MetricKind.NUMBER, "minimum": Decimal(0),
              "maximum": Decimal(100), "resolution": Decimal(1), "editable": True}
    values.update(extra)
    return WidgetSpec(**values)


def ranged() -> WidgetSpec:
    return number(minimum=None, maximum=None,
                  allowed_ranges=(RemoteRange(lower=Decimal(100), upper=Decimal(900)),),
                  technical_ranges=(RemoteRange(lower=Decimal(0), upper=Decimal(1000)),))


class Collect:
    def __init__(self, widget) -> None:
        self.values = []
        widget.value_requested.connect(lambda _h, value: self.values.append(value))


class ShownWindows(QObject):
    """Records every window that is shown while it listens."""

    def __init__(self, app: QApplication) -> None:
        super().__init__()
        self.app, self.shown = app, []
        app.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: ANN001, N802 - Qt naming
        if event.type() == QEvent.Show and isinstance(watched, QWidget) and watched.isWindow():
            self.shown.append(type(watched).__name__)
        return False

    def stop(self) -> list[str]:
        self.app.removeEventFilter(self)
        return self.shown


class CardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_all_matching_types_are_listed_and_the_first_is_the_default(self) -> None:
        spec = number()
        self.assertEqual(matching_controls(spec), [SliderWidget, StepperWidget, ReadoutWidget])
        self.assertIs(pick_control(spec), SliderWidget)
        self.assertIsInstance(build_widget(spec, settings=CardSettings(control="StepperWidget")), StepperWidget)
        # A type that cannot show the metric falls back to the default.
        self.assertIsInstance(build_widget(spec, settings=CardSettings(control="TextWidget")), SliderWidget)

    def test_every_card_has_a_larger_pen_and_only_own_cards_a_bin(self) -> None:
        remote = MetricCard(number())
        own = MetricCard(number(), deletable=True)
        self.assertIsNotNone(remote.edit_button)
        self.assertIsNone(remote.delete_button)
        self.assertIsNotNone(own.delete_button)
        self.assertEqual(remote.edit_button.font().pixelSize(), 17)

    def test_network_pen_opens_card_settings_and_switching_type_rebuilds_the_card(self) -> None:
        board = WidgetBoard()
        board.set_metrics([number()])
        board.show()
        card = board.card("m.n")
        QTest.mouseClick(card.edit_button, Qt.LeftButton)
        popup = board.settings_popup
        self.assertTrue(popup.isVisible())
        editor = popup.editor
        self.assertEqual([editor.type_box.itemText(i) for i in range(editor.type_box.count())],
                         ["Slider", "Number input", "Value only"])
        editor.type_box.setCurrentIndex(1)
        self.assertIsInstance(board.card("m.n").control, StepperWidget)
        self.assertEqual(board.card_settings("m.n").control, "StepperWidget")
        popup.close()
        board.close()

    def test_own_pen_asks_for_the_edit_dialog(self) -> None:
        board = WidgetBoard(deletable=True)
        asked = []
        board.edit_requested.connect(asked.append)
        board.set_metrics([number()])
        board.card("m.n").edit_button.click()
        self.assertEqual(asked, ["m.n"])
        self.assertIsNone(board.settings_popup)

    def test_stepper_buttons_can_be_hidden_in_pairs(self) -> None:
        stepper = StepperWidget(number(), settings=CardSettings(small_steps=False))
        self.assertEqual([b.text() for b in stepper.buttons if not b.isHidden()], ["-10", "+10"])
        stepper = StepperWidget(number(), settings=CardSettings(large_steps=False))
        self.assertEqual([b.text() for b in stepper.buttons if not b.isHidden()], ["-1", "+1"])
        self.assertEqual({b.property("kind") for b in stepper.buttons}, {"design-outline"})

    def test_slider_sends_on_release_or_while_dragging(self) -> None:
        for update_on, expected in (("release", []), ("change", [Decimal(5)])):
            slider = SliderWidget(number(), settings=CardSettings(update_on=update_on))
            sent = Collect(slider)
            slider.slider.setSliderDown(True)
            slider.slider.setValue(5)
            self.assertEqual(sent.values, expected, update_on)

    def test_slider_spans_the_technical_range_and_blocks_outside_the_allowed_one(self) -> None:
        slider = SliderWidget(ranged())
        sent = Collect(slider)
        self.assertEqual((slider.low_label.text(), slider.high_label.text()), ("0", "1000"))
        self.assertEqual(slider.allowed_label.text(), "allowed 100 – 900")
        self.assertFalse(slider.allowed_label.isHidden())
        slider.slider.setValue(0)
        slider._on_released()
        self.assertEqual(sent.values[-1], Decimal(100))
        self.assertEqual(slider.slider.value(), 100)
        plain = SliderWidget(ranged(), settings=CardSettings(show_ranges=False))
        self.assertEqual((plain.low_label.text(), plain.high_label.text()), ("100", "900"))
        self.assertTrue(plain.allowed_label.isHidden())
        self.assertIn("technical 0 – 1000", MetricCard(ranged()).footer.text())

    def test_waveform_legend_and_speed(self) -> None:
        spec = WidgetSpec(handle="w", label="W", kind=MetricKind.WAVEFORM, sample_period=Decimal("0.01"),
                          minimum=Decimal(-1), maximum=Decimal(1))
        plot = SampleArrayWidget(spec).plot
        self.assertTrue(plot.show_legend)
        self.assertEqual(plot.window_samples, 1000)
        slow = SampleArrayWidget(spec, settings=CardSettings(seconds_per_width=Decimal(5), legend=False)).plot
        self.assertEqual(slow.window_samples, 500)
        self.assertFalse(slow.show_legend)
        self.assertEqual(plot.legend_texts(), ["1", "0", "-1"])
        self.assertIn("10 s per width", MetricCard(spec).footer.text())

    def test_text_shows_unsent_changes(self) -> None:
        spec = WidgetSpec(handle="t", label="T", kind=MetricKind.TEXT, editable=True)
        text = TextWidget(spec)
        text.show_value("abc")
        self.assertTrue(text.pending_label.isHidden())
        text.edit.setText("abcd")
        self.assertFalse(text.pending_label.isHidden())
        self.assertEqual(text.pending_label.text(), "● Not sent")
        self.assertEqual(text.edit.palette().color(text.edit.foregroundRole()).name(), token("state/pending").name())
        text.apply.click()
        self.assertTrue(text.pending_label.isHidden())
        self.assertEqual(text.apply.property("kind"), "design")

    def test_text_multiline_sends_on_ctrl_enter_and_split_shows_the_device_value(self) -> None:
        spec = WidgetSpec(handle="t", label="T", kind=MetricKind.TEXT, editable=True)
        text = TextWidget(spec, settings=CardSettings(multiline=True, split=True))
        sent = Collect(text)
        text.show_value("old")
        self.assertEqual(text.current_label.text(), "old")
        editor = text.multi_edit
        editor.setPlainText("")
        QTest.keyClicks(editor, "a")
        QTest.keyClick(editor, Qt.Key_Return)
        QTest.keyClicks(editor, "b")
        self.assertEqual(sent.values, [])
        QTest.keyClick(editor, Qt.Key_Return, Qt.ControlModifier)
        self.assertEqual(sent.values, ["a\nb"])

    def test_text_field_of_either_mode_is_visible(self) -> None:
        spec = WidgetSpec(handle="t", label="T", kind=MetricKind.TEXT, editable=True)
        for multiline in (False, True):
            # Showing a field before it has a parent flashes it up as a window of its own.
            windows = ShownWindows(self.app)
            text = TextWidget(spec, settings=CardSettings(multiline=multiline))
            self.assertEqual(windows.stop(), [], multiline)
            text.show()
            self.app.processEvents()
            shown, unused = (text.multi_edit, text.edit) if multiline else (text.edit, text.multi_edit)
            self.assertTrue(shown.isVisible(), multiline)
            self.assertFalse(unused.isVisible(), multiline)
            text.close()

    def test_no_card_flashes_a_stray_window_while_it_is_built(self) -> None:
        wave = WidgetSpec(handle="w", label="W", kind=MetricKind.WAVEFORM, sample_period=Decimal("0.01"))
        text = WidgetSpec(handle="t", label="T", kind=MetricKind.TEXT, editable=True)
        for spec in (ranged(), number(), wave, text):
            for settings in (CardSettings(), CardSettings(multiline=True, split=True, legend=False)):
                windows = ShownWindows(self.app)
                card = MetricCard(spec, settings=settings)
                editor = CardSettingsEditor(spec, settings)
                self.assertEqual(windows.stop(), [], spec.handle)
                card.deleteLater()
                editor.deleteLater()

    def test_editor_offers_split_only_for_remote_controllable_text(self) -> None:
        writable = WidgetSpec(handle="t", label="T", kind=MetricKind.TEXT, editable=True)
        fixed = WidgetSpec(handle="t", label="T", kind=MetricKind.TEXT, editable=False)
        offered, hidden = CardSettingsEditor(writable, CardSettings()), CardSettingsEditor(fixed, CardSettings())
        self.assertFalse(offered.split_box.isHidden())
        self.assertTrue(hidden.split_box.isHidden())


if __name__ == "__main__":
    unittest.main()
