"""The files the Linux packages install agree with the application and its port range."""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from gui_test_support import application
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

import run_toolbox
import sdctoolbox
from sdctoolbox import constants

PACKAGING = ROOT / "packaging" / "linux"
sys.path.insert(0, str(PACKAGING))
import build_packages  # noqa: E402

FIRST, LAST = constants.PORT_RANGE[0], constants.PORT_RANGE[-1]
INSTALL_LAYOUT = (
    "/opt/sdc-testing-toolbox",
    "/usr/bin/sdc-testing-toolbox",
    "/usr/share/applications/sdc-testing-toolbox.desktop",
    "/usr/share/icons/hicolor/scalable/apps/sdc-testing-toolbox.svg",
    "/usr/lib/firewalld/services/sdc-testing-toolbox.xml",
    "/etc/ufw/applications.d/sdc-testing-toolbox",
    "/usr/share/doc/sdc-testing-toolbox/copyright",
    "/usr/share/licenses/sdc-testing-toolbox/LICENSE",
)


def nfpm_depends(text: str) -> dict[str, list[str]]:
    depends: dict[str, list[str]] = {}
    lines = text.splitlines()
    for line in lines[lines.index("overrides:") + 1 :]:
        if packager := re.fullmatch(r"  (\w+):", line):
            current = depends.setdefault(packager.group(1), [])
        elif item := re.fullmatch(r"      - (.+)", line):
            current.append(item.group(1).strip("'\""))
    return depends


def read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


class LinuxPackagingFiles(unittest.TestCase):
    def test_desktop_entry(self) -> None:
        entry = read_ini(PACKAGING / "sdc-testing-toolbox.desktop")["Desktop Entry"]
        expected = {
            "Type": "Application",
            "Name": "SDC Testing Toolbox",
            "Exec": "sdc-testing-toolbox",
            "Icon": constants.DESKTOP_ID,
            "Terminal": "false",
            "Categories": "Development;Network;",
            "StartupWMClass": constants.APP_NAME,
        }
        self.assertEqual({key: entry.get(key) for key in expected}, expected)
        self.assertIn("IEEE 11073 SDC", entry.get("Comment", ""))

    def test_firewalld_service_ports(self) -> None:
        root = ET.parse(PACKAGING / "firewalld" / "sdc-testing-toolbox.xml").getroot()
        ports = {(port.get("protocol"), port.get("port")) for port in root.iter("port")}
        self.assertEqual(
            ports,
            {("udp", "3702"), ("udp", f"{FIRST}-{LAST}"), ("tcp", f"{FIRST}-{LAST}")},
        )
        self.assertIsNone(root.find("source-port"))
        self.assertEqual(root.findtext("short"), "SDC Testing Toolbox")

    def test_ufw_profile_ports(self) -> None:
        profile = read_ini(PACKAGING / "ufw" / "sdc-testing-toolbox")["SDC-Testing-Toolbox"]
        self.assertEqual(profile["title"], "SDC Testing Toolbox")
        self.assertTrue(profile["description"])
        self.assertEqual(
            profile["ports"], f"3702/udp|{FIRST}:{LAST}/udp|{FIRST}:{LAST}/tcp"
        )

    def test_icon_is_square_svg(self) -> None:
        root = ET.parse(PACKAGING / "sdc-testing-toolbox.svg").getroot()
        self.assertTrue(root.tag.endswith("svg"))
        _x, _y, width, height = root.get("viewBox").split()
        self.assertEqual(width, height)

    def test_apprun_quotes_its_paths(self) -> None:
        text = (PACKAGING / "AppRun").read_bytes().decode("utf-8")
        self.assertTrue(text.startswith("#!/bin/sh\n"))
        self.assertNotIn("\r", text)
        self.assertIn('"$here/usr/lib/sdc-testing-toolbox/SDC-Testing-Toolbox" "$@"', text)

    def test_application_identity(self) -> None:
        application()
        run_toolbox.configure_application_identity()
        self.assertEqual(QGuiApplication.desktopFileName(), "sdc-testing-toolbox")
        self.assertEqual(QCoreApplication.applicationName(), "SDC-Testing-Toolbox")


