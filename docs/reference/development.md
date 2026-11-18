---
title: Development
parent: Reference
nav_order: 5
---

# Development
{: .no_toc }

Building, testing and releasing the toolbox.

1. TOC
{:toc}

## Setting up

```powershell
git clone https://github.com/DMeurer/SDC-Testing-Toolbox.git
cd SDC-Testing-Toolbox
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
```

Python 3.12 is the tested interpreter. `requirements.txt` and `requirements-build.txt` pin the
direct dependencies only.

## Running the tests

Every suite is a plain script. `tests/suites.json` lists them with the platforms and events they run
on, and `tests/run_suites.py` runs the default set that CI runs:

```powershell
.venv\Scripts\python.exe tests\run_suites.py
```

Run one suite directly; Qt suites need the offscreen platform:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python.exe tests\gui_dialogs.py
```

| Kind | Examples |
|---|---|
| Core | `provider_core.py` (descriptors, values, alarms, contexts, rollback), `config_roundtrip.py`, `mdib_roundtrip.py`, `presets.py` (every preset as a working device), `mdib_check.py`, `nomenclature.py` |
| Window | `gui_dialogs.py`, `gui_layout.py`, `gui_cards_plots.py`, `widget_controls.py`, `gui_redesign.py`, `multi_provider.py`, `consumer_lifecycle.py` |
| Automation | `mcp_server.py`, `console_commands.py` |
| Network | `mdib_network.py`, `connection_loss.py`, `operation_subscription.py`, `passive_discovery.py`, `acceptance_core.py`, `tls_acceptance.py` (Linux only; separate CI jobs) |
| Broad regression | `gui_smoke.py`: opens every window once against a live peer; its own CI job |
| Policy | `licensing.py`, `workflow_security.py`, `diagnostics/check_api.py` |

## Building the application

PyInstaller makes a standalone build. Build Windows on Windows and Linux on Linux.

**Windows:**

```powershell
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm SDC-Testing-Toolbox.spec
.\dist\SDC-Testing-Toolbox\SDC-Testing-Toolbox.exe --name packaged-smoke --ip 127.0.0.1 --smoke-test
Compress-Archive -Path dist\SDC-Testing-Toolbox -DestinationPath dist\SDC-Testing-Toolbox-windows-x86_64.zip
```

The folder `dist\SDC-Testing-Toolbox` must stay together: it holds the shared libraries, the
license texts, `THIRD_PARTY_NOTICES.md` and a `DEPENDENCY_INVENTORY.json` made for that build.

**Linux** (Ubuntu 22.04 shown; the build inherits its host's glibc):

```bash
sudo apt-get install -y binutils libbrotli1 libdbus-1-3 libegl1 libfontconfig1 libfreetype6 libgl1 \
  libglib2.0-0 libgtk-3-0 libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-randr0 libxcb-render-util0 libxcb-render0 libxcb-shape0 libxcb-shm0 libxcb-sync1 \
  libxcb-xfixes0 libxcb-xkb1 libxkbcommon-x11-0 libxkbcommon0
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
.venv/bin/python -m PyInstaller --clean --noconfirm SDC-Testing-Toolbox.spec
tar -C dist -czf dist/SDC-Testing-Toolbox-linux-x86_64.tar.gz SDC-Testing-Toolbox
.venv/bin/python packaging/linux/build_packages.py
```

The last step makes the `.deb`, `.rpm` and AppImage. It downloads pinned packaging tools to
`build/packaging-tools` and refuses any download whose SHA-256 does not match.

Check the license payload of a finished archive:

```powershell
.venv\Scripts\python.exe tests\licensing.py --artifact dist\SDC-Testing-Toolbox-windows-x86_64.zip
```

## Continuous integration

The *Build application* workflow runs for pull requests to `develop`, version tags, a weekly
schedule and by hand. It runs the test suites on Linux and Windows, the network and GUI smoke
suites, both builds with a smoke test of the built program, a license check of every artifact, and
installs each Linux package on six distributions. The artifacts are kept for 90 days.

## Releases

The version is `__version__` in `sdctoolbox/__init__.py`; the builds and the Linux packages read it
there. A release goes through a `release/<version>` branch with the version bump, merged into
`main` and back into `develop`, and a tag `v<version>` on `main`, which starts the build.

## This documentation

The site is the `docs` folder, built by GitHub Pages; `docs/README.md` explains how to preview it.
After a GUI change, take the screenshots again:

```powershell
.venv\Scripts\python.exe helpers\docs_screenshots.py
```

## License

The toolbox is under the GNU General Public License version 3 only. Its dependencies keep their own
terms: sdc11073 is MIT, PySide6 and Qt are available under LGPLv3, GPL or commercial terms
depending on the component. Binary releases must also publish the
corresponding source defined in `legal/SOURCE_OFFER.md`. See
[Third-Party Notices](https://github.com/DMeurer/SDC-Testing-Toolbox/blob/main/THIRD_PARTY_NOTICES.md).
