"""The concrete metric controls.

Each one declares which metrics it can represent. The factory asks them in order, so a more
specific control gets the chance to claim a metric before a more general one.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
)

from ...model import MAX_DECIMAL_WIRE_CHARS, SAMPLE_ARRAY_KINDS, MetricKind, RemoteRange
from ..decimal_input import DecimalInputError, parse_decimal_input
from ..helpers import NO_VALUE, value_text
from ..no_wheel import NoWheelComboBox, NoWheelSlider
from ..styling import constrain_dynamic_label, mark_as_error, mute, set_button_kind, token
from .base import MetricWidget, WidgetSpec
from .plot import SamplePlot

# A Qt slider works in ints. Above this many steps it stops being a useful control and the
# stepper is a better fit, so the slider declines the metric.
MAX_SLIDER_STEPS = 100_000

# How much the stepper's buttons move the value.
STEP_SMALL = Decimal("1")
STEP_LARGE = Decimal("10")

def _trim(value: Decimal) -> str:
    """Render a Decimal without a trailing '.0' but keep genuine decimals."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def show_choice_value(box: QComboBox, value: object) -> None:
    """Display even a missing or non-writable current value without offering it as a write."""
    with QSignalBlocker(box):
        box.setPlaceholderText(value_text(value))
        box.setCurrentIndex(-1 if value is None else box.findText(str(value)))


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
    caption = "Plot"

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        return spec.kind in SAMPLE_ARRAY_KINDS

    def footer_extras(self) -> list[str]:
        if self.spec.kind is not MetricKind.WAVEFORM:
            return []
        return [f"{_trim(self.settings.seconds_per_width)} s per width"]

    def build(self) -> None:
        self.plot = SamplePlot(
            scrolling=self.spec.kind is MetricKind.WAVEFORM,
            minimum=self.spec.minimum,
            maximum=self.spec.maximum,
            # What lets the trace move at the rate the samples were taken at rather than
            # lurching once per report. It is on the descriptor for exactly this.
            sample_period=self.spec.sample_period,
            show_legend=self.settings.legend,
            plot_seconds=float(self.settings.seconds_per_width),
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
    caption = "Dropdown"

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        return spec.kind is MetricKind.CHOICE and bool(spec.allowed_values)

    def build(self) -> None:
        self.box = NoWheelComboBox()
        self.box.addItems(list(self.spec.allowed_values))
        show_choice_value(self.box, None)
        self.box.activated.connect(self._on_activated)
        # What a read-only choice shows instead: a disabled drop-down greys the value out.
        self.value_label = QLabel(NO_VALUE)
        constrain_dynamic_label(self.value_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.box)
        layout.addWidget(self.value_label)

    def _on_activated(self, index: int) -> None:
        # activated rather than currentIndexChanged: only fire on a real click, not when
        # show_value moves the selection.
        self.request(self.box.itemText(index))

    def show_value(self, value: Any) -> None:
        self.value_label.setText(NO_VALUE if value is None else str(value))
        if self.busy_editing():
            return
        show_choice_value(self.box, value)

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        self.box.setEnabled(editable)
        self.box.setVisible(editable)
        self.value_label.setVisible(not editable)

    def busy_editing(self) -> bool:
        return self.box.view().isVisible()


class _RangeSlider(NoWheelSlider):
    """A slider that marks the parts of its track a value may not take."""

    def __init__(self, orientation: Qt.Orientation) -> None:
        super().__init__(orientation)
        # (from, to) as fractions of the track.
        self.blocked: list[tuple[float, float]] = []

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        super().paintEvent(event)
        if not self.blocked:
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.CC_Slider, option, QStyle.SC_SliderGroove, self)
        colour = QColor(token("state/blocked"))
        colour.setAlphaF(0.55)
        painter = QPainter(self)
        for start, end in self.blocked:
            left = groove.left() + start * groove.width()
            painter.fillRect(int(left), groove.center().y() - 2, max(1, int((end - start) * groove.width())), 4, colour)
        painter.end()