class PackageBuild(unittest.TestCase):
    def test_package_version(self) -> None:
        cases = {
            "0.1.0.dev0": "0.1.0~~dev0",
            "0.1.0": "0.1.0",
            "1.2.0a2": "1.2.0~a2",
            "1.2.0rc1": "1.2.0~rc1",
            "1.2.0rc1.dev3": "1.2.0~rc1~dev3",
            "1.2.0.post1": "1.2.0+post1",
            "1.2.0.post1.dev2": "1.2.0+post1~dev2",
        }
        for pep440, expected in cases.items():
            with self.subTest(pep440):
                self.assertEqual(build_packages.package_version(pep440), expected)
        with self.assertRaises(ValueError):
            build_packages.package_version("1.2.0+local")

    @unittest.skipUnless(shutil.which("dpkg"), "needs dpkg")
    def test_package_versions_sort_like_pep440(self) -> None:
        ordered = [
            "1.2.0.dev1",
            "1.2.0a1",
            "1.2.0b1",
            "1.2.0rc1.dev3",
            "1.2.0rc1",
            "1.2.0",
            "1.2.0.post1.dev2",
            "1.2.0.post1",
        ]
        converted = [build_packages.package_version(v) for v in ordered]
        for lower, higher in zip(converted, converted[1:]):
            with self.subTest(f"{lower} < {higher}"):
                result = subprocess.run(["dpkg", "--compare-versions", lower, "lt", higher])
                self.assertEqual(result.returncode, 0)

    def test_project_version(self) -> None:
        self.assertEqual(build_packages.project_version(ROOT), sdctoolbox.__version__)

    def test_render_nfpm_config(self) -> None:
        text = build_packages.render_nfpm_config(
            bundle=Path("/build/SDC-Testing-Toolbox"),
            packaging_dir=PACKAGING,
            version="0.1.0~~dev0",
            release="7",
        )
        self.assertNotIn("${", text)
        for expected in (
            "name: sdc-testing-toolbox",
            "version: 0.1.0~~dev0",
            'release: "7"',
            "license: GPL-3.0-only",
            "Dominik Meurer <50107525+DMeurer@users.noreply.github.com>",
            "config|noreplace",
            *INSTALL_LAYOUT,
        ):
            self.assertIn(expected, text)
        self.assertEqual(text.count("compression: xz"), 2)
        # Only a pre-remove script, which keeps firewalld zones valid after uninstall.
        self.assertIn(f"preremove: {PACKAGING.resolve().as_posix()}/preremove.sh", text)
        self.assertNotRegex(text, r"(preinstall|postinstall|postremove):")

    def test_nfpm_declares_measured_dependencies(self) -> None:
        text = build_packages.render_nfpm_config(
            bundle=Path("/build/SDC-Testing-Toolbox"),
            packaging_dir=PACKAGING,
            version="0.1.0",
            release="1",
        )
        depends = nfpm_depends(text)
        self.assertIn("libstdc++6", depends["deb"])
        self.assertIn("libstdc++.so.6()(64bit)", depends["rpm"])
        self.assertTrue(depends["deb"])
        self.assertEqual(len(depends["deb"]), len(depends["rpm"]))
        for capability in depends["rpm"]:
            self.assertRegex(capability, r"^\S+\.so\.[\d.]+\(\S*\)\(64bit\)$")

    def test_fetch_tool_verifies_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "runtime-x86_64"
            source.write_bytes(b"runtime bytes")
            digest = hashlib.sha256(b"runtime bytes").hexdigest()
            wrong = {"runtime": build_packages.Tool(source.as_uri(), "0" * 64)}
            with patch.dict(build_packages.TOOLS, wrong):
                with self.assertRaisesRegex(ValueError, "runtime"):
                    build_packages.fetch_tool("runtime", Path(temp) / "cache-bad")
            right = {"runtime": build_packages.Tool(source.as_uri(), digest)}
            with patch.dict(build_packages.TOOLS, right):
                fetched = build_packages.fetch_tool("runtime", Path(temp) / "cache")
            self.assertEqual(fetched.read_bytes(), b"runtime bytes")

    def test_package_names(self) -> None:
        self.assertEqual(
            build_packages.package_names("0.1.0~~dev0", "7"),
            {
                "deb": "sdc-testing-toolbox_0.1.0~~dev0-7_amd64.deb",
                "rpm": "sdc-testing-toolbox-0.1.0~~dev0-7.x86_64.rpm",
            },
        )

    def test_glibc_floor_is_the_newest_version_an_elf_file_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = fake_bundle(Path(temp) / "bundle")
            (bundle / "_internal" / "liba.so").write_bytes(
                b"\x7fELF\x00GLIBC_2.2.5\x00GLIBC_2.35\x00GLIBC_PRIVATE\x00"
            )
            (bundle / "_internal" / "libb.so").write_bytes(b"\x7fELF\x00GLIBC_2.17\x00")
            (bundle / "notes.txt").write_bytes(b"GLIBC_2.99\x00")
            self.assertEqual(build_packages.glibc_floor(bundle), (2, 35))

    def test_declared_glibc_floor_covers_the_bundle(self) -> None:
        config = build_packages.render_nfpm_config(
            bundle=Path("/build/SDC-Testing-Toolbox"),
            packaging_dir=PACKAGING,
            version="0.1.0",
            release="1",
        )
        with tempfile.TemporaryDirectory() as temp:
            bundle = fake_bundle(Path(temp) / "bundle")
            # libpython and libcairo from the Ubuntu 22.04 builder need hypot@GLIBC_2.35.
            library = bundle / "_internal" / "libpython.so"
            library.write_bytes(b"\x7fELF\x00GLIBC_2.35\x00")
            self.assertEqual(build_packages.glibc_floor_problems(bundle, config), [])
            library.write_bytes(b"\x7fELF\x00GLIBC_2.99\x00")
            problems = build_packages.glibc_floor_problems(bundle, config)
        self.assertEqual([problem.split(":")[0] for problem in problems], ["deb", "rpm"])
        self.assertTrue(all("2.99" in problem for problem in problems))

    def test_main_refuses_a_too_low_glibc_floor_before_fetching_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = fake_bundle(Path(temp) / "bundle")
            (bundle / "_internal" / "libx.so").write_bytes(b"\x7fELF\x00GLIBC_2.99\x00")
            arguments = ["--bundle", str(bundle), "--out", str(Path(temp) / "out")]
            with (
                patch.object(build_packages.sys, "platform", "linux"),
                patch.object(build_packages, "fetch_tool", side_effect=AssertionError),
                patch("sys.stderr"),
            ):
                self.assertEqual(build_packages.main([*arguments, "--formats", "deb", "rpm"]), 1)


