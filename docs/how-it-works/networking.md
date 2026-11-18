---
title: Networking
parent: How it works
nav_order: 5
---

# Networking
{: .no_toc }

1. TOC
{:toc}

## One address

WS-Discovery in the library binds to **one IPv4 address**. A normal computer has several: Ethernet,
Wi-Fi, a VPN, Hyper-V or WSL adapters, Bluetooth. So the toolbox asks which one to use: the start
dialog lists them with their adapter names, usable ones first, and marks link-local addresses
(169.254.x.x), which reach nothing. On the command line it is `--ip`.

The default is `127.0.0.1` (loopback), so two toolboxes on one computer find each other without
involving the network at all. To reach another computer, both must use addresses on a network they
share.

## Ports

| Port | Protocol | Used for |
|---|---|---|
| 3702 | UDP | WS-Discovery multicast (Hello, Probe, Bye) |
| 21073–21172 | TCP | the provider's SDC services and each consumer's report receiver |
| 21073–21172 | UDP | the reply socket of discovery |

The library would bind its servers to any free port; `ports.py` binds them inside 21073–21172
instead, so a firewall can be opened for a fixed range. Each provider and consumer takes the first
free port. If every port in the range is taken, the toolbox falls back to a port the operating
system picks, logs a warning such as *TCP port 40312 is outside 21073-21172 because no port in that
range was free*, and shows the latest one in the status bar. Peers behind a firewall cannot reach
that port; close other toolbox instances or open it.

For the Linux packages' firewall definitions, see [Installation](../getting-started/install.md#opening-the-firewall-on-linux).

## Identity

| Name | Where it comes from |
|---|---|
| EPR | `urn:uuid:` plus a UUID derived from the instance name, so it survives restarts |
| Friendly name | `--friendly-name`, else the preset's stored name, else the instance name |
| Serial number | the MDIB's `MetaData` if it names one, else derived from the instance name |
| Location scope | the location context; changing it re-announces the device |

Two toolboxes must not run under the same name at the same time: they would share an EPR.

## HTTP and HTTPS

Without TLS every SOAP message is plain HTTP, readable by anyone on the network. With TLS both
sides authenticate with certificates; see [Security and TLS](../user-guide/security.md). Discovery
stays unencrypted either way, as WS-Discovery defines it.

## Checking the network itself

`diagnostics/` holds two scripts that use none of the toolbox's own code, straight from the
library's tutorial. If these two cannot see each other, the problem is the network, not the
toolbox:

```powershell
.venv\Scripts\python.exe diagnostics\minimal_provider.py --ip 127.0.0.1   # terminal 1
.venv\Scripts\python.exe diagnostics\minimal_consumer.py --ip 127.0.0.1   # terminal 2
```
