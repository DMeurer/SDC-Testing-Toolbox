"""Check licensing sources or the legal payload in a final build archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from legal_payload import (  # noqa: I001 - ROOT must precede local imports
    ARTIFACT_ROOT,
    INVENTORY_NAME,
    filter_binaries,
    validate_payload,
)
from script_support import Report


PINNED_LEGAL_SHA256 = {
    "legal/licenses/qt/LGPL-3.0-only.txt": "da7eabb7bafdf7d3ae5e9f223aa5bdc1eece45ac569dc21b3b037520b4464768",
    "legal/licenses/qt/GPL-3.0-only.txt": "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903",
    "legal/licenses/qt/Qt-GPL-exception-1.0.txt": "40678d338ce53cd93f8b22b281a2ecbcaa3ee65ce60b25ffb0c462b0530846b2",
    "legal/licenses/openssl/Apache-2.0.txt": "7d5450cb2d142651b8afa315b5f238efc805dad827d91ba367d8516bc9d49e7a",
}


def _archive_files(path: Path) -> list[tuple[str, bytes]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return [
                (info.filename, archive.read(info))
                for info in archive.infolist()
                if not info.is_dir()
            ]
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as archive:
            return [
                (member.name, extracted.read())
                for member in archive.getmembers()
                if member.isfile()
                and (extracted := archive.extractfile(member)) is not None
            ]
    raise ValueError(f"unsupported or invalid archive: {path}")


def _check_pinned_legal_hashes(
    report: Report, files: dict[str, bytes], *, source: str
) -> None:
    for relative, expected in PINNED_LEGAL_SHA256.items():
        matches = [
            value
            for name, value in files.items()
            if name.replace("\\", "/") == relative
            or name.replace("\\", "/").endswith(f"/{relative}")
        ]
        report.check(
            len(matches) == 1,
            f"{source} contains one pinned {relative}",
            str(len(matches)),
        )
        if len(matches) == 1:
            actual = hashlib.sha256(matches[0]).hexdigest()
            report.check(
                actual == expected,
                f"{source} {relative} matches pinned upstream bytes",
                f"expected {expected}, got {actual}",
            )


def _pinned_legal_hashes_match(files: dict[str, bytes]) -> bool:
    return all(
        hashlib.sha256(files[relative]).hexdigest() == expected
        for relative, expected in PINNED_LEGAL_SHA256.items()
    )


def check_binary_filter(report: Report) -> None:
    windows_excluded = [
        (r"PySide6\Qt6Pdf.dll", r"C:\build\site-packages\PySide6\Qt6Pdf.dll", "BINARY"),
        (
            r"PySide6\Qt6PdfWidgets.dll",
            r"C:\build\site-packages\PySide6\Qt6PdfWidgets.dll",
            "BINARY",
        ),
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
        (
            r"PySide6\QtPdf.pyd",
            r"C:\build\site-packages\PySide6\QtPdf.pyd",
            "EXTENSION",
        ),
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
            str(
                installed
                / "PySide6"
                / "Qt"
                / "plugins"
                / "imageformats"
                / "libqpdf.so.debug"
            ),
            "BINARY",
        ),
    ]

    for platform_name, excluded in (
        ("Windows", windows_excluded),
        ("Linux", linux_excluded),
    ):
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
        "GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007"
        in license_text,
        "project includes canonical GPLv3 text",
    )
    report.check(
        "END OF TERMS AND CONDITIONS" in license_text, "GPLv3 text is complete"
    )
    source_files = {
        relative: (
            ROOT / "LICENSE"
            if relative == "legal/licenses/qt/GPL-3.0-only.txt"
            else ROOT / relative
        ).read_bytes()
        for relative in PINNED_LEGAL_SHA256
    }
    _check_pinned_legal_hashes(report, source_files, source="source tree")
    mutated = dict(source_files)
    target = "legal/licenses/qt/LGPL-3.0-only.txt"
    mutated[target] = bytes([mutated[target][0] ^ 1]) + mutated[target][1:]
    report.check(
        not _pinned_legal_hashes_match(mutated),
        "pinned legal hash check rejects a one-byte mutation",
    )

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
    report.check(
        "generate_payload(" in spec, "PyInstaller spec generates artifact inventory"
    )
    report.check(
        "exclude_binaries=True" in spec,
        "build keeps shared libraries outside executable",
    )
    report.check(
        "bundle = COLLECT(" in spec, "both platforms use one-directory collection"
    )
    report.check(
        'contents_directory="."' in spec, "legal payload is visible at bundle root"
    )

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
        report.check(
            (ROOT / relative).is_file(), f"source legal payload includes {relative}"
        )


def check_archive(report: Report, archive_path: Path) -> None:
    report.require(archive_path.is_file(), "artifact archive exists", str(archive_path))
    try:
        files = _archive_files(archive_path)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        report.check(False, "artifact archive opens", str(error))
        return
    report.check(bool(files), "artifact archive contains files", str(len(files)))
    inventory_count = sum(
        name.replace("\\", "/").endswith(f"/{INVENTORY_NAME}") for name, _value in files
    )
    report.check(
        inventory_count == 1,
        "artifact contains one dependency inventory",
        str(inventory_count),
    )
    errors = validate_payload(files)
    report.check(
        not errors,
        "artifact legal payload and inventory are complete",
        "; ".join(errors),
    )
    file_map = dict(files)
    _check_pinned_legal_hashes(report, file_map, source="artifact")

    inventory_path = next(
        (
            name
            for name in file_map
            if name.replace("\\", "/").endswith(f"/{INVENTORY_NAME}")
        ),
        None,
    )
    if inventory_path is None:
        return
    inventory = json.loads(file_map[inventory_path].decode("utf-8"))

    malformed_root = dict(file_map)
    malformed_root[inventory_path] = b"[]"
    try:
        malformed_root_errors = validate_payload(malformed_root)
    except Exception as error:  # noqa: BLE001 - this check must detect every exception
        report.check(
            False,
            "artifact validation collects a malformed inventory-root error without raising",
            f"raised {type(error).__name__}: {error}",
        )
    else:
        report.check(
            any(
                "inventory root is not an object" in error
                for error in malformed_root_errors
            ),
            "artifact validation collects a malformed inventory-root error without raising",
            "; ".join(malformed_root_errors),
        )

    def inventory_mutation(
        description: str,
        mutate: Callable[[dict[str, object]], object],
        expected: str,
    ) -> None:
        changed_inventory = json.loads(json.dumps(inventory))
        mutate(changed_inventory)
        changed = dict(file_map)
        changed[inventory_path] = json.dumps(changed_inventory).encode("utf-8")
        try:
            mutation_errors = validate_payload(changed)
        except Exception as error:  # noqa: BLE001 - this check must detect every exception
            report.check(False, description, f"raised {type(error).__name__}: {error}")
            return
        report.check(
            bool(mutation_errors)
            and any(expected in error for error in mutation_errors),
            description,
            "; ".join(mutation_errors),
        )

    inventory_mutation(
        "artifact validation rejects a missing required_documents field without raising",
        lambda value: value.pop("required_documents"),
        "required_documents",
    )
    inventory_mutation(
        "artifact validation rejects an empty required_documents list without raising",
        lambda value: value.__setitem__("required_documents", []),
        "required_documents",
    )
    inventory_mutation(
        "artifact validation rejects an extra required document without raising",
        lambda value: value["required_documents"].append("legal/UNTRUSTED.txt"),
        "required_documents",
    )
    missing_required_document = [
        (name, value) for name, value in files if name != f"{ARTIFACT_ROOT}/LICENSE"
    ]
    report.check(
        any(
            "missing required legal payload: LICENSE" in error
            for error in validate_payload(missing_required_document)
        ),
        "artifact validation rejects a code-required document missing from the archive",
    )

    malformed_mutations = (
        (
            "root field",
            lambda value: value.__setitem__("platform", []),
            "platform is not an object",
        ),
        (
            "components",
            lambda value: value.__setitem__("components", {}),
            "components is not a list",
        ),
        (
            "component entry",
            lambda value: value["components"].__setitem__(0, []),
            "components[0] is not an object",
        ),
        (
            "component evidence",
            lambda value: value["components"][0].__setitem__("evidence", []),
            "evidence is not an object",
        ),
        (
            "component notice paths",
            lambda value: value["components"][0].__setitem__("notice_paths", {}),
            "notice_paths is not a list",
        ),
        (
            "native components",
            lambda value: value.__setitem__("native_components", {}),
            "native_components is not a list",
        ),
        (
            "native entry",
            lambda value: value["native_components"].__setitem__(0, []),
            "native_components[0] is not an object",
        ),
        (
            "native evidence",
            lambda value: value["native_components"][0].__setitem__("evidence", []),
            "evidence is not an object",
        ),
        (
            "native notice hashes",
            lambda value: value["native_components"][0].__setitem__(
                "notice_sha256", []
            ),
            "notice_sha256 is not an object",
        ),
        (
            "resolved runtime",
            lambda value: value.__setitem__("resolved_runtime", {}),
            "resolved_runtime is not a list",
        ),
        (
            "build-only classification",
            lambda value: value.__setitem__("build_only", {}),
            "build_only is not a list",
        ),
        (
            "unpackaged runtime classification",
            lambda value: value.__setitem__("resolved_runtime_not_packaged", {}),
            "resolved_runtime_not_packaged is not a list",
        ),
        (
            "generation",
            lambda value: value.__setitem__("generation", []),
            "generation is not an object",
        ),
    )
    for level, mutate, expected in malformed_mutations:
        inventory_mutation(
            f"artifact validation collects malformed {level} errors without raising",
            mutate,
            expected,
        )

    fabricated = json.loads(json.dumps(inventory["components"][0]))
    fabricated["name"] = "Fabricated module component"
    fabricated["evidence"] = {"modules": ["aiohappyeyeballs"], "artifact_paths": []}
    inventory_mutation(
        "artifact validation rejects fabricated module-only component evidence",
        lambda value: value["components"].append(fabricated),
        "module evidence has no code-owned component mapping",
    )

    executable = (
        f"{ARTIFACT_ROOT}/SDC-Testing-Toolbox.exe"
        if inventory.get("platform", {}).get("system") == "Windows"
        else f"{ARTIFACT_ROOT}/SDC-Testing-Toolbox"
    )
    missing_executable = [(name, value) for name, value in files if name != executable]
    report.check(
        any(
            "missing expected platform executable file" in error
            for error in validate_payload(missing_executable)
        ),
        "artifact validation rejects a missing platform executable",
    )
    unexpected_root = files + [("unexpected-root/file.txt", b"unexpected")]
    report.check(
        any(
            "outside the single" in error for error in validate_payload(unexpected_root)
        ),
        "artifact validation rejects a file at an unexpected archive root",
    )
    canonical_name, canonical_value = next(
        (name, value) for name, value in files if name.endswith("/LICENSE")
    )
    duplicate_variants = (
        canonical_name.swapcase(),
        canonical_name.replace("/", "\\"),
        f"{ARTIFACT_ROOT}/legal/caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt",
    )
    for description, duplicate_name in zip(
        ("case", "slash", "Unicode"), duplicate_variants
    ):
        entries = list(files)
        if description == "Unicode":
            entries.extend(
                [
                    (duplicate_name, b"first"),
                    (
                        f"{ARTIFACT_ROOT}/legal/cafe\N{COMBINING ACUTE ACCENT}.txt",
                        b"second",
                    ),
                ]
            )
        else:
            entries.append((duplicate_name, canonical_value))
        report.check(
            any(
                "duplicate normalized path" in error
                for error in validate_payload(entries)
            ),
            f"artifact validation rejects {description}-normalized duplicate paths",
        )
    traversal = files + [(f"{ARTIFACT_ROOT}/../escaped.txt", b"escape")]
    report.check(
        any(
            "unsafe or malformed path" in error for error in validate_payload(traversal)
        ),
        "artifact validation rejects path traversal",
    )

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
        changed = dict(file_map)
        changed[inventory_path] = json.dumps(omitted).encode("utf-8")
        report.check(
            any("unclassified" in error for error in validate_payload(changed)),
            "artifact validation rejects an omitted resolved runtime component",
            removable,
        )

    native_components = inventory.get("native_components", [])
    report.check(
        bool(native_components), "artifact inventory identifies native components"
    )
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
            "legal/licenses/python/LICENSE.txt": __import__("hashlib")
            .sha256(
                file_map[
                    next(
                        name
                        for name in file_map
                        if name.replace("\\", "/").endswith(
                            "/legal/licenses/python/LICENSE.txt"
                        )
                    )
                ]
            )
            .hexdigest()
        }
        changed = dict(file_map)
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
            for name, value in file_map.items()
            if not name.replace("\\", "/").endswith(f"/{removable_notice}")
        }
        report.check(
            any(
                "missing bundled notice" in error for error in validate_payload(missing)
            ),
            "artifact validation rejects a missing native notice",
            removable_notice,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--artifact", type=Path, help="validate an extracted final ZIP or tar archive"
    )
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