def fake_bundle(path: Path, executable: str = "binary") -> Path:
    (path / "_internal").mkdir(parents=True)
    (path / "SDC-Testing-Toolbox").write_bytes(executable.encode())
    (path / "LICENSE").write_bytes(b"license text")
    (path / "_internal" / "libx.so").write_bytes(b"\x7fELF library")
    return path


class AppImageBuild(unittest.TestCase):
    def test_build_appdir_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = fake_bundle(Path(temp) / "bundle")
            appdir = Path(temp) / "AppDir"
            build_packages.build_appdir(bundle, PACKAGING, appdir)
            for relative in (
                "AppRun",
                "sdc-testing-toolbox.desktop",
                "sdc-testing-toolbox.svg",
                "usr/share/applications/sdc-testing-toolbox.desktop",
                "usr/share/icons/hicolor/scalable/apps/sdc-testing-toolbox.svg",
                "usr/share/sdc-testing-toolbox/firewall/firewalld/sdc-testing-toolbox.xml",
                "usr/share/sdc-testing-toolbox/firewall/ufw/sdc-testing-toolbox",
            ):
                self.assertTrue((appdir / relative).is_file(), relative)
            installed = appdir / "usr" / "lib" / "sdc-testing-toolbox"
            for relative in ("SDC-Testing-Toolbox", "LICENSE", "_internal/libx.so"):
                self.assertEqual(
                    (installed / relative).read_bytes(), (bundle / relative).read_bytes()
                )
            if os.name == "posix":
                self.assertTrue((appdir / "AppRun").stat().st_mode & 0o111)

    def test_appimage_name(self) -> None:
        self.assertEqual(
            build_packages.appimage_name("0.1.0.dev0", "7"),
            "SDC-Testing-Toolbox-0.1.0.dev0-7-x86_64.AppImage",
        )

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "needs a POSIX sh")
    def test_apprun_handles_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            script = '#!/bin/sh\nfor argument in "$@"; do echo "$argument"; done\n'
            bundle = fake_bundle(Path(temp) / "bundle", executable=script)
            (bundle / "SDC-Testing-Toolbox").chmod(0o755)
            appdir = Path(temp) / "with space" / "AppDir"
            build_packages.build_appdir(bundle, PACKAGING, appdir)
            environment = {key: value for key, value in os.environ.items() if key != "APPDIR"}
            result = subprocess.run(
                [str(appdir / "AppRun"), "a b", "c"],
                capture_output=True,
                text=True,
                env=environment,
                check=True,
            )
            self.assertEqual(result.stdout.splitlines(), ["a b", "c"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
