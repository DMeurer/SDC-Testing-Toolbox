"""The MDIB check window: findings for My device or a connection, beside the main window.

My device is checked again by the main window whenever its MDIB changes; a connection only
when asked. Double-clicking a finding shows that element in its pane. On My device, Fix makes
the change the selected finding offers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import mdib_check
from .styling import muted_colour, set_button_kind, token

if TYPE_CHECKING:
    from collections.abc import Callable

    from .main_window import MainWindow

MY_DEVICE = "My device"
FIX_COLUMN = 3
LEVEL_COLOURS = {"error": "text/error", "warning": "state/pending"}


def status_text(findings: list[mdib_check.Finding]) -> str:
    """The status bar button: errors if any, else warnings, else OK. Hints alone are OK."""
    count = mdib_check.counts(findings)
    for level in ("error", "warning"):
        if count[level]:
            return f"MDIB check: {count[level]} {level}{'' if count[level] == 1 else 's'}"
    return "MDIB check: OK"


def reveal(pane, handle: str, tree: QTreeWidget | None, handle_column: int) -> bool:  # noqa: ANN001 - either pane
    """Bring `handle` into view in a provider or consumer pane: its card or row, its alarm, or its tree entry."""
    if handle in pane.board.handles:
        pane.metrics_section.set_expanded(True)
        pane.select_handle(handle)
        pane.board.ensureWidgetVisible(pane.board.card(handle))
        return True
    table = pane.alert_table
    for row in range(table.rowCount()):
        if table.item(row, 0) is not None and table.item(row, 0).text() == handle:
            pane.alarms_section.set_expanded(True)
            table.selectRow(row)
            table.scrollToItem(table.item(row, 0))
            return True
    matches = tree.findItems(handle, Qt.MatchExactly | Qt.MatchRecursive, handle_column) if tree is not None else []
    if matches:
        pane.structure_section.set_expanded(True)
        tree.setCurrentItem(matches[0])
        tree.scrollToItem(matches[0])
        return True
    return False


class _TargetBox(QComboBox):
    """Lists the open connections afresh each time it opens."""

    def __init__(self, refresh: Callable[[], None]) -> None:
        super().__init__()
        self._refresh = refresh

    def showPopup(self) -> None:  # noqa: N802 - Qt naming
        self._refresh()
        super().showPopup()


class MdibCheckWindow(QWidget):
    """Level | Where | Finding for the chosen MDIB, as its own top-level window."""

    def __init__(self, main: MainWindow) -> None:
        # A parented Qt.Window stays a separate, non-modal window and closes with its parent.
        super().__init__(main, Qt.Window)
        self.main = main
        self.setWindowTitle(f"MDIB check — {main.service.friendly_name}")
        self.resize(1000, 420)

        self.target_box = _TargetBox(self.refresh_targets)
        self.target_box.setAccessibleName("What to check")
        self.target_box.activated.connect(lambda _index: self.run())
        self.again_button = QPushButton("Check again")
        set_button_kind(self.again_button, "design-outline")
        self.again_button.clicked.connect(self.run)
        self.fix_button = QPushButton("Fix")
        set_button_kind(self.fix_button, "design")
        self.fix_button.setToolTip("Make the change in the Fix column for the selected finding")
        self.fix_button.clicked.connect(self.fix_selected)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Level", "Where", "Finding", "Fix"])
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)
        self.tree.setColumnWidth(0, 80)
        self.tree.setColumnWidth(1, 200)
        self.tree.setColumnWidth(2, 480)
        self.tree.itemDoubleClicked.connect(lambda item, _column: self.show_element(item))
        self.tree.currentItemChanged.connect(lambda *_: self._update_fix_button())

        top = QHBoxLayout()
        top.addWidget(QLabel("Check"))
        top.addWidget(self.target_box, 1)
        top.addWidget(self.again_button)
        top.addWidget(self.fix_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.tree, 1)
        self.refresh_targets()
        self._update_fix_button()

    @property
    def connection(self) -> int | None:
        """The chosen connection number, or None for My device."""
        return self.target_box.currentData()

    def refresh_targets(self) -> None:
        """My device and every connected tab, keeping the current choice where it still exists."""
        chosen = self.connection
        self.target_box.clear()
        self.target_box.addItem(MY_DEVICE, None)
        tabs = self.main.network_tabs
        for pane in tabs.panes:
            if pane.remote is not None:
                self.target_box.addItem(tabs.tabText(tabs.indexOf(pane)).replace("&&", "&"),
                                        pane.property("connectionNumber"))
        self.target_box.setCurrentIndex(max(0, self.target_box.findData(chosen)))

    def _pane(self, number: int):  # noqa: ANN202
        return next((p for p in self.main.network_tabs.panes
                     if p.property("connectionNumber") == number and p.remote is not None), None)

    def run(self) -> None:
        """Check the chosen MDIB now and show the result."""
        number = self.connection
        if number is None:
            self.main.recheck_my_device()  # comes back through show_findings
            return
        pane = self._pane(number)
        if pane is None:
            self.refresh_targets()
            self.show_findings([], note=f"Connection {number} is closed.")
            return
        self.show_findings(mdib_check.check(pane.remote.mdib))

    def show_findings(self, findings: list[mdib_check.Finding], note: str = "") -> None:
        mine = self.connection is None
        self.summary_label.setText(note or f"{mdib_check.summary(findings)}. Checked against {mdib_check.SOURCES}."
                                   + (" Fix makes the change in the Fix column." if mine else ""))
        # Only My device can be changed; a connection's MDIB belongs to the peer.
        self.tree.setColumnHidden(FIX_COLUMN, not mine)
        self.fix_button.setVisible(mine)
        selected = self._selected_finding()
        self.tree.clear()
        for finding in findings:
            text = finding.text + (f" ({finding.rule})" if finding.rule else "")
            item = QTreeWidgetItem([finding.level, finding.where, text, finding.fix.label if finding.fix else ""])
            item.setData(0, Qt.UserRole, finding.handle)
            item.setData(0, Qt.UserRole + 1, finding)
            if finding.fix is not None:
                item.setForeground(FIX_COLUMN, token("accent/text"))
            item.setToolTip(2, text)
            colour = token(LEVEL_COLOURS[finding.level]) if finding.level in LEVEL_COLOURS else muted_colour(self.tree)
            item.setForeground(0, colour)
            if finding.level in LEVEL_COLOURS:
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            self.tree.addTopLevelItem(item)
            if selected is not None and (finding.handle, finding.text) == (selected.handle, selected.text):
                self.tree.setCurrentItem(item)
        self._update_fix_button()

    def _selected_finding(self) -> mdib_check.Finding | None:
        item = self.tree.currentItem()
        return None if item is None else item.data(0, Qt.UserRole + 1)

    def _update_fix_button(self) -> None:
        finding = self._selected_finding()
        self.fix_button.setEnabled(self.connection is None and finding is not None and finding.fix is not None)

    def fix_selected(self) -> bool:
        """Make the selected finding's fix on My device, then check again."""
        finding = self._selected_finding()
        if self.connection is not None or finding is None or finding.fix is None:
            return False
        main = self.main
        try:
            main.service.apply_fix(finding.handle, finding.fix)
        except (KeyError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, "Could not fix", str(exc))
            return False
        main.provider_pane.refresh()
        main.provider_pane.refresh_alerts()
        main.statusBar().showMessage(f"{finding.where}: {finding.fix.label}", 8000)
        main.recheck_my_device()
        return True

    def show_element(self, item: QTreeWidgetItem) -> bool:
        """Show the finding's element in My device or in its connection's tab."""
        handle = item.data(0, Qt.UserRole)
        main = self.main
        number = self.connection
        if number is None:
            pane, tab = main.provider_pane, 0
            tree, column = pane.native_tree, 0
        else:
            pane, tab = self._pane(number), 1
            if pane is None:
                return False
            main.network_tabs.setCurrentWidget(pane)
            tree, column = pane.tree, 1
        if not main.split_view_enabled:
            main.tabs.setCurrentIndex(tab)
        shown = reveal(pane, handle, tree, column)
        if not shown:
            main.statusBar().showMessage(f"{handle} has no row, card or tree entry to show", 8000)
        main.raise_()
        main.activateWindow()
        return shown
