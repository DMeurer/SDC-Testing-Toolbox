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
from types import SimpleNamespace

# Must be set before QApplication is created, otherwise Qt wants a real display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QSizePolicy,
)

from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import pm_types  # noqa: E402

import run_toolbox  # noqa: E402
from sdctoolbox import constants  # noqa: E402
from sdctoolbox.constants import CODE_DIMENSIONLESS, epr_for  # noqa: E402
from sdctoolbox.gui.consumer_pane import COL_RANGE as COL_R_RANGE  # noqa: E402
from sdctoolbox.gui.consumer_pane import COL_VALUE as COL_R_VALUE  # noqa: E402
from sdctoolbox.gui.consumer_pane import COL_WRITABLE as COL_R_WRITABLE  # noqa: E402
from sdctoolbox.gui.context_dialog import ContextDialog  # noqa: E402
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
from sdctoolbox.gui.widgets import WidgetSpec  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    AlertKind,
    AlertManifestation,
    AlertPriority,
    AlertSignalSpec,
    AlertSpec,
    Coding,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    PatientMeasurement,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402

# This file lives in tests/, so its own directory is on the path.
from acceptance_provider import PEER_INSTANCE, UPDATE_CONTEXT_COMMAND  # noqa: E402
from script_support import Report  # noqa: E402


def pump(app: QApplication, seconds: float = 0.4) -> None:
    """Let Qt deliver queued signals, including those emitted from other threads."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def send_wheel(app: QApplication, widget, notches: int = -1) -> None:  # noqa: ANN001
    """Turn the wheel over a widget, the way a real scroll reaches it.

    One notch is 120 units, negative being a scroll downwards.
    """
    delta = QPoint(0, notches * 120)
    centre = widget.rect().center()
    app.sendEvent(
        widget,
        QWheelEvent(
            QPointF(centre),
            QPointF(widget.mapToGlobal(centre)),
            delta,
            delta,
            Qt.NoButton,
            Qt.NoModifier,
            Qt.NoScrollPhase,
            False,  # noqa: FBT003 - inverted, a positional in the Qt signature
        ),
    )


def wait_for(app: QApplication, predicate, timeout: float = 30.0) -> bool:  # noqa: ANN001
    """Pump the event loop until predicate() is true or the time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.05)
    return predicate()


