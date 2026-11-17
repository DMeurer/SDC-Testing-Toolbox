"""What the toolbox window shows, for an agent explaining it to a beginner.

Kept next to the code it describes: when a button, column, icon or menu changes, change its
entry here too. TOPICS explains the window, CONCEPTS the SDC idea behind it.
"""

from __future__ import annotations

TOPICS: dict[str, tuple[str, str]] = {
    "window": ("The main window, its two tabs and the status bar", """\
# The main window

The toolbox is two things at once:

- **My device** (first tab): the SDC device this toolbox publishes. You build it here.
- **Network** (second tab): every other SDC device. You find, connect to and control them here.

*View > Split view* (F8) shows both side by side instead of as tabs.

The **status bar** at the bottom says *Publishing on <address>* and the device's EPR (its
network identity, `urn:uuid:...`). At its right:
- **MDIB check: 2 errors** (or *1 warning*, or *OK*): My device's live check result, updated
  by itself after every change. Click it to open the MDIB check (see *mdib check*).
- **Activity log** opens a window listing what happened: values other devices changed,
  actions they ran, alarms raised and cleared, on My device and on connected devices.
"""),
    "menus": ("The File and View menus", """\
# Menus

**File**
- *Import config...*: replace My device with an MDIB file. This restarts the provider, so
  connected devices must reconnect.
- *Export config...*: save My device as an MDIB file. *MDIB with toolbox behaviour* (the
  default) also stores what SDC has no place for (generated waveforms, action effects, card
  layout), so it loads back complete. *MDIB* writes plain SDC only, for other tools.
- *Check MDIB...* (Ctrl+K): the MDIB check window (see *mdib check*).
- *Load preset*: one of the shipped example devices.
- *Exit*.

**View**
- *Always show menu bar*: unticked, the menu bar hides until you press Alt.
- *Use widgets if possible* (F9): cards instead of table rows for metrics.
- *Split view* (F8): My device and Network side by side.
- *Fullscreen* (F11).
- *Dark mode*.
- *Advanced mode*: the controls for testing consumers (see *advanced mode*). The
  *Simple | Advanced* switch at the right of the tab strip does the same; the choice is
  remembered for the next start.
"""),
    "my device": ("The My device tab: buttons, info lines and sections", """\
# My device

**Buttons at the top**
- *New data source...*: create a metric (number, text, choice, waveform, distribution).
- *Patient and location...*: edit the patient and location contexts.

**Info lines**
- *Transport*: HTTP (lab only, unencrypted) or HTTPS with TLS.
- The patient and the location the device currently publishes.
- After loading an MDIB file: a note that it is edited like any device and that its alarms
  keep their supplied state until their source changes.

**Sections** (click a heading to fold it)
- *MDIB structure*: the device's description as a tree, with each element's handle.
  Hovering an entry shows its XML.
- *Metrics* (the chip counts them): one card or table row per data source.
- *Alarms* (chips: how many, and how many are active): the alarm table and its buttons.
- *Faults* (Advanced mode only; chips: *Advanced* and how many faults are on): simulated
  faults for testing consumers, see *faults*.

**Actions** row at the bottom: *New action...* and one button per action, which runs it.
"""),
    "network": ("The Network tab: discovery, connection tabs and what a connection shows", """\
# Network

**Discovery** section
- *Scan* searches the network for SDC devices; the chip says how many were found.
- *Connect* opens the selected device (or double-click it). *Disconnect* closes the current tab.
- *Connect next new device*: connect automatically to the next device that appears.
- Each row shows the device's EPR, a chip for its transport (HTTP or HTTPS) and, when it is
  open, a *Tab N* chip naming its connection tab.

**Connection tabs** (*1 · Insufflator*): one per connected device, each independent. The X
closes it; Ctrl+T adds an empty one.

A connected device shows:
- *Connected to <EPR>*, its transport and its patient.
- *MDIB structure*, *Metrics* and *Alarms* sections like My device, but read from the device.
  Metrics it will accept writes for have an editor; the others say *read-only*.
- *Actions*: a button per action the device offers.
- In table mode, an editor under the table: select a writable row, type, *Set on device*.

A write reports the device's own answer. *Fin* means done; *Fail* comes with the reason the
device gave, such as a value outside its allowed range.
"""),
    "cards": ("Metric cards: title, buttons, footer, and the control types", """\
# Metric cards

Each card shows one metric:
- **Title**: the metric's label.
- **Pencil**: card settings (My device: edit the data source and its card).
- **X** (My device only): remove the data source.
- **The control**, chosen to fit the metric (see below).
- **Footer**: the handle, the unit, and extras: *read-only* (no write allowed), *samples*
  (a waveform or distribution), *0.1s/sample* (sample period), *10 s per width* (how much time
  the plot shows), *over 0 to 50 Hz* (a distribution's domain).

**Control types** (the `control` card setting):
- `SliderWidget` (*Slider*): a number with a minimum and a maximum. *Update on* release sends
  when you let go; *change* sends while dragging. *Show ranges* labels both ends.
- `StepperWidget` (*Number input*): a number with -10, -1, +1, +10 buttons (small and large
  steps can be hidden).
- `ChoiceWidget` (*Dropdown*): a choice from the allowed values.
- `TextWidget` (*Text*): text with a *Set* button; *split* shows current and desired value,
  *multiline* allows several lines (Ctrl+Enter sends).
- `SampleArrayWidget` (*Plot*): a waveform as a moving trace, or a distribution as bars.
  *Legend* shows the axis values; *seconds per width* sets the time shown.
- `ReadoutWidget` (*Value only*): just the value, for any metric.
"""),
    "metric table": ("The metric tables (View > Use widgets unticked)", """\
# Metric tables

**My device** columns: Handle, Label, Kind, Value, Range, Unit, Remote control.
- *Value* is live and editable; *Set value* under the table sends it.
- *Remote control*: ticked means other devices may write the value; unticked, writes are
  refused (the set operation stays but is disabled). *n/a*: waveforms and distributions cannot
  be remote-controlled in SDC.
- A greyed row is a metric switched off: no generation, no remote sets, no alarms.

**Network** columns: Handle, Label, Kind, Value, Range, Unit, Writable.
- *Range* shows the allowed range for writes, or *technical:* the range the device can produce.
- *Writable* says whether the device offers an enabled set operation for it.
"""),
    "alarms": ("The alarm table, its columns and buttons", """\
# Alarms

**My device** columns: Handle, Label, Watches (the source metric), Raise when (the limits, or
*by hand*), Kind (Phy physiological, Tec technical, Oth other), Priority (Lo, Me, Hi), State
(*active* in red, or *clear*), Signals (icons, see *signal icons*).

**Network** columns: Handle, Label, Watches, Limits, Kind, Priority, Signals, State.

**Buttons** (My device, for the selected alarm)
- *New alarm...*, *Edit alarm...* (limits and priority), *Remove alarm*.
- *Raise* / *Clear*: set an alarm without limits by hand. An alarm with limits follows its
  source metric; change the value instead.
- *Acknowledge*: mark the signals as seen. The condition stays present: acknowledging changes
  how an alarm is announced, not whether it is true.
- *Stop latched*: stop signals that keep announcing after their condition cleared.
- *Remote\\** / *Local\\**: move the delegable signals' location between here (Loc) and a
  simulated remote location (Rem). The asterisk is there because it only demonstrates the
  Loc/Rem state; it does not hand the signal over to another device. Disabled when the
  alarm's signals are not delegable.
"""),
    "signal icons": ("The four icons in the Signals column", """\
# Signal icons

One icon per signal the alarm has, in this order:
- **Eye**: visible (Vis), such as a light or a flashing display.
- **Speaker**: audible (Aud), a tone.
- **Phone with vibration marks**: tangible (Tan), vibration.
- **Circle**: other (Oth).

An icon is **red** while its signal announces the condition (presence *On*, or *Latch* for a
latching signal after the condition cleared) and **grey** otherwise (*Off* or acknowledged).
Hover the column to see each signal's presence: *Ack* means acknowledged, *->Rem* a simulated
remote location.
"""),
    "mdib check": ("The MDIB check window and its status bar button", """\
# MDIB check

A window beside the main one (*File > Check MDIB...*, Ctrl+K, or the *MDIB check* button in
the status bar). It lists where an MDIB differs from what SDC asks beyond the XML schema, and
says for each what a consumer loses by it.

- **Check**: *My device*, or an open connection by its tab (*1 · Insufflator*). My device is
  checked again by itself after every change; a connection only on *Check again*.
- **Summary**: *2 errors · 1 warning · 3 hints. Checked against ...*
- **Columns**: *Level*, *Where* (label and handle), *Finding* (what is wrong, why it matters,
  and the rule in brackets, such as *10701 TR0204*).
- **Levels**: *error* (red) breaks a "shall" rule of IEEE 11073-10700 (base) or -10701
  (metrics), or points at an element that does not exist; *warning* (orange) breaks a
  "should" rule or an MDC coding rule; *hint* (grey) is a toolbox convention that would make
  the device easier to use, such as a resolution or an allowed range.
- **Double-click** a finding to show its element: its card or table row, its alarm, or its
  entry in the MDIB structure tree. For a connection, its tab comes to the front.

The status bar button shows My device's result: *MDIB check: 2 errors*, else the warnings,
else *OK* (hints alone count as OK). Agents get the same list from the `check_mdib` tool.
"""),
    "dialogs": ("New data source, Edit, New alarm, New action, Patient and location", """\
# Dialogs

- **New data source**: label, kind, unit, range and resolution for numbers, allowed values for
  choices, sample period and shape for waveforms, domain for distributions, a section
  (channel), and *remote control* for numbers, texts and choices.
- **Edit data source** (pencil on My device): what may change while the device runs: the
  allowed range or values for writes, remote control, the waveform curve, on/off, and the card.
- **New alarm**: the metric to watch, label, kind, priority, limits (numbers only), the
  signals (manifestation and latching) and whether they are delegable.
- **New action**: label, what it acts on (the device, a unit or a channel), and the values it
  sets when run.
- **Patient and location**: name, sex, type, date of birth, height, weight and race; facility,
  building, floor, point of care, room and bed. Clearing every patient field detaches the
  patient.
- **Pick an MDC code**: opened by the *Pick…* buttons beside Measures and Unit in New data
  source; see the topic "code picker".
"""),
    "code picker": ("Pick an MDC code: choosing IEEE 11073-10101 codes by name", """# Pick an MDC code

New data source has two rows for codes: **Measures** (what the metric is, its Type) and
**Unit**. Each has a *Pick…* button that opens this dialog; picking fills the field with the
term, its reference ID and its code, e.g. `Pulse rate · MDC_PULS_RATE (149514)`, and the
data source gets that code in the IEEE 11073-10101 (MDC) coding system. Without a pick the
type is a private code made from the label, and a typed unit is a label only.

- **Look for**: What is measured, Units, Alarm codes or Everything. Preselected by the
  *Pick…* that opened it.
- **Search**: filters live by name, reference ID or code; the best matches come first.
- **Code | Reference ID | Term**: the context-free code ((partition << 16) + term code), the
  standard's reference ID and a short name in the toolbox's own words. Double-click picks.
- **Load codes.csv…**: the toolbox ships a few hundred common codes. Your own export of the
  standard (helpers/Coding Systems/export_11073_tables.py) adds the rest; the file is
  remembered and loads again next time. A file that cannot be used is reported and ignored.

Over MCP, search_codes does the same search; pass a result's {code, system, label} as the
`type` or `unit` of add_metric.
"""),
    "advanced mode": ("Simple and Advanced mode: what Advanced mode adds", """\
# Advanced mode

The toolbox starts in **Simple** mode, for learning SDC. **Advanced** mode (View > Advanced
mode, or the *Simple | Advanced* switch at the right of the tab strip) adds controls for
people testing their own SDC consumer. Nothing Simple mode shows is hidden in Advanced mode.

It adds, on My device:
- the **Faults** section under Alarms (see *faults*),
- a chip next to a card's title naming its fault, such as *StndBy*, *Qst* or *Fail*.

The mode is a view setting only: faults set over MCP work in either mode, and stay on when
you switch back to Simple.
"""),
    "faults": ("The Faults section: simulated faults and what a consumer sees", """\
# Faults

States that go wrong, so you can see how a consumer copes. Shown in Advanced mode.

**Device-wide** (buttons; a tinted one is on; runtime only, never saved):
- *Answer after ... ms*: wait 250 to 5000 ms before answering Set, Activate, GetMdib,
  GetMdState and GetMdDescription requests. Tests timeouts.
- *Refuse every write*: every set and activate is answered *Fail*, with a reason saying it is
  a simulated fault.
- *Pause reports*: no state or waveform reports go out (descriptor changes and operation
  reports still do). Switched off, every current state is reported once so consumers catch up.
- *Drop subscriptions*: forgets every subscription now without telling the subscriber (no
  SubscriptionEnd). Reports stop; the consumer finds out when its Renew fails.
- *Leave without Bye*: vanishes like a crashed device: no Bye, no answer to Probe or Resolve,
  every request dropped unanswered, subscriptions forgotten. Switched off, the device comes
  back with a Hello and consumers must connect again.
- *Reset all faults*: ends every fault, device-wide and per metric.

**Per metric** (table: Handle, Label, Activation, Validity, Mode, What consumers see). These
are real MDIB state, stay on every value written later (by you, a consumer, an action or the
generator) and are saved in MDIB exports. A value differing from normal is in the accent colour.
- *Activation* (ActivationState): *On* normal; *StndBy* sensor in standby, *NotRdy* not ready
  yet, *Fail* failed, *Shtdn* shutting down: a consumer should show no value; generated
  waveforms stop. *Off* is the ordinary switch.
- *Validity* (MetricQuality/@Validity): *Vld* valid; *Vldated* validated by a clinician; *Ong*
  measurement still going on; *Qst* questionable; *Calib* taken while calibrating; *Inv*
  invalid, do not use; *Oflw* / *Uflw* above / below the measuring range; *NA* not available.
- *Mode* (MetricQuality/@Mode): *Real* from a real measurement; *Test* test data; *Demo* demo
  data (normal for generated waveforms and distributions).
"""),
    "startup": ("The start dialog and command line", """\
# Starting the toolbox

Without arguments it asks: the device name (decides its EPR), the network address to bind
discovery to (each with its adapter name; link-local 169.254.x.x addresses are dead ends), an
optional config file or preset, a friendly name, and verbose logging.

Command line: `--name`, `--ip`, `--config`, `--friendly-name`, `--theme light|dark`,
`--fullscreen`, `--mcp [PORT]` (this MCP server), `--headless` (no window; needs --mcp).
"""),
}

