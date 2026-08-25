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

Python 3.12 is deliberate, see `PLANNING.md`.

## Status

Stages 0 and 1 are complete: the core works headless and is covered by an acceptance test.
The GUI starts at stage 2.

| Stage | Content | Status |
|---|---|---|
| 0 | Environment, API reconciliation, networking settled | done |
| 1 | Headless core: create a metric at runtime, remote-control it | done |
| 2 | GUI provider side | open |
| 3 | GUI consumer side (defensive against foreign MDIBs) | open |
| 4 | Alerts, waveforms, persistence, TLS | open |

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

`--ip 192.168.0.152` works as well; both paths are verified.

## Documents

- `INSTRUCTIONS.md` — specification: what gets built and under which rules
- `PLANNING.md` — rationale, measurements and the traps found along the way
