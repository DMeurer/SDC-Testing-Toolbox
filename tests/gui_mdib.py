"""Original native MDIB GUI/startup/console integration checks (offscreen)."""

from __future__ import annotations

import contextlib
import io
from html import unescape
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication, QMessageBox
from mdib_test_support import original_mdib

from sdctoolbox import config
from sdctoolbox.gui.main_window import MainWindow
from sdctoolbox.gui.startup_dialog import StartupDialog
from sdctoolbox.mdib_io import NativeMdibConfig
from sdctoolbox.provider_service import ProviderService


class GuiMdibTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        warnings = patch.object(QMessageBox, "warning")
        warnings.start()
        self.addCleanup(warnings.stop)
        self.service = ProviderService(instance_name=f"gui-native-{self._testMethodName}")
        self.service.start()
        self.window = MainWindow(self.service)
        self.addCleanup(self.service.stop)
        self.addCleanup(self.window.close)

    def test_native_waveform_snapshot_is_drawn_once_even_after_refresh(self) -> None:
        self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml")
        pane = self.window.provider_pane
        plot = pane.board.card("demo.wave").control.plot
        plot.flush()
        self.assertEqual(plot.samples, [0.0, 1.5, -0.5])
        pane.refresh()
        pane.set_use_widgets(False)
        pane.set_use_widgets(True)
        plot.flush()
        self.assertEqual(plot.samples, [0.0, 1.5, -0.5])
        pane.board.clear()
        pane._refresh_board()
        rebuilt = pane.board.card("demo.wave").control.plot
        rebuilt.flush()
        self.assertEqual(rebuilt.samples, [0.0, 1.5, -0.5])

    def test_native_containment_exposes_descriptor_xml_and_current_state(self) -> None:
        from decimal import Decimal

        self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml")
        pane = self.window.provider_pane
        self.assertIn("demo-monitor", unescape(pane._native_items["demo.root"].toolTip(1)))
        self.assertIn('OperationTarget="demo.rate"', unescape(pane._native_items["demo.set"].toolTip(1)))
        self.service.set_value("demo.rate", Decimal("20"))
        tip = unescape(pane._native_items["demo.rate"].child(0).toolTip(1))
        self.assertIn('Value="20"', tip)
        self.assertNotIn('Value="12.5"', tip)

    def test_console_edits_a_loaded_mdib_in_place(self) -> None:
        from examples.console import ProviderShell

        self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml")
        graph = self.service.mdib
        shell = ProviderShell(self.service)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            for command in ("generator on", "patient off"):
                try:
                    shell.onecmd(command)
                except ValueError as exc:
                    self.fail(f"{command} terminated the command loop: {exc}")
        self.assertNotIn("rejected", output.getvalue())
        self.assertTrue(self.service.generator_running)
        self.assertIs(self.service.mdib, graph)

    def test_import_rebinds_only_provider_and_keeps_every_editor(self) -> None:
        old_pane = self.window.provider_pane
        tabs = self.window.network_tabs
        self.window.set_use_widgets(False)
        self.assertTrue(self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml"))
        pane = self.window.provider_pane
        self.assertIsNot(pane, old_pane)
        self.assertTrue(old_pane.bridge._closed)
        self.assertIs(self.window.network_tabs, tabs)
        self.assertIs(pane.bridge._mdib, self.service.mdib)
        self.assertFalse(pane.use_widgets)
        self.assertEqual(pane.table.rowCount(), 3)
        self.assertIsNotNone(pane.native_tree)
        self.assertTrue(pane.new_button.isEnabled())
        self.assertTrue(pane.context_button.isEnabled())
        pane.select_handle("demo.rate")
        self.assertTrue(pane.remove_button.isEnabled())
        self.assertTrue(pane.apply_button.isEnabled())
        pane.select_alert_handle("demo.alarm")
        self.assertTrue(pane.toggle_alert_button.isEnabled())
        profile = config.parse({"version": 3, "metrics": [{"label": "Managed again", "kind": "text"}]})
        self.assertTrue(self.window.apply_config(profile, "managed.json"))
        self.assertTrue(self.window.provider_pane.new_button.isEnabled())
        self.assertIs(self.window.network_tabs, tabs)

    def test_cancelled_native_import_keeps_original_graph_and_warns_about_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native.xml"
            source.write_bytes(original_mdib())
            original = self.service.mdib
            with patch.object(QMessageBox, "question", return_value=QMessageBox.No) as question:
                self.assertFalse(self.window.load_config(source, confirm=True))
            self.assertIs(self.service.mdib, original)
            self.assertIn("reconnect", question.call_args.args[2].lower())

    def test_export_filter_selects_combined_xml_and_rejects_conflicting_suffix(self) -> None:
        self.assertTrue(self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml"))
        with tempfile.TemporaryDirectory() as directory:
            from sdctoolbox.gui.main_window import QFileDialog
            target = Path(directory) / "export.xml"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), config.MDIB_FILE_FILTER)):
                self.assertEqual(self.window.export_config(), target)
            self.assertIsInstance(config.load_file(target), NativeMdibConfig)
            bad = Path(directory) / "not-xml.json"
            with patch.object(QFileDialog, "getSaveFileName", return_value=(str(bad), config.MDIB_FILE_FILTER)), patch.object(QMessageBox, "warning") as warning:
                self.assertIsNone(self.window.export_config())
            self.assertTrue(warning.called)
            self.assertFalse(bad.exists())

    def test_startup_snapshot_and_console_commands_support_native_xml(self) -> None:
        from examples.console import ProviderShell

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "startup.xml"
            source.write_bytes(original_mdib())
            dialog = StartupDialog()
            dialog.config_box.setCurrentText(str(source))
            dialog._on_accept()
            self.assertIsNotNone(dialog.settings(), dialog.error_label.text())
            self.assertIsInstance(dialog.settings().device_config, NativeMdibConfig)
            shell = ProviderShell(self.service)
            target = Path(directory) / "saved.xml"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                shell.do_import(str(source))
                shell.do_export(str(target))
                shell.do_remove("demo.rate")
            self.assertIsInstance(config.load_file(target), NativeMdibConfig)
            self.assertIn("reconnect", output.getvalue().lower())
            self.assertIn("removed demo.rate", output.getvalue().lower())
            self.assertIsNone(self.service.mdib.entities.by_handle("demo.rate"))
            dialog.deleteLater()

    def test_console_export_writes_behaviour_on_request(self) -> None:
        from examples.console import ProviderShell
        from sdctoolbox.model import ActionSpec

        self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml")
        self.service.add_action(ActionSpec("Run", "demo.root", effects={"demo.mode": "RUN"}))
        shell = ProviderShell(self.service)
        with tempfile.TemporaryDirectory() as directory:
            plain, marked = Path(directory) / "plain.xml", Path(directory) / "with behaviour.xml"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                shell.do_export(str(plain))
                shell.do_export(f"--behaviour {marked}")
                shell.do_export(f"--behaviour {Path(directory) / 'profile.json'}")
            self.assertNotIn(b"urn:sdc-testing-toolbox:behaviour", plain.read_bytes())
            self.assertIn(b"urn:sdc-testing-toolbox:behaviour", marked.read_bytes())
            self.assertFalse((Path(directory) / "profile.json").exists())
            self.assertIn("--behaviour needs an .xml file", output.getvalue())

    def test_command_line_starts_from_native_snapshot(self) -> None:
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [sys.executable, str(root / "run_toolbox.py"), "--name", "native-cli-smoke", "--config",
             str(root / "presets" / "mdib-demo-infusion-pump.xml"), "--smoke-test"],
            cwd=root, capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_loaded_mdib_is_editable_in_the_pane(self) -> None:
        self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml")
        pane = self.window.provider_pane
        for button in (pane.new_button, pane.context_button, pane.new_alert_button):
            self.assertTrue(button.isEnabled(), button.text())
        rows = [row for row in range(pane.table.rowCount()) if "demo.rate" in
                " ".join(pane.table.item(row, column).text() for column in range(pane.table.columnCount())
                         if pane.table.item(row, column) is not None)]
        self.assertTrue(rows)
        pane.table.selectRow(rows[0])
        self.assertTrue(pane.remove_button.isEnabled())
        self.assertTrue(pane.board.card("demo.wave") is not None)

    def test_alarm_buttons_card_bin_and_waveform_editor_work_on_a_loaded_mdib(self) -> None:
        from mdib_test_support import adoption_mdib
        from sdc11073.xml_types import pm_types

        self.window.apply_config(NativeMdibConfig(adoption_mdib()), "adoption.xml")
        pane = self.window.provider_pane
        pane.select_alert_handle("demo.alarm")
        self.app.processEvents()
        self.assertTrue(pane.toggle_alert_button.isEnabled())
        self.assertEqual(pane.toggle_alert_button.text(), "Clear")
        self.assertTrue(pane.acknowledge_button.isEnabled())
        pane._on_acknowledge()
        self.assertEqual(self.service.signal_states("demo.alarm")[0].presence, str(pm_types.AlertSignalPresence.ACK))
        pane.toggle_alert_button.click()
        self.assertFalse(self.service.alert_present("demo.alarm"))
        pane.toggle_alert_button.click()
        self.assertTrue(self.service.alert_present("demo.alarm"))

        pane.select_alert_handle("demo.paused")
        self.app.processEvents()
        self.assertTrue(pane.remove_alert_button.isEnabled())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            pane.remove_alert_button.click()
            pane.board.delete_requested.emit("demo.mode")
        self.assertNotIn("demo.paused", self.service.list_alerts())
        self.assertIsNone(self.service.mdib.entities.by_handle("demo.mode"))

        for row in range(pane.table.rowCount()):
            if any(pane.table.item(row, column) is not None and pane.table.item(row, column).text() == "demo.wave"
                   for column in range(pane.table.columnCount())):
                pane.table.selectRow(row)
        self.app.processEvents()
        self.assertEqual(pane.selected_handle(), "demo.wave")
        self.assertTrue(pane.editor_stack.isEnabled(), "a loaded waveform can be edited like any other")

    def test_importing_a_behaviour_export_in_the_window_keeps_waveforms_moving(self) -> None:
        import time

        self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml")
        self.service.start_generator()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "behaviour.xml"
            config.save(self.service, target, behaviour=True)
            self.service.stop_generator()
            self.assertTrue(self.window.apply_config(config.load_file(target), target))
        self.assertTrue(self.service.generator_running)
        plot = self.window.provider_pane.board.card("demo.wave").control.plot
        plot.flush()
        before = plot.samples
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and plot.samples == before:
            self.app.processEvents()
            plot.flush()
            time.sleep(0.05)
        self.assertNotEqual(plot.samples, before, "the waveform card never received a generated block")

    def test_export_offers_plain_and_behaviour_mdib(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        from sdctoolbox.model import ActionSpec

        self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml")
        self.service.add_action(ActionSpec("Run", "demo.root", effects={"demo.mode": "RUN"}))
        with tempfile.TemporaryDirectory() as directory:
            for selected, expected in ((config.MDIB_FILE_FILTER, False), (config.MDIB_BEHAVIOUR_FILE_FILTER, True)):
                target = Path(directory) / f"{expected}.xml"
                with patch.object(QFileDialog, "getSaveFileName", return_value=(str(target), selected)) as dialog:
                    self.assertEqual(self.window.export_config(), target)
                self.assertEqual(dialog.call_args.kwargs["selectedFilter"], config.MDIB_FILE_FILTER)
                self.assertEqual(b"urn:sdc-testing-toolbox:behaviour" in target.read_bytes(), expected)

    def test_failed_activation_rebinds_recovered_provider_without_retiring_consumers(self) -> None:
        old_pane = self.window.provider_pane
        tabs = self.window.network_tabs
        original_start = self.service._start_graph
        attempts = 0
        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("GUI activation failure")
            return original_start(*args, **kwargs)
        with patch.object(self.service, "_start_graph", side_effect=fail_once):
            self.assertFalse(self.window.apply_config(NativeMdibConfig(original_mdib()), "original.xml"))
        self.assertIsNot(self.window.provider_pane, old_pane)
        self.assertIs(self.window.provider_pane.bridge._mdib, self.service.mdib)
        self.assertTrue(old_pane.bridge._closed)
        self.assertIs(self.window.network_tabs, tabs)


if __name__ == "__main__":
    unittest.main()
