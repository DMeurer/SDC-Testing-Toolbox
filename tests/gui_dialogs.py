"""Deterministic offscreen checks for user-input dialogs."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_test_support import Report, application, pump
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from sdctoolbox.gui.context_dialog import ContextDialog
from sdctoolbox.gui.new_alert_dialog import NewAlertDialog
from sdctoolbox.gui.new_metric_dialog import NewMetricDialog
from sdctoolbox.gui.startup_dialog import StartupDialog
from sdctoolbox.model import (
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
)


def select_kind(dialog: NewMetricDialog, kind: MetricKind) -> None:
    for index in range(dialog.kind_box.count()):
        if dialog.kind_box.itemData(index) is kind:
            dialog.kind_box.setCurrentIndex(index)
            return
    raise AssertionError(f"dialog does not offer {kind.value}")


def metric_dialog_checks(report: Report) -> None:
    dialog = NewMetricDialog()
    try:
        select_kind(dialog, MetricKind.NUMBER)
        dialog.label_edit.setText("Zoom level")
        dialog.minimum_edit.setText("1")
        dialog.maximum_edit.setText("100")
        dialog._on_accept()
        spec = dialog.spec()
        report.check(
            spec is not None and (spec.minimum, spec.maximum) == (Decimal(1), Decimal(100)),
            "valid metric input reaches its specification",
        )
    finally:
        dialog.deleteLater()

    for value in ("NaN", "Infinity", "-Infinity"):
        invalid = NewMetricDialog()
        try:
            select_kind(invalid, MetricKind.NUMBER)
            invalid.label_edit.setText("Invalid number")
            invalid.minimum_edit.setText(value)
            invalid._on_accept()
            report.check(
                invalid.spec() is None and "finite number" in invalid.error_label.text(),
                f"metric dialog rejects {value}",
                invalid.error_label.text(),
            )
        finally:
            invalid.deleteLater()


def alert_dialog_checks(report: Report) -> None:
    metrics = {"m.number": MetricSpec(label="Number", kind=MetricKind.NUMBER, handle="m.number")}
    dialog = NewAlertDialog(metrics)
    try:
        dialog.label_edit.setText("Too high")
        dialog.upper_edit.setText("10")
        dialog._on_accept()
        spec = dialog.spec()
        report.check(
            spec is not None and spec.source_handle == "m.number" and spec.upper_limit == Decimal(10),
            "valid alarm input reaches its specification",
        )
    finally:
        dialog.deleteLater()

    invalid = NewAlertDialog(metrics)
    try:
        invalid.label_edit.setText("Invalid alarm")
        invalid.upper_edit.setText("NaN")
        invalid._on_accept()
        report.check(
            invalid.spec() is None and "finite number" in invalid.error_label.text(),
            "alarm dialog rejects a non-finite limit",
            invalid.error_label.text(),
        )
    finally:
        invalid.deleteLater()


def context_dialog_checks(report: Report) -> None:
    dialog = ContextDialog(LocationInfo(facility="HOSP"), PatientInfo())
    try:
        dialog.birth_edit.setText("not a date")
        dialog._on_accept()
        report.check(
            dialog.patient() is None and "1980-04-01" in dialog.error_label.text(),
            "context dialog rejects an invalid date",
            dialog.error_label.text(),
        )
        report.check(dialog.error_label.textFormat() == Qt.PlainText, "context errors render as plain text")
    finally:
        dialog.deleteLater()


def startup_dialog_checks(report: Report) -> None:
    for description, address in (
        ("hostname", "localhost"),
        ("IPv6 address", "::1"),
        ("malformed address", "999.1.2.3"),
    ):
        dialog = StartupDialog(name="invalid-address")
        try:
            dialog.ip_box.setCurrentText(address)
            dialog.show()
            pump(application(), 0.05)
            dialog._on_accept()
            report.check(
                dialog.result() != QDialog.Accepted
                and dialog.settings() is None
                and "IPv4 address" in dialog.error_label.text(),
                f"startup dialog rejects a {description}",
                dialog.error_label.text(),
            )
        finally:
            dialog.close()
            dialog.deleteLater()


def main() -> int:
    application()
    report = Report()
    print("GUI dialogs (offscreen)")
    metric_dialog_checks(report)
    alert_dialog_checks(report)
    context_dialog_checks(report)
    startup_dialog_checks(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
