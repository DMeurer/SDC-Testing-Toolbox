"""Generate and validate the legal payload for a PyInstaller bundle."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
from collections import defaultdict, deque
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path

INVENTORY_NAME = "DEPENDENCY_INVENTORY.json"
LEGAL_ROOT = "legal"
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
)

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


def filter_binaries(binaries: Iterable[object]) -> list[object]:
    """Drop Windows host libraries and machine-local PATH contamination."""
    if platform.system() != "Windows":
        return list(binaries)
    installed_base = _resolved(sysconfig.get_config_var("installed_base"))
    environment_root = _resolved(sys.prefix)
    windows_root = _resolved(os.environ["WINDIR"])
    result = []
    for entry in binaries:
        destination, source_text = _entry_parts(entry)
        normalized_destination = destination.replace("\\", "/").lower()
        if normalized_destination.endswith(
            (
                "pyside6/qt6pdf.dll",
                "pyside6/qt6pdf.so",
                "pyside6/qt6virtualkeyboard.dll",
                "pyside6/qt6virtualkeyboard.so",
                "pyside6/plugins/imageformats/qpdf.dll",
                "pyside6/plugins/imageformats/libqpdf.so",
                "pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
                "pyside6/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.so",
            )
        ):
            continue
        source = _resolved(source_text)
        if source.is_relative_to(windows_root):
            continue
        if source.is_relative_to(installed_base) or source.is_relative_to(environment_root):
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
    value = distribution.metadata.get("License-Expression") or distribution.metadata.get("License")
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
        marker = next(index for index, part in enumerate(parts) if part.lower() in {"licenses", "license_files"})
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
            destination = f"{LEGAL_ROOT}/licenses/{name}/{Path(relative).name}-{len(used) + 1}"
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
        f"{LEGAL_ROOT}/licenses/qt/LGPL-3.0-only.txt": project_root / LEGAL_ROOT / "licenses" / "qt" / "LGPL-3.0-only.txt",
        f"{LEGAL_ROOT}/licenses/qt/GPL-3.0-only.txt": project_root / "LICENSE",
        f"{LEGAL_ROOT}/licenses/qt/Qt-GPL-exception-1.0.txt": project_root / LEGAL_ROOT / "licenses" / "qt" / "Qt-GPL-exception-1.0.txt",
        f"{LEGAL_ROOT}/licenses/openssl/Apache-2.0.txt": project_root / LEGAL_ROOT / "licenses" / "openssl" / "Apache-2.0.txt",
    }
    for destination, source in documents.items():
        if not source.is_file():
            raise RuntimeError(f"required legal source is missing: {source}")
        target = output / Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return sorted(documents)


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
            raise RuntimeError(f"cannot assign packaged Linux library to a Debian package: {source}")
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
            raise RuntimeError(f"Debian package has no machine-readable notice file: {package}")
        copyright_text = copyright_file.read_text(encoding="utf-8", errors="replace")
        licenses = sorted(set(re.findall(r"^License:\s*([^\n]+)", copyright_text, re.MULTILINE)))
        if not licenses:
            raise RuntimeError(f"Debian copyright file declares no license: {copyright_file}")
        notice = f"{LEGAL_ROOT}/licenses/debian/{package.replace(':', '_')}/copyright"
        target = output / Path(notice)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(copyright_file, target)
        notice_paths = [notice]
        for common in sorted(set(re.findall(r"/usr/share/common-licenses/([A-Za-z0-9.+-]+)", copyright_text))):
            source = Path("/usr/share/common-licenses") / common
            if source.is_file():
                destination = f"{LEGAL_ROOT}/licenses/debian/common/{common}"
                common_target = output / Path(destination)
                common_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, common_target)
                notice_paths.append(destination)
        source_name = source_package or package.split(":", 1)[0]
        components.append(
            {
                "name": f"Debian package {package}",
                "version": version,
                "scope": "runtime-native-library",
                "license_expression": " AND ".join(licenses),
                "project_url": homepage or f"https://packages.ubuntu.com/search?keywords={source_name}",
                "source_archive_urls": [f"https://packages.ubuntu.com/source/jammy/{source_name}"],
                "notice_paths": notice_paths,
                "evidence": {"modules": [], "artifact_paths": sorted(owners[package])},
            }
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
    build_closure = _dependency_closure(("PyInstaller", "pyinstaller-hooks-contrib"), distributions)

    modules: dict[str, set[str]] = defaultdict(set)
    artifact_paths: dict[str, set[str]] = defaultdict(set)
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
        if not matched and source.is_relative_to(installed_base) and "site-packages" not in {
            part.lower() for part in source.parts
        }:
            modules["python"].add(module)

    for collection, is_binary in ((binaries, True), (datas, False)):
        for entry in collection:
            destination, source_text = _entry_parts(entry)
            source = _resolved(source_text)
            matched = owners.get(source, set())
            if not matched and "site-packages" in {part.lower() for part in source.parts}:
                unmatched_site_packages.append(str(source))
            for name in matched:
                artifact_paths[name].add(destination.replace("\\", "/"))
            if not matched and source.is_relative_to(installed_base):
                artifact_paths["python"].add(destination.replace("\\", "/"))
            elif not matched and is_binary and platform.system() == "Linux":
                native_paths[source].add(destination.replace("\\", "/"))

    if unmatched_site_packages:
        details = "\n".join(sorted(set(unmatched_site_packages))[:20])
        raise RuntimeError(f"cannot assign packaged site-packages files to distributions:\n{details}")

    packaged = set(modules) | set(artifact_paths)
    packaged.discard("python")
    # PyInstaller's bootloader and loader are embedded after Analysis and therefore
    # cannot be attributed from an Analysis TOC like ordinary distributions.
    packaged.add("pyinstaller")
    components: list[dict[str, object]] = []
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
                    if name in {"pyside6", "pyside6-addons", "pyside6-essentials", "shiboken6"}
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
                    or (["SDC-Testing-Toolbox executable bootloader and embedded loader"] if name == "pyinstaller" else []),
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
            "version": platform.python_version(),
            "scope": "runtime",
            "license_expression": "PSF-2.0",
            "project_url": f"https://www.python.org/downloads/release/python-{sys.version_info.major}{sys.version_info.minor}{sys.version_info.micro}/",
            "source_archive_urls": [
                (
                    f"https://www.python.org/ftp/python/{platform.python_version()}/"
                    f"Python-{platform.python_version()}.tar.xz"
                )
            ],
            "notice_paths": [python_notice],
            "evidence": {
                "modules": sorted(modules["python"]),
                "artifact_paths": sorted(artifact_paths["python"]),
            },
        }
    )

    if platform.system() == "Windows":
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
            components.append(
                {
                    "name": "Microsoft Visual C++ Runtime",
                    "version": version,
                    "scope": "runtime-native-library",
                    "license_expression": "LicenseRef-Microsoft-Visual-Cpp-Runtime-2015-2022",
                    "project_url": "https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/",
                    "source_archive_urls": [],
                    "notice_paths": [python_notice],
                    "evidence": {"modules": [], "artifact_paths": sorted(paths)},
                }
            )
        for version, paths in sorted(openssl_paths.items()):
            components.append(
                {
                    "name": "OpenSSL",
                    "version": version,
                    "scope": "runtime-native-library",
                    "license_expression": "Apache-2.0",
                    "project_url": "https://github.com/openssl/openssl",
                    "source_archive_urls": [
                        (
                            f"https://github.com/openssl/openssl/releases/download/"
                            f"openssl-{version}/openssl-{version}.tar.gz"
                        )
                    ],
                    "notice_paths": [f"{LEGAL_ROOT}/licenses/openssl/Apache-2.0.txt"],
                    "evidence": {"modules": [], "artifact_paths": sorted(paths)},
                }
            )

    components.extend(_debian_native_components(native_paths, output))

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
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "components": sorted(components, key=lambda item: str(item["name"]).lower()),
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
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

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


def validate_payload(files: dict[str, bytes]) -> list[str]:
    """Return validation errors for files extracted from an artifact."""
    errors: list[str] = []
    normalized = {name.replace("\\", "/").lstrip("./"): value for name, value in files.items()}
    inventory_candidates = [name for name in normalized if name.endswith(f"/{INVENTORY_NAME}") or name == INVENTORY_NAME]
    if len(inventory_candidates) != 1:
        return [f"expected one {INVENTORY_NAME}, found {len(inventory_candidates)}"]
    inventory_path = inventory_candidates[0]
    prefix = inventory_path[: -len(INVENTORY_NAME)]
    try:
        inventory = json.loads(normalized[inventory_path].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return [f"invalid {inventory_path}: {error}"]

    if inventory.get("schema_version") != 2:
        errors.append(f"unsupported inventory schema version: {inventory.get('schema_version')!r}")

    def present(relative: str) -> bool:
        return prefix + relative in normalized

    for relative in inventory.get("required_documents", []):
        if not present(relative):
            errors.append(f"missing required legal payload: {relative}")
    components = inventory.get("components")
    if not isinstance(components, list) or not components:
        errors.append("inventory has no runtime components")
        return errors
    required_fields = (
        "name",
        "version",
        "scope",
        "license_expression",
        "project_url",
        "notice_paths",
        "evidence",
    )
    for component in components:
        label = component.get("name", "<unnamed>") if isinstance(component, dict) else "<invalid>"
        if not isinstance(component, dict):
            errors.append("inventory component is not an object")
            continue
        for field in required_fields:
            if not component.get(field):
                errors.append(f"{label}: missing {field}")
        for notice in component.get("notice_paths", []):
            if not present(notice):
                errors.append(f"{label}: missing bundled notice {notice}")
        evidence = component.get("evidence", {})
        if not evidence.get("modules") and not evidence.get("artifact_paths"):
            errors.append(f"{label}: no artifact/module-graph evidence")
        for artifact_path in evidence.get("artifact_paths", []):
            if " " in artifact_path and not Path(artifact_path).suffix:
                continue
            candidates = (prefix + artifact_path, prefix + "_internal/" + artifact_path)
            if not any(candidate in normalized for candidate in candidates):
                errors.append(f"{label}: inventoried artifact path is absent: {artifact_path}")
        if any(token in str(component.get("license_expression")) for token in ("GPL", "LGPL")):
            source_urls = component.get("source_archive_urls")
            if not source_urls or not all(str(url).startswith("https://") for url in source_urls):
                errors.append(f"{label}: no versioned corresponding-source archive URL")
    names = {component.get("name") for component in components if isinstance(component, dict)}
    expected_components = ["Python", "Qt", "PySide6_Essentials", "sdc11073"]
    if inventory.get("platform", {}).get("system") == "Windows":
        expected_components.extend(("Microsoft Visual C++ Runtime", "OpenSSL"))
    for expected in expected_components:
        if expected not in names:
            errors.append(f"inventory is missing expected runtime component {expected}")
    build_only = inventory.get("build_only")
    if not isinstance(build_only, list):
        errors.append("inventory has no build-only classification")
    resolved_not_packaged = inventory.get("resolved_runtime_not_packaged")
    if not isinstance(resolved_not_packaged, list):
        errors.append("inventory has no resolved-runtime-not-packaged classification")
    classified = {
        item.get("name")
        for item in (build_only or []) + (resolved_not_packaged or [])
        if isinstance(item, dict)
    }
    for expected in ("pyinstaller-hooks-contrib", "PySide6_Addons"):
        if expected not in classified:
            errors.append(f"inventory does not classify expected unbundled distribution {expected}")
    resolved_runtime = inventory.get("resolved_runtime")
    if not isinstance(resolved_runtime, list) or not resolved_runtime:
        errors.append("inventory has no resolved runtime dependency closure")
    else:
        resolved_names = {
            _canonicalize_name(item.get("name", ""))
            for item in resolved_runtime
            if isinstance(item, dict) and item.get("name") and item.get("version")
        }
        classified_runtime = {
            _canonicalize_name(component.get("name", ""))
            for component in components
            if isinstance(component, dict)
        } | {
            _canonicalize_name(item.get("name", ""))
            for item in (resolved_not_packaged or [])
            if isinstance(item, dict)
        }
        missing_runtime = resolved_names - classified_runtime
        if missing_runtime:
            errors.append(
                "resolved runtime dependencies are unclassified: "
                + ", ".join(sorted(missing_runtime))
            )
    native_notices = {
        "OpenSSL": f"{LEGAL_ROOT}/licenses/openssl/Apache-2.0.txt",
    }
    for component in components:
        expected_notice = native_notices.get(component.get("name"))
        if expected_notice and expected_notice not in component.get("notice_paths", []):
            errors.append(f"{component['name']}: missing component-specific legal notice")
    qt = next((component for component in components if component.get("name") == "Qt"), None)
    if qt and not any(
        str(path).lower().endswith((".dll", ".so", ".so.6"))
        for path in qt.get("evidence", {}).get("artifact_paths", [])
    ):
        errors.append("Qt inventory has no separately packaged shared library")
    if any(
        "qt6pdf" in name.lower() or "virtualkeyboard" in name.lower()
        for name in normalized
    ):
        errors.append("artifact contains excluded unused Qt PDF or Virtual Keyboard payload")
    return errors
