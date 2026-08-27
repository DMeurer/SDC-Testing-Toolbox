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
- [x] **4b — Contexts and signal handling.** Editable patient and location, acknowledgement and delegation, a preset picker.
- [x] **4c — Waveforms and distributions.** Both sample-array kinds, a generator for waveforms, and a plot to watch them on.
- [ ] **4d — TLS.**
- [ ] **5 - Presets for real devices.** Emulate a real device, so a consumer can be tested without the device being present.

## Try it

Run it with no arguments and it asks how to start: device name, which address to bind discovery to, an optional config file, and whether to log verbosely. Everything is filled in with a working default, so Start is usually enough.

```powershell
.venv\Scripts\python.exe run_toolbox.py
```

Two of those fields are dropdowns rather than plain boxes, for the same reason. The address list matters because discovery binds to a **single** IPv4 address and a normal machine has eight or nine: each entry names its adapter, usable addresses come first, and link-local ones are marked as the dead ends they are. The config list offers the presets that ship with the tool, so the common case needs no file dialog at all. Both stay editable if you want something that is not on the list.

Give it any argument and it starts straight away instead, which is what you want for a shortcut or a script:

```powershell
.venv\Scripts\python.exe run_toolbox.py --name alpha
.venv\Scripts\python.exe run_toolbox.py --name beta --config presets\insufflator.json
```

Start it twice under different names to have two devices find each other. The name decides the EPR, so restarting under the same name keeps that device's identity on the network — and two instances must not share one.

The **My device** panel is the device you publish. *New data source…* creates a number, text or choice; the checkbox in the last column decides whether other devices may write to it. The value column is live — it updates whether you edit it here or somebody changes it over the network. *Patient and location…* edits the two contexts, and the line beside it shows what they currently say.

The **Network** panel is everybody else's. *Scan* finds providers, *Connect* loads one, and you get its containment tree above and its metrics below. Rows the device will accept writes for are marked writable; select one and the editor underneath adapts to it — a combo box for a choice, a plain field with the permitted range for a number. The result of a write is reported as the provider's own `InvocationState`.

The menu bar is on screen by default. *View → Always show menu bar* hides it until you press **Alt**, if you would rather have the room. *View → Split view* (F8) swaps between the two panels sitting side by side with a movable divider, and the same two stacked as tabs. *View → Use widgets if possible* (F9) swaps the metric tables for a control per metric.

## Widgets

By default each metric gets the control that suits it rather than a row in a table:

| Metric                                  | Control                                   |
|-----------------------------------------|-------------------------------------------|
| Choice                                  | dropdown of its allowed values            |
| Waveform or distribution                | a plot                                    |
| Number with a minimum **and** a maximum | slider, labelled with both ends           |
| Number without both                     | the value, with −10 −1 +1 +10 either side |
| Text                                    | a field                                   |
| Anything else                           | the value, read-only                      |

"If possible" is the operative part. A metric type this tool has never heard of still gets a card showing its value — it just cannot be edited. Nothing disappears because no control fits it.

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

It reaches everything the window does, which makes it the quickest way to watch an acknowledgement not clear an alarm:

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
provider> delegate al.pressure_high
  Vis:Ack->Rem Aud:Ack->Rem
provider> where HOSP/Surgery/2/OR1/1/Table
  HOSP / Surgery / 2 / OR1 / 1 / Table
provider> patient Ada Lovelace F Ad 1815-12-10
  Ada Lovelace (F, Ad, 1815-12-10)
provider> presets
  Insufflator        6 data source(s), 3 alarm(s)
      Six data sources and three alarms, roughly what a laparoscopic insufflator publishes.
