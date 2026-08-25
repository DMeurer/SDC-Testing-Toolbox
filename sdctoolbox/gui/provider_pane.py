"""The "My Device" tab: the data sources we publish ourselves.

Shows one row per data source with its live value, lets the user create and delete them,
edit their values, and toggle whether other devices may write to them.

Values change from two directions: the user editing them here, and a remote consumer writing
over the network. Both arrive through the MdibBridge, so the table looks the same either way.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sdc11073.xml_types import pm_types

from ..model import MetricKind
from .new_metric_dialog import NewMetricDialog
from .qt_bridge import MdibBridge

if TYPE_CHECKING:
    from ..provider_service import ProviderService

COLUMNS = ["Handle", "Label", "Kind", "Value", "Unit", "Remote control"]
COL_HANDLE, COL_LABEL, COL_KIND, COL_VALUE, COL_UNIT, COL_CONTROL = range(len(COLUMNS))

NO_VALUE = "\u2014"  # em dash

EDITOR_TEXT = 0
EDITOR_CHOICE = 1


class ProviderPane(QWidget):
    """Everything the user does to the device they are publishing."""

    def __init__(self, service: ProviderService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        #: Guards against the table's own updates being mistaken for user clicks.
        self._refreshing = False

        self._build_ui()

        self.bridge = MdibBridge(service.mdib, self)
        self.bridge.metrics_changed.connect(self._on_values_changed)
        self.bridge.descriptors_added.connect(lambda _: self.refresh())
        self.bridge.descriptors_deleted.connect(lambda _: self.refresh())
        self.bridge.operations_changed.connect(lambda _: self.refresh())

        self.refresh()

    # -- construction --------------------------------------------------------------

    def _build_ui(self) -> None:
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_HANDLE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_LABEL, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_KIND, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_VALUE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_UNIT, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_CONTROL, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        self.new_button = QPushButton("New data source\u2026")
        self.new_button.clicked.connect(self._on_new)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._on_remove)
        self.remove_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)

        self.editor_label = QLabel("Select a data source to change its value")
        self.value_edit = QLineEdit()
        self.value_edit.returnPressed.connect(self._on_apply)
        self.choice_box = QComboBox()
        self.editor_stack = QStackedWidget()
        self.editor_stack.addWidget(self.value_edit)
        self.editor_stack.addWidget(self.choice_box)
        self.apply_button = QPushButton("Set value")
        self.apply_button.clicked.connect(self._on_apply)

        editor = QHBoxLayout()
        editor.addWidget(self.editor_label)
        editor.addWidget(self.editor_stack, 1)
        editor.addWidget(self.apply_button)
        self._set_editor_enabled(enabled=False)

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.table, 1)
        layout.addLayout(editor)

    # -- table -------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the table from the MDIB."""
        specs = self.service.list_metrics()
        selected = self.selected_handle()

        self._refreshing = True
        try:
            self.table.setRowCount(len(specs))
            for row, (handle, spec) in enumerate(sorted(specs.items())):
                value = self.service.get_value(handle)
                cells = {
                    COL_HANDLE: handle,
                    COL_LABEL: spec.label,
                    COL_KIND: spec.kind.value,
                    COL_VALUE: NO_VALUE if value is None else str(value),
                    COL_UNIT: spec.unit_label,
                }
                for column, text in cells.items():
                    item = QTableWidgetItem(text)
                    if column == COL_VALUE:
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(row, column, item)

                control = QTableWidgetItem()
                if spec.kind.controllable:
                    enabled = self._control_enabled(handle)
                    control.setFlags(control.flags() | Qt.ItemIsUserCheckable)
                    control.setCheckState(Qt.Checked if enabled else Qt.Unchecked)
                    control.setToolTip(
                        "Other devices may write this value"
                        if enabled
                        else "Writes from other devices are refused",
                    )
                else:
                    control.setFlags(Qt.ItemIsEnabled)
                    control.setText("n/a")
                    control.setToolTip(f"{spec.kind.value} metrics cannot be remote-controlled")
                self.table.setItem(row, COL_CONTROL, control)
        finally:
            self._refreshing = False

        if selected is not None:
            self.select_handle(selected)
        self._on_selection_changed()

    def _on_values_changed(self, states_by_handle: dict) -> None:
        """Update just the value cells. Cheaper than a rebuild and keeps the selection."""
        self._refreshing = True
        try:
            for row in range(self.table.rowCount()):
                handle_item = self.table.item(row, COL_HANDLE)
                if handle_item is None or handle_item.text() not in states_by_handle:
                    continue
                value = self.service.get_value(handle_item.text())
                item = self.table.item(row, COL_VALUE)
                if item is not None:
                    item.setText(NO_VALUE if value is None else str(value))
        finally:
            self._refreshing = False

    # -- selection -----------------------------------------------------------------

    def selected_handle(self) -> str | None:
        """Handle of the selected row, or None."""
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), COL_HANDLE)
        return item.text() if item else None

    def select_handle(self, handle: str) -> None:
        """Restore the selection to a given handle, if it still exists."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_HANDLE)
            if item is not None and item.text() == handle:
                self.table.selectRow(row)
                return

    def _on_selection_changed(self) -> None:
        handle = self.selected_handle()
        self.remove_button.setEnabled(handle is not None)
        if handle is None:
            self.editor_label.setText("Select a data source to change its value")
            self._set_editor_enabled(enabled=False)
            return

        spec = self.service.list_metrics().get(handle)
        if spec is None:
            self._set_editor_enabled(enabled=False)
            return

        self.editor_label.setText(f"{spec.label}:")
        current = self.service.get_value(handle)
        if spec.kind is MetricKind.CHOICE:
            self.editor_stack.setCurrentIndex(EDITOR_CHOICE)
            self.choice_box.clear()
            self.choice_box.addItems(list(spec.allowed_values))
            if current is not None:
                index = self.choice_box.findText(str(current))
                if index >= 0:
                    self.choice_box.setCurrentIndex(index)
        else:
            self.editor_stack.setCurrentIndex(EDITOR_TEXT)
            self.value_edit.setText("" if current is None else str(current))
        self._set_editor_enabled(enabled=True)

    def _set_editor_enabled(self, *, enabled: bool) -> None:
        self.editor_stack.setEnabled(enabled)
        self.apply_button.setEnabled(enabled)

    # -- actions -------------------------------------------------------------------

    def _on_new(self) -> None:
        dialog = NewMetricDialog(self)
        if dialog.exec() != NewMetricDialog.Accepted:
            return
        spec = dialog.spec()
        if spec is None:
            return
        try:
            handle = self.service.add_metric(spec)
        except (ValueError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, "Could not create data source", str(exc))
            return
        self.refresh()
        self.select_handle(handle)

    def _on_remove(self) -> None:
        handle = self.selected_handle()
        if handle is None:
            return
        confirm = QMessageBox.question(
            self,
            "Remove data source",
            f"Remove {handle}?\n\nConnected consumers will see it disappear.",
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.service.remove_metric(handle)
        except KeyError as exc:
            QMessageBox.warning(self, "Could not remove", str(exc))
        self.refresh()

    def _on_apply(self) -> None:
        handle = self.selected_handle()
        if handle is None:
            return
        spec = self.service.list_metrics().get(handle)
        if spec is None:
            return

        if spec.kind is MetricKind.CHOICE:
            value: Decimal | str = self.choice_box.currentText()
        elif spec.kind is MetricKind.NUMBER:
            raw = self.value_edit.text().strip()
            try:
                value = Decimal(raw)
            except InvalidOperation:
                QMessageBox.warning(self, "Not a number", f"{raw!r} is not a valid number.")
                return
        else:
            value = self.value_edit.text()

        try:
            self.service.set_value(handle, value)
        except (KeyError, TypeError) as exc:
            QMessageBox.warning(self, "Could not set value", str(exc))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Handle the remote-control checkbox being clicked."""
        if self._refreshing or item.column() != COL_CONTROL:
            return
        handle_item = self.table.item(item.row(), COL_HANDLE)
        if handle_item is None:
            return
        handle = handle_item.text()
        wants_control = item.checkState() == Qt.Checked
        try:
            if wants_control:
                self.service.enable_control(handle)
            else:
                self.service.disable_control(handle)
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Could not change remote control", str(exc))
            self.refresh()

    # -- helpers -------------------------------------------------------------------

    def _control_enabled(self, handle: str) -> bool:
        """Whether writes from other devices are currently accepted for this metric."""
        operation_handle = self.service.operation_handle_for(handle)
        if operation_handle is None:
            return False
        entity = self.service.mdib.entities.by_handle(operation_handle)
        if entity is None:
            return False
        return getattr(entity.state, "OperatingMode", None) == pm_types.OperatingMode.ENABLED
