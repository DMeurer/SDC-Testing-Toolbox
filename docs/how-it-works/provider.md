---
title: The provider
parent: How it works
nav_order: 2
---

# The provider
{: .no_toc }

`ProviderService` (`sdctoolbox/provider_service.py`) publishes My device. It turns the toolbox's
simple descriptions of data sources, alarms and actions into BICEPS descriptors and states, keeps
the operations in sync, and has no GUI dependency.

1. TOC
{:toc}

## Starting

A provider is built from a name, a network address and optionally a device identity and TLS
settings. The **name decides the EPR**: it is a UUID derived from the name (UUID version 5 in a
fixed namespace), so a device restarted under the same name keeps its identity on the network.

`start()` without an MDIB publishes a small bootstrap device: an MDS `mds0`, a VMD `vmd0`, a channel
`ch0.vmd0`, an SCO, an alert system and a system context with a patient and a location. `start(mdib)`
publishes an MDIB from a file instead, exactly as it is.

Which manufacturer and model the device announces is fixed when the provider is built, because the
library sets the DPWS `ThisModel` and `ThisDevice` metadata then. That is why `run_toolbox.py` reads
a config file **before** it builds the provider.

## From a data source to BICEPS

A data source is a `MetricSpec` (`model.py`): label, kind, unit, range, resolution, whether it is
controllable, and so on. `add_metric` turns it into BICEPS:

1. It validates the spec completely before touching the MDIB.
2. It picks a handle: `m.` plus the label as a slug (`Zoom level` → `m.zoom_level`), with a number
   added if it is taken.
3. It creates the descriptor of the right type (`NumericMetricDescriptor` and so on) in the
   channel of the spec's section, creating the section's channel on first use.
4. It writes the descriptor in a **descriptor transaction**. The library then sends a description
   modification report, which is how a connected consumer sees the new metric without
   reconnecting.
5. It sets the initial value in a state transaction, which sends a metric report.
6. If the spec is controllable, `enable_control` registers a set operation `op.<name>` in the SCO
   that points at the metric. Switching remote control off later sets the operation's
   `OperatingMode` to disabled; the operation stays.

Every change to the MDIB happens inside a transaction, and every transaction raises the
MdibVersion. If a descriptor cannot be announced, the provider removes it again, so the MDIB never
holds a handle consumers were told about but the provider has no record of.

The handle prefixes are fixed in `constants.py`:

| Prefix | For |
|---|---|
| `m.` | metrics |
| `op.` | set operations |
| `act.` | actions (activate operations) |
| `al.` | alert conditions |
| `sig.` | alert signals |
| `vmd.`, `ch.` | sections |

## Remote writes

One generic handler (`handlers.py`) serves every controllable metric; the target handle travels
with the request. It refuses a write that is disabled, outside the allowed range, not an allowed
value, or refused by a simulated fault, and returns the error code and a reason.

The library reports only the state of a result, so the toolbox's `ReasonedSetService` adds the
error code and text to the report. That is how a consumer learns *why* a write failed. A similar
repair, `VersionedGetService`, adds the MDIB versions to the narrower Get answers.

## Alarms

`add_alert` creates an alert condition (a limit alert condition when it has limits) and its
signals in the alert system. When the source metric changes, the provider compares it with the
limits and updates the condition's presence. Acknowledging changes the signals' presence and
leaves the condition alone; delegation is refused unless the signal allows it.

## Waveforms and distributions

`sample_generation.py` runs one generator thread. Every quarter of a second it produces the next
block of samples for each generated waveform and distribution and publishes them: waveforms as a
waveform stream, distributions as a metric report. A metric whose sensor is not *On* is skipped.

## Simulated faults

The faults of [Advanced mode](../user-guide/advanced-mode.md) live where a misbehaving device
differs from a healthy one:

- `FaultableProvider`, a subclass of the library's `SdcProvider`, holds back reports while
  *Pause reports* is on, forgets subscriptions, and goes silent for *Leave without Bye*;
- the services and handlers delay answers and refuse writes;
- per-metric faults are real MDIB state (`ActivationState`, `MetricQuality`) that the provider puts
  on every later value.

## Loaded MDIBs

`native_mdib.py` *adopts* an MDIB from a file: it reads the descriptors into the provider's
bookkeeping (which metrics, alarms, operations and sections there are) without rebuilding or
correcting anything. Toolbox behaviour extensions are read and removed before the MDIB is
published. After that, a loaded device is edited through the same methods as a built one, using the
file's own MDS, SCO, alert system and channels.

## Files and transactions

`config.py` and `mdib_io.py` write a device as one MDIB file and read it back. An old JSON profile
is applied in a transaction: if anything fails, the provider restores the metrics, alarms, actions,
sections and contexts it had, and publishes the changes that undo it, so connected consumers see
the same device again. The versions keep going up; they never go back.