def wait_for_peer_ready(process: subprocess.Popen, timeout: float = 40.0) -> bool:
    """Wait for the subprocess marker after its provider HTTP service is ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            return False
        if "READY" in line:
            return True
    return False


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
        provider_margins = pane.layout().contentsMargins()
        network_margins = window.network_pane.layout().contentsMargins()
        report.check(
            (provider_margins.top(), provider_margins.bottom())
            == (network_margins.top(), network_margins.bottom()),
            "both panels pad their content by the same amount",
            f"provider {provider_margins.top()}/{provider_margins.bottom()}, "
            f"network {network_margins.top()}/{network_margins.bottom()}",
        )
        report.check(
            provider_margins.top() == 0,
            "and neither adds its own on top of the panel's",
            str(provider_margins.top()),
        )
        report.check(pane.table.rowCount() == 0, "table starts empty")
        report.check(not pane.remove_button.isEnabled(), "remove is disabled with no selection")
        report.check(not pane.apply_button.isEnabled(), "editor is disabled with no selection")

        divider_before = window.splitter.sizes()
        window.resize(1400, 560)
        pump(app)
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
            [t for t in file_items if t]
            == ["&Import config\u2026", "&Export config\u2026", "Load &preset", "E&xit"],
            "File has import, export, presets and exit",
            str(file_items),
        )
        preset_items = [a.text() for a in window.presets_menu.actions()]
        report.check(bool(preset_items), "the preset submenu is populated", str(preset_items))
        report.check(
            all(a.isEnabled() for a in window.presets_menu.actions()),
            "and every entry is loadable, so none of the shipped presets is broken",
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

        non_finite_text = ("NaN", "sNaN", "Infinity", "-Infinity")
        metric_decimal_fields = (
            (MetricKind.NUMBER, "resolution_edit", "resolution"),
            (MetricKind.NUMBER, "minimum_edit", "minimum"),
            (MetricKind.NUMBER, "maximum_edit", "maximum"),
            (MetricKind.WAVEFORM, "sample_period_edit", "sample period"),
            (MetricKind.DISTRIBUTION, "domain_min_edit", "domain minimum"),
            (MetricKind.DISTRIBUTION, "domain_max_edit", "domain maximum"),
        )
        for kind, edit_name, field in metric_decimal_fields:
            for invalid in non_finite_text:
                invalid_dialog = NewMetricDialog()
                fill_dialog(invalid_dialog, kind=kind, label="Invalid numeric input")
                getattr(invalid_dialog, edit_name).setText(invalid)
                invalid_dialog._on_accept()  # noqa: SLF001
                message = invalid_dialog.error_label.text()
                report.check(
                    invalid_dialog.spec() is None and field in message and "finite number" in message,
                    f"{field} rejects {invalid} with a field-level message",
                    message,
                )
                invalid_dialog.deleteLater()

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
        alert_dialog.signal_boxes[1][1].setChecked(False)
        alert_dialog.signal_boxes[0][2].setChecked(True)
        alert_dialog.signal_boxes[2][1].setChecked(True)
        alert_dialog._on_accept()  # noqa: SLF001
        report.check(
            alert_dialog.spec() is not None
            and alert_dialog.spec().signals
            == (
                AlertSignalSpec(AlertManifestation.VIS, latching=True),
                AlertSignalSpec(AlertManifestation.TAN),
            ),
            "signal selection and latching reach the alarm spec",
            str(alert_dialog.spec().signals if alert_dialog.spec() else None),
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
        for edit_name, field in (("lower_edit", "lower limit"), ("upper_edit", "upper limit")):
            for invalid in non_finite_text:
                invalid_alert = NewAlertDialog(alert_metrics)
                invalid_alert.label_edit.setText("Invalid limit")
                getattr(invalid_alert, edit_name).setText(invalid)
                invalid_alert._on_accept()  # noqa: SLF001
                message = invalid_alert.error_label.text()
                report.check(
                    invalid_alert.spec() is None and field in message and "finite number" in message,
                    f"{field} rejects {invalid} with a field-level message",
                    message,
                )
                invalid_alert.deleteLater()
        alert_dialog.deleteLater()
        alert_dialog2.deleteLater()

        print("\n2d. The patient and location dialog")
        context_dialog = ContextDialog(LocationInfo(facility="HOSP"), PatientInfo())
        report.check(
            context_dialog.facility_edit.text() == "HOSP",
            "it opens on the current location",
            context_dialog.facility_edit.text(),
        )
        context_dialog.birth_edit.setText("the fourth of July")
        context_dialog._on_accept()  # noqa: SLF001
        report.check(context_dialog.patient() is None, "an unusable date of birth is refused")
        report.check(
            not context_dialog.error_label.isHidden(),
            "and says what a date should look like",
            context_dialog.error_label.text()[:44],
        )
        context_dialog.birth_edit.setText("1980-04-01")
        context_dialog.given_edit.setText("Ada")
        context_dialog.room_edit.setText("12")
        context_dialog.height_value_edit.setText("170.5")
        context_dialog.height_unit_code_edit.setText("demo-cm")
        context_dialog.height_unit_system_edit.setText("private")
        context_dialog.height_unit_label_edit.setText("cm")
        context_dialog.weight_value_edit.setText("72.4")
        context_dialog.weight_unit_code_edit.setText("demo-kg")
        context_dialog.weight_unit_system_edit.setText("private")
        context_dialog.weight_unit_label_edit.setText("kg")
        context_dialog.race_code_edit.setText("demo-race")
        context_dialog.race_system_edit.setText("private")
        context_dialog.race_label_edit.setText("Demo race")
        for index in range(context_dialog.sex_box.count()):
            if context_dialog.sex_box.itemData(index) == "F":
                context_dialog.sex_box.setCurrentIndex(index)
        context_dialog._on_accept()  # noqa: SLF001
        entered_patient = context_dialog.patient()
        entered_location = context_dialog.location()
        report.check(entered_patient is not None, "a valid combination is accepted")
        if entered_patient is not None:
            report.check(entered_patient.given_name == "Ada", "the name is carried through")
            report.check(
                entered_patient.sex == "F",
                "and sex comes out as its BICEPS code, not its caption",
                entered_patient.sex,
            )
            report.check(
                entered_patient.height is not None
                and entered_patient.height.value == Decimal("170.5")
                and entered_patient.height.unit.code == "demo-cm"
                and entered_patient.weight is not None
                and entered_patient.weight.value == Decimal("72.4")
                and entered_patient.race is not None
                and entered_patient.race.code == "demo-race",
                "and height, weight and race keep their BICEPS value shapes",
            )
        if entered_location is not None:
            report.check(entered_location.room == "12", "and the location too", entered_location.room)
        context_dialog.deleteLater()

        bad_measurement_dialog = ContextDialog(LocationInfo(), PatientInfo())
        bad_measurement_dialog.height_value_edit.setText("170")
        bad_measurement_dialog._on_accept()  # noqa: SLF001
        report.check(
            bad_measurement_dialog.patient() is None and "unit code" in bad_measurement_dialog.error_label.text(),
            "a partial demographic measurement is refused in the dialog",
            bad_measurement_dialog.error_label.text(),
        )
        bad_measurement_dialog.deleteLater()

        bad_patient_dialog = ContextDialog(LocationInfo(), PatientInfo())
        bad_patient_dialog.given_edit.setText("Bad\ufffe")
        bad_patient_dialog._on_accept()  # noqa: SLF001
        report.check(
            bad_patient_dialog.patient() is None
            and "not allowed in XML" in bad_patient_dialog.error_label.text(),
            "invalid basic patient text is reported by the dialog",
            bad_patient_dialog.error_label.text(),
        )
        bad_patient_dialog.deleteLater()

        bad_error_dialog = ContextDialog(LocationInfo(), PatientInfo())
        markup = "<img src=not-found width=10000 height=1>"
        bad_error_dialog.race_code_edit.setText("demo-race")
        bad_error_dialog.race_system_edit.setText(markup)
        bad_error_dialog._on_accept()  # noqa: SLF001
        report.check(
            bad_error_dialog.patient() is None
            and bad_error_dialog.error_label.textFormat() == Qt.PlainText
            and markup in bad_error_dialog.error_label.text(),
            "demographic validation errors render markup-looking input as plain text",
            bad_error_dialog.error_label.text(),
        )
        report.check(
            bad_error_dialog.error_label.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored,
            "demographic validation errors cannot force the dialog wider",
        )
        bad_error_dialog.deleteLater()

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

        print("\n4a. The table editor goes away with the table")
        consumer_pane = window.network_pane
        window.set_use_widgets(False)
        pump(app)
        report.check(not pane.editor_widget.isHidden(), "the editor is there in table mode")
        report.check(not pane.remove_button.isHidden(), "and so is Remove")
        report.check(
            not consumer_pane.editor_widget.isHidden(),
            "on the network panel too",
        )

        window.set_use_widgets(True)
        pump(app)
        # Both drive the table's selection, and the board has none: each card is its own
        # editor and carries its own bin.
        report.check(pane.editor_widget.isHidden(), "both go away in widget mode")
        report.check(pane.remove_button.isHidden(), "including Remove")
        report.check(
            consumer_pane.editor_widget.isHidden(),
            "and the network panel follows the same rule",
        )
        # The result of a write is not part of the editor. In widget mode a card is what
        # sends it, so hiding whether the peer accepted would take the answer away with
        # the question.
        report.check(
            not consumer_pane.invocation_label.isHidden(),
            "but the invocation result stays, because a card still writes",
        )

        window.set_use_widgets(False)
        pump(app)
        report.check(not pane.editor_widget.isHidden(), "and come back with the table")

        print("\n4b. The wheel scrolls, it does not edit")
        # The board is a scroll area full of cards. A combo box or slider that takes the
        # wheel rewrites every value the pointer crosses on the way down the panel - and on
        # our own board those writes go straight into the MDIB.
        window.set_use_widgets(True)
        pane.refresh()
        pump(app)

        mode_card = pane.board.card(mode)
        zoom_card = pane.board.card(zoom)
        report.check(mode_card is not None and zoom_card is not None, "both cards are on the board")

        # The wheel has to reach the actual input, not the card or the MetricWidget wrapper:
        # a plain QWidget ignores wheel events anyway, so aiming at those would pass whether
        # or not anything was fixed.
        mode_input = mode_card.control.box
        zoom_input = zoom_card.control.slider
        report.check(
            type(mode_input).__name__ == "NoWheelComboBox"
            and type(zoom_input).__name__ == "NoWheelSlider",
            "the cards are built from the wheel-proof controls",
            f"{type(mode_input).__name__}, {type(zoom_input).__name__}",
        )

        before_mode = service.get_value(mode)
        before_zoom = service.get_value(zoom)
        # The slider only writes while it has focus, so a stray wheel moves the handle
        # without reaching the MDIB. Watch the handle, which is what actually moves.
        before_handle = zoom_input.value()
        for widget in (mode_input, zoom_input):
            for _ in range(5):
                send_wheel(app, widget)
        pump(app)
        report.check(
            service.get_value(mode) == before_mode,
            "scrolling over a choice card leaves the value alone",
            f"{before_mode} -> {service.get_value(mode)}",
        )
        report.check(
            zoom_input.value() == before_handle,
            "scrolling over a number card leaves the slider where it was",
            f"handle {before_handle} -> {zoom_input.value()}",
        )
        report.check(
            service.get_value(zoom) == before_zoom,
            "and nothing reaches the mdib either",
            f"{before_zoom} -> {service.get_value(zoom)}",
        )

        # ... but the board still has to scroll, or every card becomes a dead zone. Two
        # cards fit in the panel, so add enough to push it past the bottom.
        filler = [
            service.add_metric(MetricSpec(label=f"Filler {i}", kind=MetricKind.NUMBER))
            for i in range(12)
        ]
        pane.refresh()
        pump(app)
        bar = pane.board.verticalScrollBar()
        report.check(bar.maximum() > 0, "the board has something to scroll", f"max {bar.maximum()}")
        if bar.maximum() > 0:
            bar.setValue(0)
            send_wheel(app, pane.board.card(mode).control.box)
            pump(app)
            report.check(
                bar.value() > 0,
                "and the wheel scrolls the board past the card instead",
                f"scrollbar 0 -> {bar.value()}",
            )
        for handle in filler:
            service.remove_metric(handle)
        pane.refresh()
        pump(app)

        # The editor combo below the board, and the tab bar, behave the same way.
        window.set_use_widgets(False)
        pane.select_handle(mode)
        pump(app)
        index_before = pane.choice_box.currentIndex()
        send_wheel(app, pane.choice_box)
        pump(app)
        report.check(
            pane.choice_box.currentIndex() == index_before,
            "the editor combo ignores the wheel too",
            f"{index_before} -> {pane.choice_box.currentIndex()}",
        )

        window.set_split_view(False)
        pump(app)
        tab_before = window.tabs.currentIndex()
        send_wheel(app, window.tabs.tabBar())
        pump(app)
        report.check(
            window.tabs.currentIndex() == tab_before,
            "and the tab bar does not flick between panels",
            f"{tab_before} -> {window.tabs.currentIndex()}",
        )
        window.set_split_view(True)
        pump(app)

        print("\n4c. Waveforms and distributions on the board")
        wave_spec = MetricSpec(
            label="Pleth",
            kind=MetricKind.WAVEFORM,
            unit_label="%",
            minimum=Decimal("0"),
            maximum=Decimal("100"),
            sample_period=Decimal("0.05"),
        )
        wave = service.add_metric(wave_spec)
        dist = service.add_metric(
            MetricSpec(
                label="Spectrum",
                kind=MetricKind.DISTRIBUTION,
                unit_label="dB",
                domain_unit_label="Hz",
                domain_minimum=Decimal("0"),
                domain_maximum=Decimal("500"),
            ),
        )
        window.set_use_widgets(True)
        pane.refresh()
        pump(app)

        wave_card = pane.board.card(wave)
        dist_card = pane.board.card(dist)
        report.check(wave_card is not None and dist_card is not None, "both get a card")
        report.check(
            type(wave_card.control).__name__ == "SampleArrayWidget",
            "a waveform gets the plot, not the read-out",
            type(wave_card.control).__name__,
        )
        report.check(
            wave_card.control.plot.scrolling and not dist_card.control.plot.scrolling,
            "the waveform scrolls and the distribution does not",
        )

        # The generator is already running, so samples should reach the plot on their own.
        report.check(
            wait_for(app, lambda: bool(pane.board.card(wave).control.plot.samples), timeout=20.0),
            "samples reach the waveform plot without anybody asking",
            f"{len(pane.board.card(wave).control.plot.samples)} on the plot",
        )

        # A distribution used to have nothing driving it at all: no generator, and no way
        # to push a block from the window, so the card read "waiting for samples" for ever.
        report.check(
            wait_for(app, lambda: bool(pane.board.card(dist).control.plot.samples), timeout=15.0),
            "a distribution fills itself, without anybody pushing a block",
            f"{len(pane.board.card(dist).control.plot.samples)} bars",
        )

        first_distribution = [Decimal(index) for index in range(32)]
        service.set_samples(dist, first_distribution)
        pump(app)
        dist_card.control.plot.flush()
        report.check(
            dist_card.control.plot.samples == [float(index) for index in range(32)],
            "a distribution block reaches its plot",
            str(dist_card.control.plot.samples),
        )
        service.set_samples(dist, [Decimal("7")] * 32)
        pump(app)
        dist_card.control.plot.flush()
        report.check(
            dist_card.control.plot.samples == [7.0] * 32,
            "and the next block replaces it rather than appending",
            str(dist_card.control.plot.samples),
        )

        print("\n4c-2. A refresh must not splice the last block back into the trace")
        # The bug this pins: the plot appends whatever it is shown, and show_value is the
        # refresh path - it fires for a descriptor being added, an operation changing, the
        # board being rebuilt. Appending there duplicated the newest block every time, and
        # because each pane pushed *every* metric on *every* report, two waveforms made
        # both of their traces jagged and a third made it worse.
        service.stop_generator()
        pump(app)
        wave_plot = pane.board.card(wave).control.plot
        wave_plot.clear()

        block = [Decimal("10"), Decimal("20"), Decimal("30")]
        service.set_samples(wave, block)
        pump(app)
        # flush rather than waiting: a scrolling plot reveals gradually, so asserting on
        # what happens to be on screen after an arbitrary pump is a race. See 4e.
        wave_plot.flush()
        pump(app, seconds=0.1)
        report.check(
            wave_plot.samples == [10.0, 20.0, 30.0],
            "a block arriving is drawn once",
            str(wave_plot.samples),
        )

        before = list(wave_plot.samples)
        pane.refresh()
        pane._refresh_board()  # noqa: SLF001
        pane.refresh_alerts()
        pump(app)
        wave_plot.flush()
        report.check(
            wave_plot.samples == before,
            "and a refresh does not draw it again",
            f"{len(before)} -> {len(wave_plot.samples)} samples",
        )

        # Adding any metric refreshes the pane. That is the hiccup, seen from the code.
        spare = service.add_metric(MetricSpec(label="Spare", kind=MetricKind.NUMBER))
        pane.refresh()
        pump(app)
        pane.board.card(wave).control.plot.flush()
        report.check(
            pane.board.card(wave).control.plot.samples == before,
            "adding another metric does not disturb a running trace",
            f"{len(before)} -> {len(pane.board.card(wave).control.plot.samples)} samples",
        )
        service.remove_metric(spare)
        pane.refresh()
        pump(app)

        # One waveform's report must not touch another's plot.
        wave2 = service.add_metric(
            MetricSpec(
                label="Second",
                kind=MetricKind.WAVEFORM,
                minimum=Decimal("0"),
                maximum=Decimal("100"),
                sample_period=Decimal("0.5"),
            ),
        )
        service.stop_generator()
        pane.refresh()
        pump(app)
        first_plot = pane.board.card(wave).control.plot
        first_plot.clear()
        pane.board.card(wave2).control.plot.clear()
        service.set_samples(wave, [Decimal("40")])
        pump(app)
        first_plot.flush()
        held = list(first_plot.samples)
        service.set_samples(wave2, [Decimal("99")])
        pump(app)
        first_plot.flush()
        report.check(
            first_plot.samples == held,
            "a report for one waveform leaves the other's trace alone",
            f"{held} -> {first_plot.samples}",
        )
        service.remove_metric(wave2)
        pane.refresh()
        pump(app)

        # It has to survive being painted, which is where a divide by zero would show up.
        window.set_use_widgets(False)
        pump(app)
        report.check(
            "sample" in cell(pane, wave, COL_VALUE),
            "the table says how many samples rather than printing them",
            cell(pane, wave, COL_VALUE),
        )
        report.check(
            cell(pane, dist, COL_RANGE) == "0 to 500 Hz",
            "and shows a distribution's domain in the range column",
            cell(pane, dist, COL_RANGE),
        )
        window.set_use_widgets(True)
        pump(app)

        print("\n4e. A trace moves at the rate its samples were taken at")
        # A waveform arrives in blocks - a quarter second of signal, four times a second -
        # and drawing a block the moment it lands makes the trace lurch rather than move.
        # SamplePeriod is on the descriptor so a consumer can place samples in time, and
        # this is the consumer doing that.
        paced = pane.board.card(wave).control.plot
        paced.clear()
        pump(app)
        report.check(paced.paced, "a waveform plot paces itself", f"period {wave_spec.sample_period}s")

        service.set_samples(wave, [Decimal(str(v)) for v in range(20)])
        app.processEvents()
        report.check(
            len(paced.samples) < 20,  # noqa: PLR2004
            "a block is not dumped on screen the instant it arrives",
            f"{len(paced.samples)} drawn, {len(paced.pending)} waiting",
        )
        report.check(
            len(paced.samples) + len(paced.pending) == 20,  # noqa: PLR2004
            "but nothing is lost: everything given is drawn or waiting",
            f"{len(paced.samples)} + {len(paced.pending)}",
        )
        report.check(
            wait_for(app, lambda: not paced.pending, timeout=5.0),
            "and it all arrives in its own time",
            f"{len(paced.samples)} drawn",
        )

        # A distribution has no time base to pace against - the whole picture is replaced
        # at once - so it eases from the old bar heights to the new ones instead. Same
        # purpose, different mechanism.
        dist_plot = pane.board.card(dist).control.plot
        report.check(not dist_plot.paced, "a distribution does not pace")
        report.check(dist_plot.tweening, "it eases between blocks instead")

        dist_plot.clear()
        dist_plot.add_samples([Decimal("10")] * 32)
        dist_plot.flush()
        pump(app, seconds=0.1)
        service.set_samples(dist, [Decimal("90")] * 32)
        app.processEvents()
        drawn = dist_plot.samples
        report.check(
            drawn != [90.0] * 32 and dist_plot.target == [90.0] * 32,
            "a new block is aimed at, not snapped to",
            f"drawn {[round(v, 1) for v in drawn]}, target {dist_plot.target}",
        )
        report.check(
            wait_for(app, lambda: dist_plot.samples == [90.0] * 32, timeout=3.0),
            "and the bars arrive there",
            str([round(v, 1) for v in dist_plot.samples]),
        )
        report.check(
            not dist_plot._timer.isActive(),  # noqa: SLF001
            "then stop, rather than animating an unchanging picture for ever",
        )

        print("\n4d. The dialog offers all five kinds")
        metric_dialog = NewMetricDialog()
        captions = [metric_dialog.kind_box.itemText(i) for i in range(metric_dialog.kind_box.count())]
        report.check(len(captions) == 5, "five kinds are offered", str(captions))  # noqa: PLR2004
        for index, caption in enumerate(captions):
            metric_dialog.kind_box.setCurrentIndex(index)
            metric_dialog._on_kind_changed()  # noqa: SLF001
            if caption == "Waveform":
                report.check(
                    not metric_dialog.sample_period_edit.isHidden()
                    and metric_dialog.domain_unit_edit.isHidden(),
                    "a waveform shows the sample period and hides the domain",
                )
                report.check(
                    metric_dialog.controllable_box.isHidden(),
                    "and hides the control checkbox, because nothing can write a waveform",
                )
            elif caption == "Distribution":
                report.check(
                    not metric_dialog.domain_unit_edit.isHidden()
                    and metric_dialog.sample_period_edit.isHidden(),
                    "a distribution shows the domain and hides the sample period",
                )
        metric_dialog.kind_box.setCurrentIndex(captions.index("Waveform"))
        metric_dialog._on_kind_changed()  # noqa: SLF001
        metric_dialog.label_edit.setText("From the dialog")
        metric_dialog.sample_period_edit.setText("0.25")
        metric_dialog._on_accept()  # noqa: SLF001
        built = metric_dialog.spec()
        report.check(built is not None, "a waveform can be built from the dialog")
        if built is not None:
            report.check(
                built.sample_period == Decimal("0.25"),
                "with the sample period entered",
                str(built.sample_period),
            )
            report.check(not built.controllable, "and never controllable")
        metric_dialog.deleteLater()

        service.remove_metric(wave)
        service.remove_metric(dist)
        service.stop_generator()
        pane.refresh()
        pump(app)

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
        shown_warnings.clear()
        current = service.get_value(zoom)
        for invalid in non_finite_text:
            pane.value_edit.setText(invalid)
            pane._on_apply()  # noqa: SLF001
            title, message = shown_warnings[-1] if shown_warnings else ("", "")
            report.check(
                service.get_value(zoom) == current
                and title == "Invalid value"
                and "value" in message
                and "finite number" in message,
                f"the local numeric editor rejects {invalid} without writing",
                f"{title}: {message}",
            )

        print("\n8c. Alarm signals and contexts in the pane")
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

        def signals_cell(handle: str = acked) -> str:
            for row in range(pane.alert_table.rowCount()):
                if pane.alert_table.item(row, ACOL_HANDLE).text() == handle:
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
            pane.delegate_button.isEnabled() and pane.delegate_button.text() == "Remote*",
            "remote-location simulation is offered for a capable alarm",
            pane.delegate_button.text(),
        )
        pane._on_delegate()  # noqa: SLF001
        pump(app)
        report.check("->Rem" in signals_cell(), "the simulated remote location shows in the cell", signals_cell())
        report.check(
            pane.delegate_button.text() == "Local*",
            "and the button offers the local simulation",
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

        latched_alarm = service.add_alert(
            AlertSpec(
                label="Latched zoom",
                source_handle=zoom,
                upper_limit=Decimal("90"),
                signals=(AlertSignalSpec(AlertManifestation.VIS, latching=True),),
            ),
        )
        service.set_value(zoom, Decimal("50"))
        pane.refresh_alerts()
        pane.select_alert_handle(latched_alarm)
        pump(app)
        report.check(
            "Vis:Latch" in signals_cell(latched_alarm),
            "a latched signal is shown after the condition clears",
        )
        report.check(pane.stop_latched_button.isEnabled(), "Stop latched is offered while a signal latches")
        pane._on_stop_latched()  # noqa: SLF001
        pump(app)
        report.check("Vis:Off" in signals_cell(latched_alarm), "Stop latched updates the signals cell")
        service.remove_alert(latched_alarm)
        pane.refresh_alerts()
        pump(app)

        report.check(
            "HOSP" in pane.context_label.text(),
            "the pane shows the default location",
            pane.context_label.text(),
        )
        service.set_patient(
            PatientInfo(
                given_name="Ada",
                family_name="Lovelace",
                height=PatientMeasurement(
                    value=Decimal("170.5"),
                    unit=Coding(code="demo-cm", system="private", label="cm"),
                ),
                weight=PatientMeasurement(
                    value=Decimal("72.4"),
                    unit=Coding(code="demo-kg", system="private", label="kg"),
                ),
                race=Coding(
                    code="demo-race",
                    system="private",
                    label="A deliberately long demographic display label that must wrap instead of widening the window",
                ),
            ),
        )
        report.check(
            wait_for(app, lambda: "Ada Lovelace" in pane.context_label.text()),
            "and the patient once one is attached without a manual refresh",
            pane.context_label.text(),
        )
        report.check(
            "height 170.5 cm" in pane.context_label.text(),
            "the provider pane summarizes demographic measurements",
            pane.context_label.text(),
        )
        window.resize(1170, 620)
        pump(app)
        report.check(
            window.minimumWidth() <= 1170,  # noqa: PLR2004
            "demographic text does not force an oversized window",
            str(window.minimumWidth()),
        )
        service.clear_patient()
        report.check(
            wait_for(app, lambda: "Ada" not in pane.context_label.text()),
            "and drops them again when they are detached without a manual refresh",
            pane.context_label.text(),
        )

        # Peer labels are untrusted text. The Network pane must show markup-looking content
        # literally and must not let it dictate the splitter's minimum width.
        consumer = window.network_pane
        consumer_minimum_before = consumer.minimumSizeHint().width()
        consumer.remote = SimpleNamespace(
            patient_contexts=lambda: {
                "PC.foreign": PatientInfo(
                    given_name="<b>Foreign</b>",
                    race=Coding(
                        code="foreign-race",
                        system="private",
                        label="<img src=not-found width=10000 height=1>",
                    ),
                ),
            },
        )
        consumer._refresh_contexts()  # noqa: SLF001 - exercise peer context presentation
        pump(app)
        report.check(
            consumer.context_label.textFormat() == Qt.PlainText
            and "<b>Foreign</b>" in consumer.context_label.text()
            and "<img src=not-found width=10000 height=1>" in consumer.context_label.text(),
            "peer demographic markup is rendered as literal plain text",
            consumer.context_label.text(),
        )
        report.check(
            consumer.context_label.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored,
            "peer demographic text cannot force the network pane wider",
        )

        hostile_dimension = "<img src=not-found width=10000 height=10000>"
        hostile_handle = f"handle {hostile_dimension}"
        hostile_label = f"<b>peer label</b> {hostile_dimension}"
        hostile_unit = f"unit {hostile_dimension}"
        hostile_domain = f"domain {hostile_dimension}"
        hostile_note = f"note {hostile_dimension}"
        hostile_value = f"<i>peer value</i> {hostile_dimension}"
        consumer.board.set_metrics(
            [
                WidgetSpec(
                    hostile_handle,
                    hostile_label,
                    None,
                    unit=hostile_unit,
                    domain=hostile_domain,
                    note=hostile_note,
                ),
                WidgetSpec("hostile.error", "Error source", MetricKind.NUMBER),
            ],
        )
        consumer.board.show_values({hostile_handle: hostile_value})
        hostile_card = consumer.board.card(hostile_handle)
        error_card = consumer.board.card("hostile.error")
        error_message = f"failed for {hostile_dimension}"
        error_card.control._show_error(error_message)  # noqa: SLF001
        pump(app)

        hostile_labels = (
            hostile_card.heading,
            hostile_card.footer,
            hostile_card.control.readout,
            error_card.control.error_label,
        )
        report.check(
            all(label.textFormat() == Qt.PlainText for label in hostile_labels)
            and hostile_card.heading.text() == hostile_label
            and all(
                text in hostile_card.footer.text()
                for text in (hostile_handle, hostile_unit, hostile_domain, hostile_note)
            )
            and hostile_card.control.readout.text() == hostile_value
            and error_card.control.error_label.text() == error_message,
            "peer metric labels, handles, units, domains, values, notes and errors "
            "stay literal",
        )
        report.check(
            all(
                label.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
                and label.maximumHeight() <= label.fontMetrics().lineSpacing() * 3 + 2
                for label in hostile_labels
            )
            and hostile_card.minimumSizeHint().width() <= 260,
            "hostile metric markup and long metadata cannot expand a card",
            f"card minimum={hostile_card.minimumSizeHint().width()}",
        )

        consumer._set_status(error_message)  # noqa: SLF001
        consumer.editor_label.setText(hostile_label)
        consumer._on_set_failed(error_message)  # noqa: SLF001
        report.check(
            all(
                label.textFormat() == Qt.PlainText
                for label in (
                    consumer.status_label,
                    consumer.editor_label,
                    consumer.invocation_label,
                )
            )
            and consumer.status_label.sizePolicy().horizontalPolicy()
            == QSizePolicy.Ignored
            and consumer.editor_label.maximumWidth() <= 240
            and consumer.invocation_label.maximumWidth() <= 240
            and consumer.status_label.text() == error_message
            and consumer.editor_label.text() == hostile_label
            and hostile_dimension in consumer.invocation_label.text()
            and consumer.minimumSizeHint().width() <= consumer_minimum_before,
            "peer status, editor text and invocation errors are literal without "
            "expanding the pane",
            f"pane minimum={consumer.minimumSizeHint().width()}",
        )
        consumer.remote = None
        consumer._reset_remote_ui()  # noqa: SLF001 - restore the disconnected state
        consumer._set_status("Not connected")  # noqa: SLF001
        consumer._refresh_contexts()  # noqa: SLF001 - restore the disconnected state

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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            if not report.check(wait_for_peer_ready(peer), "the peer reports ready before connection"):
                return report.summary()
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
                report.check(
                    wait_for(app, lambda: "height 170.5 cm" in consumer.context_label.text(), timeout=20),
                    "peer patient demographics are shown",
                    consumer.context_label.text(),
                )
                if peer.stdin is not None:
                    peer.stdin.write(f"{UPDATE_CONTEXT_COMMAND}\n")
                    peer.stdin.flush()
                report.check(
                    wait_for(app, lambda: "Grace Hopper" in consumer.context_label.text(), timeout=30),
                    "peer context reports update the patient display automatically",
                    consumer.context_label.text(),
                )

                remote_cell = lambda h, c: cell_of(consumer.table, h, c)  # noqa: E731
                zoom_range = remote_cell("m.zoom_level", COL_R_RANGE)
                report.check(
                    "1 to 100" in zoom_range and "step 1" in zoom_range,
                    "the peer's complete allowed range is shown",
                    str(zoom_range),
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

                # The peer publishes three waveforms, which is the case the consumer used
                # to mangle: it pushed every metric's samples on every report, so each
                # waveform's latest block was spliced into its own trace once per other
                # waveform. Two made both jagged; three made it worse.
                window.set_use_widgets(True)
                pump(app)
                got_trace = wait_for(
                    app,
                    lambda: consumer.board.card("m.saw") is not None
                    and bool(consumer.board.card("m.saw").control.plot.samples),
                    timeout=30.0,
                )
                report.check(got_trace, "the peer's waveform reaches a plot here")
                if got_trace:
                    saw_plot = consumer.board.card("m.saw").control.plot
                    step = 100.0 / 40.0

                    def saw_breaks() -> list[str]:
                        """Where the drawn sawtooth stops rising by its fixed step.

                        Continuity rather than length, because the peer keeps sending: a
                        block legitimately arrives while the panel is being refreshed, so
                        comparing sample counts across that window would be a race. A
                        spliced block breaks the ramp; an honest one does not.
                        """
                        drawn = list(consumer.board.card("m.saw").control.plot.samples)
                        return [
                            f"{drawn[i - 1]}->{drawn[i]}"
                            for i in range(1, len(drawn))
                            if not (drawn[i] < drawn[i - 1] and drawn[i - 1] > 100.0 - step * 2)
                            and abs(drawn[i] - drawn[i - 1] - step) > 0.05  # noqa: PLR2004
                        ]

                    report.check(
                        not saw_breaks(),
                        "and the trace drawn from it is continuous, with three waveforms running",
                        f"{len(saw_plot.samples)} samples, breaks at {saw_breaks()[:4]}",
                    )
                    held = list(saw_plot.samples)
                    consumer.refresh_values()
                    consumer.refresh()
                    pump(app)
                    drawn_now = list(consumer.board.card("m.saw").control.plot.samples)
                    report.check(
                        drawn_now[: len(held)] == held and not saw_breaks(),
                        "and refreshing the panel does not splice the last block back in",
                        f"{len(held)} -> {len(drawn_now)} samples, breaks at {saw_breaks()[:4]}",
                    )
                window.set_use_widgets(False)
                pump(app)

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

                for invalid in non_finite_text:
                    consumer.value_edit.setText(invalid)
                    consumer._on_apply()  # noqa: SLF001
                    message = consumer.invocation_label.text()
                    report.check(
                        "value" in message and "finite number" in message,
                        f"the remote numeric editor rejects {invalid} before invocation",
                        message,
                    )

                consumer.value_edit.setText("500")
                consumer._on_apply()  # noqa: SLF001
                report.check(
                    "not permitted" in consumer.invocation_label.text(),
                    "an out-of-range remote write gets immediate field feedback",
                    consumer.invocation_label.text(),
                )

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
        with tempfile.TemporaryDirectory(prefix="sdctoolbox-gui-") as raw_workdir:
            workdir = Path(raw_workdir)
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
        report.check(not workdir.exists(), "the GUI import/export directory is removed after use")

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
        report.check(startup.chosen_config() is None, "no config file by default")
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

        listed_startup = StartupDialog(name="listed")
        listed_startup.ip_box.setCurrentIndex(0)
        listed_ip = listed_startup.ip_box.itemData(0)
        listed_startup._on_accept()
        report.check(
            listed_startup.settings() is not None and listed_startup.settings().ip == listed_ip,
            "a listed IPv4 address is accepted",
            listed_ip,
        )
        listed_startup.deleteLater()

        manual_ip = "192.0.2.123"
        report.check(manual_ip not in addresses, "the manual test address is not listed")
        manual_startup = StartupDialog(name="manual")
        manual_startup.ip_box.setCurrentText(f"  {manual_ip}  ")
        manual_startup._on_accept()
        report.check(
            manual_startup.settings() is not None and manual_startup.settings().ip == manual_ip,
            "an unlisted manual IPv4 address is normalized and accepted",
            manual_startup.settings().ip if manual_startup.settings() else "not accepted",
        )
        manual_startup.deleteLater()

        invalid_addresses = {
            "whitespace-only": "   ",
            "hostname": "localhost",
            "IPv6": "::1",
            "malformed": "999.1.2.3",
            "address with a suffix": "192.0.2.1  \u2014  adapter",
        }
        for description, invalid_ip in invalid_addresses.items():
            invalid_startup = StartupDialog(name="invalid")
            invalid_startup.ip_box.setCurrentText(invalid_ip)
            invalid_startup.show()
            pump(app)
            invalid_startup._on_accept()
            report.check(
                invalid_startup.isVisible()
                and invalid_startup.result() != QDialog.Accepted
                and invalid_startup.settings() is None,
                f"a {description} bind address keeps the startup dialog open",
            )
            report.check(
                not invalid_startup.error_label.isHidden()
                and "IPv4 address" in invalid_startup.error_label.text(),
                f"a {description} bind address gets a field-specific error",
                invalid_startup.error_label.text(),
            )
            invalid_startup.deleteLater()

        startup.name_edit.setText("   ")
        startup._on_accept()  # noqa: SLF001
        report.check(startup.settings() is None, "a blank name is refused")
        report.check(not startup.error_label.isHidden(), "and says why", startup.error_label.text()[:44])

        startup.name_edit.setText("beta")
        startup.config_box.setCurrentText("does-not-exist.json")
        startup._on_accept()  # noqa: SLF001
        report.check(startup.settings() is None, "a missing config file is refused")

        # The presets folder is offered in the same list, so the common case needs no
        # file dialog at all.
        preset_paths = [
            startup.config_box.itemData(i)
            for i in range(startup.config_box.count())
            if startup.config_box.itemData(i)
        ]
        report.check(
            {Path(path).name for path in preset_paths} == constants.SHIPPED_PRESET_FILES,
            "the exact shipped preset inventory is offered in the list",
            str(sorted(Path(path).name for path in preset_paths)),
        )
        report.check(
            all(Path(p).exists() for p in preset_paths),
            "and every one of them exists",
        )
        if preset_paths:
            startup.config_box.setCurrentIndex(1)
            report.check(
                startup.chosen_config() == preset_paths[0],
                "picking one yields its path, not its caption",
                str(startup.chosen_config()),
            )

        startup.config_box.setCurrentText(str(ROOT / "presets" / "insufflator.json"))
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
