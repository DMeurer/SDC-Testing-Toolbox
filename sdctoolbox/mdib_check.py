"""Checks an MDIB against what SDC asks beyond schema validity, in plain language.

A schema-valid MDIB can still leave a consumer guessing: a metric with no code, a value with
no time, an alarm nobody hears. Each finding says what is wrong and what it costs a consumer,
so a beginner learns from it and an agent can fix it.

Rules come from three places:

- The participant key purposes IEEE 11073-10700 (base) and -10701 (metrics). Their "shall"
  rules are errors, "should" rules warnings; the rule reference names the requirement.
- MDC coding: codes a consumer can recognise.
- Toolbox conventions (hints): not required, but each costs a consumer something.

Only what a snapshot shows is checked, never behaviour over time. Works on a ProviderMdib and
a ConsumerMdib alike: it reads descriptors and states the way the rest of the toolbox does.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sdc11073.xml_types import pm_qnames as pm

from . import constants, nomenclature
from .mdib_io import live_entities
from .native_mdib import label

LEVELS = ("error", "warning", "hint")
SOURCES = "IEEE 11073-10700 and -10701, MDC coding and toolbox conventions"

# IEEE 11073-10101 as 10700 TR1842 wants it named: this prefix and a version of 3 or more.
MDC_SYSTEM_PREFIX = "urn:oid:1.3.111.2.11073.10101."
# BICEPS's implied CodingSystem; it names 10101-2004, which 10700 TR1840 rules out.
LEGACY_MDC_SYSTEM = "urn:oid:1.2.840.10004.1.1.1.0.0.1"
# 10101 keeps the term codes from 0xF000 of each partition for private use.
PRIVATE_TERM_CODES = 0xF000

NUMERIC_AND_ARRAYS = frozenset({pm.NumericMetricDescriptor, pm.RealTimeSampleArrayMetricDescriptor,
                                pm.DistributionSampleArrayMetricDescriptor})
SAFETY_ORDER = ("Inf", "MedA", "MedB", "MedC")
MEDICAL = frozenset(SAFETY_ORDER[1:])
PRESENT_STATES = frozenset({"On", "StndBy", "Shtdn", "Fail"})


@dataclass(frozen=True)
class Fix:
    """A change the toolbox can make to its own MDIB that settles a finding."""

    label: str  # what the fix does, in a few words: "Use MDC_PULS_RATE"
    action: str  # coding | audible
    parts: tuple[str, ...] = ()  # coding: which of "type" and "unit" to change
    code: str | None = None  # coding: the new MDC code; None keeps the code and only names the current system


@dataclass(frozen=True)
class Finding:
    """One thing the check found: how bad, where, what and why, and the rule it rests on."""

    level: str  # error | warning | hint
    area: str  # Coding, Metric, Alarm, Remote control, Device, Context
    handle: str
    text: str
    rule: str | None = None
    label: str = ""
    fix: Fix | None = None

    @property
    def where(self) -> str:
        return self.handle if self.label in ("", self.handle) else f"{self.label} ({self.handle})"


def check(mdib) -> list[Finding]:  # noqa: ANN001 - a ProviderMdib or ConsumerMdib
    """Every finding for this MDIB, errors first."""
    with mdib.mdib_lock:
        findings = _Checker(live_entities(mdib, include_contexts=True)).run()
    return sorted(findings, key=lambda f: (LEVELS.index(f.level), f.area, f.handle))


def counts(findings: list[Finding]) -> dict[str, int]:
    return {level: sum(f.level == level for f in findings) for level in LEVELS}


def summary(findings: list[Finding]) -> str:
    """'2 errors · 1 warning · 0 hints', or 'No findings'."""
    if not findings:
        return "No findings"
    return " · ".join(f"{n} {level}{'' if n == 1 else 's'}" for level, n in counts(findings).items())


def _is_mdc(value) -> bool:  # noqa: ANN001 - a CodedValue
    """Whether a CodedValue names an MDC code: its system is MDC (or left out) and its code a number."""
    system = value.CodingSystem
    return (value.Code or "").isdigit() and (system in (None, LEGACY_MDC_SYSTEM) or system.startswith(MDC_SYSTEM_PREFIX))


def _match(value, what: str, descriptor) -> nomenclature.Term | None:  # noqa: ANN001
    """The MDC term that best matches the element's description or code, if any."""
    texts = [text.text for text in value.ConceptDescription or () if text.text] + [value.Code or ""]
    kind = "unit" if what == "unit" else "alert" if descriptor.is_alert_condition_descriptor else "measurement"
    unit_value = getattr(descriptor, "Unit", None) if kind == "measurement" else None
    unit = unit_value.Code if unit_value is not None and _is_mdc(unit_value) else None
    unit = None if unit == constants.CODE_DIMENSIONLESS else unit  # the default when no unit was picked
    for text in texts:
        for hit in nomenclature.search(text, kind) if text.strip() else []:
            usual = nomenclature.units_for(hit.code)
            # A term measured in other units is the wrong thing ("Flow rate" in L/min is no respiration rate).
            if not unit or not usual or unit in usual:
                return hit
    return None


