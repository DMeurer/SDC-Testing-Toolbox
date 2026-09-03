"""Check licensing sources or the legal payload in a final build archive."""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from legal_payload import INVENTORY_NAME, validate_payload
from tests.script_support import Report


def _archive_files(path: Path) -> dict[str, bytes]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return {
                info.filename: archive.read(info)
                for info in archive.infolist()
                if not info.is_dir()
            }
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as archive:
            return {
                member.name: extracted.read()
                for member in archive.getmembers()
                if member.isfile() and (extracted := archive.extractfile(member)) is not None
            }
    raise ValueError(f"unsupported or invalid archive: {path}")


def check_sources(report: Report) -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    report.check(
        "GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007" in license_text,
        "project includes canonical GPLv3 text",
    )
    report.check("END OF TERMS AND CONDITIONS" in license_text, "GPLv3 text is complete")

    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for phrase in (
        "DEPENDENCY_INVENTORY.json",
        "one-directory bundle",
        "corresponding-source archives",
        "CI release candidate",
        "legal advice",
    ):
        report.check(phrase in notices, f"third-party notice defines {phrase}")

    spec = (ROOT / "SDC-Testing-Toolbox.spec").read_text(encoding="utf-8")
    report.check("generate_payload(" in spec, "PyInstaller spec generates artifact inventory")
    report.check("exclude_binaries=True" in spec, "build keeps shared libraries outside executable")
    report.check("bundle = COLLECT(" in spec, "both platforms use one-directory collection")
    report.check('contents_directory="."' in spec, "legal payload is visible at bundle root")

    for relative in (
        "legal/SOURCE_OFFER.md",
        "legal/PROVENANCE.md",
        "legal/licenses/qt/LGPL-3.0-only.txt",
        "legal/licenses/qt/Qt-GPL-exception-1.0.txt",
    ):
        report.check((ROOT / relative).is_file(), f"source legal payload includes {relative}")


def check_archive(report: Report, archive_path: Path) -> None:
    report.require(archive_path.is_file(), "artifact archive exists", str(archive_path))
    try:
        files = _archive_files(archive_path)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        report.check(False, "artifact archive opens", str(error))
        return
    report.check(bool(files), "artifact archive contains files", str(len(files)))
    inventory_count = sum(
        name.replace("\\", "/").endswith(f"/{INVENTORY_NAME}") for name in files
    )
    report.check(inventory_count == 1, "artifact contains one dependency inventory", str(inventory_count))
    errors = validate_payload(files)
    report.check(not errors, "artifact legal payload and inventory are complete", "; ".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, help="validate an extracted final ZIP or tar archive")
    arguments = parser.parse_args()

    report = Report()
    print("Licensing and packaged legal payload")
    if arguments.artifact:
        check_archive(report, arguments.artifact)
    else:
        check_sources(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
