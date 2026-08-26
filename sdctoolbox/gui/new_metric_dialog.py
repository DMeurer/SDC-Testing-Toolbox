"""The "New data source" dialog.

Collects everything MetricSpec needs, and nothing it does not. The set of kinds offered here
is limited to the three BICEPS metric types that can be remote-controlled; waveforms and
distributions exist in the model but have no editor yet.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
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
from ..model import MetricKind, MetricSpec, slugify
from .styling import mark_as_error, mute

# Label shown in the combo box -> kind. Order decides the order in the dropdown.
OFFERED_KINDS: list[tuple[str, MetricKind]] = [
    ("Number", MetricKind.NUMBER),
    ("Text", MetricKind.TEXT),
    ("Choice", MetricKind.CHOICE),
]


class NewMetricDialog(QDialog):
    """Ask the user for a data source definition."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New data source")
        self.setMinimumWidth(420)

        self.kind_box = QComboBox()
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
        is_choice = self._kind is MetricKind.CHOICE
        is_number = self._kind is MetricKind.NUMBER

        self._set_row_visible(self.values_edit, visible=is_choice)
        self._set_row_visible(self.resolution_edit, visible=is_number)
        self._set_row_visible(self.limits_widget, visible=is_number)

        self.values_edit.setToolTip(
            "Comma separated. These become the AllowedValue list of the metric.",
        )
        self.resolution_edit.setToolTip(
            "BICEPS requires a resolution on a numeric metric. Use 1 for whole numbers.",
        )
        self.limits_widget.setToolTip(
            "Optional lower and upper limit. Either can be left blank.\n"
            "Becomes TechnicalRange on the metric and AllowedRange on its set operation, "
            "so other devices are refused values outside it.",
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
        if self._kind is MetricKind.NUMBER:
            raw = self.resolution_edit.text().strip() or "1"
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

        try:
            self._spec = MetricSpec(
                label=label,
                kind=self._kind,
                unit_label=self.unit_edit.text().strip(),
                allowed_values=values,
                resolution=resolution,
                minimum=minimum,
                maximum=maximum,
                controllable=self.controllable_box.isChecked(),
                initial_value=values[0] if values else None,
            )
        except (ValueError, TypeError) as exc:
            self._fail(str(exc))
            return

        self.accept()