CONCEPTS: dict[str, tuple[str, str]] = {
    "sdc": ("What SDC is", """\
# SDC

IEEE 11073 SDC (Service-oriented Device Connectivity) lets medical devices from different
manufacturers find each other on a network, describe themselves, exchange live data and
control each other. Three standards: 10207 (BICEPS, the data and service model), 20702
(MDPWS, the web-services transport) and 20701 (how the two fit together, plus discovery).
A **provider** publishes a device; a **consumer** connects to one. The toolbox is both.
"""),
    "mdib": ("The MDIB, MdDescription and MdState", """\
# MDIB

The Medical Device Information Base is a device's complete self-description:
- **MdDescription**: what the device *is*, as a containment tree: MDS (the device) > VMD (a
  functional unit) > Channel (a group) > Metric (one value), plus the SCO (operations), the
  alert system and the system context (patient, location).
- **MdState**: what everything *is right now*: values, alarm presences, operation modes.
Every element has a **handle**, a stable id unique in the device.
"""),
    "metric kinds": ("The five metric types", """\
# Metric kinds

- `NumericMetric` (*number*): a value with a unit and a resolution. No integer type: whole
  numbers have resolution 1.
- `StringMetric` (*text*).
- `EnumStringMetric` (*choice*): one of a fixed list. No boolean type: use two allowed values.
- `RealTimeSampleArrayMetric` (*waveform*): samples over time, streamed.
- `DistributionSampleArrayMetric` (*distribution*): samples over a domain, like a spectrum.
**TechnicalRange** is what the device can produce; **AllowedRange** (on the set operation's
state) is what a consumer may request.
"""),
    "operations": ("Remote control: set operations and actions", """\
# Operations

A value is not writable by a flag on it: a separate **set operation** in the SCO points at the
metric (`OperationTarget`). Its **OperatingMode** (enabled or disabled) can change at runtime.
An **ActivateOperation** is an action with nothing to write: press, and the device does
something. A request answers with an **InvocationState**: Wait, Start, then Fin (done), FinMod
(done, modified) or Fail with an error. The device decides; it may refuse.
"""),
    "alarms": ("Alert conditions and alert signals", """\
# Alarms

- **AlertCondition**: the fact ("pressure too high"), with a kind (physiological, technical,
  other) and a priority. A **LimitAlertCondition** watches a metric against limits by itself.
- **AlertSignal**: how the fact is announced (visible, audible, tangible, other). One condition
  can drive several signals. Acknowledging changes the signals, not the condition. A
  **latching** signal keeps announcing after the condition clears until it is stopped.
  **Delegation** lets another device announce a signal.
"""),
    "contexts": ("Patient and location", """\
# Contexts

Context states say *who* and *where*. They are multi-state: a new patient disassociates the
old one, which is kept, so the MDIB records who was attached when. The location is also a
discovery scope, so changing it re-announces the device.
"""),
    "discovery": ("Finding devices: WS-Discovery and EPRs", """\
# Discovery

Devices announce themselves by multicast (Hello), answer searches (Probe) and say goodbye
(Bye). No registry or broker. Each device has an **EPR** (endpoint reference, `urn:uuid:...`)
that stays the same across restarts. Discovery binds to one network address; a laptop has
many, so pick the one the other device can reach.
"""),
    "toolbox behaviour": ("What the toolbox stores beyond SDC", """\
# Toolbox behaviour

An MDIB export *with toolbox behaviour* adds `ext:Extension` elements in the namespace
`urn:sdc-testing-toolbox:behaviour`, marked `MustUnderstand="false"` so any other tool may
ignore them: which waveforms the generator drives and their shape, what each action sets,
how each card looks, and the device's friendly name, firmware and preset name. The toolbox
reads and removes them on load; the published MDIB never contains them.
"""),
}


def lookup(entries: dict[str, tuple[str, str]], query: str | None, kind: str) -> str:
    """The entry named `query`, else the entries whose words match, else the list of topics."""
    listing = "\n".join(f"- **{name}**: {summary}" for name, (summary, _text) in entries.items())
    if not query or not query.strip():
        return f"# {kind} topics\n\n{listing}\n\nAsk again with one of these as the topic."
    wanted = query.strip().lower()
    if wanted in entries:
        return entries[wanted][1]
    words = [word for word in wanted.replace("-", " ").split() if len(word) > 2]
    scored = sorted(
        ((sum(text.lower().count(word) + 5 * name.count(word) for word in words), name)
         for name, (_summary, text) in entries.items()),
        reverse=True,
    )
    hits = [name for score, name in scored[:2] if score > 0]
    if not hits:
        return f"Nothing about {query!r}. {kind} topics:\n\n{listing}"
    return "\n\n".join(entries[name][1] for name in hits)
