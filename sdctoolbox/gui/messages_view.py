"""The Messages section of a Network tab: the SOAP messages of one connection, and the XML of one."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..message_log import KEEP, Message, MessageLog
from .no_wheel import NoWheelComboBox
from .styling import apply_row_selection_style, mute, set_button_kind
from .table_columns import TableColumns

COLUMNS = ["Time", "Message", "Direction", "Size"]
COL_TIME, COL_MESSAGE, COL_DIRECTION, COL_SIZE = range(len(COLUMNS))
# Caption -> which messages it shows.
SHOW = [
    ("All messages", lambda m: True),
    ("Without waveforms", lambda m: not m.is_waveform),
    ("Requests and responses", lambda m: m.kind != "report"),
    ("Reports", lambda m: m.kind == "report"),
]
POLL_MS = 500


def clock(seconds: float) -> str:
    """14:02:15.120: when a message went over the wire, to the millisecond."""
    return time.strftime("%H:%M:%S", time.localtime(seconds)) + f".{int(seconds * 1000) % 1000:03d}"


class MessagesView(QWidget):
    """Lists the messages of the connection set with `set_log`, newest last, and shows the selected one."""

    count_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._log: MessageLog | None = None
        self._last = 0  # the number of the newest message looked at
        self._shown: list[Message] = []  # the table's rows

        self.show_box = NoWheelComboBox()
        for caption, _ in SHOW:
            self.show_box.addItem(caption)
        self.show_box.currentIndexChanged.connect(self._refill)
        self.copy_button = QPushButton("Copy XML")
        self.copy_button.clicked.connect(self.copy_selected)
        self.save_button = QPushButton("Save all…")
        self.save_button.clicked.connect(self._on_save)
        for button in (self.copy_button, self.save_button):
            set_button_kind(button, "design-outline")
        self.pause_box = QCheckBox("Pause")
        self.pause_box.setToolTip("Keep the list still to read it; messages are still recorded and appear on untick")
        self.pause_box.toggled.connect(lambda paused: None if paused else self.poll())
        tools = QHBoxLayout()
        tools.setContentsMargins(0, 0, 0, 0)
        tools.addWidget(QLabel("Show"))
        tools.addWidget(self.show_box)
        tools.addWidget(self.copy_button)
        tools.addWidget(self.save_button)
        tools.addWidget(self.pause_box)
        tools.addStretch(1)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(False)
        self._columns = TableColumns(self.table, flexible=COL_MESSAGE)
        apply_row_selection_style(self.table)
        self.table.itemSelectionChanged.connect(self._show_selected)

        self.caption = QLabel("Select a message to read its XML.")
        mute(self.caption)
        self.xml_view = QPlainTextEdit()
        self.xml_view.setReadOnly(True)
        self.xml_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.xml_view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        reader = QWidget()
        reader_layout = QVBoxLayout(reader)
        reader_layout.setContentsMargins(0, 0, 0, 0)
        reader_layout.addWidget(self.caption)
        reader_layout.addWidget(self.xml_view, 1)

        # Drag the handle to give the list or the XML more room.
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(reader)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(tools)
        layout.addWidget(self.splitter, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.poll)
        self._update_buttons()

    def set_log(self, log: MessageLog | None) -> None:
        """Show this connection's messages (None: no connection)."""
        self._log = log
        self._last = 0
        self._refill()
        if log is None:
            self._timer.stop()
        else:
            self._timer.start()

    def _keep(self, message: Message) -> bool:
        return SHOW[self.show_box.currentIndex()][1](message)

    def _refill(self) -> None:
        self.table.setRowCount(0)
        self._shown = []
        self._last = 0
        self.poll(force=True)
        self._show_selected()

    def poll(self, *, force: bool = False) -> None:
        """Add the messages that arrived since the last look."""
        if self._log is None:
            self.count_changed.emit(0)
            return
        if self.pause_box.isChecked() and not force:
            return
        new = self._log.messages(self._last)
        if new:
            self._last = new[-1].number
            self._append([message for message in new if self._keep(message)])
        self.count_changed.emit(self._log.total)
        self._update_buttons()

    def _append(self, messages: list[Message]) -> None:
        if not messages:
            return
        bar = self.table.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 2
        self.table.setUpdatesEnabled(False)
        for message in messages:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, text in enumerate((clock(message.time), message.summary, message.direction, message.size)):
                item = QTableWidgetItem(text)
                if column == COL_SIZE:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, column, item)
            self._shown.append(message)
        overflow = len(self._shown) - KEEP
        if overflow > 0:
            position = bar.value()
            for _ in range(overflow):
                self.table.removeRow(0)
            del self._shown[:overflow]
            if not at_end:
                # The rows being read move up as old ones drop off; keep them in view.
                bar.setValue(max(0, position - overflow))
        self.table.setUpdatesEnabled(True)
        self._columns.refit()
        if at_end:
            self.table.scrollToBottom()

    def selected(self) -> Message | None:
        rows = self.table.selectionModel().selectedRows()
        return self._shown[rows[0].row()] if rows else None

    def _show_selected(self) -> None:
        message = self.selected()
        if message is None:
            self.caption.setText("Select a message to read its XML.")
            self.xml_view.clear()
        else:
            self.caption.setText(
                f"{message.action}, {message.direction} at {clock(message.time)} ({message.size})",
            )
            self.xml_view.setPlainText(message.pretty())
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.copy_button.setEnabled(self.selected() is not None)
        self.save_button.setEnabled(self._log is not None and self._log.total > 0)

    def copy_selected(self) -> None:
        message = self.selected()
        if message is not None:
            QApplication.clipboard().setText(message.pretty())

    def save(self, path: str | Path) -> int:
        """Write every kept message, oldest first, as text; returns how many."""
        messages = self._log.messages() if self._log is not None else []
        with Path(path).open("w", encoding="utf-8") as output:
            for message in messages:
                output.write(f"== {clock(message.time)}  {message.direction}  {message.summary}  ({message.size})\n")
                output.write(message.pretty().rstrip() + "\n\n")
        return len(messages)

    def _on_save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save messages", "messages.txt", "Text files (*.txt)")
        if not path:
            return
        try:
            self.save(path)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save messages", str(exc))
