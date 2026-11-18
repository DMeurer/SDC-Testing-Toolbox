---
title: Text console
parent: Automation
nav_order: 2
---

# Text console
{: .no_toc }

1. TOC
{:toc}

`examples/console.py` is the toolbox in a terminal, in two modes. Open a terminal for each:

```powershell
.venv\Scripts\python.exe examples\console.py provider --name alpha
.venv\Scripts\python.exe examples\console.py consumer
```

| Option | Means |
|---|---|
| `provider` / `consumer` | which shell to start |
| `--ip` | the address discovery uses, `127.0.0.1` by default |
| `--name` | the provider's name, which decides its EPR |
| `--config` | a preset or MDIB file to load at startup (provider) |
| `--verbose` | show the SDC library's logging |

Type `help` for the command list and `help <command>` for one command. `quit` ends the shell.

The console runs from the source code; the packaged builds do not include it.

## The provider shell

```
provider> add number Zoom level 1..100
provider> add choice Mode IDLE RUN PAUSE
provider> add waveform Pleth 0..100 --shape pulse --cycle 1
provider> set m.zoom_level 5
provider> list
```

| Command | Does |
|---|---|
| `add <kind> <label…> [min..max]` | adds a data source; `--shape` and `--cycle` for waveforms, `shapes` lists the shapes |
| `set <handle> <value>` | changes a value |
| `control <handle> on\|off` | allows or refuses remote writes |
| `remove <handle>` | removes a data source or an action |
| `list` | the data sources, their values and whether they can be written |
| `samples`, `generator` | sets a sample array's samples, starts or stops the generator |
| `alert <source> <label…> [min..max] [--delegable]` | adds an alarm with a visual and an audible signal |
| `alerts`, `raise`, `clear`, `ack`, `delegate` | the alarm table and its buttons |
| `action <label…> handle=value … [--target HANDLE]` | adds an action that sets values when it runs |
| `actions`, `run <handle>` | lists the actions, runs one |
| `where`, `patient` | shows or sets the location and the patient |
| `fault <handle> activation=… validity=… mode=…` | a [fault](../user-guide/advanced-mode.md) on one metric |
| `fault device answer_delay_ms=… refuse_writes=on …` | device-wide faults |
| `faults`, `faults reset` | shows the faults, ends them all |
| `check`, `fix <handle> [fix]` | the [MDIB check](../user-guide/mdib-check.md) and its fixes |
| `codes <text> [--kind K]` | searches the MDC codes |
| `presets`, `import <file>`, `export [--plain] <file>` | presets and MDIB files |
| `tool [name] [JSON]` | runs any [MCP tool](mcp.md#the-tools); `tool` alone lists them |

`tool` closes every gap: whatever an agent can do to My device can be typed here.

```
provider> tool update_metric {"handle": "m.zoom_level", "unit": {"code": "262656", "system": "mdc"}}
provider> tool set_contexts {"patient": {"given_name": "Ada"}}
```

### Watching an acknowledgement not clear an alarm

```
provider> alert m.pressure Pressure high 0..30 --delegable
created al.pressure_high  (outside 0 to 30)
  signals: sig.pressure_high.vis, sig.pressure_high.aud
provider> set m.pressure 80
provider> alerts
  handle                 watches            when                 state    signals
  al.pressure_high       m.pressure         outside 0 to 30      PRESENT  Vis:On Aud:On
provider> ack al.pressure_high
acknowledged 2 signal(s); al.pressure_high is still present
provider> alerts
  handle                 watches            when                 state    signals
  al.pressure_high       m.pressure         outside 0 to 30      PRESENT  Vis:Ack Aud:Ack
```

## The consumer shell

```
consumer> scan
  [0] urn:uuid:053b9f8f-0aa5-5290-8797-351f901ebd74
      sdc.ctxt.loc:/sdc.ctxt.loc.detail/HOSP///CU1//Toolbox?fac=HOSP&poc=CU1&bed=Toolbox
consumer> connect 0
connection 1: urn:uuid:053b9f8f-0aa5-5290-8797-351f901ebd74
consumer> set m.zoom_level 500
refused by the provider (Fail) - Oth: 500 is above the maximum 100 of 'm.zoom_level'
consumer> test
```

| Command | Does |
|---|---|
| `scan [seconds]` | looks for providers |
| `connect <index>` | connects to a device from the last scan; earlier connections stay open |
| `connections`, `use <n>`, `disconnect [n]` | lists the connections (`*` marks the current one), switches, closes one |
| `list` | the current device's metrics, their ranges and whether they can be written |
| `set <handle> <value>` | asks the device to change a value and prints its answer |
| `watch [seconds]` | prints changes as they arrive |
| `alerts`, `patient` | the device's alarms and patient |
| `actions`, `run <handle>` | the device's actions; runs one |
| `check` | the MDIB check on the device |
| `test` | the [Device test](../user-guide/device-test.md) |
| `messages [count] [--kind request\|response\|report] [--xml]` | the newest SOAP messages |
| `message <number>` | one message's XML |
| `codes <text>` | searches the MDC codes |

Commands act on the current connection.
