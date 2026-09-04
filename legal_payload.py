"""Generate and validate the legal payload for a PyInstaller bundle."""

from __future__ import annotations

import hashlib
import json
import marshal
import os
import platform as runtime_platform
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import unicodedata
import zlib
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from importlib import metadata
from pathlib import Path

INVENTORY_NAME = "DEPENDENCY_INVENTORY.json"
LEGAL_ROOT = "legal"
ARTIFACT_ROOT = "SDC-Testing-Toolbox"
ARTIFACT_LAYOUT = "PyInstaller one-directory bundle with replaceable shared libraries"
REQUIRED_DOCUMENTS = (
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    INVENTORY_NAME,
    f"{LEGAL_ROOT}/SOURCE_OFFER.md",
    f"{LEGAL_ROOT}/PROVENANCE.md",
    f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt",
    f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt",
    f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt",
    f"{LEGAL_ROOT}/licenses/openssl/Apache-2.0.txt",
    f"{LEGAL_ROOT}/licenses/common/Zlib.txt",
    f"{LEGAL_ROOT}/licenses/iconv/LGPL-2.1-only.txt",
    f"{LEGAL_ROOT}/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.txt",
    f"{LEGAL_ROOT}/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.docx",
    f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/LICENSE.txt",
    f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/ijg-license.txt",
    f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/COPYRIGHT.txt",
    f"{LEGAL_ROOT}/licenses/qt-third-party/libtiff/COPYRIGHT",
    f"{LEGAL_ROOT}/licenses/qt-third-party/libwebp/COPYING",
)

