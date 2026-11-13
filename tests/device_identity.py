"""Friendly name and DPWS identity: --friendly-name, the startup dialog and MDIB presets."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lxml import etree
from mdib_test_support import PM, original_mdib
from PySide6.QtWidgets import QApplication

import run_toolbox
from sdctoolbox import config
from sdctoolbox.gui.startup_dialog import StartupDialog
from sdctoolbox.mdib_io import NativeMdibConfig, load_mdib_file

PUMP = ROOT / "presets" / "mdib-demo-infusion-pump.xml"
JSON_PRESET = ROOT / "presets" / "ventilator.json"


def with_metadata(data: bytes) -> bytes:
    """The fixture with an MDS MetaData block, which is where a device says who made it."""
    root = etree.fromstring(data)
    mds = root.find(f".//{{{PM}}}Mds")
    meta = etree.Element(f"{{{PM}}}MetaData")
    for tag, text in (("Manufacturer", "Example Medical"), ("ModelName", "Demo 3000"),
                      ("ModelNumber", "D3K"), ("SerialNumber", "SN-42")):
        element = etree.SubElement(meta, f"{{{PM}}}{tag}")
        element.text = text
    # MetaData sits between Sco and SystemContext in the schema sequence.
    mds.insert(list(mds).index(mds.find(f"{{{PM}}}SystemContext")), meta)
    return etree.tostring(root)


class FriendlyNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def service_for(self, argv: list[str]):
        args = run_toolbox.parse_args(argv)
        settings = run_toolbox.settings_from_args(args)
        device_config = config.load_file(settings.config_path) if settings.config_path else None
        return run_toolbox.make_service(settings, device_config)

    def test_option_is_parsed_and_validated(self) -> None:
        self.assertEqual(run_toolbox.parse_args(["--friendly-name", "Bed 4"]).friendly_name, "Bed 4")
        self.assertIsNone(run_toolbox.parse_args([]).friendly_name)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            run_toolbox.parse_args(["--friendly-name", "bad\x01name"])
        self.assertEqual(raised.exception.code, 2)

    def test_option_overrides_a_json_preset(self) -> None:
        service = self.service_for(["--name", "fn-json", "--config", str(JSON_PRESET), "--friendly-name", "Bed 4"])
        self.assertEqual(service.friendly_name, "Bed 4")

    def test_json_default_is_unchanged(self) -> None:
        self.assertEqual(self.service_for(["--name", "fn-plain"]).friendly_name, "Toolbox fn-plain")

    def test_mdib_preset_announces_the_instance_name_unprefixed(self) -> None:
        service = self.service_for(["--name", "fn-mdib", "--config", str(PUMP)])
        self.assertEqual(service.friendly_name, "fn-mdib")

    def test_option_overrides_an_mdib_preset(self) -> None:
        service = self.service_for(["--name", "fn-mdib2", "--config", str(PUMP), "--friendly-name", "Pump A"])
        self.assertEqual(service.friendly_name, "Pump A")

    def test_mdib_metadata_becomes_the_dpws_identity(self) -> None:
        settings = run_toolbox.settings_from_args(run_toolbox.parse_args(["--name", "fn-meta"]))
        service = run_toolbox.make_service(settings, NativeMdibConfig(with_metadata(original_mdib())))
        self.assertEqual(service.device.manufacturer, "Example Medical")
        self.assertEqual(service.device.model_name, "Demo 3000")
        self.assertEqual(service.device.model_number, "D3K")
        self.assertEqual(service.serial_number, "SN-42")
        self.assertEqual(service.friendly_name, "fn-meta")

    def test_mdib_without_metadata_keeps_toolbox_model_and_instance_serial(self) -> None:
        settings = run_toolbox.settings_from_args(run_toolbox.parse_args(["--name", "fn-nometa"]))
        service = run_toolbox.make_service(settings, load_mdib_file(PUMP))
        self.assertEqual(service.serial_number, "fn-nometa")
        self.assertEqual(service.device.manufacturer, config.DeviceInfo().manufacturer)

    def test_startup_dialog_field(self) -> None:
        dialog = StartupDialog(name="fn-dialog")
        dialog._on_accept()
        self.assertIsNone(dialog.settings().friendly_name)
        dialog = StartupDialog(name="fn-dialog")
        dialog.friendly_name_edit.setText("  Theatre 2  ")
        dialog._on_accept()
        self.assertEqual(dialog.settings().friendly_name, "Theatre 2")
        self.assertEqual(run_toolbox.make_service(dialog.settings(), None).friendly_name, "Theatre 2")
        dialog = StartupDialog(name="fn-dialog-mdib")
        dialog.config_box.setCurrentText(str(PUMP))
        dialog.friendly_name_edit.setText("Pump A")
        dialog._on_accept()
        settings = dialog.settings()
        self.assertEqual(run_toolbox.make_service(settings, settings.device_config).friendly_name, "Pump A")
        dialog = StartupDialog(name="fn-dialog")
        dialog.friendly_name_edit.setText("bad\x01")
        dialog._on_accept()
        self.assertIsNone(dialog.settings())
        self.assertTrue(dialog.error_label.text())


if __name__ == "__main__":
    unittest.main()
