---
title: Limitations
parent: Reference
nav_order: 3
---

# Limitations
{: .no_toc }

The toolbox is a focused fixture for learning and testing SDC, not a conformance claim. IEEE
11073-10207 defines a far wider model than one device needs, and the library supplies much of the
wire plumbing, but a library feature is not automatically a toolbox feature.

1. TOC
{:toc}

| Label | Means |
|---|---|
| **Will cause problems** | a test that depends on it cannot succeed with the toolbox as it is |
| **Might cause problems** | a valid device can use it, so a test can be incomplete, misleading or need manual recovery |
| **Invisible** | a normal demonstration works, but some valid information is not shown |

## Toolbox

| Severity | Not supported or partial | Consequence |
|---|---|---|
| **Will cause problems** | Remote control covers set value, set string and activate operations without arguments. The consumer cannot send SetContextState, SetAlertState, SetMetricState or SetComponentState. A loaded MDIB that declares one publishes it but refuses every request (`Fail`, error `Oth`). | Remote context association, remote alarm handling and actions with arguments cannot be tested end to end. |
| **Might cause problems** | Contexts cover the patient and the location. Ensemble, workflow, means and operator contexts are not modelled or shown. | Tests of care teams, workflows or device ensembles need another tool or the raw MDIB. |
| **Might cause problems** | An alarm has one source metric; automatic limits work for numbers only; acknowledgement and delegation are local. | Remote alarm control, real delegation and limits on texts or choices cannot be tested. |
| **Might cause problems** | There are no lifecycle sequences: a metric does not go from NotRdy to On by itself, and the MDS, VMDs and channels have no fault controls. | Degraded states are set by hand or over MCP; timed or component-level failures cannot be simulated. |
| **Might cause problems** | Sample arrays have no annotations and come from one generator thread that is not real-time. | Fine for streaming and display tests, not for timing or annotation tests. |
| **Might cause problems** | When a device restarts with a new MDIB, only its tab disconnects and needs a manual reconnect. | Automatic recovery after a restart is not covered. |
| **Might cause problems** | A provider drops a consumer that does not accept a report within 2 s, so one stalled consumer cannot hold up the others. | A consumer on a very slow link, or paused in a debugger, loses its subscriptions and has to reconnect. |
| **Might cause problems** | There is no declared conformance profile, and testing against an independent implementation is manual. | Passing the toolbox's own tests does not prove interoperability. |
| **Invisible** | Detailed views exist for the five metric types only. Other parts and extensions stay in the containment tree or the raw MDIB. | A simple device looks complete, while foreign extensions are only in the tree. |
| **Invisible** | The window takes the first text description it finds; it does not pick a language or use the localization service. | Localized texts are not tested. |
| **Invisible** | A scan lists every SDC provider on the address, without a location filter, except the toolbox's own. | Pick the device to test deliberately. |

## Library (sdc11073 3.0.0)

| Severity | Not supported or partial | Consequence |
|---|---|---|
| **Might cause problems** | No per-certificate, per-operation authorization or invoker identity. | TLS admits or rejects a participant, but cannot give certificates different rights. |
| **Might cause problems** | The `/Get` service answers WS-MetadataExchange `GetMetadata` with a WS-Transfer `GetResponse`; the device root rejects `GetMetadata`. | Peers relying on `GetMetadata` may fail to read metadata. |
| **Might cause problems** | A slow but responsive consumer can block the bounded notification dispatcher. | One slow consumer can slow reports to the others. |
| **Might cause problems** | Once, the sudden death of a backlogged subscriber froze the provider, and stopping it hung. | A provider may need a process restart. |
| **Might cause problems** | The consumer keeps historic context states. | Many patient changes grow a long-running consumer's memory. |
| **Might cause problems** | A TLS rejection on the report path can look like a slow peer to the consumer. | The provider's log has the real reason. |
| **Might cause problems** | A forged WS-Discovery `Bye` can briefly remove a healthy device from discovery. | Discovery is unauthenticated. |
| **Might cause problems** | A flood of unicast Probes (3,000 observed) can wedge discovery shutdown. | Stopping may not return. |
| **Might cause problems** | On shutdown, a SubscriptionEnd may carry a malformed address and go to `NotifyTo` when no `EndTo` was given. | A strict consumer may not recognise which subscription ended. |
| **Might cause problems** | `GetContainmentTree` and `GetDescriptor` return a SOAP fault, so the toolbox does not advertise the containment tree service. | Peers must use `GetMdib`, `GetMdDescription` and `GetMdState`. |
| **Might cause problems** | The library's consumer fails on a waveform report without the optional `DeterminationTime`; the toolbox uses the arrival time instead. | Other consumers using the library directly may fail on such reports. |
