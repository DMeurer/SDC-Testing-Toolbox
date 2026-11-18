---
title: Glossary
parent: Reference
nav_order: 6
---

# Glossary

| Term | Meaning |
|---|---|
| **ActivateOperation** | an operation that makes a device do something, with nothing to write; an *action* in the toolbox |
| **Alert condition** | the fact behind an alarm, such as "pressure too high"; present or not |
| **Alert signal** | how an alert condition is announced: visible, audible, tangible or other |
| **AllowedRange** | the range a consumer may request through a set operation |
| **BICEPS** | IEEE 11073-10207, the information and service model of SDC |
| **Channel** | a group of metrics inside a VMD; a *section* in the toolbox |
| **Consumer** | a participant that connects to a provider, reads it and may control it; the Network tab |
| **Context** | who and where: the patient, the location, and other contexts |
| **Delegation** | letting another device announce an alert signal |
| **Descriptor** | the description of one element of the MDIB: what it is |
| **DomainUnit** | the unit along which a distribution's samples are spread, such as Hz |
| **EPR** | endpoint reference, a device's network identity (`urn:uuid:…`) |
| **Handle** | the id of an element, unique within the device, such as `m.heart_rate` |
| **InvocationState** | a device's answer to an operation request: Wait, Start, Fin, FinMod or Fail |
| **MDC** | the IEEE 11073-10101 nomenclature: numeric codes for what is measured and its unit |
| **MDIB** | Medical Device Information Base, a device's complete self-description |
| **MdibVersion** | a counter that goes up with every change of the MDIB |
| **MdDescription** | the descriptor half of the MDIB |
| **MdState** | the state half of the MDIB |
| **MDPWS** | IEEE 11073-20702, the web-services transport of SDC |
| **MDS** | Medical Device System: the device as a whole, the root of the containment tree |
| **Metric** | one value of a device: number, text, choice, waveform or distribution; a *data source* in the toolbox |
| **MetricQuality** | a metric value's validity and mode |
| **OperatingMode** | whether an operation is enabled right now |
| **Participant key purpose** | a standard that says what a device of a kind must provide, such as IEEE 11073-10700 and -10701 |
| **Provider** | a participant that publishes a device; My device |
| **Report** | a message a provider sends to its subscribers when something changes |
| **SCO** | service control object: where a device's operations live |
| **SDC** | Service-oriented Device Connectivity, the IEEE 11073 family for networked medical devices |
| **Sequence id** | identifies one life of an MDIB; a new one means the device restarted with a new MDIB |
| **State** | the current value of a descriptor: a metric's value, an alarm's presence |
| **Subscription** | a consumer's request for reports, renewed before it runs out (WS-Eventing) |
| **TechnicalRange** | the range a metric can produce |
| **Toolbox behaviour** | extensions the toolbox stores in its MDIB files for what SDC has no place for |
| **VMD** | Virtual Medical Device: one function of the device, such as its pump |
| **WS-Discovery** | how SDC devices announce themselves and find each other, over UDP multicast |
