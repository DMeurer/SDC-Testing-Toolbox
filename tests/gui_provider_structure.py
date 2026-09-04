"""Deterministic offscreen checks for provider structural refreshes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_test_support import Report, WindowFixture, wait_for
from PySide6.QtCore import Qt
from sdc11073.xml_types import pm_types

from sdctoolbox import constants
from sdctoolbox.gui.provider_pane import ACOL_HANDLE, COL_CONTROL, COL_HANDLE
from sdctoolbox.model import ActionSpec, AlertSpec, MetricKind, MetricSpec


def row_for(table, handle: str, column: int = 0) -> int | None:
    for row in range(table.rowCount()):
        item = table.item(row, column)
        if item is not None and item.text() == handle:
            return row
    return None


def control_state(pane, handle: str) -> Qt.CheckState | None:
    row = row_for(pane.table, handle, COL_HANDLE)
    if row is None:
        return None
    item = pane.table.item(row, COL_CONTROL)
    return item.checkState() if item is not None else None


def main() -> int:
    report = Report()
    print("GUI provider structure (offscreen)")
    with WindowFixture("gui-provider-structure") as fixture:
        app = fixture.app
        service = fixture.service
        window = fixture.window
        assert window is not None
        pane = window.provider_pane

        report.check(
            pane.new_button.isVisible()
            and pane.context_button.isVisible()
            and not pane.remove_button.isVisible(),
            "the provider toolbar shows global actions and hides table-only removal in widget mode",
        )
        report.check(
            pane.views.count() == 2
            and pane.views.widget(0) is pane.metric_stack
            and pane.alert_table.isVisible(),
            "the provider pane contains metric and alarm views",
        )
        report.check(not pane.actions_widget.isVisible(), "an empty action toolbar stays hidden")
        report.check(
            pane.new_alert_button.isVisible()
            and not pane.remove_alert_button.isEnabled()
            and not pane.toggle_alert_button.isEnabled(),
            "the alarm toolbar starts ready for creation without selection actions",
        )

        metric = service.add_metric(MetricSpec(label="Structural source", kind=MetricKind.NUMBER))
        report.require(
            wait_for(app, lambda: control_state(pane, metric) == Qt.Unchecked),
            "a runtime metric appears without a set operation",
        )

        operation = service.enable_control(metric)
        report.check(
            wait_for(app, lambda: control_state(pane, metric) == Qt.Checked),
            "a new set-operation descriptor makes remote control available",
        )
        service.disable_control(metric)
        report.check(
            wait_for(app, lambda: control_state(pane, metric) == Qt.Unchecked),
            "a set-operation state change makes remote control unavailable",
        )
        service.enable_control(metric)
        report.check(
            wait_for(app, lambda: control_state(pane, metric) == Qt.Checked),
            "a set-operation state change restores remote control availability",
        )
        report.check(
            service.operation_handle_for(metric) == operation,
            "state refreshes retain the existing set-operation descriptor",
        )

        action = service.add_action(
            ActionSpec(label="Structural action", target_handle=constants.MDS_HANDLE),
        )
        report.require(
            wait_for(
                app,
                lambda: action in pane.action_buttons
                and pane.action_buttons[action].isEnabled()
                and pane.actions_widget.isVisible(),
            ),
            "a runtime action adds an enabled button and reveals its toolbar",
        )
        service._set_operating_mode(action, pm_types.OperatingMode.DISABLED)
        report.check(
            wait_for(app, lambda: not pane.action_buttons[action].isEnabled()),
            "an activate-operation state change disables its action button",
        )
        service._set_operating_mode(action, pm_types.OperatingMode.ENABLED)
        report.check(
            wait_for(app, lambda: pane.action_buttons[action].isEnabled()),
            "an activate-operation state change restores action availability",
        )
        service.remove_action(action)
        report.check(
            wait_for(
                app,
                lambda: action not in pane.action_buttons and not pane.actions_widget.isVisible(),
            ),
            "removing the runtime action removes its button and hides the empty toolbar",
        )

        alert = service.add_alert(AlertSpec(label="Structural alarm", source_handle=metric))
        report.require(
            wait_for(app, lambda: row_for(pane.alert_table, alert, ACOL_HANDLE) is not None),
            "a runtime alarm appears in the alarm view",
        )
        pane.select_alert_handle(alert)
        report.check(
            pane.remove_alert_button.isEnabled()
            and pane.toggle_alert_button.isEnabled()
            and pane.toggle_alert_button.text() == "Raise",
            "selecting the alarm enables its applicable toolbar actions",
        )
        service.remove_alert(alert)
        report.check(
            wait_for(
                app,
                lambda: row_for(pane.alert_table, alert, ACOL_HANDLE) is None
                and not pane.remove_alert_button.isEnabled()
                and not pane.toggle_alert_button.isEnabled(),
            ),
            "removing the runtime alarm clears its row and selection actions",
        )

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
