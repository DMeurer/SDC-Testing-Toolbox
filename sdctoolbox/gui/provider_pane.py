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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sdc11073.xml_types import pm_types

from ..model import MetricKind
from .context_dialog import ContextDialog
from .new_alert_dialog import NewAlertDialog
from .new_metric_dialog import NewMetricDialog
from .no_wheel import NoWheelComboBox
from .qt_bridge import MdibBridge
from .styling import apply_row_selection_style, mute, muted_colour
from .table_columns import TableColumns
from .widgets import WidgetBoard, from_spec

if TYPE_CHECKING:
    from ..provider_service import ProviderService

COLUMNS = ["Handle", "Label", "Kind", "Value", "Range", "Unit", "Remote control"]
COL_HANDLE, COL_LABEL, COL_KIND, COL_VALUE, COL_RANGE, COL_UNIT, COL_CONTROL = range(len(COLUMNS))

NO_VALUE = "\u2014"  # em dash

ALERT_COLUMNS = ["Handle", "Label", "Watches", "Raise when", "Kind", "Priority", "State", "Signals"]
(
    ACOL_HANDLE,
    ACOL_LABEL,
    ACOL_SOURCE,
    ACOL_WHEN,
    ACOL_KIND,
    ACOL_PRIORITY,
    ACOL_STATE,
    ACOL_SIGNALS,
) = range(len(ALERT_COLUMNS))

EDITOR_TEXT = 0
EDITOR_CHOICE = 1


