"""Build the Linux deb, rpm and AppImage from the PyInstaller bundle.

Run on Linux after `pyinstaller SDC-Testing-Toolbox.spec`:

    .venv/bin/python packaging/linux/build_packages.py

The packaging tools are downloaded once into build/packaging-tools and must match their
pinned SHA-256 sums.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import string
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGING_DIR = Path(__file__).resolve().parent
FORMATS = ("deb", "rpm", "appimage")


@dataclass(frozen=True)
class Tool:
    url: str
    sha256: str


TOOLS = {
    "nfpm": Tool(
        "https://github.com/goreleaser/nfpm/releases/download/v2.48.0/nfpm_2.48.0_Linux_x86_64.tar.gz",
        "6d7f9a28b2f397934403950fcbba83f598b08d02edbe79a0815b3b4897341750",
    ),
    "appimagetool": Tool(
        "https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage",
        "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0",
    ),
    "runtime": Tool(
        "https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64",
        "2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d",
    ),
}

# Canonical public PEP 440 only: N(.N)*[{a|b|rc}N][.postN][.devN].
_PEP440 = re.compile(
    r"(?P<release>\d+(?:\.\d+)*)(?:(?P<pre>a|b|rc)(?P<pre_number>\d+))?"
    r"(?:\.post(?P<post>\d+))?(?:\.dev(?P<dev>\d+))?"
)


def package_version(pep440: str) -> str:
    """Spell a PEP 440 version so dpkg and rpm order it the way pip does.

    "~" sorts before everything, even the end of the version, so pre-releases come before
    their final release. A dev build of a final release gets "~~" so it also sorts before
    that release's alphas.
    """
    match = _PEP440.fullmatch(pep440)
    if match is None:
        raise ValueError(f"not a canonical public PEP 440 version: {pep440!r}")
    text = match["release"]
    if match["pre"]:
        text += f"~{match['pre']}{match['pre_number']}"
    if match["post"] is not None:
        text += f"+post{match['post']}"
    if match["dev"] is not None:
        bare = not match["pre"] and match["post"] is None
        text += f"{'~~' if bare else '~'}dev{match['dev']}"
    return text


def project_version(root: Path) -> str:
    text = (root / "sdctoolbox" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if match is None:
        raise ValueError("sdctoolbox/__init__.py has no __version__")
    return match[1]


def render_nfpm_config(*, bundle: Path, packaging_dir: Path, version: str, release: str) -> str:
    template = string.Template((packaging_dir / "nfpm.yaml").read_text(encoding="utf-8"))
    return template.substitute(
        bundle=bundle.resolve().as_posix(),
        packaging=packaging_dir.resolve().as_posix(),
        version=version,
        release=release,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_tool(name: str, cache: Path) -> Path:
    """Download a pinned tool once, refuse it unless its SHA-256 matches, return its path."""
    tool = TOOLS[name]
    cache.mkdir(parents=True, exist_ok=True)
    download = cache / tool.url.rsplit("/", 1)[-1]
    if not download.is_file() or _sha256(download) != tool.sha256:
        partial = download.with_name(download.name + ".part")
        with urllib.request.urlopen(tool.url, timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        actual = _sha256(partial)
        if actual != tool.sha256:
            partial.unlink()
            raise ValueError(f"{name}: {tool.url} has SHA-256 {actual}, expected {tool.sha256}")
        partial.replace(download)
    path = download
    if name == "nfpm":
        path = cache / "nfpm"
        with tarfile.open(download) as archive:
            path.write_bytes(archive.extractfile("nfpm").read())
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def package_names(version: str, release: str) -> dict[str, str]:
    return {
        "deb": f"sdc-testing-toolbox_{version}-{release}_amd64.deb",
        "rpm": f"sdc-testing-toolbox-{version}-{release}.x86_64.rpm",
    }


_GLIBC_NEEDED = re.compile(rb"GLIBC_(\d+)\.(\d+)(?:\.\d+)?\x00")
_GLIBC_DECLARED = {
    "deb": re.compile(r"^\s+- libc6 \(>= (\d+)\.(\d+)\)$", re.MULTILINE),
    "rpm": re.compile(r"^\s+- libc\.so\.6\(GLIBC_(\d+)\.(\d+)\)\(64bit\)$", re.MULTILINE),
}


def glibc_floor(bundle: Path) -> tuple[int, int]:
    """The newest glibc symbol version any ELF file in the bundle names."""
    floor = (0, 0)
    for path in bundle.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
            data = stream.read()
        for match in _GLIBC_NEEDED.finditer(data):
            floor = max(floor, (int(match[1]), int(match[2])))
    return floor


def glibc_floor_problems(bundle: Path, nfpm_config: str) -> list[str]:
    """Each package whose declared glibc is older than the one the bundle needs.

    The floor moves with the build host, so a newer builder must raise the declaration too.
    """
    needed = glibc_floor(bundle)
    problems = []
    for packager, pattern in _GLIBC_DECLARED.items():
        match = pattern.search(nfpm_config)
        declared = (int(match[1]), int(match[2])) if match else (0, 0)
        if declared < needed:
            problems.append(
                f"{packager}: the bundle needs glibc {needed[0]}.{needed[1]} but nfpm.yaml "
                f"declares {declared[0]}.{declared[1]}; raise its libc dependency"
            )
    return problems


def appimage_name(pep440: str, release: str) -> str:
    return f"SDC-Testing-Toolbox-{pep440}-{release}-x86_64.AppImage"


def build_appdir(bundle: Path, packaging_dir: Path, appdir: Path) -> None:
    """Lay out an AppDir: launcher, desktop entry and icon on top, the bundle under usr/lib."""
    desktop = packaging_dir / "sdc-testing-toolbox.desktop"
    icon = packaging_dir / "sdc-testing-toolbox.svg"
    share = appdir / "usr" / "share"
    firewall = share / "sdc-testing-toolbox" / "firewall"
    files = [
        (packaging_dir / "AppRun", appdir / "AppRun", 0o755),
        (desktop, appdir / desktop.name, 0o644),
        (icon, appdir / icon.name, 0o644),
        (desktop, share / "applications" / desktop.name, 0o644),
        (icon, share / "icons" / "hicolor" / "scalable" / "apps" / icon.name, 0o644),
        (
            packaging_dir / "firewalld" / "sdc-testing-toolbox.xml",
            firewall / "firewalld" / "sdc-testing-toolbox.xml",
            0o644,
        ),
        (
            packaging_dir / "ufw" / "sdc-testing-toolbox",
            firewall / "ufw" / "sdc-testing-toolbox",
            0o644,
        ),
    ]
    for source, destination, mode in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(mode)
    shutil.copytree(bundle, appdir / "usr" / "lib" / "sdc-testing-toolbox", symlinks=True)


def build_appimage(*, bundle: Path, target: Path, tools_dir: Path) -> None:
    appimagetool = fetch_tool("appimagetool", tools_dir)
    runtime = fetch_tool("runtime", tools_dir)
    # appimagetool is itself an AppImage; extract-and-run spares the build host FUSE.
    environment = {**os.environ, "ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"}
    with tempfile.TemporaryDirectory() as temp:
        appdir = Path(temp) / "SDC-Testing-Toolbox.AppDir"
        build_appdir(bundle, PACKAGING_DIR, appdir)
        command = [
            str(appimagetool),
            "--no-appstream",
            "--runtime-file",
            str(runtime),
            str(appdir),
            str(target),
        ]
        subprocess.run(command, check=True, env=environment)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Linux packages from the bundle.")
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist" / "SDC-Testing-Toolbox")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--release", default=os.environ.get("GITHUB_RUN_NUMBER") or "1")
    parser.add_argument("--formats", nargs="+", choices=FORMATS, default=list(FORMATS))
    parser.add_argument("--tools-dir", type=Path, default=ROOT / "build" / "packaging-tools")
    args = parser.parse_args(argv)

    if not sys.platform.startswith("linux"):
        print("error: the Linux packages can only be built on Linux", file=sys.stderr)
        return 2
    if not (args.bundle / "SDC-Testing-Toolbox").is_file():
        print(f"error: no PyInstaller bundle at {args.bundle}", file=sys.stderr)
        return 2

    version = package_version(project_version(ROOT))
    names = package_names(version, args.release)
    args.out.mkdir(parents=True, exist_ok=True)
    packagers = [name for name in args.formats if name in names]
    if packagers:
        nfpm_config = render_nfpm_config(
            bundle=args.bundle,
            packaging_dir=PACKAGING_DIR,
            version=version,
            release=args.release,
        )
        problems = glibc_floor_problems(args.bundle, nfpm_config)
        if problems:
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            return 1
        nfpm = fetch_tool("nfpm", args.tools_dir)
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "nfpm.yaml"
            config.write_text(nfpm_config, encoding="utf-8")
            for packager in packagers:
                target = args.out / names[packager]
                command = [str(nfpm), "package", "-f", str(config), "-p", packager, "-t", str(target)]
                subprocess.run(command, check=True)
                print(target)
    if "appimage" in args.formats:
        target = args.out / appimage_name(project_version(ROOT), args.release)
        build_appimage(bundle=args.bundle, target=target, tools_dir=args.tools_dir)
        print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
