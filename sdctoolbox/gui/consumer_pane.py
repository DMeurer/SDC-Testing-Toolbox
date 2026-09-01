"""The "Network" panel: other people's devices.

Nothing here may assume the peer was built by this tool. Foreign providers routinely omit
units, concept descriptions and values, model their tree to a different depth, and expose
node types we have never heard of. All of that has to be shown rather than discarded, so the
panel doubles as a plain MDIB browser.

Discovery, connecting and remote writes all block for seconds at a time, so they run through
AsyncCall rather than on the GUI thread.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sdc11073.xml_types import msg_types

from ..consumer_service import ConsumerService
from ..model import MetricKind
from .async_call import AsyncCall
from .no_wheel import NoWheelComboBox
from .qt_bridge import MdibBridge
from .styling import apply_row_selection_style, mute, muted_colour
from .table_columns import TableColumns
from .widgets import WidgetBoard, from_remote_metric

if TYPE_CHECKING:
    from ..consumer_service import DiscoveredDevice, RemoteDevice

COLUMNS = ["Handle", "Label", "Kind", "Value", "Range", "Unit", "Writable"]
COL_HANDLE, COL_LABEL, COL_KIND, COL_VALUE, COL_RANGE, COL_UNIT, COL_WRITABLE = range(len(COLUMNS))

# Floors that stop the panel from dictating a width the splitter cannot move.
ALERT_COLUMNS = ["Handle", "Label", "Watches", "Limits", "Kind", "Priority", "Signals", "State"]
(
    ACOL_HANDLE,
    ACOL_LABEL,
    ACOL_SOURCE,
    ACOL_LIMITS,
    ACOL_KIND,
    ACOL_PRIORITY,
    ACOL_SIGNALS,
    ACOL_STATE,
) = range(len(ALERT_COLUMNS))

MIN_CHILD_WIDTH = 200
MIN_PANEL_WIDTH = 240

NO_VALUE = "\u2014"

EDITOR_TEXT = 0
EDITOR_CHOICE = 1


def _displayable(metric) -> object:  # noqa: ANN001 - a RemoteMetric
    """What a card should be given: a block of samples, or a single value."""
    return list(metric.samples) if metric.is_sample_array else metric.value


def _value_text(metric) -> str:  # noqa: ANN001 - a RemoteMetric
    """What the table's Value cell shows."""
    if metric.is_sample_array:
        return f"{len(metric.samples)} sample(s)" if metric.samples else NO_VALUE
    return NO_VALUE if metric.value is None else str(metric.value)

FINISHED_STATES = (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD)


