"""The MDIB check: each rule fires on a small MDIB that breaks it and stays quiet on one that does not.

Usage:  .venv/Scripts/python.exe tests/mdib_check.py
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mdib_test_support import MSG, PM, XSI  # noqa: E402

from sdctoolbox import mdib_check  # noqa: E402
from sdctoolbox.mdib_io import parse_mdib  # noqa: E402

MDC = 'CodingSystem="urn:oid:1.3.111.2.11073.10101.3"'
# One of everything, each written the way SDC asks for it, so the check finds nothing.
CLEAN = f'''<msg:GetMdibResponse xmlns:msg="{MSG}" xmlns:pm="{PM}" xmlns:xsi="{XSI}"
    SequenceId="urn:uuid:22222222-2222-2222-2222-222222222222" MdibVersion="5">
  <msg:Mdib SequenceId="urn:uuid:22222222-2222-2222-2222-222222222222" MdibVersion="5">
    <pm:MdDescription>
      <pm:Mds Handle="mds">
        <pm:Type Code="69650" {MDC}><pm:ConceptDescription Lang="en">Monitor</pm:ConceptDescription></pm:Type>
        <pm:AlertSystem Handle="alerts">
          <pm:AlertCondition Handle="hr.high" Kind="Phy" Priority="Hi"><pm:Type Code="196674" {MDC}/><pm:Source>hr</pm:Source></pm:AlertCondition>
          <pm:AlertSignal Handle="hr.high.aud" ConditionSignaled="hr.high" Manifestation="Aud" Latching="false"><pm:Type Code="196674" {MDC}/></pm:AlertSignal>
        </pm:AlertSystem>
        <pm:Sco Handle="sco">
          <pm:Operation Handle="rate.op" xsi:type="pm:SetValueOperationDescriptor" OperationTarget="rate"><pm:Type Code="128" {MDC}/></pm:Operation>
        </pm:Sco>
        <pm:MetaData><pm:Manufacturer>Example</pm:Manufacturer><pm:ModelName>Monitor</pm:ModelName><pm:ModelNumber>1</pm:ModelNumber><pm:SerialNumber>42</pm:SerialNumber></pm:MetaData>
        <pm:SystemContext Handle="ctx"><pm:LocationContext Handle="loc"/></pm:SystemContext>
        <pm:Clock Handle="clock"/>
        <pm:Vmd Handle="vmd"><pm:Channel Handle="ch">
          <pm:Metric Handle="hr" xsi:type="pm:NumericMetricDescriptor" MetricCategory="Msrmt" MetricAvailability="Cont" Resolution="1" DeterminationPeriod="PT1S">
            <pm:Type Code="147842" {MDC}><pm:ConceptDescription Lang="en">Heart rate</pm:ConceptDescription></pm:Type>
            <pm:Unit Code="264864" {MDC}/><pm:TechnicalRange Lower="0" Upper="300"/>
          </pm:Metric>
          <pm:Metric Handle="rate" xsi:type="pm:NumericMetricDescriptor" MetricCategory="Set" MetricAvailability="Intr" Resolution="1">
            <pm:Type Code="151586" {MDC}/><pm:Unit Code="264928" {MDC}/><pm:TechnicalRange Lower="0" Upper="60"/>
          </pm:Metric>
          <pm:Metric Handle="mode" xsi:type="pm:EnumStringMetricDescriptor" MetricCategory="Set" MetricAvailability="Intr">
            <pm:Type Code="184352" {MDC}/><pm:Unit Code="262656" {MDC}/>
            <pm:AllowedValue><pm:Value>A</pm:Value></pm:AllowedValue><pm:AllowedValue><pm:Value>B</pm:Value></pm:AllowedValue>
          </pm:Metric>
        </pm:Channel></pm:Vmd>
      </pm:Mds>
    </pm:MdDescription>
    <pm:MdState>
      <pm:State DescriptorHandle="mds" xsi:type="pm:MdsState"/>
      <pm:State DescriptorHandle="ch" xsi:type="pm:ChannelState" ActivationState="On"/>
      <pm:State DescriptorHandle="ctx" xsi:type="pm:SystemContextState" ActivationState="On"/>
      <pm:State DescriptorHandle="loc" Handle="loc.now" xsi:type="pm:LocationContextState" ContextAssociation="Assoc" BindingMdibVersion="1" BindingStartTime="1000"><pm:Identification Root="urn:example" Extension="bed-1"/></pm:State>
      <pm:State DescriptorHandle="rate.op" xsi:type="pm:SetValueOperationState" OperatingMode="En"><pm:AllowedRange Lower="5" Upper="40"/></pm:State>
      <pm:State DescriptorHandle="hr" xsi:type="pm:NumericMetricState" ActivationState="On"><pm:MetricValue Value="72" DeterminationTime="1000"><pm:MetricQuality Validity="Vld"/></pm:MetricValue></pm:State>
      <pm:State DescriptorHandle="rate" xsi:type="pm:NumericMetricState" ActivationState="On"><pm:MetricValue Value="12" DeterminationTime="1000"><pm:MetricQuality Validity="Vld"/></pm:MetricValue></pm:State>
      <pm:State DescriptorHandle="mode" xsi:type="pm:EnumStringMetricState" ActivationState="On"><pm:MetricValue Value="A" DeterminationTime="1000"><pm:MetricQuality Validity="Vld"/></pm:MetricValue></pm:State>
    </pm:MdState>
  </msg:Mdib>
</msg:GetMdibResponse>'''

HR_VALUE = '<pm:MetricValue Value="72" DeterminationTime="1000"><pm:MetricQuality Validity="Vld"/></pm:MetricValue>'
RATE_VALUE = '<pm:MetricValue Value="12" DeterminationTime="1000"><pm:MetricQuality Validity="Vld"/></pm:MetricValue>'
HR_DESCRIPTOR = 'Handle="hr" xsi:type="pm:NumericMetricDescriptor" MetricCategory="Msrmt" MetricAvailability="Cont" Resolution="1" DeterminationPeriod="PT1S"'
HR_UNIT = f'<pm:Unit Code="264864" {MDC}/>'
LOCATION = ('ContextAssociation="Assoc" BindingMdibVersion="1" BindingStartTime="1000">'
            '<pm:Identification Root="urn:example" Extension="bed-1"/>')


def check(*replacements: tuple[str, str]) -> list[mdib_check.Finding]:
    xml = CLEAN
    for old, new in replacements:
        assert old in xml, old
        xml = xml.replace(old, new)
    return mdib_check.check(parse_mdib(xml.encode()))


def rules(findings: list[mdib_check.Finding]) -> set[tuple[str | None, str]]:
    return {(finding.rule, finding.handle) for finding in findings}


class RuleTests(unittest.TestCase):
    def assert_finds(self, rule: str | None, handle: str, *replacements: tuple[str, str], level: str = "error") -> None:
        found = [f for f in check(*replacements) if f.rule == rule and f.handle == handle]
        self.assertTrue(found, f"{rule} on {handle} not found in {rules(check(*replacements))}")
        self.assertEqual(found[0].level, level)
        self.assertTrue(found[0].text.endswith("."), found[0].text)

    def test_explanations_name_real_types_and_the_checks_rules(self) -> None:
        import re

        from sdc11073.xml_types import pm_qnames

        from sdctoolbox.mdib_explain import EXPLANATIONS, explain

        source = (ROOT / "sdctoolbox" / "mdib_check.py").read_text(encoding="utf-8")
        for node_type, (_what, _consumer, rules) in EXPLANATIONS.items():
            self.assertTrue(hasattr(pm_qnames, node_type), node_type)
            for rule in re.findall(r"(?:TR|R)\d{4}", rules):
                self.assertIn(rule, source, f"{node_type} cites {rule}, which the MDIB check does not apply")
        tip = explain("NumericMetricDescriptor", "m.<rate>")
        self.assertIn("NumericMetricDescriptor · m.&lt;rate&gt;", tip)
        self.assertIn("Rules: 10701 TR0239", tip)
        self.assertIn("A NoSuchDescriptor element", explain("NoSuchDescriptor", "x"))

    def test_a_clean_mdib_has_no_findings(self) -> None:
        self.assertEqual(check(), [])
        self.assertEqual(mdib_check.summary([]), "No findings")

    def test_metric_rules(self) -> None:
        cases = [
            ("10701 TR0674", "hr", (HR_VALUE, "")),
            ("10701 TR0675", "hr", (HR_VALUE, '<pm:MetricValue DeterminationTime="1000"><pm:MetricQuality Validity="Vld"/></pm:MetricValue>')),
            ("10701 TR0214", "hr", (HR_VALUE, '<pm:MetricValue Value="72"><pm:MetricQuality Validity="Vld"/></pm:MetricValue>')),
            ("10701 TR0216", "rate", (RATE_VALUE, '<pm:MetricValue><pm:MetricQuality Validity="Ong"/></pm:MetricValue>')),
            ("10701 TR0187", "hr", (HR_VALUE, '<pm:MetricValue StartTime="900"><pm:MetricQuality Validity="Ong"/></pm:MetricValue>')),
            ("10701 TR0204", "hr", (' DeterminationPeriod="PT1S"', "")),
            ("10701 TR0136", "rate", ('MetricCategory="Set" MetricAvailability="Intr" Resolution="1"',
                                      'MetricCategory="Set" MetricAvailability="Cont" Resolution="1" DeterminationPeriod="PT1S"')),
            ("10701 TR0128", "rate", (RATE_VALUE, '<pm:MetricValue><pm:MetricQuality Validity="Inv"/></pm:MetricValue>')),
            ("10701 TR0199", "hr", ('Validity="Vld"/></pm:MetricValue></pm:State>\n      <pm:State DescriptorHandle="rate"',
                                    'Validity="Vld" Mode="Demo"/></pm:MetricValue></pm:State>\n      <pm:State DescriptorHandle="rate"')),
            ("10701 TR0239", "hr", ('<pm:TechnicalRange Lower="0" Upper="300"/>', "")),
            ("10701 TR0239", "hr", ('<pm:TechnicalRange Lower="0" Upper="300"/>', '<pm:TechnicalRange Lower="0"/>')),
            ("10701 TR0487", "mode", ("<pm:Value>B</pm:Value>", "<pm:Value>A</pm:Value>")),
            ("10701 TR0210", "hr", (HR_DESCRIPTOR, HR_DESCRIPTOR + ' SafetyClassification="MedA"')),
            ("10701 TR0682", "hr", (HR_DESCRIPTOR, HR_DESCRIPTOR + ' SafetyClassification="MedA"')),
            ("10701 TR1075", "hr", ('"pm:ChannelState" ActivationState="On"', '"pm:ChannelState" ActivationState="Off"')),
        ]
        for rule, handle, replacement in cases:
            with self.subTest(rule):
                self.assert_finds(rule, handle, replacement)

    def test_metric_rules_stay_quiet_when_met(self) -> None:
        medical = check((HR_DESCRIPTOR, HR_DESCRIPTOR + ' SafetyClassification="MedA" LifeTimePeriod="PT5S" MaxDelayTime="PT1S"'),
                        ('<pm:Clock Handle="clock"/>', '<pm:Clock Handle="clock" SafetyClassification="MedA"/>'))
        self.assertEqual(medical, [])
        demo = check(('<pm:State DescriptorHandle="mds" xsi:type="pm:MdsState"/>',
                      '<pm:State DescriptorHandle="mds" xsi:type="pm:MdsState" OperatingMode="Dmo"/>'),
                     ('Validity="Vld"/></pm:MetricValue></pm:State>\n      <pm:State DescriptorHandle="rate"',
                      'Validity="Vld" Mode="Demo"/></pm:MetricValue></pm:State>\n      <pm:State DescriptorHandle="rate"'))
        self.assertEqual(demo, [])
        both_off = check(('"pm:ChannelState" ActivationState="On"', '"pm:ChannelState" ActivationState="Off"'),
                         *[(f'<pm:State DescriptorHandle="{h}" xsi:type="pm:{k}MetricState" ActivationState="On">',
                            f'<pm:State DescriptorHandle="{h}" xsi:type="pm:{k}MetricState" ActivationState="Off">')
                           for h, k in (("hr", "Numeric"), ("rate", "Numeric"), ("mode", "EnumString"))])
        self.assertEqual(both_off, [])

    def test_twin_metrics_need_connector_numbers(self) -> None:
        twin = HR_DESCRIPTOR.replace('Handle="hr"', 'Handle="hr2"')
        descriptor = (f'<pm:Metric {twin}><pm:Type Code="147842" {MDC}/>{HR_UNIT}'
                      '<pm:TechnicalRange Lower="0" Upper="300"/></pm:Metric>')
        state = f'<pm:State DescriptorHandle="hr2" xsi:type="pm:NumericMetricState">{HR_VALUE}</pm:State>'
        add = [("</pm:Channel>", descriptor + "</pm:Channel>"), ("</pm:MdState>", state + "</pm:MdState>")]
        self.assertLessEqual({("10700 TR1438", "hr"), ("10700 TR1438", "hr2")}, rules(check(*add)))
        numbered = [*add, ('<pm:State DescriptorHandle="hr" xsi:type="pm:NumericMetricState" ActivationState="On">',
                           '<pm:State DescriptorHandle="hr" xsi:type="pm:NumericMetricState" ActivationState="On">'
                           '<pm:PhysicalConnector Number="1"/>'),
                    (state, state.replace(">" + HR_VALUE, '><pm:PhysicalConnector Number="2"/>' + HR_VALUE))]
        self.assertEqual(check(*numbered), [])

    def test_coding_rules(self) -> None:
        cases = [
            ("10700 TR1842", "hr", (HR_UNIT, '<pm:Unit Code="264864"/>')),
            ("10700 TR1840, TR1842", "hr", (HR_UNIT, '<pm:Unit Code="264864" CodingSystem="urn:oid:1.2.840.10004.1.1.1.0.0.1"/>')),
            ("10700 TR1814", "hr", (HR_UNIT, f'<pm:Unit Code="264864" {MDC} CodingSystemVersion="3"/>')),
            ("10700 TR1358", "hr", ('<pm:Type Code="147842"', '<pm:Type Code="61441"')),
            ("10700 TR0277", "hr", ('<pm:ConceptDescription Lang="en">Heart rate', "<pm:ConceptDescription>Heart rate")),
        ]
        for rule, handle, replacement in cases:
            with self.subTest(rule):
                self.assert_finds(rule, handle, replacement)
        self.assert_finds("10207 R0008, see 10700 6.6.3", "hr",
                          ('<pm:Type Code="147842" ' + MDC, '<pm:Type Code="hr" CodingSystem="urn:example:private"'),
                          level="warning")
        self.assert_finds("10207 R0008, see 10700 6.6.3", "hr", (HR_UNIT, '<pm:Unit Code="bpm" CodingSystem="urn:example:units"/>'),
                          level="warning")
        private = ('<pm:Type Code="147842"', '<pm:Type Code="61441"')
        translated = ("Heart rate</pm:ConceptDescription>",
                      'Heart rate</pm:ConceptDescription><pm:Translation Code="hr" CodingSystem="urn:example:private"/>')
        self.assertEqual(check(private, translated), [])

    def test_coding_conventions(self) -> None:
        no_type = check((f'<pm:Type Code="151586" {MDC}/>', ""))
        self.assertIn((None, "rate"), rules(no_type))
        self.assertTrue(any("no Type" in f.text and f.level == "warning" for f in no_type))
        symbolic = check(('<pm:Type Code="147842"', '<pm:Type Code="MDC_ECG_HEART_RATE"'))
        self.assertTrue(any("not an MDC code" in f.text for f in symbolic), symbolic)
        private_unit = check((HR_UNIT, '<pm:Unit Code="bpm" CodingSystem="urn:example:units"/>'))
        self.assertTrue(any("perhaps MDC_DIM_BEAT_PER_MIN (264864)" in f.text for f in private_unit), private_unit)
        flow = check(('<pm:Type Code="147842" ' + MDC + '><pm:ConceptDescription Lang="en">Heart rate',
                      '<pm:Type Code="flow" CodingSystem="urn:example:types"><pm:ConceptDescription Lang="en">Flow rate'),
                     (HR_UNIT, f'<pm:Unit Code="265216" {MDC}/>'))
        flow_hints = [f.text for f in flow if f.handle == "hr" and "perhaps" in f.text]
        self.assertFalse(any("MDC_AWAY_RESP_RATE" in text for text in flow_hints),
                         "a flow in L/min is not offered a term whose usual units are breaths per minute")
        both = check((HR_UNIT, '<pm:Unit Code="264864"/>'), ('<pm:Type Code="147842" ' + MDC, '<pm:Type Code="147842"'))
        self.assertEqual([f.text.startswith("Its type and unit") for f in both if f.rule == "10700 TR1842"], [True],
                         "one finding names both codes of one element")

    def test_alarm_and_remote_control_conventions(self) -> None:
        self.assert_finds(None, "hr.high", ('Manifestation="Aud"', 'Manifestation="Vis"'), level="hint")
        self.assertEqual(check(('Priority="Hi"', 'Priority="Me"'), ('Manifestation="Aud"', 'Manifestation="Vis"')), [],
                         "only a high-priority alarm needs a tone")
        unsignalled = check(('<pm:AlertSignal Handle="hr.high.aud" ConditionSignaled="hr.high" Manifestation="Aud" '
                             f'Latching="false"><pm:Type Code="196674" {MDC}/></pm:AlertSignal>', ""))
        self.assertTrue(any(f.handle == "hr.high" and "no alert signal" in f.text for f in unsignalled), unsignalled)
        self.assert_finds(None, "rate.op", ('<pm:AllowedRange Lower="5" Upper="40"/>', ""), level="hint")

    def test_dangling_references_in_a_received_mdib(self) -> None:
        # A file with these is refused on load, so break a loaded MDIB the way a peer's could be.
        mdib = parse_mdib(CLEAN.encode())
        mdib.descriptions.handle.get_one("hr.high").Source = ["gone"]
        mdib.descriptions.handle.get_one("rate.op").OperationTarget = "gone.too"
        mdib.descriptions.handle.get_one("hr.high.aud").ConditionSignaled = "gone.signal"
        found = {(f.handle, f.level) for f in mdib_check.check(mdib) if "not in the MDIB" in f.text}
        self.assertEqual(found, {("hr.high", "error"), ("rate.op", "error"), ("hr.high.aud", "error")})

    def test_device_rules(self) -> None:
        self.assert_finds("10700 TR0251", "mds", ("<pm:SerialNumber>42</pm:SerialNumber>", ""), level="hint")
        self.assert_finds("10700 TR1590", "clock", (HR_DESCRIPTOR, HR_DESCRIPTOR + ' SafetyClassification="MedA" '
                                                    'LifeTimePeriod="PT5S" MaxDelayTime="PT1S"'), level="warning")
        self.assertEqual(check((HR_DESCRIPTOR, HR_DESCRIPTOR + ' SafetyClassification="MedA" LifeTimePeriod="PT5S" '
                                'MaxDelayTime="PT1S"'), ('<pm:Clock Handle="clock"/>',
                                                         '<pm:Clock Handle="clock" SafetyClassification="MedA"/>')), [])

    def test_context_rules(self) -> None:
        cases = [
            ("10700 TR1304", (LOCATION, 'ContextAssociation="Assoc" BindingMdibVersion="1" BindingStartTime="1000">')),
            ("10700 TR1636", (LOCATION, 'ContextAssociation="Assoc"><pm:Identification Root="urn:example" Extension="bed-1"/>')),
            ("10700 TR1637", (LOCATION, LOCATION.replace('BindingStartTime="1000"', 'BindingStartTime="1000" BindingEndTime="2000"'))),
            ("10700 TR1638", (LOCATION, LOCATION.replace('"Assoc"', '"Dis"'))),
            ("10700 TR1639", (LOCATION, LOCATION.replace('"Assoc"', '"Pre"'))),
            ("10700 TR1959", ('"pm:SystemContextState" ActivationState="On"', '"pm:SystemContextState" ActivationState="Off"')),
        ]
        for rule, replacement in cases:
            with self.subTest(rule):
                self.assert_finds(rule, "loc", replacement)
        ended = LOCATION.replace('"Assoc"', '"Dis"').replace('BindingStartTime="1000"',
                                                         'BindingStartTime="1000" UnbindingMdibVersion="2" BindingEndTime="2000"')
        self.assertEqual(check((LOCATION, ended)), [])


class ShapeTests(unittest.TestCase):
    def test_errors_come_first_and_the_summary_counts(self) -> None:
        findings = check((' DeterminationPeriod="PT1S"', ""), ("<pm:SerialNumber>42</pm:SerialNumber>", ""),
                         ('Manifestation="Aud"', 'Manifestation="Vis"'))
        self.assertEqual([f.level for f in findings], ["error", "hint", "hint"])
        self.assertEqual(mdib_check.summary(findings), "1 error · 0 warnings · 2 hints")
        hr = next(f for f in findings if f.handle == "hr")
        self.assertEqual(hr.where, "Heart rate (hr)")

    def test_every_preset_can_be_checked(self) -> None:
        for path in sorted((ROOT / "presets").glob("*.xml")):
            with self.subTest(path.name):
                findings = mdib_check.check(parse_mdib(path.read_bytes()))
                self.assertTrue(all(f.level in mdib_check.LEVELS and f.text for f in findings))
                errors = [f"{f.rule} {f.where}: {f.text}" for f in findings if f.level == "error"]
                self.assertEqual(errors, [], "a shipped preset breaks no rule the check knows")


class OwnDeviceTests(unittest.TestCase):
    def test_my_device_starts_without_errors(self) -> None:
        from sdctoolbox.provider_service import ProviderService

        service = ProviderService(instance_name="mdib-check-default")
        service.start()
        try:
            errors = [f"{f.rule} {f.where}" for f in mdib_check.check(service.mdib) if f.level == "error"]
        finally:
            service.stop()
        self.assertEqual(errors, [])


class FixTests(unittest.TestCase):
    """What a finding offers to fix, and that applying it settles the finding."""

    def setUp(self) -> None:
        from sdctoolbox.provider_service import ProviderService

        self.service = ProviderService(instance_name=f"mdib-fix-{self._testMethodName}")
        self.service.start()
        self.addCleanup(self.service.stop)

    def findings(self, handle: str) -> list[mdib_check.Finding]:
        return [f for f in mdib_check.check(self.service.mdib) if f.handle == handle]

    def test_a_private_code_named_outright_becomes_its_mdc_code(self) -> None:
        from sdctoolbox.model import MetricKind, MetricSpec

        handle = self.service.add_metric(MetricSpec("Pulse rate", MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(300)))
        fix = next(f.fix for f in self.findings(handle) if f.fix is not None)
        self.assertEqual((fix.label, fix.parts, fix.code), ("Use MDC_PULS_RATE", ("type",), "149514"))
        self.service.apply_fix(handle, fix)
        descriptor = self.service.mdib.entities.by_handle(handle).descriptor
        self.assertEqual((descriptor.Type.Code, descriptor.Type.CodingSystem), ("149514", "urn:oid:1.3.111.2.11073.10101.3"))
        self.assertEqual(self.service.list_metrics()[handle].type_coding.code, "149514", "later edits keep it")
        self.assertFalse(any(f.area == "Coding" and "type" in f.text for f in self.findings(handle)))

    def test_a_near_miss_stays_a_hint_without_a_fix(self) -> None:
        from sdctoolbox.model import MetricKind, MetricSpec

        # The search finds respiration rate for "Flow rate"; a button must not apply that.
        handle = self.service.add_metric(MetricSpec("Flow rate", MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(30)))
        coding = [f for f in self.findings(handle) if f.area == "Coding"]
        self.assertTrue(coding)
        self.assertTrue(all(f.fix is None for f in coding))

    def test_the_outdated_coding_system_is_renamed(self) -> None:
        from sdctoolbox.mdib_io import NativeMdibConfig

        xml = (ROOT / "presets" / "patient-monitor.xml").read_text(encoding="utf-8")
        legacy = xml.replace("urn:oid:1.3.111.2.11073.10101.3", "urn:oid:1.2.840.10004.1.1.1.0.0.1")
        self.service.replace_mdib(NativeMdibConfig(legacy.encode("utf-8")))
        fixable = [f for f in mdib_check.check(self.service.mdib) if f.rule == "10700 TR1840, TR1842"]
        self.assertTrue(fixable)
        for finding in fixable:
            self.assertEqual(finding.fix.label, "Use the current MDC coding system")
            self.service.apply_fix(finding.handle, finding.fix)
        self.assertFalse([f for f in mdib_check.check(self.service.mdib) if f.level == "error"])

    def test_a_high_priority_alarm_gets_an_audible_signal(self) -> None:
        from sdctoolbox.model import AlertPriority, AlertSignalSpec, AlertSpec, MetricKind, MetricSpec

        source = self.service.add_metric(MetricSpec("Pulse rate", MetricKind.NUMBER, minimum=Decimal(0), maximum=Decimal(300)))
        alarm = self.service.add_alert(AlertSpec("Pulse high", source, priority=AlertPriority.HIGH, upper_limit=Decimal(120),
                                                 signals=(AlertSignalSpec("Vis"),)))
        fix = next(f.fix for f in self.findings(alarm) if f.fix is not None)
        self.assertEqual(fix.label, "Add an audible signal")
        self.service.apply_fix(alarm, fix)
        self.assertEqual(sorted(s.manifestation for s in self.service.signal_states(alarm)), ["Aud", "Vis"])
        self.assertFalse(any(f.fix is not None for f in self.findings(alarm)))
        self.service.set_value(source, Decimal(150))
        self.assertTrue(self.service.alert_present(alarm), "the new signal belongs to a working alarm")


class WindowTests(unittest.TestCase):
    """The window and the status bar button, on a running My device."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        from sdctoolbox.gui.main_window import MainWindow
        from sdctoolbox.provider_service import ProviderService

        cls.app = QApplication.instance() or QApplication([])
        cls.service = ProviderService(instance_name="mdib-check-window")
        cls.service.start()
        cls.window = MainWindow(cls.service)
        cls.window.show()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.window.close()
        cls.service.stop()

    def wait_for(self, condition, seconds: float = 5) -> bool:  # noqa: ANN001
        import time

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.02)
        return condition()

    def live_text(self) -> str:
        from sdctoolbox.gui.mdib_check_window import status_text

        return status_text(mdib_check.check(self.service.mdib))

    def test_status_text_counts_errors_then_warnings(self) -> None:
        from sdctoolbox.gui.mdib_check_window import status_text

        def finding(level: str) -> mdib_check.Finding:
            return mdib_check.Finding(level, "Metric", "x", "Text.")

        self.assertEqual(status_text([]), "MDIB check: OK")
        self.assertEqual(status_text([finding("hint")]), "MDIB check: OK")
        self.assertEqual(status_text([finding("warning"), finding("hint")]), "MDIB check: 1 warning")
        self.assertEqual(status_text([finding("error"), finding("error"), finding("warning")]), "MDIB check: 2 errors")

    def test_the_button_follows_my_device(self) -> None:
        from sdctoolbox.model import MetricKind, MetricSpec

        self.assertTrue(self.window.check_button.text().startswith(self.live_text()))
        before = list(self.window.mdib_findings)
        self.service.add_metric(MetricSpec("Probe", MetricKind.NUMBER, handle="m.probe", unit_label="mmHg"))
        self.assertTrue(self.wait_for(lambda: self.window.mdib_findings != before), "a change is checked by itself")
        self.assertTrue(any(f.handle == "m.probe" for f in self.window.mdib_findings))
        self.assertTrue(self.window.check_button.text().startswith(self.live_text()))

    def test_window_lists_colours_and_shows_findings(self) -> None:
        from PySide6.QtCore import Qt

        from sdctoolbox.gui.styling import token
        from sdctoolbox.model import MetricKind, MetricSpec

        if "m.shown" not in self.service.list_metrics():
            self.service.add_metric(MetricSpec("Shown", MetricKind.NUMBER, handle="m.shown"))
        self.assertTrue(self.wait_for(lambda: "m.shown" in self.window.provider_pane.board.handles))
        self.window.check_action.trigger()
        check_window = self.window.check_window
        self.assertTrue(check_window.isVisible())
        self.assertEqual(check_window.target_box.currentText(), "My device")
        check_window.run()
        findings = self.window.mdib_findings
        self.assertEqual(check_window.tree.topLevelItemCount(), len(findings))
        self.assertTrue(check_window.summary_label.text().startswith(mdib_check.summary(findings)))
        self.assertIn("Checked against", check_window.summary_label.text())
        items = [check_window.tree.topLevelItem(i) for i in range(check_window.tree.topLevelItemCount())]
        error = next(item for item in items if item.text(0) == "error")
        self.assertEqual(error.foreground(0).color(), token("text/error"))
        self.assertTrue(error.font(0).bold())
        shown = next(item for item in items if item.data(0, Qt.UserRole) == "m.shown")
        self.window.tabs.setCurrentIndex(1)
        self.assertTrue(check_window.show_element(shown))
        self.assertEqual(self.window.tabs.currentIndex(), 0, "My device comes to the front")
        self.assertEqual(self.window.provider_pane.selected_handle(), "m.shown")

    def test_fix_changes_my_device_and_only_my_device(self) -> None:
        from PySide6.QtCore import Qt

        from sdctoolbox.model import MetricKind, MetricSpec

        if "m.fix_me" not in self.service.list_metrics():
            self.service.add_metric(MetricSpec("Pulse rate", MetricKind.NUMBER, handle="m.fix_me",
                                               minimum=Decimal(0), maximum=Decimal(300)))
        self.window.check_action.trigger()
        check_window = self.window.check_window
        check_window.run()
        self.assertFalse(check_window.tree.isColumnHidden(3))
        items = [check_window.tree.topLevelItem(i) for i in range(check_window.tree.topLevelItemCount())]
        item = next(i for i in items if i.data(0, Qt.UserRole) == "m.fix_me" and i.text(3))
        self.assertEqual(item.text(3), "Use MDC_PULS_RATE")
        check_window.tree.setCurrentItem(item)
        self.assertTrue(check_window.fix_button.isEnabled())
        self.assertTrue(check_window.fix_selected())
        self.assertEqual(self.service.mdib.entities.by_handle("m.fix_me").descriptor.Type.Code, "149514")
        self.assertFalse(any(f.handle == "m.fix_me" and f.fix for f in self.window.mdib_findings), "checked again")
        check_window.show_findings([], note="A connection.")  # what a connection's check looks like
        check_window.target_box.addItem("Peer", 7)
        check_window.target_box.setCurrentIndex(check_window.target_box.count() - 1)
        check_window.show_findings([])
        self.assertTrue(check_window.tree.isColumnHidden(3), "a peer's MDIB is not ours to change")
        self.assertFalse(check_window.fix_button.isVisible())
        check_window.target_box.setCurrentIndex(0)

    def test_a_new_pane_is_watched_too(self) -> None:
        from sdctoolbox.model import MetricKind, MetricSpec

        self.assertTrue(self.window.load_config(ROOT / "presets" / "insufflator.xml"))
        self.assertEqual(self.window.check_button.text().split("  ")[0], self.live_text())
        before = list(self.window.mdib_findings)
        self.service.add_metric(MetricSpec("After load", MetricKind.NUMBER, handle="m.after_load"))
        self.assertTrue(self.wait_for(lambda: self.window.mdib_findings != before))
        self.assertTrue(any(f.handle == "m.after_load" for f in self.window.mdib_findings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
