"""What each element of an MDIB is, in plain words: the tooltip of a row in an MDIB structure tree.

Rules named here are the ones the MDIB check applies to that kind of element, so the
explanation and the check's findings point to the same places in the standards.
"""

from __future__ import annotations

from html import escape

# localname -> (what it is, what a consumer does with it, the rules the MDIB check applies)
EXPLANATIONS: dict[str, tuple[str, str, str]] = {
    "MdsDescriptor": (
        "The medical device system: the whole device as a consumer sees it, with its maker, model and serial number.",
        "Reads MetaData to tell which device this is, and walks down from here to every part of it.",
        "10700 TR0251: MetaData names manufacturer, model and serial number."),
    "VmdDescriptor": (
        "A virtual medical device: one function of the device, such as its ECG or its pump.",
        "Groups what it shows by VMD; a switched-off VMD means everything under it is off too.",
        ""),
    "ChannelDescriptor": (
        "A channel: a group of metrics that belong together, such as the values of one sensor.",
        "Shows the channel's metrics together; a switched-off channel means its metrics are off.",
        "10701 TR1075: a metric in a switched-off channel is off too."),
    "NumericMetricDescriptor": (
        "A number the device measures, calculates or lets a user set, with its unit.",
        "Shows the value with its unit, and uses the ranges to scale a display and to spot impossible values.",
        "10701 TR0239: a technical range with both ends. 10701 TR0210, TR0682: LifeTimePeriod and MaxDelayTime "
        "for medically relevant values."),
    "StringMetricDescriptor": (
        "A text value the device reports, such as a mode or a free-text status.",
        "Shows the text as it comes; it cannot check or convert it.",
        "10701 TR0675: a value said to be valid carries a value."),
    "EnumStringMetricDescriptor": (
        "A text value from a fixed list, such as a mode with a few allowed settings.",
        "Offers the allowed values as a choice and can show their meaning.",
        "10701 TR0487: every allowed value appears once."),
    "RealTimeSampleArrayMetricDescriptor": (
        "A waveform: samples at a fixed rate, such as an ECG or a pleth curve.",
        "Draws the samples as a curve, using the sample period for the time axis.",
        "10701 TR0239: a technical range with both ends, so a curve can be scaled."),
    "DistributionSampleArrayMetricDescriptor": (
        "A distribution: values spread over a range, such as a spectrum.",
        "Draws the samples over their domain.",
        "10701 TR0239: a technical range with both ends."),
    "AlertSystemDescriptor": (
        "The alert system: switches the alarms below it on, off or paused as a whole.",
        "Reads its state to know whether the alarms below it can be raised at all.",
        ""),
    "AlertConditionDescriptor": (
        "An alarm condition: something the device detects, such as a technical fault.",
        "Shows whether it is present and how urgent it is.",
        "10207 R0008: its type uses an MDC code where one exists."),
    "LimitAlertConditionDescriptor": (
        "A limit alarm: raised when a watched value leaves its limits.",
        "Shows the limits next to the value and whether the alarm is present.",
        "10207 R0008: its type uses an MDC code where one exists."),
    "AlertSignalDescriptor": (
        "An alert signal: how the device announces an alarm, by sound, light or text.",
        "Shows whether the signal is on, and may switch it off or acknowledge it if the device allows.",
        ""),
    "ScoDescriptor": (
        "The service control object: holds every operation a consumer may call on this part of the device.",
        "Looks here to learn what it can change remotely.",
        ""),
    "SetValueOperationDescriptor": (
        "An operation that sets a number on the device.",
        "Offers a control for the target metric, limited to the allowed range the operation states.",
        "A hint when it has no AllowedRange: a consumer then learns the limits only by being refused."),
    "SetStringOperationDescriptor": (
        "An operation that sets a text or a choice on the device.",
        "Offers a text field or a list for the target metric.",
        ""),
    "ActivateOperationDescriptor": (
        "An operation that makes the device do something, such as start a measurement.",
        "Shows it as a button.",
        ""),
    "SetAlertStateOperationDescriptor": (
        "An operation that changes an alarm's state, for example to switch off a signal.",
        "Offers the change for the alarm it targets.",
        ""),
    "SetContextStateOperationDescriptor": (
        "An operation that sets a context, such as the patient or the location.",
        "Lets a user associate a patient or location with the device.",
        ""),
    "SetComponentStateOperationDescriptor": (
        "An operation that changes a component's state, such as switching a channel on.",
        "Offers the change for the component it targets.",
        ""),
    "SetMetricStateOperationDescriptor": (
        "An operation that changes a metric's state, such as switching it on or off.",
        "Offers the change for the metric it targets.",
        ""),
    "SystemContextDescriptor": (
        "The system context: holds the patient, location and other contexts of the device.",
        "Reads the contexts below it to tell which patient and place the data belongs to.",
        "10700 TR1959: a switched-off system context has no associated context."),
    "PatientContextDescriptor": (
        "The patient context: which patient the device is used on.",
        "Shows the associated patient and ties every value to them.",
        "10700 TR1304, TR1636: an associated patient has an Identification and says since when."),
    "LocationContextDescriptor": (
        "The location context: where the device is, such as a bed in a ward.",
        "Shows the location and uses it to find the device.",
        "10700 TR1304, TR1636: an associated location has an Identification and says since when."),
    "EnsembleContextDescriptor": (
        "The ensemble context: groups devices used together.",
        "Shows which devices belong to the same ensemble.",
        ""),
    "OperatorContextDescriptor": (
        "The operator context: who is using the device.",
        "Shows the operator.",
        ""),
    "WorkflowContextDescriptor": (
        "The workflow context: the order or procedure the device is used for.",
        "Shows the order the data belongs to.",
        ""),
    "MeansContextDescriptor": (
        "The means context: the means by which the device is used.",
        "Shows the means.",
        ""),
    "ClockDescriptor": (
        "The clock: the device's time and how it keeps it in step.",
        "Uses it to judge the device's time stamps.",
        "10700 TR1590: as safety-critical as the most critical part of the device."),
    "BatteryDescriptor": (
        "A battery of the device.",
        "Shows its charge and whether the device runs on it.",
        ""),
}


def explain(node_type: str, handle: str) -> str:
    """The tooltip for one element: its type and handle, what it is, what a consumer does, the rules."""
    what, consumer, rules = EXPLANATIONS.get(node_type, (
        f"A {node_type} element of the MDIB.", "Finds what it holds in IEEE 11073-10207 (BICEPS).", ""))
    parts = [f"<b>{escape(node_type)} · {escape(handle)}</b>", escape(what),
             f"A consumer {escape(consumer[0].lower() + consumer[1:])}"]
    if rules:
        parts.append(f"Rules: {escape(rules)}")
    return "".join(f"<p style='white-space:normal'>{part}</p>" for part in parts)
