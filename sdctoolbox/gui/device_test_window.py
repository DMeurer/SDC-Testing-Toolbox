"""The Device test window: run the Device test against one connection, beside the main window."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from .. import device_test
from .async_call import AsyncCall
from .mdib_check_window import _TargetBox
from .styling import muted_colour, set_button_kind, token

if TYPE_CHECKING:
    from .main_window import MainWindow

NOTE = "Tests how the device behaves over SDC; it changes values on the device, so run it on a test device only."


class DeviceTestWindow(QWidget):
    """Result | Test | What happened | Standard for the chosen connection."""

    def __init__(self, main: MainWindow) -> None:
        super().__init__(main, Qt.Window)
        self.main = main
        self.resize(1060, 300)
        self._worker = AsyncCall(self)
        self._worker.managed_finished.connect(lambda key, results: self._show(key, results))
        self._worker.managed_failed.connect(lambda key, message: self._show(key, None, message))
        self._run_count = 0

        self.target_box = _TargetBox(self.refresh_targets)
        self.target_box.setAccessibleName("Device to test")
        self.target_box.activated.connect(lambda _index: self.run())
        self.summary_label = QLabel(NOTE)
        self.summary_label.setWordWrap(True)
        self.again_button = QPushButton("Run again")
        set_button_kind(self.again_button, "design-outline")
        self.again_button.clicked.connect(self.run)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Result", "Test", "What happened", "Standard"])
        self.tree.setRootIsDecorated(False)
        self.tree.setWordWrap(True)
        for column, width in enumerate((80, 180, 520)):
            self.tree.setColumnWidth(column, width)

        top = QHBoxLayout()
        top.addWidget(self.target_box)
        top.addWidget(self.summary_label, 1)
        top.addWidget(self.again_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)
        self.refresh_targets()

    @property
    def connection(self) -> int | None:
        return self.target_box.currentData()

    def refresh_targets(self) -> None:
        """Every connected tab, keeping the current choice where it still exists."""
        chosen = self.connection
        self.target_box.clear()
        tabs = self.main.network_tabs
        for pane in tabs.panes:
            if pane.remote is not None:
                self.target_box.addItem(tabs.tabText(tabs.indexOf(pane)).replace("&&", "&"),
                                        pane.property("connectionNumber"))
        self.target_box.setCurrentIndex(max(0, self.target_box.findData(chosen)))
        self.setWindowTitle(f"Device test — {self.target_box.currentText() or 'no connection'}")

    def choose(self, number: int) -> None:
        self.refresh_targets()
        self.target_box.setCurrentIndex(max(0, self.target_box.findData(number)))
        self.setWindowTitle(f"Device test — {self.target_box.currentText()}")

    def _pane(self, number: int | None):  # noqa: ANN202
        return next((p for p in self.main.network_tabs.panes
                     if p.property("connectionNumber") == number and p.remote is not None), None)

    def run(self) -> bool:
        """Test the chosen connection on a worker thread; the results replace the table."""
        pane = self._pane(self.connection)
        if pane is None:
            self.refresh_targets()
            self.summary_label.setText("Connect to a device in the Network tab first. " + NOTE)
            return False
        self._run_count += 1
        if not self._worker.start_managed(("test", self._run_count), None, device_test.run, pane.service, pane.remote):
            return False
        self.setWindowTitle(f"Device test — {self.target_box.currentText()}")
        self.again_button.setEnabled(False)
        self.tree.clear()
        self.summary_label.setText(f"Testing {self.target_box.currentText()}… " + NOTE)
        return True

    def _show(self, key: tuple, results: list[device_test.Result] | None, message: str = "") -> None:
        if key[1] != self._run_count:
            return
        self.again_button.setEnabled(True)
        if results is None:
            self.summary_label.setText(f"The test stopped: {message}. " + NOTE)
            return
        finished = time.strftime("%H:%M:%S")
        self.summary_label.setText(f"{device_test.summary(results)}, finished {finished}. {NOTE}")
        for result in results:
            item = QTreeWidgetItem([result.result, result.test, result.happened, result.standard])
            item.setToolTip(2, result.happened)
            if result.result == device_test.PASSED:
                item.setForeground(0, token("kind/mdib"))
            elif result.result == device_test.FAILED:
                item.setForeground(0, token("text/error"))
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            else:
                item.setForeground(0, muted_colour(self.tree))
            self.tree.addTopLevelItem(item)
