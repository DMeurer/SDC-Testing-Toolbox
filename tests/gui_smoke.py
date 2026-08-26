"""Smoke test for the graphical provider side.

Builds the real window with Qt's offscreen backend and drives the actual widgets, so this
catches wiring mistakes that a test against the service layer alone would miss.

The last group is the one that matters most: it writes a value from a background thread, the
way a remote consumer does, and checks the table picks it up. That is the whole reason
MdibBridge exists.

Exit code 0 means all checks passed.

Usage:  .venv/Scripts/python.exe tests/gui_smoke.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path

# Must be set before QApplication is created, otherwise Qt wants a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
)

from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import pm_types  # noqa: E402

import run_toolbox  # noqa: E402
from sdctoolbox import constants  # noqa: E402
from sdctoolbox.constants import CODE_DIMENSIONLESS, epr_for  # noqa: E402
from sdctoolbox.gui.consumer_pane import COL_RANGE as COL_R_RANGE  # noqa: E402
from sdctoolbox.gui.consumer_pane import COL_VALUE as COL_R_VALUE  # noqa: E402
from sdctoolbox.gui.consumer_pane import COL_WRITABLE as COL_R_WRITABLE  # noqa: E402
from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.gui.new_alert_dialog import NewAlertDialog  # noqa: E402
from sdctoolbox.gui.new_metric_dialog import NewMetricDialog  # noqa: E402
from sdctoolbox.gui.provider_pane import (  # noqa: E402
    ACOL_HANDLE,
    ACOL_SIGNALS,
    COL_CONTROL,
    COL_HANDLE,
    COL_KIND,
    COL_LABEL,
    COL_RANGE,
    COL_UNIT,
    COL_VALUE,
    NO_VALUE,
)
from sdctoolbox.gui.startup_dialog import LINK_LOCAL_PREFIX, StartupDialog  # noqa: E402
from sdctoolbox.gui.styling import mute  # noqa: E402
from sdctoolbox.model import AlertKind, AlertPriority, AlertSpec, MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

# This file lives in tests/, so its own directory is on the path.
from acceptance_provider import PEER_INSTANCE  # noqa: E402


class Report:
    """Collects pass/fail results and prints them as they happen."""

    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, description: str, detail: str = "") -> bool:  # noqa: FBT001
        self.checks += 1
        if not ok:
            self.failures += 1
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {description}{suffix}", flush=True)
        return ok

    def summary(self) -> int:
        print("-" * 74)
        if self.failures:
            print(f"RESULT: {self.failures} of {self.checks} checks FAILED")
            return 1
        print(f"RESULT: all {self.checks} checks passed")
        return 0


def pump(app: QApplication, seconds: float = 0.4) -> None:
    """Let Qt deliver queued signals, including those emitted from other threads."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def wait_for(app: QApplication, predicate, timeout: float = 30.0) -> bool:  # noqa: ANN001
    """Pump the event loop until predicate() is true or the time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.05)
    return predicate()


def cell_of(table, handle: str, column: int) -> str | None:  # noqa: ANN001
    """Text of one cell, found by the handle in the first column."""
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item is not None and item.text() == handle:
            got = table.item(row, column)
            return got.text() if got else None
    return None


def fill_dialog(
    dialog: NewMetricDialog,
    *,
    kind: MetricKind,
    label: str,
    unit: str = "",
    values: str = "",
    resolution: str = "",
    minimum: str = "",
    maximum: str = "",
    controllable: bool = True,
) -> None:
    """Set the dialog's widgets as a user would."""
    for index in range(dialog.kind_box.count()):
        if dialog.kind_box.itemData(index) is kind:
            dialog.kind_box.setCurrentIndex(index)
            break
    dialog.label_edit.setText(label)
    dialog.unit_edit.setText(unit)
    dialog.values_edit.setText(values)
    dialog.resolution_edit.setText(resolution)
    dialog.minimum_edit.setText(minimum)
    dialog.maximum_edit.setText(maximum)
    dialog.controllable_box.setChecked(controllable)


def row_for(pane, handle: str) -> int | None:  # noqa: ANN001
    for row in range(pane.table.rowCount()):
        item = pane.table.item(row, COL_HANDLE)
        if item is not None and item.text() == handle:
            return row
    return None


def cell(pane, handle: str, column: int) -> str | None:  # noqa: ANN001
    row = row_for(pane, handle)
    if row is None:
        return None
    item = pane.table.item(row, column)
    return item.text() if item else None