```

## Presets

*File → Export config* writes everything you have set up — the data sources, the alarms and the contexts — to a JSON file. *File → Import config* builds it again, replacing whatever the device currently has.

*File → Load preset* lists the ready-made devices in `presets/`, so the ones that ship with the tool need no file dialog. The same list appears in the startup window. A preset that will not parse is left out of the menu rather than breaking it; open it with *Import config* if you want to know why.

Any of them can also be loaded at startup:

```powershell
.venv\Scripts\python.exe run_toolbox.py --config presets\insufflator.json
.venv\Scripts\python.exe examples\console.py provider --config presets\insufflator.json
```

`presets/insufflator.json` is an example: eight data sources including a waveform and a distribution, three alarms and a location. A bad file is refused before anything starts, naming what is wrong with it.

Handles are recorded in the file, so a preset reproduces the same MDIB every time. That matters if a script or another device refers to them by name.

## Waveforms and distributions

These are the two BICEPS metric types whose state carries *many* values instead of one, and the two the toolbox could not create until milestone 4c. They look similar and are not:

|                  | Waveform (`RealTimeSampleArrayMetric`)      | Distribution (`DistributionSampleArrayMetric`) |
|------------------|---------------------------------------------|------------------------------------------------|
| samples are over | **time**                                     | a **domain** — the `DomainUnit`, e.g. Hz       |
| arrives as       | a `WaveformStream`                           | an `EpisodicMetricReport`                       |
| behaves like     | a moving window; blocks keep coming          | one whole picture that gets replaced            |
| mandatory extras | `Resolution`, `SamplePeriod`                 | `Resolution`, `DomainUnit`                      |
| drawn as         | a scrolling trace                            | bars across its `DistributionRange`             |

The distinction between `Unit` and `DomainUnit` is the one worth seeing on screen: a spectrum is measured in dB (`Unit`) *across* a range of Hz (`DomainUnit`), and they are separate elements for that reason. The card says `over 0 to 500 Hz` underneath for exactly this.

**Neither can be remote-controlled.** That is not a limitation of this tool: BICEPS defines no operation whose argument is a sample array, so the New data source dialog hides the control checkbox for both, and a peer's waveform is marked `samples, read-only`.

Add a waveform and it starts generating immediately — a waveform with nothing driving it publishes a descriptor and never a sample, which looks like a broken device rather than an idle one. Pick the curve from *Shape*; sine, sawtooth, square and noise are there so a consumer you are testing can be checked against something recognisable by eye. The shape is **not** a BICEPS concept: the standard carries samples and says nothing about what they look like.

A distribution has nothing driving it and waits to be given a block, from the console:

```
provider> samples m.spectrum 3 9 27 9 3
  5 sample(s): 3 9 27 9 3
provider> waveforms off
  generator stopped
