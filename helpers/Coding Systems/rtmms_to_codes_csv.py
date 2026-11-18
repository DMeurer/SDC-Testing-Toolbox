"""Turn a NIST RTMMS term download (.xlsx) into a codes.csv the toolbox can load.

RTMMS (https://rtmms.nist.gov, free account, "Download Term Files") lists the IEEE 11073-10101
terms with the units each measure may use. The toolbox reads the result with "Load codes…";
its ``unit_codes`` column narrows the unit picker to the units RTMMS lists for a measure.

Standard library only. Rejected, withdrawn and deprecated terms are left out, and so are units
RTMMS gives only through a unit group (``UNIT_GROUPS`` without ``CF_UCODE10``), which the
download does not resolve.

Usage:  python "helpers/Coding Systems/rtmms_to_codes_csv.py" rtmms_base_terms.xlsx [codes.csv]
"""

from __future__ import annotations

import csv
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
COLUMNS = (
    "reference_id", "common_term", "systematic_name", "symbol", "part_code", "cf_code10", "unit_codes", "status",
)
LEFT_OUT = ("Rejected", "Withdrawn", "Deprecated")
REFERENCE_ID = re.compile(r"MDC(?:_[A-Z0-9]+)+")


def _column(ref: str) -> int:
    """The 0-based column of a cell reference such as "AB12"."""
    number = 0
    for letter in re.match(r"[A-Z]+", ref).group(0):
        number = number * 26 + ord(letter) - 64
    return number - 1


def sheets(path: Path) -> Iterator[tuple[str, list[list[str]]]]:
    """Every sheet of an .xlsx file as (name, rows of cell texts)."""
    with zipfile.ZipFile(path) as book:
        shared = []
        if "xl/sharedStrings.xml" in book.namelist():
            for item in ET.fromstring(book.read("xl/sharedStrings.xml")).findall("m:si", NS):
                shared.append("".join(t.text or "" for t in item.iter(f"{{{NS['m']}}}t")))
        targets = {rel.get("Id"): rel.get("Target") for rel in ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))}
        for sheet in ET.fromstring(book.read("xl/workbook.xml")).find("m:sheets", NS):
            target = targets[sheet.get(f"{{{NS['r']}}}id")].lstrip("/")
            target = target if target.startswith("xl/") else f"xl/{target}"
            rows = []
            for row in ET.fromstring(book.read(target)).iter(f"{{{NS['m']}}}row"):
                cells = {}
                for cell in row.findall("m:c", NS):
                    value = cell.find("m:v", NS)
                    text = value.text if value is not None else "".join(t.text or "" for t in cell.iter(f"{{{NS['m']}}}t"))
                    if cell.get("t") == "s" and text:
                        text = shared[int(text)]
                    cells[_column(cell.get("r"))] = text or ""
                rows.append([cells.get(i, "") for i in range(max(cells, default=-1) + 1)])
            yield sheet.get("name"), rows


def rows(path: Path) -> Iterator[dict[str, str]]:
    """The codes.csv rows of an RTMMS download: one per usable term of its partition sheets."""
    for name, table in sheets(path):
        if not re.fullmatch(r"partition_\d+", name) or not table or "RefID" not in table[0]:
            continue
        head = table[0]
        for cells in table[1:]:
            row = dict(zip(head, cells, strict=False))
            ref_id = REFERENCE_ID.match(row.get("RefID", "").strip())  # drops "(Preferred)" and the like
            if ref_id is None or row.get("Status", "").startswith(LEFT_OUT):
                continue
            yield {
                "reference_id": ref_id.group(0),
                "common_term": row.get("CommonTerm", "").strip(),
                "systematic_name": row.get("SystematicName", "").strip(),
                # A unit's first UCUM spelling, e.g. "mL/min"; RTMMS's own Symbol column is not plain text.
                "symbol": next(iter(row.get("UOM_UCUM", "").split()), "") if name == "partition_4" else "",
                "part_code": row.get("PartCode", "").strip(),
                "cf_code10": row.get("CF_CODE10", "").strip(),
                "unit_codes": " ".join(code for code in row.get("CF_UCODE10", "").split() if code.isdigit()),
                "status": row.get("Status", "").strip(),
            }


def convert(source: Path, target: Path) -> int:
    """Write the codes.csv for `source` to `target`; returns how many rows it wrote."""
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, COLUMNS)
        writer.writeheader()
        for row in rows(source):
            writer.writerow(row)
            written += 1
    return written


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.strip().rpartition("Usage:")[2].strip(), file=sys.stderr)
        return 2
    source = Path(argv[1])
    target = Path(argv[2]) if len(argv) == 3 else Path(__file__).parent / "output" / "rtmms.csv"  # noqa: PLR2004
    try:
        count = convert(source, target)
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(f"{source} is not an RTMMS term download that can be read: {exc}", file=sys.stderr)
        return 1
    print(f"{count} terms written to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
