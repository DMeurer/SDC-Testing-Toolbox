"""The "New data source" dialog.

Collects everything MetricSpec needs, and nothing it does not. All five BICEPS metric types
are offered; the rows change to suit, because what is mandatory depends entirely on which
one is chosen and a field that does not apply is worse than absent.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..constants import METRIC_HANDLE_PREFIX
from ..model import DEFAULT_SAMPLE_PERIOD, MetricKind, MetricSpec, WaveformShape, slugify
from .no_wheel import NoWheelComboBox
from .styling import mark_as_error, mute

# Label shown in the combo box -> kind. Order decides the order in the dropdown.
OFFERED_KINDS: list[tuple[str, MetricKind]] = [
    ("Number", MetricKind.NUMBER),
    ("Text", MetricKind.TEXT),
    ("Choice", MetricKind.CHOICE),
    ("Waveform", MetricKind.WAVEFORM),
    ("Distribution", MetricKind.DISTRIBUTION),
]

# Caption -> shape, read by index for the same reason the kind is. See NewMetricDialog._kind.
OFFERED_SHAPES: list[tuple[str, WaveformShape]] = [
    ("Sine", WaveformShape.SINE),
    ("Sawtooth", WaveformShape.SAWTOOTH),
    ("Square", WaveformShape.SQUARE),
    ("Noise", WaveformShape.NOISE),
]


class NewMetricDialog(QDialog):
    """Ask the user for a data source definition."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New data source")
        self.setMinimumWidth(420)

        self.kind_box = NoWheelComboBox()
        for caption, kind in OFFERED_KINDS:
            self.kind_box.addItem(caption, kind)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Zoom level")

        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("optional, e.g. steps or mmHg")

        self.values_edit = QLineEdit()
        self.values_edit.setPlaceholderText("IDLE, RUN, PAUSE")

        self.resolution_edit = QLineEdit()
        self.resolution_edit.setPlaceholderText("1")

        self.minimum_edit = QLineEdit()
        self.minimum_edit.setPlaceholderText("optional")
        self.maximum_edit = QLineEdit()
        self.maximum_edit.setPlaceholderText("optional")
        limits = QHBoxLayout()
        limits.setContentsMargins(0, 0, 0, 0)
        limits.addWidget(self.minimum_edit)
        limits.addWidget(QLabel("to"))
        limits.addWidget(self.maximum_edit)
        self.limits_widget = QWidget()
        self.limits_widget.setLayout(limits)

        self.controllable_box = QCheckBox("Allow other devices to change this value")
        self.controllable_box.setChecked(True)

        # -- waveform
        self.sample_period_edit = QLineEdit()
        self.sample_period_edit.setPlaceholderText(str(DEFAULT_SAMPLE_PERIOD))
        self.shape_box = NoWheelComboBox()
        for caption, shape in OFFERED_SHAPES:
            self.shape_box.addItem(caption, shape)

        # -- distribution
        self.domain_unit_edit = QLineEdit()
        self.domain_unit_edit.setPlaceholderText("Hz")
        self.domain_min_edit = QLineEdit()
        self.domain_min_edit.setPlaceholderText("0")
        self.domain_max_edit = QLineEdit()
        self.domain_max_edit.setPlaceholderText("500")
        domain = QHBoxLayout()
        domain.setContentsMargins(0, 0, 0, 0)
        domain.addWidget(self.domain_min_edit)
        domain.addWidget(QLabel("to"))
        domain.addWidget(self.domain_max_edit)
        self.domain_widget = QWidget()
        self.domain_widget.setLayout(domain)

        self.handle_preview = QLabel("-")
        mute(self.handle_preview)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        mark_as_error(self.error_label)
        self.error_label.hide()

        form = QFormLayout()
        form.addRow("Kind", self.kind_box)
        form.addRow("Label", self.label_edit)
        form.addRow("Unit", self.unit_edit)
        form.addRow("Allowed values", self.values_edit)
        form.addRow("Resolution", self.resolution_edit)
        form.addRow("Range", self.limits_widget)
        form.addRow("Sample period", self.sample_period_edit)
        form.addRow("Shape", self.shape_box)
        form.addRow("Domain unit", self.domain_unit_edit)
        form.addRow("Domain", self.domain_widget)
        form.addRow("", self.controllable_box)
        form.addRow("Handle", self.handle_preview)
        self.form = form

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

        self.kind_box.currentIndexChanged.connect(self._on_kind_changed)
        self.label_edit.textChanged.connect(self._update_preview)

        self._on_kind_changed()
        self._update_preview()

    # -- result --------------------------------------------------------------------

    def spec(self) -> MetricSpec | None:
        """The definition the user entered, or None if the dialog was cancelled."""
        return getattr(self, "_spec", None)

    # -- internals -----------------------------------------------------------------

    @property
    def _kind(self) -> MetricKind:
        """The selected kind.

        Taken from the list by index rather than from the combo box's userData. That works
        today because MetricKind is a plain Enum, but the BICEPS enums subclass str and Qt
        silently converts those to plain strings on the way through a QVariant. Reading the
        list avoids depending on which kind of enum this happens to be.
        """
        return OFFERED_KINDS[self.kind_box.currentIndex()][1]

    @property
    def _shape(self) -> WaveformShape:
        """The selected curve. Read by index, for the same reason as _kind."""
        return OFFERED_SHAPES[self.shape_box.currentIndex()][1]

    def _set_row_visible(self, widget: QWidget, *, visible: bool) -> None:
        """Show or hide a form row, label included.

        Hiding beats disabling here: a greyed-out "Allowed values" box on a number is still
        something to read and dismiss, and the dialog is short enough that the rows moving
        is less distracting than the clutter.
        """
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    def _on_kind_changed(self) -> None:
        kind = self._kind
        is_choice = kind is MetricKind.CHOICE
        is_number = kind is MetricKind.NUMBER
        is_waveform = kind is MetricKind.WAVEFORM
        is_distribution = kind is MetricKind.DISTRIBUTION
        # Resolution and a value range are mandatory on a number and on both sample arrays;
        # only text and choice have no use for either.
        has_scale = is_number or is_waveform or is_distribution

        self._set_row_visible(self.values_edit, visible=is_choice)
        self._set_row_visible(self.resolution_edit, visible=has_scale)
        self._set_row_visible(self.limits_widget, visible=has_scale)
        self._set_row_visible(self.sample_period_edit, visible=is_waveform)
        self._set_row_visible(self.shape_box, visible=is_waveform)
        self._set_row_visible(self.domain_unit_edit, visible=is_distribution)
        self._set_row_visible(self.domain_widget, visible=is_distribution)
        # Neither sample-array kind can be written by anybody: BICEPS defines no operation
        # whose argument is a sample array.
        self.controllable_box.setVisible(kind.controllable)
        if not kind.controllable:
            self.controllable_box.setChecked(False)

        self.values_edit.setToolTip(
            "Comma separated. These become the AllowedValue list of the metric.",
        )
        self.resolution_edit.setToolTip(
            "BICEPS requires a resolution on a numeric metric and on both sample-array\n"
            "kinds. Use 1 for whole numbers.",
        )
        self.limits_widget.setToolTip(
            "Optional lower and upper limit. Either can be left blank.\n"
            "Becomes TechnicalRange on the metric and AllowedRange on its set operation, "
            "so other devices are refused values outside it.",
        )
        self.sample_period_edit.setToolTip(
            "Seconds between two samples. Mandatory on a waveform, and what tells a\n"
            "consumer how to place the samples in time.",
        )
        self.shape_box.setToolTip(
            "The curve to generate. Nothing to do with BICEPS, which carries samples and\n"
            "says nothing about their shape - this is so there is something recognisable\n"
            "to send.",
        )
        self.domain_unit_edit.setToolTip(
            "What the samples are spread over, as opposed to Unit, which is what each one\n"
            "measures. A spectrum is in dB (unit) across Hz (domain unit). Mandatory.",
        )
        self.domain_widget.setToolTip(
            "The extent of that domain, i.e. the x axis. Becomes DistributionRange, which\n"
            "is a different thing from the value range above.",
        )
        # The rows that went away leave the dialog taller than it needs to be.
        self.adjustSize()

    def _update_preview(self) -> None:
        label = self.label_edit.text().strip()
        self.handle_preview.setText(METRIC_HANDLE_PREFIX + slugify(label) if label else "-")

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_accept(self) -> None:
        self.error_label.hide()

        label = self.label_edit.text().strip()
        if not label:
            self._fail("Give the data source a label.")
            return

        values: tuple[str, ...] = ()
        if self._kind is MetricKind.CHOICE:
            values = tuple(part.strip() for part in self.values_edit.text().split(",") if part.strip())
            if len(values) < 2:  # noqa: PLR2004
                self._fail("A choice needs at least two allowed values, comma separated.")
                return
            if len(set(values)) != len(values):
                self._fail("The allowed values must be distinct.")
                return

        resolution = None
        minimum = None
        maximum = None
        kind = self._kind
        # Same three kinds the Resolution and Range rows are shown for.
        if kind in (MetricKind.NUMBER, MetricKind.WAVEFORM, MetricKind.DISTRIBUTION):
            default_resolution = "1" if kind is MetricKind.NUMBER else "0.1"
            raw = self.resolution_edit.text().strip() or default_resolution
            try:
                resolution = Decimal(raw)
            except InvalidOperation:
                self._fail(f"{raw!r} is not a valid resolution. Use a number such as 1 or 0.1.")
                return
            if resolution <= 0:
                self._fail("The resolution must be greater than zero.")
                return

            for caption, edit in (("minimum", self.minimum_edit), ("maximum", self.maximum_edit)):
                text = edit.text().strip()
                if not text:
                    continue
                try:
                    parsed = Decimal(text)
                except InvalidOperation:
                    self._fail(f"{text!r} is not a valid {caption}.")
                    return
                if caption == "minimum":
                    minimum = parsed
                else:
                    maximum = parsed

            if minimum is not None and maximum is not None and minimum > maximum:
                self._fail(f"The minimum ({minimum}) must not be greater than the maximum ({maximum}).")
                return

        sample_period = None
        if kind is MetricKind.WAVEFORM:
            raw = self.sample_period_edit.text().strip() or str(DEFAULT_SAMPLE_PERIOD)
            try:
                sample_period = Decimal(raw)
            except InvalidOperation:
                self._fail(f"{raw!r} is not a valid sample period. Use seconds, such as 0.1.")
                return
            if sample_period <= 0:
                self._fail("The sample period must be greater than zero.")
                return

        domain_unit = ""
        domain_minimum = None
        domain_maximum = None
        if kind is MetricKind.DISTRIBUTION:
            domain_unit = self.domain_unit_edit.text().strip()
            for caption, edit in (
                ("domain minimum", self.domain_min_edit),
                ("domain maximum", self.domain_max_edit),
            ):
                text = edit.text().strip()
                if not text:
                    continue
                try:
                    parsed = Decimal(text)
                except InvalidOperation:
                    self._fail(f"{text!r} is not a valid {caption}.")
                    return
                if caption.endswith("minimum"):
                    domain_minimum = parsed
                else:
                    domain_maximum = parsed
            if (
                domain_minimum is not None
                and domain_maximum is not None
                and domain_minimum > domain_maximum
            ):
                self._fail(
                    f"The domain minimum ({domain_minimum}) must not be greater than "
                    f"the domain maximum ({domain_maximum}).",
                )
                return

        try:
            self._spec = MetricSpec(
                label=label,
                kind=kind,
                unit_label=self.unit_edit.text().strip(),
                allowed_values=values,
                resolution=resolution,
                minimum=minimum,
                maximum=maximum,
                controllable=self.controllable_box.isChecked() and kind.controllable,
                initial_value=values[0] if values else None,
                sample_period=sample_period,
                shape=self._shape,
                domain_unit_label=domain_unit,
                domain_minimum=domain_minimum,
                domain_maximum=domain_maximum,
            )
        except (ValueError, TypeError) as exc:
            self._fail(str(exc))
            return

        self.accept()