class SliderWidget(MetricWidget):
    """A slider, for a number with both a minimum and a maximum.

    With the range option on and a peer that publishes both, the track spans the technical
    range and the parts outside the allowed range are marked and cannot be picked.
    """

    priority = 20
    caption = "Slider"

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        ranges = spec.slider_ranges
        if (
            spec.kind is not MetricKind.NUMBER
            or len(ranges) != 1
            or ranges[0].lower is None
            or ranges[0].upper is None
        ):
            return False
        return cls._steps(spec) is not None

    @staticmethod
    def _steps(spec: WidgetSpec, allowed_range: RemoteRange | None = None) -> int | None:
        """How many slider positions the range needs, or None if that is unreasonable."""
        allowed_range = allowed_range or spec.slider_ranges[0]
        with localcontext() as context:
            context.prec = MAX_DECIMAL_WIRE_CHARS * 2 + 32
            span = allowed_range.upper - allowed_range.lower
        if span <= 0:
            return None
        resolution = allowed_range.step_width or spec.resolution or Decimal("1")
        if not resolution.is_finite() or resolution <= 0:
            return None
        span_numerator, span_denominator = span.as_integer_ratio()
        resolution_numerator, resolution_denominator = resolution.as_integer_ratio()
        numerator = span_numerator * resolution_denominator
        denominator = span_denominator * resolution_numerator
        if allowed_range.step_width is None:
            steps = (numerator + denominator - 1) // denominator
        else:
            steps = numerator // denominator
        if steps < 1 or steps > MAX_SLIDER_STEPS:
            return None
        return steps

    def _technical_span(self) -> RemoteRange | None:
        """The technical range to draw, when it is worth showing next to the allowed one."""
        spec = self.spec
        if not (self.settings.show_ranges and len(spec.allowed_ranges) == 1 and len(spec.technical_ranges) == 1):
            return None
        technical, allowed = spec.technical_ranges[0], spec.allowed_ranges[0]
        if None in (technical.lower, technical.upper, allowed.lower, allowed.upper):
            return None
        encloses = technical.lower <= allowed.lower and allowed.upper <= technical.upper
        wider = (technical.lower, technical.upper) != (allowed.lower, allowed.upper)
        if not (encloses and wider and self._steps(spec, technical)):
            return None
        return technical

    def build(self) -> None:
        self._allowed = self.spec.slider_ranges[0]
        technical = self._technical_span()
        self._range = technical or self._allowed
        self._clamp = self._allowed if technical is not None else None
        self._resolution = self._range.step_width or self.spec.resolution or Decimal("1")
        self._include_upper = self._range.step_width is None
        self._steps = self._steps(self.spec, self._range) or 1
        self._last_sent: Decimal | None = None

        self.slider = _RangeSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(self._steps)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(1, self._steps // 10))
        self.slider.valueChanged.connect(self._on_moved)
        self.slider.sliderReleased.connect(self._on_released)
        self._focus_policy = self.slider.focusPolicy()

        self.readout = QLabel(NO_VALUE)
        constrain_dynamic_label(self.readout)
        self.readout.setAlignment(Qt.AlignCenter)
        self.readout.setMinimumWidth(70)

        low = QLabel(_trim(self._range.lower))
        high = QLabel(_trim(self._position_to_value(self._steps)))
        for end in (low, high):
            constrain_dynamic_label(end, max_width=90)
            mute(end)
        self.low_label = low
        self.high_label = high

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(low)
        row.addWidget(self.slider, 1)
        row.addWidget(high)

        self.allowed_label = QLabel("")
        self.allowed_label.setAlignment(Qt.AlignCenter)
        font = self.allowed_label.font()
        font.setPixelSize(11)
        self.allowed_label.setFont(font)
        palette = self.allowed_label.palette()
        palette.setColor(QPalette.WindowText, token("state/blocked"))
        self.allowed_label.setPalette(palette)
        if self._clamp is not None:
            self.allowed_label.setText(f"allowed {_trim(self._clamp.lower)} \u2013 {_trim(self._clamp.upper)}")
            span = self._range.upper - self._range.lower
            self.slider.blocked = [
                part for part in (
                    (0.0, float((self._clamp.lower - self._range.lower) / span)),
                    (float((self._clamp.upper - self._range.lower) / span), 1.0),
                ) if part[1] > part[0]
            ]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.readout)
        layout.addLayout(row)
        layout.addWidget(self.allowed_label)
        # Only once it has a parent: showing a parentless label opens it as a window.
        self.allowed_label.setVisible(self._clamp is not None)

    def footer_extras(self) -> list[str]:
        if self._clamp is None:
            return []
        return [f"technical {_trim(self._range.lower)} \u2013 {_trim(self._range.upper)}"]

    def _clamped(self, value: Decimal) -> Decimal:
        if self._clamp is None:
            return value
        return min(max(value, self._clamp.lower), self._clamp.upper)

    def _send(self, value: Decimal) -> None:
        if self.slider.value() != self._value_to_position(value):
            with QSignalBlocker(self.slider):
                self.slider.setValue(self._value_to_position(value))
            self.readout.setText(_trim(value))
        self._last_sent = value
        self.request(value)

    def _position_to_value(self, position: int) -> Decimal:
        if self._include_upper and position >= self._steps:
            return self._range.upper
        with localcontext() as context:
            context.prec = MAX_DECIMAL_WIRE_CHARS * 2 + 32
            value = self._range.lower + Decimal(position) * self._resolution
        return min(value, self._range.upper)

    def _value_to_position(self, value: Decimal) -> int:
        if value <= self._range.lower:
            return 0
        if value >= self._range.upper:
            return self._steps
        with localcontext() as context:
            context.prec = MAX_DECIMAL_WIRE_CHARS * 2 + 32
            offset = (value - self._range.lower) / self._resolution
        return max(0, min(self._steps, int(offset)))

    def _on_moved(self, position: int) -> None:
        value = self._clamped(self._position_to_value(position))
        self.readout.setText(_trim(value))
        # In "release" mode only the readout moves while dragging, so a drag across the
        # range does not fire a write per pixel. "Change" sends every new value.
        if self.slider.isSliderDown():
            if self.settings.update_on == "change" and value != self._last_sent:
                self._send(value)
        elif self.slider.hasFocus():
            self._send(value)

    def _on_released(self) -> None:
        self._send(self._clamped(self._position_to_value(self.slider.value())))

    def show_value(self, value: Any) -> None:
        if self.busy_editing():
            return
        if value is None:
            self.readout.setText(NO_VALUE)
            return
        if not isinstance(value, Decimal):
            return
        self.slider.blockSignals(True)
        self.slider.setValue(self._value_to_position(value))
        self.slider.blockSignals(False)
        self.readout.setText(_trim(value))

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        # Inert rather than disabled, so the position still reads at full contrast.
        self.slider.setAttribute(Qt.WA_TransparentForMouseEvents, not editable)
        self.slider.setFocusPolicy(self._focus_policy if editable else Qt.NoFocus)

    def busy_editing(self) -> bool:
        return self.slider.isSliderDown()


