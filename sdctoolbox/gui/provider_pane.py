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
    QSplitter,
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
from .new_alert_dialog import NewAlertDialog
from .new_metric_dialog import NewMetricDialog
from .qt_bridge import MdibBridge
from .styling import apply_row_selection_style, muted_colour
from .widgets import WidgetBoard, from_spec

if TYPE_CHECKING:
    from ..provider_service import ProviderService

COLUMNS = ["Handle", "Label", "Kind", "Value", "Range", "Unit", "Remote control"]
COL_HANDLE, COL_LABEL, COL_KIND, COL_VALUE, COL_RANGE, COL_UNIT, COL_CONTROL = range(len(COLUMNS))

#: Label never shrinks below this, however little room is left.
MIN_LABEL_WIDTH = 120

#: Floor for every other column.
MIN_SECTION_WIDTH = 40

NO_VALUE = "\u2014"  # em dash

ALERT_COLUMNS = ["Handle", "Label", "Watches", "Raise when", "Kind", "Priority", "State"]
(
    ACOL_HANDLE,
    ACOL_LABEL,
    ACOL_SOURCE,
    ACOL_WHEN,
    ACOL_KIND,
    ACOL_PRIORITY,
    ACOL_STATE,
) = range(len(ALERT_COLUMNS))

EDITOR_TEXT = 0
EDITOR_CHOICE = 1


