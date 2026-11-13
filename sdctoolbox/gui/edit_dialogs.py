"""Edit a running data source or alarm.

Only what BICEPS keeps in a state (or what is toolbox behaviour) can change here: the allowed
range and values of a set operation, alarm limits and priority, whether a metric is switched
on, and the generated curve. What the descriptor says is shown read-only, because changing it
would be a description modification that consumers treat as a new device structure.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..model import AlertPriority, MetricKind
from .decimal_input import DecimalInputError, parse_decimal_input
from .new_alert_dialog import PRIORITY_CAPTIONS
from .new_metric_dialog import OFFERED_DISTRIBUTIONS, distribution_combo, selected_shape, shape_combo
from .no_wheel import NoWheelComboBox
from .styling import constrain_dynamic_label, mark_as_error

if TYPE_CHECKING:
    from ..provider_service import ProviderService


def _text(value: Decimal | None) -> str:
    return "" if value is None else str(value)


def _optional_decimal(edit: QLineEdit, field: str) -> Decimal | None:
    return parse_decimal_input(edit.text(), field) if edit.text().strip() else None


def _pair(first: QWidget, second: QWidget) -> QWidget:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(first)
    row.addWidget(QLabel("to"))
    row.addWidget(second)
    widget = QWidget()
    widget.setLayout(row)
    return widget


class _EditDialog(QDialog):
    """Shared frame: a form, an error line, OK and Cancel. OK applies, and stays open on a refusal."""

    def __init__(self, title: str, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.form = QFormLayout()
        self.error_label = QLabel("")
        constrain_dynamic_label(self.error_label, max_lines=3)
        mark_as_error(self.error_label)
        self.error_label.hide()
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(self.form)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)

    def _fixed(self, caption: str, text: str) -> QLabel:
        label = QLabel(text)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setToolTip("Defined by the descriptor, so fixed while the device runs")
        self.form.addRow(caption, label)
        return label

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _on_accept(self) -> None:
        self.error_label.hide()
        try:
            self.apply()
        except (DecimalInputError, ValueError, KeyError) as exc:
            self._fail(str(exc))
            return
        self.accept()

    def apply(self) -> None:
        raise NotImplementedError


class EditSourceDialog(_EditDialog):
    """The runtime-editable parts of one data source."""

    def __init__(self, service: ProviderService, handle: str, parent: QWidget | None = None) -> None:
        self.service, self.handle = service, handle
        spec = self.spec = service.list_metrics()[handle]
        super().__init__(f"Edit {spec.label}", parent)
        settable = spec.controllable or service.operation_handle_for(handle) is not None

        self._fixed("Label", spec.label)
        self._fixed("Kind", spec.kind.value)
        self._fixed("Unit", spec.unit_label or "-")
        if spec.kind is MetricKind.NUMBER or spec.is_sample_array:
            self.technical_label = self._fixed("Technical range", spec.range_text() or "unbounded")
        if spec.kind is MetricKind.CHOICE:
            self._fixed("Values", ", ".join(spec.allowed_values))
        if spec.sample_period is not None:
            self._fixed("Sample period", f"{spec.sample_period} s")
        if spec.kind is MetricKind.DISTRIBUTION:
            self._fixed("Domain", spec.domain_text())

        self.active_box = QCheckBox("Switched on")
        self.active_box.setChecked(spec.active)
        self.active_box.setToolTip("Off keeps the last value, stops generation, refuses remote sets and raises no alarms")
        self.form.addRow("", self.active_box)

        lower, upper = spec.effective_allowed_range()
        self.allowed_minimum_edit = QLineEdit(_text(lower))
        self.allowed_maximum_edit = QLineEdit(_text(upper))
        allowed_range = _pair(self.allowed_minimum_edit, self.allowed_maximum_edit)
        self.form.addRow("Remote may set", allowed_range)
        self._show(allowed_range, spec.kind is MetricKind.NUMBER and settable,
                   self.allowed_minimum_edit, self.allowed_maximum_edit)

        self.values_list = QListWidget()
        allowed = set(spec.operation_values or spec.allowed_values)
        for value in spec.allowed_values:
            item = QListWidgetItem(value)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if value in allowed else Qt.Unchecked)
            self.values_list.addItem(item)
        self.form.addRow("Remote may pick", self.values_list)
        self._show(self.values_list, spec.kind is MetricKind.CHOICE and settable)

        self.shape_box = shape_combo()
        self.shape_box.setCurrentIndex(self.shape_box.findData(spec.shape))
        self.cycle_edit = QLineEdit(str(spec.cycle_seconds))
        self.form.addRow("Shape", self.shape_box)
        self.form.addRow("Cycle length (s)", self.cycle_edit)
        self._show(self.shape_box, spec.kind is MetricKind.WAVEFORM)
        self._show(self.cycle_edit, spec.kind is MetricKind.WAVEFORM)

        self.distribution_box = distribution_combo()
        self.distribution_box.setCurrentIndex(
            [shape for _, shape in OFFERED_DISTRIBUTIONS].index(spec.distribution_shape),
        )
        self.form.addRow("Distribution", self.distribution_box)
        self._show(self.distribution_box, spec.kind is MetricKind.DISTRIBUTION)

    def _show(self, row: QWidget, visible: bool, *parts: QWidget) -> None:  # noqa: FBT001
        self.form.setRowVisible(row, visible)
        for widget in (row, *parts):
            widget.setVisible(visible)

    def changes(self) -> dict[str, Any]:
        """What differs from the running source. Raises DecimalInputError on unreadable input."""
        spec, result = self.spec, {}
        if not self.allowed_minimum_edit.isHidden():
            allowed = (_optional_decimal(self.allowed_minimum_edit, "allowed minimum"),
                       _optional_decimal(self.allowed_maximum_edit, "allowed maximum"))
            if allowed != spec.effective_allowed_range():
                result["allowed_range"] = allowed
        if not self.values_list.isHidden():
            values = [self.values_list.item(row).text() for row in range(self.values_list.count())
                      if self.values_list.item(row).checkState() == Qt.Checked]
            if tuple(values) != (spec.operation_values or spec.allowed_values):
                result["allowed_values"] = values
        if spec.kind is MetricKind.WAVEFORM:
            if selected_shape(self.shape_box) is not spec.shape:
                result["shape"] = selected_shape(self.shape_box)
            cycle = parse_decimal_input(self.cycle_edit.text(), "cycle length", positive=True)
            if cycle != spec.cycle_seconds:
                result["cycle_seconds"] = cycle
        if spec.kind is MetricKind.DISTRIBUTION:
            shape = OFFERED_DISTRIBUTIONS[self.distribution_box.currentIndex()][1]
            if shape is not spec.distribution_shape:
                result["distribution_shape"] = shape
        if self.active_box.isChecked() != spec.active:
            result["active"] = self.active_box.isChecked()
        return result

    def apply(self) -> None:
        changes = self.changes()
        if "allowed_range" in changes:
            self.service.set_allowed_range(self.handle, *changes["allowed_range"])
        if "allowed_values" in changes:
            self.service.set_allowed_values(self.handle, changes["allowed_values"])
        curve = {name: changes[name] for name in ("shape", "cycle_seconds", "distribution_shape") if name in changes}
        if curve:
            self.service.set_generator(self.handle, **curve)
        if "active" in changes:
            self.service.set_metric_active(self.handle, active=changes["active"])


class EditAlertDialog(_EditDialog):
    """The runtime-editable parts of one alarm: its limits and its priority."""

    def __init__(self, service: ProviderService, handle: str, parent: QWidget | None = None) -> None:
        self.service, self.handle = service, handle
        spec = self.spec = service.list_alerts()[handle]
        super().__init__(f"Edit {spec.label}", parent)
        self._fixed("Label", spec.label)
        self._fixed("Watches", spec.source_handle)
        self._fixed("Kind", spec.kind.value)
        self._fixed("Defined priority", spec.priority.value)

        self.lower_edit = QLineEdit(_text(spec.lower_limit))
        self.upper_edit = QLineEdit(_text(spec.upper_limit))
        if spec.has_limits:
            max_lower, max_upper = service._max_limits(spec)  # noqa: SLF001 - what MaxLimits carries
            self._fixed("Max limits", f"{_text(max_lower) or 'open'} to {_text(max_upper) or 'open'}")
            self.form.addRow("Limits", _pair(self.lower_edit, self.upper_edit))

        self.priority_box = NoWheelComboBox()
        for caption, priority in PRIORITY_CAPTIONS:
            self.priority_box.addItem(caption, priority)
        self.priority_box.setCurrentIndex(self._priority_index(spec.actual_priority or spec.priority))
        self.form.addRow("Priority", self.priority_box)

    @staticmethod
    def _priority_index(priority: AlertPriority) -> int:
        return [value for _, value in PRIORITY_CAPTIONS].index(priority)

    def apply(self) -> None:
        spec = self.spec
        if spec.has_limits:
            limits = (_optional_decimal(self.lower_edit, "lower limit"), _optional_decimal(self.upper_edit, "upper limit"))
            if limits != (spec.lower_limit, spec.upper_limit):
                self.service.set_alert_limits(self.handle, *limits)
        priority = PRIORITY_CAPTIONS[self.priority_box.currentIndex()][1]
        if priority is not (spec.actual_priority or spec.priority):
            self.service.set_alert_priority(self.handle, priority)
