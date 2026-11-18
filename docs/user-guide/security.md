---
title: Security and TLS
parent: User guide
nav_order: 12
---

# Security and TLS
{: .no_toc }

1. TOC
{:toc}

By default the toolbox speaks plain `http://`, the smallest setup for a lab demonstration. Anyone
on the network can then read, subscribe to and control its device. For anything beyond a trusted
lab network, use TLS.

## Mutual TLS

Tick **Require TLS with client certificates** in the start dialog, or give all of `--tls-cert`,
`--tls-key` and `--tls-ca` on the command line. Then:

- every SDC service and every report callback uses HTTPS with **mutual** TLS: both sides present a
  certificate, and both check the other's;
- TLS 1.2 or newer is required, the server's host name or IP must be in its certificate, and the
  library's usual fallback from TLS to plain HTTP is refused;
- a peer the trust settings do not accept cannot read, subscribe or invoke anything.

Each participant needs its own PEM certificate and key, and must trust the CA that issued the
other participant's certificate.

An encrypted private key reads its password from the environment variable
`SDC_TOOLBOX_TLS_KEY_PASSWORD`; it is never accepted on the command line or saved. The start dialog
asks for it in a masked field instead.

## Making test certificates

```powershell
.venv\Scripts\python.exe helpers\tls\generate_certificates.py
```

It writes a local test CA and certificates for a provider and a consumer to
`helpers\tls\generated\` (ignored by Git), protects the keys with a password you choose, and writes
a `README.txt` with the two exact startup commands. Add `--ip <address>` when testing beyond
loopback, so the certificates name the address in use. See `helpers/tls/README.md` for the options.

A directly self-signed certificate also works, if the other participant trusts it explicitly.

## Options for lab setups

| Option | Start dialog field | Does |
|---|---|---|
| `--tls-peer-fingerprint` | | additionally pins one expected SHA-256 certificate. Checked on the connections this toolbox opens: as a consumer, and as a provider delivering reports. |
| `--tls-trusted-dir` | Trusted certificate folder | trusts every PEM certificate (`.pem`, `.crt`, `.cer`) in a folder, self-signed ones included. Mutual TLS stays in force. |
| `--tls-allow-self-signed` | Allow self-signed peer certificates | accepts any peer with a currently valid self-signed certificate. The provider then **stops requesting client certificates**, so it accepts any consumer. Use only on a network you trust. |
| `--tls-no-hostname-check` | Skip hostname/IP check | the certificate no longer has to name the host or IP; the chain is still checked |
| `--tls-server-name` | | a DNS name to advertise for the provider and its callbacks |

## What TLS does not cover

- **Discovery** stays unsecured UDP multicast. What a device announces is not proof of who it is;
  the HTTPS handshake and an optional fingerprint pin are the trust decision.
- **Per-operation permissions**: the library does not pass the client's certificate to the
  operation handler, so the toolbox cannot give different certificates different rights or record
  which certificate invoked an operation. This is
  [sdc11073 issue #490](https://github.com/Draegerwerk/sdc11073/issues/490).

The MCP server has its own rules; see [MCP](../automation/mcp.md#security).
