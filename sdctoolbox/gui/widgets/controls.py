"""The concrete metric controls.

Each one declares which metrics it can represent. The factory asks them in order, so a more
specific control gets the chance to claim a metric before a more general one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ...model import SAMPLE_ARRAY_KINDS, MetricKind
from ..decimal_input import DecimalInputError, parse_decimal_input
from ..no_wheel import NoWheelComboBox, NoWheelSlider
from ..styling import mark_as_error, mute
from .base import MetricWidget, WidgetSpec
from .plot import SamplePlot

# A Qt slider works in ints. Above this many steps it stops being a useful control and the
# stepper is a better fit, so the slider declines the metric.
MAX_SLIDER_STEPS = 100_000

# How much the stepper's buttons move the value.
STEP_SMALL = Decimal("1")
STEP_LARGE = Decimal("10")

NO_VALUE = "\u2014"


def _trim(value: Decimal) -> str:
    """Render a Decimal without a trailing '.0' but keep genuine decimals."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class SampleArrayWidget(MetricWidget):
    """A plot, for the two kinds whose state carries many values instead of one.

    Both sample-array kinds land here because both are a list of numbers against an axis,
    but they are drawn differently and for a reason:

    * a **waveform** is samples against *time*, arriving in blocks. Older samples scroll off
      the left, so what you see is a window onto something continuous.
    * a **distribution** is samples against a *domain* - the DomainUnit, e.g. Hz. It is one
      complete picture that gets replaced, not a moving window, so it is drawn as bars
      across its DistributionRange.

    Painted by hand rather than with QtCharts: it is a polyline, and the alternative is a
    dependency this project has deliberately kept out.
    """

    priority = 15

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        return spec.kind in SAMPLE_ARRAY_KINDS

    def build(self) -> None:
        self.plot = SamplePlot(
            scrolling=self.spec.kind is MetricKind.WAVEFORM,
            minimum=self.spec.minimum,
            maximum=self.spec.maximum,
            # What lets the trace move at the rate the samples were taken at rather than
            # lurching once per report. It is on the descriptor for exactly this.
            sample_period=self.spec.sample_period,
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.plot)

    def show_value(self, value: Any) -> None:
        """Show the metric's *current* content. Must be safe to call repeatedly.

        This is the refresh path, and it fires for reasons that have nothing to do with new
        data: a descriptor being added, an operation changing, the board being rebuilt. A
        scrolling plot therefore must not touch its trace here, or every refresh would
        splice the latest block into it a second time.

        It used to seed an empty plot, so a rebuilt card was not blank until the next block.
        That was a race: the block it seeded with could already be in flight, and the queued
        waveform event then appended it a second time. On a sawtooth the duplicate shows as
        a step *backwards*, which is how it was caught. A waveform card is blank for at most
        one block after a rebuild instead, which nobody will notice at four blocks a second.

        New data arrives through append_samples.
        """
        if not isinstance(value, (list, tuple)):
            return
        if not self.plot.scrolling:
            # A distribution is one whole picture of its domain, so replacing is both
            # correct and idempotent - and it has no stream to collide with.
            self.plot.add_samples(value)

    def append_samples(self, samples: Any) -> None:
        """Take a genuinely new block. Only the waveform stream calls this."""
        if isinstance(samples, (list, tuple)) and samples:
            self.plot.add_samples(samples)

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001, ARG002
        # Nothing to enable: BICEPS defines no operation that writes a sample array, so
        # these are read-only for everyone, not just for us.
        return


class ChoiceWidget(MetricWidget):
    """A dropdown, for a metric with a fixed list of allowed values."""

    priority = 10

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        return spec.kind is MetricKind.CHOICE and bool(spec.allowed_values)

    def build(self) -> None:
        self.box = NoWheelComboBox()
        self.box.addItems(list(self.spec.allowed_values))
        self.box.activated.connect(self._on_activated)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.box)

    def _on_activated(self, index: int) -> None:
        # activated rather than currentIndexChanged: only fire on a real click, not when
        # show_value moves the selection.
        self.request(self.box.itemText(index))

    def show_value(self, value: Any) -> None:
        if value is None or self.busy_editing():
            return
        index = self.box.findText(str(value))
        if index >= 0:
            self.box.blockSignals(True)
            self.box.setCurrentIndex(index)
            self.box.blockSignals(False)

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        self.box.setEnabled(editable)

    def busy_editing(self) -> bool:
        return self.box.view().isVisible()


