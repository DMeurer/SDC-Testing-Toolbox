"""Exercise text-row extraction without requiring optional PDF/Excel packages."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "helpers" / "Coding Systems" / "export_11073_tables.py"


def load_exporter():
    spec = importlib.util.spec_from_file_location("coding_export_under_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    # Only the external PDF reader is substituted; extraction runs the actual helper.
    with patch.dict(sys.modules, {"pymupdf": ModuleType("pymupdf"), spec.name: module}):
        spec.loader.exec_module(module)
    return module


EXPORTER = load_exporter()


class CodingExportTest(unittest.TestCase):
    def extract(self, text: str):
        page = SimpleNamespace(get_text=lambda *args, **kwargs: text)
        data = EXPORTER.ExportData()
        EXPORTER.extract_code_lines(
            EXPORTER.page_text(page),
            {"document_id": "fixture", "source_filename": "fixture.pdf"},
            3,
            data,
        )
        return data.codes

    def test_each_pdf_text_line_produces_its_own_code_row(self) -> None:
        rows = self.extract("  MDC_FIRST  1::1\n  MDC_SECOND 1::2\n")
        self.assertEqual(
            [(row["reference_id"], row["part_code"], row["cf_code10"]) for row in rows],
            [("MDC_FIRST", "1::1", "65537"), ("MDC_SECOND", "1::2", "65538")],
        )
        self.assertEqual([row["source_row_number"] for row in rows], ["1", "2"])
        self.assertEqual(json.loads(rows[1]["source_values_json"]), ["MDC_SECOND 1::2"])

    def test_identifiers_and_codes_from_different_lines_are_not_joined(self) -> None:
        self.assertEqual(self.extract("MDC_FIRST\nUnrelated row 2::3\n"), [])

    def test_whitespace_only_page_keeps_missing_text_diagnostic(self) -> None:
        page = SimpleNamespace(get_text=lambda *args, **kwargs: " \n\t\n")
        pdf = SimpleNamespace(page_count=1, load_page=lambda index: page, close=lambda: None)
        with TemporaryDirectory(prefix="coding-export-") as temporary, patch.object(
            EXPORTER.pymupdf, "open", return_value=pdf, create=True,
        ), patch.object(EXPORTER.pymupdf, "VersionBind", "fixture", create=True):
            source = Path(temporary) / "blank.pdf"
            source.write_bytes(b"fixture")
            data = EXPORTER.ExportData()
            EXPORTER.extract_document(source, None, data)
        self.assertEqual(
            [(issue["source_pdf_page"], issue["issue"]) for issue in data.issues],
            [("1", "no_machine_readable_text")],
        )
        self.assertEqual(data.codes, [])


if __name__ == "__main__":
    unittest.main()
