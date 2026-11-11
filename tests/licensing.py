"""Check licensing sources or the legal payload in a final build archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import marshal
import re
import struct
import sys
import tarfile
import zipfile
import zlib
from collections.abc import Callable
from email.message import Message
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from legal_payload import (  # noqa: I001 - ROOT must precede local imports
    ARTIFACT_ROOT,
    INVENTORY_NAME,
    REQUIRED_DOCUMENTS,
    filter_binaries,
    validate_payload,
)
import legal_payload
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


def check_conditional_typing_extensions(
    report: Report, files: dict[str, bytes], inventory_path: str
) -> None:
    inventory = json.loads(files[inventory_path])
    windows = inventory["platform"]["system"] == "Windows"
    executable = f"{ARTIFACT_ROOT}/SDC-Testing-Toolbox" + (".exe" if windows else "")
    modules = {
        module
        for component in inventory["components"]
        if component["name"] != "typing_extensions"
        for module in component["evidence"]["modules"]
    }
    notice = "legal/licenses/typing-extensions/LICENSE"

    def fixture(minor: int, *, shipped: bool = False) -> dict[str, bytes]:
        changed_inventory = json.loads(json.dumps(inventory))
        for field in (
            "components", "resolved_runtime", "resolved_runtime_not_packaged", "build_only"
        ):
            changed_inventory[field] = [
                item
                for item in changed_inventory[field]
                if item["name"] not in {"typing_extensions", "typing-extensions"}
            ]
        python = next(
            c for c in changed_inventory["components"] if c["name"] == "Python"
        )
        library = f"python3{minor}.dll" if windows else f"libpython3.{minor}.so.1.0"
        old_libraries = [
            path
            for path in python["evidence"]["artifact_paths"]
            if re.fullmatch(
                r"(?:python3\d+t?\.dll|libpython3\.\d+t?\.so(?:\.\d+)*)",
                Path(path).name,
            )
        ]
        python["version"] = f"3.{minor}.0"
        python["evidence"]["artifact_paths"] = [
            path
            for path in python["evidence"]["artifact_paths"]
            if path not in old_libraries
        ] + [library]
        old_basenames = {Path(path).name for path in old_libraries}
        changed = {
            name: value
            for name, value in files.items()
            if Path(name).name not in old_basenames
        }
        changed[f"{ARTIFACT_ROOT}/{library}"] = b"Python runtime fixture"
        included_modules = modules | ({"typing_extensions"} if shipped else set())
        # Only the CArchive/PYZ tables are needed; no fixture code is executed.
        pyz = (
            b"PYZ\0" + b"\0" * 4 + struct.pack("!i", 12)
            + marshal.dumps([(name, (0, 0, 0)) for name in sorted(included_modules)])
        )
        toc = struct.pack("!IIIIBc", 22, 0, len(pyz), len(pyz), 0, b"z") + b"PYZ\0"
        changed[executable] = pyz + toc + struct.pack(
            "!8sIIII64s",
            b"MEI\014\013\012\013\016",
            len(pyz) + len(toc) + 88,
            len(pyz),
            len(toc),
            300 + minor,
            library.encode(),
        )
        if shipped:
            changed_inventory["components"].append(
                {
                    "name": "typing_extensions",
                    "version": "4.16.0",
                    "scope": "runtime",
                    "license_expression": "PSF-2.0",
                    "project_url": "https://github.com/python/typing_extensions",
                    "source_archive_urls": [],
                    "notice_paths": [notice],
                    "evidence": {"modules": ["typing_extensions"], "artifact_paths": []},
                }
            )
            changed[f"{ARTIFACT_ROOT}/{notice}"] = b"Python Software Foundation License"
        if minor < 13:
            changed_inventory["resolved_runtime"].append(
                {"name": "typing_extensions", "version": "4.16.0"}
            )
        changed[inventory_path] = json.dumps(changed_inventory).encode()
        return changed

    def check(
        description: str, changed: dict[str, bytes], expected: str | None = None
    ) -> None:
        errors = validate_payload(changed)
        report.check(
            not errors
            if expected is None
            else any(expected in error for error in errors),
            description,
            "; ".join(errors),
        )

    for minor, shipped in ((12, True), (13, False), (14, False), (13, True)):
        check(
            f"Python 3.{minor} conditional typing_extensions closure (shipped={shipped})",
            fixture(minor, shipped=shipped),
        )
    missing = fixture(12)
    check(
        "Python 3.12 rejects missing required typing_extensions", missing,
        "missing expected runtime component typing_extensions",
    )
    missing_inventory = json.loads(missing[inventory_path])
    missing_inventory["resolved_runtime"] = [
        c for c in missing_inventory["resolved_runtime"]
        if c["name"] != "typing_extensions"
    ]
    python = next(c for c in missing_inventory["components"] if c["name"] == "Python")
    python["version"] = "3.13.0"
    missing[inventory_path] = json.dumps(missing_inventory).encode()
    check(
        "Python inventory claim cannot hide the 3.12 typing_extensions requirement",
        missing, "missing expected runtime component typing_extensions",
    )
    check(
        "Python inventory version must agree with CArchive runtime", missing,
        "Python.version does not match executable runtime",
    )

    shipped = fixture(13, shipped=True)
    omitted = json.loads(shipped[inventory_path])
    omitted["components"] = [
        c for c in omitted["components"] if c["name"] != "typing_extensions"
    ]
    shipped[inventory_path] = json.dumps(omitted).encode()
    check(
        "Python 3.13 rejects an unclassified embedded typing_extensions", shipped,
        "missing expected runtime component typing_extensions",
    )
    loose = fixture(13)
    loose[f"{ARTIFACT_ROOT}/typing_extensions.py"] = b"# standalone module"
    check(
        "Python 3.13 rejects an unclassified loose typing_extensions", loose,
        "missing expected runtime component typing_extensions",
    )
    missing_notice = fixture(13, shipped=True)
    del missing_notice[f"{ARTIFACT_ROOT}/{notice}"]
    check(
        "Python 3.13 shipped typing_extensions still requires its notice",
        missing_notice, "missing bundled notice",
    )
    wrong_notice = fixture(13, shipped=True)
    wrong_inventory = json.loads(wrong_notice[inventory_path])
    typing = next(
        c for c in wrong_inventory["components"] if c["name"] == "typing_extensions"
    )
    typing["notice_paths"] = ["legal/licenses/python/LICENSE.txt"]
    wrong_notice[inventory_path] = json.dumps(wrong_inventory).encode()
    check(
        "Python 3.13 rejects a misassigned typing_extensions notice", wrong_notice,
        "missing or misassigned component-specific legal notice",
    )

    missing_library = fixture(13)
    library_path = f"{ARTIFACT_ROOT}/" + (
        "python313.dll" if windows else "libpython3.13.so.1.0"
    )
    del missing_library[library_path]
    check(
        "Conditional closure requires the CArchive Python library in the artifact",
        missing_library, "executable Python library is absent",
    )
    mismatched_library = fixture(13)
    binary = mismatched_library[executable]
    mismatched_library[executable] = binary[:-64] + (
        b"python312.dll" if windows else b"libpython3.12.so.1.0"
    ).ljust(64, b"\0")
    check(
        "Conditional closure rejects a CArchive version/library mismatch",
        mismatched_library, "executable Python version and library disagree",
    )
    unreadable = fixture(13)
    unreadable[executable] = b"not a PyInstaller executable"
    check(
        "Unreadable executable cannot waive the typing_extensions requirement",
        unreadable, "missing expected runtime component typing_extensions",
    )
    invalid_cookie = fixture(13)
    binary = invalid_cookie[executable]
    invalid_cookie[executable] = (
        binary[:-80] + struct.pack("!I", len(binary) + 1) + binary[-76:]
    )
    check(
        "Out-of-bounds CArchive cannot waive the typing_extensions requirement",
        invalid_cookie, "missing expected runtime component typing_extensions",
    )
    bounds_fixture = fixture(13)
    binary = bounds_fixture[executable]
    pyz_length = struct.unpack("!I", binary[-76:-72])[0]
    pyz = binary[:pyz_length]
    toc = binary[pyz_length:-88]

    def repack(pyz_data: bytes, toc_data: bytes) -> bytes:
        magic, _size, _offset, _length, version, library = struct.unpack(
            "!8sIIII64s", binary[-88:]
        )
        return bytes(pyz_data) + bytes(toc_data) + struct.pack(
            "!8sIIII64s", magic, len(pyz_data) + len(toc_data) + 88,
            len(pyz_data), len(toc_data), version, library,
        )

    for description, field_offset, value in (
        ("overlong stored entry", 8, len(binary) + 1),
        ("entry overlapping TOC", 8, pyz_length + 1),
        ("entry offset beyond DATA", 4, pyz_length + 1),
        ("maximum unsigned entry offset", 4, 0xFFFFFFFF),
        ("inconsistent uncompressed entry length", 12, pyz_length + 1),
    ):
        changed = dict(bounds_fixture)
        changed_toc = bytearray(toc)
        struct.pack_into("!I", changed_toc, field_offset, value)
        changed[executable] = repack(pyz, changed_toc)
        check(
            f"Malformed {description} cannot waive typing_extensions", changed,
            "missing expected runtime component typing_extensions",
        )
    for trailing_length in (1, 17):
        changed = dict(bounds_fixture)
        changed[executable] = repack(pyz, toc + b"\0" * trailing_length)
        check(
            f"Incomplete trailing TOC ({trailing_length} bytes) cannot waive typing_extensions",
            changed, "missing expected runtime component typing_extensions",
        )
    for offset, length in ((pyz_length - 1, 2), (pyz_length + 1, 0), (0xFFFFFFFF, 0)):
        changed = dict(bounds_fixture)
        extra_entry = struct.pack(
            "!IIIIBc", 22, offset, length, length, 0, b"x"
        ) + b"bad\0"
        changed[executable] = repack(pyz, toc + extra_entry)
        check(
            f"Out-of-range non-PYZ entry ({offset}, {length}) cannot waive typing_extensions",
            changed, "missing expected runtime component typing_extensions",
        )
    for offset in (12 - pyz_length, -1, 0, 11, pyz_length, pyz_length + 1):
        changed = dict(bounds_fixture)
        changed_pyz = bytearray(pyz)
        struct.pack_into("!i", changed_pyz, 8, offset)
        changed[executable] = repack(changed_pyz, toc)
        check(
            f"Invalid PYZ TOC offset {offset} cannot waive typing_extensions", changed,
            "missing expected runtime component typing_extensions",
        )
    compressed_pyz = zlib.compress(pyz)
    compressed_toc = bytearray(toc)
    struct.pack_into("!I", compressed_toc, 8, len(compressed_pyz))
    compressed_toc[16] = 1
    compressed_fixture = dict(bounds_fixture)
    compressed_fixture[executable] = repack(compressed_pyz, compressed_toc)
    check("Valid compressed PYZ preserves conditional closure", compressed_fixture)
    struct.pack_into("!I", compressed_toc, 12, pyz_length + 1)
    compressed_fixture[executable] = repack(compressed_pyz, compressed_toc)
    check(
        "Inconsistent decompressed PYZ length cannot waive typing_extensions",
        compressed_fixture, "missing expected runtime component typing_extensions",
    )
    omitted_dependency = fixture(13)
    omitted_inventory = json.loads(omitted_dependency[inventory_path])
    for field in ("components", "resolved_runtime"):
        omitted_inventory[field] = [
            c for c in omitted_inventory[field] if c["name"] != "aiohttp"
        ]
    omitted_dependency[inventory_path] = json.dumps(omitted_inventory).encode()
    check(
        "Python 3.13 still rejects other code-owned dependency omissions",
        omitted_dependency, "resolved_runtime does not match the code-owned",
    )
    invented_dependency = fixture(13)
    invented_inventory = json.loads(invented_dependency[inventory_path])
    invented_inventory["resolved_runtime"].append(
        {"name": "typing_extensions", "version": "4.16.0"}
    )
    invented_dependency[inventory_path] = json.dumps(invented_inventory).encode()
    check(
        "Inventory cannot invent a Python 3.13 typing_extensions requirement",
        invented_dependency, "resolved_runtime does not match the code-owned",
    )


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


def legal_fixture() -> tuple[dict[str, bytes], dict[str, object]]:
    """A complete, offline artifact; the executable table contains no runnable code."""
    notices = {
        "aiohappyeyeballs": ["aiohappyeyeballs/LICENSE"],
        "aiohttp": ["aiohttp/LICENSE.txt", "aiohttp/vendor/llhttp/LICENSE"],
        "aiosignal": ["aiosignal/LICENSE"],
        "attrs": ["attrs/LICENSE"],
        "cffi": ["cffi/LICENSE"],
        "cryptography": ["cryptography/LICENSE", "cryptography/LICENSE.APACHE", "cryptography/LICENSE.BSD"],
        "frozenlist": ["frozenlist/LICENSE"],
        "idna": ["idna/LICENSE.md"],
        "ifaddr": ["ifaddr/LICENSE.txt"],
        "lxml": ["lxml/LICENSE.txt", "lxml/LICENSES.txt"],
        "multidict": ["multidict/LICENSE"],
        "propcache": ["propcache/LICENSE", "propcache/NOTICE"],
        "pyinstaller": ["pyinstaller/COPYING.txt"],
        "PySide6": ["pyside6/LicenseRef-Qt-Commercial.txt"],
        "PySide6_Essentials": ["pyside6-essentials/LicenseRef-Qt-Commercial.txt"],
        "Python": ["python/LICENSE.txt"],
        "Qt": [],
        "sdc11073": ["sdc11073/LICENSE"],
        "shiboken6": ["shiboken6/LicenseRef-Qt-Commercial.txt"],
        "yarl": ["yarl/LICENSE", "yarl/NOTICE"],
    }
    qt_notices = [
        "legal/licenses/qt/LGPL-3.0-only.txt",
        "legal/licenses/qt/GPL-3.0-only.txt",
        "legal/licenses/qt/Qt-GPL-exception-1.0.txt",
    ]
    files = {
        f"{ARTIFACT_ROOT}/{relative}": (
            ROOT / ("LICENSE" if relative == qt_notices[1] else relative)
        ).read_bytes()
        for relative in REQUIRED_DOCUMENTS
        if relative != INVENTORY_NAME
    }
    components = []
    modules = []
    for name, suffixes in notices.items():
        paths = [f"legal/licenses/{suffix}" for suffix in suffixes]
        for path in paths:
            files[f"{ARTIFACT_ROOT}/{path}"] = (
                b"notice fixture: valid commercial Qt licenses; Bootloader Exception; "
                b"PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2; MIT License Draeger"
            )
        if name in {"PySide6", "PySide6_Essentials", "shiboken6", "Qt"}:
            paths.extend(qt_notices)
        if name == "Qt":
            paths.append("legal/SOURCE_OFFER.md")
        module = {
            "attrs": "attr", "Python": "__future__", "PySide6_Essentials": "PySide6",
        }.get(name, name)
        modules.append(module)
        components.append({
            "name": name,
            "version": "3.13.0" if name == "Python" else "6.11.2",
            "scope": "runtime",
            "license_expression": "MIT",
            "project_url": "https://example.test/project",
            "source_archive_urls": [
                "https://download.qt.io/official_releases/qt/6.11/6.11.2/submodules/qtbase-everywhere-src-6.11.2.tar.xz"
            ] if name == "Qt" else [],
            "notice_paths": paths,
            "evidence": {
                "modules": [module] if name not in {"Qt", "pyinstaller"} else [],
                "artifact_paths": ["PySide6/Qt6Core.dll"] if name == "Qt" else (
                    ["SDC-Testing-Toolbox executable bootloader and embedded loader"]
                    if name == "pyinstaller" else []
                ),
            },
        })
    pyz = b"PYZ\0" + b"\0" * 4 + struct.pack("!i", 12) + marshal.dumps(
        [(name, (0, 0, 0)) for name in modules]
    )
    toc = struct.pack("!IIIIBc", 22, 0, len(pyz), len(pyz), 0, b"z") + b"PYZ\0"
    files[f"{ARTIFACT_ROOT}/SDC-Testing-Toolbox"] = pyz + toc + struct.pack(
        "!8sIIII64s", b"MEI\014\013\012\013\016", len(pyz) + len(toc) + 88,
        len(pyz), len(toc), 313, b"libpython3.13.so.1.0",
    )
    files[f"{ARTIFACT_ROOT}/libpython3.13.so.1.0"] = b"Python runtime fixture"
    files[f"{ARTIFACT_ROOT}/PySide6/Qt6Core.dll"] = b"Qt shared library fixture"
    unpackaged = ["pycparser", "PySide6_Addons"]
    inventory = {
        "schema_version": 2,
        "artifact_layout": "PyInstaller one-directory bundle with replaceable shared libraries",
        "platform": {"system": "Linux", "machine": "x86_64"},
        "components": components,
        "native_components": [],
        "build_only": [{"name": "pyinstaller-hooks-contrib", "version": "1", "reason": "not bundled"}],
        "resolved_runtime_not_packaged": [
            {"name": name, "version": "1", "reason": "not bundled"} for name in unpackaged
        ],
        "resolved_runtime": [
            {"name": name, "version": "6.11.2"}
            for name in [*notices, *unpackaged] if name not in {"Python", "Qt", "pyinstaller"}
        ],
        "required_documents": list(REQUIRED_DOCUMENTS),
        "generation": {
            "method": "offline fixture", "runtime_roots": ["PySide6", "cryptography", "sdc11073"],
            "build_roots": ["PyInstaller", "pyinstaller-hooks-contrib"],
        },
    }
    files[f"{ARTIFACT_ROOT}/{INVENTORY_NAME}"] = json.dumps(inventory).encode()
    return files, inventory


def check_debian_common_notices(report: Report) -> None:
    with TemporaryDirectory(prefix="sdctoolbox-debian-") as temporary:
        root = Path(temporary)
        copyright_path = root / "doc" / "libfixture1" / "copyright"
        copyright_path.parent.mkdir(parents=True)
        copyright_path.write_text(
            "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
            "License: GPL-2\n See /usr/share/common-licenses/GPL-2 for the full text.\n",
            encoding="utf-8",
        )
        free_copyright = root / "doc" / "libfree2" / "copyright"
        free_copyright.parent.mkdir()
        free_copyright.write_text(
            "This package was debianized by a fixture maintainer.\n"
            "Copyright (C) 2024 Fixture Authors\n"
            "License: prose that is not a DEP-5 license field\n"
            "The full text is in /usr/share/common-licenses/GPL-2.\n"
            "Some files cite \"/usr/share/common-licenses/GPL-2.0\".\n",
            encoding="utf-8",
        )
        common = root / "common" / "GPL-2"
        common.parent.mkdir()
        common.write_text("GNU GENERAL PUBLIC LICENSE Version 2 fixture", encoding="utf-8")

        def system_path(value: str) -> Path:
            return {
                "/usr/share/doc": root / "doc",
                "/usr/share/common-licenses": root / "common",
            }.get(value, Path(value))

        # Merged-/usr hosts resolve /lib to /usr/lib while dpkg may record /lib.
        dpkg_owners = {
            "/lib/libfixture.so.1": "libfixture1:amd64",
            "/usr/lib/libfree.so.2": "libfree2:amd64",
        }

        def query(argv: list[str], **_kwargs: object) -> object:
            if argv[1] == "-S":
                owner = dpkg_owners.get(argv[2])
                return SimpleNamespace(
                    returncode=0 if owner else 1,
                    stdout=f"{owner}: {argv[2]}\n" if owner else "",
                )
            name = argv[-1].split(":", 1)[0]
            return SimpleNamespace(
                returncode=0,
                stdout=f"1.0-1build1\thttps://example.test/{name}\t{name}-src\t1:1.0-1\n",
            )

        output = root / "output"
        with patch.object(legal_payload, "Path", side_effect=system_path), patch.object(
            legal_payload.subprocess, "run", side_effect=query,
        ):
            native, free = legal_payload._debian_native_components(
                {
                    PurePosixPath("/usr/lib/libfixture.so.1"): {"libfixture.so.1"},
                    PurePosixPath("/usr/lib/libfree.so.2"): {"libfree.so.2"},
                },
                output,
            )
        expected = [
            "legal/licenses/debian/libfixture1/copyright",
            "legal/licenses/debian/common/GPL-2",
        ]
        report.check(native["notice_paths"] == expected, "Debian generation includes the referenced common GPL-2 text")
        report.check(
            native["evidence"]["owner"]["debian_binary_package"] == "libfixture1",
            "Debian generation assigns a merged-/usr library through its dpkg /lib path",
        )
        report.check(
            native["license_expression"] == "GPL-2"
            and native["source_archive_urls"]
            == ["https://launchpad.net/ubuntu/+source/libfixture1-src/1:1.0-1"],
            "Debian generation records DEP-5 licenses and the versioned source package URL",
            f"{native['license_expression']!r} {native['source_archive_urls']!r}",
        )
        free_expected = [
            "legal/licenses/debian/libfree2/copyright",
            "legal/licenses/debian/common/GPL-2",
        ]
        report.check(
            free["license_expression"] == "LicenseRef-Debian-libfree2-copyright"
            and free["notice_paths"] == free_expected,
            "Debian generation ships free-form copyright files without parsing prose as licenses",
            f"{free['license_expression']!r} {free['notice_paths']!r}",
        )
        files, inventory = legal_fixture()
        inventory["native_components"] = [native]
        files[f"{ARTIFACT_ROOT}/libfixture.so.1"] = b"native fixture"
        for path in expected:
            files[f"{ARTIFACT_ROOT}/{path}"] = (output / path).read_bytes()
        key = f"{ARTIFACT_ROOT}/{INVENTORY_NAME}"
        files[key] = json.dumps(inventory).encode()
        errors = validate_payload(files)
        report.check(not errors, "validator accepts the complete generated Debian notice set", "; ".join(errors))

        for description, mutate, expected_error in (
            ("omitted common notice claim", lambda n, f: n["notice_paths"].pop(), "missing or misassigned"),
            ("missing common notice file", lambda n, f: f.pop(f"{ARTIFACT_ROOT}/{expected[1]}"), "missing bundled notice"),
            ("unrelated common notice", lambda n, f: n["notice_paths"].append("legal/licenses/common/Zlib.txt"), "missing or misassigned"),
            ("unsafe common notice path", lambda n, f: n["notice_paths"].append("legal/../escape"), "unsafe or non-normalized"),
            ("incorrect common hash", lambda n, f: n["notice_sha256"].__setitem__(expected[1], "0" * 64), "SHA-256 mismatch"),
        ):
            changed_inventory = json.loads(json.dumps(inventory))
            changed = dict(files)
            changed_native = changed_inventory["native_components"][0]
            mutate(changed_native, changed)
            if description != "incorrect common hash":
                changed_native["notice_sha256"] = {
                    path: hashlib.sha256(changed[f"{ARTIFACT_ROOT}/{path}"]).hexdigest()
                    for path in changed_native["notice_paths"]
                    if f"{ARTIFACT_ROOT}/{path}" in changed
                }
            changed[key] = json.dumps(changed_inventory).encode()
            errors = validate_payload(changed)
            report.check(any(expected_error in error for error in errors), f"Debian validation rejects {description}", "; ".join(errors))

        no_reference = json.loads(json.dumps(inventory))
        native = no_reference["native_components"][0]
        native["notice_paths"] = expected[:1]
        files[f"{ARTIFACT_ROOT}/{expected[0]}"] = b"Format: Debian copyright\nLicense: MIT\n"
        native["notice_sha256"] = {
            expected[0]: hashlib.sha256(files[f"{ARTIFACT_ROOT}/{expected[0]}"]).hexdigest(),
        }
        files[key] = json.dumps(no_reference).encode()
        errors = validate_payload(files)
        report.check(not errors, "Debian copyright without common-license references needs only its own notice", "; ".join(errors))

        free_files, free_inventory = legal_fixture()
        free_inventory["native_components"] = [free]
        free_files[f"{ARTIFACT_ROOT}/libfree.so.2"] = b"native fixture"
        for path in free_expected:
            free_files[f"{ARTIFACT_ROOT}/{path}"] = (output / path).read_bytes()
        free_files[key] = json.dumps(free_inventory).encode()
        errors = validate_payload(free_files)
        report.check(not errors, "validator accepts a free-form Debian copyright notice", "; ".join(errors))

        unrelated = b"Not a package notice; see /usr/share/common-licenses/GPL-2.\n"
        free_files[f"{ARTIFACT_ROOT}/{free_expected[0]}"] = unrelated
        free["notice_sha256"][free_expected[0]] = hashlib.sha256(unrelated).hexdigest()
        free_files[key] = json.dumps(free_inventory).encode()
        errors = validate_payload(free_files)
        report.check(
            any("unexpected content" in error for error in errors),
            "Debian validation rejects a free-form notice without copyright statements",
            "; ".join(errors),
        )


def check_qt_imageformat_sources(report: Report) -> None:
    archive = "https://download.qt.io/official_releases/qt/6.11/6.11.2/submodules/qtimageformats-everywhere-src-6.11.2.tar.xz"
    for plugin, required in (
        ("PySide6/plugins/imageformats/qtiff.dll", True),
        ("PySide6/Qt/plugins/imageformats/libqwebp.so", True),
        ("PySide6/plugins/imageformats/qjpeg.dll", False),
        ("vendor/qwebp.dll", False),
    ):
        with TemporaryDirectory(prefix="sdctoolbox-qt-sources-") as temporary:
            root = Path(temporary)
            distributions = {}
            for name in ("pyside6", "pyside6-essentials", "sdc11073", "cryptography", "pyinstaller", "pyinstaller-hooks-contrib"):
                metadata = Message()
                metadata["Name"] = name
                metadata["License"] = "MIT"
                metadata["Home-page"] = "https://example.test/project"
                notice = f"{name}/LICENSE"
                (root / name).mkdir()
                (root / notice).write_text("notice fixture", encoding="utf-8")
                distributions[name] = SimpleNamespace(
                    metadata=metadata, version="6.11.2", requires=[], files=[notice],
                    locate_file=lambda relative: root / relative,
                )
            distributions["pyside6-essentials"].files.append("plugin")
            (root / "plugin").write_bytes(b"plugin fixture")
            with patch.object(legal_payload, "_distributions", return_value=distributions), patch.object(
                legal_payload, "_linked_library_names", return_value=set(),
            ):
                legal_payload.generate_payload(ROOT, root / "output", [], [(plugin, str(root / "plugin"), "BINARY")], [])
            generated = json.loads((root / "output" / INVENTORY_NAME).read_text(encoding="utf-8"))
            qt = next(c for c in generated["components"] if c["name"] == "Qt")
            report.check((archive in qt["source_archive_urls"]) == required, f"Qt generation conditionally supplies qtimageformats for {plugin}")

        files, inventory = legal_fixture()
        qt = next(c for c in inventory["components"] if c["name"] == "Qt")
        files[f"{ARTIFACT_ROOT}/{plugin}"] = b"plugin fixture"
        key = f"{ARTIFACT_ROOT}/{INVENTORY_NAME}"
        files[key] = json.dumps(inventory).encode()
        errors = validate_payload(files)
        report.check(
            any("qtimageformats" in error for error in errors) if required else not errors,
            f"Qt validation derives imageformat source requirements from archive files: {plugin}", "; ".join(errors),
        )
        if required:
            qt["source_archive_urls"].append(archive)
            files[key] = json.dumps(inventory).encode()
            errors = validate_payload(files)
            report.check(not errors, "Qt validation accepts the exact versioned imageformats source archive", "; ".join(errors))
            qt["source_archive_urls"][-1] = archive.replace("6.11.2", "6.11.1")
            files[key] = json.dumps(inventory).encode()
            errors = validate_payload(files)
            report.check(any("qtimageformats" in error for error in errors), "Qt validation rejects a wrong-version imageformats archive", "; ".join(errors))


def check_qt_wheel_notice_platforms(report: Report) -> None:
    qt_wheels = ("PySide6", "PySide6_Essentials", "shiboken6")
    files, inventory = legal_fixture()
    for component in inventory["components"]:
        if component["name"] in qt_wheels:
            commercial = [
                path for path in component["notice_paths"]
                if path.endswith("/LicenseRef-Qt-Commercial.txt")
            ]
            component["notice_paths"] = [
                path for path in component["notice_paths"] if path not in commercial
            ]
            for path in commercial:
                files.pop(f"{ARTIFACT_ROOT}/{path}")
    key = f"{ARTIFACT_ROOT}/{INVENTORY_NAME}"
    files[key] = json.dumps(inventory).encode()
    errors = validate_payload(files)
    report.check(not errors, "Linux validation accepts Qt wheels that ship no license files", "; ".join(errors))
    inventory["platform"]["system"] = "Windows"
    files[key] = json.dumps(inventory).encode()
    errors = validate_payload(files)
    report.check(
        all(
            any(error.startswith(f"{name}: missing or misassigned") for error in errors)
            for name in qt_wheels
        ),
        "Windows validation still requires the Qt wheels' LicenseRef-Qt-Commercial notices",
        "; ".join(errors),
    )


def check_sources(report: Report) -> None:
    check_binary_filter(report)
    check_debian_common_notices(report)
    check_qt_imageformat_sources(report)
    check_qt_wheel_notice_platforms(report)
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
    check_conditional_typing_extensions(report, file_map, inventory_path)

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