class SliderWidget(MetricWidget):
    """A slider, for a number with both a minimum and a maximum."""

    priority = 20

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        if spec.kind is not MetricKind.NUMBER or not spec.bounded:
            return False
        return cls._steps(spec) is not None

    @staticmethod
    def _steps(spec: WidgetSpec) -> int | None:
        """How many slider positions the range needs, or None if that is unreasonable."""
        span = spec.maximum - spec.minimum
        if span <= 0:
            return None
        resolution = spec.resolution or Decimal("1")
        if not resolution.is_finite() or resolution <= 0:
            resolution = Decimal("1")
        span_numerator, span_denominator = span.as_integer_ratio()
        resolution_numerator, resolution_denominator = resolution.as_integer_ratio()
        numerator = span_numerator * resolution_denominator
        denominator = span_denominator * resolution_numerator
        steps = (numerator + denominator - 1) // denominator
        if steps < 1 or steps > MAX_SLIDER_STEPS:
            return None
        return steps

    def build(self) -> None:
        self._resolution = self.spec.resolution or Decimal("1")
        if not self._resolution.is_finite() or self._resolution <= 0:
            self._resolution = Decimal("1")
        self._steps = self._steps(self.spec) or 1

        self.slider = NoWheelSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(self._steps)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(1, self._steps // 10))
        self.slider.valueChanged.connect(self._on_moved)
        self.slider.sliderReleased.connect(self._on_released)

        self.readout = QLabel(NO_VALUE)
        self.readout.setAlignment(Qt.AlignCenter)
        self.readout.setMinimumWidth(70)

        low = QLabel(_trim(self.spec.minimum))
        high = QLabel(_trim(self.spec.maximum))
        for end in (low, high):
            mute(end)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(low)
        row.addWidget(self.slider, 1)
        row.addWidget(high)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.readout)
        layout.addLayout(row)

    def _position_to_value(self, position: int) -> Decimal:
        if position >= self._steps:
            return self.spec.maximum
        value = self.spec.minimum + Decimal(position) * self._resolution
        return min(value, self.spec.maximum)

    def _value_to_position(self, value: Decimal) -> int:
        if value <= self.spec.minimum:
            return 0
        if value >= self.spec.maximum:
            return self._steps
        offset = (value - self.spec.minimum) / self._resolution
        return max(0, min(self._steps, int(offset)))

    def _on_moved(self, position: int) -> None:
        self.readout.setText(_trim(self._position_to_value(position)))
        # While dragging, only the readout moves. The value is sent on release, so a drag
        # across the range does not fire a write per pixel.
        if not self.slider.isSliderDown() and self.slider.hasFocus():
            self.request(self._position_to_value(position))

    def _on_released(self) -> None:
        self.request(self._position_to_value(self.slider.value()))

    def show_value(self, value: Any) -> None:
        if not isinstance(value, Decimal) or self.busy_editing():
            return
        self.slider.blockSignals(True)
        self.slider.setValue(self._value_to_position(value))
        self.slider.blockSignals(False)
        self.readout.setText(_trim(value))

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        self.slider.setEnabled(editable)

    def busy_editing(self) -> bool:
        return self.slider.isSliderDown()


