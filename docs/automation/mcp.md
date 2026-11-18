---
title: MCP for agents
parent: Automation
nav_order: 1
---

# MCP for agents
{: .no_toc }

1. TOC
{:toc}

With `--mcp`, the toolbox serves the [Model Context Protocol](https://modelcontextprotocol.io), so
an AI agent such as Claude Code can drive it: build and change My device, check it, connect to
other devices, read and write them, test them, and explain what the window shows. It makes the
start easier ("make me an infusion pump with an occlusion alarm", "what does the crossed-out
speaker mean?") and turns writing an MDIB into describing the device.

## Starting it

```powershell
# with the window, on port 11073
.venv\Scripts\python.exe run_toolbox.py --mcp

# another port, with a preset
.venv\Scripts\python.exe run_toolbox.py --mcp 12000 --config presets\ventilator.xml

# without a window, until Ctrl+C
.venv\Scripts\python.exe run_toolbox.py --headless --mcp
```

The packaged builds take the same options: `SDC-Testing-Toolbox.exe --mcp`. The status bar shows
the address (*MCP on …*).

Then tell the agent where it is. For Claude Code:

```powershell
claude mcp add --transport http sdc-toolbox http://127.0.0.1:11073/mcp
```

Any MCP client that speaks *streamable HTTP* works the same way.

## With and without a window

With a window, every change an agent makes goes the same way a click does, so the window follows
it: cards appear, alarms light up, connections open as Network tabs, and each change is listed in
the activity log as *MCP: …*. Slow network work, such as a scan or a remote write, never freezes
the window.

Without a window (`--headless`, which needs `--mcp`), the same tools work, except the ones about the
window.

## The tools

| Area | Tools |
|---|---|
| Getting started | `overview`, `explain_gui` (panels, columns, buttons, icons, card controls, menus), `explain_sdc` (the SDC idea behind them) |
| My device | `search_codes` (MDC codes by name), `list_metrics`, `add_metric`, `update_metric`, `set_value`, `remove_metric`; `list_alarms`, `add_alarm`, `update_alarm` (limits, priority, raise, acknowledge, stop latched, delegate), `remove_alarm`; `list_actions`, `add_action`, `run_action`, `remove_action`; `set_contexts`, `set_generator` |
| Faults | `get_faults`, `set_metric_fault`, `set_device_faults`, `reset_faults` |
| Whole devices and MDIB | `build_device` (a whole device in one validated step), `check_mdib` (My device or a connection), `fix_mdib`, `get_mdib`, `export_mdib`, `load_mdib`, `list_presets`, `load_preset` |
| Network | `scan`, `connect`, `list_connections`, `disconnect`, `read_metrics`, `read_alarms`, `read_actions`, `remote_set_value`, `remote_run_action`, `remote_mdib`, `test_device`, `read_messages` |
| The window | `get_view`, `set_view` (tab, split view, cards or tables, dark mode, Advanced mode, activity log), `get_card_settings`, `set_card_settings`, `read_activity_log` |

The guide behind `explain_gui` is also offered as MCP resources, `toolbox://guide/…`.

A connection is addressed by its number, the *N* of its *Tab N* chip in the window.

## Shapes

Data sources, alarms and actions use the same shape as a toolbox profile, so an agent's input gets
the same checks a loaded file gets. Numbers may be JSON numbers or decimal strings; they are read
as decimals, never as floating point.

**A data source** (`add_metric`):

```json
{
  "label": "Pulse rate",
  "kind": "number",
  "type": {"code": "149514", "system": "mdc", "label": "Pulse rate"},
  "unit": {"code": "264864", "system": "mdc", "label": "bpm"},
  "minimum": 0, "maximum": 300, "resolution": 1,
  "controllable": true,
  "initial_value": 72
}
```

`kind` is `number`, `text`, `choice`, `waveform` or `distribution`. A choice takes
`allowed_values`; a waveform `sample_period`, `shape` and `cycle_seconds`; a distribution
`domain_unit`, `domain_minimum`, `domain_maximum` and `distribution_shape`. `section` names a
channel and `handle` sets a stable id. `search_codes` returns `{code, system, label}` ready to use
as `type` or `unit`.

**An alarm** (`add_alarm`):

```json
{
  "label": "Pulse high", "watches": "m.pulse_rate",
  "kind": "Phy", "priority": "Hi", "upper_limit": 140,
  "signals": [{"manifestation": "Vis"}, {"manifestation": "Aud", "latching": true}],
  "delegable": false
}
```

**An action** (`add_action`):

```json
{"label": "Reset", "target": "mds0", "effects": {"m.mode": "IDLE"}, "note": "back to idle"}
```

**A whole device** (`build_device`) is `{name, description, metrics: [...], alerts: [...],
actions: [...], contexts: {location, patient}}`. It is checked completely before anything changes;
`{}` gives an empty device.

## A typical session

1. `overview`: what is running and where to start.
2. `build_device` or `add_metric` / `add_alarm` / `add_action`.
3. `check_mdib`, then `fix_mdib` for the fixes it offers.
4. `export_mdib` to save the result.
5. `scan`, `connect`, `test_device` and `read_messages` against another device.

## Security

The server listens on `127.0.0.1` only and refuses requests from web pages (a foreign `Origin`).
There is **no authentication**: any program on this computer can control the device while `--mcp`
runs.

The server is built on Python's standard library and speaks MCP over streamable HTTP with plain
JSON answers, so the packaged builds need no extra dependency.
