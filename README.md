# SDC Testing Toolbox

A desktop tool for learning and testing **IEEE 11073 SDC**. Build an SDC device in a few clicks,
publish it on the network, find other SDC devices, subscribe to their data and remote-control them.
Built on [sdc11073](https://github.com/Draegerwerk/sdc11073) and PySide6.

**Documentation: [sdc.d-meurer.com](https://sdc.d-meurer.com)**

> [!WARNING]
> This is a learning and test tool, not a medical device. Its SDC library, `sdc11073`, is not
> meant for clinical trials, clinical studies or clinical routine use.

## Installation

### Ready-made builds

Download a build from the [releases](https://github.com/DMeurer/SDC-Testing-Toolbox/releases):

- **Windows:** unzip `SDC-Testing-Toolbox-…-windows-x86_64.zip` and start `SDC-Testing-Toolbox.exe`.
- **Linux:** a `.deb`, an `.rpm` or an AppImage, for x86-64 with glibc 2.35 or newer.

The [installation guide](https://sdc.d-meurer.com/getting-started/install.html) covers each
package and the firewall ports.

### From source

You need Python 3.12.

```powershell
git clone https://github.com/DMeurer/SDC-Testing-Toolbox.git
cd SDC-Testing-Toolbox
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On Linux, use `python3.12 -m venv .venv` and `.venv/bin/python`.

## Usage

```powershell
.venv\Scripts\python.exe run_toolbox.py
```

With no options, a start dialog asks for everything. Common options:

- `--name`
- `--ip`
- `--config`
- `--theme`
- `--fullscreen`
- `--mcp`
- `--headless`
- the `--tls-…` options

All of them are explained in [Command line](https://sdc.d-meurer.com/reference/command-line.html).

```powershell
.venv\Scripts\python.exe run_toolbox.py --name alpha --ip 127.0.0.1 --config presets\ventilator.xml
```

More tools in the repository:

| Path | What it is |
|---|---|
| `examples/console.py` | the [text console](https://sdc.d-meurer.com/automation/console.html), `provider` or `consumer` |
| `helpers/tls/generate_certificates.py` | a local test CA and certificates for [TLS](https://sdc.d-meurer.com/user-guide/security.html) |
| `helpers/Coding Systems/` | turn your copy of IEEE 11073-10101 or NIST RTMMS into a `codes.csv` ([MDC codes](https://sdc.d-meurer.com/user-guide/mdc-codes.html)) |
| `diagnostics/` | checks of the library and the network, without the toolbox ([Troubleshooting](https://sdc.d-meurer.com/reference/troubleshooting.html)) |

## Development

Install the build tools as well:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
```

**Tests.** Run the set that CI runs:

```powershell
.venv\Scripts\python.exe tests\run_suites.py
```

Or run one suite on its own. Qt suites need the offscreen platform:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python.exe tests\gui_dialogs.py
```

**Builds.** Build Windows on Windows and Linux on Linux:

```powershell
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm SDC-Testing-Toolbox.spec
```

On Linux, `packaging/linux/build_packages.py` then makes the `.deb`, `.rpm` and AppImage.

[Development](https://sdc.d-meurer.com/reference/development.html) has the details:

- the Linux system libraries
- archives and license checks
- CI and the release steps

## License

[GPL-3.0-only](LICENSE). Dependencies keep their own licenses; see
[Third-Party Notices](THIRD_PARTY_NOTICES.md).
