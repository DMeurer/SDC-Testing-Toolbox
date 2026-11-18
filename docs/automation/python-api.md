---
title: Python
parent: Automation
nav_order: 3
---

# Python
{: .no_toc }

1. TOC
{:toc}

The window is a skin over two services in the `sdctoolbox` package, and you can use them directly
from your own scripts and tests. Run them from the repository with the project's Python, or put
the repository on `sys.path`.

{: .note }
These are the toolbox's internal classes, not a published library. They change when the toolbox
changes; pin a release if you depend on them.

## A provider

`ProviderService` publishes a device. It starts with an empty MDS, VMD and channel, and data
sources are added at runtime:

```python
from decimal import Decimal

from sdctoolbox.model import AlertSpec, Coding, LocationInfo, MetricKind, MetricSpec, PatientInfo, PatientMeasurement
from sdctoolbox.provider_service import ProviderService

with ProviderService(instance_name="alpha") as provider:  # starts it, stops it at the end
    handle = provider.add_metric(MetricSpec(
        label="Zoom level",
        kind=MetricKind.NUMBER,
        unit_label="steps",
        minimum=Decimal("1"), maximum=Decimal("100"),
        controllable=True,
        initial_value=Decimal("1"),
    ))
    provider.set_value(handle, Decimal("5"))
    provider.disable_control(handle)  # keeps the operation, refuses writes

    provider.set_location(LocationInfo(facility="HOSP", point_of_care="OR1", bed="A"))
    provider.set_patient(PatientInfo(
        given_name="Ada", family_name="Lovelace",
        height=PatientMeasurement(Decimal("170.5"), Coding("demo-cm", "private", "cm")),
    ))

    alarm = provider.add_alert(AlertSpec(
        label="Zoom high", source_handle=handle, upper_limit=Decimal("90"), delegable=True,
    ))
    provider.set_value(handle, Decimal("95"))  # raises it
    provider.acknowledge_alert(alarm)  # signals go Ack, the condition stays present
    print([signal.summary() for signal in provider.signal_states(alarm)])
```

Useful arguments of `ProviderService`: `ip` (the discovery address, `127.0.0.1` by default),
`instance_name` (decides the EPR), `friendly_name`, and `tls_config`.

To start from a file instead:

```python
from sdctoolbox import config
from sdctoolbox.mdib_io import parse_mdib

device = config.load_file("presets/patient-monitor.xml")
provider = ProviderService(instance_name="monitor")
provider.start(parse_mdib(device.xml))
```

## A consumer

`ConsumerService` finds providers and connects to them. A connection is a `RemoteDevice`:

```python
from decimal import Decimal

from sdctoolbox.consumer_service import ConsumerService

with ConsumerService() as service:
    devices = service.scan(timeout=5)
    remote = service.connect(devices[0])

    for handle, metric in remote.metrics().items():
        print(handle, metric.kind, metric.value, metric.controllable_now)

    state = remote.set_value("m.zoom_level", Decimal("9"))
    print(state, remote.last_invocation_detail)  # Fin, or Fail and the device's reason

    for message in remote.messages.messages()[-5:]:
        print(message.direction, message.summary)
    remote.close()
```

`RemoteDevice` also has `alerts()`, `actions()`, `run_action(handle)`, `patient_contexts()`,
`get_mdib()` and `bind(...)` for callbacks on changes. `remote.mdib` is the library's raw consumer
MDIB, for everything the high-level methods do not cover.

The consumer side never assumes the device was built by the toolbox: a metric may have no unit, no
label and no value, and the methods return what is there.

## The checks

```python
from sdctoolbox import device_test, mdib_check

findings = mdib_check.check(provider.mdib)  # or remote.mdib
print(mdib_check.summary(findings))
for finding in findings:
    print(finding.level, finding.where, finding.text, finding.rule)

results = device_test.run(service, remote)
print(device_test.summary(results))
```

## Without code: the MCP tools

`sdctoolbox.mcp.tools.ToolboxTools` is everything the [MCP tools](mcp.md) do, as Python methods
taking the same JSON shapes. The console's `tool` command uses it.
