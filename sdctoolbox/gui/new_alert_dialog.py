"""The "New alarm" dialog.

An alarm watches one metric. Give it limits and it follows that metric by itself; leave them
blank and it only moves when you raise or clear it by hand.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from PySide6.QtWidgets import (
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

from ..constants import ALERT_HANDLE_PREFIX
from ..model import AlertKind, AlertPriority, AlertSpec, MetricKind, MetricSpec, slugify
from .styling import mark_as_error, mute

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


class NewAlertDialog(QDialog):
    """Ask the user for an alarm definition."""

    def __init__(self, metrics: dict[str, MetricSpec], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New alarm")
        self.setMinimumWidth(440)
        self._metrics = metrics

        self.source_box = QComboBox()
        for handle, spec in sorted(metrics.items()):
            caption = f"{spec.label} ({handle})" if spec.label else handle
            self.source_box.addItem(caption, handle)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Pressure out of range")

        self.kind_box = QComboBox()
        for caption, kind in KIND_CAPTIONS:
            self.kind_box.addItem(caption, kind)

        self.priority_box = QComboBox()
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

        self.hint = QLabel(
            "Leave the limits blank for an alarm you raise by hand. "
            "A visual and an audible signal are created either way.",
        )
        self.hint.setWordWrap(True)
        mute(self.hint)

        self.handle_preview = QLabel("-")
        mute(self.handle_preview)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        mark_as_error(self.error_label)
        self.error_label.hide()

        form = QFormLayout()
        form.addRow("Watches", self.source_box)
        form.addRow("Label", self.label_edit)
        form.addRow("Kind", self.kind_box)
        form.addRow("Priority", self.priority_box)
        form.addRow("Raise when", self.limits_widget)
        form.addRow("Handle", self.handle_preview)

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
        # Limits only mean anything against a number.
        numeric = spec is not None and spec.kind is MetricKind.NUMBER
        self.limits_widget.setEnabled(numeric)
        self.limits_widget.setToolTip(
            "" if numeric else "Limits can only be compared against a numeric metric",
        )

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
        if self.limits_widget.isEnabled():
            for caption, edit in (("lower limit", self.lower_edit), ("upper limit", self.upper_edit)):
                text = edit.text().strip()
                if not text:
                    continue
                try:
                    parsed = Decimal(text)
                except InvalidOperation:
                    self._fail(f"{text!r} is not a valid {caption}.")
                    return
                if caption.startswith("lower"):
                    lower = parsed
                else:
                    upper = parsed

        try:
            self._spec = AlertSpec(
                label=label,
                source_handle=source,
                kind=self.kind_box.currentData(),
                priority=self.priority_box.currentData(),
                lower_limit=lower,
                upper_limit=upper,
            )
        except (ValueError, TypeError) as exc:
            self._fail(str(exc))
            return

        self.accept()
