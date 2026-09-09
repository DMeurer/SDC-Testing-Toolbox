# SDC-testing-toolbox

A desktop tool for understanding **IEEE 11073 SDC**: create data sources with a few clicks, publish them on the network, discover other SDC devices, subscribe to their data sources and remote-control them.

Built on [sdc11073](https://github.com/Draegerwerk/sdc11073) and PySide6.

> [!WARNING]
> Learning tool, not a medical device. By its own notice the python library `sdc11073` is not intended for
> clinical trials, clinical studies or clinical routine use, and was not developed according
> to ISO 9001.

## Setup

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python 3.12 is the project's tested interpreter and is selected explicitly on Windows and in
CI. The pinned runtime dependencies declare support for Python 3.10 through 3.14, so Ubuntu
22.04's default Python 3.10 is also suitable. `requirements.txt` and
`requirements-build.txt` pin direct dependencies only; pip still resolves transitive
dependencies at install time, so they are not a complete reproducible lock.

## Application builds

PyInstaller produces a standalone Windows executable and a standalone Linux application
bundle. Builds are platform-specific: build Windows on Windows and Linux on Linux rather than
trying to cross-compile either artifact.

Install the pinned build tooling alongside the runtime dependencies, then run the tracked
specification:

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm SDC-Testing-Toolbox.spec
```

The Windows result is the `dist\SDC-Testing-Toolbox` directory. Keep that
directory intact: it contains replaceable shared libraries, `LICENSE`,
`THIRD_PARTY_NOTICES.md`, an artifact-specific `DEPENDENCY_INVENTORY.json`,
source/relinking instructions, and collected license texts. The inventory is
generated from installed distributions and PyInstaller analysis for each
native build; it is not a static lock file.

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv binutils libbrotli1 libdbus-1-3 libegl1 \
  libfontconfig1 libfreetype6 libgl1 \
  libglib2.0-0 libgtk-3-0 libx11-xcb1 libxcb-cursor0 libxcb-icccm4 \
  libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
  libxcb-render0 libxcb-shape0 libxcb-shm0 libxcb-sync1 libxcb-xfixes0 \
  libxcb-xkb1 libxkbcommon-x11-0 libxkbcommon0
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-build.txt
.venv/bin/python -m PyInstaller --clean --noconfirm SDC-Testing-Toolbox.spec
tar -C dist -czf dist/SDC-Testing-Toolbox-linux-x86_64.tar.gz SDC-Testing-Toolbox
```

The workflow's Linux archive targets x86-64 desktop distributions with glibc 2.35 or newer
because it is built on Ubuntu 22.04. A local build inherits its build host's glibc baseline.
The archive contains application-specific shared libraries as separate files, while normal desktop system
libraries remain host dependencies. Keep the bundle together after extracting it; the
executable is `SDC-Testing-Toolbox/SDC-Testing-Toolbox`.

The `Build application` GitHub Actions workflow performs both native builds, smoke-tests the
actual packaged applications, validates the final archives' legal payloads, and uploads Windows
and Linux archives as release candidates. Run it
manually when an artifact is needed; it also runs for pull requests to `develop` and version
tags.

## License

SDC Testing Toolbox is free software licensed under the
[GNU General Public License version 3 only](LICENSE) (`GPL-3.0-only`). This is
the toolbox's license, not the license of its dependencies.

Dependencies retain their own terms. In particular, `sdc11073` 3.0.0 is MIT
licensed, `cryptography` is Apache-2.0 OR BSD-3-Clause, while PySide6 and Qt are available under applicable LGPLv3, GPL, or
commercial terms depending on the components and license option. Packaged
builds also contain Python and files produced or embedded by PyInstaller. See
[Third-Party Notices](THIRD_PARTY_NOTICES.md) for attribution, authoritative
links, and distribution considerations. Review the generated inventory and
bundled texts for the exact artifact. Public binary releases must also publish
the exact corresponding-source payload defined in `legal/SOURCE_OFFER.md`;
these materials are operational guidance, not legal advice or a guarantee of
compliance.

## Milestones

- [x] **0 — Groundwork.** Direct dependencies pinned, sdc11073 API verified, networking settled.
- [x] **1 — Core.** Create data sources at runtime, publish them, remote-control them. Headless, with a console front end and an acceptance test.
- [x] **2 — Provider UI.** Metric list with live values, "New data source" dialog.
- [x] **3 — Consumer UI.** Discovery, MDIB browser, editors for controllable metrics. Accepts foreign MDIBs defensively; detailed metric and operation views cover the supported subset.
- [x] **4a — Alarms and presets.** Alert conditions with their signals, and configs you can export, import and load at startup.
- [x] **4b — Contexts and signal handling.** Editable patient and location, acknowledgement and delegation, a preset picker.
- [x] **4c — Waveforms and distributions.** Both sample-array kinds, a generator for both, and a plot to watch them on.
- [x] **4d — Device presets.** Seven realistic virtual device profiles with coded values, device identity, subsystem structure, and actions, each built by tests.
- [x] **4e — TLS.** Strict TLS 1.2+ mutual authentication, certificate inspection and CA-based participant authorization.

## Try it

Run it with no arguments and it asks how to start: device name, which address to bind discovery to, an optional config file, and whether to log verbosely.
Everything is filled in with a working default, so Start is usually enough.

```powershell
.venv\Scripts\python.exe run_toolbox.py
```

Two of those fields are dropdowns rather than plain boxes, for the same reason.
The address list matters because discovery binds to a **single** IPv4 address and a normal machine has eight or nine: each entry names its adapter, usable addresses come first, and link-local ones are marked as the dead ends they are. The config list offers the presets that ship with the tool, so the common case needs no file dialog at all. Both stay editable if you want something that is not on the list.

Give it any argument and it starts straight away instead, which is what you want for a shortcut or a script:

```powershell
.venv\Scripts\python.exe run_toolbox.py --name alpha
.venv\Scripts\python.exe run_toolbox.py --name beta --config presets\insufflator.json
```

Start it twice under different names to have two devices find each other. The name decides the EPR, so restarting under the same name keeps that device's identity on the network — and two instances must not share one.

The **My device** panel is the device you publish. *New data source…* creates a number, text, choice, waveform or distribution.
The remote-control checkbox is offered only for number, text and choice sources. The value column is live — it updates whether you edit it here or somebody changes it over the network.
*Patient and location…* edits the two contexts, and the line beside it shows what they currently say.

The **Network** panel is everybody else's. *Scan* finds providers, *Connect* loads one, and you get its containment tree above and its metrics below.
Rows the device will accept writes for are marked writable; select one and the editor underneath adapts to it — a combo box for a choice, a plain field with the permitted range for a number.
The result of a write is reported as the provider's own `InvocationState`.

The menu bar is on screen by default.\
*View → Always show menu bar* hides it until you press **Alt**, if you would rather have the room.\
*View → Split view* (F8) swaps between the two panels sitting side by side with a movable divider, and the same two stacked as tabs.\
*View → Use widgets if possible* (F9) swaps the metric tables for a control per metric.\

## Widgets

By default each metric gets the control that suits it rather than a row in a table:

| Metric                                  | Control                                   |
|-----------------------------------------|-------------------------------------------|
| Choice                                  | dropdown of its allowed values            |
| Waveform or distribution                | a plot                                    |
| Number with a minimum **and** a maximum | slider, labelled with both ends           |
| Number without both                     | the value, with −10 −1 +1 +10 either side |
| Text                                    | a field                                   |
| Other peer entity or extension          | containment tree only                     |

"If possible" is the operative part. The widgets cover the five BICEPS metric types this tool recognizes.
Other MDIB entities and extensions remain in the containment tree, but are not turned into metric cards or editable controls.

The fallbacks are deliberate too. A slider needs both ends of the range to mean anything, so a one-sided limit gets the stepper.
A range that would need more than 100 000 slider steps gets the stepper as well, because at that point a slider is a lie.

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

The second is on a *state*, so BICEPS permits the window to be narrowed while the device runs, the same way `OperatingMode` can.
For a controllable bounded numeric metric, this toolbox initializes both ranges from one metric specification and does not yet expose later narrowing.
Neither is enforced by the library, so the provider checks incoming values itself and answers `FAILED`.

There is also a console provider, if you would rather have both sides in text:

```powershell
.venv\Scripts\python.exe examples\console.py provider
```

It covers the core provider and consumer workflows in text, but does not mirror every GUI view or action button.
It is the quickest way to watch an acknowledgement not clear an alarm:

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
  Insufflator        9 data source(s), 3 alarm(s)
      Laparoscopic insufflator: nine metrics for gas flow, pressure control, and related alarms. One metric larger than the infusion pump, and the first preset this project had.
```

## Presets

*File → Export config* writes device metadata, data-source definitions and scalar current values, alarm and action definitions, and currently associated patient/location contexts to a JSON file.
It is not a full live-device snapshot: sample blocks, alert/signal state, control mode, generator state and context history are not exported.
*File → Import config* validates descriptor references and generated section containment before changing the provider. Replacement removes tracked metrics, alarms and actions before rebuilding them; `replace=False` appends the profile while retaining the existing graph.
It is not a whole-MDIB replacement. In either mode, omitting `device` metadata leaves the running identity unchanged, while supplied metadata also cannot change an already running provider. Omitting `contexts.patient` or `contexts.location` leaves that currently associated context unchanged. An explicit patient block is associated, including an empty block to detach the current patient; an explicit empty location is rejected because a published location needs at least one detail.
Both `replace=True` replacement and `replace=False` append are transactional for operational failures: the provider restores its prior managed metrics, alarms, actions, sections and associated contexts, publishing compensating transactions so connected consumers can recover the same graph. Append compensation removes only appended descriptors and restores contexts the failed import touched; it does not recreate unchanged pre-existing descriptors. This is recovery, not external atomicity: a subscribed consumer can observe temporary changes before the compensation, and a rollback failure is reported together with the import failure rather than hidden. Successful compensation keeps provider and consumer MDIB, descriptor, state and context-state versions monotonic; it advances versions instead of rewinding them.

*File → Load preset* lists the ready-made devices in `presets/`, so the ones that ship with the tool need no file dialog.
The same list appears in the startup window. Loading a profile with malformed field types or other schema errors raises `ConfigError`; `list_presets` catches those strict validation failures, invalid JSON and unreadable files and skips those entries, so one malformed preset does not interrupt discovery.
Explicit profile versions must be JSON integers from 1 through the current format version, 3; preset discovery skips files outside that range. Profiles without a version predate versioning and are intentionally read with legacy version 1 semantics.
The seven shipped presets are canonical current-schema profiles, not legacy compatibility fixtures. They are kept at profile format version 3 with explicit alert signal definitions. Tests for older readable formats use synthetic profile data instead of holding a shipped preset back on an earlier schema.

Any of them can also be loaded at startup:

```powershell
.venv\Scripts\python.exe run_toolbox.py --config presets\patient-monitor.json
.venv\Scripts\python.exe examples\console.py provider --config presets\ventilator.json
```

Load it at startup rather than importing it afterwards if you want the device to announce that model.
DPWS `ThisModel` and `ThisDevice` metadata is fixed when the provider is constructed. Startup loading reads the profile's `device` block before construction and then applies its live graph after startup; importing into an already running provider applies the graph but cannot change that provider's manufacturer, model, firmware, friendly name or other construction-time device metadata.
The startup name still decides the EPR and serial number; a saved `device.instance_name` does not override it.

For a profile that imports successfully, recorded handles make its defined metrics, alerts and actions addressable under stable names. They do not reproduce an identical live MDIB or provider identity.

### The shipped devices

| Preset                | What it is                            | Worth looking at                                                                                                    |
|-----------------------|---------------------------------------|---------------------------------------------------------------------------------------------------------------------|
| `patient-monitor`     | Bedside vitals                        | ECG, plethysmogram and arterial pressure running together; the one device whose parameters have real standard terms |
| `ventilator`          | Airway pressure, flow and volume      | Three synchronised waveforms, and what a stream of sample arrays costs                                              |
| `infusion-pump`       | Volumetric pump                       | The smallest, and the easiest to read end to end                                                                    |
| `hf-generator`        | Electrosurgery                        | A bimodal impedance spectrum, and a *Stop output* action                                                            |
| `surgical-microscope` | Robotic scope, after an Aesculap Aeos | Six axes, fixpoint and free modes, ICG fluorescence, and *Home axes* — none of which is a value you write           |
| `endoscopic-camera`   | Camera and light source               | Image profiles, and a *White balance now* action with nothing to type                                               |
| `insufflator`         | Laparoscopic insufflator              | Nine metrics, one more than the infusion pump; the first preset this project had                                    |

### What the presets are actually demonstrating

Every metric carries a **coded value**, because a label is not semantics. `"unit": "mmHg"` alone publishes a dimensionless number with a human comment attached; another device can do nothing with it.
The shipped metric profiles use two coding-system aliases:

- **`mdc`** — IEEE 11073-10101, for parameters that genuinely have a standard term.
- **`private`** — `urn:sdc-testing-toolbox:private`, for everything that does not.

Which is which is the interesting part, and `tests/presets.py` counts it:

```
patient-monitor         24 mdc,   2 private  (92% standard)
ventilator              23 mdc,   1 private  (96% standard)
infusion-pump           13 mdc,   3 private  (81% standard)
insufflator             10 mdc,   9 private  (53% standard)
hf-generator            11 mdc,  10 private  (52% standard)
surgical-microscope     15 mdc,  20 private  (43% standard)
endoscopic-camera        9 mdc,  15 private  (38% standard)
```

A patient monitor is almost entirely expressible in the standard's own vocabulary.
A surgical microscope is not, and neither is an electrosurgery generator — for those, the parts with standard terms are mostly the *units* (mm, degrees, watts), while what the device actually does has no agreed term at all.
That is not a shortcut taken here; it is the gap that work on extending the 1010X nomenclature exists to close, and marking it beats inventing codes that look official.

The `mdc` entries use the decimal context-free numeric codes defined by IEEE 11073-10101:2020
and its published amendments. Concepts and units for which those sources provide no direct
match remain explicitly `private`; the presets do not derive or invent MDC codes.

## Actions

Not everything a device does is a value you can write. *Home the axes*, *give a bolus*, *white balance now* — there is nothing to type and nothing to read back, only something to invoke.
BICEPS models that with an `ActivateOperation`, which is a different operation kind from the `SetValueOperation` behind a controllable metric.

They appear as a row of buttons on both panels. Network buttons follow the peer operation's `OperatingMode`.
My device buttons run their declared effects and do not currently mirror a locally changed operation mode.
What one does here is apply declared effects to metrics:

```json
{
	"label": "Home axes",
	"target": "vmd.motion",
	"effects": {
		"m.axis_x": "0",
		"m.axis_y": "0",
		"m.axis_z": "300",
		"m.motion_mode": "LOCKED"
	}
}
```

That stands in for machinery a real device would have behind homing an axis, and it is deliberately visible: an action whose result cannot be seen cannot be checked, and here the result is exactly the values it moved.
Invoke *Home axes* on a microscope from the other instance and watch six numbers change at once — without any of them having been sent.

## Waveforms and distributions

These are the two BICEPS metric types whose state carries *many* values instead of one. They look similar and are not:

|                  | Waveform (`RealTimeSampleArrayMetric`) | Distribution (`DistributionSampleArrayMetric`) |
|------------------|----------------------------------------|------------------------------------------------|
| samples are over | **time**                               | a **domain** — the `DomainUnit`, e.g. Hz       |
| arrives as       | a `WaveformStream`                     | an `EpisodicMetricReport`                      |
| behaves like     | a moving window; blocks keep coming    | one whole picture that gets replaced           |
| mandatory extras | `Resolution`, `SamplePeriod`           | `Resolution`, `DomainUnit`                     |
| drawn as         | a scrolling trace                      | bars across its `DistributionRange`            |

The distinction between `Unit` and `DomainUnit` is the one worth seeing on screen: a spectrum is measured in dB (`Unit`) *across* a range of Hz (`DomainUnit`), and they are separate elements for that reason.
The card says `over 0 to 500 Hz` underneath for exactly this.

**Neither can be remote-controlled.**
That is not a limitation of this tool: BICEPS defines no operation whose argument is a sample array, so the New data source dialog hides the control checkbox for both, and a peer's waveform is marked `samples, read-only`.

Add either kind and it starts generating immediately — a waveform with nothing driving it publishes a descriptor and never a sample, which looks like a broken device rather than an idle one.
Pick the curve from *Shape*; sine, sawtooth, square and noise are there so a consumer you are testing can be checked against something recognisable by eye.
The shape is **not** a BICEPS concept: the standard carries samples and says nothing about what they look like. The generators just make you see something without having to fill in too much dummy data by hand.

A distribution gets a drifting bell across its domain, which is the shape that makes one recognisable as a distribution rather than a signal.
Push your own 32-sample block instead and that metric comes off the generator, so what you set stays put. A distribution domain must have positive width.
`DistributionRange/StepWidth` is derived from that fixed 32-bin geometry and does not change when sample values change.
It is **not** `Resolution`: StepWidth is how far apart two samples sit along the domain, Resolution is how finely one sample value is measured.

### Why the trace moves smoothly

A waveform arrives in blocks — a quarter of a second of signal, four times a second. Drawing a block the moment it lands makes the trace lurch rather than move, which is unreadable if you are trying to follow a curve.

Sending smaller blocks would be legal — SDC exists to stream sample arrays and the report rate is nobody's business but the device's — but it is the wrong lever for this tool.
It multiplies SOAP messages and will make this a network problem.

Instead, the plot buffers what arrives and reveals it at the rate the samples were taken at, which is what `SamplePeriod` on the descriptor is *for*:
the standard tells a consumer how to place samples in time, and this is a consumer doing that. Two rules keep it honest:

- **Nothing is invented.** When the buffer runs dry the trace stops until the next block. A device that has gone quiet looks like one.
- **It cannot drift.** If the buffer runs more than 1.5 s long — a hiccup, or a peer sending faster than it declared — it drains faster than real time until the backlog is gone.
  A display that falls further behind every second is worse than a chunky one.

A distribution has no time base to pace against - the whole picture is replaced at once - so it eases from the old bar heights to the new ones instead.
Same purpose, different mechanism: a value that moves is readable where one that jumps is not.
The first block grows up from the floor, and a block arriving mid-move re-aims from wherever the bars have got to rather than queueing, because the newest picture is the true one.

The plot is painted by hand in `sdctoolbox/gui/widgets/plot.py`.
It is a polyline and a couple of guide lines, and pulling in a charting library for that would have been the largest dependency in the project by a wide margin.

## Alarms

BICEPS keeps two things apart that are easy to confuse:

|               |                                                                                                 |
|---------------|-------------------------------------------------------------------------------------------------|
| **condition** | the fact — "the pressure is too high". Has a kind and a priority, and is either present or not. |
| **signal**    | how that fact is announced — visually, audibly, or by vibration.                                |

One condition can drive several signals, which is why they are separate objects rather than flags on one. New alarms default to visual and audible signals; their manifestation and latching behavior can be configured independently.

The GUI allows alarm limits only for numeric sources.
For a decimal numeric source, limits make it a `LimitAlertCondition` that follows its source metric; without limits it stays a plain `AlertCondition` that only moves when you raise or clear it by hand.
The console and config loader currently accept limits for text and choice sources too, but those conditions never become present automatically.
Either way a value written by a remote consumer moves a supported numeric source exactly as a local edit does.

The Signals column is where the split stops being academic. Two buttons act on it:

| Button          | What it changes                               |
|-----------------|-----------------------------------------------|
| **Acknowledge** | each signal's `Presence`, from `On` to `Ack`  |
| **Stop latched** | each cleared latching signal's `Presence`, from `Latch` to `Off` |
| **Delegate**    | each signal's `Location`, from `Loc` to `Rem` |

Acknowledging is the interesting one, because it does **not** clear the alarm.
The condition keeps its `Presence`; only the announcement changes.
Watch the State and Signals columns while you do it — State stays `PRESENT` and Signals goes to `Vis:Ack Aud:Ack`.
Push the source further out of range and the acknowledgement survives, because the fact has not changed. Bring it back into range and out again, and it does not, because that is a new occurrence.

Delegation records that another device announces the signal instead.
BICEPS only permits it where the descriptor sets `SignalDelegationSupported`, which is the *Delegation* checkbox in the New alarm dialog, and nothing in sdc11073 enforces that — so the provider does.
Note what this is: it marks where the announcement belongs. It does not arrange for anybody to pick it up, which would need a second device offering a delegable signal of its own.

## Contexts

Contexts are the part of BICEPS that says *who* and *where*, as opposed to what the device is measuring. *Patient and location…* on the My device panel edits both.

They are worth a look because they behave unlike anything else in the MDIB:

- **They are multi-state.** Setting a patient does not overwrite the previous one.
  The old state is *disassociated* and kept, and a new one is associated, so the MDIB records who was attached when.
  Attach two patients in a row and look at `PC.mds0` in the consumer's containment tree: there are two states, one `Assoc` and one `Dis`.
- **The location is also a discovery scope.** Changing it re-announces the device, so a consumer filtering on the old location stops seeing it. The patient never leaves the MDIB.

Clearing every patient field detaches the patient rather than attaching a nameless one.

The patient editor supports name, sex, patient type, date of birth, height, weight and race from `pm:PatientDemographicsCoreData`. The Network panel and consumer console show associated peer patient contexts read-only.

Height and weight are BICEPS `Measurement` values: each needs a Decimal value and a coded measurement unit. Fresh providers start with Alex Example, a 170 cm height and a 70 kg weight, using MDC metric units. Race is a BICEPS `CodedValue`, not free text. The editor and JSON profile therefore require a code and coding system for each of those values. Use `mdc`, `private`, or an explicit coding-system URI, and use verified terminology when testing interoperability. The fields are informational demographics; a device's own measured height or weight should be modelled as a metric when quality and timing matter.

New exports use profile format version 3. Earlier profiles without these demographic fields or alert signal definitions remain readable.

An exported patient block has this shape:

```json
{
  "contexts": {
    "patient": {
      "given_name": "Ada",
      "height": {
        "value": "170.5",
        "unit": {"code": "<verified-unit-code>", "system": "<coding-system-uri>", "label": "cm"}
      },
      "weight": {
        "value": "72.4",
        "unit": {"code": "<verified-unit-code>", "system": "<coding-system-uri>", "label": "kg"}
      },
      "race": {"code": "<verified-race-code>", "system": "<coding-system-uri>", "label": "display label"}
    }
  }
}
```

## Not Supported at the Moment - What might cause Problems

This is a focused SDC learning fixture, not an IEEE 11073 conformance claim. IEEE 11073-10207 (BICEPS) defines a wider information and service model than one device must use; IEEE 11073-20701 and IEEE 11073-20702 define the surrounding SDC architecture and medical-device web-services profile. `sdc11073` supplies much of the DPWS/BICEPS wire plumbing, but a library capability is not automatically an end-to-end toolbox feature.

| Label                    | Meaning in this tool                                                                                                              |
|--------------------------|-----------------------------------------------------------------------------------------------------------------------------------|
| **Invisible**            | A normal two-toolbox demonstration still works, but valid peer information or a discovery feature is not surfaced by the toolbox. |
| **Might cause problems** | A valid device or deployment can use the capability, so a test can be incomplete, misleading or need manual recovery.             |
| **Will cause problems**  | A test or deployment that depends on the capability cannot succeed safely or correctly with the current toolbox.                  |

| Severity                 | Not supported or partial                                                                                                                                                                                                                                                                                | Practical consequence                                                                                                                                                                                                   |
|--------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Might cause problems** | Per-certificate, per-operation authorization and known-invoker audit identity are unavailable.                                                                                                                                                                                                           | TLS can admit or reject a participant by its trusted CA/certificate, but the current sdc11073 operation handler cannot apply distinct permissions or report the client certificate identity for each operation. |
| **Will cause problems**  | Remote control covers `SetValueOperation`, `SetStringOperation`, and argumentless `ActivateOperation` only. The toolbox does not publish or drive `SetContextState`, `SetAlertState`, `SetMetricState` or `SetComponentState` operations.                                                               | Valid state-changing workflows, actions requiring arguments, remote context association, and remote alert handling cannot be exercised end to end.                                                                      |
| **Might cause problems** | Contexts cover patient and location only. The Network panel shows associated peer patients read-only, but ensemble, workflow, means and operator contexts are not modelled or shown.                                                                                                                      | Tests involving care-team, workflow or multi-device context coordination need another fixture or direct access to the raw MDIB.                                                                                         |
| **Might cause problems** | The locally published alert model has one source metric per condition, local acknowledgement/delegation, and automatic limits only for decimal scalar values. Signals can use every standard manifestation and latching option. The consumer still displays peer source handles and signal manifestations. | Remote alert control, interoperable delegation, and limit conditions on text or choice sources cannot be exercised correctly.                                                                                 |
| **Might cause problems** | The provider is a small MDIB model: scalar writes set `Validity=Valid` and `ActivationState=On`, with no controls for quality, component state or lifecycle transitions.                                                                                                                                | It cannot simulate many degraded, unavailable, inactive or quality-qualified states a consumer may need to handle.                                                                                                      |
| **Might cause problems** | Sample arrays have no waveform annotations and use one non-real-time generator thread.                                                  | It is useful for basic streaming and display tests, not for timing or annotation conformance tests.                                                                                      |
| **Might cause problems** | The GUI keeps one active peer. When a peer changes MDIB sequence or instance identity, it disconnects and requires a manual reconnect.                                                                                                                                                                  | Multi-peer monitoring and automatic restart/reload recovery are not covered.                                                                                                                                            |
| **Might cause problems** | There is no declared conformance profile and no end-to-end test against an independent SDC implementation or product.                                                                                                                                                                                   | A passing repository suite demonstrates this toolbox talking to itself across processes, not product interoperability or standards conformance.                                                                         |
| **Invisible**            | The consumer gives detailed metric views only for the five BICEPS metric descriptor types it recognizes. Other entities and extensions remain in the generic containment tree or raw `RemoteDevice.mdib`.                                                                                               | A simple peer appears complete, while foreign extensions and most non-metric state are not available through the high-level UI/API.                                                                                     |
| **Invisible**            | The GUI takes the first inline concept description it finds. It does not select a language or use the peer's LocalizationService.                                                                                                                                                                       | The English labels in shipped presets look normal; localized or service-supplied text is not tested.                                                                                                                    |
| **Invisible**            | Discovery scans the selected IPv4 interface without a location-scope filter, but excludes the provider published by the same toolbox window by EPR.                                                                                                                                                                            | A scan can list unrelated SDC providers, so test selection must still be deliberate.                                                                                                                     |

## Tests

The pull-request workflow runs all 17 deterministic suites as separately reported Linux jobs
with `QT_QPA_PLATFORM=offscreen`:

| Suite | Covers |
|-------|--------|
| `diagnostics/check_api.py` | required sdc11073 API surface |
| `tests/workflow_security.py` | full-SHA action pins, release comments and Dependabot policy |
| `tests/diagnostic_behavior.py` | diagnostic signature and update-result reporting |
| `tests/security.py` | TLS policy, PEM validation, certificate metadata and fingerprint pins |
| `tests/application_defaults.py` | shared application defaults and distinct acceptance identity |
| `tests/import_bootstrap.py` | direct test imports under unrelated package shadowing |
| `tests/licensing.py` | project licensing, notices and build legal-payload sources |
| `tests/provider_core.py` | provider descriptors, values, alarms, contexts and rollback |
| `tests/presets.py` | every shipped preset built as a working device |
| `tests/config_roundtrip.py` | versioned export/import, validation and transactional replacement/append |
| `tests/widget_controls.py` | widget selection and real control interactions |
| `tests/gui_dialogs.py` | metric, alarm, context and startup validation |
| `tests/gui_cards_plots.py` | card construction and waveform/distribution rendering |
| `tests/gui_layout.py` | split/tab modes and responsive card reflow |
| `tests/gui_provider_structure.py` | provider action, alarm and operation structural refreshes |
| `tests/service_lifecycle.py` | provider and consumer startup fault cleanup |
| `tests/consumer_lifecycle.py` | window-close races, stale work and natural real-window shutdown |
| `tests/acceptance_readiness.py` | bounded provider readiness waits and subprocess cleanup |

The platform-sensitive `application_defaults.py`, `provider_core.py`,
`consumer_lifecycle.py`, and `acceptance_readiness.py` suites also run on Windows with Python
3.12. These Windows jobs use offscreen Qt and upload their captured subprocess, Qt, and test
output when they fail. A separate Windows job checks the console-rendered signal summary under
a strict `cp1252` encoding. The other deterministic suites and the network acceptance suite
are Linux-only; the native packaging smoke tests still cover both Windows and Linux artifacts.

Run any deterministic suite with the project interpreter, for example:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python.exe tests\gui_dialogs.py
.venv\Scripts\python.exe tests\gui_cards_plots.py
.venv\Scripts\python.exe tests\gui_layout.py
.venv\Scripts\python.exe tests\gui_provider_structure.py
.venv\Scripts\python.exe tests\consumer_lifecycle.py
```

`tests/acceptance_core.py` and `tests/tls_acceptance.py` are Linux-only network acceptance suites. CI runs each in an isolated job on every pull request, version tag, manual workflow dispatch and weekly schedule. The TLS suite generates a temporary self-signed test CA and participant certificates, then verifies HTTPS discovery, mutual authentication, MDIB retrieval and remote control without storing credentials in the repository. These checks exercise this implementation; they are not interoperability testing against an independent SDC stack or product.

`tests/gui_smoke.py` remains a manual broad regression script because it is intentionally long and duplicates the focused GUI suites while also starting a live peer. Run it when changing interactions that cross several GUI areas; it uses the offscreen backend and needs no display.

The packaging jobs separately launch each built Windows and Linux application with `--smoke-test`. `tests/acceptance_provider.py` is a subprocess fixture used by acceptance scripts, not a standalone suite.

`provider_core.py` is the odd one out: it is about what the MDIB must never be left in. Its first section deliberately writes a descriptor BICEPS cannot serialise *without* the rollback, watches the orphan appear, and only then checks that the guarded path leaves nothing behind — so a passing run means the check is still capable of failing.

## Security

The default remains plain `http://` for the smallest possible lab demonstration. Select
**Require TLS with client certificates** at startup, or give all of `--tls-cert`, `--tls-key`
and `--tls-ca`, to run SDC services and event callbacks with strict mutual TLS. Both
participants require their own PEM certificate/key and trust the CA bundle that issued the
other participant's certificate. TLS requires version 1.2 or newer, verifies the server
hostname/IP SAN, requires client certificates on both receiving endpoints, and refuses the
library's normal TLS-to-HTTP fallback.

For unattended use, an encrypted private key reads its password from
`SDC_TOOLBOX_TLS_KEY_PASSWORD`; the password is never accepted as a command-line argument or
written to a profile. The GUI prompts in its masked password field instead.

One self-signed test CA issuing a certificate for each toolbox process is the recommended
offline test setup. A directly self-signed participant certificate also works if it is put in
the other participant's trusted CA bundle. Self-signed does not mean accepting arbitrary
certificates: trust must remain explicit. Use `--tls-peer-fingerprint` to additionally pin
one expected SHA-256 leaf certificate.

Generate a self-contained local test CA plus provider and consumer identities with:

```powershell
.venv\Scripts\python.exe helpers\tls\generate_certificates.py
```

It writes ignored material under `helpers\tls\generated\`, protects private keys with a
prompted password by default, and creates a `README.txt` with the two exact startup commands.
Use `--ip <LAN-address>` when testing beyond loopback so the certificate IP SAN matches the
selected interface. See [`helpers/tls/README.md`](helpers/tls/README.md) for options and safety
rules.

The Network pane identifies discovered `HTTP` and `HTTPS` endpoints. Once connected over TLS,
it displays the peer subject, issuer and SHA-256 fingerprint; the core retains its serial
number, validity dates, SANs and EKUs for diagnostics. TLS mode is CA/certificate-based
**participant authorization**: a peer not accepted by the configured trust store cannot read,
subscribe or invoke operations.

WS-Discovery remains unsecured UDP multicast, so discovery data is not identity proof. The
HTTPS handshake and optional certificate pin are the trust decision.

The current `sdc11073` operation handler does not retain the inbound client's certificate
identity. The toolbox therefore cannot provide distinct per-certificate, per-operation
permissions or standards-conformant known-invoker audit reports. This is the upstream
unimplemented GLUE R0078 feature: [sdc11073 issue #490](https://github.com/Draegerwerk/sdc11073/issues/490).

## Using the core

```python
from decimal import Decimal
from sdctoolbox.model import (
    AlertSpec,
    Coding,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    PatientMeasurement,
)
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
    provider.set_patient(
        PatientInfo(
            given_name="Ada",
            family_name="Lovelace",
            height=PatientMeasurement(
                Decimal("170.5"),
                Coding("demo-cm", "private", "cm"),
            ),
        ),
    )

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