class ProviderPane(QWidget):
    """Everything the user does to the device they are publishing."""

    def __init__(self, service: ProviderService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        #: Guards against the table's own updates being mistaken for user clicks.
        self._refreshing = False
        #: Guards against our own column sizing being mistaken for the user dragging.
        self._adjusting_columns = False
        #: Once the user drags a column divider we stop choosing widths for them.
        self._user_sized_columns = False

        self._build_ui()

        self.bridge = MdibBridge(service.mdib, self)
        self.bridge.metrics_changed.connect(self._on_values_changed)
        self.bridge.descriptors_added.connect(lambda _: self.refresh())
        self.bridge.descriptors_deleted.connect(lambda _: self.refresh())
        self.bridge.operations_changed.connect(lambda _: self.refresh())
        self.bridge.alerts_changed.connect(lambda _: self.refresh_alerts())

        self.refresh()
        self.refresh_alerts()

    # -- construction --------------------------------------------------------------

    def _build_ui(self) -> None:
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        # The table never scrolls sideways: the columns are always made to fit the width
        # available, so nothing can hide off the right-hand edge.
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = self.table.horizontalHeader()
        # Interactive throughout, so every column can be dragged. Widths are chosen
        # automatically until the user drags one; after that only Label is adjusted, and
        # only as far as is needed to keep the total inside the viewport.
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(MIN_SECTION_WIDTH)
        header.sectionResized.connect(self._on_section_resized)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        apply_row_selection_style(self.table)

        # The same metrics, shown either as a control each or as one row each. Both are
        # always built; the View menu decides which is on top.
        self.board = WidgetBoard()
        self.board.value_requested.connect(self._on_widget_value_requested)
        self.metric_stack = QStackedWidget()
        self.metric_stack.addWidget(self.board)
        self.metric_stack.addWidget(self.table)

        self.new_button = QPushButton("New data source\u2026")
        self.new_button.clicked.connect(self._on_new)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._on_remove)
        self.remove_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)

        # -- alarms
        self.alert_table = QTableWidget(0, len(ALERT_COLUMNS))
        self.alert_table.setHorizontalHeaderLabels(ALERT_COLUMNS)
        self.alert_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.alert_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.alert_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.alert_table.verticalHeader().setVisible(False)
        self.alert_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.alert_table.horizontalHeader().setStretchLastSection(True)
        self.alert_table.itemSelectionChanged.connect(self._on_alert_selection_changed)
        apply_row_selection_style(self.alert_table)

        self.new_alert_button = QPushButton("New alarm\u2026")
        self.new_alert_button.clicked.connect(self._on_new_alert)
        self.remove_alert_button = QPushButton("Remove alarm")
        self.remove_alert_button.clicked.connect(self._on_remove_alert)
        self.toggle_alert_button = QPushButton("Raise")
        self.toggle_alert_button.clicked.connect(self._on_toggle_alert)

        alert_buttons = QHBoxLayout()
        alert_buttons.addWidget(QLabel("Alarms"))
        alert_buttons.addStretch(1)
        alert_buttons.addWidget(self.toggle_alert_button)
        alert_buttons.addWidget(self.new_alert_button)
        alert_buttons.addWidget(self.remove_alert_button)

        alert_box = QWidget()
        alert_layout = QVBoxLayout(alert_box)
        alert_layout.setContentsMargins(0, 0, 0, 0)
        alert_layout.addLayout(alert_buttons)
        alert_layout.addWidget(self.alert_table)

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

        self._views = QSplitter(Qt.Vertical)
        self._views.addWidget(self.metric_stack)
        self._views.addWidget(alert_box)
        self._views.setStretchFactor(0, 3)
        self._views.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addLayout(buttons)
        layout.addWidget(self.views, 1)
        layout.addLayout(editor)

    @property
    def views(self) -> QSplitter:
        """Metrics above, alarms below, with a divider between them."""
        return self._views

    # -- widgets or table ----------------------------------------------------------

    @property
    def use_widgets(self) -> bool:
        """Whether the metrics are shown as controls rather than as a table."""
        return self.metric_stack.currentWidget() is self.board

    def set_use_widgets(self, enabled: bool) -> None:  # noqa: FBT001 - matches the Qt signal
        """Switch between a control per metric and the table."""
        self.metric_stack.setCurrentWidget(self.board if enabled else self.table)
        if enabled:
            self._refresh_board()

    def _refresh_board(self) -> None:
        """Rebuild the controls from the current metrics."""
        specs = self.service.list_metrics()
        self.board.set_metrics(
            [
                from_spec(handle, spec, editable=True)
                for handle, spec in sorted(specs.items())
            ],
        )
        self.board.show_values({handle: self.service.get_value(handle) for handle in specs})

    def _on_widget_value_requested(self, handle: str, value: object) -> None:
        """A control asked for a value. On our own device that is simply a write."""
        try:
            self.service.set_value(handle, value)
        except ValueError as exc:
            QMessageBox.warning(self, "Outside the allowed range", str(exc))
            self._refresh_board()
        except (KeyError, TypeError) as exc:
            QMessageBox.warning(self, "Could not set value", str(exc))
            self._refresh_board()

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
                    COL_RANGE: spec.range_text(),
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
                    control.setForeground(muted_colour(self))
                    control.setToolTip(f"{spec.kind.value} metrics cannot be remote-controlled")
                self.table.setItem(row, COL_CONTROL, control)
        finally:
            self._refreshing = False

        if selected is not None:
            self.select_handle(selected)
        self._on_selection_changed()
        self._fit_columns()
        self._refresh_board()

    # -- column widths -------------------------------------------------------------

    def _fit_columns(self, changed: int | None = None) -> None:
        """Make the columns exactly fill the viewport, never more.

        Label is the elastic one: it takes whatever the other columns leave behind. When
        even its minimum does not fit, width is clawed back from the others, starting with
        the column the user just widened and then from the widest remaining ones.

        Until the user drags a divider the other columns are sized to their contents; after
        that their widths are left alone except when something has to give.
        """
        if self._adjusting_columns:
            return

        self._adjusting_columns = True
        try:
            if not self._user_sized_columns:
                self.table.resizeColumnsToContents()

            available = self.table.viewport().width()
            if available <= 0:
                return  # not laid out yet; a later resize event will do the work

            columns = range(self.table.columnCount())
            others = sum(self.table.columnWidth(c) for c in columns if c != COL_LABEL)

            leftover = available - others
            if leftover >= MIN_LABEL_WIDTH:
                self.table.setColumnWidth(COL_LABEL, leftover)
                return

            # Too narrow. Pin Label at its minimum and take the difference off the rest.
            self.table.setColumnWidth(COL_LABEL, MIN_LABEL_WIDTH)
            excess = others + MIN_LABEL_WIDTH - available

            preferred = [changed] if changed not in (None, COL_LABEL) else []
            rest = sorted(
                (c for c in columns if c != COL_LABEL and c != changed),
                key=self.table.columnWidth,
                reverse=True,
            )
            for column in preferred + rest:
                if excess <= 0:
                    break
                spare = self.table.columnWidth(column) - MIN_SECTION_WIDTH
                if spare <= 0:
                    continue
                take = min(excess, spare)
                self.table.setColumnWidth(column, self.table.columnWidth(column) - take)
                excess -= take
        finally:
            self._adjusting_columns = False

    def _on_section_resized(self, index: int, _old: int, _new: int) -> None:
        """The user dragged a divider: take our hands off the widths, then re-fit."""
        if self._adjusting_columns:
            return
        self._user_sized_columns = True
        self._fit_columns(changed=index)

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Keep the columns filling the window whenever it changes size."""
        super().resizeEvent(event)
        self._fit_columns()

    def showEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Refit on becoming visible.

        Moving the pane between the splitter and the tab widget changes how much room it
        has without necessarily producing a resize event, so the widths have to be
        recalculated when it reappears.
        """
        super().showEvent(event)
        self._fit_columns()

    def _on_values_changed(self, states_by_handle: dict) -> None:
        """Update just the value cells. Cheaper than a rebuild and keeps the selection."""
        self.board.show_values(
            {handle: self.service.get_value(handle) for handle in states_by_handle},
        )
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

    # -- alarms --------------------------------------------------------------------

    def refresh_alerts(self) -> None:
        """Rebuild the alarm table."""
        alerts = self.service.list_alerts()
        selected = self.selected_alert_handle()

        self.alert_table.setRowCount(len(alerts))
        for row, (handle, spec) in enumerate(sorted(alerts.items())):
            present = self.service.alert_present(handle)
            cells = {
                ACOL_HANDLE: handle,
                ACOL_LABEL: spec.label,
                ACOL_SOURCE: spec.source_handle,
                ACOL_WHEN: spec.limit_text() or "by hand",
                ACOL_KIND: spec.kind.value,
                ACOL_PRIORITY: spec.priority.value,
                ACOL_STATE: "PRESENT" if present else "clear",
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                if column == ACOL_STATE and not present:
                    item.setForeground(muted_colour(self))
                if column == ACOL_WHEN and not spec.has_limits:
                    item.setToolTip("No limits, so this alarm only moves when you raise or clear it")
                self.alert_table.setItem(row, column, item)

        self.alert_table.resizeColumnsToContents()
        if selected is not None:
            self.select_alert_handle(selected)
        self._on_alert_selection_changed()

    def selected_alert_handle(self) -> str | None:
        """Handle of the selected alarm row, or None."""
        model = self.alert_table.selectionModel()
        rows = model.selectedRows() if model else []
        if not rows:
            return None
        item = self.alert_table.item(rows[0].row(), ACOL_HANDLE)
        return item.text() if item else None

    def select_alert_handle(self, handle: str) -> None:
        """Restore the alarm selection, if that alarm still exists."""
        for row in range(self.alert_table.rowCount()):
            item = self.alert_table.item(row, ACOL_HANDLE)
            if item is not None and item.text() == handle:
                self.alert_table.selectRow(row)
                return

    def _on_alert_selection_changed(self) -> None:
        handle = self.selected_alert_handle()
        spec = self.service.list_alerts().get(handle) if handle else None
        self.remove_alert_button.setEnabled(spec is not None)
        # An alarm with limits is computed from its source, so raising it by hand would only
        # be undone by the next value change. Offer the button only where it means something.
        can_toggle = spec is not None and not spec.has_limits
        self.toggle_alert_button.setEnabled(can_toggle)
        if spec is None:
            self.toggle_alert_button.setText("Raise")
            self.toggle_alert_button.setToolTip("")
        elif spec.has_limits:
            self.toggle_alert_button.setText("Raise")
            self.toggle_alert_button.setToolTip(
                "This alarm follows its source metric; change the value instead",
            )
        else:
            present = self.service.alert_present(handle)
            self.toggle_alert_button.setText("Clear" if present else "Raise")
            self.toggle_alert_button.setToolTip("")

    def _on_new_alert(self) -> None:
        metrics = self.service.list_metrics()
        if not metrics:
            QMessageBox.information(
                self,
                "Nothing to watch",
                "Create a data source first. An alarm always watches one.",
            )
            return
        dialog = NewAlertDialog(metrics, self)
        if dialog.exec() != NewAlertDialog.Accepted:
            return
        spec = dialog.spec()
        if spec is None:
            return
        try:
            handle = self.service.add_alert(spec)
        except (KeyError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Could not create alarm", str(exc))
            return
        self.refresh_alerts()
        self.select_alert_handle(handle)

    def _on_remove_alert(self) -> None:
        handle = self.selected_alert_handle()
        if handle is None:
            return
        confirm = QMessageBox.question(
            self,
            "Remove alarm",
            f"Remove {handle} and its signals?",
        )
        if confirm != QMessageBox.Yes:
            return
        self.service.remove_alert(handle)
        self.refresh_alerts()

    def _on_toggle_alert(self) -> None:
        handle = self.selected_alert_handle()
        if handle is None:
            return
        try:
            self.service.set_alert_presence(handle, not self.service.alert_present(handle))
        except KeyError as exc:
            QMessageBox.warning(self, "Could not change the alarm", str(exc))
        self.refresh_alerts()

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
            self.value_edit.setPlaceholderText(spec.range_text() if spec.has_range else "")
        if spec.has_range:
            self.editor_label.setText(f"{spec.label} ({spec.range_text()}):")
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
        except ValueError as exc:
            QMessageBox.warning(self, "Outside the allowed range", str(exc))
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