_NATIVE_NOTICE_RULES = {
    "lxml wheel: zlib": (
        (
            f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt",
            ("officiall distributed binary wheels", "**zlib**"),
        ),
        (f"{LEGAL_ROOT}/licenses/common/Zlib.txt", ("Jean-loup Gailly", "Mark Adler")),
    ),
    "lxml wheel: iconv": (
        (f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt", ("**iconv**: LGPL 2.1",)),
        (
            f"{LEGAL_ROOT}/licenses/iconv/LGPL-2.1-only.txt",
            ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 2.1, February 1999"),
        ),
    ),
    "lxml wheel: libxml2": (
        (
            f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt",
            ("**libxml2**: MIT", "The Libxml2 Contributors"),
        ),
    ),
    "lxml wheel: libxslt": (
        (
            f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt",
            ("**libxslt**: MIT", "DANIEL VEILLARD"),
        ),
    ),
    "lxml wheel: libexslt": (
        (
            f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt",
            ("**libexslt**: MIT", "Thomas Broyer"),
        ),
    ),
    "Qt image plugin: libjpeg-turbo": (
        (
            f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/LICENSE.txt",
            ("libjpeg-turbo Licenses", "Modified (3-clause) BSD License"),
        ),
        (
            f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/ijg-license.txt",
            ("Independent JPEG Group", "executable code is distributed"),
        ),
        (
            f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/COPYRIGHT.txt",
            ("D. R. Commander", "Thomas G. Lane"),
        ),
    ),
    "Qt image plugin: libtiff": (
        (
            f"{LEGAL_ROOT}/licenses/qt-third-party/libtiff/COPYRIGHT",
            ("Sam Leffler", "Silicon Graphics"),
        ),
    ),
    "Qt image plugin: libwebp": (
        (
            f"{LEGAL_ROOT}/licenses/qt-third-party/libwebp/COPYING",
            ("Copyright (c) 2010, Google Inc.", "Redistribution and use"),
        ),
    ),
    "Qt: zlib": (
        (f"{LEGAL_ROOT}/licenses/common/Zlib.txt", ("Jean-loup Gailly", "Mark Adler")),
    ),
    "Microsoft Visual C++ Runtime": (
        (
            f"{LEGAL_ROOT}/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.txt",
            (
                "MICROSOFT VISUAL C++ 2015 - 2022 RUNTIME",
                "EULA ID: Cpp_2015-2022_ENU.1033",
            ),
        ),
        (f"{LEGAL_ROOT}/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.docx", ()),
    ),
    "OpenSSL": (
        (
            f"{LEGAL_ROOT}/licenses/openssl/Apache-2.0.txt",
            ("Apache License", "Version 2.0, January 2004"),
        ),
    ),
}

_CORE_NOTICE_RULES = {
    "aiohappyeyeballs": ((f"{LEGAL_ROOT}/licenses/aiohappyeyeballs/LICENSE", ()),),
    "aiohttp": (
        (f"{LEGAL_ROOT}/licenses/aiohttp/LICENSE.txt", ()),
        (f"{LEGAL_ROOT}/licenses/aiohttp/vendor/llhttp/LICENSE", ()),
    ),
    "aiosignal": ((f"{LEGAL_ROOT}/licenses/aiosignal/LICENSE", ()),),
    "attrs": ((f"{LEGAL_ROOT}/licenses/attrs/LICENSE", ()),),
    "frozenlist": ((f"{LEGAL_ROOT}/licenses/frozenlist/LICENSE", ()),),
    "idna": ((f"{LEGAL_ROOT}/licenses/idna/LICENSE.md", ()),),
    "ifaddr": ((f"{LEGAL_ROOT}/licenses/ifaddr/LICENSE.txt", ()),),
    "lxml": (
        (f"{LEGAL_ROOT}/licenses/lxml/LICENSE.txt", ()),
        (f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt", ()),
    ),
    "multidict": ((f"{LEGAL_ROOT}/licenses/multidict/LICENSE", ()),),
    "propcache": (
        (f"{LEGAL_ROOT}/licenses/propcache/LICENSE", ()),
        (f"{LEGAL_ROOT}/licenses/propcache/NOTICE", ()),
    ),
    "Python": (
        (
            f"{LEGAL_ROOT}/licenses/python/LICENSE.txt",
            ("PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2",),
        ),
    ),
    "Qt": (
        (
            f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt",
            ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 3, 29 June 2007"),
        ),
        (
            f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt",
            ("GNU GENERAL PUBLIC LICENSE", "Version 3, 29 June 2007"),
        ),
        (
            f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt",
            ("The Qt Company GPL Exception",),
        ),
        (f"{LEGAL_ROOT}/SOURCE_OFFER.md", ("Corresponding Source",)),
    ),
    "PySide6": (
        (
            f"{LEGAL_ROOT}/licenses/pyside6/LicenseRef-Qt-Commercial.txt",
            ("valid commercial Qt licenses",),
        ),
        (
            f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt",
            ("GNU LESSER GENERAL PUBLIC LICENSE",),
        ),
        (f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt", ("GNU GENERAL PUBLIC LICENSE",)),
        (
            f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt",
            ("The Qt Company GPL Exception",),
        ),
    ),
    "PySide6_Essentials": (
        (
            f"{LEGAL_ROOT}/licenses/pyside6-essentials/LicenseRef-Qt-Commercial.txt",
            ("valid commercial Qt licenses",),
        ),
        (
            f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt",
            ("GNU LESSER GENERAL PUBLIC LICENSE",),
        ),
        (f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt", ("GNU GENERAL PUBLIC LICENSE",)),
        (
            f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt",
            ("The Qt Company GPL Exception",),
        ),
    ),
    "shiboken6": (
        (
            f"{LEGAL_ROOT}/licenses/shiboken6/LicenseRef-Qt-Commercial.txt",
            ("valid commercial Qt licenses",),
        ),
        (
            f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt",
            ("GNU LESSER GENERAL PUBLIC LICENSE",),
        ),
        (f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt", ("GNU GENERAL PUBLIC LICENSE",)),
        (
            f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt",
            ("The Qt Company GPL Exception",),
        ),
    ),
    "sdc11073": (
        (f"{LEGAL_ROOT}/licenses/sdc11073/LICENSE", ("MIT License", "Draeger")),
    ),
    "pyinstaller": (
        (f"{LEGAL_ROOT}/licenses/pyinstaller/COPYING.txt", ("Bootloader Exception",)),
    ),
    "typing_extensions": ((f"{LEGAL_ROOT}/licenses/typing-extensions/LICENSE", ()),),
    "yarl": (
        (f"{LEGAL_ROOT}/licenses/yarl/LICENSE", ()),
        (f"{LEGAL_ROOT}/licenses/yarl/NOTICE", ()),
    ),
}

# A module-only inventory claim must agree with ownership knowledge in the
# validator, rather than becoming true merely because the inventory says so.
_MODULE_PREFIXES = {
    "aiohappyeyeballs": ("aiohappyeyeballs",),
    "aiohttp": ("aiohttp",),
    "aiosignal": ("aiosignal",),
    "attrs": ("attr", "attrs"),
    "frozenlist": ("frozenlist",),
    "idna": ("idna",),
    "ifaddr": ("ifaddr",),
    "lxml": ("lxml",),
    "multidict": ("multidict",),
    "propcache": ("propcache",),
    "pyinstaller": (
        "_pyi_rth_utils",
        "pyimod",
    ),
    "pyside6": ("PySide6",),
    "pyside6-essentials": ("PySide6",),
    "python": (
        "__future__",
        "asyncio",
        "importlib",
    ),
    "qt": ("Qt libraries and plugins carried by PySide6 wheels",),
    "sdc11073": ("sdc11073",),
    "shiboken6": ("shiboken6",),
    "typing-extensions": ("typing_extensions",),
    "yarl": ("yarl",),
}

_LICENSE_OVERRIDES = {
    "aiosignal": "Apache-2.0",
    "pyinstaller": "(GPL-2.0-or-later WITH Bootloader-exception) OR Apache-2.0 OR MIT",
    "pyinstaller-hooks-contrib": "Apache-2.0 OR GPL-2.0-only",
}
_SOURCE_OVERRIDES = {
    "pyinstaller-hooks-contrib": "https://github.com/pyinstaller/pyinstaller-hooks-contrib",
}
_LEGAL_BASENAME = re.compile(
    r"^(license|licence|copying|notice|authors|copyright)", re.IGNORECASE
)


def _canonicalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _entry_parts(entry: object) -> tuple[str, str]:
    values = tuple(entry)  # type: ignore[arg-type]
    if len(values) < 2:
        raise ValueError(f"unexpected PyInstaller TOC entry: {entry!r}")
    return str(values[0]), str(values[1])


def _resolved(path: str | Path) -> Path:
    return Path(path).resolve()


def _is_unused_qt_binary(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    basename = normalized.rsplit("/", 1)[-1]
    if re.fullmatch(
        r"(?:qt6(?:pdf|virtualkeyboard)[^/]*\.dll|libqt6(?:pdf|virtualkeyboard)[^/]*\.so(?:\.\d+)*)",
        basename,
    ):
        return True
    if "/pyside6/" in f"/{normalized}" and re.fullmatch(
        r"qtpdf[^/]*\.(?:pyd|so(?:\.\d+)*)", basename
    ):
        return True
    plugin_path = f"/{normalized}"
    return bool(
        (
            "/plugins/imageformats/" in plugin_path
            and re.fullmatch(r"(?:qpdf\.dll|libqpdf\.so(?:\.\d+)*)", basename)
        )
        or (
            "/plugins/platforminputcontexts/" in plugin_path
            and re.fullmatch(
                r"(?:qtvirtualkeyboardplugin\.dll|libqtvirtualkeyboardplugin\.so(?:\.\d+)*)",
                basename,
            )
        )
    )


def filter_binaries(
    binaries: Iterable[object], platform: str | None = None
) -> list[object]:
    """Drop unused Qt payloads, plus Windows host/PATH contamination."""
    platform = platform or runtime_platform.system()
    windows_root_text = os.environ.get("WINDIR")
    windows_root = _resolved(windows_root_text) if windows_root_text else None
    installed_base = _resolved(sysconfig.get_config_var("installed_base"))
    environment_root = _resolved(sys.prefix)
    result = []
    for entry in binaries:
        destination, source_text = _entry_parts(entry)
        if _is_unused_qt_binary(destination) or _is_unused_qt_binary(source_text):
            continue
        if platform != "Windows":
            result.append(entry)
            continue
        source = _resolved(source_text)
        if windows_root is not None and source.is_relative_to(windows_root):
            continue
        if source.is_relative_to(installed_base) or source.is_relative_to(
            environment_root
        ):
            result.append(entry)
            continue
        print(f"legal inventory: excluding machine-local binary {source}")
    return result


def _distributions() -> dict[str, metadata.Distribution]:
    result: dict[str, metadata.Distribution] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata.get("Name")
        if name:
            result[_canonicalize_name(name)] = distribution
    return result


def _distribution_file_owners(
    distributions: dict[str, metadata.Distribution],
) -> dict[Path, set[str]]:
    owners: dict[Path, set[str]] = defaultdict(set)
    for name, distribution in distributions.items():
        for relative in distribution.files or ():
            owners[_resolved(distribution.locate_file(relative))].add(name)
    return owners


def _dependency_closure(
    roots: Iterable[str], distributions: dict[str, metadata.Distribution]
) -> set[str]:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    environment = default_environment()
    pending = deque(_canonicalize_name(root) for root in roots)
    found: set[str] = set()
    while pending:
        name = pending.popleft()
        if name in found:
            continue
        distribution = distributions.get(name)
        if distribution is None:
            raise RuntimeError(f"required distribution is not installed: {name}")
        found.add(name)
        for requirement_text in distribution.requires or ():
            requirement = Requirement(requirement_text)
            if requirement.marker and not requirement.marker.evaluate(environment):
                continue
            pending.append(_canonicalize_name(requirement.name))
    return found


def _project_url(distribution: metadata.Distribution, name: str) -> str:
    preferred = ("repository", "source", "source code", "homepage", "home")
    parsed: list[tuple[str, str]] = []
    for value in distribution.metadata.get_all("Project-URL") or ():
        label, separator, url = value.partition(",")
        if separator and url.strip().startswith(("https://", "http://")):
            parsed.append((label.strip().lower(), url.strip()))
    for wanted in preferred:
        for label, url in parsed:
            if label == wanted:
                return url
    homepage = distribution.metadata.get("Home-page")
    if homepage and homepage.startswith(("https://", "http://")):
        return homepage
    if parsed:
        return parsed[0][1]
    return _SOURCE_OVERRIDES.get(name, "")


def _license_expression(distribution: metadata.Distribution, name: str) -> str:
    if name in _LICENSE_OVERRIDES:
        return _LICENSE_OVERRIDES[name]
    value = distribution.metadata.get(
        "License-Expression"
    ) or distribution.metadata.get("License")
    return value.strip() if value else ""


def _legal_files(distribution: metadata.Distribution) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for relative in distribution.files or ():
        relative_path = Path(str(relative))
        if not _LEGAL_BASENAME.match(relative_path.name):
            continue
        source = _resolved(distribution.locate_file(relative))
        if source.is_file():
            files.append((source, relative_path.as_posix()))
    return sorted(set(files), key=lambda item: item[1].lower())


def _safe_notice_name(relative: str) -> str:
    parts = Path(relative).parts
    try:
        marker = next(
            index
            for index, part in enumerate(parts)
            if part.lower() in {"licenses", "license_files"}
        )
        parts = parts[marker + 1 :]
    except StopIteration:
        parts = (parts[-1],)
    return "/".join(parts)


def _copy_distribution_notices(
    name: str, distribution: metadata.Distribution, output: Path
) -> list[str]:
    notice_paths: list[str] = []
    used: set[str] = set()
    for source, relative in _legal_files(distribution):
        suffix = _safe_notice_name(relative)
        destination = f"{LEGAL_ROOT}/licenses/{name}/{suffix}"
        if destination.lower() in used:
            destination = (
                f"{LEGAL_ROOT}/licenses/{name}/{Path(relative).name}-{len(used) + 1}"
            )
        used.add(destination.lower())
        target = output / Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        notice_paths.append(destination)
    return notice_paths


def _copy_static_documents(project_root: Path, output: Path) -> list[str]:
    documents = {
        "LICENSE": project_root / "LICENSE",
        "THIRD_PARTY_NOTICES.md": project_root / "THIRD_PARTY_NOTICES.md",
        f"{LEGAL_ROOT}/SOURCE_OFFER.md": project_root / LEGAL_ROOT / "SOURCE_OFFER.md",
        f"{LEGAL_ROOT}/PROVENANCE.md": project_root / LEGAL_ROOT / "PROVENANCE.md",
        f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "qt"
        / "LGPL-3.0-only.txt",
        f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt": project_root / "LICENSE",
        f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "qt"
        / "Qt-GPL-exception-1.0.txt",
        f"{LEGAL_ROOT}/licenses/openssl/Apache-2.0.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "openssl"
        / "Apache-2.0.txt",
        f"{LEGAL_ROOT}/licenses/common/Zlib.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "common"
        / "Zlib.txt",
        f"{LEGAL_ROOT}/licenses/iconv/LGPL-2.1-only.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "iconv"
        / "LGPL-2.1-only.txt",
        f"{LEGAL_ROOT}/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "microsoft"
        / "Visual-Cpp-Runtime-2015-2022.txt",
        f"{LEGAL_ROOT}/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.docx": project_root
        / LEGAL_ROOT
        / "licenses"
        / "microsoft"
        / "Visual-Cpp-Runtime-2015-2022.docx",
        f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/LICENSE.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "qt-third-party"
        / "libjpeg-turbo"
        / "LICENSE.txt",
        f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/ijg-license.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "qt-third-party"
        / "libjpeg-turbo"
        / "ijg-license.txt",
        f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/COPYRIGHT.txt": project_root
        / LEGAL_ROOT
        / "licenses"
        / "qt-third-party"
        / "libjpeg-turbo"
        / "COPYRIGHT.txt",
        f"{LEGAL_ROOT}/licenses/qt-third-party/libtiff/COPYRIGHT": project_root
        / LEGAL_ROOT
        / "licenses"
        / "qt-third-party"
        / "libtiff"
        / "COPYRIGHT",
        f"{LEGAL_ROOT}/licenses/qt-third-party/libwebp/COPYING": project_root
        / LEGAL_ROOT
        / "licenses"
        / "qt-third-party"
        / "libwebp"
        / "COPYING",
    }
    for destination, source in documents.items():
        if not source.is_file():
            raise RuntimeError(f"required legal source is missing: {source}")
        target = output / Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return sorted(documents)


def _notice_hashes(output: Path, notice_paths: Iterable[str]) -> dict[str, str]:
    return {
        path: hashlib.sha256((output / Path(path)).read_bytes()).hexdigest()
        for path in notice_paths
    }


def _native_component(
    output: Path,
    *,
    name: str,
    version: str,
    license_expression: str,
    project_url: str,
    notice_paths: list[str],
    artifact_paths: Iterable[str],
    artifact_patterns: list[str],
    owner_evidence: dict[str, str],
    source_archive_urls: list[str] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "scope": "runtime-embedded-native-library",
        "license_expression": license_expression,
        "project_url": project_url,
        "source_archive_urls": source_archive_urls or [],
        "notice_paths": notice_paths,
        "notice_sha256": _notice_hashes(output, notice_paths),
        "evidence": {
            "artifact_paths": sorted(artifact_paths),
            "artifact_patterns": artifact_patterns,
            "owner": owner_evidence,
        },
    }


def _windows_file_version(path: Path) -> str:
    import pefile

    image = pefile.PE(str(path), fast_load=True)
    image.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
    )
    for file_info in getattr(image, "FileInfo", ()):
        for entry in file_info:
            for table in getattr(entry, "StringTable", ()):
                for key, value in table.entries.items():
                    if key.decode(errors="replace") == "FileVersion":
                        return value.decode(errors="replace")
    raise RuntimeError(f"cannot read native library version from {path}")


def _linked_library_names(path: Path) -> set[str]:
    if runtime_platform.system() == "Windows":
        import pefile

        image = pefile.PE(str(path), fast_load=True)
        image.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
        return {
            entry.dll.decode(errors="replace").lower()
            for entry in getattr(image, "DIRECTORY_ENTRY_IMPORT", ())
        }
    if runtime_platform.system() == "Linux":
        result = subprocess.run(
            ["ldd", str(path)], check=True, capture_output=True, text=True
        )
        return {
            Path(line.split("=>", 1)[0].strip()).name.lower()
            for line in result.stdout.splitlines()
            if ".so" in line
        }
    return set()


def _lxml_native_components(
    distribution: metadata.Distribution,
    artifact_sources: dict[str, Path],
    output: Path,
) -> list[dict[str, object]]:
    carriers = {
        destination: source
        for destination, source in artifact_sources.items()
        if Path(destination).name.lower().startswith("etree.")
        and Path(destination).suffix.lower() in {".pyd", ".so"}
    }
    if not carriers:
        return []
    linked = set().union(
        *(_linked_library_names(source) for source in carriers.values())
    )
    if any(
        name.startswith(("libxml2", "libxslt", "libexslt", "libiconv", "zlib"))
        for name in linked
    ):
        return []

    from lxml import etree

    includes = Path(distribution.locate_file("lxml/includes"))

    def define(relative: str, name: str) -> str:
        text = (includes / relative).read_text(encoding="utf-8", errors="replace")
        match = re.search(rf'^#define\s+{name}\s+"([^"]+)"', text, re.MULTILINE)
        if not match:
            raise RuntimeError(f"lxml bundled header does not declare {name}")
        return match.group(1)

    versions = {
        "zlib": define("extlibs/zlib.h", "ZLIB_VERSION"),
        "iconv": ".".join(map(str, etree.ICONV_COMPILED_VERSION)),
        "libxml2": ".".join(map(str, etree.LIBXML_COMPILED_VERSION)),
        "libxslt": define("libxslt/xsltconfig.h", "LIBXSLT_DOTTED_VERSION"),
        "libexslt": define("libexslt/exsltconfig.h", "LIBEXSLT_DOTTED_VERSION"),
    }
    specs = {
        "zlib": (
            "Zlib",
            "https://zlib.net/",
            [
                f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt",
                f"{LEGAL_ROOT}/licenses/common/Zlib.txt",
            ],
        ),
        "iconv": (
            "LGPL-2.1-only",
            "https://www.gnu.org/software/libiconv/",
            [
                f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt",
                f"{LEGAL_ROOT}/licenses/iconv/LGPL-2.1-only.txt",
            ],
        ),
        "libxml2": (
            "MIT",
            "https://gitlab.gnome.org/GNOME/libxml2",
            [f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt"],
        ),
        "libxslt": (
            "MIT",
            "https://gitlab.gnome.org/GNOME/libxslt",
            [f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt"],
        ),
        "libexslt": (
            "MIT",
            "https://gitlab.gnome.org/GNOME/libxslt",
            [f"{LEGAL_ROOT}/licenses/lxml/LICENSES.txt"],
        ),
    }
    paths = sorted(carriers)
    owner = {
        "carrier_distribution": f"lxml {distribution.version}",
        "component_attribution": "lxml wheel licenses/LICENSES.txt, Binary wheels section",
        "linkage": "compiled lxml constants/headers; carrier has no external libxml/iconv/zlib import",
    }
    return [
        _native_component(
            output,
            name=f"lxml wheel: {name}",
            version=versions[name],
            license_expression=license_expression,
            project_url=project_url,
            notice_paths=notices,
            artifact_paths=paths,
            artifact_patterns=["lxml/etree*.pyd", "lxml/etree*.so"],
            owner_evidence=owner,
            source_archive_urls=(
                [
                    f"https://ftp.gnu.org/pub/gnu/libiconv/libiconv-{versions[name]}.tar.gz"
                ]
                if name == "iconv"
                else []
            ),
        )
        for name, (license_expression, project_url, notices) in specs.items()
    ]


def _qt_native_components(
    qt_version: str,
    artifact_sources: dict[str, Path],
    output: Path,
) -> list[dict[str, object]]:
    # Component versions and licenses come from Qt's matching tagged source
    # qt_attribution.json files, not from guesses based on plugin names.
    if qt_version != "6.11.2":
        if any(
            "/plugins/imageformats/" in f"/{path.lower()}" for path in artifact_sources
        ):
            raise RuntimeError(
                f"Qt native third-party metadata is not maintained for {qt_version}"
            )
        return []
    specs = {
        "qjpeg": (
            "Qt image plugin: libjpeg-turbo",
            "3.2.0",
            "IJG AND BSD-3-Clause",
            "https://github.com/libjpeg-turbo/libjpeg-turbo",
            [
                f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/LICENSE.txt",
                f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/ijg-license.txt",
                f"{LEGAL_ROOT}/licenses/qt-third-party/libjpeg-turbo/COPYRIGHT.txt",
            ],
        ),
        "qtiff": (
            "Qt image plugin: libtiff",
            "4.7.2",
            "libtiff",
            "https://gitlab.com/libtiff/libtiff",
            [f"{LEGAL_ROOT}/licenses/qt-third-party/libtiff/COPYRIGHT"],
        ),
        "qwebp": (
            "Qt image plugin: libwebp",
            "1.6.0",
            "BSD-3-Clause",
            "https://chromium.googlesource.com/webm/libwebp/",
            [f"{LEGAL_ROOT}/licenses/qt-third-party/libwebp/COPYING"],
        ),
    }
    components = []
    for stem, (
        name,
        version,
        license_expression,
        project_url,
        notices,
    ) in specs.items():
        carriers = {
            destination: source
            for destination, source in artifact_sources.items()
            if "/plugins/imageformats/" in f"/{destination.lower()}"
            and Path(destination).stem.lower() in {stem, f"lib{stem}"}
        }
        if not carriers:
            continue
        imports = set().union(
            *(_linked_library_names(source) for source in carriers.values())
        )
        library_prefix = {
            "qjpeg": "libjpeg",
            "qtiff": "libtiff",
            "qwebp": "libwebp",
        }[stem]
        external = {
            library for library in imports if library.startswith(library_prefix)
        }
        if external:
            # Linux wheels can link plugins to distribution libraries. Those
            # are inventoried from dpkg ownership instead of Qt source metadata.
            packaged_libraries = {
                destination: source
                for destination, source in artifact_sources.items()
                if Path(destination).name.lower() in external
            }
            if not packaged_libraries or not all(
                "/pyside6/" in f"/{str(source).replace('\\', '/').lower()}"
                for source in packaged_libraries.values()
            ):
                continue
            carriers.update(packaged_libraries)
        components.append(
            _native_component(
                output,
                name=name,
                version=version,
                license_expression=license_expression,
                project_url=project_url,
                notice_paths=notices,
                artifact_paths=carriers,
                artifact_patterns=[
                    f"PySide6/**/plugins/imageformats/{stem}.*",
                    f"PySide6/**/plugins/imageformats/lib{stem}.so*",
                ],
                owner_evidence={
                    "carrier_distribution": f"PySide6_Essentials {qt_version}",
                    "component_attribution": f"Qt {qt_version} source qt_attribution.json",
                    "linkage": "codec is embedded in plugin or its imported codec library is packaged",
                },
            )
        )

    core = {
        destination: source
        for destination, source in artifact_sources.items()
        if Path(destination).name.lower()
        in {"qt6core.dll", "libqt6core.so", "libqt6core.so.6"}
    }
    possible_zlib_carriers = dict(core)
    possible_zlib_carriers.update(
        {
            destination: source
            for destination, source in artifact_sources.items()
            if "/plugins/imageformats/" in f"/{destination.lower()}"
            and Path(destination).stem.lower() in {"qtiff", "libqtiff"}
        }
    )
    embedded_zlib = {}
    for destination, source in possible_zlib_carriers.items():
        binary = source.read_bytes()
        linked = _linked_library_names(source)
        if (
            b"1.3.2" in binary
            and b"zlib" in binary.lower()
            and not any(name.startswith("libz.so") for name in linked)
        ):
            embedded_zlib[destination] = source
    if embedded_zlib:
        components.append(
            _native_component(
                output,
                name="Qt: zlib",
                version="1.3.2",
                license_expression="Zlib",
                project_url="https://zlib.net/",
                notice_paths=[f"{LEGAL_ROOT}/licenses/common/Zlib.txt"],
                artifact_paths=embedded_zlib,
                artifact_patterns=[
                    "PySide6/Qt6Core.dll",
                    "PySide6/**/libQt6Core.so*",
                    "PySide6/**/plugins/imageformats/qtiff.*",
                    "PySide6/**/plugins/imageformats/libqtiff.so*",
                ],
                owner_evidence={
                    "carrier_distribution": f"PySide6_Essentials {qt_version}",
                    "component_attribution": f"Qt {qt_version} qtbase zlib/qt_attribution.json",
                    "linkage": "carriers contain zlib and 1.3.2 strings and have no external libz import",
                },
            )
        )
    return components


def _debian_native_components(
    native_paths: dict[Path, set[str]], output: Path
) -> list[dict[str, object]]:
    if not native_paths:
        return []
    owners: dict[str, set[str]] = defaultdict(set)
    for source, paths in native_paths.items():
        query = subprocess.run(
            ["dpkg-query", "-S", str(source)],
            check=False,
            capture_output=True,
            text=True,
        )
        if query.returncode != 0:
            raise RuntimeError(
                f"cannot assign packaged Linux library to a Debian package: {source}"
            )
        package = query.stdout.split(":", 1)[0]
        owners[package].update(paths)

    components = []
    for package in sorted(owners):
        query = subprocess.run(
            [
                "dpkg-query",
                "-W",
                "-f=${Version}\t${Homepage}\t${source:Package}\n",
                package,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        version, homepage, source_package = query.stdout.rstrip("\n").split("\t")
        copyright_file = Path("/usr/share/doc") / package.split(":", 1)[0] / "copyright"
        if not copyright_file.is_file():
            raise RuntimeError(
                f"Debian package has no machine-readable notice file: {package}"
            )
        copyright_text = copyright_file.read_text(encoding="utf-8", errors="replace")
        licenses = sorted(
            set(re.findall(r"^License:\s*([^\n]+)", copyright_text, re.MULTILINE))
        )
        if not licenses:
            raise RuntimeError(
                f"Debian copyright file declares no license: {copyright_file}"
            )
        notice = f"{LEGAL_ROOT}/licenses/debian/{package.replace(':', '_')}/copyright"
        target = output / Path(notice)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(copyright_file, target)
        notice_paths = [notice]
        for common in sorted(
            set(
                re.findall(
                    r"/usr/share/common-licenses/([A-Za-z0-9.+-]+)", copyright_text
                )
            )
        ):
            source = Path("/usr/share/common-licenses") / common
            if source.is_file():
                destination = f"{LEGAL_ROOT}/licenses/debian/common/{common}"
                common_target = output / Path(destination)
                common_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, common_target)
                notice_paths.append(destination)
        source_name = source_package or package.split(":", 1)[0]
        components.append(
            _native_component(
                output,
                name=f"Debian package {package}",
                version=version,
                license_expression=" AND ".join(licenses),
                project_url=homepage
                or f"https://packages.ubuntu.com/search?keywords={source_name}",
                source_archive_urls=[
                    f"https://packages.ubuntu.com/source/jammy/{source_name}"
                ],
                notice_paths=notice_paths,
                artifact_paths=owners[package],
                artifact_patterns=[f"**/{Path(path).name}" for path in owners[package]],
                owner_evidence={
                    "debian_binary_package": package,
                    "debian_source_package": source_name,
                    "method": "dpkg-query -S and package copyright file",
                },
            )
        )
    return components


def generate_payload(
    project_root: Path,
    output: Path,
    pure: Iterable[object],
    binaries: Iterable[object],
    datas: Iterable[object],
) -> list[tuple[str, str, str]]:
    """Generate payload files and return PyInstaller data-file tuples."""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    static_documents = _copy_static_documents(project_root, output)

    distributions = _distributions()
    owners = _distribution_file_owners(distributions)
    runtime_closure = _dependency_closure(("sdc11073", "PySide6"), distributions)
    build_closure = _dependency_closure(
        ("PyInstaller", "pyinstaller-hooks-contrib"), distributions
    )

    modules: dict[str, set[str]] = defaultdict(set)
    artifact_paths: dict[str, set[str]] = defaultdict(set)
    artifact_sources: dict[str, Path] = {}
    unmatched_site_packages: list[str] = []
    native_paths: dict[Path, set[str]] = defaultdict(set)
    installed_base = _resolved(sysconfig.get_config_var("installed_base"))

    for entry in pure:
        module, source_text = _entry_parts(entry)
        source = _resolved(source_text)
        matched = owners.get(source, set())
        if matched >= {"pyside6", "pyside6-addons", "pyside6-essentials"}:
            matched = {"pyside6"}
        if not matched and "site-packages" in {part.lower() for part in source.parts}:
            unmatched_site_packages.append(str(source))
        for name in matched:
            modules[name].add(module)
        if (
            not matched
            and source.is_relative_to(installed_base)
            and "site-packages" not in {part.lower() for part in source.parts}
        ):
            modules["python"].add(module)

    for collection, is_binary in ((binaries, True), (datas, False)):
        for entry in collection:
            destination, source_text = _entry_parts(entry)
            source = _resolved(source_text)
            if is_binary:
                artifact_sources[destination.replace("\\", "/")] = source
            matched = owners.get(source, set())
            if not matched and "site-packages" in {
                part.lower() for part in source.parts
            }:
                unmatched_site_packages.append(str(source))
            for name in matched:
                artifact_paths[name].add(destination.replace("\\", "/"))
            if not matched and source.is_relative_to(installed_base):
                artifact_paths["python"].add(destination.replace("\\", "/"))
            elif not matched and is_binary and runtime_platform.system() == "Linux":
                native_paths[source].add(destination.replace("\\", "/"))

    if unmatched_site_packages:
        details = "\n".join(sorted(set(unmatched_site_packages))[:20])
        raise RuntimeError(
            f"cannot assign packaged site-packages files to distributions:\n{details}"
        )

    packaged = set(modules) | set(artifact_paths)
    packaged.discard("python")
    # PyInstaller's bootloader and loader are embedded after Analysis and therefore
    # cannot be attributed from an Analysis TOC like ordinary distributions.
    packaged.add("pyinstaller")
    components: list[dict[str, object]] = []
    native_components: list[dict[str, object]] = []
    for name in sorted(packaged):
        distribution = distributions[name]
        expression = _license_expression(distribution, name)
        project_url = _project_url(distribution, name)
        notice_paths = _copy_distribution_notices(name, distribution, output)
        if name in {"pyside6", "pyside6-addons", "pyside6-essentials", "shiboken6"}:
            notice_paths.extend(
                [
                    f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt",
                    f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt",
                    f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt",
                ]
            )
        missing = []
        if not expression:
            missing.append("declared license/license expression")
        if not project_url:
            missing.append("project/source URL")
        if not notice_paths:
            missing.append("bundled license or notice text")
        if missing:
            raise RuntimeError(f"{name} metadata lacks {', '.join(missing)}")
        scope = "runtime"
        if name in build_closure and name not in runtime_closure:
            scope = "runtime-embedded-build-tool"
        components.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "scope": scope,
                "license_expression": expression,
                "project_url": project_url,
                "source_archive_urls": (
                    [
                        (
                            f"https://download.qt.io/official_releases/QtForPython/pyside6/"
                            f"PySide6-{distribution.version}-src/"
                            f"pyside-setup-everywhere-src-{distribution.version}.tar.xz"
                        )
                    ]
                    if name
                    in {"pyside6", "pyside6-addons", "pyside6-essentials", "shiboken6"}
                    else (
                        [
                            (
                                f"https://github.com/pyinstaller/pyinstaller/"
                                f"archive/refs/tags/v{distribution.version}.tar.gz"
                            )
                        ]
                        if name == "pyinstaller"
                        else []
                    )
                ),
                "notice_paths": notice_paths,
                "evidence": {
                    "modules": sorted(modules[name]),
                    "artifact_paths": sorted(artifact_paths[name])
                    or (
                        [
                            "SDC-Testing-Toolbox executable bootloader and embedded loader"
                        ]
                        if name == "pyinstaller"
                        else []
                    ),
                },
            }
        )

    python_license = installed_base / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError(f"Python license is missing: {python_license}")
    python_notice = f"{LEGAL_ROOT}/licenses/python/LICENSE.txt"
    target = output / Path(python_notice)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(python_license, target)
    components.append(
        {
            "name": "Python",
            "version": runtime_platform.python_version(),
            "scope": "runtime",
            "license_expression": "PSF-2.0",
            "project_url": f"https://www.python.org/downloads/release/python-{sys.version_info.major}{sys.version_info.minor}{sys.version_info.micro}/",
            "source_archive_urls": [
                (
                    f"https://www.python.org/ftp/python/{runtime_platform.python_version()}/"
                    f"Python-{runtime_platform.python_version()}.tar.xz"
                )
            ],
            "notice_paths": [python_notice],
            "evidence": {
                "modules": sorted(modules["python"]),
                "artifact_paths": sorted(artifact_paths["python"]),
            },
        }
    )

    if runtime_platform.system() == "Windows":
        microsoft_paths: dict[str, set[str]] = defaultdict(set)
        openssl_paths: dict[str, set[str]] = defaultdict(set)
        for collection in (binaries, datas):
            for entry in collection:
                destination, source_text = _entry_parts(entry)
                if Path(destination).name.lower().startswith(("msvcp", "vcruntime")):
                    microsoft_paths[_windows_file_version(Path(source_text))].add(
                        destination.replace("\\", "/")
                    )
                if Path(destination).name.lower().startswith(("libcrypto-", "libssl-")):
                    openssl_paths[_windows_file_version(Path(source_text))].add(
                        destination.replace("\\", "/")
                    )
        for version, paths in sorted(microsoft_paths.items()):
            native_components.append(
                _native_component(
                    output,
                    name="Microsoft Visual C++ Runtime",
                    version=version,
                    license_expression="LicenseRef-Microsoft-Visual-Cpp-Runtime-2015-2022",
                    project_url="https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/",
                    notice_paths=[
                        f"{LEGAL_ROOT}/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.txt",
                        f"{LEGAL_ROOT}/licenses/microsoft/Visual-Cpp-Runtime-2015-2022.docx",
                    ],
                    artifact_paths=paths,
                    artifact_patterns=["**/MSVCP*.dll", "**/VCRUNTIME*.dll"],
                    owner_evidence={
                        "file_version_resource": version,
                        "source": "Python installation or PySide6/Shiboken6 wheel recorded by Analysis TOC",
                    },
                )
            )
        for version, paths in sorted(openssl_paths.items()):
            native_components.append(
                _native_component(
                    output,
                    name="OpenSSL",
                    version=version,
                    license_expression="Apache-2.0",
                    project_url="https://github.com/openssl/openssl",
                    source_archive_urls=[
                        (
                            f"https://github.com/openssl/openssl/releases/download/"
                            f"openssl-{version}/openssl-{version}.tar.gz"
                        )
                    ],
                    notice_paths=[f"{LEGAL_ROOT}/licenses/openssl/Apache-2.0.txt"],
                    artifact_paths=paths,
                    artifact_patterns=["**/libcrypto-*.dll", "**/libssl-*.dll"],
                    owner_evidence={
                        "file_version_resource": version,
                        "source": "CPython installation recorded by Analysis TOC",
                    },
                )
            )

    native_components.extend(_debian_native_components(native_paths, output))
    if "lxml" in packaged:
        native_components.extend(
            _lxml_native_components(distributions["lxml"], artifact_sources, output)
        )

    qt_owners = {"pyside6-essentials", "pyside6-addons", "pyside6", "shiboken6"}
    if packaged & qt_owners:
        qt_version = distributions["pyside6"].version
        qt_paths = sorted(
            path
            for owner in qt_owners
            for path in artifact_paths.get(owner, set())
            if not Path(path).name.lower().startswith(("msvcp", "vcruntime"))
        )
        qt_modules = ["qtbase"]
        if any("Qt6Qml" in path or "Qt6Quick" in path for path in qt_paths):
            qt_modules.append("qtdeclarative")
        if any("Qt6Svg" in path or "/qsvg" in path for path in qt_paths):
            qt_modules.append("qtsvg")
        if any("translations/" in path for path in qt_paths):
            qt_modules.append("qttranslations")
        components.append(
            {
                "name": "Qt",
                "version": qt_version,
                "scope": "runtime-shared-libraries",
                "license_expression": "LGPL-3.0-only",
                "project_url": f"https://download.qt.io/official_releases/qt/{'.'.join(qt_version.split('.')[:2])}/{qt_version}/submodules/",
                "source_archive_urls": [
                    f"https://download.qt.io/official_releases/qt/"
                    f"{'.'.join(qt_version.split('.')[:2])}/{qt_version}/submodules/"
                    f"{module}-everywhere-src-{qt_version}.tar.xz"
                    for module in qt_modules
                ],
                "notice_paths": [
                    f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt",
                    f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt",
                    f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt",
                    f"{LEGAL_ROOT}/SOURCE_OFFER.md",
                ],
                "evidence": {
                    "modules": ["Qt libraries and plugins carried by PySide6 wheels"],
                    "artifact_paths": qt_paths,
                },
            }
        )
        native_components.extend(
            _qt_native_components(qt_version, artifact_sources, output)
        )

    build_only = []
    for name in sorted(build_closure - packaged):
        distribution = distributions[name]
        build_only.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "reason": "installed build dependency; no distribution-owned file found in the artifact",
            }
        )

    resolved_not_packaged = []
    for name in sorted(runtime_closure - packaged):
        distribution = distributions[name]
        resolved_not_packaged.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "reason": "resolved runtime requirement; no distribution-owned file found in the artifact",
            }
        )

    inventory = {
        "schema_version": 2,
        "artifact_layout": "PyInstaller one-directory bundle with replaceable shared libraries",
        "platform": {
            "system": runtime_platform.system(),
            "machine": runtime_platform.machine(),
        },
        "components": sorted(components, key=lambda item: str(item["name"]).lower()),
        "native_components": sorted(
            native_components,
            key=lambda item: (str(item["name"]).lower(), str(item["version"])),
        ),
        "build_only": build_only,
        "resolved_runtime_not_packaged": resolved_not_packaged,
        "resolved_runtime": [
            {
                "name": distributions[name].metadata["Name"],
                "version": distributions[name].version,
            }
            for name in sorted(runtime_closure)
        ],
        "required_documents": list(REQUIRED_DOCUMENTS),
        "generation": {
            "method": "installed distribution metadata plus PyInstaller Analysis TOCs",
            "runtime_roots": ["PySide6", "sdc11073"],
            "build_roots": ["PyInstaller", "pyinstaller-hooks-contrib"],
        },
    }
    inventory_path = output / INVENTORY_NAME
    inventory_path.write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    generated_files = static_documents + [
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.name != INVENTORY_NAME
    ]
    generated_files.append(INVENTORY_NAME)
    return [
        (relative, str(output / Path(relative)), "DATA")
        for relative in sorted(set(generated_files))
    ]


def _archive_path(path: object) -> tuple[str, str] | None:
    if not isinstance(path, str) or not path or "\x00" in path:
        return None
    slash_path = unicodedata.normalize("NFC", path.replace("\\", "/"))
    if slash_path.startswith("/") or re.match(r"^[A-Za-z]:", slash_path):
        return None
    parts = slash_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    normalized = "/".join(parts)
    return normalized, unicodedata.normalize("NFC", normalized).casefold()


def _pyinstaller_modules(executable: bytes) -> set[str]:
    """Read module names from a PyInstaller executable without importing it."""
    cookie_magic = b"MEI\014\013\012\013\016"
    cookie_format = "!8sIIII64s"
    cookie_length = struct.calcsize(cookie_format)
    cookie_offset = executable.rfind(cookie_magic)
    if cookie_offset < 0 or cookie_offset + cookie_length > len(executable):
        return set()
    _magic, archive_length, toc_offset, toc_length, _version, _library = struct.unpack(
        cookie_format, executable[cookie_offset : cookie_offset + cookie_length]
    )
    archive_start = cookie_offset + cookie_length - archive_length
    toc = executable[
        archive_start + toc_offset : archive_start + toc_offset + toc_length
    ]
    modules: set[str] = set()
    pyz_data = None
    position = 0
    entry_header = "!IIIIBc"
    entry_header_length = struct.calcsize(entry_header)
    while position + entry_header_length <= len(toc):
        entry_length, offset, length, _raw_length, compressed, typecode = struct.unpack(
            entry_header, toc[position : position + entry_header_length]
        )
        if entry_length < entry_header_length or position + entry_length > len(toc):
            return set()
        name = toc[position + entry_header_length : position + entry_length]
        name = name.rstrip(b"\0").decode("utf-8")
        if typecode in {b"m", b"M"}:
            modules.add(name)
        if typecode == b"z":
            pyz_data = executable[
                archive_start + offset : archive_start + offset + length
            ]
            if compressed:
                pyz_data = zlib.decompress(pyz_data)
        position += entry_length
    if pyz_data is None or not pyz_data.startswith(b"PYZ\0") or len(pyz_data) < 12:
        return modules
    pyz_toc_offset = struct.unpack("!i", pyz_data[8:12])[0]
    pyz_toc = marshal.loads(pyz_data[pyz_toc_offset:])
    if not isinstance(pyz_toc, (dict, list, tuple)):
        return set()
    entries = pyz_toc.keys() if isinstance(pyz_toc, dict) else dict(pyz_toc).keys()
    modules.update(name for name in entries if isinstance(name, str))
    return modules


def validate_payload(
    files: Mapping[str, bytes] | Iterable[tuple[str, bytes]],
) -> list[str]:
    """Return collected validation errors for files extracted from an artifact."""
    errors: list[str] = []
    entries: Iterable[object] = files.items() if isinstance(files, Mapping) else files
    normalized: dict[str, tuple[str, bytes]] = {}
    try:
        iterator = iter(entries)
    except TypeError:
        return ["archive file collection is not iterable"]
    for index, entry in enumerate(iterator):
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            errors.append(f"archive entry {index} is not a name/bytes pair")
            continue
        raw_name, value = entry
        parsed = _archive_path(raw_name)
        if parsed is None:
            errors.append(f"archive entry has unsafe or malformed path: {raw_name!r}")
            continue
        name, key = parsed
        if not isinstance(value, bytes):
            errors.append(f"archive entry is not file bytes: {name}")
            continue
        if key in normalized:
            errors.append(
                f"archive has duplicate normalized path: {normalized[key][0]!r} and {name!r}"
            )
            continue
        normalized[key] = (name, value)

    expected_root_key = unicodedata.normalize("NFC", ARTIFACT_ROOT).casefold()
    other_roots = sorted(
        {
            name.split("/", 1)[0]
            for name, _value in normalized.values()
            if name.split("/", 1)[0] != ARTIFACT_ROOT
        }
    )
    if other_roots:
        errors.append(
            f"archive contains files outside the single {ARTIFACT_ROOT} root: "
            + ", ".join(other_roots)
        )
    root_files = sorted(name for name, _value in normalized.values() if "/" not in name)
    if root_files:
        errors.append(
            "archive contains files outside its root directory: "
            + ", ".join(root_files)
        )
    if not any(
        name.startswith(f"{ARTIFACT_ROOT}/") for name, _value in normalized.values()
    ):
        errors.append(f"archive has no files under expected root {ARTIFACT_ROOT}")

    def archive_file(relative: str) -> bytes | None:
        parsed = _archive_path(f"{ARTIFACT_ROOT}/{relative}")
        if parsed is None:
            return None
        found = normalized.get(parsed[1])
        return (
            found[1] if found and found[0].split("/", 1)[0] == ARTIFACT_ROOT else None
        )

    inventory_key = f"{expected_root_key}/{INVENTORY_NAME.casefold()}"
    inventory_entry = normalized.get(inventory_key)
    if (
        inventory_entry is None
        or inventory_entry[0] != f"{ARTIFACT_ROOT}/{INVENTORY_NAME}"
    ):
        errors.append(f"expected one {ARTIFACT_ROOT}/{INVENTORY_NAME}, found none")
        return errors
    try:
        inventory = json.loads(inventory_entry[1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        errors.append(f"invalid {ARTIFACT_ROOT}/{INVENTORY_NAME}: {error}")
        return errors
    if not isinstance(inventory, dict):
        errors.append("inventory root is not an object")
        return errors

    root_fields = {
        "schema_version",
        "artifact_layout",
        "platform",
        "components",
        "native_components",
        "build_only",
        "resolved_runtime_not_packaged",
        "resolved_runtime",
        "required_documents",
        "generation",
    }
    missing_root = root_fields - inventory.keys()
    extra_root = inventory.keys() - root_fields
    for field in sorted(missing_root):
        errors.append(f"inventory root is missing field {field}")
    for field in sorted(extra_root):
        errors.append(f"inventory root has unexpected field {field}")
    schema_version = inventory.get("schema_version")
    if type(schema_version) is not int or schema_version != 2:
        errors.append(f"unsupported inventory schema version: {schema_version!r}")
    if inventory.get("artifact_layout") != ARTIFACT_LAYOUT:
        errors.append("inventory artifact_layout is invalid")

    def object_field(
        value: object, label: str, fields: set[str]
    ) -> dict[str, object] | None:
        if not isinstance(value, dict):
            errors.append(f"{label} is not an object")
            return None
        for field in sorted(fields - value.keys()):
            errors.append(f"{label} is missing field {field}")
        for field in sorted(value.keys() - fields):
            errors.append(f"{label} has unexpected field {field}")
        return value

    def string_field(value: object, label: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label} is not a non-empty string")
            return None
        return value

    def string_list(
        value: object, label: str, *, allow_empty: bool = False
    ) -> list[str]:
        if not isinstance(value, list):
            errors.append(f"{label} is not a list")
            return []
        result: list[str] = []
        for index, item in enumerate(value):
            validated = string_field(item, f"{label}[{index}]")
            if validated is not None:
                result.append(validated)
        if not allow_empty and not result:
            errors.append(f"{label} is empty")
        if len(set(result)) != len(result):
            errors.append(f"{label} contains duplicate values")
        return result

    platform_value = object_field(
        inventory.get("platform"), "inventory platform", {"system", "machine"}
    )
    system = None
    if platform_value is not None:
        system = string_field(platform_value.get("system"), "inventory platform.system")
        string_field(platform_value.get("machine"), "inventory platform.machine")
        if system not in {"Windows", "Linux"}:
            errors.append(f"inventory platform.system is unsupported: {system!r}")
    executable = (
        "SDC-Testing-Toolbox.exe" if system == "Windows" else "SDC-Testing-Toolbox"
    )
    executable_bytes = archive_file(executable)
    if executable_bytes is None:
        errors.append(
            f"archive is missing expected platform executable file {executable}"
        )
        embedded_modules: set[str] = set()
    else:
        try:
            embedded_modules = _pyinstaller_modules(executable_bytes)
        except (
            EOFError,
            ImportError,
            OverflowError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            struct.error,
            zlib.error,
        ):
            embedded_modules = set()
        if not embedded_modules:
            errors.append(
                "platform executable has no readable PyInstaller module table"
            )

    required_documents = string_list(
        inventory.get("required_documents"), "inventory required_documents"
    )
    if required_documents != list(REQUIRED_DOCUMENTS):
        errors.append(
            "inventory required_documents does not exactly match code-owned requirements"
        )
    for relative in REQUIRED_DOCUMENTS:
        if archive_file(relative) is None:
            errors.append(f"missing required legal payload: {relative}")

    generation = object_field(
        inventory.get("generation"),
        "inventory generation",
        {"method", "runtime_roots", "build_roots"},
    )
    if generation is not None:
        string_field(generation.get("method"), "inventory generation.method")
        runtime_roots = string_list(
            generation.get("runtime_roots"), "inventory generation.runtime_roots"
        )
        build_roots = string_list(
            generation.get("build_roots"), "inventory generation.build_roots"
        )
        if runtime_roots != ["PySide6", "sdc11073"]:
            errors.append(
                "inventory generation.runtime_roots is not the code-owned root set"
            )
        if build_roots != ["PyInstaller", "pyinstaller-hooks-contrib"]:
            errors.append(
                "inventory generation.build_roots is not the code-owned root set"
            )

    component_fields = {
        "name",
        "version",
        "scope",
        "license_expression",
        "project_url",
        "source_archive_urls",
        "notice_paths",
        "evidence",
    }
    native_fields = component_fields | {"notice_sha256"}
    component_evidence_fields = {"modules", "artifact_paths"}
    native_evidence_fields = {"artifact_paths", "artifact_patterns", "owner"}

    def safe_relative_paths(
        value: object, label: str, *, allow_empty: bool = False
    ) -> list[str]:
        result = string_list(value, label, allow_empty=allow_empty)
        valid: list[str] = []
        for path in result:
            parsed = _archive_path(path)
            if parsed is None or parsed[0] != path:
                errors.append(
                    f"{label} contains unsafe or non-normalized path: {path!r}"
                )
            else:
                valid.append(path)
        return valid

    def validate_notices(component: dict[str, object], label: str) -> list[str]:
        notices = safe_relative_paths(
            component.get("notice_paths"), f"{label}.notice_paths"
        )
        for notice in notices:
            if not notice.startswith(f"{LEGAL_ROOT}/"):
                errors.append(f"{label}: notice path is outside {LEGAL_ROOT}: {notice}")
            if archive_file(notice) is None:
                errors.append(f"{label}: missing bundled notice {notice}")
        return notices

    def validate_urls(component: dict[str, object], label: str) -> list[str]:
        project_url = string_field(component.get("project_url"), f"{label}.project_url")
        if project_url is not None and not project_url.startswith(
            ("https://", "http://")
        ):
            errors.append(f"{label}.project_url is not an HTTP(S) URL")
        urls = string_list(
            component.get("source_archive_urls"),
            f"{label}.source_archive_urls",
            allow_empty=True,
        )
        if any(not url.startswith("https://") for url in urls):
            errors.append(f"{label}.source_archive_urls contains a non-HTTPS URL")
        expression = component.get("license_expression")
        if (
            isinstance(expression, str)
            and any(token in expression for token in ("GPL", "LGPL"))
            and not urls
        ):
            errors.append(f"{label}: no versioned corresponding-source archive URL")
        return urls

    def validate_artifact_paths(
        value: object, label: str, *, allow_empty: bool = False
    ) -> list[str]:
        paths = safe_relative_paths(
            value, f"{label}.artifact_paths", allow_empty=allow_empty
        )
        for artifact_path in paths:
            if (
                artifact_path
                == "SDC-Testing-Toolbox executable bootloader and embedded loader"
            ):
                if archive_file(executable) is None:
                    errors.append(f"{label}: PyInstaller carrier executable is absent")
                continue
            if (
                archive_file(artifact_path) is None
                and archive_file(f"_internal/{artifact_path}") is None
            ):
                errors.append(
                    f"{label}: inventoried artifact path is absent: {artifact_path}"
                )
        return paths

    component_values = inventory.get("components")
    if not isinstance(component_values, list):
        errors.append("inventory components is not a list")
        component_values = []
    elif not component_values:
        errors.append("inventory components is empty")
    components: list[dict[str, object]] = []
    component_names: list[str] = []
    for index, value in enumerate(component_values):
        component = object_field(
            value, f"inventory components[{index}]", component_fields
        )
        if component is None:
            continue
        components.append(component)
        raw_name = string_field(
            component.get("name"), f"inventory components[{index}].name"
        )
        label = raw_name or f"inventory components[{index}]"
        if raw_name is not None:
            component_names.append(raw_name)
        for field in ("version", "scope", "license_expression"):
            string_field(component.get(field), f"{label}.{field}")
        validate_urls(component, label)
        notices = validate_notices(component, label)
        evidence = object_field(
            component.get("evidence"), f"{label}.evidence", component_evidence_fields
        )
        modules: list[str] = []
        artifact_paths: list[str] = []
        if evidence is not None:
            modules = string_list(
                evidence.get("modules"), f"{label}.evidence.modules", allow_empty=True
            )
            artifact_paths = validate_artifact_paths(
                evidence.get("artifact_paths"), label, allow_empty=True
            )
            if not modules and not artifact_paths:
                errors.append(f"{label}: no artifact/module-graph evidence")
        if raw_name is not None and modules:
            prefixes = _MODULE_PREFIXES.get(_canonicalize_name(raw_name))
            mapped_modules = {
                module
                for module in modules
                if prefixes
                and any(
                    module == prefix or module.startswith(f"{prefix}.")
                    for prefix in prefixes
                )
            }
            if not mapped_modules:
                errors.append(
                    f"{label}: module evidence has no code-owned component mapping"
                )
            elif not artifact_paths and not (mapped_modules & embedded_modules):
                errors.append(
                    f"{label}: module evidence has no corresponding executable archive entry"
                )
        elif raw_name is not None and not artifact_paths:
            prefixes = _MODULE_PREFIXES.get(_canonicalize_name(raw_name))
            if not prefixes or not any(
                module == prefix or module.startswith(f"{prefix}.")
                for module in embedded_modules
                for prefix in prefixes
            ):
                errors.append(
                    f"{label}: no corresponding artifact or executable module evidence"
                )
        rules = _CORE_NOTICE_RULES.get(raw_name or "")
        if rules:
            expected_paths = [path for path, _phrases in rules]
            if notices != expected_paths:
                errors.append(
                    f"{label}: missing or misassigned component-specific legal notice"
                )
            for path, phrases in rules:
                content = archive_file(path)
                if content is not None:
                    text = content.decode("utf-8", errors="replace")
                    if not all(phrase in text for phrase in phrases):
                        errors.append(
                            f"{label}: component-specific legal notice has unexpected content: {path}"
                        )

    if len(set(component_names)) != len(component_names):
        errors.append("inventory components contains duplicate names")
    expected_component_names = {
        "aiohappyeyeballs",
        "aiohttp",
        "aiosignal",
        "attrs",
        "frozenlist",
        "idna",
        "ifaddr",
        "lxml",
        "multidict",
        "propcache",
        "pyinstaller",
        "PySide6",
        "PySide6_Essentials",
        "Python",
        "Qt",
        "sdc11073",
        "shiboken6",
        "typing_extensions",
        "yarl",
    }
    missing_components = expected_component_names - set(component_names)
    unexpected_components = set(component_names) - expected_component_names
    for name in sorted(missing_components):
        errors.append(f"inventory is missing expected runtime component {name}")
    for name in sorted(unexpected_components):
        errors.append(f"inventory contains unexpected runtime component {name}")

    classification_fields = {"name", "version", "reason"}

    def classifications(field: str) -> list[dict[str, object]]:
        value = inventory.get(field)
        if not isinstance(value, list):
            errors.append(f"inventory {field} is not a list")
            return []
        result = []
        for index, item in enumerate(value):
            entry = object_field(
                item, f"inventory {field}[{index}]", classification_fields
            )
            if entry is None:
                continue
            for key in classification_fields:
                string_field(entry.get(key), f"inventory {field}[{index}].{key}")
            result.append(entry)
        names = [
            entry["name"] for entry in result if isinstance(entry.get("name"), str)
        ]
        if len(set(names)) != len(names):
            errors.append(f"inventory {field} contains duplicate names")
        return result

    build_only = classifications("build_only")
    resolved_not_packaged = classifications("resolved_runtime_not_packaged")
    classified_names = {
        entry.get("name")
        for entry in build_only + resolved_not_packaged
        if isinstance(entry.get("name"), str)
    }
    for expected in ("pyinstaller-hooks-contrib", "PySide6_Addons"):
        if expected not in classified_names:
            errors.append(
                f"inventory does not classify expected unbundled distribution {expected}"
            )

    resolved_fields = {"name", "version"}
    resolved_value = inventory.get("resolved_runtime")
    resolved_names: list[str] = []
    if not isinstance(resolved_value, list):
        errors.append("inventory resolved_runtime is not a list")
    elif not resolved_value:
        errors.append("inventory resolved_runtime is empty")
    else:
        for index, item in enumerate(resolved_value):
            entry = object_field(
                item, f"inventory resolved_runtime[{index}]", resolved_fields
            )
            if entry is None:
                continue
            name = string_field(
                entry.get("name"), f"inventory resolved_runtime[{index}].name"
            )
            string_field(
                entry.get("version"), f"inventory resolved_runtime[{index}].version"
            )
            if name is not None:
                resolved_names.append(name)
    if len(set(resolved_names)) != len(resolved_names):
        errors.append("inventory resolved_runtime contains duplicate names")
    expected_runtime = {
        "aiohappyeyeballs",
        "aiohttp",
        "aiosignal",
        "attrs",
        "frozenlist",
        "idna",
        "ifaddr",
        "lxml",
        "multidict",
        "propcache",
        "PySide6",
        "PySide6_Addons",
        "PySide6_Essentials",
        "sdc11073",
        "shiboken6",
        "typing_extensions",
        "yarl",
    }
    if set(resolved_names) != expected_runtime or len(resolved_names) != len(
        expected_runtime
    ):
        errors.append(
            "inventory resolved_runtime does not match the code-owned dependency closure"
        )
    classified_runtime = {_canonicalize_name(name) for name in component_names} | {
        _canonicalize_name(str(entry["name"]))
        for entry in resolved_not_packaged
        if isinstance(entry.get("name"), str)
    }
    missing_runtime = {
        _canonicalize_name(name) for name in resolved_names
    } - classified_runtime
    if missing_runtime:
        errors.append(
            "resolved runtime dependencies are unclassified: "
            + ", ".join(sorted(missing_runtime))
        )

    native_values = inventory.get("native_components")
    if not isinstance(native_values, list):
        errors.append("inventory native_components is not a list")
        native_values = []
    native_names: list[str] = []
    native_identities: list[tuple[str, str]] = []
    for index, value in enumerate(native_values):
        component = object_field(
            value, f"inventory native_components[{index}]", native_fields
        )
        if component is None:
            continue
        raw_name = string_field(
            component.get("name"), f"inventory native_components[{index}].name"
        )
        label = raw_name or f"inventory native_components[{index}]"
        version = string_field(component.get("version"), f"{label}.version")
        if raw_name is not None:
            native_names.append(raw_name)
            if version is not None:
                native_identities.append((raw_name, version))
        for field in ("scope", "license_expression"):
            string_field(component.get(field), f"{label}.{field}")
        validate_urls(component, label)
        notices = validate_notices(component, label)
        evidence = object_field(
            component.get("evidence"), f"{label}.evidence", native_evidence_fields
        )
        owner: dict[str, object] = {}
        if evidence is not None:
            validate_artifact_paths(evidence.get("artifact_paths"), label)
            string_list(
                evidence.get("artifact_patterns"), f"{label}.evidence.artifact_patterns"
            )
            owner_value = evidence.get("owner")
            if not isinstance(owner_value, dict) or not owner_value:
                errors.append(f"{label}.evidence.owner is not a non-empty object")
            else:
                owner = owner_value
                for key, owner_item in owner.items():
                    if (
                        not isinstance(key, str)
                        or not key
                        or not isinstance(owner_item, str)
                        or not owner_item
                    ):
                        errors.append(
                            f"{label}.evidence.owner must contain non-empty string pairs"
                        )
                        break
        rules = _NATIVE_NOTICE_RULES.get(raw_name or "")
        package = owner.get("debian_binary_package")
        if rules is None and isinstance(package, str) and package:
            copyright_path = (
                f"{LEGAL_ROOT}/licenses/debian/{package.replace(':', '_')}/copyright"
            )
            rules = ((copyright_path, ("Format:", "License:")),)
        if rules is None:
            errors.append(f"{label}: no code-owned component-specific notice mapping")
        else:
            expected_paths = [path for path, _phrases in rules]
            if notices != expected_paths:
                errors.append(
                    f"{label}: missing or misassigned component-specific legal notice"
                )
            for path, phrases in rules:
                content = archive_file(path)
                if content is not None:
                    text = content.decode("utf-8", errors="replace")
                    if phrases and not all(phrase in text for phrase in phrases):
                        errors.append(
                            f"{label}: component-specific legal notice has unexpected content: {path}"
                        )
        hashes = component.get("notice_sha256")
        if not isinstance(hashes, dict):
            errors.append(f"{label}.notice_sha256 is not an object")
            hashes = {}
        elif set(hashes) != set(notices):
            errors.append(f"{label}.notice_sha256 keys do not match notice_paths")
        for notice in notices:
            expected_hash = hashes.get(notice)
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                errors.append(f"{label}: invalid bundled notice SHA-256: {notice}")
                continue
            content = archive_file(notice)
            if (
                content is not None
                and expected_hash != hashlib.sha256(content).hexdigest()
            ):
                errors.append(f"{label}: bundled notice SHA-256 mismatch: {notice}")

    if len(set(native_identities)) != len(native_identities):
        errors.append(
            "inventory native_components contains duplicate name/version entries"
        )
    expected_native: set[str] = set()
    archive_names = [
        name
        for name, _value in normalized.values()
        if name.startswith(f"{ARTIFACT_ROOT}/")
    ]
    lowered = [name.casefold() for name in archive_names]
    if system == "Windows":
        if any("/lxml/etree" in name and name.endswith(".pyd") for name in lowered):
            expected_native.update(
                f"lxml wheel: {name}"
                for name in ("zlib", "iconv", "libxml2", "libxslt", "libexslt")
            )
        if any(Path(name).name.startswith(("msvcp", "vcruntime")) for name in lowered):
            expected_native.add("Microsoft Visual C++ Runtime")
        if any(
            Path(name).name.startswith(("libcrypto-", "libssl-")) for name in lowered
        ):
            expected_native.add("OpenSSL")
        for plugin, component_name in {
            "qjpeg": "Qt image plugin: libjpeg-turbo",
            "qtiff": "Qt image plugin: libtiff",
            "qwebp": "Qt image plugin: libwebp",
        }.items():
            if any(
                re.search(rf"/(?:lib)?{plugin}\.(?:dll|so(?:\.\d+)*)$", name)
                for name in lowered
            ):
                expected_native.add(component_name)
        qt_carriers = [
            value
            for name, value in normalized.values()
            if Path(name).name.casefold()
            in {
                "qt6core.dll",
                "libqt6core.so",
                "libqt6core.so.6",
                "qtiff.dll",
                "libqtiff.so",
            }
        ]
        if any(b"1.3.2" in value and b"zlib" in value.lower() for value in qt_carriers):
            expected_native.add("Qt: zlib")
    for name in sorted(expected_native - set(native_names)):
        errors.append(f"inventory is missing expected native component {name}")
    if system == "Windows":
        for name in sorted(set(native_names) - set(_NATIVE_NOTICE_RULES)):
            errors.append(f"inventory contains unexpected native component {name}")

    qt = next(
        (component for component in components if component.get("name") == "Qt"), None
    )
    qt_evidence = qt.get("evidence") if qt else None
    qt_paths = (
        qt_evidence.get("artifact_paths") if isinstance(qt_evidence, dict) else None
    )
    if not isinstance(qt_paths, list) or not any(
        isinstance(path, str) and path.casefold().endswith((".dll", ".so", ".so.6"))
        for path in qt_paths
    ):
        errors.append("Qt inventory has no separately packaged shared library")
    if any("qt6pdf" in name or "virtualkeyboard" in name for name in lowered):
        errors.append(
            "artifact contains excluded unused Qt PDF or Virtual Keyboard payload"
        )
    return errors
