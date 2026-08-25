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
import sys
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
from PySide6.QtWidgets import QApplication, QHeaderView, QLabel  # noqa: E402

from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import pm_types  # noqa: E402

from sdctoolbox.constants import CODE_DIMENSIONLESS  # noqa: E402
from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.gui.new_metric_dialog import NewMetricDialog  # noqa: E402
from sdctoolbox.gui.provider_pane import (  # noqa: E402
    COL_CONTROL,
    COL_HANDLE,
    COL_KIND,
    COL_LABEL,
    COL_UNIT,
    COL_VALUE,
    NO_VALUE,
)
from sdctoolbox.model import MetricKind  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402


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


def fill_dialog(
    dialog: NewMetricDialog,
    *,
    kind: MetricKind,
    label: str,
    unit: str = "",
    values: str = "",
    resolution: str = "",
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

        print("\n1. Window")
        report.check(window.tabs.count() == 2, "two tabs", str(window.tabs.count()))  # noqa: PLR2004
        report.check(window.tabs.tabText(0) == "My device", "first tab is the provider")
        report.check(pane.table.rowCount() == 0, "table starts empty")
        report.check(not pane.remove_button.isEnabled(), "remove is disabled with no selection")
        report.check(not pane.apply_button.isEnabled(), "editor is disabled with no selection")

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

        fill_dialog(dialog, kind=MetricKind.NUMBER, label="Zoom level", unit="steps")
        report.check(
            dialog.handle_preview.text() == "m.zoom_level",
            "handle preview follows the label",
            dialog.handle_preview.text(),
        )
        dialog._on_accept()  # noqa: SLF001
        spec = dialog.spec()
        report.check(spec is not None, "a valid definition is accepted")
        dialog.deleteLater()

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
            service.set_value(zoom, Decimal("777"))
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
            cell(pane, zoom, COL_VALUE) == "777",
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

        print("\n9. Column sizing")
        header = pane.table.horizontalHeader()
        modes = {header.sectionResizeMode(c) for c in range(pane.table.columnCount())}
        report.check(
            modes == {QHeaderView.Interactive},
            "every column is draggable",
            ", ".join(str(m) for m in modes),
        )
        before = pane.table.columnWidth(COL_LABEL)
        pane.table.setColumnWidth(COL_LABEL, before + 60)
        pump(app)
        report.check(
            pane.table.columnWidth(COL_LABEL) == before + 60,
            "a resized column keeps its width",
            f"{before} -> {pane.table.columnWidth(COL_LABEL)}",
        )
        pane.refresh()
        pump(app)
        report.check(
            pane.table.columnWidth(COL_LABEL) == before + 60,
            "and a refresh does not undo it",
            str(pane.table.columnWidth(COL_LABEL)),
        )

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

        print("\n11. Legibility on a dark theme")
        dark = QPalette()
        dark.setColor(QPalette.ColorRole.Window, QColor("#1e1e1e"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("#e0e0e0"))
        dark.setColor(QPalette.ColorRole.Base, QColor("#252526"))
        dark.setColor(QPalette.ColorRole.Text, QColor("#e0e0e0"))
        app.setPalette(dark)

        dark_window = MainWindow(service)
        placeholder = dark_window.tabs.widget(1)
        dark_label = placeholder.findChild(QLabel)
        text_colour = dark_label.palette().color(QPalette.ColorRole.WindowText)
        background = QColor("#1e1e1e")
        contrast = abs(text_colour.lightness() - background.lightness())
        report.check(
            contrast > 60,  # noqa: PLR2004
            "network tab text is legible on a dark background",
            f"text {text_colour.name()} on {background.name()}, lightness gap {contrast}",
        )

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
        dark_window.close()
        dark_window.deleteLater()

        window.close()
    finally:
        service.stop()

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