class ProviderPane(QWidget):
    """Everything the user does to the device they are publishing."""

    def __init__(self, service: ProviderService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        # Guards against the table's own updates being mistaken for user clicks.
        self._refreshing = False

        self._build_ui()

        self.bridge = MdibBridge(service.mdib, self)
        self.bridge.metrics_changed.connect(self._on_values_changed)
        self.bridge.descriptors_added.connect(lambda _: self.refresh())
        self.bridge.descriptors_deleted.connect(lambda _: self.refresh())
        self.bridge.operations_changed.connect(lambda _: self.refresh())
        self.bridge.alerts_changed.connect(lambda _: self.refresh_alerts())
        self.bridge.contexts_changed.connect(lambda _: self.refresh_contexts())
        # Waveforms travel as a WaveformStream, not an EpisodicMetricReport, so they
        # never reach metrics_changed.
        self.bridge.waveforms_changed.connect(self._on_waveforms_changed)

        self.refresh()
        self.refresh_alerts()
        self.refresh_actions()
        self.refresh_contexts()

    # -- construction --------------------------------------------------------------

    def _build_ui(self) -> None:
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self._columns = TableColumns(self.table, flexible=COL_LABEL)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        apply_row_selection_style(self.table)

        # The same metrics, shown either as a control each or as one row each. Both are
        # always built; the View menu decides which is on top.
        self.board = WidgetBoard(deletable=True)
        self.board.value_requested.connect(self._on_widget_value_requested)
        self.board.delete_requested.connect(self._on_card_delete_requested)
        self.metric_stack = QStackedWidget()
        self.metric_stack.addWidget(self.board)
        self.metric_stack.addWidget(self.table)

        self.new_button = QPushButton("New data source\u2026")
        self.new_button.clicked.connect(self._on_new)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._on_remove)
        self.remove_button.setEnabled(False)
        self.context_button = QPushButton("Patient and location\u2026")
        self.context_button.clicked.connect(self._on_edit_contexts)
        self.context_label = QLabel("")
        self.context_label.setWordWrap(True)
        self.context_label.setTextFormat(Qt.PlainText)
        self.context_label.setTextInteractionFlags(Qt.NoTextInteraction)
        self.context_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        mute(self.context_label)

        buttons = QHBoxLayout()
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.remove_button)
        buttons.addWidget(self.context_button)
        buttons.addStretch(1)

        # Actions are the things the device *does*. They get their own row rather than a
        # place in the metric table, because they are not values and behave nothing like
        # one: there is nothing to read back, only something to invoke.
        self.actions_row = QHBoxLayout()
        self.actions_label = QLabel("Actions")
        self.actions_row.addWidget(self.actions_label)
        self.actions_row.addStretch(1)
        self.action_buttons: dict[str, QPushButton] = {}
        self.actions_widget = QWidget()
        self.actions_widget.setLayout(self.actions_row)

        # -- alarms
        self.alert_table = QTableWidget(0, len(ALERT_COLUMNS))
        self.alert_table.setHorizontalHeaderLabels(ALERT_COLUMNS)
        self.alert_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.alert_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.alert_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.alert_table.verticalHeader().setVisible(False)
        self._alert_columns = TableColumns(self.alert_table, flexible=ACOL_LABEL)
        self.alert_table.itemSelectionChanged.connect(self._on_alert_selection_changed)
        apply_row_selection_style(self.alert_table)

        self.new_alert_button = QPushButton("New alarm\u2026")
        self.new_alert_button.clicked.connect(self._on_new_alert)
        self.remove_alert_button = QPushButton("Remove alarm")
        self.remove_alert_button.clicked.connect(self._on_remove_alert)
        self.toggle_alert_button = QPushButton("Raise")
        self.toggle_alert_button.clicked.connect(self._on_toggle_alert)
        self.acknowledge_button = QPushButton("Acknowledge")
        self.acknowledge_button.clicked.connect(self._on_acknowledge)
        self.stop_latched_button = QPushButton("Stop latched")
        self.stop_latched_button.clicked.connect(self._on_stop_latched)
        self.delegate_button = QPushButton("Delegate")
        self.delegate_button.clicked.connect(self._on_delegate)

        alert_buttons = QHBoxLayout()
        alert_buttons.addWidget(QLabel("Alarms"))
        alert_buttons.addStretch(1)
        alert_buttons.addWidget(self.toggle_alert_button)
        alert_buttons.addWidget(self.acknowledge_button)
        alert_buttons.addWidget(self.stop_latched_button)
        alert_buttons.addWidget(self.delegate_button)
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
        self.choice_box = NoWheelComboBox()
        self.editor_stack = QStackedWidget()
        self.editor_stack.addWidget(self.value_edit)
        self.editor_stack.addWidget(self.choice_box)
        self.apply_button = QPushButton("Set value")
        self.apply_button.clicked.connect(self._on_apply)

        editor = QHBoxLayout()
        editor.setContentsMargins(0, 0, 0, 0)
        editor.addWidget(self.editor_label)
        editor.addWidget(self.editor_stack, 1)
        editor.addWidget(self.apply_button)
        # A widget rather than a bare layout, because the whole row has to disappear in
        # widget mode and a layout cannot be hidden.
        self.editor_widget = QWidget()
        self.editor_widget.setLayout(editor)
        self._set_editor_enabled(enabled=False)

        self._views = QSplitter(Qt.Vertical)
        self._views.addWidget(self.metric_stack)
        self._views.addWidget(alert_box)
        self._views.setStretchFactor(0, 3)
        self._views.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        # TitledPanel already pads both panels. Without this the style's own 9px lands on
        # top of that here but not in the consumer pane, which sets the same zero - so the
        # two panels disagreed by 9px at the top and the bottom.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
        layout.addWidget(self.context_label)
        layout.addWidget(self.views, 1)
        layout.addWidget(self.actions_widget)
        layout.addWidget(self.editor_widget)

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
        # Both of these work off the table's selection, and the board has no selection.
        # In widget mode each card carries its own control and its own bin, so an editor
        # and a Remove button down here would only ever be disabled and confusing.
        self.remove_button.setVisible(not enabled)
        self.editor_widget.setVisible(not enabled)
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
        self.board.show_values({handle: self._displayable(handle, spec) for handle, spec in specs.items()})

    def _on_card_delete_requested(self, handle: str) -> None:
        """A card's bin was clicked."""
        self._confirm_and_remove(handle)

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
                cells = {
                    COL_HANDLE: handle,
                    COL_LABEL: spec.label,
                    COL_KIND: spec.kind.value,
                    COL_VALUE: self._value_text(handle, spec),
                    COL_RANGE: spec.domain_text() or spec.range_text(),
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
        self._columns.refit()
        self._refresh_board()

    def _displayable(self, handle: str, spec) -> object:  # noqa: ANN001 - a MetricSpec
        """What a card should be given: a block of samples, or a single value."""
        if spec.is_sample_array:
            return self.service.get_samples(handle)
        return self.service.get_value(handle)

    def _value_text(self, handle: str, spec) -> str:  # noqa: ANN001 - a MetricSpec
        """What the table's Value cell shows.

        A sample array has no single value to print, and printing three hundred of them
        would be useless, so the cell says how many arrived instead. The card draws them.
        """
        if spec.is_sample_array:
            count = len(self.service.get_samples(handle))
            return f"{count} sample(s)" if count else NO_VALUE
        value = self.service.get_value(handle)
        return NO_VALUE if value is None else str(value)

    def _refresh_sample_cells(self, handles) -> None:  # noqa: ANN001 - any iterable
        """Update the count in the Value column for the metrics named."""
        specs = self.service.list_metrics()
        self._refreshing = True
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, COL_HANDLE)
                if item is None or item.text() not in handles:
                    continue
                spec = specs.get(item.text())
                cell = self.table.item(row, COL_VALUE)
                if spec is not None and cell is not None:
                    cell.setText(self._value_text(item.text(), spec))
        finally:
            self._refreshing = False

    def _on_waveforms_changed(self, blocks_by_handle: dict) -> None:
        """A block of samples arrived. Only the cards need it; the table shows a summary.

        The blocks come with the signal rather than being read back out of the mdib, which
        by the time Qt delivers this may already hold a later one - see
        MdibBridge._on_waveforms.

        Creating a waveform descriptor also lands here, with a state that had no MetricValue
        yet: a descriptor transaction puts the new sample-array state in the same bucket a
        WaveformStream comes from. Such an event carries an empty block and appends nothing.
        """
        self.board.append_samples(blocks_by_handle)
        self._refresh_sample_cells(blocks_by_handle)

    def _on_values_changed(self, states_by_handle: dict) -> None:
        """Update just the value cells. Cheaper than a rebuild and keeps the selection."""
        specs = self.service.list_metrics()
        self.board.show_values(
            {
                handle: self._displayable(handle, specs[handle])
                for handle in states_by_handle
                if handle in specs
            },
        )
        self._refreshing = True
        try:
            for row in range(self.table.rowCount()):
                handle_item = self.table.item(row, COL_HANDLE)
                if handle_item is None or handle_item.text() not in states_by_handle:
                    continue
                spec = specs.get(handle_item.text())
                item = self.table.item(row, COL_VALUE)
                if spec is not None and item is not None:
                    item.setText(self._value_text(handle_item.text(), spec))
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
            signals = self.service.signal_states(handle)
            cells = {
                ACOL_HANDLE: handle,
                ACOL_LABEL: spec.label,
                ACOL_SOURCE: spec.source_handle,
                ACOL_WHEN: spec.limit_text() or "by hand",
                ACOL_KIND: spec.kind.value,
                ACOL_PRIORITY: spec.priority.value,
                ACOL_STATE: "PRESENT" if present else "clear",
                ACOL_SIGNALS: "  ".join(signal.summary() for signal in signals),
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                if column == ACOL_STATE and not present:
                    item.setForeground(muted_colour(self))
                if column == ACOL_WHEN and not spec.has_limits:
                    item.setToolTip("No limits, so this alarm only moves when you raise or clear it")
                if column == ACOL_SIGNALS:
                    item.setToolTip(
                        "How each signal is announcing the condition. Ack means it has been\n"
                        "acknowledged; the condition itself is still present. ->Rem means it\n"
                        "has been delegated to another device. Latch means a latching signal\n"
                        "continues to announce a cleared condition until stopped.",
                    )
                    if not present:
                        item.setForeground(muted_colour(self))
                self.alert_table.setItem(row, column, item)

        self._alert_columns.refit()
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
        self._update_signal_buttons(handle, spec)

    def _update_signal_buttons(self, handle: str | None, spec: object | None) -> None:
        """Acknowledge and Delegate follow the selected alarm's signals."""
        signals = self.service.signal_states(handle) if handle and spec is not None else []
        present = bool(handle) and spec is not None and self.service.alert_present(handle)

        # Acknowledging a condition that is not raised is meaningless, and the provider
        # refuses it, so do not offer it either.
        unacknowledged = [signal for signal in signals if not signal.acknowledged]
        self.acknowledge_button.setEnabled(present and bool(unacknowledged))
        if spec is None:
            self.acknowledge_button.setToolTip("")
        elif not present:
            self.acknowledge_button.setToolTip("Nothing to acknowledge: this alarm is not raised")
        elif not unacknowledged:
            self.acknowledge_button.setToolTip("Every signal is already acknowledged")
        else:
            self.acknowledge_button.setToolTip(
                "Mark the signals as seen. The condition stays present - acknowledging\n"
                "changes how an alarm is announced, not whether it is true.",
            )

        latched = [signal for signal in signals if signal.latched]
        self.stop_latched_button.setEnabled(bool(latched))
        self.stop_latched_button.setToolTip(
            "Stop signals that are latching after the condition cleared" if latched else "No signals are latching"
        )

        delegable = [signal for signal in signals if signal.delegable]
        self.delegate_button.setEnabled(bool(delegable))
        anywhere_delegated = any(signal.delegated for signal in delegable)
        self.delegate_button.setText("Take back" if anywhere_delegated else "Delegate")
        if spec is None:
            self.delegate_button.setToolTip("")
        elif not delegable:
            self.delegate_button.setToolTip(
                "This alarm's signals are not delegable: their descriptors do not set\n"
                "SignalDelegationSupported",
            )
        else:
            self.delegate_button.setToolTip(
                "Record that another device announces these signals, by moving their\n"
                "Location from Loc to Rem.",
            )

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

    def _on_acknowledge(self) -> None:
        handle = self.selected_alert_handle()
        if handle is None:
            return
        try:
            self.service.acknowledge_alert(handle)
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Could not acknowledge", str(exc))
        self.refresh_alerts()

    def _on_stop_latched(self) -> None:
        handle = self.selected_alert_handle()
        if handle is None:
            return
        try:
            self.service.stop_latched_signals(handle)
        except KeyError as exc:
            QMessageBox.warning(self, "Could not stop latched signals", str(exc))
        self.refresh_alerts()

    def _on_delegate(self) -> None:
        """Hand every delegable signal of the selected alarm over, or take them all back."""
        handle = self.selected_alert_handle()
        if handle is None:
            return
        delegable = [signal for signal in self.service.signal_states(handle) if signal.delegable]
        if not delegable:
            return
        # One button for the whole condition, so the target is whatever the majority is not.
        delegate = not any(signal.delegated for signal in delegable)
        try:
            for signal in delegable:
                self.service.set_signal_delegated(signal.handle, delegated=delegate)
        except (KeyError, ValueError) as exc:
            QMessageBox.warning(self, "Could not delegate", str(exc))
        self.refresh_alerts()

    # -- contexts ------------------------------------------------------------------

    def refresh_actions(self) -> None:
        """Rebuild the action buttons from what the device publishes."""
        specs = self.service.list_actions()
        for handle, button in list(self.action_buttons.items()):
            if handle not in specs:
                self.actions_row.removeWidget(button)
                button.deleteLater()
                del self.action_buttons[handle]

        for handle, spec in sorted(specs.items()):
            button = self.action_buttons.get(handle)
            if button is None:
                button = QPushButton(spec.label)
                button.clicked.connect(lambda _=False, h=handle: self._on_run_action(h))
                # Before the stretch, so buttons stay left and the row does not jump about
                # as actions come and go.
                self.actions_row.insertWidget(self.actions_row.count() - 1, button)
                self.action_buttons[handle] = button
            button.setToolTip(f"{spec.note}\n{handle}: {spec.summary()}".strip())

        # A device with no actions should not show an empty toolbar saying "Actions".
        self.actions_widget.setVisible(bool(specs))

    def _on_run_action(self, handle: str) -> None:
        try:
            self.service.run_action(handle)
        except (KeyError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Could not run the action", str(exc))
        self.refresh()

    def refresh_contexts(self) -> None:
        """Show who and where the device currently says it is."""
        location = self.service.get_location()
        patient = self.service.get_patient()
        parts = []
        if not patient.is_empty():
            parts.append(patient.summary())
        if not location.is_empty():
            parts.append(location.summary())
        self.context_label.setText("   \u00b7   ".join(parts))

    def _on_edit_contexts(self) -> None:
        dialog = ContextDialog(self.service.get_location(), self.service.get_patient(), self)
        if dialog.exec() != ContextDialog.Accepted:
            return
        location = dialog.location()
        patient = dialog.patient()
        try:
            if location is not None:
                self.service.set_location(location)
            if patient is not None:
                self.service.set_patient(patient)
        except (RuntimeError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "Could not change the contexts", str(exc))
        self.refresh_contexts()

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
        self._confirm_and_remove(handle)

    def _confirm_and_remove(self, handle: str) -> None:
        """Ask, then delete. Shared by the table's button and each card's bin."""
        spec = self.service.list_metrics().get(handle)
        name = f"{spec.label} ({handle})" if spec and spec.label else handle
        watching = [
            alert_handle
            for alert_handle, alert in self.service.list_alerts().items()
            if alert.source_handle == handle
        ]

        question = f"Remove {name}?\n\nConnected consumers will see it disappear."
        if watching:
            # Removing the metric would leave these pointing at nothing, so say so before
            # rather than after.
            question += f"\n\nThese alarms watch it and will be removed too:\n  {', '.join(watching)}"

        if QMessageBox.question(self, "Remove data source", question) != QMessageBox.Yes:
            return

        try:
            for alert_handle in watching:
                self.service.remove_alert(alert_handle)
            self.service.remove_metric(handle)
        except KeyError as exc:
            QMessageBox.warning(self, "Could not remove", str(exc))
        self.refresh()
        self.refresh_alerts()

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
