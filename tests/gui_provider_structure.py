"""Deterministic offscreen checks for provider structural refreshes."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_test_support import Report, WindowFixture, application, wait_for
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from sdc11073.xml_types import pm_types

from sdctoolbox import constants
from sdctoolbox.gui.provider_pane import ACOL_HANDLE, COL_CONTROL, COL_HANDLE
from decimal import Decimal

from sdctoolbox.gui import provider_pane as provider_pane_module
from sdctoolbox.gui.edit_dialogs import EditAlertDialog, EditSourceDialog
from sdctoolbox.model import ActionSpec, AlertPriority, AlertSpec, MetricKind, MetricSpec, WaveformShape


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


def check_actions_only_confirmation(report: Report) -> None:
    for answer in (QMessageBox.No, QMessageBox.Yes):
        with WindowFixture(f"followup-action-confirm-{answer.name}") as fixture, TemporaryDirectory(prefix="sdc-followup-confirm-") as temporary:
            service, window = fixture.service, fixture.window
            action = service.add_action(ActionSpec("Only action", target_handle="mds0", handle="action.original"))
            before_actions = service.list_actions()
            entity = service.mdib.entities.by_handle(action)
            before_versions = (service.mdib.mdib_version, service.mdib.mdstate_version, service.mdib.mddescription_version, entity.descriptor.DescriptorVersion, entity.state.StateVersion)
            report.check(not service.list_metrics() and not service.list_alerts() and len(before_actions) == 1, "the replacement fixture has only one argumentless action under mds0")
            source = Path(temporary) / "replacement.json"
            source.write_text('{"version":3,"actions":[{"label":"Replacement action","target":"mds0","handle":"action.replacement"}]}', encoding="utf-8")
            questions, warnings = [], []

            def question(_parent, title, text, *_args, **_kwargs):
                questions.append((title, text))
                return answer

            with (
                patch.object(QMessageBox, "question", question),
                patch.object(QMessageBox, "warning", lambda _parent, title, text: warnings.append((title, text))),
            ):
                result = window.load_config(source, confirm=True)
            report.check(len(questions) == 1, f"actions-only replacement asks before {answer.name}")
            report.check(not warnings, f"actions-only replacement handles {answer.name} without an import warning", str(warnings))
            if answer == QMessageBox.No:
                after = service.mdib.entities.by_handle(action)
                after_versions = None if after is None else (service.mdib.mdib_version, service.mdib.mdstate_version, service.mdib.mddescription_version, after.descriptor.DescriptorVersion, after.state.StateVersion)
                report.check(not result and service.list_actions() == before_actions, "No preserves the action instead of silently replacing it")
                report.check(after_versions == before_versions, "No preserves MDIB, descriptor and action-state versions", f"{before_versions} -> {after_versions}")
            else:
                report.check(result and set(service.list_actions()) == {"action.replacement"} and not service.list_metrics() and not service.list_alerts(), "Yes applies the actions-only replacement through the actual config path")
                report.check(service.mdib.mdib_version > before_versions[0], "Yes advances the MDIB version when replacement is applied")


def check_live_edits(report: Report) -> None:
    with WindowFixture("gui-live-edit") as fixture:
        app, service, pane = fixture.app, fixture.service, fixture.window.provider_pane
        rate = service.add_metric(MetricSpec("Edit rate", MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(100),
                                             controllable=True, initial_value=Decimal(10)))
        dialog = EditSourceDialog(service, rate)
        report.check((dialog.allowed_minimum_edit.text(), dialog.allowed_maximum_edit.text()) == ("0", "100"),
                     "the edit dialog starts from the current allowed range")
        report.check("0" in dialog.technical_label.text() and "100" in dialog.technical_label.text(),
                     "the technical range is shown, read-only", dialog.technical_label.text())
        dialog.allowed_maximum_edit.setText("120")
        dialog._on_accept()  # noqa: SLF001
        report.check(dialog.result() != dialog.DialogCode.Accepted and bool(dialog.error_label.text()),
                     "an allowed range past the technical one is refused in the dialog", dialog.error_label.text())
        report.check(service.list_metrics()[rate].effective_allowed_range() == (0, 100), "and nothing changes")
        dialog.allowed_minimum_edit.setText("20")
        dialog.allowed_maximum_edit.setText("40")
        dialog._on_accept()  # noqa: SLF001
        report.check(dialog.result() == dialog.DialogCode.Accepted
                     and service.list_metrics()[rate].effective_allowed_range() == (20, 40),
                     "20..40 is applied to the running provider")
        dialog.deleteLater()

        dialog = EditSourceDialog(service, rate)
        dialog.active_box.setChecked(False)
        dialog._on_accept()  # noqa: SLF001
        report.check(not service.list_metrics()[rate].active, "the dialog switches the metric off")
        pane.refresh()
        row = row_for(pane.table, rate, COL_HANDLE)
        report.check(pane.table.item(row, COL_HANDLE).foreground().color() == provider_pane_module.muted_colour(pane),
                     "a switched-off metric's row is greyed")
        report.check(not pane.board.card(rate).control.isEnabled(), "and so is its card")
        dialog.deleteLater()

        wave = service.add_metric(MetricSpec("Edit wave", MetricKind.WAVEFORM, minimum=Decimal(0), maximum=Decimal(1)))
        dialog = EditSourceDialog(service, wave)
        report.check(dialog.allowed_minimum_edit.isHidden(), "a waveform has no allowed range to edit")
        dialog.shape_box.setCurrentIndex(dialog.shape_box.findData(WaveformShape.SQUARE))
        dialog.cycle_edit.setText("8")
        dialog._on_accept()  # noqa: SLF001
        spec = service.list_metrics()[wave]
        report.check((spec.shape, spec.cycle_seconds) == (WaveformShape.SQUARE, 8), "the dialog changes the curve",
                     f"{spec.shape} {spec.cycle_seconds}")
        dialog.deleteLater()

        alarm = service.add_alert(AlertSpec("Edit alarm", rate, lower_limit=Decimal(5), upper_limit=Decimal(90)))
        dialog = EditAlertDialog(service, alarm)
        report.check((dialog.lower_edit.text(), dialog.upper_edit.text()) == ("5", "90"),
                     "the alarm dialog starts from the current limits")
        dialog.upper_edit.setText("80")
        dialog.priority_box.setCurrentIndex(dialog.priority_box.findData(AlertPriority.HIGH))
        dialog._on_accept()  # noqa: SLF001
        spec = service.list_alerts()[alarm]
        report.check((spec.upper_limit, spec.actual_priority) == (80, AlertPriority.HIGH),
                     "the alarm dialog moves the limit and the priority", f"{spec.upper_limit} {spec.actual_priority}")
        dialog.deleteLater()
        report.check(wait_for(app, lambda: row_for(pane.alert_table, alarm, 0) is not None
                              and pane.alert_table.item(row_for(pane.alert_table, alarm, 0), 5).text() == "Hi"),
                     "the alarm table shows the actual priority")

        opened = []

        class FakeDialog:
            def __init__(self, _service, handle, _parent=None) -> None:
                opened.append(handle)

            def exec(self) -> int:
                return 0

        with patch.object(provider_pane_module, "EditSourceDialog", FakeDialog),                 patch.object(provider_pane_module, "EditAlertDialog", FakeDialog):
            card = pane.board.card(rate)
            report.require(card is not None and card.edit_button is not None, "a provider card has an edit button")
            card.edit_button.click()
            pane.set_use_widgets(False)
            pane.select_handle(rate)
            report.check(pane.edit_button.isVisible() and pane.edit_button.isEnabled(), "the table view offers Edit...")
            pane.edit_button.click()
            pane.select_alert_handle(alarm)
            report.check(pane.edit_alert_button.isEnabled(), "a selected alarm offers Edit alarm...")
            pane.edit_alert_button.click()
        report.check(opened == [rate, rate, alarm], "each opens the edit dialog for its entry", str(opened))


def check_scheduled_refresh_after_stop(report: Report) -> None:
    import sys

    errors = []
    previous = sys.excepthook
    sys.excepthook = lambda kind, value, trace: errors.append(value)
    try:
        with WindowFixture("gui-refresh-after-stop") as fixture:
            pane = fixture.window.provider_pane
            pane._schedule(pane.refresh)  # noqa: SLF001 - a refresh queued by a late MDIB signal
            pane._schedule(pane.refresh_contexts)  # noqa: SLF001
            fixture.service.stop()
            wait_for(fixture.app, lambda: False, timeout=0.2)
    finally:
        sys.excepthook = previous
    report.check(not errors, "a refresh queued before the provider stopped does nothing afterwards", str(errors))


def main() -> int:
    report = Report()
    app = application()
    print("GUI provider structure (offscreen)")
    check_actions_only_confirmation(report)
    check_live_edits(report)
    check_scheduled_refresh_after_stop(report)
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
