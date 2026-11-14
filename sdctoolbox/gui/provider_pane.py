"""The "My Device" tab: the data sources we publish ourselves.

Shows one row per data source with its live value, lets the user create and delete them,
edit their values, and toggle whether other devices may write to them.

Values change from two directions: the user editing them here, and a remote consumer writing
over the network. Both arrive through the MdibBridge, so the table looks the same either way.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from html import escape
from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from lxml import etree

from ..model import MetricKind
from ..mdib_io import live_entities, mdib_bytes
from .collapsible import CollapsibleSection
from .context_dialog import ContextDialog
from .decimal_input import DecimalInputError, parse_decimal_input
from .edit_dialogs import EditAlertDialog, EditSourceDialog
from .helpers import (
    sample_count_text,
    select_table_row,
    selected_table_value,
    value_text,
)
from .new_alert_dialog import NewAlertDialog
from .new_metric_dialog import NewMetricDialog
from .no_wheel import NoWheelComboBox
from .qt_bridge import MdibBridge
from .signal_icons import SIGNALS_ROLE, SignalIconsDelegate, tooltip
from .styling import (
    apply_row_selection_style,
    constrain_dynamic_label,
    mute,
    muted_colour,
    set_button_kind,
    token,
)
from .table_columns import TableColumns
from .widgets import WidgetBoard, from_spec
from .widgets.base import CardSettings
from .widgets.controls import show_choice_value

if TYPE_CHECKING:
    from ..provider_service import ProviderService

COLUMNS = ["Handle", "Label", "Kind", "Value", "Range", "Unit", "Remote control"]
COL_HANDLE, COL_LABEL, COL_KIND, COL_VALUE, COL_RANGE, COL_UNIT, COL_CONTROL = range(len(COLUMNS))

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

METRIC_DESCRIPTOR_TYPES = frozenset(kind.descriptor_qname for kind in MetricKind)
SET_OPERATION_DESCRIPTOR_TYPES = frozenset(
    {
        pm.SetValueOperationDescriptor,
        pm.SetStringOperationDescriptor,
    },
)
ALERT_DESCRIPTOR_TYPES = frozenset(
    {
        pm.AlertConditionDescriptor,
        pm.LimitAlertConditionDescriptor,
        pm.AlertSignalDescriptor,
    },
)


class ProviderPane(QWidget):
    """Everything the user does to the device they are publishing."""

    def __init__(self, service: ProviderService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        # Guards against the table's own updates being mistaken for user clicks.
        self._refreshing = False
        self._editor_definition = None
        self._pending_rebuilds: list = []

        self._build_ui()

        self.bridge = MdibBridge(service.mdib, self)
        self.bridge.metrics_changed.connect(self._on_values_changed)
        # Structural observables fire while the provider is leaving its transaction. Queue
        # the view work so service bookkeeping is complete and no GUI code runs in that
        # callback stack.
        self.bridge.descriptors_added.connect(self._on_descriptors_changed, Qt.QueuedConnection)
        self.bridge.descriptors_updated.connect(self._on_descriptors_changed, Qt.QueuedConnection)
        self.bridge.descriptors_deleted.connect(self._on_descriptors_changed, Qt.QueuedConnection)
        self.bridge.operations_changed.connect(self._on_operations_changed, Qt.QueuedConnection)
        self.bridge.alerts_changed.connect(lambda _: self._schedule(self.refresh_alerts))
        self.bridge.contexts_changed.connect(lambda _: self._schedule(self.refresh_contexts))
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
        # Cards look as the device's file says; _set_card_settings() keeps the service current.
        for handle, settings in self.service.card_settings.items():
            self.board.set_card_settings(handle, settings)
        self.board.value_requested.connect(self._on_widget_value_requested)
        self.board.delete_requested.connect(self._on_card_delete_requested)
        self.board.edit_requested.connect(self._edit_source)
        self.board.card_rebuilt.connect(lambda _handle: self._refresh_board())
        self.metric_stack = QStackedWidget()
        self.metric_stack.addWidget(self.board)
        self.metric_stack.addWidget(self.table)

        self.new_button = QPushButton("New data source\u2026")
        set_button_kind(self.new_button, "highlight")
        self.new_button.clicked.connect(self._on_new)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._on_remove)
        self.remove_button.setEnabled(False)
        self.edit_button = QPushButton("Edit…")
        self.edit_button.setToolTip("Change what may change while the device runs")
        self.edit_button.clicked.connect(lambda: self._edit_source(self.selected_handle()))
        self.edit_button.setEnabled(False)
        self.context_button = QPushButton("Patient and location\u2026")
        self.context_button.clicked.connect(self._on_edit_contexts)
        set_button_kind(self.context_button, "highlight-outline")
        for button in (self.edit_button, self.remove_button):
            set_button_kind(button, "design-outline")
        self.context_label = QLabel("")
        constrain_dynamic_label(self.context_label, max_lines=3)
        self.context_label.setTextInteractionFlags(Qt.NoTextInteraction)
        mute(self.context_label)
        transport = "Transport: TLS with client certificates" if self.service.tls_enabled else "Transport: HTTP (lab only)"
        self.transport_label = QLabel(transport)
        mute(self.transport_label)

        buttons = QHBoxLayout()
        buttons.addWidget(self.new_button)
        buttons.addWidget(self.edit_button)
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
        self.alert_table.setItemDelegateForColumn(ACOL_SIGNALS, SignalIconsDelegate(self.alert_table))

        self.new_alert_button = QPushButton("New alarm\u2026")
        self.new_alert_button.clicked.connect(self._on_new_alert)
        self.remove_alert_button = QPushButton("Remove alarm")
        self.remove_alert_button.clicked.connect(self._on_remove_alert)
        self.edit_alert_button = QPushButton("Edit alarm…")
        self.edit_alert_button.clicked.connect(self._on_edit_alert)
        self.toggle_alert_button = QPushButton("Raise")
        self.toggle_alert_button.clicked.connect(self._on_toggle_alert)
        self.acknowledge_button = QPushButton("Acknowledge")
        self.acknowledge_button.clicked.connect(self._on_acknowledge)
        self.stop_latched_button = QPushButton("Stop latched")
        self.stop_latched_button.clicked.connect(self._on_stop_latched)
        self.delegate_button = QPushButton("Remote*")
        self.delegate_button.clicked.connect(self._on_delegate)

        # Creating and changing alarms first, then what you do to a running one.
        alert_buttons = self.alert_toolbar = QHBoxLayout()
        for button, kind in ((self.new_alert_button, "highlight"),
                             (self.edit_alert_button, "highlight-outline"),
                             (self.remove_alert_button, "highlight-outline"),
                             (self.toggle_alert_button, "design-outline"),
                             (self.acknowledge_button, "design-outline"),
                             (self.stop_latched_button, "design-outline"),
                             (self.delegate_button, "design-outline")):
            set_button_kind(button, kind)
            alert_buttons.addWidget(button)
            if button is self.remove_alert_button:
                alert_buttons.addSpacing(16)
        alert_buttons.addStretch(1)

        alert_box = QWidget()
        alert_layout = QVBoxLayout(alert_box)
        alert_layout.setContentsMargins(0, 0, 0, 0)
        alert_layout.addLayout(alert_buttons)
        alert_layout.addWidget(self.alert_table)

        self.editor_label = QLabel("Select a data source to change its value")
        constrain_dynamic_label(self.editor_label, max_lines=2, max_width=240)
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

        self.metrics_section = CollapsibleSection("Metrics", self.metric_stack)
        self.alarms_section = CollapsibleSection("Alarms", alert_box, expanded=False)
        self.structure_section = None
        self.native_tree = None
        self._native_items = {}
        self._native_state_items = {}
        if self.service.from_mdib_file:
            self.native_tree = QTreeWidget()
            self.native_tree.setHeaderLabels(["Handle", "Descriptor", "Current state"])
            self.native_tree.setUniformRowHeights(True)
            self.native_tree.setAccessibleName("MDIB containment")
            self.structure_section = CollapsibleSection("MDIB structure", self.native_tree, expanded=False)
            self.structure_section.set_hint("Descriptor tree with handles")

        layout = QVBoxLayout(self)
        # TitledPanel already pads both panels. Without this the style's own 9px lands on
        # top of that here but not in the consumer pane, which sets the same zero - so the
        # two panels disagreed by 9px at the top and the bottom.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(buttons)
        layout.addWidget(self.transport_label)
        layout.addWidget(self.context_label)
        if self.service.from_mdib_file:
            hint = QLabel("Loaded from an MDIB file and editable like any device. Its alarms keep their supplied "
                          "state until their source changes. Hover containment entries for XML.")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        for section in (self.structure_section, self.metrics_section, self.alarms_section):
            if section is not None:
                layout.addWidget(section, 1)
        layout.addWidget(self.actions_widget)
        layout.addWidget(self.editor_widget)

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
        self.edit_button.setVisible(not enabled)
        self.editor_widget.setVisible(not enabled)
        if enabled:
            self._refresh_board()

    def _refresh_board(self) -> None:
        """Rebuild the controls from the current metrics."""
        specs = self.service.list_metrics()
        waveform_cards = {handle: self.board.card(handle) for handle, spec in specs.items()
                          if spec.kind is MetricKind.WAVEFORM}
        self.board.set_metrics(
            [
                from_spec(handle, spec, editable=True)
                for handle, spec in sorted(specs.items())
            ],
        )
        self.board.show_values({handle: self._displayable(handle, spec) for handle, spec in specs.items()})
        if self.service.from_mdib_file:
            # Native snapshots have no incoming stream to seed a newly created waveform card.
            self.board.append_samples({handle: self.service.get_samples(handle) for handle, card in waveform_cards.items()
                                       if card is not self.board.card(handle)})

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
        blocker = QSignalBlocker(self.table)
        try:
            self.table.clearSelection()
            self.table.setCurrentItem(None)
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
                    if not spec.active:
                        item.setForeground(muted_colour(self))
                        item.setToolTip("Switched off: no generation, no remote sets, no alarms")
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
            if selected is not None:
                self.select_handle(selected)
        finally:
            self._refreshing = False
            blocker.unblock()

        self._on_selection_changed()
        self.metrics_section.set_chips([str(len(specs))])
        self._columns.refit()
        self._refresh_board()
        if self.native_tree is not None:
            self._refresh_native_tree()

    def _refresh_native_tree(self) -> None:
        """Keep every imported entity inspectable, including ones without a metric card."""
        self.native_tree.clear()
        mdib = self.service.mdib
        with mdib.mdib_lock:
            # Handles, types and state handles only, read in place rather than copied.
            entities = {
                entity.handle: (entity.node_type.localname, entity.parent_handle, [
                    (getattr(state, "Handle", None) or entity.handle, state.NODETYPE.localname)
                    for state in (entity.states if entity.descriptor.is_context_descriptor else [entity.state])
                    if state is not None
                ])
                for entity in live_entities(mdib, include_contexts=True)
            }
            snapshot = etree.fromstring(mdib_bytes(mdib))[0]
        specs = self.service.list_metrics()
        descriptor_nodes = {}

        def collect_descriptors(parent):
            for child in parent:
                if child.get("Handle") is not None:
                    descriptor_nodes[child.get("Handle")] = child
                    collect_descriptors(child)

        collect_descriptors(snapshot.find(pm.MdDescription))
        state_nodes = {node.get("Handle") or node.get("DescriptorHandle"): node for node in snapshot.find(pm.MdState)}
        self._native_state_items = {}
        self._native_items = {
            handle: QTreeWidgetItem([handle, node_type, ""])
            for handle, (node_type, _parent, _states) in entities.items()
        }
        for handle, (_node_type, parent_handle, states) in entities.items():
            item = self._native_items[handle]
            parent = self._native_items.get(parent_handle)
            if parent is None:
                self.native_tree.addTopLevelItem(item)
            else:
                parent.addChild(item)
            descriptor_node = descriptor_nodes.get(handle)
            if descriptor_node is not None:
                item.setToolTip(1, f"<pre>{escape(etree.tostring(descriptor_node, encoding='unicode'))}</pre>")
            for state_handle, state_type in states:
                node = state_nodes[state_handle]
                child = QTreeWidgetItem([state_handle, state_type, ""])
                child.setToolTip(1, f"<pre>{escape(etree.tostring(node, encoding='unicode'))}</pre>")
                item.addChild(child)
                self._native_state_items[state_handle] = child
            if handle in specs:
                item.setText(2, self._value_text(handle, specs[handle]))
        self.native_tree.expandToDepth(1)

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
            return sample_count_text(self.service.get_samples(handle))
        return value_text(self.service.get_value(handle))

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
        for handle in states_by_handle:
            if handle in self._native_items and handle in specs:
                self._native_items[handle].setText(2, self._value_text(handle, specs[handle]))
                state = deepcopy(self.service.mdib.entities.by_handle(handle).state)
                node = state.mk_state_node(pm.State, self.service.mdib.nsmapper)
                self._native_state_items[handle].setToolTip(1, f"<pre>{escape(etree.tostring(node, encoding='unicode'))}</pre>")
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

        selected = self.selected_handle()
        if selected is not None and selected in states_by_handle:
            current = self.service.get_value(selected)
            if self.editor_stack.currentIndex() == EDITOR_CHOICE:
                if not self.choice_box.view().isVisible():
                    show_choice_value(self.choice_box, current)
            elif not self.value_edit.hasFocus():
                self.value_edit.setText("" if current is None else str(current))

    def _on_descriptors_changed(self, descriptors_by_handle: dict) -> None:
        """Refresh only the views affected by a structural MDIB change."""
        node_types = {
            getattr(descriptor, "NODETYPE", None)
            for descriptor in descriptors_by_handle.values()
        }
        if node_types & (METRIC_DESCRIPTOR_TYPES | SET_OPERATION_DESCRIPTOR_TYPES):
            self._schedule(self.refresh)
        if node_types & ALERT_DESCRIPTOR_TYPES:
            self._schedule(self.refresh_alerts)
        if pm.ActivateOperationDescriptor in node_types:
            self._schedule(self.refresh_actions)

    def _schedule(self, rebuild) -> None:  # noqa: ANN001
        """Rebuild a view once the current burst of MDIB signals has been delivered.

        Importing a profile changes hundreds of descriptors and operations, each arriving as
        its own signal. Rebuilding the pane for every one left the window "Not Responding".
        """
        if not self._pending_rebuilds:
            QTimer.singleShot(0, self, self._run_scheduled)
        if rebuild not in self._pending_rebuilds:
            self._pending_rebuilds.append(rebuild)

    def _run_scheduled(self) -> None:
        pending, self._pending_rebuilds = self._pending_rebuilds, []
        if not self.service.running:
            return  # queued by a signal from just before the provider stopped
        for rebuild in pending:
            rebuild()

    def _on_operations_changed(self, states_by_handle: dict) -> None:
        """Follow operation modes in metric controls and local action availability."""
        changed_handles = set(states_by_handle)
        action_handles = set(self.service.list_actions())
        if changed_handles & action_handles:
            self._schedule(self.refresh_actions)

        metric_operation_handles = {
            operation_handle
            for metric_handle in self.service.list_metrics()
            if (operation_handle := self.service.operation_handle_for(metric_handle)) is not None
        }
        if changed_handles & metric_operation_handles:
            self._schedule(self.refresh)

    # -- alarms --------------------------------------------------------------------

    def refresh_alerts(self) -> None:
        """Rebuild the alarm table."""
        alerts = self.service.list_alerts()
        selected = self.selected_alert_handle()

        self.alert_table.setRowCount(len(alerts))
        active = 0
        for row, (handle, spec) in enumerate(sorted(alerts.items())):
            present = self.service.alert_present(handle)
            active += present
            signals = self.service.signal_states(handle)
            presences = {str(signal.manifestation): str(signal.presence) for signal in signals}
            cells = {
                ACOL_HANDLE: handle,
                ACOL_LABEL: spec.label,
                ACOL_SOURCE: spec.source_handle,
                ACOL_WHEN: spec.limit_text() or "by hand",
                ACOL_KIND: spec.kind.value,
                ACOL_PRIORITY: (spec.actual_priority or spec.priority).value,
                ACOL_STATE: "active" if present else "clear",
                ACOL_SIGNALS: "",
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                if column == ACOL_STATE and not present:
                    item.setForeground(muted_colour(self))
                if column == ACOL_STATE and present:
                    item.setForeground(token("text/error"))
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                if column == ACOL_WHEN and not spec.has_limits:
                    item.setToolTip("No limits, so this alarm only moves when you raise or clear it")
                if column == ACOL_SIGNALS:
                    item.setData(SIGNALS_ROLE, presences)
                    item.setData(Qt.UserRole, "  ".join(signal.summary() for signal in signals))
                    item.setToolTip(
                        tooltip(presences) + "\n\n"
                        "How each signal is announcing the condition. Ack means it has been\n"
                        "acknowledged; the condition itself is still present. ->Rem is a\n"
                        "simulated remote location, not a delegation handoff. Latch means a signal\n"
                        "continues to announce a cleared condition until stopped.",
                    )
                    if not present:
                        item.setForeground(muted_colour(self))
                self.alert_table.setItem(row, column, item)

        self._alert_columns.refit()
        self.alarms_section.set_chips([str(len(alerts)), f"{active} active" if active else "none active"])
        if selected is not None:
            self.select_alert_handle(selected)
        self._on_alert_selection_changed()

    def selected_alert_handle(self) -> str | None:
        """Handle of the selected alarm row, or None."""
        return selected_table_value(self.alert_table, ACOL_HANDLE)

    def select_alert_handle(self, handle: str) -> None:
        """Restore the alarm selection, if that alarm still exists."""
        select_table_row(self.alert_table, ACOL_HANDLE, handle)

    def _on_alert_selection_changed(self) -> None:
        handle = self.selected_alert_handle()
        spec = self.service.list_alerts().get(handle) if handle else None
        self.remove_alert_button.setEnabled(spec is not None)
        self.edit_alert_button.setEnabled(spec is not None)
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
        """Signal controls follow the selected alarm's current state and capabilities."""
        signals = self.service.signal_states(handle) if handle and spec is not None else []
        present = bool(handle) and spec is not None and self.service.alert_present(handle)

        # Acknowledging a condition that is not raised is meaningless, and the provider
        # refuses it, so do not offer it either.
        unacknowledged = [signal for signal in signals if signal.acknowledgeable]
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
        anywhere_remote = any(signal.remote_location for signal in delegable)
        self.delegate_button.setText("Local*" if anywhere_remote else "Remote*")
        if spec is None:
            self.delegate_button.setToolTip("")
        elif not delegable:
            self.delegate_button.setToolTip(
                "This alarm's signals are not delegable: their descriptors do not set\n"
                "SignalDelegationSupported",
            )
        else:
            self.delegate_button.setToolTip(
                "Demonstrate Loc/Rem state only. This does not perform the normative\n"
                "signal-delegation handoff with another device.",
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

    def _edit_source(self, handle: str | None) -> None:
        if handle is None or handle not in self.service.list_metrics():
            return
        card = self.board.card(handle)
        dialog = EditSourceDialog(self.service, handle, self,
                                  card=None if card is None else (card.spec, self.board.card_settings(handle)))
        if dialog.exec():
            if dialog.card_settings() is not None:
                self._set_card_settings(handle, dialog.card_settings())
            self.refresh()
            self.refresh_alerts()

    def _set_card_settings(self, handle: str, settings: CardSettings) -> None:
        """Change a card, and the service's copy an MDIB export with toolbox behaviour stores."""
        self.service.card_settings[handle] = settings
        self.board.set_card_settings(handle, settings)

    def _on_edit_alert(self) -> None:
        handle = self.selected_alert_handle()
        if handle is None:
            return
        if EditAlertDialog(self.service, handle, self).exec():
            self.refresh_alerts()

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
        remote = not any(signal.remote_location for signal in delegable)
        try:
            for signal in delegable:
                self.service.set_signal_delegated(signal.handle, delegated=remote)
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
                set_button_kind(button, "design-outline")
                button.clicked.connect(lambda _=False, h=handle: self._on_run_action(h))
                # Before the stretch, so buttons stay left and the row does not jump about
                # as actions come and go.
                self.actions_row.insertWidget(self.actions_row.count() - 1, button)
                self.action_buttons[handle] = button
            button.setText(spec.label)
            button.setEnabled(self._operation_enabled(handle))
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
        return selected_table_value(self.table, COL_HANDLE)

    def select_handle(self, handle: str) -> None:
        """Restore the selection to a given handle, if it still exists."""
        select_table_row(self.table, COL_HANDLE, handle)

    def _on_selection_changed(self) -> None:
        if self._refreshing:
            return
        handle = self.selected_handle()
        self.remove_button.setEnabled(handle is not None)
        self.edit_button.setEnabled(handle is not None)
        if handle is None:
            self._editor_definition = None
            self.editor_label.setText("Select a data source to change its value")
            self._set_editor_enabled(enabled=False)
            return

        spec = self.service.list_metrics().get(handle)
        if spec is None:
            self._editor_definition = None
            self._set_editor_enabled(enabled=False)
            return

        definition = from_spec(handle, spec, editable=True)
        same_definition = definition == self._editor_definition
        self._editor_definition = definition
        self.editor_label.setText(f"{spec.label}:")
        current = self.service.get_value(handle)
        if spec.kind is MetricKind.CHOICE:
            self.editor_stack.setCurrentIndex(EDITOR_CHOICE)
            if not same_definition or not self.choice_box.view().isVisible():
                self.choice_box.clear()
                self.choice_box.addItems(list(spec.allowed_values))
                show_choice_value(self.choice_box, current)
        else:
            self.editor_stack.setCurrentIndex(EDITOR_TEXT)
            if not same_definition or not self.value_edit.hasFocus():
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
        if dialog.card_control():
            self._set_card_settings(handle, CardSettings(control=dialog.card_control()))
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
        dependent_actions = [
            action_handle
            for action_handle, action in self.service.list_actions().items()
            if action.target_handle == handle or handle in action.effects
        ]

        question = f"Remove {name}?\n\nConnected consumers will see it disappear."
        if watching:
            question += f"\n\nThese alarms watch it and will be removed too:\n  {', '.join(watching)}"
        if dependent_actions:
            question += f"\n\nThese actions depend on it and will be removed too:\n  {', '.join(dependent_actions)}"

        if QMessageBox.question(self, "Remove data source", question) != QMessageBox.Yes:
            return

        try:
            self.service.remove_metric(handle)
        except KeyError as exc:
            QMessageBox.warning(self, "Could not remove", str(exc))
        self.refresh()
        self.refresh_alerts()
        self.refresh_actions()

    def _on_apply(self) -> None:
        handle = self.selected_handle()
        if handle is None:
            return
        spec = self.service.list_metrics().get(handle)
        if spec is None:
            return

        if spec.kind is MetricKind.CHOICE:
            if self.choice_box.currentIndex() < 0:
                return
            value: Decimal | str = self.choice_box.currentText()
        elif spec.kind is MetricKind.NUMBER:
            raw = self.value_edit.text().strip()
            try:
                value = parse_decimal_input(raw, "value", minimum=spec.minimum, maximum=spec.maximum)
            except DecimalInputError as exc:
                QMessageBox.warning(self, "Invalid value", str(exc))
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
        return self._operation_enabled(operation_handle)

    def _operation_enabled(self, handle: str) -> bool:
        """Whether an operation is currently available, including its BICEPS default."""
        # Read in place: the library's entity getter deep-copies, and this runs for every row.
        state = self.service.mdib.states.descriptor_handle.get_one(handle, allow_none=True)
        if state is None:
            return False
        mode = getattr(state, "OperatingMode", None)
        return mode is None or mode == pm_types.OperatingMode.ENABLED