def _exact(term: nomenclature.Term | None, value) -> nomenclature.Term | None:  # noqa: ANN001
    """The term only when the element's description names it outright, so a fix cannot guess wrong.

    The search also finds near misses ("Flow rate" finds respiration rate); those stay a hint.
    """
    if term is None:
        return None
    names = {term.term.casefold(), term.term.split(",")[0].strip().casefold(), term.symbol.casefold()} - {""}
    texts = {text.text.strip().casefold() for text in value.ConceptDescription or () if text.text}
    return term if names & texts else None


def _suggestion(term: nomenclature.Term | None) -> str:
    return f", perhaps {term.ref_id} ({term.code})" if term is not None else ""


def _v(value: object) -> object:
    """An sdc11073 enum's XML value; plain values unchanged."""
    return getattr(value, "value", value)


def has_value(metric_value: object) -> bool:
    """Whether a metric value carries a value or samples."""
    return getattr(metric_value, "Value", None) is not None or bool(getattr(metric_value, "Samples", None))


class _Checker:
    def __init__(self, entities: list) -> None:
        self.entities = entities
        self.by_handle = {entity.handle: entity for entity in entities}
        # An operation may target a context state, whose handle is not a descriptor's.
        self.known = set(self.by_handle) | {state.Handle for entity in entities for state in entity.states}
        self.findings: list[Finding] = []

    def add(self, level: str, area: str, entity, text: str, rule: str | None = None,  # noqa: ANN001
            fix: Fix | None = None) -> None:
        self.findings.append(Finding(level, area, entity.handle, text, rule, label(entity.descriptor), fix))

    def run(self) -> list[Finding]:
        signalled = defaultdict(list)  # condition handle -> the manifestations announcing it
        for e in self.entities:
            if e.node_type == pm.AlertSignalDescriptor:
                signalled[e.descriptor.ConditionSignaled].append(_v(e.descriptor.Manifestation))
        for entity in self.entities:
            self._coding(entity)
            descriptor = entity.descriptor
            if descriptor.is_metric_descriptor:
                self._metric(entity)
            elif descriptor.is_alert_condition_descriptor:
                self._condition(entity, signalled)
            elif entity.node_type == pm.AlertSignalDescriptor:
                self._signal(entity)
            elif descriptor.is_operational_descriptor:
                self._operation(entity)
            elif entity.node_type == pm.MdsDescriptor:
                self._mds(entity)
            elif descriptor.is_context_descriptor:
                self._context(entity)
            elif entity.node_type == pm.SystemContextDescriptor:
                self._system_context(entity)
        self._connector_numbers()
        return self.findings

    # -- helpers -----------------------------------------------------------------------

    def _ancestor(self, entity, node_type):  # noqa: ANN001, ANN202
        while entity is not None and entity.node_type != node_type:
            entity = self.by_handle.get(entity.parent_handle)
        return entity

    def _activation(self, entity) -> str:  # noqa: ANN001
        # BICEPS implies On when a state leaves ActivationState out.
        return _v(getattr(entity.state, "ActivationState", None)) or "On"

    # -- coding ------------------------------------------------------------------------

    def _coding(self, entity) -> None:  # noqa: ANN001, C901
        descriptor = entity.descriptor
        coded = {"type": getattr(descriptor, "Type", None)}
        if hasattr(descriptor, "Unit"):
            coded["unit"] = descriptor.Unit
        if coded["type"] is None and (descriptor.is_metric_descriptor or descriptor.is_alert_condition_descriptor
                                      or descriptor.is_operational_descriptor):
            self.add("warning", "Coding", entity, "Has no Type, so a consumer can only guess what it is from its "
                     "handle or label. A code lets a consumer recognise it and show or use it correctly.")
        problems: dict[tuple[str, str | None, str], list[str]] = defaultdict(list)
        # What settles a problem: an MDC term to use, or None for naming the current coding system.
        fixes: dict[tuple[str, str | None, str], nomenclature.Term | None] = {}
        for what, value in coded.items():
            if value is None:
                continue
            code, system = value.Code or "", value.CodingSystem
            mdc = system in (None, LEGACY_MDC_SYSTEM) or system.startswith(MDC_SYSTEM_PREFIX)
            if any(text.text and not text.Lang for text in value.ConceptDescription or ()):
                problems[("error", "10700 TR0277", "description has no language (Lang), so a consumer cannot pick "
                          "the text for its user's language")].append(what)
            if not mdc:
                if what == "unit" or descriptor.is_metric_descriptor or descriptor.is_alert_condition_descriptor:
                    why = "cannot convert or compare the value" if what == "unit" else "cannot recognise it"
                    term = _match(value, what, descriptor)
                    key = ("warning", "10207 R0008, see 10700 6.6.3",
                           f"uses the coding system {system}, which other devices do not know, so a consumer "
                           f"{why}; use an IEEE 11073-10101 (MDC) code where one exists{_suggestion(term)}")
                    problems[key].append(what)
                    if _exact(term, value) is not None:
                        fixes[key] = term
                continue
            if system is None:
                key = ("error", "10700 TR1842", "names IEEE 11073-10101 only by BICEPS's default coding system; "
                       f"set CodingSystem to {MDC_SYSTEM_PREFIX}3 (or later) so a consumer knows which "
                       "nomenclature version the code is from")
                problems[key].append(what)
                fixes[key] = None
            elif system == LEGACY_MDC_SYSTEM:
                key = ("error", "10700 TR1840, TR1842", f"uses the outdated coding system {system} (10101-2004); "
                       f"use {MDC_SYSTEM_PREFIX}3 (or later), which current consumers expect")
                problems[key].append(what)
                fixes[key] = None
            elif value.CodingSystemVersion:
                problems[("error", "10700 TR1814", "gives a CodingSystemVersion although the CodingSystem URI "
                          "already names the version; two places for one fact can disagree")].append(what)
            if not code.isdigit():
                term = _match(value, what, descriptor)
                key = ("warning", None, f"code {code!r} is not an MDC code: those are numbers (such as 147842 "
                       f"for heart rate), so a consumer will not find it in the nomenclature{_suggestion(term)}")
                problems[key].append(what)
                if _exact(term, value) is not None:
                    fixes[key] = term
            elif int(code) & 0xFFFF >= PRIVATE_TERM_CODES and not any(t.CodingSystem for t in value.Translation or ()):
                problems[("error", "10700 TR1358", f"code {code} is a private MDC code but no Translation names the "
                          "private coding system that defines it, so a consumer cannot look it up")].append(what)
        for key, whats in problems.items():
            level, rule, text = key
            fix = None
            if key in fixes:
                term = fixes[key]
                fix = (Fix("Use the current MDC coding system", "coding", tuple(whats)) if term is None
                       else Fix(f"Use {term.ref_id}", "coding", tuple(whats), term.code))
            self.add(level, "Coding", entity, f"Its {' and '.join(whats)} {text}.", rule, fix)

    # -- metrics -----------------------------------------------------------------------

    def _metric(self, entity) -> None:  # noqa: ANN001, C901, PLR0912
        d, state = entity.descriptor, entity.state
        value = getattr(state, "MetricValue", None)
        activation = self._activation(entity)
        category, availability = _v(d.MetricCategory), _v(d.MetricAvailability)
        validity = _v(getattr(getattr(value, "MetricQuality", None), "Validity", None))
        valued = has_value(value)
        missing_value = False
        if activation == "On" and value is None:
            missing_value = True
            self.add("error", "Metric", entity, "Is switched on but has no MetricValue, so a consumer has nothing "
                     "to show and cannot tell a missing value from a lost report.", "10701 TR0674")
        if validity in ("Vld", "Vldated") and not valued:
            missing_value = True
            self.add("error", "Metric", entity, "Says its value is valid but carries no value.", "10701 TR0675")
        if category in ("Set", "Preset") and activation in ("On", "StndBy") and not valued and not missing_value:
            self.add("error", "Metric", entity, "Is a setting but has no value, so a consumer cannot show what "
                     "is set.", "10701 TR0128")
        if valued and value.DeterminationTime is None:
            self.add("error", "Metric", entity, "Has a value without a DeterminationTime, so a consumer cannot tell "
                     "how old it is.", "10701 TR0214")
        if validity == "Ong" and value.StartTime is None:
            self.add("error", "Metric", entity, "Is being measured (validity Ong) but has no StartTime, so a "
                     "consumer cannot tell since when.", "10701 TR0216")
        if availability == "Cont" and validity == "Ong":
            self.add("error", "Metric", entity, "Is continuous but says a measurement is ongoing; a continuous "
                     "value is always there, so a consumer would wait for nothing.", "10701 TR0187")
        if availability == "Cont" and d.DeterminationPeriod is None:
            self.add("error", "Metric", entity, "Is continuous but has no DeterminationPeriod, so a consumer cannot "
                     "tell how often a new value comes or notice when updates stop.", "10701 TR0204")
        if category in ("Set", "Preset") and availability != "Intr":
            self.add("error", "Metric", entity, f"Is a {category} metric, which changes only when someone sets it, "
                     "so its MetricAvailability must be Intr (intermittent).", "10701 TR0136")
        mode = _v(getattr(getattr(value, "MetricQuality", None), "Mode", None))
        if mode in ("Demo", "Test"):
            mds = self._ancestor(entity, pm.MdsDescriptor)
            if mds is not None and _v(getattr(mds.state, "OperatingMode", None)) not in ("Dmo", "Srv", "Mtn"):
                self.add("error", "Metric", entity, f"Its value is {mode} data while the device says it runs "
                         "normally, so a consumer could take test values for real ones; set the MDS OperatingMode "
                         "to Dmo, Srv or Mtn.", "10701 TR0199")
        if entity.node_type in NUMERIC_AND_ARRAYS and not any(
                r.Lower is not None and r.Upper is not None for r in d.TechnicalRange or ()):
            self.add("error", "Metric", entity, "Has no TechnicalRange with both a lower and an upper end, so a "
                     "consumer cannot scale a display or spot an impossible value.", "10701 TR0239")
        if entity.node_type == pm.EnumStringMetricDescriptor:
            values = [allowed.Value for allowed in d.AllowedValue]
            if len(values) != len(set(values)):
                self.add("error", "Metric", entity, "Lists the same allowed value twice, so a consumer cannot tell "
                         "the two entries apart.", "10701 TR0487")
        if category in ("Msrmt", "Clc") and _v(d.SafetyClassification) in MEDICAL:
            if entity.node_type != pm.RealTimeSampleArrayMetricDescriptor and d.LifeTimePeriod is None \
                    and getattr(state, "LifeTimePeriod", None) is None:
                self.add("error", "Metric", entity, "Is medically relevant but has no LifeTimePeriod, so a consumer "
                         "cannot tell when a value is too old to use.", "10701 TR0210")
            if d.MaxDelayTime is None:
                self.add("error", "Metric", entity, "Is medically relevant but has no MaxDelayTime, so a consumer "
                         "cannot tell how late a value may arrive.", "10701 TR0682")
        channel = self.by_handle.get(entity.parent_handle)
        if channel is not None and channel.node_type == pm.ChannelDescriptor \
                and self._activation(channel) == "Off" and activation != "Off":
            self.add("error", "Metric", entity, "Its channel is switched off but it is not, which contradicts "
                     "itself for a consumer.", "10701 TR1075")
        if entity.node_type == pm.NumericMetricDescriptor and d.Resolution is None:
            self.add("hint", "Metric", entity, "Has no Resolution, so a consumer does not know how many digits "
                     "to show or how far apart two values must be to differ.")

    def _connector_numbers(self) -> None:
        """10700 TR1438: look-alike metrics in one channel need distinct connector numbers."""
        groups = defaultdict(list)
        for entity in self.entities:
            d = entity.descriptor
            if not d.is_metric_descriptor or d.Type is None or self._activation(entity) not in PRESENT_STATES:
                continue
            unit = getattr(d, "Unit", None)
            key = (entity.parent_handle, _v(d.MetricCategory), d.Type.Code, d.Type.CodingSystem,
                   getattr(unit, "Code", None), getattr(unit, "CodingSystem", None))
            groups[key].append(entity)
        for twins in groups.values():
            if len(twins) < 2:  # noqa: PLR2004
                continue
            numbers = [getattr(getattr(e.state, "PhysicalConnector", None), "Number", None) for e in twins]
            for entity, number in zip(twins, numbers, strict=True):
                if number is None or numbers.count(number) > 1:
                    others = ", ".join(e.handle for e in twins if e is not entity)
                    self.add("error", "Metric", entity, f"Has the same type, unit and category as {others} in the "
                             "same channel; without a distinct PhysicalConnector Number a consumer cannot tell "
                             "which is which.", "10700 TR1438")

    # -- alarms ------------------------------------------------------------------------

    def _condition(self, entity, signalled: dict) -> None:  # noqa: ANN001
        d = entity.descriptor
        for source in d.Source or ():
            if source not in self.by_handle:
                self.add("error", "Alarm", entity, f"Watches {source}, which is not in the MDIB, so a consumer "
                         "cannot show what the alarm is about.")
        manifestations = signalled.get(entity.handle)
        if not manifestations:
            self.add("hint", "Alarm", entity, "Has no alert signal, so nothing announces it; a consumer sees the "
                     "condition but not how the device itself would alert anyone.")
        elif _v(d.Priority) == "Hi" and "Aud" not in manifestations:
            self.add("hint", "Alarm", entity, "Is high priority but has no audible signal, so nobody who is not "
                     "looking at a screen notices it.", fix=Fix("Add an audible signal", "audible"))

    def _signal(self, entity) -> None:  # noqa: ANN001
        condition = entity.descriptor.ConditionSignaled
        if condition is not None and condition not in self.by_handle:
            self.add("error", "Alarm", entity, f"Announces {condition}, which is not in the MDIB, so a consumer "
                     "cannot tell what it signals.")

    # -- remote control ----------------------------------------------------------------

    def _operation(self, entity) -> None:  # noqa: ANN001
        target = entity.descriptor.OperationTarget
        if target not in self.known:
            self.add("error", "Remote control", entity, f"Acts on {target}, which is not in the MDIB, so a consumer "
                     "cannot tell what it changes.")
        if entity.node_type == pm.SetValueOperationDescriptor and not getattr(entity.state, "AllowedRange", None):
            self.add("hint", "Remote control", entity, "Has no AllowedRange, so a consumer learns what the device "
                     "accepts only by being refused; with it, a consumer can offer a fitting control.")

    # -- device ------------------------------------------------------------------------

    def _mds(self, entity) -> None:  # noqa: ANN001
        meta = entity.descriptor.MetaData
        missing = [name for name in ("Manufacturer", "ModelName", "ModelNumber", "SerialNumber")
                   if not getattr(meta, name, None)]
        if missing:
            self.add("hint", "Device", entity, f"MetaData has no {', '.join(missing)}, so a consumer cannot tell "
                     "which device this is, for example to tell two of the same model apart.", "10700 TR0251")
        clocks = [e for e in self.entities if e.node_type == pm.ClockDescriptor
                  and self._ancestor(e, pm.MdsDescriptor) is entity]
        if clocks:
            most = max((_v(e.descriptor.SafetyClassification) or "Inf" for e in self.entities
                        if self._ancestor(e, pm.MdsDescriptor) is entity), key=SAFETY_ORDER.index)
            for clock in clocks:
                if (_v(clock.descriptor.SafetyClassification) or "Inf") != most:
                    self.add("warning", "Device", clock, f"Its SafetyClassification should be {most}, like the "
                             "most critical part of the device: time stamps of that part rely on this clock.",
                             "10700 TR1590")

    # -- context -----------------------------------------------------------------------

    def _context(self, entity) -> None:  # noqa: ANN001
        binding = ("BindingMdibVersion", "BindingStartTime")
        unbinding = ("UnbindingMdibVersion", "BindingEndTime")
        for state in entity.states:
            association = _v(state.ContextAssociation)
            name = f"Context state {state.Handle}"
            if association == "Assoc":
                if not state.Identification:
                    self.add("error", "Context", entity, f"{name} is associated but has no Identification, so a "
                             "consumer cannot tell which patient or place it means.", "10700 TR1304")
                if any(getattr(state, n) is None for n in binding):
                    self.add("error", "Context", entity, f"{name} is associated but lacks BindingMdibVersion or "
                             "BindingStartTime, so a consumer cannot tell since when it applies.", "10700 TR1636")
                if any(getattr(state, n) is not None for n in unbinding):
                    self.add("error", "Context", entity, f"{name} is associated but already has an end "
                             "(UnbindingMdibVersion or BindingEndTime).", "10700 TR1637")
            elif association == "Dis" and any(getattr(state, n) is None for n in binding + unbinding):
                self.add("error", "Context", entity, f"{name} is disassociated but does not say both when it "
                         "started and when it ended, so a consumer cannot tell which data belonged to it.",
                         "10700 TR1638")
            elif association == "Pre" and any(getattr(state, n) is not None for n in binding + unbinding):
                self.add("error", "Context", entity, f"{name} is only pre-associated but already has binding "
                         "times or versions.", "10700 TR1639")

    def _system_context(self, entity) -> None:  # noqa: ANN001
        if self._activation(entity) != "Off":
            return
        for context in self.entities:
            if context.parent_handle == entity.handle and any(
                    _v(state.ContextAssociation) != "No" for state in context.states):
                self.add("error", "Context", context, "Its system context is switched off, yet it still has an "
                         "association, which a consumer would wrongly trust.", "10700 TR1959")
