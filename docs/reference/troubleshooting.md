---
title: Troubleshooting
parent: Reference
nav_order: 4
---

# Troubleshooting
{: .no_toc }

1. TOC
{:toc}

## Scan finds nothing

1. **Same address family?** Both toolboxes must use addresses on a network they share. With both
   on one computer, use `127.0.0.1` for both.
2. **A link-local address?** An address 169.254.x.x reaches nothing. Pick another one in the start
   dialog.
3. **Firewall?** Discovery needs UDP 3702, and the SDC services TCP and UDP 21073–21172. On
   Windows, allow the toolbox when asked; on Linux, see
   [Opening the firewall](../getting-started/install.md#opening-the-firewall-on-linux).
4. **The network or the toolbox?** Run the two diagnostics scripts, which use none of the
   toolbox's code:

   ```powershell
   .venv\Scripts\python.exe diagnostics\minimal_provider.py --ip 127.0.0.1   # terminal 1
   .venv\Scripts\python.exe diagnostics\minimal_consumer.py --ip 127.0.0.1   # terminal 2
   ```

   If they cannot see each other, the problem is the network.

## Connecting fails or the connection is lost

- **A TLS device with a plain toolbox, or the other way round**: both sides must use TLS, and each
  must trust the other's certificate. The discovery list shows *HTTP* or *HTTPS* for each device.
- **The device restarted**: a new MDIB means a new connection. Connect again.
- **A paused or slow consumer** is dropped by the provider after 2 seconds without accepting a
  report. Connect again.
- With `--verbose`, the SDC library's log says what failed.

## A write is refused

The status bar shows the device's own answer. Common reasons:

- the value is outside the allowed range, or not one of the allowed values;
- the device has remote control switched off for it (the operation is disabled);
- the device simulates *Refuse every write* (see [Advanced mode](../user-guide/advanced-mode.md)).

## A port outside 21073–21172

The status bar shows a warning such as *TCP port 40312 is outside 21073-21172*. Every port in the
range is taken, usually by other toolbox instances. Close some, or open that port in the firewall.

## Two devices with the same identity

Two toolboxes started with the same `--name` share an EPR, and other devices cannot tell them
apart. Give each its own name.

## Loading a file fails

The message names what is wrong. Typical causes: the file has separate description and state
parts (only combined MDIBs load), it declares a DTD or entities (refused for safety), a reference
points at a handle that does not exist, or a duration uses days (`P1D`) instead of hours, minutes
and seconds (`PT24H`).

## The MCP server does not start

*Could not start the MCP server* means the port is taken, often by another toolbox with `--mcp`.
Use another port: `--mcp 12000`.

## Is the library still what the toolbox expects?

```powershell
.venv\Scripts\python.exe diagnostics\check_api.py
```

checks that the installed sdc11073 still has every part of its API the toolbox uses.