```

The plot is painted by hand in `sdctoolbox/gui/widgets/plot.py`. It is a polyline and a couple of guide lines, and pulling in a charting library for that would have been the largest dependency in the project by a wide margin.

## Alarms

BICEPS keeps two things apart that are easy to confuse:

|               |                                                                                                 |
|---------------|-------------------------------------------------------------------------------------------------|
| **condition** | the fact — "the pressure is too high". Has a kind and a priority, and is either present or not. |
| **signal**    | how that fact is announced — visually, audibly, or by vibration.                                |

One condition can drive several signals, which is why they are separate objects rather than flags on one. Every alarm this tool creates gets a visual and an audible signal, so the split is visible in the MDIB tree.

Give an alarm limits and it becomes a `LimitAlertCondition` that follows its source metric by itself; leave them out and it stays a plain `AlertCondition` that only moves when you raise or clear it. Either way a value written by a remote consumer moves it exactly as a local edit does.

The Signals column is where the split stops being academic. Two buttons act on it:

| Button          | What it changes                               |
|-----------------|-----------------------------------------------|
| **Acknowledge** | each signal's `Presence`, from `On` to `Ack`  |
| **Delegate**    | each signal's `Location`, from `Loc` to `Rem` |

Acknowledging is the interesting one, because it does **not** clear the alarm. The condition keeps its `Presence`; only the announcement changes. Watch the State and Signals columns while you do it — State stays `PRESENT` and Signals goes to `Vis:Ack Aud:Ack`. Push the source further out of range and the acknowledgement survives, because the fact has not changed. Bring it back into range and out again and it does not, because that is a new occurrence.

Delegation records that another device announces the signal instead. BICEPS only permits it where the descriptor sets `SignalDelegationSupported`, which is the *Delegation* checkbox in the New alarm dialog, and nothing in sdc11073 enforces that — so the provider does. Note what this is: it marks where the announcement belongs. It does not arrange for anybody to pick it up, which would need a second device offering a delegable signal of its own.

## Contexts

Contexts are the part of BICEPS that says *who* and *where*, as opposed to what the device is measuring. *Patient and location…* on the My device panel edits both.

They are worth a look because they behave unlike anything else in the MDIB:

- **They are multi-state.** Setting a patient does not overwrite the previous one. The old state is *disassociated* and kept, and a new one is associated, so the MDIB records who was attached when. Attach two patients in a row and look at `PC.mds0` in the consumer's containment tree: there are two states, one `Assoc` and one `Dis`.
- **The location is also a discovery scope.** Changing it re-announces the device, so a consumer filtering on the old location stops seeing it. The patient never leaves the MDIB.

Clearing every patient field detaches the patient rather than attaching a nameless one.

The patient fields are a subset of `pm:PatientDemographicsCoreData` — name, sex, patient type and date of birth. Height, weight and race are in the standard and deliberately left out: inviting someone to type a weight into a learning tool suggests a clinical purpose it has none of.

## Tests

Five suites, all runnable from a terminal, all printing PASS/FAIL per check.

| Suite                       | Checks | Covers                                                                                |
|-----------------------------|--------|---------------------------------------------------------------------------------------|
| `tests/acceptance_core.py`  | 65     | two processes: discovery, control, rejections, runtime descriptors, alarms, waveforms  |
| `tests/gui_smoke.py`        | 241    | the real window offscreen, plus a live peer process                                    |
| `tests/widget_controls.py`  | 64     | which control for which metric, then controls driven for real                          |
| `tests/provider_core.py`    | 61     | descriptor rollback, sample arrays, signal handling, contexts, presets                 |
| `tests/config_roundtrip.py` | 25     | export, reimport, compare; broken files refused                                        |

```powershell
.venv\Scripts\python.exe tests\acceptance_core.py
.venv\Scripts\python.exe tests\gui_smoke.py
.venv\Scripts\python.exe tests\widget_controls.py
.venv\Scripts\python.exe tests\provider_core.py
.venv\Scripts\python.exe tests\config_roundtrip.py
```

The acceptance test runs a provider in one process and checks it from a consumer in another. The GUI suite builds the real window on Qt's offscreen backend and drives the actual widgets, including a live connection to a provider in another process — no display needed.

`provider_core.py` is the odd one out: it is about what the MDIB must never be left in. Its first section deliberately writes a descriptor BICEPS cannot serialise *without* the rollback, watches the orphan appear, and only then checks that the guarded path leaves nothing behind — so a passing run means the check is still capable of failing.

## Security

Everything runs over plain `http://`. Neither `ProviderService` nor `ConsumerService` passes an `ssl_context_container`, so there is no TLS, no certificates and no authentication — treat it as a lab tool on a network you trust.

The log line `Using SSL is enabled. TLS 1.3 Support = True` is a capability message from sdc11073, not a statement about the connection.

## Using the core

```python
from decimal import Decimal
from sdctoolbox.model import AlertSpec, LocationInfo, MetricKind, MetricSpec, PatientInfo
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

    provider.set_location(LocationInfo(facility="HOSP", point_of_care="OR1", bed="A"))
    provider.set_patient(PatientInfo(given_name="Ada", family_name="Lovelace"))

    alarm = provider.add_alert(
        AlertSpec(label="Zoom high", source_handle=handle, upper_limit=Decimal("90"), delegable=True)
    )
    provider.set_value(handle, Decimal("95"))  # raises it
    provider.acknowledge_alert(alarm)  # signals go Ack, condition stays present
    print([signal.summary() for signal in provider.signal_states(alarm)])
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

