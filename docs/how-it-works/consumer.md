---
title: The consumer
parent: How it works
nav_order: 3
---

# The consumer
{: .no_toc }

`ConsumerService` and `RemoteDevice` (`sdctoolbox/consumer_service.py`) are the Network side. They
must survive MDIBs the toolbox did not create, so nothing in them may assume the toolbox's handles,
its coding system, or even that a metric has a value, a unit or a type. Foreign devices routinely
leave all of those out.

1. TOC
{:toc}

## Discovery

`SharedDiscovery` listens for WS-Discovery on one IPv4 address. Every Network tab uses the same
one, so they all list the same devices. *Scan* sends a Probe and collects the answers;
devices that announce themselves with a Hello appear without a scan, and a Bye removes them. The
toolbox's own provider is left out by its EPR.

## Connecting

`ConsumerService.connect(device)`:

1. resolves the device's addresses and reads its metadata;
2. fetches the whole MDIB with `GetMdib` and builds the library's consumer MDIB from it;
3. subscribes to the reports the device declares (state changes, description modifications,
   waveform streams, operation invocations), one subscription per hosted service, and logs any
   report it cannot subscribe to;
4. renews the subscriptions before they run out.

The result is a `RemoteDevice`. Its `metrics()`, `alerts()`, `actions()` and `patient_contexts()`
read the consumer MDIB into simple objects (`RemoteMetric`, `RemoteAlert`, `RemoteAction`) with
`None` wherever the device said nothing. `bind()` registers callbacks for changes.

## Writing and running

`set_value(handle, value)` finds the set operation whose target is the metric, sends the request
and waits for the final invocation state. `last_invocation_detail` keeps the error code and the
reason the device gave. `run_action(handle)` does the same for an activate operation.

A connection never decides for the device: the toolbox sends the request even if it expects a
refusal, and shows what the device answered.

## When the device changes or disappears

- Description modification reports update the consumer MDIB, and the window rebuilds the affected
  cards and rows.
- A new sequence id means the device restarted with a new MDIB. The tab disconnects, because the
  old handles may no longer mean the same thing.
- A failed renewal or an unreachable device ends the connection, and the tab says so.
- A provider drops a consumer that does not accept a report within 2 seconds, so one stalled
  consumer cannot hold up the others.

## The message log

The library writes every SOAP message it sends or receives to its `sdc_comm.*` loggers.
`message_log.py` puts one handler on them and gives each message to the connection it belongs to:

- a request by the address it goes to,
- its response by the thread that sent the request,
- a report by the consumer's own notification path.

The library does not log a response that comes with an HTTP error status, so `message_log.py`
logs those bodies itself; that is how a device's SOAP Fault reaches Messages.

Each connection keeps its newest 500 messages. Messages of no connection, such as My device's own
traffic, are dropped unread. The XML is parsed with entity resolution, DTDs and network access
switched off.

## The Device test

`device_test.py` runs its six checks one after another against a `RemoteDevice`. A check that
raises counts as *Failed* with the reason; a check the device gives no chance for is *Skipped*. The
out-of-range test uses the device's own allowed range, and puts the old value back if the device
wrongly accepts the new one.
