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
- [ ] **4 — Extras.** Alerts, waveforms, saved configurations, TLS.

## Try it

Start it twice. Each instance publishes a device and can discover the other.

```powershell
.venv\Scripts\python.exe run_toolbox.py --name alpha
.venv\Scripts\python.exe run_toolbox.py --name beta
```

The **My device** panel is the device you publish. *New data source…* creates a number, text
or choice; the checkbox in the last column decides whether other devices may write to it. The
value column is live — it updates whether you edit it here or somebody changes it over the
network.

The **Network** panel is everybody else's. *Scan* finds providers, *Connect* loads one, and
you get its containment tree above and its metrics below. Rows the device will accept writes
for are marked writable; select one and the editor underneath adapts to it — a combo box for
a choice, a plain field with the permitted range for a number. The result of a write is
reported as the provider's own `InvocationState`.

Press **Alt** for the menu bar. *View → Split view* (F8) swaps between the two panels sitting
side by side with a movable divider, and the same two stacked as tabs.

Two things worth doing, because they are what makes SDC interesting:

- Add a data source in one instance while the other is connected to it. It appears in the
  other's table straight away, with no reconnect.
- Untick its checkbox and try to set it from the other side. The write is refused and the
  value stays put.

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

Numbers can carry limits. Those become two different things in BICEPS, because the standard
separates what a device can produce from what a caller may ask for:

| | |
|---|---|
| `NumericMetricDescriptor/TechnicalRange` | on the metric — what it can produce |
| `SetValueOperationState/AllowedRange` | on the set operation — what you may request |

The second is on a *state*, so the permitted window can be narrowed while the device runs,
the same way `OperatingMode` can. Neither is enforced by the library, so the provider checks
incoming values itself and answers `FAILED`.

There is also a console provider, if you would rather have both sides in text:

```powershell
.venv\Scripts\python.exe examples\console.py provider
```

## Tests
Runs a provider in one process and checks it from a consumer in another. 34 checks covering
discovery, all three controllable metric kinds, value rejection, disabled controls and
descriptor creation at runtime.

```powershell
.venv\Scripts\python.exe tests\acceptance_core.py
```

The GUI has its own smoke test, which builds the real window on Qt's offscreen backend and
drives the actual widgets, including a live connection to a provider in another process.
111 checks, no display needed.

```powershell
.venv\Scripts\python.exe tests\gui_smoke.py
```
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

