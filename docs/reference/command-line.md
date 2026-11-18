---
title: Command line
parent: Reference
nav_order: 1
---

# Command line

```text
run_toolbox.py [--name NAME] [--ip ADDRESS] [--config FILE] [--friendly-name TEXT]
               [--theme light|dark] [--fullscreen] [--verbose]
               [--mcp [PORT]] [--headless]
               [--tls-cert PEM --tls-key PEM --tls-ca PEM] [TLS options…]
```

The packaged builds take the same options: `SDC-Testing-Toolbox.exe --name alpha` on Windows,
`sdc-testing-toolbox --name alpha` after installing a Linux package.

**Without any option** the toolbox opens the start dialog, which asks for the same things with the
same defaults. **With any option** it starts at once, which is what you want for a shortcut or a
script.

## Device and display

| Option | Default | Means |
|---|---|---|
| `--name NAME` | `toolbox` | the instance name. It decides the device's EPR, so the same name keeps the same identity; two running toolboxes need different names. |
| `--ip ADDRESS` | `127.0.0.1` | the IPv4 address discovery binds to; see [Networking](../how-it-works/networking.md) |
| `--config FILE` | none | a preset, an MDIB file or an old JSON profile to start with, such as `presets/ventilator.xml` |
| `--friendly-name TEXT` | from the file, else `--name` | the name the device announces |
| `--theme light\|dark` | the system's | the colours to start with |
| `--fullscreen` | off | starts fullscreen; F11 restores the window |
| `--verbose` | off | shows the SDC library's logging |

## Agents

| Option | Means |
|---|---|
| `--mcp [PORT]` | serves [MCP](../automation/mcp.md) on 127.0.0.1, port 11073 unless given |
| `--headless` | runs without a window until Ctrl+C. Needs `--mcp`, or nothing could control it. |

## TLS

See [Security and TLS](../user-guide/security.md).

| Option | Means |
|---|---|
| `--tls-cert PEM` | this participant's certificate or certificate chain |
| `--tls-key PEM` | its private key. An encrypted key reads its password from `SDC_TOOLBOX_TLS_KEY_PASSWORD`. |
| `--tls-ca PEM` | the trusted CA certificates |
| `--tls-trusted-dir FOLDER` | trusts every PEM certificate in a folder, self-signed ones included |
| `--tls-peer-fingerprint SHA256` | pins one expected peer certificate |
| `--tls-server-name NAME` | a DNS name to advertise for the provider and its callbacks |
| `--tls-allow-self-signed` | accepts valid self-signed peer certificates (lab only; the provider then accepts any consumer) |
| `--tls-no-hostname-check` | does not require the peer certificate to name its host or IP |

## Examples

```powershell
.venv\Scripts\python.exe run_toolbox.py --name alpha
.venv\Scripts\python.exe run_toolbox.py --name beta --config presets\insufflator.xml
.venv\Scripts\python.exe run_toolbox.py --name monitor --ip 192.168.1.20 --theme dark --fullscreen
.venv\Scripts\python.exe run_toolbox.py --headless --mcp --config presets\ventilator.xml
```

Over SSH the window still needs a graphical desktop: set `DISPLAY` and its authorization, or use
graphical forwarding. `--ip` is an address of the machine the toolbox runs on.
