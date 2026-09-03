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

from legal_payload import INVENTORY_NAME, filter_binaries, validate_payload
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


def check_binary_filter(report: Report) -> None:
    windows_excluded = [
        (r"PySide6\Qt6Pdf.dll", r"C:\build\site-packages\PySide6\Qt6Pdf.dll", "BINARY"),
        (r"PySide6\Qt6PdfWidgets.dll", r"C:\build\site-packages\PySide6\Qt6PdfWidgets.dll", "BINARY"),
        (
            r"PySide6\Qt6VirtualKeyboardQml.dll",
            r"C:\build\site-packages\PySide6\Qt6VirtualKeyboardQml.dll",
            "BINARY",
        ),
        (
            r"PySide6\plugins\imageformats\qpdf.dll",
            r"C:\build\site-packages\PySide6\plugins\imageformats\qpdf.dll",
            "BINARY",
        ),
        (
            r"PySide6\plugins\platforminputcontexts\qtvirtualkeyboardplugin.dll",
            r"C:\build\site-packages\PySide6\plugins\platforminputcontexts\qtvirtualkeyboardplugin.dll",
            "BINARY",
        ),
        (r"PySide6\QtPdf.pyd", r"C:\build\site-packages\PySide6\QtPdf.pyd", "EXTENSION"),
    ]
    linux_excluded = [
        (
            "PySide6/Qt/lib/libQt6Pdf.so.6",
            "/build/site-packages/PySide6/Qt/lib/libQt6Pdf.so.6",
            "BINARY",
        ),
        (
            "libQt6VirtualKeyboardQml.so.6",
            "/build/site-packages/PySide6/Qt/lib/libQt6VirtualKeyboardQml.so.6",
            "BINARY",
        ),
        (
            "PySide6/Qt/plugins/imageformats/libqpdf.so",
            "/build/site-packages/PySide6/Qt/plugins/imageformats/libqpdf.so",
            "BINARY",
        ),
        (
            "PySide6/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so",
            "/build/site-packages/PySide6/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so",
            "BINARY",
        ),
        (
            "PySide6/QtPdf.abi3.so",
            "/build/site-packages/PySide6/QtPdf.abi3.so",
            "EXTENSION",
        ),
    ]
    installed = Path(sys.prefix)
    allowed = [
        ("PySide6/Qt6Core.dll", str(installed / "PySide6" / "Qt6Core.dll"), "BINARY"),
        ("PySide6/Qt6Gui.dll", str(installed / "PySide6" / "Qt6Gui.dll"), "BINARY"),
        (
            "PySide6/Qt6Widgets.dll",
            str(installed / "PySide6" / "Qt6Widgets.dll"),
            "BINARY",
        ),
        (
            "PySide6/Qt/lib/libQt6Network.so.6",
            str(installed / "PySide6" / "Qt" / "lib" / "libQt6Network.so.6"),
            "BINARY",
        ),
        ("vendor/qpdf.dll", str(installed / "vendor" / "qpdf.dll"), "BINARY"),
        ("vendor/libqpdf.so", str(installed / "vendor" / "libqpdf.so"), "BINARY"),
        (
            "PySide6/Qt/plugins/imageformats/libqpdf.so.debug",
            str(installed / "PySide6" / "Qt" / "plugins" / "imageformats" / "libqpdf.so.debug"),
            "BINARY",
        ),
    ]

    for platform_name, excluded in (("Windows", windows_excluded), ("Linux", linux_excluded)):
        filtered = filter_binaries(excluded + allowed, platform=platform_name)
        report.check(
            filtered == allowed,
            f"{platform_name} TOCs exclude only unused Qt PDF and Virtual Keyboard binaries",
        )

    machine_local = ("local.dll", str(ROOT / "vendor" / "local.dll"), "BINARY")
    report.check(
        filter_binaries([machine_local], platform="Windows") == [],
        "Windows filtering rejects a machine-local PATH binary",
    )
    report.check(
        filter_binaries([machine_local], platform="Linux") == [machine_local],
        "non-Windows filtering retains a non-Qt binary",
    )


def check_sources(report: Report) -> None:
    check_binary_filter(report)
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
        "legal/licenses/common/Zlib.txt",
        "legal/licenses/iconv/LGPL-2.1-only.txt",
        "legal/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.txt",
        "legal/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.docx",
        "legal/licenses/qt-third-party/libjpeg-turbo/LICENSE.txt",
        "legal/licenses/qt-third-party/libjpeg-turbo/ijg-license.txt",
        "legal/licenses/qt-third-party/libjpeg-turbo/COPYRIGHT.txt",
        "legal/licenses/qt-third-party/libtiff/COPYRIGHT",
        "legal/licenses/qt-third-party/libwebp/COPYING",
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

    native_components = inventory.get("native_components", [])
    report.check(bool(native_components), "artifact inventory identifies native components")
    for native in native_components:
        report.check(
            bool(native.get("evidence", {}).get("owner")),
            f"{native.get('name')} records native owner/source evidence",
        )
        wrong_notice = json.loads(json.dumps(inventory))
        mutated = next(
            component
            for component in wrong_notice["native_components"]
            if component.get("name") == native.get("name")
            and component.get("version") == native.get("version")
        )
        mutated["notice_paths"] = ["legal/licenses/python/LICENSE.txt"]
        mutated["notice_sha256"] = {
            "legal/licenses/python/LICENSE.txt": __import__("hashlib").sha256(
                files[next(name for name in files if name.replace("\\", "/").endswith("/legal/licenses/python/LICENSE.txt"))]
            ).hexdigest()
        }
        changed = dict(files)
        changed[inventory_path] = json.dumps(wrong_notice).encode("utf-8")
        report.check(
            any(
                f"{native.get('name')}: missing or misassigned component-specific legal notice"
                in error
                for error in validate_payload(changed)
            ),
            f"artifact validation rejects an unrelated {native.get('name')} notice",
        )

    removable_notice = next(
        (
            notice
            for component in native_components
            for notice in component.get("notice_paths", [])
            if notice != "legal/licenses/lxml/LICENSES.txt"
        ),
        None,
    )
    if removable_notice:
        missing = {
            name: value
            for name, value in files.items()
            if not name.replace("\\", "/").endswith(f"/{removable_notice}")
        }
        report.check(
            any("missing bundled notice" in error for error in validate_payload(missing)),
            "artifact validation rejects a missing native notice",
            removable_notice,
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
