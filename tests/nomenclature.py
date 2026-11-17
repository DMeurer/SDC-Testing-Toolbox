"""The MDC code list, its search, the user's own codes.csv, and the Pick an MDC code dialog.

Usage:  .venv/Scripts/python.exe tests/nomenclature.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox, QPushButton  # noqa: E402

from sdctoolbox import constants, nomenclature  # noqa: E402
from sdctoolbox.gui import code_picker  # noqa: E402
from sdctoolbox.gui.code_picker import CodePickerDialog  # noqa: E402
from sdctoolbox.gui.new_metric_dialog import NewMetricDialog  # noqa: E402
from sdctoolbox.model import Coding  # noqa: E402

# Made up for the test: not rows of the standard.
CSV_HEADER = "reference_id,common_term,systematic_name,part,code,part_code,cf_code10\n"
CSV_ROWS = (
    "MDC_TOY_WIDGET_RATE,Toy widget rate,,2,65001,2::65001,196073\n"  # usable
    "MDC_TOY_UNIT,,Toy unit,4,65002,4::65002,327146\n"  # usable, term from systematic_name
    "MDC_TOY_ALARM,Toy alarm,,3,65003,3::65003,\n"  # usable without cf_code10
    "MDC_TOY_WRONG_CF,Wrong sum,,2,65004,2::65004,1\n"  # cf_code10 disagrees: skipped
    "MDC_TOY_BAD_CODE,Bad code,,2,x,2::x,\n"  # no valid code: skipped
    "not_a_ref_id,Lower case,,2,65005,2::65005,196077\n"  # no reference ID: skipped
    "MDC_PULS_RATE,Already shipped,,2,18442,2::18442,149514\n"  # shipped: skipped
    "MDC_TOY_WIDGET_RATE,Toy widget rate again,,2,65001,2::65001,196073\n"  # duplicate: first wins
)


def write(directory: str, name: str, text: str, encoding: str = "utf-8") -> Path:
    path = Path(directory) / name
    path.write_text(text, encoding=encoding)
    return path


def forget_loaded_codes() -> None:
    nomenclature._loaded.clear()  # noqa: SLF001 - reset the session state between tests
    nomenclature.loaded_from = None
    code_picker._remembered_tried = False  # noqa: SLF001


class NomenclatureTests(unittest.TestCase):
    def tearDown(self) -> None:
        forget_loaded_codes()

    def test_units_per_measure_are_shipped_units_of_shipped_measurements(self) -> None:
        self.assertEqual(nomenclature.units_for("149514"), ("264864", "264672"))  # bpm, 1/min
        self.assertEqual(nomenclature.units_for(184352), ())  # a ventilation mode has none
        for measure, units in nomenclature._UNITS.items():  # noqa: SLF001
            self.assertEqual(nomenclature.lookup(measure).kind, "measurement", measure)
            self.assertTrue(units and all(nomenclature.lookup(u).kind == "unit" for u in units), measure)

    def test_shipped_terms_are_unique_and_well_formed(self) -> None:
        codes = [term.code for term in nomenclature.SHIPPED]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len({term.ref_id for term in nomenclature.SHIPPED}), len(codes))
        for term in nomenclature.SHIPPED:
            self.assertRegex(term.ref_id, r"^MDC(_[A-Z0-9]+)+$")
            self.assertEqual(str(int(term.code)), term.code)
            self.assertTrue(term.term, term)
            self.assertIn(term.kind, ("measurement", "unit", "alert"), term)
            self.assertEqual(bool(term.symbol), term.kind == "unit", term)
        self.assertGreaterEqual(len(codes), 150)

    def test_every_metric_code_a_preset_uses_ships(self) -> None:
        mdc = constants.CODING_SYSTEM_MDC
        used = set()
        for path in (ROOT / "presets").glob("*.xml"):
            for element in ET.parse(path).iter():
                tag = element.tag.rpartition("}")[2]
                if tag in {"Type", "Unit", "DomainUnit", "MeasurementUnit"} and element.get("CodingSystem", mdc) == mdc:
                    used.add(element.get("Code"))
        self.assertTrue(used)
        self.assertEqual(sorted(code for code in used if nomenclature.lookup(code) is None), [])

    def test_the_old_mdc_oid_is_read_as_mdc_and_written_as_the_new_one(self) -> None:
        coding = Coding.from_wire(code="147842", coding_system=constants.CODING_SYSTEM_MDC_LEGACY)
        self.assertEqual((coding.system, coding.coding_system), ("mdc", "urn:oid:1.3.111.2.11073.10101.3"))
        self.assertTrue(coding.is_standard)

    def test_kind_comes_from_the_partition(self) -> None:
        self.assertEqual(nomenclature.kind_of("149514"), "measurement")  # 2::18442
        self.assertEqual(nomenclature.kind_of("16928802"), "measurement")  # 258::20514, a setting
        self.assertEqual(nomenclature.kind_of("266016"), "unit")  # 4::3872
        self.assertEqual(nomenclature.kind_of("196688"), "alert")  # 3::80
        self.assertEqual(nomenclature.kind_of(str(7 << 16)), "other")

    def test_lookup_by_code_or_reference_id(self) -> None:
        self.assertEqual(nomenclature.lookup("149514").ref_id, "MDC_PULS_RATE")
        self.assertEqual(nomenclature.lookup(266016).symbol, "mmHg")
        self.assertEqual(nomenclature.lookup(" MDC_DIM_BEAT_PER_MIN ").code, "264864")
        self.assertIsNone(nomenclature.lookup("1"))
        self.assertIsNone(nomenclature.lookup("pulse rate"))

    def test_captions(self) -> None:
        self.assertEqual(nomenclature.lookup("149514").caption, "Pulse rate · MDC_PULS_RATE (149514)")
        unit = nomenclature.lookup("264864")
        self.assertEqual(unit.caption, "bpm · MDC_DIM_BEAT_PER_MIN (264864)")
        self.assertEqual(unit.display, "bpm, beats per minute")

    def test_search_puts_the_best_match_first(self) -> None:
        def refs(text: str, kind: str | None = None) -> list[str]:
            return [term.ref_id for term in nomenclature.search(text, kind)]

        self.assertEqual(refs("MDC_PULS_RATE")[0], "MDC_PULS_RATE")
        self.assertEqual(refs("149530"), ["MDC_PULS_OXIM_PULS_RATE"])
        self.assertEqual(refs("PULSE RATE", "measurement")[0], "MDC_PULS_RATE")
        self.assertEqual(refs("spo2"), ["MDC_PULS_OXIM_SAT_O2"])
        self.assertEqual(refs("mmhg", "unit"), ["MDC_DIM_MMHG"])
        self.assertEqual(refs("temp", "measurement")[0], "MDC_TEMP")
        self.assertTrue(all(ref.startswith("MDC_PULS_OXIM") for ref in refs("puls_oxim")))
        self.assertIn("MDC_AWAY_CO2_ET", refs("co2 end-tidal"), "every word has to match, in any order")
        self.assertEqual(refs("pulse rate", "unit"), [])
        self.assertEqual(refs("no such thing at all"), [])

    def test_search_filters_by_kind_and_limits(self) -> None:
        alerts = nomenclature.search("", "alert")
        self.assertEqual([t.ref_id for t in alerts[:2]], ["MDC_EVT_HI", "MDC_EVT_LO"], "table order without text")
        self.assertTrue(all(term.kind == "alert" for term in alerts))
        self.assertEqual(len(nomenclature.search("")), len(nomenclature.SHIPPED))
        self.assertEqual(len(nomenclature.search("", None, 5)), 5)
        with self.assertRaisesRegex(ValueError, "colour"):
            nomenclature.search("rate", "colour")

    def test_a_codes_csv_adds_its_usable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write(directory, "codes.csv", CSV_HEADER + CSV_ROWS, encoding="utf-8-sig")
            self.assertEqual(nomenclature.load_csv(path), 3)
        self.assertEqual(nomenclature.loaded_from, path)
        widget = nomenclature.lookup("MDC_TOY_WIDGET_RATE")
        self.assertEqual((widget.code, widget.term, widget.kind, widget.shipped),
                         (str((2 << 16) + 65001), "Toy widget rate", "measurement", False))
        self.assertEqual(nomenclature.lookup("MDC_TOY_UNIT").label, "Toy unit", "no symbol: the term labels it")
        self.assertEqual(nomenclature.lookup("MDC_TOY_ALARM").kind, "alert")
        for skipped in ("MDC_TOY_WRONG_CF", "MDC_TOY_BAD_CODE"):
            self.assertIsNone(nomenclature.lookup(skipped))
        self.assertEqual(nomenclature.lookup("149514").term, "Pulse rate", "the shipped term wins")
        self.assertEqual([t.ref_id for t in nomenclature.search("toy", "unit")], ["MDC_TOY_UNIT"])
        self.assertEqual(nomenclature.search("rate")[0].shipped, True, "shipped terms rank first")

    def test_a_malformed_codes_csv_is_refused_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            nomenclature.load_csv(write(directory, "good.csv", CSV_HEADER + CSV_ROWS))
            for name, text, encoding, fragment in (
                ("columns.csv", "ref,code\nMDC_X,1\n", "utf-8", "columns"),
                ("rows.csv", CSV_HEADER + "MDC_TOY,,,2,x,2::x,\n", "utf-8", "no row"),
                ("empty.csv", "", "utf-8", "columns"),
                ("binary.csv", "reference_id,part_code\nMDC_\xe9,2::1\n", "latin-1", "could not be read"),
            ):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(nomenclature.NomenclatureFileError, fragment):
                        nomenclature.load_csv(write(directory, name, text, encoding))
                    self.assertEqual(nomenclature.loaded_from.name, "good.csv")
                    self.assertIsNotNone(nomenclature.lookup("MDC_TOY_WIDGET_RATE"))
            with self.assertRaises(nomenclature.NomenclatureFileError):
                nomenclature.load_csv(Path(directory) / "missing.csv")


class PickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        # Remembered choices go to a scratch ini file, never the user's own.
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, cls.settings_dir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.settings_dir.cleanup()

    def setUp(self) -> None:
        code_picker.settings().clear()
        forget_loaded_codes()
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        forget_loaded_codes()
        self.directory.cleanup()

    def test_settings_stay_in_the_test_directory(self) -> None:
        self.assertTrue(code_picker.settings().fileName().startswith(self.settings_dir.name.replace("\\", "/")))

    def test_search_filters_live_and_double_click_picks(self) -> None:
        dialog = CodePickerDialog("unit")
        self.assertEqual(dialog.windowTitle(), "Pick an MDC code")
        self.assertEqual(dialog.kind_box.currentText(), "Units")
        dialog.search_edit.setText("mmHg")
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual([dialog.table.item(0, column).text() for column in range(3)],
                         ["266016", "MDC_DIM_MMHG", "mmHg, millimetre of mercury"])
        dialog.search_edit.setText("pulse rate")
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
        dialog.kind_box.setCurrentIndex(0)  # What is measured
        self.assertEqual(dialog.table.item(0, 1).text(), "MDC_PULS_RATE")
        dialog.table.cellDoubleClicked.emit(0, 0)
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertEqual(dialog.term().code, "149514")
        dialog.kind_box.setCurrentIndex(2)  # Alarm codes
        dialog.search_edit.setText("occlusion")
        self.assertEqual(dialog.term().ref_id, "MDC_EVT_OCCL")
        self.assertIn(f"{len(nomenclature.SHIPPED)} common codes ship", dialog.source_label.text())

    def test_units_narrow_to_the_measure_until_unticked(self) -> None:
        dialog = CodePickerDialog("unit", measure=nomenclature.lookup("149514"))
        self.assertEqual(dialog.only_units_box.text(), "Only units for Pulse rate (MDC_PULS_RATE)")
        self.assertTrue(dialog.only_units_box.isChecked())
        self.assertEqual([dialog.table.item(r, 1).text() for r in range(dialog.table.rowCount())],
                         ["MDC_DIM_BEAT_PER_MIN", "MDC_DIM_PER_MIN"])
        dialog.search_edit.setText("beat")
        self.assertEqual(dialog.term().ref_id, "MDC_DIM_BEAT_PER_MIN")
        dialog.search_edit.setText("mmHg")
        self.assertEqual(dialog.table.rowCount(), 0)
        dialog.only_units_box.setChecked(False)
        self.assertEqual(dialog.term().ref_id, "MDC_DIM_MMHG")
        dialog.kind_box.setCurrentIndex(0)  # What is measured: the box has no say
        self.assertFalse(dialog._form.isRowVisible(dialog._only_units_row))  # noqa: SLF001
        self.assertFalse(CodePickerDialog("unit")._form.isRowVisible(2), "no measure, no box")  # noqa: SLF001
        modes = CodePickerDialog("unit", measure=nomenclature.lookup("184352"))
        self.assertFalse(modes._form.isRowVisible(modes._only_units_row), "no known units, no box")  # noqa: SLF001

    def test_load_codes_csv_adds_and_remembers_the_file(self) -> None:
        path = write(self.directory.name, "codes.csv", CSV_HEADER + CSV_ROWS)
        dialog = CodePickerDialog(None)
        self.assertEqual(dialog.kind_box.currentText(), "Everything")
        with patch.object(code_picker.QFileDialog, "getOpenFileName", return_value=(str(path), "")):
            dialog.load_button.click()
        dialog.search_edit.setText("toy widget")
        self.assertEqual(dialog.term().ref_id, "MDC_TOY_WIDGET_RATE")
        self.assertIn("3 more come from codes.csv", dialog.source_label.text())
        self.assertEqual(Path(code_picker.settings().value(code_picker.SETTINGS_KEY)), path)

        forget_loaded_codes()  # the next session
        CodePickerDialog()
        self.assertEqual(nomenclature.loaded_from, path)

    def test_a_malformed_file_is_reported_and_ignored(self) -> None:
        good = write(self.directory.name, "codes.csv", CSV_HEADER + CSV_ROWS)
        bad = write(self.directory.name, "bad.csv", "nothing,useful\n1,2\n")
        dialog = CodePickerDialog()
        with patch.object(code_picker.QFileDialog, "getOpenFileName", return_value=(str(good), "")):
            dialog.load_button.click()
        with patch.object(code_picker.QFileDialog, "getOpenFileName", return_value=(str(bad), "")), \
                patch.object(QMessageBox, "warning") as warning:
            dialog.load_button.click()
        self.assertEqual(warning.call_args.args[1], "Could not load codes")
        self.assertEqual(nomenclature.loaded_from, good, "the good file stays loaded")
        self.assertEqual(Path(code_picker.settings().value(code_picker.SETTINGS_KEY)), good)

    def test_a_remembered_file_that_no_longer_loads_is_forgotten(self) -> None:
        code_picker.settings().setValue(code_picker.SETTINGS_KEY, str(Path(self.directory.name) / "gone.csv"))
        with patch.object(QMessageBox, "warning") as warning:
            CodePickerDialog()
            CodePickerDialog()
        self.assertEqual(warning.call_count, 1, "tried once per session")
        self.assertIsNone(code_picker.settings().value(code_picker.SETTINGS_KEY))
        self.assertIsNone(nomenclature.loaded_from)


class NewDataSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.settings_dir = tempfile.TemporaryDirectory()
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, cls.settings_dir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.settings_dir.cleanup()

    def pick(self, dialog: NewMetricDialog, widget, search: str) -> None:  # noqa: ANN001
        """Press the row's Pick…, search in the picker and press OK."""
        def run(picker: CodePickerDialog) -> int:
            self.assertEqual(picker.kind_box.currentData(), "unit" if widget is dialog.unit_widget else "measurement")
            picker.search_edit.setText(search)
            return QDialog.Accepted

        with patch.object(CodePickerDialog, "exec", run):
            widget.findChild(QPushButton).click()

    def new_dialog(self) -> NewMetricDialog:
        dialog = NewMetricDialog()
        dialog.label_edit.setText("Pulse")
        dialog.minimum_edit.setText("0")  # a number needs both ends (10701 TR0239)
        dialog.maximum_edit.setText("300")
        return dialog

    def test_rows_sit_after_label(self) -> None:
        dialog = self.new_dialog()
        rows = [dialog.form.itemAt(row, dialog.form.ItemRole.LabelRole).widget().text() for row in range(4)]
        self.assertEqual(rows, ["Kind", "Label", "Measures", "Unit"])
        self.assertEqual(dialog.measures_widget.findChild(QPushButton).text(), "Pick…")
        self.assertEqual(dialog.measures_widget.findChild(QPushButton).property("kind"), "design-outline")

    def test_the_unit_picker_knows_the_measure(self) -> None:
        dialog = self.new_dialog()
        self.pick(dialog, dialog.measures_widget, "pulse rate")
        seen = []
        with patch.object(CodePickerDialog, "exec", lambda picker: seen.append(picker.only_units_box.text()) or 0):
            dialog.unit_widget.findChild(QPushButton).click()
        self.assertEqual(seen, ["Only units for Pulse rate (MDC_PULS_RATE)"])

    def test_picked_terms_become_the_codings(self) -> None:
        dialog = self.new_dialog()
        self.pick(dialog, dialog.measures_widget, "pulse rate")
        self.pick(dialog, dialog.unit_widget, "bpm")
        self.assertEqual(dialog.measures_edit.text(), "Pulse rate · MDC_PULS_RATE (149514)")
        self.assertEqual(dialog.unit_edit.text(), "bpm · MDC_DIM_BEAT_PER_MIN (264864)")
        dialog._on_accept()  # noqa: SLF001 - what OK does
        spec = dialog.spec()
        self.assertEqual(spec.type_coding, Coding(code="149514", system="mdc", label="Pulse rate"))
        self.assertEqual(spec.unit_coding, Coding(code="264864", system="mdc", label="bpm"))
        self.assertEqual(spec.unit_label, "bpm")
        self.assertEqual(spec.effective_type().coding_system, constants.CODING_SYSTEM_MDC)

    def test_typed_units_stay_labels_and_typed_codes_count(self) -> None:
        dialog = self.new_dialog()
        self.pick(dialog, dialog.unit_widget, "bpm")
        dialog.unit_edit.setText("beats")  # typed over the pick
        dialog.measures_edit.setText("MDC_PULS_OXIM_SAT_O2")
        dialog._on_accept()  # noqa: SLF001
        spec = dialog.spec()
        self.assertEqual((spec.unit_coding, spec.unit_label), (None, "beats"))
        self.assertEqual(spec.type_coding.code, "150456")

    def test_nothing_picked_keeps_the_private_type(self) -> None:
        dialog = self.new_dialog()
        dialog._on_accept()  # noqa: SLF001
        self.assertIsNone(dialog.spec().type_coding)
        self.assertEqual(dialog.spec().effective_type().system, "private")

    def test_an_unknown_typed_code_is_refused(self) -> None:
        dialog = self.new_dialog()
        dialog.measures_edit.setText("heartbeat thing")
        dialog._on_accept()  # noqa: SLF001
        self.assertIsNone(dialog.spec())
        self.assertFalse(dialog.error_label.isHidden())
        self.assertIn("Pick…", dialog.error_label.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