def main() -> int:  # noqa: PLR0915 - a linear test reads better in one piece
    basic_logging_setup(level=logging.WARNING)
    report = Report()

    # A modal dialog would block forever with no one to click it. Record instead of showing.
    shown_warnings: list[tuple[str, str]] = []

    def fake_warning(_parent, title, text, *_args, **_kwargs):  # noqa: ANN001, ANN202
        shown_warnings.append((title, text))
        return QMessageBox.StandardButton.Ok

    def fake_question(_parent, _title, _text, *_args, **_kwargs):  # noqa: ANN001, ANN202
        return QMessageBox.StandardButton.Yes

    QMessageBox.warning = staticmethod(fake_warning)
    QMessageBox.question = staticmethod(fake_question)

    print("=" * 74)
    print("GUI smoke test (offscreen)")
    print("=" * 74)

    app = QApplication(sys.argv)
    service = ProviderService(instance_name="gui-smoke")
    service.start()

    try:
        window = MainWindow(service)
        window.show()
        pump(app)
        pane = window.provider_pane

        print("\n1. Window layout")
        report.check(window.split_view_enabled, "starts split")
        report.check(window.splitter.count() == 2, "two panels", str(window.splitter.count()))  # noqa: PLR2004
        report.check(
            window.splitter.orientation() == Qt.Horizontal,
            "side by side, divided vertically",
        )
        report.check(
            not window.splitter.childrenCollapsible(),
            "neither panel can be dragged shut",
        )
        report.check(window.provider_panel.heading.isVisible(), "panels carry a heading when split")
        report.check(pane.table.rowCount() == 0, "table starts empty")
        report.check(not pane.remove_button.isEnabled(), "remove is disabled with no selection")
        report.check(not pane.apply_button.isEnabled(), "editor is disabled with no selection")

        divider_before = window.splitter.sizes()
        window.splitter.setSizes([divider_before[0] + 120, max(80, divider_before[1] - 120)])
        pump(app)
        moved_sizes = window.splitter.sizes()
        report.check(
            moved_sizes[0] > divider_before[0],
            "the divider can be moved",
            f"{divider_before} -> {moved_sizes}",
        )

        print("\n1b. Menu bar")
        menu_bar = window.menuBar()
        report.check(menu_bar.isVisible(), "the menu bar is on screen by default")
        report.check(window.always_show_menu, "because Always show menu bar starts on")
        report.check(
            "Alt" not in window.statusBar().currentMessage(),
            "so the status bar does not mention Alt",
            window.statusBar().currentMessage()[-40:],
        )
        titles = [action.text() for action in menu_bar.actions()]
        report.check(titles == ["&File", "&View"], "File and View menus", str(titles))
        file_items = [a.text() for a in menu_bar.actions()[0].menu().actions()]
        view_items = [a.text() for a in menu_bar.actions()[1].menu().actions() if a.text()]
        report.check(
            [t for t in file_items if t] == ["&Import config\u2026", "&Export config\u2026", "E&xit"],
            "File has import, export and exit",
            str(file_items),
        )
        report.check(
            view_items == ["Always show &menu bar", "Use &widgets if possible", "&Split view"],
            "View has all three toggles",
            str(view_items),
        )
        report.check(
            bool(window.exit_action.shortcut().toString()),
            "Exit has a working shortcut",
            window.exit_action.shortcut().toString(),
        )

        window.toggle_menu_bar()
        pump(app)
        report.check(
            menu_bar.isVisible(),
            "Alt does nothing while the bar is pinned, rather than hiding it",
        )

        window.always_show_menu_action.setChecked(False)
        pump(app)
        report.check(not menu_bar.isVisible(), "unpinning hides it")
        report.check(
            "Alt" in window.statusBar().currentMessage(),
            "and the status bar says how to get it back",
        )
        window.toggle_menu_bar()
        pump(app)
        report.check(menu_bar.isVisible(), "Alt reveals it")
        window.toggle_menu_bar()
        pump(app)
        report.check(not menu_bar.isVisible(), "and Alt puts it away again")

        window.always_show_menu_action.setChecked(True)
        pump(app)
        report.check(menu_bar.isVisible(), "re-pinning brings it back")
        window._maybe_hide_menu_bar()  # noqa: SLF001
        pump(app)
        report.check(
            menu_bar.isVisible(),
            "and closing a menu no longer hides it",
        )

        print("\n1c. Switching layout")
        report.check(window.split_view_action.isCheckable(), "split view is a checkbox")
        report.check(window.split_view_action.isChecked(), "and starts on")

        window.split_view_action.setChecked(False)
        pump(app)
        report.check(not window.split_view_enabled, "unchecking switches to tabs")
        report.check(window.tabs.count() == 2, "both panels became tabs", str(window.tabs.count()))  # noqa: PLR2004
        report.check(
            [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["My device", "Network"],
            "tabs are named after the panels",
        )
        # QTabWidget shows only its current page. Forcing both visible on the way in draws
        # them on top of each other until the first tab switch takes the visibility back.
        visible_panels = [
            panel.title
            for panel in (window.provider_panel, window.network_panel)
            if panel.isVisible()
        ]
        report.check(
            len(visible_panels) == 1,
            "exactly one panel is visible in tab mode, not both stacked",
            str(visible_panels),
        )
        report.check(
            window.tabs.currentIndex() == 0,
            "and the first tab is the current one",
            str(window.tabs.currentIndex()),
        )
        report.check(
            window.provider_panel.isVisible() and not window.network_panel.isVisible(),
            "the visible one is the current tab's panel",
        )

        window.tabs.setCurrentIndex(1)
        pump(app)
        report.check(
            window.network_panel.isVisible() and not window.provider_panel.isVisible(),
            "switching tabs swaps which panel shows",
        )
        window.tabs.setCurrentIndex(0)
        pump(app)

        report.check(
            not window.provider_panel.heading.isVisible(),
            "the heading is dropped in tab mode, the tab already says it",
        )
        report.check(
            window.provider_pane.table.rowCount() == 0,
            "the provider pane survived the move",
        )

        window.split_view_action.setChecked(True)
        pump(app)
        report.check(window.split_view_enabled, "checking switches back to split")
        report.check(window.splitter.count() == 2, "both panels are back in the splitter")
        report.check(window.tabs.count() == 0, "and no longer in the tab widget")
        report.check(window.provider_panel.heading.isVisible(), "headings come back")
        report.check(
            window.splitter.sizes()[0] > divider_before[0],
            "the divider returns where it was left",
            f"{moved_sizes} -> {window.splitter.sizes()}",
        )

        print("\n2. Dialog validation")
        dialog = NewMetricDialog()
        fill_dialog(dialog, kind=MetricKind.NUMBER, label="")
        dialog._on_accept()  # noqa: SLF001 - driving the slot is the point
        report.check(dialog.spec() is None, "empty label is refused")
        # isVisible() is False for any child of an unshown window, so ask isHidden() instead.
        report.check(
            not dialog.error_label.isHidden() and bool(dialog.error_label.text()),
            "an error message is shown",
            dialog.error_label.text(),
        )

        fill_dialog(dialog, kind=MetricKind.CHOICE, label="Mode", values="ONLY_ONE")
        dialog._on_accept()  # noqa: SLF001
        report.check(dialog.spec() is None, "choice with one value is refused")

        fill_dialog(dialog, kind=MetricKind.CHOICE, label="Mode", values="A, B, A")
        dialog._on_accept()  # noqa: SLF001
        report.check(dialog.spec() is None, "choice with duplicate values is refused")

        fill_dialog(dialog, kind=MetricKind.NUMBER, label="Zoom", resolution="abc")
        dialog._on_accept()  # noqa: SLF001
        report.check(dialog.spec() is None, "non-numeric resolution is refused")

        fill_dialog(dialog, kind=MetricKind.NUMBER, label="Zoom", minimum="oops")
        dialog._on_accept()  # noqa: SLF001
        report.check(dialog.spec() is None, "non-numeric minimum is refused")

        fill_dialog(dialog, kind=MetricKind.NUMBER, label="Zoom", minimum="100", maximum="1")
        dialog._on_accept()  # noqa: SLF001
        report.check(dialog.spec() is None, "minimum above maximum is refused")

        fill_dialog(dialog, kind=MetricKind.NUMBER, label="Zoom level", unit="steps", minimum="1", maximum="100")
        report.check(
            dialog.handle_preview.text() == "m.zoom_level",
            "handle preview follows the label",
            dialog.handle_preview.text(),
        )
        dialog._on_accept()  # noqa: SLF001
        spec = dialog.spec()
        report.check(spec is not None, "a valid definition is accepted")
        report.check(
            spec is not None and (spec.minimum, spec.maximum) == (Decimal("1"), Decimal("100")),
            "the range reached the spec",
            f"{spec.minimum} to {spec.maximum}" if spec else "-",
        )

        print("\n2b. Inapplicable inputs are hidden, not greyed out")
        for index in range(dialog.kind_box.count()):
            if dialog.kind_box.itemText(index) == "Choice":
                dialog.kind_box.setCurrentIndex(index)
        dialog._on_kind_changed()  # noqa: SLF001
        report.check(not dialog.values_edit.isHidden(), "a choice shows its allowed values")
        report.check(dialog.resolution_edit.isHidden(), "a choice hides resolution")
        report.check(dialog.limits_widget.isHidden(), "a choice hides the range")

        for index in range(dialog.kind_box.count()):
            if dialog.kind_box.itemText(index) == "Number":
                dialog.kind_box.setCurrentIndex(index)
        dialog._on_kind_changed()  # noqa: SLF001
        report.check(dialog.values_edit.isHidden(), "a number hides allowed values")
        report.check(not dialog.resolution_edit.isHidden(), "a number shows resolution")
        report.check(not dialog.limits_widget.isHidden(), "a number shows the range")

        for index in range(dialog.kind_box.count()):
            if dialog.kind_box.itemText(index) == "Text":
                dialog.kind_box.setCurrentIndex(index)
        dialog._on_kind_changed()  # noqa: SLF001
        report.check(
            dialog.values_edit.isHidden()
            and dialog.resolution_edit.isHidden()
            and dialog.limits_widget.isHidden(),
            "text hides all three",
        )
        dialog.deleteLater()

        print("\n2c. The alarm dialog")
        alert_metrics = {
            "m.num": MetricSpec(label="A number", kind=MetricKind.NUMBER, handle="m.num"),
            "m.txt": MetricSpec(label="Some text", kind=MetricKind.TEXT, handle="m.txt"),
        }
        alert_dialog = NewAlertDialog(alert_metrics)
        alert_dialog.label_edit.setText("Too high")
        alert_dialog.upper_edit.setText("100")
        for index in range(alert_dialog.source_box.count()):
            if alert_dialog.source_box.itemData(index) == "m.num":
                alert_dialog.source_box.setCurrentIndex(index)
        alert_dialog._on_source_changed()  # noqa: SLF001
        report.check(not alert_dialog.limits_widget.isHidden(), "a numeric source shows the limits")
        alert_dialog._on_accept()  # noqa: SLF001
        alert_spec = alert_dialog.spec()
        report.check(
            alert_spec is not None and alert_spec.upper_limit == Decimal("100"),
            "and the limit reaches the spec",
            str(alert_spec.upper_limit) if alert_spec else "-",
        )
        # The enums BICEPS defines subclass str, so a QVariant round trip hands back a
        # plain str. Anything downstream then refuses it.
        report.check(
            alert_spec is not None and isinstance(alert_spec.kind, AlertKind),
            "kind is a real enum, not the string Qt hands back",
            type(alert_spec.kind).__name__ if alert_spec else "-",
        )
        report.check(
            alert_spec is not None and isinstance(alert_spec.priority, AlertPriority),
            "and so is priority",
            type(alert_spec.priority).__name__ if alert_spec else "-",
        )

        alert_dialog2 = NewAlertDialog(alert_metrics)
        alert_dialog2.label_edit.setText("Manual")
        alert_dialog2.upper_edit.setText("100")
        for index in range(alert_dialog2.source_box.count()):
            if alert_dialog2.source_box.itemData(index) == "m.txt":
                alert_dialog2.source_box.setCurrentIndex(index)
        alert_dialog2._on_source_changed()  # noqa: SLF001
        report.check(alert_dialog2.limits_widget.isHidden(), "a text source hides the limits")
        alert_dialog2._on_accept()  # noqa: SLF001
        report.check(
            alert_dialog2.spec() is not None and alert_dialog2.spec().upper_limit is None,
            "and a limit typed before switching is not smuggled through",
            str(alert_dialog2.spec().upper_limit) if alert_dialog2.spec() else "-",
        )
        alert_dialog.deleteLater()
        alert_dialog2.deleteLater()

        print("\n3. Creating data sources")
        zoom = service.add_metric(spec)
        pane.refresh()
        pump(app)
        report.check(row_for(pane, zoom) is not None, f"{zoom} has a row")
        report.check(cell(pane, zoom, COL_KIND) == "number", "kind column")
        report.check(cell(pane, zoom, COL_VALUE) == NO_VALUE, "a metric without a value shows a dash")

        dialog = NewMetricDialog()
        fill_dialog(dialog, kind=MetricKind.CHOICE, label="Mode", values="IDLE, RUN, PAUSE")
        dialog._on_accept()  # noqa: SLF001
        mode = service.add_metric(dialog.spec())
        dialog.deleteLater()
        pane.refresh()
        pump(app)
        report.check(cell(pane, mode, COL_VALUE) == "IDLE", "choice starts on its first value")

        print("\n4. Editing through the widgets")
        pane.select_handle(zoom)
        pump(app)
        report.check(pane.selected_handle() == zoom, "selection follows")
        report.check(pane.apply_button.isEnabled(), "editor enabled once a row is selected")
        report.check(pane.editor_stack.currentIndex() == 0, "a number gets the text editor")

        pane.value_edit.setText("42")
        pane._on_apply()  # noqa: SLF001
        pump(app)
        report.check(cell(pane, zoom, COL_VALUE) == "42", "value set from the editor", str(cell(pane, zoom, COL_VALUE)))

        pane.select_handle(mode)
        pump(app)
        report.check(pane.editor_stack.currentIndex() == 1, "a choice gets the combo box")
        report.check(
            [pane.choice_box.itemText(i) for i in range(pane.choice_box.count())] == ["IDLE", "RUN", "PAUSE"],
            "combo box is filled from AllowedValue",
        )
        pane.choice_box.setCurrentIndex(1)
        pane._on_apply()  # noqa: SLF001
        pump(app)
        report.check(cell(pane, mode, COL_VALUE) == "RUN", "choice set from the combo box")

        print("\n5. Remote control checkbox")
        row = row_for(pane, zoom)
        item = pane.table.item(row, COL_CONTROL)
        report.check(item.checkState() == Qt.Checked, "control starts enabled")

        item.setCheckState(Qt.Unchecked)
        pump(app)
        operation = service.operation_handle_for(zoom)
        state = service.mdib.entities.by_handle(operation).state
        report.check(
            state.OperatingMode == pm_types.OperatingMode.DISABLED,
            "unchecking disables the operation",
            str(state.OperatingMode),
        )

        item = pane.table.item(row_for(pane, zoom), COL_CONTROL)
        item.setCheckState(Qt.Checked)
        pump(app)
        state = service.mdib.entities.by_handle(operation).state
        report.check(
            state.OperatingMode == pm_types.OperatingMode.ENABLED,
            "checking enables it again",
            str(state.OperatingMode),
        )

        print("\n6. Updates from another thread (what MdibBridge is for)")
        done = threading.Event()
        worker_thread_name: list[str] = []

        def write_from_worker() -> None:
            worker_thread_name.append(threading.current_thread().name)
            service.set_value(zoom, Decimal("88"))  # inside the metric's 1..100 range
            done.set()

        worker = threading.Thread(target=write_from_worker, name="pretend-sco-worker")
        worker.start()
        done.wait(timeout=10)
        worker.join(timeout=10)
        pump(app, seconds=1.5)

        report.check(
            worker_thread_name and worker_thread_name[0] != threading.main_thread().name,
            "the write really came from another thread",
            worker_thread_name[0] if worker_thread_name else "?",
        )
        report.check(
            cell(pane, zoom, COL_VALUE) == "88",
            "table shows a value written off the GUI thread",
            str(cell(pane, zoom, COL_VALUE)),
        )

        print("\n7. Runtime creation and removal")
        late = service.add_metric(
            type(spec)(label="Late arrival", kind=MetricKind.NUMBER, controllable=True),
        )
        pump(app, seconds=1.0)
        report.check(row_for(pane, late) is not None, "a source added at runtime appears without a manual refresh")

        service.remove_metric(late)
        pump(app, seconds=1.0)
        report.check(row_for(pane, late) is None, "and disappears when removed")

        print("\n8. Units")
        report.check(
            cell(pane, zoom, COL_UNIT) == "steps",
            "a unit is shown when there is one",
            str(cell(pane, zoom, COL_UNIT)),
        )
        report.check(
            cell(pane, mode, COL_UNIT) == "",
            "a dimensionless metric leaves the unit cell empty",
            repr(cell(pane, mode, COL_UNIT)),
        )
        unit_descriptor = service.mdib.entities.by_handle(mode).descriptor.Unit
        report.check(
            unit_descriptor is not None and unit_descriptor.Code == CODE_DIMENSIONLESS,
            "but the descriptor still carries MDC_DIM_DIMLESS",
            str(getattr(unit_descriptor, "Code", None)),
        )
        report.check(
            not (unit_descriptor.ConceptDescription or []),
            "and no invented concept description",
        )

        print("\n8b. Range")
        report.check(
            cell(pane, zoom, COL_RANGE) == "1 to 100",
            "the range column shows the limits",
            str(cell(pane, zoom, COL_RANGE)),
        )
        report.check(
            cell(pane, mode, COL_RANGE) == "",
            "an unbounded metric leaves the range cell empty",
            repr(cell(pane, mode, COL_RANGE)),
        )

        descriptor = service.mdib.entities.by_handle(zoom).descriptor
        technical = (descriptor.TechnicalRange or [None])[0]
        report.check(
            technical is not None and (technical.Lower, technical.Upper) == (Decimal("1"), Decimal("100")),
            "TechnicalRange written on the metric descriptor",
            f"{getattr(technical, 'Lower', None)} to {getattr(technical, 'Upper', None)}",
        )
        operation_state = service.mdib.entities.by_handle(service.operation_handle_for(zoom)).state
        allowed = (operation_state.AllowedRange or [None])[0]
        report.check(
            allowed is not None and (allowed.Lower, allowed.Upper) == (Decimal("1"), Decimal("100")),
            "AllowedRange written on the operation state",
            f"{getattr(allowed, 'Lower', None)} to {getattr(allowed, 'Upper', None)}",
        )

        pane.select_handle(zoom)
        pump(app)
        report.check(
            "1 to 100" in pane.editor_label.text(),
            "the editor names the range",
            pane.editor_label.text(),
        )

        current = service.get_value(zoom)
        shown_warnings.clear()
        for attempt in ("0", "101"):
            pane.value_edit.setText(attempt)
            pane._on_apply()  # noqa: SLF001
            pump(app)
            report.check(
                service.get_value(zoom) == current,
                f"setting {attempt} locally is refused",
                str(service.get_value(zoom)),
            )
        report.check(
            len(shown_warnings) == 2,  # noqa: PLR2004
            "and the user is told why",
            "; ".join(title for title, _ in shown_warnings),
        )
        pane.value_edit.setText("100")
        pane._on_apply()  # noqa: SLF001
        pump(app)
        report.check(
            service.get_value(zoom) == Decimal("100"),
            "the boundary value itself is accepted",
            str(service.get_value(zoom)),
        )

        print("\n8c. Alarm signals in the pane")
        acked = service.add_alert(
            AlertSpec(
                label="Zoom too high",
                source_handle=zoom,
                upper_limit=Decimal("90"),
                delegable=True,
            ),
        )
        pane.refresh_alerts()
        pump(app)
        pane.select_alert_handle(acked)
        pump(app)

        def signals_cell() -> str:
            for row in range(pane.alert_table.rowCount()):
                if pane.alert_table.item(row, ACOL_HANDLE).text() == acked:
                    return pane.alert_table.item(row, ACOL_SIGNALS).text()
            return ""

        # zoom sits at 100 from the range section above, so the alarm is already raised.
        report.check(service.alert_present(acked), "the alarm is raised by the value already set")
        report.check(
            signals_cell() == "Vis:On  Aud:On",
            "the signals column shows both signals",
            signals_cell(),
        )
        report.check(pane.acknowledge_button.isEnabled(), "Acknowledge is offered while raised")
        pane._on_acknowledge()  # noqa: SLF001
        pump(app)
        report.check(signals_cell() == "Vis:Ack  Aud:Ack", "acknowledging updates the cell", signals_cell())
        report.check(
            service.alert_present(acked),
            "and leaves the condition present, because Ack is about the announcement",
        )
        report.check(
            not pane.acknowledge_button.isEnabled(),
            "Acknowledge switches off once there is nothing left to acknowledge",
        )

        report.check(
            pane.delegate_button.isEnabled() and pane.delegate_button.text() == "Delegate",
            "Delegate is offered for a delegable alarm",
            pane.delegate_button.text(),
        )
        pane._on_delegate()  # noqa: SLF001
        pump(app)
        report.check("->Rem" in signals_cell(), "delegating shows in the cell", signals_cell())
        report.check(
            pane.delegate_button.text() == "Take back",
            "and the button offers the way back",
            pane.delegate_button.text(),
        )
        pane._on_delegate()  # noqa: SLF001
        pump(app)
        report.check("->Rem" not in signals_cell(), "taking it back undoes it", signals_cell())

        plain_alarm = service.add_alert(AlertSpec(label="Not delegable", source_handle=zoom))
        pane.refresh_alerts()
        pane.select_alert_handle(plain_alarm)
        pump(app)
        report.check(
            not pane.delegate_button.isEnabled(),
            "an alarm without SignalDelegationSupported cannot be delegated from the UI",
        )
        service.remove_alert(plain_alarm)
        service.remove_alert(acked)
        pane.refresh_alerts()
        pump(app)

        print("\n9. Column sizing, all four tables")
        # A table only has a width while it is the visible page.
        consumer = window.network_pane
        window.set_use_widgets(False)
        window.split_view_action.setChecked(False)
        window.resize(1100, 620)
        pump(app, seconds=0.5)

        tables = [
            ("provider metrics", pane.table, pane._columns),  # noqa: SLF001
            ("provider alarms", pane.alert_table, pane._alert_columns),  # noqa: SLF001
            ("consumer metrics", consumer.table, consumer._columns),  # noqa: SLF001
            ("consumer alarms", consumer.alert_table, consumer._alert_columns),  # noqa: SLF001
        ]

        for name, table, columns in tables:
            report.check(
                table.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff,
                f"{name}: no horizontal scrolling",
            )
            modes = {
                table.horizontalHeader().sectionResizeMode(c)
                for c in range(table.columnCount())
            }
            report.check(
                modes == {QHeaderView.Interactive},
                f"{name}: every separator is draggable",
                ", ".join(str(m) for m in modes),
            )
            report.check(
                columns.total_width() == columns.available_width(),
                f"{name}: fills the viewport exactly",
                f"{columns.total_width()} vs {columns.available_width()}",
            )

        report.check(
            pane.table.columnWidth(COL_LABEL) > pane.table.columnWidth(COL_KIND),
            "Label takes the slack while the rest stay at their contents",
            f"label={pane.table.columnWidth(COL_LABEL)} kind={pane.table.columnWidth(COL_KIND)}",
        )

        print("\n9b. A separator trades width with its neighbour")
        table, columns = pane.table, pane._columns  # noqa: SLF001
        before = [table.columnWidth(c) for c in range(table.columnCount())]
        room = before[COL_LABEL] - 110  # what Label can give before hitting its floor
        table.setColumnWidth(COL_HANDLE, before[COL_HANDLE] + 30)
        pump(app)
        after = [table.columnWidth(c) for c in range(table.columnCount())]

        moved = after[COL_HANDLE] - before[COL_HANDLE]
        report.check(moved > 0, "widening a column widens it", str(moved))
        report.check(
            after[COL_LABEL] - before[COL_LABEL] == -moved,
            "and takes exactly that from the neighbour on its right",
            f"neighbour {before[COL_LABEL]} -> {after[COL_LABEL]}",
        )
        report.check(
            after[COL_KIND:] == before[COL_KIND:],
            "leaving every other column alone",
        )
        report.check(
            columns.total_width() == columns.available_width(),
            "so the total is unchanged",
            f"{columns.total_width()} vs {columns.available_width()}",
        )
        report.check(
            moved <= room + 1,
            "and it stops when the neighbour reaches its minimum",
            f"asked 30, moved {moved}, neighbour had {room}",
        )

        last = table.columnCount() - 1
        before = [table.columnWidth(c) for c in range(table.columnCount())]
        table.setColumnWidth(last, before[last] + 80)
        pump(app)
        report.check(
            [table.columnWidth(c) for c in range(table.columnCount())] == before,
            "the last separator refuses, having nothing to its right to take from",
        )

        print("\n9c. Resizing the window keeps every table exact")
        for width in (1500, 900, 700, 1100):
            window.resize(width, 620)
            pump(app, seconds=0.35)
            for name, _table, columns in tables:
                report.check(
                    columns.total_width() == columns.available_width(),
                    f"{width}px: {name} still exact",
                    f"{columns.total_width()} vs {columns.available_width()}",
                )

        window.split_view_action.setChecked(True)
        pump(app, seconds=0.4)
        for name, _table, columns in tables:
            report.check(
                columns.total_width() == columns.available_width(),
                f"back in split view: {name} still exact",
                f"{columns.total_width()} vs {columns.available_width()}",
            )
        window.set_use_widgets(True)
        pump(app)

        print("\n10. Selection is one flat band")
        qss = pane.table.styleSheet()
        report.check("outline: 0" in qss, "focus ring suppressed")
        report.check("border: 0px" in qss, "per-cell borders suppressed")
        report.check(
            "palette(highlight)" in qss and "palette(highlighted-text)" in qss,
            "selection colours come from the palette, not hard-coded",
        )
        report.check(
            ":selected:!active" in qss,
            "selection stays flat when the window loses focus",
        )

        print("\n10b. Network panel against a real peer")
        consumer = window.network_pane
        peer = subprocess.Popen(  # noqa: S603
            [sys.executable, str(ROOT / "tests" / "acceptance_provider.py"), "--seconds", "120"],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            report.check(not consumer.disconnect_button.isEnabled(), "disconnect is off before connecting")
            report.check(consumer.tree.topLevelItemCount() == 0, "tree starts empty")

            consumer._on_scan()  # noqa: SLF001 - driving the button's slot
            found = wait_for(app, lambda: bool(consumer.devices), timeout=40)
            report.check(found, "the peer is discovered", f"{len(consumer.devices)} device(s)")

            # Our own provider is on the network too, and so is any toolbox window the user
            # happens to have open, so pick the peer by EPR rather than trusting the order.
            peer_epr = epr_for(PEER_INSTANCE).urn
            peer_row = next(
                (i for i, d in enumerate(consumer.devices) if d.epr == peer_epr),
                None,
            )
            report.check(
                peer_row is not None,
                "the acceptance provider is among them",
                f"{[d.epr for d in consumer.devices]}",
            )

            if peer_row is not None:
                consumer.device_list.setCurrentRow(peer_row)
                pump(app)
                report.check(consumer.connect_button.isEnabled(), "connect becomes available")

                consumer._on_connect()  # noqa: SLF001
                connected = wait_for(app, lambda: consumer.remote is not None, timeout=40)
                report.check(connected, "connection established")

            if consumer.remote is not None:
                wait_for(app, lambda: consumer.table.rowCount() > 0, timeout=20)
                report.check(consumer.tree.topLevelItemCount() > 0, "the MDIB tree is populated")
                report.check(consumer.table.rowCount() >= 4, "metrics are listed", str(consumer.table.rowCount()))  # noqa: PLR2004
                report.check(consumer.disconnect_button.isEnabled(), "disconnect becomes available")

                remote_cell = lambda h, c: cell_of(consumer.table, h, c)  # noqa: E731
                report.check(
                    remote_cell("m.zoom_level", COL_R_RANGE) == "1 to 100",
                    "the peer's range is shown",
                    str(remote_cell("m.zoom_level", COL_R_RANGE)),
                )
                report.check(
                    remote_cell("m.zoom_level", COL_R_WRITABLE) == "yes",
                    "an enabled control reads as writable",
                    str(remote_cell("m.zoom_level", COL_R_WRITABLE)),
                )
                report.check(
                    remote_cell("m.locked_setting", COL_R_WRITABLE) == "disabled",
                    "a disabled control is marked as such rather than writable",
                    str(remote_cell("m.locked_setting", COL_R_WRITABLE)),
                )

                consumer.select_handle("m.mode")
                pump(app)
                report.check(
                    consumer.editor_stack.currentIndex() == 1,
                    "a choice on the peer gets a combo box",
                )
                report.check(
                    [consumer.choice_box.itemText(i) for i in range(consumer.choice_box.count())]
                    == ["IDLE", "RUN", "PAUSE"],
                    "filled from the peer's AllowedValue",
                )

                consumer.select_handle("m.locked_setting")
                pump(app)
                report.check(
                    not consumer.apply_button.isEnabled(),
                    "the editor is refused for a disabled control",
                )

                consumer.select_handle("m.zoom_level")
                pump(app)
                report.check(consumer.apply_button.isEnabled(), "and offered for an enabled one")
                consumer.value_edit.setText("55")
                consumer._on_apply()  # noqa: SLF001
                accepted = wait_for(
                    app,
                    lambda: "accepted" in consumer.invocation_label.text(),
                    timeout=30,
                )
                report.check(accepted, "a remote write is accepted", consumer.invocation_label.text())
                wait_for(app, lambda: remote_cell("m.zoom_level", COL_R_VALUE) == "55", timeout=20)
                report.check(
                    remote_cell("m.zoom_level", COL_R_VALUE) == "55",
                    "and the new value comes back",
                    str(remote_cell("m.zoom_level", COL_R_VALUE)),
                )

                consumer.value_edit.setText("500")
                consumer._on_apply()  # noqa: SLF001
                refused = wait_for(
                    app,
                    lambda: "refused" in consumer.invocation_label.text(),
                    timeout=30,
                )
                report.check(refused, "an out-of-range write is refused", consumer.invocation_label.text())

                consumer._on_disconnect()  # noqa: SLF001
                pump(app)
                report.check(consumer.remote is None, "disconnect clears the connection")
                report.check(consumer.table.rowCount() == 0, "and empties the table")
        finally:
            peer.terminate()
            try:
                peer.wait(timeout=15)
            except subprocess.TimeoutExpired:
                peer.kill()

        print("\n10c. Export and import from the File menu")
        workdir = Path(tempfile.mkdtemp(prefix="sdctoolbox-gui-"))
        preset = workdir / "preset.json"

        # The file dialogs would block with nobody to answer them.
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(preset), ""))  # noqa: ARG005
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(preset), ""))  # noqa: ARG005

        before_metrics = sorted(service.list_metrics())
        before_alerts = sorted(service.list_alerts())
        report.check(bool(before_metrics), "there is something to export", str(before_metrics))

        written = window.export_config()
        report.check(written is not None and written.exists(), "export writes the file")

        service.remove_metric(before_metrics[0])
        pane.refresh()
        pump(app)
        report.check(
            sorted(service.list_metrics()) != before_metrics,
            "the device is changed after exporting",
        )

        report.check(window.import_config(), "import reports success")
        pump(app)
        report.check(
            sorted(service.list_metrics()) == before_metrics,
            "the data sources are back",
            str(sorted(service.list_metrics())),
        )
        report.check(
            sorted(service.list_alerts()) == before_alerts,
            "and so are the alarms",
            str(sorted(service.list_alerts())),
        )
        report.check(
            pane.table.rowCount() == len(before_metrics),
            "the table was refreshed by the import",
            str(pane.table.rowCount()),
        )

        preset.write_text('{"metrics": [{"label": "broken"}]}', encoding="utf-8")
        shown_warnings.clear()
        report.check(not window.import_config(), "a broken file is refused")
        report.check(
            len(shown_warnings) == 1,
            "and the user is told why",
            shown_warnings[0][1][:60] if shown_warnings else "no message",
        )

        print("\n10d. The startup window")
        startup = StartupDialog()
        report.check(
            startup.name_edit.text() == constants.DEFAULT_INSTANCE_NAME,
            "the name defaults to the same value the command line uses",
            startup.name_edit.text(),
        )
        report.check(
            startup.chosen_ip() == constants.DEFAULT_IP,
            "and so does the address",
            startup.chosen_ip(),
        )
        report.check(not startup.config_edit.text(), "no config file by default")
        report.check(not startup.verbose_box.isChecked(), "and quiet logging")

        addresses = [startup.ip_box.itemData(i) for i in range(startup.ip_box.count())]
        report.check(bool(addresses), "the address list is populated", f"{len(addresses)} entries")
        report.check(
            constants.DEFAULT_IP in addresses,
            "loopback is among them",
        )
        link_local = [a for a in addresses if a.startswith(LINK_LOCAL_PREFIX)]
        usable = [a for a in addresses if not a.startswith(LINK_LOCAL_PREFIX)]
        if link_local and usable:
            report.check(
                addresses.index(usable[-1]) < addresses.index(link_local[0]),
                "usable addresses are listed before link-local ones",
            )
        report.check(
            "\u2014" in startup.ip_box.itemText(0),
            "each entry names its adapter",
            startup.ip_box.itemText(0)[:48],
        )
        report.check(
            startup.chosen_ip() == startup.ip_box.itemData(startup.ip_box.currentIndex()),
            "the adapter name is not smuggled into the address",
            startup.chosen_ip(),
        )

        startup.name_edit.setText("   ")
        startup._on_accept()  # noqa: SLF001
        report.check(startup.settings() is None, "a blank name is refused")
        report.check(not startup.error_label.isHidden(), "and says why", startup.error_label.text()[:44])

        startup.name_edit.setText("beta")
        startup.config_edit.setText("does-not-exist.json")
        startup._on_accept()  # noqa: SLF001
        report.check(startup.settings() is None, "a missing config file is refused")

        startup.config_edit.setText(str(ROOT / "presets" / "insufflator.json"))
        startup.verbose_box.setChecked(True)
        startup._on_accept()  # noqa: SLF001
        chosen = startup.settings()
        report.check(chosen is not None, "a valid combination is accepted")
        if chosen is not None:
            report.check(chosen.name == "beta", "the name is carried through", chosen.name)
            report.check(chosen.verbose, "and the verbose flag")
            report.check(bool(chosen.config_path), "and the config file")
        startup.deleteLater()

        print("\n10e. Arguments skip the startup window")
        report.check(
            run_toolbox.settings_from_args(run_toolbox.parse_args([])).name
            == constants.DEFAULT_INSTANCE_NAME,
            "argument defaults match the dialog's",
        )
        from_args = run_toolbox.settings_from_args(
            run_toolbox.parse_args(["--name", "gamma", "--ip", "127.0.0.1", "--verbose"]),
        )
        report.check(from_args.name == "gamma", "arguments are honoured", from_args.name)
        report.check(from_args.verbose, "including verbose")
        report.check(
            type(from_args) is type(chosen),
            "arguments and the dialog produce the same shape, so main() cannot tell them apart",
        )

        print("\n11. Legibility on a dark theme")
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("#e0e0e0"))
        dark.setColor(QPalette.ColorRole.Base, QColor("#252526"))
        dark.setColor(QPalette.ColorRole.Text, QColor("#e0e0e0"))
        app.setPalette(dark)

        background = QColor("#1e1e1e")
        muted_label = QLabel("secondary text")
        mute(muted_label)
        text_colour = muted_label.palette().color(QPalette.ColorRole.WindowText)
        contrast = abs(text_colour.lightness() - background.lightness())
        report.check(
            contrast > 60,  # noqa: PLR2004
            "muted text stays legible on a dark background",
            f"text {text_colour.name()} on {background.name()}, lightness gap {contrast}",
        )

        dark_window = MainWindow(service)
        dark_dialog = NewMetricDialog(dark_window)
        preview_colour = dark_dialog.handle_preview.palette().color(QPalette.ColorRole.WindowText)
        report.check(
            abs(preview_colour.lightness() - background.lightness()) > 60,  # noqa: PLR2004
            "handle preview is legible on a dark background",
            f"{preview_colour.name()}, lightness {preview_colour.lightness()}",
        )
        error_col = dark_dialog.error_label.palette().color(QPalette.ColorRole.WindowText)
        report.check(
            error_col.lightness() > background.lightness(),
            "error text is lighter than a dark background",
            f"{error_col.name()}, lightness {error_col.lightness()}",
        )
        dark_dialog.deleteLater()
        dark_window.network_pane.shutdown()
        dark_window.close()
        dark_window.deleteLater()

        window.network_pane.shutdown()
        window.close()
    finally:
        service.stop()

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
