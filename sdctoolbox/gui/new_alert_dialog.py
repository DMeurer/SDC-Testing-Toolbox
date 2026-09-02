"""The "New alarm" dialog.

An alarm watches one metric. Give it limits and it follows that metric by itself; leave them
blank and it only moves when you raise or clear it by hand.
"""

from __future__ import annotations

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

from ..constants import ALERT_HANDLE_PREFIX
from ..model import (
    AlertKind,
    AlertManifestation,
    AlertPriority,
    AlertSignalSpec,
    AlertSpec,
    MetricKind,
    MetricSpec,
    slugify,
)
from .decimal_input import DecimalInputError, parse_decimal_input
from .no_wheel import NoWheelComboBox
from .styling import constrain_dynamic_label, mark_as_error, mute

KIND_CAPTIONS = [
    ("Technical", AlertKind.TECHNICAL),
    ("Physiological", AlertKind.PHYSIOLOGICAL),
    ("Other", AlertKind.OTHER),
]

PRIORITY_CAPTIONS = [
    ("High", AlertPriority.HIGH),
    ("Medium", AlertPriority.MEDIUM),
    ("Low", AlertPriority.LOW),
    ("None", AlertPriority.NONE),
]

MANIFESTATION_CAPTIONS = [
    ("Visual", AlertManifestation.VIS),
    ("Audible", AlertManifestation.AUD),
    ("Tangible", AlertManifestation.TAN),
    ("Other", AlertManifestation.OTH),
]


