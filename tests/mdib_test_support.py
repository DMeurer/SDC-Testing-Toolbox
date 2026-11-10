"""Original synthetic BICEPS fixture; unrelated to private development references."""

from __future__ import annotations

PM = "http://standards.ieee.org/downloads/11073/11073-10207-2017/participant"
MSG = "http://standards.ieee.org/downloads/11073/11073-10207-2017/message"
EXT = "http://standards.ieee.org/downloads/11073/11073-10207-2017/extension"
XSI = "http://www.w3.org/2001/XMLSchema-instance"


def original_mdib() -> bytes:
    """A complete monitor snapshot with explicit state, history and non-bootstrap handles."""
    return f'''<msg:GetMdibResponse xmlns:msg="{MSG}" xmlns:pm="{PM}" xmlns:xsi="{XSI}"
        xmlns:ext="{EXT}" xmlns:demo="urn:sdc-testing-toolbox:test-extension"
        SequenceId="urn:uuid:11111111-1111-1111-1111-111111111111" MdibVersion="17">
      <msg:Mdib SequenceId="urn:uuid:11111111-1111-1111-1111-111111111111" MdibVersion="17" InstanceId="3">
        <pm:MdDescription DescriptionVersion="7">
          <pm:Mds Handle="demo.root">
            <ext:Extension><demo:Note importance="synthetic">original fixture</demo:Note></ext:Extension>
            <pm:Type Code="demo-monitor" CodingSystem="urn:sdc-testing-toolbox:private"><pm:ConceptDescription>MDIB demo monitor</pm:ConceptDescription></pm:Type>
            <pm:AlertSystem Handle="demo.alerts" SelfCheckPeriod="PT30S">
              <pm:AlertCondition Handle="demo.alarm" Kind="Tec" Priority="Me"><pm:Source>demo.rate</pm:Source></pm:AlertCondition>
              <pm:AlertSignal Handle="demo.signal" ConditionSignaled="demo.alarm" Manifestation="Vis" Latching="false"/>
            </pm:AlertSystem>
            <pm:Sco Handle="demo.sco">
              <pm:Operation Handle="demo.set" xsi:type="pm:SetValueOperationDescriptor" OperationTarget="demo.rate"/>
              <pm:Operation Handle="demo.disabled" xsi:type="pm:SetValueOperationDescriptor" OperationTarget="demo.rate"/>
              <pm:Operation Handle="demo.mode.set" xsi:type="pm:SetStringOperationDescriptor" OperationTarget="demo.mode" MaxLength="4"/>
              <pm:Operation Handle="demo.action" xsi:type="pm:ActivateOperationDescriptor" OperationTarget="demo.root"/>
            </pm:Sco>
            <pm:SystemContext Handle="demo.context"><pm:PatientContext Handle="demo.patient"/><pm:LocationContext Handle="demo.location"/></pm:SystemContext>
            <pm:Vmd Handle="demo.vmd"><pm:Channel Handle="demo.channel">
              <pm:Metric Handle="demo.rate" xsi:type="pm:NumericMetricDescriptor" MetricCategory="Set" MetricAvailability="Cont" Resolution="0.1">
                <pm:Type Code="demo-rate" CodingSystem="urn:sdc-testing-toolbox:private"><pm:ConceptDescription>Demo rate</pm:ConceptDescription></pm:Type>
                <pm:Unit Code="262656"/><pm:TechnicalRange Lower="0" Upper="50"/>
              </pm:Metric>
              <pm:Metric Handle="demo.mode" xsi:type="pm:EnumStringMetricDescriptor" MetricCategory="Set" MetricAvailability="Cont">
                <pm:Unit Code="262656"/><pm:AllowedValue><pm:Value>IDLE</pm:Value></pm:AllowedValue><pm:AllowedValue><pm:Value>RUN</pm:Value></pm:AllowedValue>
              </pm:Metric>
              <pm:Metric Handle="demo.wave" xsi:type="pm:RealTimeSampleArrayMetricDescriptor" MetricCategory="Msrmt" MetricAvailability="Cont" Resolution="0.1" SamplePeriod="PT0.01S"><pm:Unit Code="262656"/></pm:Metric>
            </pm:Channel></pm:Vmd>
          </pm:Mds>
        </pm:MdDescription>
        <pm:MdState StateVersion="11">
          <pm:State DescriptorHandle="demo.root" xsi:type="pm:MdsState" ActivationState="StndBy"/>
          <pm:State DescriptorHandle="demo.alerts" xsi:type="pm:AlertSystemState" ActivationState="Psd"/>
          <pm:State DescriptorHandle="demo.alarm" xsi:type="pm:AlertConditionState" ActivationState="On" Presence="true"/>
          <pm:State DescriptorHandle="demo.signal" xsi:type="pm:AlertSignalState" ActivationState="On" Presence="On"/>
          <pm:State DescriptorHandle="demo.set" xsi:type="pm:SetValueOperationState" OperatingMode="En"><pm:AllowedRange Lower="5" Upper="30" StepWidth="0.5"/></pm:State>
          <pm:State DescriptorHandle="demo.disabled" xsi:type="pm:SetValueOperationState" OperatingMode="Dis"><pm:AllowedRange Lower="31" Upper="40"/></pm:State>
          <pm:State DescriptorHandle="demo.mode.set" xsi:type="pm:SetStringOperationState" OperatingMode="En"><pm:AllowedValues><pm:Value>IDLE</pm:Value></pm:AllowedValues></pm:State>
          <pm:State DescriptorHandle="demo.action" xsi:type="pm:ActivateOperationState" OperatingMode="En"/>
          <pm:State DescriptorHandle="demo.rate" xsi:type="pm:NumericMetricState" StateVersion="4"><pm:MetricValue Value="12.5"><pm:MetricQuality Validity="Vld"/></pm:MetricValue></pm:State>
          <pm:State DescriptorHandle="demo.mode" xsi:type="pm:EnumStringMetricState"><pm:MetricValue Value="RUN"><pm:MetricQuality Validity="Vld"/></pm:MetricValue></pm:State>
          <pm:State DescriptorHandle="demo.wave" xsi:type="pm:RealTimeSampleArrayMetricState"><pm:MetricValue Samples="0 1.5 -0.5"><pm:MetricQuality Validity="Vld"/></pm:MetricValue></pm:State>
          <pm:State DescriptorHandle="demo.patient" Handle="demo.history" xsi:type="pm:PatientContextState" ContextAssociation="Dis" StateVersion="2"><pm:CoreData><pm:Givenname>Synthetic previous</pm:Givenname></pm:CoreData></pm:State>
          <pm:State DescriptorHandle="demo.patient" Handle="demo.current" xsi:type="pm:PatientContextState" ContextAssociation="Assoc"><pm:CoreData><pm:Givenname>Synthetic current</pm:Givenname></pm:CoreData></pm:State>
          <pm:State DescriptorHandle="demo.location" Handle="demo.location.state" xsi:type="pm:LocationContextState" ContextAssociation="Assoc"><pm:LocationDetail Facility="DEMO" Bed="SYNTHETIC"/></pm:State>
        </pm:MdState>
      </msg:Mdib>
    </msg:GetMdibResponse>'''.encode()
