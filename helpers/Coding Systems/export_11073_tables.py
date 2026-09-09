"""Export table data from IEEE 11073 coding-system PDFs for local lookup.

PDF tables do not expose a canonical row/column model. This script keeps the
original reconstructed columns and values alongside a conservative normalized
lookup view, so uncertain extraction can always be traced to its source page.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import pymupdf
except ImportError as exc:  # pragma: no cover - startup guidance
    raise SystemExit(
        "Missing PyMuPDF. Install helper dependencies with "
        "'.venv\\Scripts\\python.exe -m pip install -r helpers\\requirements.txt'.",
    ) from exc

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
except ImportError:
    Workbook = None  # type: ignore[assignment,misc]


EXTRACTOR_VERSION = "1.1"
REFERENCE_ID = re.compile(r"\bMDC(?:_[A-Z0-9]+)+\b")
PART_CODE = re.compile(r"\b(?P<part>\d{1,3})\s*::\s*(?P<code>\d{1,6})\b")
CF_CODE = re.compile(r"\b\d{5,10}\b")
WHITESPACE = re.compile(r"\s+")
CODE_TABLE_MARKERS = ("mdc_", "part::code", "code10", "cf_code10", "reference id")
TRAILING_PART_AND_CODE = re.compile(r"\b(?P<part>\d{1,3})\s+(?P<code>\d{1,6})\s*$")

HEADER_ALIASES = {
    "reference_id": ("refid", "reference id", "reference identifier", "ref id"),
    "systematic_name": ("sysname", "systematic name"),
    "common_term": ("common term", "term", "display name"),
    "mnemonic": ("mnemonic", "acronym"),
    "description": ("description", "definition", "description definition"),
    "part_code": ("part code", "part::code", "part code10"),
    "part": ("part",),
    "code": ("code10", "term code", "numeric code", "code"),
    "cf_code10": ("cf code10", "cf_code10", "context free code"),
    "uom_mdc": ("uom mdc",),
    "uom_ucum": ("uom ucum",),
}


@dataclass
class ExportData:
    documents: list[dict[str, str]] = field(default_factory=list)
    tables: list[dict[str, str]] = field(default_factory=list)
    table_rows: list[dict[str, str]] = field(default_factory=list)
    codes: list[dict[str, str]] = field(default_factory=list)
    issues: list[dict[str, str]] = field(default_factory=list)


def clean(value: Any) -> str:
    """Return a stable one-line representation without changing identifiers."""
    if value is None:
        return ""
    return WHITESPACE.sub(" ", str(value)).strip()


def normalized_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def document_id(path: Path) -> str:
    return hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:12]


def parse_page_range(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"(\d+)-(\d+)", value)
    if match is None:
        msg = "--pages must use inclusive PDF page numbers, for example 860-990"
        raise argparse.ArgumentTypeError(msg)
    start, end = map(int, match.groups())
    if start < 1 or end < start:
        msg = "--pages must have positive start and end >= start"
        raise argparse.ArgumentTypeError(msg)
    return start, end


def page_text(page: Any) -> str:
    return clean(page.get_text("text", sort=True))


def find_title(page: Any, table_bbox: tuple[float, float, float, float]) -> str:
    """Use nearby text as a title without treating a guessed title as a key."""
    candidates: list[tuple[float, str]] = []
    for block in page.get_text("blocks", sort=True):
        x0, y0, _x1, y1, text, *_rest = block
        candidate = clean(text)
        if not candidate or y1 > table_bbox[1] or table_bbox[1] - y1 > 120:
            continue
        if x0 > table_bbox[2] or y0 < 35:
            continue
        if "table" in candidate.casefold() or candidate.startswith(("A.", "B.", "C.", "D.")):
            candidates.append((y1, candidate))
    return candidates[-1][1] if candidates else ""


def deduplicate_headers(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(values, start=1):
        base = clean(value) or f"column_{index}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        result.append(base if count == 1 else f"{base} ({count})")
    return result


def looks_like_header(row: list[str]) -> bool:
    text = " ".join(row).casefold()
    return (
        len([value for value in row if value]) >= 2
        and any(token in text for token in ("refid", "reference id", "sysname", "part", "code10", "description"))
    )


def header_index(headers: list[str], field: str) -> int | None:
    aliases = HEADER_ALIASES[field]
    for index, header in enumerate(headers):
        candidate = normalized_header(header)
        if candidate in aliases:
            return index
    return None


def row_value(row: list[str], headers: list[str], field: str) -> str:
    index = header_index(headers, field)
    return row[index] if index is not None and index < len(row) else ""


def find_reference_id(values: Iterable[str]) -> str:
    cells = list(values)
    for start, value in enumerate(cells):
        match = re.search(r"MDC(?:_[A-Z0-9]+)*_?", value)
        if match is None:
            continue
        # PDF table extraction often breaks a RefId at an underscore boundary.
        # Continue through identifier fragments only, never into a numeric column.
        candidate = match.group(0)
        for fragment in cells[start + 1:]:
            fragment = clean(fragment)
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", fragment):
                break
            candidate += fragment
        candidate = candidate.rstrip("_")
        if REFERENCE_ID.fullmatch(candidate) is not None:
            return candidate
    return ""


def find_part_code(values: Iterable[str]) -> tuple[str, str, str]:
    for value in values:
        match = PART_CODE.search(value)
        if match is not None:
            part, code = match.group("part"), match.group("code")
            return part, code, f"{part}::{code}"
    return "", "", ""


def find_cf_code(values: Iterable[str], part: str, code: str) -> str:
    for value in values:
        match = CF_CODE.search(value)
        if match is not None:
            return match.group(0)
    if part.isdecimal() and code.isdecimal():
        return str(int(part) * 65536 + int(code))
    return ""


def normalize_code_row(headers: list[str], values: list[str]) -> dict[str, str]:
    reference_id = row_value(values, headers, "reference_id") or find_reference_id(values)
    part, code, part_code = find_part_code([row_value(values, headers, "part_code"), *values])
    if not part_code:
        part = row_value(values, headers, "part")
        code = row_value(values, headers, "code")
        if part.isdecimal() and code.isdecimal():
            part_code = f"{part}::{code}"
    cf_code10 = row_value(values, headers, "cf_code10") or find_cf_code([], part, code)
    return {
        "reference_id": reference_id,
        "systematic_name": row_value(values, headers, "systematic_name"),
        "common_term": row_value(values, headers, "common_term"),
        "mnemonic": row_value(values, headers, "mnemonic"),
        "description": row_value(values, headers, "description"),
        "part": part,
        "code": code,
        "part_code": part_code,
        "cf_code10": cf_code10,
        "uom_mdc": row_value(values, headers, "uom_mdc"),
        "uom_ucum": row_value(values, headers, "uom_ucum"),
        "validation_status": "exact_reference_id_and_part_code" if reference_id and part_code else "raw_table_row_only",
    }


def extract_code_lines(
    text: str,
    document: dict[str, str],
    page_number: int,
    data: ExportData,
) -> None:
    """Build a strict code index from the PDF's ordered text layer.

    In dense IEEE tables this is more reliable than a generic table grid: the
    whole visual row remains a text line even when the grid splits its columns.
    """
    title = ""
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = clean(line)
        if "table " in line.casefold():
            title = line
        reference = find_reference_id([line])
        if not reference:
            continue
        part, code, part_code = find_part_code([line])
        if not part_code:
            trailing = TRAILING_PART_AND_CODE.search(line)
            if trailing is None:
                continue
            part, code = trailing.group("part"), trailing.group("code")
            part_code = f"{part}::{code}"
        cf_code10 = find_cf_code([], part, code)
        table_id = f"{document['document_id']}-p{page_number}-text"
        data.codes.append({
            "table_row_id": f"{table_id}-r{line_number}",
            "table_id": table_id,
            "document_id": document["document_id"],
            "source_filename": document["source_filename"],
            "source_pdf_page": str(page_number),
            "source_row_number": str(line_number),
            "table_title_raw": title,
            "source_columns_json": json.dumps(["source_text_line"], ensure_ascii=False),
            "source_values_json": json.dumps([line], ensure_ascii=False),
            "source_row_json": json.dumps({"source_text_line": line}, ensure_ascii=False),
            "reference_id": reference,
            "systematic_name": "",
            "common_term": "",
            "mnemonic": "",
            "description": "",
            "part": part,
            "code": code,
            "part_code": part_code,
            "cf_code10": cf_code10,
            "uom_mdc": "",
            "uom_ucum": "",
            "validation_status": "text_line_reference_id_and_part_code",
        })


def inferred_headers(title: str, width: int) -> list[str] | None:
    """Label recurring Annex C code tables when their printed header is off-page."""
    normalized_title = title.casefold()
    if width == 5 and ("partition" in normalized_title or "c.4" in normalized_title):
        return ["RefId", "Disc", "Part::Code", "CF_CODE10", "column_5"]
    return None


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    columns = list(dict.fromkeys(column for row in rows for column in row))
    with path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_workbook(path: Path, datasets: dict[str, list[dict[str, str]]]) -> None:
    if Workbook is None:
        return
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in datasets.items():
        worksheet = workbook.create_sheet(name)
        columns = list(dict.fromkeys(column for row in rows for column in row))
        worksheet.append(columns)
        for row in rows:
            worksheet.append([row.get(column, "") for column in columns])
        if columns:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for cell in worksheet[1]:
                cell.font = Font(bold=True)
            for index, column in enumerate(columns, start=1):
                values = [len(str(row.get(column, ""))) for row in rows[:500]]
                worksheet.column_dimensions[get_column_letter(index)].width = min(60, max(12, max(values, default=0) + 2))
    workbook.save(path)


def extract_document(path: Path, page_range: tuple[int, int] | None, data: ExportData) -> None:
    identifier = document_id(path)
    timestamp = datetime.now(UTC).isoformat()
    try:
        pdf = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - a corrupt PDF must be recorded
        data.issues.append({
            "document_id": identifier,
            "source_filename": path.name,
            "source_pdf_page": "",
            "issue": "document_open_failed",
            "detail": str(exc),
        })
        return

    document = {
        "document_id": identifier,
        "source_filename": path.name,
        "source_path": str(path.resolve()),
        "sha256": source_hash(path),
        "source_file_size_bytes": str(path.stat().st_size),
        "pdf_page_count": str(pdf.page_count),
        "extraction_engine": f"PyMuPDF {pymupdf.VersionBind}",
        "extractor_version": EXTRACTOR_VERSION,
        "extraction_timestamp_utc": timestamp,
    }
    data.documents.append(document)

    start, end = page_range or (1, pdf.page_count)
    for page_number in range(start, min(end, pdf.page_count) + 1):
        page = pdf.load_page(page_number - 1)
        text = page_text(page)
        if not text:
            data.issues.append({
                "document_id": identifier,
                "source_filename": path.name,
                "source_pdf_page": str(page_number),
                "issue": "no_machine_readable_text",
                "detail": "Skipped without OCR to avoid corrupting identifiers and codes.",
            })
            continue
        if not any(marker in text.casefold() for marker in CODE_TABLE_MARKERS):
            continue
        extract_code_lines(text, document, page_number, data)
        try:
            tables = page.find_tables(strategy="text")
        except Exception as exc:  # noqa: BLE001 - page-level extraction is recoverable
            data.issues.append({
                "document_id": identifier,
                "source_filename": path.name,
                "source_pdf_page": str(page_number),
                "issue": "table_detection_failed",
                "detail": str(exc),
            })
            continue

        for table_number, table in enumerate(tables.tables, start=1):
            raw_rows = [[clean(cell) for cell in row] for row in table.extract()]
            raw_rows = [row for row in raw_rows if any(row)]
            if not raw_rows:
                continue
            bbox = table.bbox
            title = find_title(page, bbox)
            raw_headers = raw_rows[0] if looks_like_header(raw_rows[0]) else []
            guessed_headers = inferred_headers(title, len(raw_rows[0]))
            headers = deduplicate_headers(
                raw_headers or guessed_headers or [f"column_{index}" for index in range(1, len(raw_rows[0]) + 1)],
            )
            body_rows = raw_rows[1:] if raw_headers else raw_rows
            table_id = f"{identifier}-p{page_number}-t{table_number}"
            data.tables.append({
                "table_id": table_id,
                "document_id": identifier,
                "source_filename": path.name,
                "source_pdf_page": str(page_number),
                "table_number_on_page": str(table_number),
                "table_title_raw": title,
                "header_raw_json": json.dumps(raw_headers, ensure_ascii=False),
                "source_columns_json": json.dumps(headers, ensure_ascii=False),
                "row_count": str(len(body_rows)),
                "bbox": ",".join(f"{value:.1f}" for value in bbox),
                "extraction_status": "detected",
            })
            for row_number, row in enumerate(body_rows, start=1):
                values = row[:len(headers)] + [""] * max(0, len(headers) - len(row))
                raw = {
                    "table_row_id": f"{table_id}-r{row_number}",
                    "table_id": table_id,
                    "document_id": identifier,
                    "source_filename": path.name,
                    "source_pdf_page": str(page_number),
                    "source_row_number": str(row_number),
                    "table_title_raw": title,
                    "source_columns_json": json.dumps(headers, ensure_ascii=False),
                    "source_values_json": json.dumps(values, ensure_ascii=False),
                    "source_row_json": json.dumps(dict(zip(headers, values, strict=True)), ensure_ascii=False),
                }
                data.table_rows.append(raw)
    pdf.close()


def parse_arguments() -> argparse.Namespace:
    default_input = Path(
        r"C:\Users\meurdode\OneDrive - B. Braun\Dokumente\private\20 Bildung\AIN\Module"
        r"\00__Thesis\Literatur\ISO-IEEE-11073\CodingSystems",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=default_input, help="PDF or directory containing coding-system PDFs")
    parser.add_argument("--output", type=Path, default=Path("helpers/output"), help="Directory for generated files")
    parser.add_argument("--pages", type=parse_page_range, help="Inclusive PDF page range, e.g. 860-990")
    parser.add_argument("--csv-only", action="store_true", help="Do not create the optional Excel workbook")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    source = args.input.expanduser().resolve()
    if not source.exists():
        print(f"Input does not exist: {source}", file=sys.stderr)
        return 2
    paths = [source] if source.is_file() else sorted(source.glob("*.pdf"))
    if not paths:
        print(f"No PDFs found in: {source}", file=sys.stderr)
        return 2

    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    data = ExportData()
    for path in paths:
        print(f"Extracting {path.name}")
        extract_document(path, args.pages, data)

    datasets = {
        "Codes": data.codes,
        "Table rows": data.table_rows,
        "Tables": data.tables,
        "Documents": data.documents,
        "Issues": data.issues,
    }
    write_csv(output / "codes.csv", data.codes)
    write_csv(output / "table_rows.csv", data.table_rows)
    write_csv(output / "tables.csv", data.tables)
    write_csv(output / "documents.csv", data.documents)
    write_csv(output / "issues.csv", data.issues)
    if not args.csv_only:
        if Workbook is None:
            print("openpyxl is unavailable; wrote CSV files only.", file=sys.stderr)
        else:
            write_workbook(output / "ieee-11073-coding-systems.xlsx", datasets)

    print(
        f"Wrote {len(data.codes)} lookup rows, {len(data.table_rows)} table rows, "
        f"{len(data.tables)} tables, and {len(data.issues)} issues to {output}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
