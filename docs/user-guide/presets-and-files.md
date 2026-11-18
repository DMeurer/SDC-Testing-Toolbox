---
title: Presets and files
parent: User guide
nav_order: 11
---

# Presets and files
{: .no_toc }

1. TOC
{:toc}

## The shipped presets

Every preset is a BICEPS MDIB file in the `presets` folder. Load one with *File > Load preset*, pick
it in the start dialog, or start with `--config presets/patient-monitor.xml`.

| Preset | What it is | Worth looking at |
|---|---|---|
| Patient monitor | bedside vitals | ECG, plethysmogram and arterial pressure running together; almost every parameter has a standard code |
| Ventilator | airway pressure, flow and volume | three synchronised waveforms, and what a stream of sample arrays costs |
| Infusion pump | volumetric pump | the smallest device, and the easiest to read end to end |
| HF generator | electrosurgery | a bimodal impedance spectrum, and a *Stop output* action |
| Surgical microscope | robotic scope | six axes, fixpoint and free modes, fluorescence, and *Home axes*, none of which is a value you write |
| Endoscopic camera | camera and light source | image profiles, and a *White balance now* action with nothing to type |
| Insufflator | laparoscopic insufflator | nine metrics for gas flow and pressure control, with alarms |
| Operating table | focused example | height, Trendelenburg and lateral angle as read-only numbers with coded units |
| Ventilation pressure | focused example | a single pressure with MDC type and unit codes |
| Demo monitor, Demo infusion pump | small synthetic MDIBs | plain BICEPS files without toolbox behaviour |

The device presets carry a made-up default location and patient (*Alex Example*). They announce
themselves as the machine they describe: manufacturer, model name and model number are in the MDS
`MetaData`.

{: .tip }
Load a preset at startup (`--config` or the start dialog) rather than afterwards if you want the
device to announce that model. The model and manufacturer a device announces on the network are
fixed when it starts; loading a preset later replaces everything else.

## Importing and exporting

*File > Export config…* (**Ctrl+S**) saves My device as **one MDIB XML file with both the
MdDescription and the MdState**: the current values, sample blocks and all context states with
their history. It offers two forms:

- **MDIB with toolbox behaviour** (the default) also stores what SDC has no place for (see below),
  so the device loads back complete;
- **MDIB** writes plain BICEPS only, a minimal file for other tools.

*File > Import config…* (**Ctrl+O**) replaces My device with a file. It accepts a combined MDIB
(`GetMdibResponse`, a message `Mdib` or a participant `MdibContainer`) and the older JSON profiles.
Separate description and state files are not supported.

### What loading does

- It replaces the whole device and **restarts the provider**. Connected consumers must connect
  again. The device's name, its EPR, its network address and TLS settings stay as they are.
- It **changes nothing in the file's data**: no default patient or location is added, no alarm is
  recalculated, and nothing is generated unless the file's toolbox behaviour says so. Only the
  sequence id and the version counters are new.
- The announced manufacturer, model and serial number come from the MDS `MetaData`; the friendly
  name, firmware version and manufacturer URL from stored toolbox behaviour. A preset stores no
  serial number, so each instance keeps the one its name gives it.
- The loaded device is then edited like any other: add and remove metrics, alarms, actions and
  sections, switch remote control, set the patient and location.
- Imported alarms keep their state from the file until their source metric changes. Imported set
  operations keep their own enabled state and allowed ranges.

Before anything is replaced, the file is checked. Invalid XML, unsafe DTD or entity declarations,
broken references and content the library cannot keep are refused; a failed write never damages
an existing export file. Durations must use the hour/minute/second form, such as `PT5S`.

{: .warning }
A full MDIB export may contain patient data and context history. Review it before you share it.

## Toolbox behaviour

What a tool needs beyond SDC is stored in the file as `ext:Extension` elements in the namespace
`urn:sdc-testing-toolbox:behaviour`, marked `MustUnderstand="false"` so any other tool may ignore
them:

- which waveforms and distributions the generator drives, and their shapes;
- what each action sets, and its note;
- how each card looks: control type, step buttons, slider updates, ranges, legend, playback speed
  and text layout;
- the friendly name, firmware version, manufacturer URL, and the name of the preset it came from.

The toolbox reads these on load and removes them from the MDIB it publishes, so consumers never see
them. Extensions in other namespaces stay as they are.

## Older JSON profiles

Earlier versions saved a JSON profile: device metadata, data sources with their values, alarms,
actions, and the patient and location. These still load, with `--config` or *Import config…*
(choose *All files*), but the toolbox only writes MDIB files now.

A profile can replace the device or, from Python, be appended to it. Either way it is
transactional: if anything fails, the device goes back to how it was, and connected consumers see
the changes undone. Profiles carry a format version from 1 to 3; a profile without one is read as
version 1.