class StepperWidget(MetricWidget):
    """A value with buttons either side, for a number that has no useful range.

    The layout is  -10  -1  [ value ]  +1  +10 , and the value can also be typed.
    """

    priority = 30

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        return spec.kind is MetricKind.NUMBER

    def build(self) -> None:
        self.edit = QLineEdit()
        self.edit.setAlignment(Qt.AlignCenter)
        self.edit.setPlaceholderText(NO_VALUE)
        self.edit.returnPressed.connect(self._on_typed)

        self.buttons: list[QPushButton] = []
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        for delta in (-STEP_LARGE, -STEP_SMALL):
            row.addWidget(self._make_button(delta))
        row.addWidget(self.edit, 1)
        for delta in (STEP_SMALL, STEP_LARGE):
            row.addWidget(self._make_button(delta))

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        mark_as_error(self.error_label)
        self.error_label.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)
        layout.addWidget(self.error_label)

    def _make_button(self, delta: Decimal) -> QPushButton:
        caption = f"+{_trim(delta)}" if delta > 0 else _trim(delta)
        button = QPushButton(caption)
        button.setFixedWidth(46)
        button.clicked.connect(lambda _=False, d=delta: self._step(d))
        self.buttons.append(button)
        return button

    def _current(self, *, bounded: bool = False) -> Decimal:
        minimum = self.spec.minimum if bounded else None
        maximum = self.spec.maximum if bounded else None
        return parse_decimal_input(
            self.edit.text(),
            "value",
            minimum=minimum,
            maximum=maximum,
        )

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _clear_error(self) -> None:
        self.error_label.hide()
        self.error_label.setText("")

    def _step(self, delta: Decimal) -> None:
        text = self.edit.text().strip()
        try:
            base = self._current() if text else Decimal("0")
        except DecimalInputError as exc:
            self._show_error(str(exc))
            return
        try:
            target = base + delta
            if self.spec.minimum is not None:
                target = max(target, self.spec.minimum)
            if self.spec.maximum is not None:
                target = min(target, self.spec.maximum)
            target = parse_decimal_input(str(target), "value")
        except DecimalInputError as exc:
            self._show_error(str(exc))
            return
        except (ArithmeticError, TypeError, ValueError):
            self._show_error("The value could not be adjusted as a finite number.")
            return
        self._clear_error()
        self.edit.setText(_trim(target))
        self.request(target)

    def _on_typed(self) -> None:
        try:
            value = self._current(bounded=True)
        except DecimalInputError as exc:
            self._show_error(str(exc))
            return
        self._clear_error()
        self.request(value)

    def show_value(self, value: Any) -> None:
        if value is None or self.busy_editing():
            return
        self._clear_error()
        self.edit.setText(_trim(value) if isinstance(value, Decimal) else str(value))

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        self.edit.setReadOnly(not editable)
        for button in self.buttons:
            button.setEnabled(editable)

    def busy_editing(self) -> bool:
        return self.edit.hasFocus()


class TextWidget(MetricWidget):
    """A plain field, for a text metric."""

    priority = 40

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        return spec.kind is MetricKind.TEXT

    def build(self) -> None:
        self.edit = QLineEdit()
        self.edit.returnPressed.connect(self._on_typed)
        self.apply = QPushButton("Set")
        self.apply.setFixedWidth(46)
        self.apply.clicked.connect(self._on_typed)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.edit, 1)
        row.addWidget(self.apply)

    def _on_typed(self) -> None:
        self.request(self.edit.text())

    def show_value(self, value: Any) -> None:
        if self.busy_editing():
            return
        self.edit.setText("" if value is None else str(value))

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        self.edit.setReadOnly(not editable)
        self.apply.setEnabled(editable)

    def busy_editing(self) -> bool:
        return self.edit.hasFocus()


class ReadoutWidget(MetricWidget):
    """Just the value.

    The fallback, and the reason an unknown metric type still shows up rather than being
    dropped: whatever it is, it has a value and that can be displayed.
    """

    priority = 1000

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:  # noqa: ARG003
        return True

    def build(self) -> None:
        self.readout = QLabel(NO_VALUE)
        self.readout.setAlignment(Qt.AlignCenter)
        self.readout.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.readout)

    def show_value(self, value: Any) -> None:
        self.readout.setText(NO_VALUE if value is None else str(value))

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001, ARG002
        # Nothing to enable; this control never writes.
        return
