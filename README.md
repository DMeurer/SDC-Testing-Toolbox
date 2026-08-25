# SDC-testing-toolbox

A desktop tool for understanding **IEEE 11073 SDC**: create data sources with a few clicks,
publish them on the network, discover other SDC devices, subscribe to their data sources and
remote-control them.

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

Python 3.12 is deliberate: `python` on a typical Windows box may point at a newer release,
and an explicit `py -3.12` keeps the environment reproducible. Both dependencies also work on
3.13 and 3.14 if you prefer.

## Milestones

- [x] **0 — Groundwork.** Pinned environment, sdc11073 API verified, networking settled.
- [x] **1 — Core.** Create data sources at runtime, publish them, remote-control them. Headless, with a console front end and an acceptance test.
- [ ] **2 — Provider UI.** Metric list with live values, "New data source" dialog.
- [ ] **3 — Consumer UI.** Discovery, MDIB browser, editors for controllable metrics. Works against foreign devices, not just our own.
- [ ] **4 — Extras.** Alerts, waveforms, saved configurations, TLS.

## Try it

Two terminals. The first publishes a device, the second finds it and controls it.

```powershell
# terminal 1
.venv\Scripts\python.exe examples\console.py provider
```
```
provider> add number Zoom level
created m.zoom_level  (number)
provider> add choice Mode IDLE RUN PAUSE
created m.mode  (choice, values IDLE, RUN, PAUSE)
provider> control m.mode off
m.mode now refuses remote writes
```

```powershell
# terminal 2
.venv\Scripts\python.exe examples\console.py consumer
```
```
consumer> scan
  [0] urn:uuid:053b9f8f-0aa5-5290-8797-351f901ebd74
      sdc.ctxt.loc:/sdc.ctxt.loc.detail/HOSP///CU1//Toolbox?fac=HOSP&poc=CU1&bed=Toolbox
consumer> connect 0
connected, 15 entities in its MDIB
consumer> list
  handle                   kind     value          unit       writable
  m.mode                   choice   IDLE           no unit    disabled
      allowed: IDLE, RUN, PAUSE    (Mode)
  m.zoom_level             number   -              no unit    yes
consumer> set m.zoom_level 9
accepted, m.zoom_level is now 9
consumer> set m.mode RUN
refused by the provider (Fail)
```

Things worth trying: add a data source in terminal 1 while terminal 2 is already connected,
then run `list` again there — it appears without reconnecting. `watch` in the consumer prints
changes as they arrive. `help` lists every command.

## Acceptance test

Runs a provider in one process and checks it from a consumer in another. 34 checks covering
discovery, all three controllable metric kinds, value rejection, disabled controls and
descriptor creation at runtime.

```powershell
.venv\Scripts\python.exe tests\acceptance_core.py
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
    provider.disable_control(handle)   # keeps the operation, refuses writes
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

`diagnostics/` holds standalone scripts that use none of this project's own code. When
something breaks, they answer the first question: is it us or is it the environment?

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

WS-Discovery in sdc11073 binds to a **single IPv4 address**. On a machine with several
adapters (VPN, Hyper-V, Wi-Fi Direct, Bluetooth PAN) you have to say which one, hence the
`--ip` argument everywhere. Loopback is the default so two instances on one machine can talk
without involving the network.

