"""The "New action" dialog.

An action is a BICEPS ActivateOperation: "do the thing" rather than "take this value". It
acts on one descriptor, and in this toolbox it may also set one metric when invoked, so what
it did shows up in the metrics.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..model import ActionSpec, MetricKind, MetricSpec
from .decimal_input import DecimalInputError, parse_decimal_input
from .kind_sections import kind_section, style_dialog_buttons
from .no_wheel import NoWheelComboBox
from .styling import mark_as_error

NO_EFFECT = "(nothing)"


class NewActionDialog(QDialog):
    """Ask the user for an action definition."""

    def __init__(self, metrics: dict[str, MetricSpec], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New action")
        self.setMinimumWidth(380)
        self._metrics = metrics
        self._spec: ActionSpec | None = None

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("Zero the transducer")
        self.target_box = NoWheelComboBox()
        self.effect_box = NoWheelComboBox()
        self.effect_box.addItem(NO_EFFECT, None)
        for handle, spec in sorted(metrics.items()):
            caption = f"{handle} ({spec.label})" if spec.label else handle
            self.target_box.addItem(caption, handle)
            if not spec.is_sample_array:
                self.effect_box.addItem(caption, handle)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("optional")
        self.value_edit = QLineEdit()

        action, form = kind_section("Action", "mdib", "sent to consumers")
        form.addRow("Label", self.label_edit)
        form.addRow("Acts on", self.target_box)
        form.addRow("Note", self.note_edit)
        effect, effect_form = kind_section("Effect", "toolbox", "optional, what the simulation does")
        effect_form.addRow("Sets", self.effect_box)
        effect_form.addRow("To", self.value_edit)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        mark_as_error(self.error_label)
        self.error_label.hide()
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        style_dialog_buttons(buttons)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(action)
        layout.addWidget(effect)
        layout.addWidget(self.error_label)
        layout.addWidget(buttons)

        self.effect_box.currentIndexChanged.connect(lambda _: self.value_edit.setEnabled(self.effect_box.currentIndex() > 0))
        self.value_edit.setEnabled(False)

    def spec(self) -> ActionSpec | None:
        """The definition the user entered, or None if the dialog was cancelled."""
        return self._spec

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_accept(self) -> None:
        target = self.target_box.currentData()
        if not target:
            self._fail("Create a data source first; an action has to act on something.")
            return
        effects = {}
        effect = self.effect_box.currentData()
        if effect:
            text = self.value_edit.text().strip()
            if self._metrics[effect].kind is MetricKind.NUMBER:
                try:
                    effects[effect] = parse_decimal_input(text, "value")
                except DecimalInputError as exc:
                    self._fail(str(exc))
                    return
            else:
                effects[effect] = text
        try:
            self._spec = ActionSpec(label=self.label_edit.text().strip(), target_handle=target,
                                    effects=effects, note=self.note_edit.text().strip())
        except (ValueError, TypeError) as exc:
            self._fail(str(exc))
            return
        self.accept()
