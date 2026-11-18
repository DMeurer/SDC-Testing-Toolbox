"""The activity log: the last things that happened, in a window beside the main one.

It records what someone testing a device wants to look back on: a value set by remote
control, an alarm raised, acknowledged, latching, stopped latching or cleared, and a
connected device changing its MDIB structure. Metric values that change on their own are
left out on purpose; a generator updates them several times a second.

Everything here runs on the GUI thread. Entries come from Qt slots, never from SDC threads.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types

from ..mdib_io import live_entities
from ..native_mdib import label
from .styling import error_colour, mute, set_button_kind

if TYPE_CHECKING:
    from collections.abc import Callable

    from .qt_bridge import MdibBridge

MAX_ENTRIES = 500
# More structural changes than this in one report become one summary line.
MAX_DETAILED_CHANGES = 5
CONDITION_STATES = frozenset({pm.AlertConditionState, pm.LimitAlertConditionState})
SIGNAL = pm_types.AlertSignalPresence


@dataclass(frozen=True)
class Entry:
    """One line of the log."""

    time: str
    device: str
    text: str
    alarm: bool = False


class ActivityLog(QObject):
    """The last MAX_ENTRIES entries of this process, oldest first."""

    added = Signal(object)
    cleared = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.entries: deque[Entry] = deque(maxlen=MAX_ENTRIES)

    def add(self, device: str, text: str, *, alarm: bool = False) -> None:
        entry = Entry(datetime.now().strftime("%H:%M:%S"), device, text, alarm)
        self.entries.append(entry)
        self.added.emit(entry)

    def clear(self) -> None:
        self.entries.clear()
        self.cleared.emit()


_log: ActivityLog | None = None


def activity_log() -> ActivityLog:
    """The one log every pane writes to."""
    global _log  # noqa: PLW0603 - one log per process, created on first use
    if _log is None:
        _log = ActivityLog()
    return _log


class MdibActivity(QObject):
    """Turns one MDIB's alert and structure reports into log entries.

    Alert reports carry states, not events, so this remembers each alert's last presence
    and logs the difference. Seeded from the MDIB, so alarms already raised on connecting
    are not reported as new.
    """

    def __init__(self, bridge: MdibBridge, mdib, device: str | Callable[[], str], *,  # noqa: ANN001
                 structure: bool) -> None:
        super().__init__(bridge)
        self._mdib = mdib
        self._device = device
        self._presence: dict[str, object] = {}
        # A stand-in MDIB without a lock (as the GUI tests use) has nothing to seed from.
        if hasattr(mdib, "mdib_lock"):
            with mdib.mdib_lock:
                for entity in live_entities(mdib):
                    if entity.state is not None and hasattr(entity.state, "Presence"):
                        self._presence[entity.handle] = entity.state.Presence
        bridge.alerts_changed.connect(self._on_alerts)
        if structure:
            bridge.descriptors_added.connect(lambda found: self._on_structure(found, "added"))
            bridge.descriptors_updated.connect(lambda found: self._on_structure(found, "changed"))
            bridge.descriptors_deleted.connect(lambda found: self._on_structure(found, "removed"))

    def _name(self) -> str:
        return self._device() if callable(self._device) else self._device

    def _label(self, handle: str) -> str:
        with self._mdib.mdib_lock:
            descriptor = self._mdib.descriptions.handle.get_one(handle, allow_none=True)
            name = label(descriptor) if descriptor is not None else handle
        return name if name == handle else f"{name} ({handle})"

    def _condition_of(self, signal_handle: str) -> str:
        with self._mdib.mdib_lock:
            descriptor = self._mdib.descriptions.handle.get_one(signal_handle, allow_none=True)
            return getattr(descriptor, "ConditionSignaled", None) or signal_handle

    def _on_alerts(self, states: dict) -> None:
        # One line per condition and event, however many signals announce it.
        events: dict[tuple[str, str], bool] = {}
        for handle, state in states.items():
            presence = getattr(state, "Presence", None)
            previous = self._presence.get(handle)
            self._presence[handle] = presence
            if presence is None or presence == previous:
                continue
            if state.NODETYPE in CONDITION_STATES:
                if presence:
                    events[(handle, "Alarm raised")] = True
                elif previous:
                    events[(handle, "Alarm cleared")] = False
            elif presence == SIGNAL.ACK:
                events[(self._condition_of(handle), "Alarm acknowledged")] = False
            elif presence == SIGNAL.LATCH:
                events[(self._condition_of(handle), "Alarm latching")] = False
            elif presence == SIGNAL.OFF and previous == SIGNAL.LATCH:
                events[(self._condition_of(handle), "Alarm stopped latching")] = False
        for (handle, what), alarm in events.items():
            activity_log().add(self._name(), f"{what}: {self._label(handle)}", alarm=alarm)

    def _on_structure(self, descriptors: dict, change: str) -> None:
        if len(descriptors) > MAX_DETAILED_CHANGES:
            activity_log().add(self._name(), f"MDIB changed: {len(descriptors)} descriptors {change}")
            return
        for handle, descriptor in descriptors.items():
            node_type = getattr(getattr(descriptor, "NODETYPE", None), "localname", "")
            kind = node_type.removesuffix("Descriptor") or "descriptor"
            activity_log().add(self._name(), f"MDIB changed: {kind} {handle} {change}")


class ActivityLogWindow(QWidget):
    """The log as its own top-level window, so it can sit beside the main window."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        # A parented Qt.Window stays a separate, non-modal window and closes with its parent.
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(title)
        self.resize(760, 360)
        self.log = activity_log()

        hint = QLabel(f"Last actions on this device and on connected devices. Newest at the bottom; "
                      f"keeps the last {MAX_ENTRIES}.")
        hint.setWordWrap(True)
        mute(hint)
        self.clear_button = QPushButton("Clear")
        set_button_kind(self.clear_button, "design-outline")
        self.clear_button.clicked.connect(self.log.clear)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Time", "Device", "What happened"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setColumnWidth(0, 70)
        self.tree.setColumnWidth(1, 120)

        top = QHBoxLayout()
        top.addWidget(hint, 1)
        top.addWidget(self.clear_button)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.tree, 1)

        for entry in self.log.entries:
            self._append(entry)
        self.log.added.connect(self._append)
        self.log.cleared.connect(self.tree.clear)

    def _append(self, entry: Entry) -> None:
        item = QTreeWidgetItem([entry.time, entry.device, entry.text])
        if entry.alarm:
            item.setForeground(2, error_colour(self))
        self.tree.addTopLevelItem(item)
        while self.tree.topLevelItemCount() > MAX_ENTRIES:
            self.tree.takeTopLevelItem(0)
        self.tree.scrollToItem(item)
