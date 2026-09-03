"""Check licensing sources or the legal payload in a final build archive."""

from __future__ import annotations

import argparse
import json
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
        "legal/licenses/openssl/Apache-2.0.txt",
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

    inventory_path = next(
        (name for name in files if name.replace("\\", "/").endswith(f"/{INVENTORY_NAME}")),
        None,
    )
    if inventory_path is None:
        return
    inventory = json.loads(files[inventory_path].decode("utf-8"))

    runtime_names = {
        entry.get("name")
        for entry in inventory.get("resolved_runtime", [])
        if isinstance(entry, dict)
    }
    removable = next(
        (
            component.get("name")
            for component in inventory.get("components", [])
            if isinstance(component, dict) and component.get("name") in runtime_names
        ),
        None,
    )
    if removable:
        omitted = dict(inventory)
        omitted["components"] = [
            component
            for component in inventory["components"]
            if component.get("name") != removable
        ]
        changed = dict(files)
        changed[inventory_path] = json.dumps(omitted).encode("utf-8")
        report.check(
            any("unclassified" in error for error in validate_payload(changed)),
            "artifact validation rejects an omitted resolved runtime component",
            removable,
        )

    openssl = next(
        (
            component
            for component in inventory.get("components", [])
            if component.get("name") == "OpenSSL"
        ),
        None,
    )
    if openssl:
        wrong_notice = json.loads(json.dumps(inventory))
        next(
            component for component in wrong_notice["components"] if component.get("name") == "OpenSSL"
        )["notice_paths"] = ["legal/licenses/python/LICENSE.txt"]
        changed = dict(files)
        changed[inventory_path] = json.dumps(wrong_notice).encode("utf-8")
        report.check(
            any("OpenSSL: missing component-specific legal notice" in error for error in validate_payload(changed)),
            "artifact validation rejects an unrelated OpenSSL notice",
        )


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