class NewAlertDialog(QDialog):
    """Ask the user for an alarm definition."""

    def __init__(self, metrics: dict[str, MetricSpec], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New alarm")
        self.setMinimumWidth(440)
        self._metrics = metrics
        # Whether the limits row applies to the selected source. Tracked rather than read
        # back off the widget, so the logic does not depend on the dialog being on screen.
        self._limits_apply = False

        self.source_box = NoWheelComboBox()
        for handle, spec in sorted(metrics.items()):
            caption = f"{spec.label} ({handle})" if spec.label else handle
            self.source_box.addItem(caption, handle)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Pressure out of range")

        self.kind_box = NoWheelComboBox()
        for caption, kind in KIND_CAPTIONS:
            self.kind_box.addItem(caption, kind)

        self.priority_box = NoWheelComboBox()
        for caption, priority in PRIORITY_CAPTIONS:
            self.priority_box.addItem(caption, priority)
        self.priority_box.setCurrentIndex(1)

        self.lower_edit = QLineEdit()
        self.lower_edit.setPlaceholderText("optional")
        self.upper_edit = QLineEdit()
        self.upper_edit.setPlaceholderText("optional")
        limits = QHBoxLayout()
        limits.setContentsMargins(0, 0, 0, 0)
        limits.addWidget(QLabel("below"))
        limits.addWidget(self.lower_edit)
        limits.addWidget(QLabel("or above"))
        limits.addWidget(self.upper_edit)
        self.limits_widget = QWidget()
        self.limits_widget.setLayout(limits)

        self.delegable_box = QCheckBox("Another device may announce this alarm")
        self.delegable_box.setToolTip(
            "Sets SignalDelegationSupported on every configured signal. Without it a delegation is\n"
            "refused, because BICEPS only allows one where the descriptor says so.",
        )

        self.signal_boxes: list[tuple[AlertManifestation, QCheckBox, QCheckBox]] = []
        signals = QVBoxLayout()
        signals.setContentsMargins(0, 0, 0, 0)
        for index, (caption, manifestation) in enumerate(MANIFESTATION_CAPTIONS):
            enabled = QCheckBox(caption)
            enabled.setChecked(index < 2)
            latching = QCheckBox("Latching")
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(enabled)
            row.addWidget(latching)
            row.addStretch(1)
            signals.addLayout(row)
            self.signal_boxes.append((manifestation, enabled, latching))
        self.signals_widget = QWidget()
        self.signals_widget.setLayout(signals)

        self.hint = QLabel(
            "Leave the limits blank for an alarm you raise by hand. "
            "Select the signals to create. A latching signal keeps announcing a cleared alarm until stopped.",
        )
        self.hint.setWordWrap(True)
        mute(self.hint)

        self.handle_preview = QLabel("-")
        constrain_dynamic_label(self.handle_preview)
        mute(self.handle_preview)

        self.error_label = QLabel("")
        constrain_dynamic_label(self.error_label, max_lines=3)
        mark_as_error(self.error_label)
        self.error_label.hide()

        form = QFormLayout()
        form.addRow("Watches", self.source_box)
        form.addRow("Label", self.label_edit)
        form.addRow("Kind", self.kind_box)
        form.addRow("Priority", self.priority_box)
        form.addRow("Raise when", self.limits_widget)
        form.addRow("Delegation", self.delegable_box)
        form.addRow("Signals", self.signals_widget)
        form.addRow("Handle", self.handle_preview)
        self.form = form

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.hint)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

        self.label_edit.textChanged.connect(self._update_preview)
        self.source_box.currentIndexChanged.connect(self._on_source_changed)
        self._on_source_changed()
        self._update_preview()

    # -- result --------------------------------------------------------------------

    def spec(self) -> AlertSpec | None:
        """The definition the user entered, or None if the dialog was cancelled."""
        return getattr(self, "_spec", None)

    # -- internals -----------------------------------------------------------------

    def _on_source_changed(self) -> None:
        handle = self.source_box.currentData()
        spec = self._metrics.get(handle) if handle else None
        # Limits only mean anything against a number, so for anything else the row goes
        # away rather than sitting there greyed out.
        self._limits_apply = spec is not None and spec.kind is MetricKind.NUMBER
        self._set_row_visible(self.limits_widget, visible=self._limits_apply)

    def _set_row_visible(self, widget: QWidget, *, visible: bool) -> None:
        """Show or hide a form row, label included."""
        widget.setVisible(visible)
        label = self.form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)
        # A hidden row still reserves its height until the dialog is asked to shrink.
        self.adjustSize()

    @property
    def _kind(self) -> AlertKind:
        """The selected kind.

        Read from the list rather than the combo box's userData: the BICEPS enums subclass
        str, so Qt stores them as plain strings and hands back a str that is no longer an
        enum. AlertSpec would coerce it anyway, but taking it from here keeps the type
        correct all the way through.
        """
        return KIND_CAPTIONS[self.kind_box.currentIndex()][1]

    @property
    def _priority(self) -> AlertPriority:
        """The selected priority. Same reasoning as _kind."""
        return PRIORITY_CAPTIONS[self.priority_box.currentIndex()][1]

    def _update_preview(self) -> None:
        label = self.label_edit.text().strip()
        self.handle_preview.setText(ALERT_HANDLE_PREFIX + slugify(label) if label else "-")

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_accept(self) -> None:
        self.error_label.hide()

        source = self.source_box.currentData()
        if not source:
            self._fail("Create a data source first; an alarm has to watch something.")
            return

        label = self.label_edit.text().strip()
        if not label:
            self._fail("Give the alarm a label.")
            return

        lower = upper = None
        # Deliberately not isVisible(): a child of a dialog that has not been shown yet
        # reports False, which would silently drop the limits.
        if self._limits_apply:
            for caption, edit in (("lower limit", self.lower_edit), ("upper limit", self.upper_edit)):
                text = edit.text().strip()
                if not text:
                    continue
                try:
                    parsed = parse_decimal_input(text, caption)
                except DecimalInputError as exc:
                    self._fail(str(exc))
                    return
                if caption.startswith("lower"):
                    lower = parsed
                else:
                    upper = parsed

        signals = tuple(
            AlertSignalSpec(manifestation, latching=latching.isChecked())
            for manifestation, enabled, latching in self.signal_boxes
            if enabled.isChecked()
        )
        if not signals:
            self._fail("Select at least one signal.")
            return

        try:
            self._spec = AlertSpec(
                label=label,
                source_handle=source,
                kind=self._kind,
                priority=self._priority,
                lower_limit=lower,
                upper_limit=upper,
                delegable=self.delegable_box.isChecked(),
                signals=signals,
            )
        except (ValueError, TypeError) as exc:
            self._fail(str(exc))
            return

        self.accept()