class StepperWidget(MetricWidget):
    """A value with buttons either side, for a number that has no useful range.

    The layout is  -10  -1  [ value ]  +1  +10 , and the value can also be typed.
    """

    priority = 30
    caption = "Number input"

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
        constrain_dynamic_label(self.error_label, max_lines=3)
        mark_as_error(self.error_label)
        self.error_label.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(row)
        layout.addWidget(self.error_label)

    def _make_button(self, delta: Decimal) -> QPushButton:
        caption = f"+{_trim(delta)}" if delta > 0 else _trim(delta)
        button = QPushButton(caption)
        set_button_kind(button, "design-outline")
        button.setFixedWidth(46)
        button.setProperty("stepSize", "small" if abs(delta) == STEP_SMALL else "large")
        button.clicked.connect(lambda _=False, d=delta: self._step(d))
        self.buttons.append(button)
        return button

    def _current(self, *, validate_domain: bool = False) -> Decimal:
        return parse_decimal_input(
            self.edit.text(),
            "value",
            allowed_ranges=self.spec.control_ranges if validate_domain else None,
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
            with localcontext() as context:
                context.prec = MAX_DECIMAL_WIRE_CHARS * 2 + 32
                target = base + delta
            ranges = self.spec.control_ranges
            if len(ranges) == 1:
                if ranges[0].lower is not None:
                    target = max(target, ranges[0].lower)
                if ranges[0].upper is not None:
                    target = min(target, ranges[0].upper)
            target = parse_decimal_input(
                str(target),
                "value",
                allowed_ranges=ranges,
            )
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
            value = self._current(validate_domain=True)
        except DecimalInputError as exc:
            self._show_error(str(exc))
            return
        self._clear_error()
        self.request(value)

    def show_value(self, value: Any) -> None:
        if self.busy_editing():
            return
        self._clear_error()
        if value is None:
            self.edit.clear()
        else:
            self.edit.setText(_trim(value) if isinstance(value, Decimal) else str(value))

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        self.edit.setReadOnly(not editable)
        shown = {"small": self.settings.small_steps, "large": self.settings.large_steps}
        for button in self.buttons:
            button.setEnabled(editable)
            button.setVisible(editable and shown[button.property("stepSize")])

    def busy_editing(self) -> bool:
        return self.edit.hasFocus() or self.edit.isModified()


class _CtrlEnterSends(QObject):
    """Enter adds a line in a multiline field; Ctrl+Enter sends it."""

    def __init__(self, send, parent: QObject) -> None:  # noqa: ANN001
        super().__init__(parent)
        self._send = send

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if (event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and event.modifiers() & Qt.ControlModifier):
            self._send()
            return True
        return super().eventFilter(watched, event)


class TextWidget(MetricWidget):
    """A field for a text metric.

    While the field holds something that was neither sent nor received, it turns amber and
    says so: an unsent edit looks exactly like a value otherwise.
    """

    priority = 40
    caption = "Text"

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:
        return spec.kind is MetricKind.TEXT

    def build(self) -> None:
        self._baseline = ""
        self.multiline = self.settings.multiline
        self.edit = QLineEdit()
        self.edit.returnPressed.connect(self._on_typed)
        self.edit.textChanged.connect(self._update_pending)
        self.multi_edit = QPlainTextEdit()
        self.multi_edit.setFixedHeight(self.multi_edit.fontMetrics().lineSpacing() * 3 + 12)
        self.multi_edit.textChanged.connect(self._update_pending)
        self.multi_edit.installEventFilter(_CtrlEnterSends(self._on_typed, self.multi_edit))

        self.apply = QPushButton("Set")
        set_button_kind(self.apply, "design")
        self.apply.setFixedWidth(46)
        self.apply.clicked.connect(self._on_typed)
        self.pending_label = QLabel("\u25cf Not sent")
        palette = self.pending_label.palette()
        palette.setColor(QPalette.WindowText, token("state/pending"))
        self.pending_label.setPalette(palette)
        self.pending_label.hide()
        self.send_hint = QLabel("Ctrl+Enter to send")
        mute(self.send_hint)

        # Split: what the device holds above what the user wants it to hold.
        self.current_caption = QLabel("Current")
        self.desired_caption = QLabel("Desired")
        self.current_label = QLabel(NO_VALUE)
        constrain_dynamic_label(self.current_label, max_lines=3)
        for caption in (self.current_caption, self.desired_caption):
            mute(caption)

        send_row = QHBoxLayout()
        send_row.setContentsMargins(0, 0, 0, 0)
        send_row.addWidget(self.pending_label)
        send_row.addWidget(self.send_hint)
        send_row.addStretch(1)
        send_row.addWidget(self.apply)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.current_caption)
        layout.addWidget(self.current_label)
        layout.addWidget(self.desired_caption)
        if self.multiline:
            layout.addWidget(self.multi_edit)
            layout.addLayout(send_row)
            self.edit.setParent(self)
            self.edit.hide()
        else:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(self.edit, 1)
            row.addWidget(self.apply)
            layout.addLayout(row)
            send_row.removeWidget(self.apply)
            layout.addLayout(send_row)
            self.multi_edit.setParent(self)
            self.multi_edit.hide()
            self.send_hint.hide()

    def _field_text(self) -> str:
        return self.multi_edit.toPlainText() if self.multiline else self.edit.text()

    def _set_field_text(self, text: str) -> None:
        if self.multiline:
            self.multi_edit.setPlainText(text)
            self.multi_edit.document().setModified(False)
        else:
            self.edit.setText(text)

    def _update_pending(self) -> None:
        pending = self._field_text() != self._baseline
        self.pending_label.setVisible(pending)
        field = self.multi_edit if self.multiline else self.edit
        palette = field.palette()
        colour = token("state/pending") if pending else self.palette().color(QPalette.Text)
        palette.setColor(QPalette.Text, colour)
        field.setPalette(palette)
        field.setStyleSheet(f"border: 1.5px solid {token('state/pending').name()};" if pending else "")

    def _on_typed(self) -> None:
        text = self._field_text()
        self._baseline = text
        self.multi_edit.document().setModified(False)
        self.request(text)
        self._update_pending()

    def show_value(self, value: Any) -> None:
        text = "" if value is None else str(value)
        self.current_label.setText(NO_VALUE if value is None else text)
        if self.busy_editing():
            return
        self._baseline = text
        self._set_field_text(text)
        self._update_pending()

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        self.edit.setReadOnly(not editable)
        self.multi_edit.setReadOnly(not editable)
        self.apply.setEnabled(editable)
        self.apply.setVisible(editable)
        split = editable and self.settings.split
        for part in (self.current_caption, self.current_label, self.desired_caption):
            part.setVisible(split)

    def busy_editing(self) -> bool:
        if self.multiline:
            return self.multi_edit.hasFocus() or self.multi_edit.document().isModified()
        return self.edit.hasFocus() or self.edit.isModified()


class ReadoutWidget(MetricWidget):
    """Just the value.

    The fallback, and the reason an unknown metric type still shows up rather than being
    dropped: whatever it is, it has a value and that can be displayed.
    """

    priority = 1000
    caption = "Value only"

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:  # noqa: ARG003
        return True

    def build(self) -> None:
        self.readout = QLabel(NO_VALUE)
        constrain_dynamic_label(self.readout, max_lines=3)
        self.readout.setAlignment(Qt.AlignCenter)
        self.readout.setTextInteractionFlags(Qt.TextSelectableByMouse)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.readout)

    def show_value(self, value: Any) -> None:
        self.readout.setText(value_text(value))

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001, ARG002
        # Nothing to enable; this control never writes.
        return