class ConsumerPane(QWidget):
    """Find SDC providers, inspect what they publish, and drive the parts that allow it."""

    def __init__(self, ip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = ConsumerService(ip=ip)
        self.service.start()

        self.devices: list[DiscoveredDevice] = []
        self.remote: RemoteDevice | None = None
        self.bridge: MdibBridge | None = None

        self._build_ui()
        self._wire_async()
        self._update_buttons()

    # -- construction --------------------------------------------------------------

    def _build_ui(self) -> None:
        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self._on_scan)
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._on_connect)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._on_disconnect)

        self.status_label = QLabel("Not connected")
        self.status_label.setWordWrap(True)
        self.context_label = QLabel("")
        self.context_label.setWordWrap(True)
        self.context_label.setTextFormat(Qt.PlainText)
        self.context_label.setTextInteractionFlags(Qt.NoTextInteraction)
        self.context_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        mute(self.context_label)

        top = QHBoxLayout()
        top.addWidget(self.scan_button)
        top.addWidget(self.connect_button)
        top.addWidget(self.disconnect_button)
        top.addStretch(1)

        # What the peer says it can be told to *do*, as opposed to the values it holds.
        # A device may be able to home its axes without publishing a metric for it.
        self.actions_row = QHBoxLayout()
        self.actions_row.addWidget(QLabel("Actions"))
        self.actions_row.addStretch(1)
        self.action_buttons: dict[str, QPushButton] = {}
        self.actions_widget = QWidget()
        self.actions_widget.setLayout(self.actions_row)
        self.actions_widget.setVisible(False)

        self.device_list = QListWidget()
        self.device_list.setMaximumHeight(90)
        self.device_list.itemSelectionChanged.connect(self._update_buttons)
        self.device_list.itemDoubleClicked.connect(lambda _: self._on_connect())

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Element", "Handle"])
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        apply_row_selection_style(self.tree)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self._columns = TableColumns(self.table, flexible=COL_LABEL)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        apply_row_selection_style(self.table)

        self.alert_table = QTableWidget(0, len(ALERT_COLUMNS))
        self.alert_table.setHorizontalHeaderLabels(ALERT_COLUMNS)
        self.alert_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.alert_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.alert_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.alert_table.verticalHeader().setVisible(False)
        self._alert_columns = TableColumns(self.alert_table, flexible=ACOL_LABEL)
        apply_row_selection_style(self.alert_table)

        alert_box = QWidget()
        alert_layout = QVBoxLayout(alert_box)
        alert_layout.setContentsMargins(0, 0, 0, 0)
        alert_layout.addWidget(QLabel("Alarms"))
        alert_layout.addWidget(self.alert_table)

        self.board = WidgetBoard()
        self.board.value_requested.connect(self._on_widget_value_requested)
        self.metric_stack = QStackedWidget()
        self.metric_stack.addWidget(self.board)
        self.metric_stack.addWidget(self.table)

        self.views = QSplitter(Qt.Vertical)
        self.views.addWidget(self.tree)
        self.views.addWidget(self.metric_stack)
        self.views.addWidget(alert_box)
        self.views.setStretchFactor(0, 1)
        self.views.setStretchFactor(1, 3)
        self.views.setStretchFactor(2, 2)

        self.editor_label = QLabel("Connect to a device to control it")
        self.editor_label.setWordWrap(True)
        self.value_edit = QLineEdit()
        self.value_edit.returnPressed.connect(self._on_apply)
        self.choice_box = NoWheelComboBox()
        self.editor_stack = QStackedWidget()
        self.editor_stack.addWidget(self.value_edit)
        self.editor_stack.addWidget(self.choice_box)
        self.apply_button = QPushButton("Set on device")
        self.apply_button.clicked.connect(self._on_apply)
        self.invocation_label = QLabel("")
        self.invocation_label.setWordWrap(True)

        # The editor drives whatever is selected in the table, so it goes away in widget
        # mode where there is no selection. A widget rather than a bare layout, because a
        # layout cannot be hidden.
        editor_controls = QHBoxLayout()
        editor_controls.setContentsMargins(0, 0, 0, 0)
        editor_controls.addWidget(self.editor_label)
        editor_controls.addWidget(self.editor_stack, 1)
        editor_controls.addWidget(self.apply_button)
        self.editor_widget = QWidget()
        self.editor_widget.setLayout(editor_controls)

        # The result of a write stays, though. In widget mode a card is what sends it, and
        # whether the peer accepted or refused is the single most useful thing this panel
        # reports - hiding it with the editor would take the answer away with the question.
        editor = QHBoxLayout()
        editor.addWidget(self.editor_widget, 1)
        editor.addWidget(self.invocation_label)

        # Keep the panel from demanding more width than it needs. The two tables are
        # left alone: TableColumns sets their minimum from their own columns, and
        # overriding it here would let the splitter squeeze them below what they can draw.
        for widget in (self.tree, self.board, self.device_list, self.editor_stack):
            widget.setMinimumWidth(MIN_CHILD_WIDTH)
        self.setMinimumWidth(MIN_PANEL_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.device_list)
        layout.addWidget(self.status_label)
        layout.addWidget(self.context_label)
        layout.addWidget(self.views, 1)
        layout.addWidget(self.actions_widget)
        layout.addLayout(editor)

    def _wire_async(self) -> None:
        self._scan_call = AsyncCall(self)
        self._scan_call.finished.connect(self._on_scan_finished)
        self._scan_call.failed.connect(lambda msg: self._set_status(f"Scan failed: {msg}"))
        self._scan_call.busy_changed.connect(self._on_busy_changed)

        self._connect_call = AsyncCall(self)
        self._connect_call.finished.connect(self._on_connected)
        self._connect_call.failed.connect(lambda msg: self._set_status(f"Could not connect: {msg}"))
        self._connect_call.busy_changed.connect(self._on_busy_changed)

        self._set_call = AsyncCall(self)
        self._set_call.finished.connect(self._on_set_finished)
        self._set_call.failed.connect(self._on_set_failed)
        self._set_call.busy_changed.connect(self._on_busy_changed)

    # -- discovery -----------------------------------------------------------------

    def _on_scan(self) -> None:
        self._set_status("Searching\u2026")
        self.device_list.clear()
        self.devices = []
        self._scan_call.start(self.service.scan, timeout=8.0, expected=99)

    def _on_scan_finished(self, devices: list) -> None:
        self.devices = devices
        self.device_list.clear()
        for device in devices:
            item = QListWidgetItem(device.epr)
            location = device.location_scope
            if location:
                item.setToolTip(location)
            self.device_list.addItem(item)
        if devices:
            self.device_list.setCurrentRow(0)
            self._set_status(f"Found {len(devices)} device(s)")
        else:
            self._set_status("No devices found")
        self._update_buttons()

    def _selected_device(self) -> DiscoveredDevice | None:
        row = self.device_list.currentRow()
        if 0 <= row < len(self.devices):
            return self.devices[row]
        return None

    # -- connection ----------------------------------------------------------------

    def _on_connect(self) -> None:
        device = self._selected_device()
        if device is None:
            return
        self._teardown_remote()
        self._set_status(f"Connecting to {device.epr}\u2026")
        self._connect_call.start(self.service.connect, device)

    def _on_connected(self, remote: RemoteDevice) -> None:
        self.remote = remote
        self.bridge = MdibBridge(remote.mdib, self)
        self.bridge.metrics_changed.connect(lambda _: self.refresh_values())
        self.bridge.descriptors_added.connect(lambda _: self.refresh())
        self.bridge.descriptors_deleted.connect(lambda _: self.refresh())
        self.bridge.descriptors_updated.connect(lambda _: self.refresh())
        self.bridge.operations_changed.connect(lambda _: self.refresh())
        self.bridge.alerts_changed.connect(lambda _: self._rebuild_alerts())
        self.bridge.contexts_changed.connect(lambda _: self._refresh_contexts())
        # A peer's waveforms arrive as a WaveformStream, on their own observable.
        self.bridge.waveforms_changed.connect(self._on_waveforms_changed)
        self.bridge.peer_restarted.connect(self._on_peer_restarted)
        self._set_status(f"Connected to {remote.epr}")
        self.refresh()
        self._update_buttons()

    def _on_disconnect(self) -> None:
        self._teardown_remote()
        self._set_status("Not connected")
        self.refresh()
        self._update_buttons()

    def _teardown_remote(self) -> None:
        if self.bridge is not None:
            self.bridge.deleteLater()
            self.bridge = None
        if self.remote is not None:
            self.remote.close()
            self.remote = None

    def _on_peer_restarted(self) -> None:
        """The far end restarted, so everything we cached about it is worthless."""
        self._set_status("The device restarted. Reconnect to see it again.")
        self._teardown_remote()
        self.refresh()
        self._update_buttons()

    # -- views ---------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild both the tree and the table from whatever the peer currently says."""
        self._rebuild_tree()
        self._rebuild_table()
        self._rebuild_alerts()
        self._refresh_contexts()
        self.refresh_actions()
        self._refresh_board()
        self._on_selection_changed()

    def _rebuild_tree(self) -> None:
        self.tree.clear()
        if self.remote is None:
            return

        entities = dict(self.remote.mdib.entities.items())
        children: dict[Any, list[str]] = {}
        for handle, entity in entities.items():
            children.setdefault(getattr(entity, "parent_handle", None), []).append(handle)

        def add(handle: str, parent: QTreeWidgetItem | None) -> None:
            entity = entities[handle]
            node_type = getattr(entity, "node_type", None)
            name = getattr(node_type, "localname", str(node_type))
            item = QTreeWidgetItem([name, handle])
            if parent is None:
                self.tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            for child in sorted(children.get(handle, [])):
                add(child, item)

        for root in sorted(children.get(None, [])):
            add(root, None)
        self.tree.expandToDepth(2)
        self.tree.resizeColumnToContents(0)

    def _rebuild_table(self) -> None:
        metrics = {} if self.remote is None else self.remote.metrics()
        selected = self.selected_handle()

        self.table.setRowCount(len(metrics))
        for row, (handle, metric) in enumerate(sorted(metrics.items())):
            writable, tooltip = self._writable_text(metric)
            cells = {
                COL_HANDLE: handle,
                COL_LABEL: metric.label or "",
                COL_KIND: metric.kind.value if metric.kind else metric.node_type_name,
                COL_VALUE: _value_text(metric),
                COL_RANGE: metric.domain_text() or metric.range_text(),
                COL_UNIT: metric.unit_label or "",
                COL_WRITABLE: writable,
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                if column == COL_VALUE:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == COL_WRITABLE and writable != "yes":
                    item.setForeground(muted_colour(self))
                if column == COL_WRITABLE:
                    item.setToolTip(tooltip)
                if column == COL_LABEL and not metric.label:
                    # A foreign device may publish no concept description at all. Show the
                    # code instead of an empty cell, so the row is still identifiable.
                    item.setText(metric.type_code or "")
                    item.setForeground(muted_colour(self))
                self.table.setItem(row, column, item)

        if selected is not None:
            self.select_handle(selected)
        self._columns.refit()

    @staticmethod
    def _writable_text(metric: Any) -> tuple[str, str]:
        if metric.controllable_now:
            return "yes", "This device accepts writes to this metric"
        if metric.controllable:
            return "disabled", "There is a set operation, but it is currently disabled"
        return "no", "No set operation targets this metric"

    def refresh_values(self) -> None:
        """Update just the value cells, keeping the selection and column widths."""
        if self.remote is None:
            return
        metrics = self.remote.metrics()
        self.board.show_values({handle: _displayable(metric) for handle, metric in metrics.items()})
        for row in range(self.table.rowCount()):
            handle_item = self.table.item(row, COL_HANDLE)
            if handle_item is None:
                continue
            metric = metrics.get(handle_item.text())
            if metric is None:
                continue
            item = self.table.item(row, COL_VALUE)
            if item is not None:
                item.setText(_value_text(metric))

    def _on_waveforms_changed(self, blocks_by_handle: dict) -> None:
        """A block arrived from the peer.

        The blocks come with the signal, snapshotted when the report landed. Re-reading the
        mdib here would see whatever is newest by the time Qt delivers this, which is not
        the block this event is about - see MdibBridge._on_waveforms.

        Only the handles the report carried, too. Pushing every metric on every report -
        which is what calling refresh_values here used to do - spliced each waveform's
        latest block into its trace once per *other* waveform.
        """
        if self.remote is None:
            return
        self.board.append_samples(blocks_by_handle)
        self.refresh_values()

    # -- widgets or table ----------------------------------------------------------

    @property
    def use_widgets(self) -> bool:
        """Whether the peer's metrics are shown as controls rather than as a table."""
        return self.metric_stack.currentWidget() is self.board

    def set_use_widgets(self, enabled: bool) -> None:  # noqa: FBT001 - matches the Qt signal
        """Switch between a control per metric and the table."""
        self.metric_stack.setCurrentWidget(self.board if enabled else self.table)
        # The editor edits the table's selection, and the board has none. Each card is its
        # own editor instead.
        self.editor_widget.setVisible(not enabled)
        if enabled:
            self._refresh_board()

    def _refresh_board(self) -> None:
        """Rebuild the controls from what the peer currently publishes."""
        if self.remote is None:
            self.board.clear()
            return
        metrics = self.remote.metrics()
        self.board.set_metrics([from_remote_metric(metric) for _, metric in sorted(metrics.items())])
        self.board.show_values({handle: _displayable(metric) for handle, metric in metrics.items()})

    def _on_widget_value_requested(self, handle: str, value: object) -> None:
        """A control asked for a value on the peer. That is a remote write, so it waits."""
        if self.remote is None:
            return
        self.select_handle(handle)
        self.invocation_label.setText("waiting\u2026")
        if not self._set_call.start(self.remote.set_value, handle, value):
            self.invocation_label.setText("busy, try again")

    def refresh_actions(self) -> None:
        """Rebuild the buttons for the peer's actions."""
        actions = {} if self.remote is None else self.remote.actions()
        for handle, button in list(self.action_buttons.items()):
            if handle not in actions:
                self.actions_row.removeWidget(button)
                button.deleteLater()
                del self.action_buttons[handle]

        for handle, action in sorted(actions.items()):
            button = self.action_buttons.get(handle)
            if button is None:
                button = QPushButton(action.caption)
                button.clicked.connect(lambda _=False, h=handle: self._on_run_action(h))
                self.actions_row.insertWidget(self.actions_row.count() - 1, button)
                self.action_buttons[handle] = button
            button.setText(action.caption)
            # Same rule as a metric editor: offered only while the device says it is
            # enabled, and OperatingMode can change under us.
            button.setEnabled(action.enabled and not self._set_call.busy)
            button.setToolTip(
                f"{handle}\nacts on {action.target_handle}"
                + ("" if action.enabled else "\ndisabled by the device"),
            )
        self.actions_widget.setVisible(bool(actions))

    def _on_run_action(self, handle: str) -> None:
        if self.remote is None:
            return
        self.invocation_label.setText("waiting\u2026")
        if not self._set_call.start(self.remote.run_action, handle):
            self.invocation_label.setText("busy, try again")

    def _rebuild_alerts(self) -> None:
        """Show the peer's alarms, including how each one is announced."""
        alerts = {} if self.remote is None else self.remote.alerts()
        self.alert_table.setRowCount(len(alerts))
        for row, (handle, alert) in enumerate(sorted(alerts.items())):
            cells = {
                ACOL_HANDLE: handle,
                ACOL_LABEL: alert.label or "",
                ACOL_SOURCE: ", ".join(alert.source_handles),
                ACOL_LIMITS: alert.limit_text(),
                ACOL_KIND: alert.kind or "",
                ACOL_PRIORITY: alert.priority or "",
                # One condition, several signals: the reason BICEPS keeps them apart.
                ACOL_SIGNALS: ", ".join(sorted(alert.signals.values())),
                ACOL_STATE: "PRESENT" if alert.present else "clear",
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                if column == ACOL_STATE and not alert.present:
                    item.setForeground(muted_colour(self))
                if column == ACOL_SIGNALS and alert.signals:
                    item.setToolTip("\n".join(f"{h}: {m}" for h, m in sorted(alert.signals.items())))
                self.alert_table.setItem(row, column, item)
        self._alert_columns.refit()

    def _refresh_contexts(self) -> None:
        """Show associated peer patients without making remote contexts editable."""
        if self.remote is None:
            self.context_label.setText("")
            return
        patients = self.remote.patient_contexts()
        if not patients:
            self.context_label.setText("Patient: none attached")
            return
        lines = [
            f"{handle}: {patient.summary() or 'no demographics'}"
            for handle, patient in patients.items()
        ]
        prefix = "Patient" if len(lines) == 1 else "Patients"
        self.context_label.setText(f"{prefix}: " + " | ".join(lines))

    # -- selection and editing -----------------------------------------------------

    def selected_handle(self) -> str | None:
        """Handle of the selected metric row, or None."""
        model = self.table.selectionModel()
        rows = model.selectedRows() if model else []
        if not rows:
            return None
        item = self.table.item(rows[0].row(), COL_HANDLE)
        return item.text() if item else None

    def select_handle(self, handle: str) -> None:
        """Restore the selection to a given handle, if it is still there."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_HANDLE)
            if item is not None and item.text() == handle:
                self.table.selectRow(row)
                return

    def _on_selection_changed(self) -> None:
        self.invocation_label.setText("")
        handle = self.selected_handle()
        metric = None if (handle is None or self.remote is None) else self.remote.metrics().get(handle)

        if metric is None:
            self.editor_label.setText("Connect to a device to control it")
            self._set_editor_enabled(enabled=False)
            return

        # The editor comes from the *target* descriptor, never from the operation: it is the
        # metric that says which values are legal.
        if metric.kind is MetricKind.CHOICE and metric.allowed_values:
            self.editor_stack.setCurrentIndex(EDITOR_CHOICE)
            self.choice_box.clear()
            self.choice_box.addItems(list(metric.allowed_values))
            if metric.value is not None:
                index = self.choice_box.findText(str(metric.value))
                if index >= 0:
                    self.choice_box.setCurrentIndex(index)
        else:
            self.editor_stack.setCurrentIndex(EDITOR_TEXT)
            self.value_edit.setText("" if metric.value is None else str(metric.value))
            self.value_edit.setPlaceholderText(metric.range_text())

        caption = metric.label or handle
        if metric.has_range:
            caption = f"{caption} ({metric.range_text()})"
        self.editor_label.setText(f"{caption}:")
        self._set_editor_enabled(enabled=metric.controllable_now)
        if not metric.controllable_now:
            self.invocation_label.setText(
                "read-only" if not metric.controllable else "control disabled by the device",
            )

    def _set_editor_enabled(self, *, enabled: bool) -> None:
        self.editor_stack.setEnabled(enabled)
        self.apply_button.setEnabled(enabled and not self._set_call.busy)

    def _on_apply(self) -> None:
        handle = self.selected_handle()
        if handle is None or self.remote is None:
            return
        metric = self.remote.metrics().get(handle)
        if metric is None:
            return

        if metric.kind is MetricKind.CHOICE and metric.allowed_values:
            value: Decimal | str = self.choice_box.currentText()
        elif metric.kind is MetricKind.NUMBER:
            raw = self.value_edit.text().strip()
            try:
                value = Decimal(raw)
            except InvalidOperation:
                self.invocation_label.setText(f"{raw!r} is not a number")
                return
        else:
            value = self.value_edit.text()

        self.invocation_label.setText("waiting\u2026")
        self._set_call.start(self.remote.set_value, handle, value)

    def _on_set_finished(self, state: Any) -> None:
        if state in FINISHED_STATES:
            self.invocation_label.setText(f"accepted ({state})")
        else:
            self.invocation_label.setText(f"refused ({state})")
        self.refresh_values()

    def _on_set_failed(self, message: str) -> None:
        self.invocation_label.setText(f"failed: {message}")

    # -- chrome --------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _on_busy_changed(self, busy: bool) -> None:  # noqa: FBT001 - matches the Qt signal
        if not busy:
            self._update_buttons()
            self._set_editor_enabled(enabled=self.editor_stack.isEnabled())
        else:
            self.scan_button.setEnabled(False)
            self.connect_button.setEnabled(False)
            self.apply_button.setEnabled(False)

    def _update_buttons(self) -> None:
        working = self._scan_call.busy or self._connect_call.busy
        self.scan_button.setEnabled(not working)
        self.connect_button.setEnabled(not working and self._selected_device() is not None)
        self.disconnect_button.setEnabled(self.remote is not None)

    # -- teardown ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Disconnect and stop discovery. Called when the window closes."""
        self._teardown_remote()
        self.service.stop()
