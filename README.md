# SDC-testing-toolbox

A desktop tool for understanding **IEEE 11073 SDC**: create data sources with a few clicks, publish them on the network, discover other SDC devices, subscribe to their data sources and remote-control them.

Built on [sdc11073](https://github.com/Draegerwerk/sdc11073) (Draeger, MIT) and PySide6.

> [!WARNING]
> Learning tool, not a medical device. By its own notice `sdc11073` is not intended for
> clinical trials, clinical studies or clinical routine use, and was not developed according
> to ISO 9001.

## Setup

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python 3.12 is deliberate: `python` on a typical Windows box may point at a newer release, and an explicit `py -3.12` keeps the environment reproducible. Both dependencies also work on 3.13 and 3.14 if you prefer.

## Milestones

- [x] **0 — Groundwork.** Pinned environment, sdc11073 API verified, networking settled.
- [x] **1 — Core.** Create data sources at runtime, publish them, remote-control them. Headless, with a console front end and an acceptance test.
- [x] **2 — Provider UI.** Metric list with live values, "New data source" dialog.
- [x] **3 — Consumer UI.** Discovery, MDIB browser, editors for controllable metrics. Works against foreign devices, not just our own.
- [x] **4a — Alarms and presets.** Alert conditions with their signals, and configs you can export, import and load at startup.
- [ ] **4b — Waveforms and TLS.**

## Try it

Run it with no arguments and it asks how to start: device name, which address to bind
discovery to, an optional config file, and whether to log verbosely. Everything is filled in
with a working default, so Start is usually enough.

```powershell
.venv\Scripts\python.exe run_toolbox.py
```

The address is a list rather than a field, because discovery binds to a **single** IPv4
address and a normal machine has eight or nine. Each entry names its adapter, usable
addresses come first, and link-local ones are marked as the dead ends they are.

Give it any argument and it starts straight away instead, which is what you want for a
shortcut or a script:

```powershell
.venv\Scripts\python.exe run_toolbox.py --name alpha
.venv\Scripts\python.exe run_toolbox.py --name beta --config presets\insufflator.json
```

Start it twice under different names to have two devices find each other. The name decides
the EPR, so restarting under the same name keeps that device's identity on the network — and
two instances must not share one.

The **My device** panel is the device you publish. *New data source…* creates a number, text or choice; the checkbox in the last column decides whether other devices may write to it. The value column is live — it updates whether you edit it here or somebody changes it over the network.

The **Network** panel is everybody else's. *Scan* finds providers, *Connect* loads one, and you get its containment tree above and its metrics below. Rows the device will accept writes for are marked writable; select one and the editor underneath adapts to it — a combo box for a choice, a plain field with the permitted range for a number. The result of a write is reported as the provider's own `InvocationState`.

Press **Alt** for the menu bar. *View → Split view* (F8) swaps between the two panels sitting side by side with a movable divider, and the same two stacked as tabs. *View → Use widgets if possible* (F9) swaps the metric tables for a control per metric.

## Widgets

By default each metric gets the control that suits it rather than a row in a table:

| Metric                                  | Control                                   |
|-----------------------------------------|-------------------------------------------|
| Choice                                  | dropdown of its allowed values            |
| Number with a minimum **and** a maximum | slider, labelled with both ends           |
| Number without both                     | the value, with −10 −1 +1 +10 either side |
| Text                                    | a field                                   |
| Anything else                           | the value, read-only                      |

"If possible" is the operative part. A waveform, or a metric type this tool has never heard of, still gets a card showing its value — it just cannot be edited. Nothing disappears because no control fits it.

The fallbacks are deliberate too. A slider needs both ends of the range to mean anything, so a one-sided limit gets the stepper. A range that would need more than 100 000 slider steps gets the stepper as well, because at that point a slider is a lie.

Switch to the table with *View → Use widgets if possible* whenever you want the details:
handles, units, ranges and writability all at once.

Adding a control is one class in `sdctoolbox/gui/widgets/controls.py` and one line in
`factory.py`. Nothing else knows any control by name.

Two things worth doing, because they are what makes SDC interesting:

- Add a data source in one instance while the other is connected to it. It appears in the other's table straight away, with no reconnect.
- Untick its checkbox and try to set it from the other side. The write is refused and the value stays put.

The same is available as a text console, which is easier to script:

```powershell
.venv\Scripts\python.exe examples\console.py provider
.venv\Scripts\python.exe examples\console.py consumer
```

```
consumer> scan
  [0] urn:uuid:053b9f8f-0aa5-5290-8797-351f901ebd74
      sdc.ctxt.loc:/sdc.ctxt.loc.detail/HOSP///CU1//Toolbox?fac=HOSP&poc=CU1&bed=Toolbox
consumer> connect 0
consumer> list
  handle              kind     value    range      unit      writable
  m.mode              choice   IDLE                          disabled
      allowed: IDLE, RUN, PAUSE    (Mode)
  m.zoom_level        number   -        1 to 100   steps     yes
consumer> set m.zoom_level 9
accepted, m.zoom_level is now 9
consumer> set m.zoom_level 500
refused by the provider (Fail)
```

Numbers can carry limits. Those become two different things in BICEPS, because the standard separates what a device can produce from what a caller may ask for:

|                                          |                                             |
|------------------------------------------|---------------------------------------------|
| `NumericMetricDescriptor/TechnicalRange` | on the metric — what it can produce         |
| `SetValueOperationState/AllowedRange`    | on the set operation — what you may request |

The second is on a *state*, so the permitted window can be narrowed while the device runs, the same way `OperatingMode` can. Neither is enforced by the library, so the provider checks incoming values itself and answers `FAILED`.

There is also a console provider, if you would rather have both sides in text:

```powershell
.venv\Scripts\python.exe examples\console.py provider
```

## Presets

*File → Export config* writes everything you have set up — the data sources and the alarms — to a JSON file. *File → Import config* builds it again, replacing whatever the device currently has. The same file can be loaded at startup:

```powershell
.venv\Scripts\python.exe run_toolbox.py --config presets\insufflator.json
.venv\Scripts\python.exe examples\console.py provider --config presets\insufflator.json
```

`presets/insufflator.json` is an example: six data sources and three alarms. A bad file is refused before anything starts, naming what is wrong with it.

Handles are recorded in the file, so a preset reproduces the same MDIB every time. That matters if a script or another device refers to them by name.

## Alarms

BICEPS keeps two things apart that are easy to confuse:

|               |                                                                                                 |
|---------------|-------------------------------------------------------------------------------------------------|
| **condition** | the fact — "the pressure is too high". Has a kind and a priority, and is either present or not. |
| **signal**    | how that fact is announced — visually, audibly, or by vibration.                                |

One condition can drive several signals, which is why they are separate objects rather than flags on one. Every alarm this tool creates gets a visual and an audible signal, so the split is visible in the MDIB tree.

Give an alarm limits and it becomes a `LimitAlertCondition` that follows its source metric by itself; leave them out and it stays a plain `AlertCondition` that only moves when you raise or clear it. Either way a value written by a remote consumer moves it exactly as a local edit does.

## Tests

Runs a provider in one process and checks it from a consumer in another. 34 checks covering discovery, all three controllable metric kinds, value rejection, disabled controls and descriptor creation at runtime.

```powershell
.venv\Scripts\python.exe tests\acceptance_core.py
```

The GUI has its own smoke test, which builds the real window on Qt's offscreen backend and drives the actual widgets, including a live connection to a provider in another process. 120 checks, no display needed.

```powershell
.venv\Scripts\python.exe tests\gui_smoke.py
```

Config files have a round-trip test that exports a device, rebuilds it and compares the two, then checks that a range of broken files are refused with a usable message.

```powershell
.venv\Scripts\python.exe tests\config_roundtrip.py
```

The widgets have their own suite: which control gets picked for which metric, then the real controls driven inside the real window to check a click reaches the device.

```powershell
.venv\Scripts\python.exe tests\widget_controls.py
```

## Security

Everything runs over plain `http://`. Neither `ProviderService` nor `ConsumerService` passes an `ssl_context_container`, so there is no TLS, no certificates and no authentication — treat it as a lab tool on a network you trust.

The log line `Using SSL is enabled. TLS 1.3 Support = True` is a capability message from sdc11073, not a statement about the connection.

## Using the core

```python
from decimal import Decimal
from sdctoolbox.model import MetricKind, MetricSpec
from sdctoolbox.provider_service import ProviderService

with ProviderService(instance_name="alpha") as provider:
    handle = provider.add_metric(
        MetricSpec(
            label="Zoom level",
            kind=MetricKind.NUMBER,
            unit_label="steps",
            controllable=True,
            initial_value=Decimal("1"),
        )
    )
    provider.set_value(handle, Decimal("5"))
    provider.disable_control(handle)  # keeps the operation, refuses writes
```

```python
from sdctoolbox.consumer_service import ConsumerService

with ConsumerService() as service:
    devices = service.scan(timeout=15)
    remote = service.connect(devices[0])
    for handle, metric in remote.metrics().items():
        print(handle, metric.kind, metric.value, metric.controllable_now)
    remote.set_value("m.zoom_level", Decimal("9"))
    remote.close()
```

## Diagnostics

`diagnostics/` holds standalone scripts that use none of this project's own code. When something breaks, they answer the first question: is it us or is it the environment?

```powershell
# Does the installed sdc11073 still have the API we build on?
.venv\Scripts\python.exe diagnostics\check_api.py

# A bare provider and consumer, straight from the sdc11073 tutorial MDIB.
# If these two cannot see each other, the problem is the network, not the toolbox.
.venv\Scripts\python.exe diagnostics\minimal_provider.py --ip 127.0.0.1   # terminal 1
.venv\Scripts\python.exe diagnostics\minimal_consumer.py --ip 127.0.0.1   # terminal 2
```

`--ip 192.168.0.152` works as well; both loopback and a real LAN adapter are verified.

## Networking note

WS-Discovery in sdc11073 binds to a **single IPv4 address**. On a machine with several adapters (VPN, Hyper-V, Wi-Fi Direct, Bluetooth PAN) you have to say which one, hence the
`--ip` argument everywhere. Loopback is the default so two instances on one machine can talk without involving the network.

