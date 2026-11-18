---
title: Installation
parent: Getting started
nav_order: 1
---

# Installation
{: .no_toc }

1. TOC
{:toc}

There are three ways to get the toolbox: a ready-made build for Windows, a package or AppImage for
Linux, or the source code with Python. The builds need no Python and bring everything they use.

## Windows

1. Download `SDC-Testing-Toolbox-…-windows-x86_64.zip` from the
   [releases](https://github.com/DMeurer/SDC-Testing-Toolbox/releases).
2. Unzip it anywhere. Keep the folder `SDC-Testing-Toolbox` together: it holds the program, the
   libraries it loads, the presets and the license texts.
3. Start `SDC-Testing-Toolbox.exe`.

The build is not signed, so Windows may warn about an unknown publisher the first time. When
Windows asks whether the program may use the network, allow it for the networks you test on;
without that, other devices cannot find or reach it. See [Networking](../how-it-works/networking.md)
for the ports it uses.

## Linux

The releases have a `.deb` (Debian, Ubuntu), an `.rpm` (Fedora, openSUSE, AlmaLinux) and an
AppImage that runs on most x86-64 distributions with glibc 2.35 or newer. Each one is installed
and started on clean Ubuntu 22.04, Ubuntu 24.04, Debian 12, Fedora, openSUSE Tumbleweed and
AlmaLinux 10 before a release.

Replace `<version>` and `<release>` with the values in the file name:

```bash
# Debian and Ubuntu
sudo apt install ./sdc-testing-toolbox_<version>-<release>_amd64.deb

# Fedora and AlmaLinux
sudo dnf install ./sdc-testing-toolbox-<version>-<release>.x86_64.rpm

# openSUSE
sudo zypper install --allow-unsigned-rpm ./sdc-testing-toolbox-<version>-<release>.x86_64.rpm
```

The packages install the program to `/opt/sdc-testing-toolbox`, add the command
`sdc-testing-toolbox` and a menu entry. To remove it again, use `sudo apt purge
sdc-testing-toolbox`, `sudo dnf remove sdc-testing-toolbox` or `sudo zypper remove
sdc-testing-toolbox`.

The AppImage needs no installation:

```bash
chmod +x SDC-Testing-Toolbox-<version>-<release>-x86_64.AppImage
./SDC-Testing-Toolbox-<version>-<release>-x86_64.AppImage
```

Without FUSE, for example in a container, pass `--appimage-extract-and-run` as the first argument.

### Opening the firewall on Linux

The packages do not change your firewall. They install a firewalld service and a ufw application
profile that you switch on yourself:

```bash
# firewalld (Fedora, openSUSE, AlmaLinux)
sudo firewall-cmd --reload
sudo firewall-cmd --permanent --add-service=sdc-testing-toolbox
sudo firewall-cmd --reload

# ufw (Ubuntu, Debian)
sudo ufw allow SDC-Testing-Toolbox
```

Before uninstalling, remove a ufw rule with `sudo ufw delete allow SDC-Testing-Toolbox`; the
package cannot do it for you. The AppImage cannot install these files; copy them from
`packaging/linux/firewalld/` and `packaging/linux/ufw/` in the repository, or from the AppImage
(`--appimage-extract` puts them in `squashfs-root/usr/share/sdc-testing-toolbox/firewall/`).

## From source

You need **Python 3.12** and Git.

```powershell
git clone https://github.com/DMeurer/SDC-Testing-Toolbox.git
cd SDC-Testing-Toolbox
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run_toolbox.py
```

On Linux, use `python3.12 -m venv .venv` and `.venv/bin/python` instead. Ubuntu 22.04's own
Python 3.10 is too old. Qt needs a few desktop libraries; the list is in
[Development](../reference/development.md#building-the-application).

## Checking that it works

Start the toolbox, press **Start** in the window that asks how to start, and the main window
opens with **My device** and **Network** tabs. The status bar at the bottom says *Publishing on
127.0.0.1* and the device's identity. That is all; [Your first device](first-steps.md) goes on from
here.

If something does not work, see [Troubleshooting](../reference/troubleshooting.md).
