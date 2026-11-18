"""The Device test: a few checks against a connected provider, each naming the rule it follows.

Meant for someone building their first SDC device: it asks the device what a consumer would
ask and says in plain words what happened. It is not a conformance test; a pass here only
means the device answered these few questions the way the standards expect.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sdc11073.xml_types import msg_types
from sdc11073.xml_types import pm_qnames as pm

from .consumer_service import ConsumerError, OperationReportsMissing, PeerConnectionLost
from .model import MetricKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from .consumer_service import ConsumerService, RemoteDevice

PASSED, FAILED, SKIPPED = "Passed", "Failed", "Skipped"
ACCEPTED = (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD)
DISCOVERY_WAIT = 3.0  # seconds for a ProbeMatch
REPORT_WAIT = 6.0  # seconds to collect reports


@dataclass(frozen=True)
class Result:
    result: str  # PASSED | FAILED | SKIPPED
    test: str
    happened: str
    standard: str


def _discovery(service: ConsumerService, remote: RemoteDevice) -> tuple[str, str]:
    service.probe()
    deadline = time.monotonic() + DISCOVERY_WAIT
    while True:
        device = next((d for d in service.known_devices(max_age=None) if d.epr == remote.epr), None)
        if device is not None and device.x_addrs and device.scopes:
            return PASSED, f"Answered the probe with {len(device.x_addrs)} address(es) and {len(device.scopes)} scope(s)."
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if device is None:
        return FAILED, "Did not answer a probe for medical devices."
    return FAILED, "Answered the probe without an address." if not device.x_addrs else "Answered the probe without scopes."


def _read_mdib(_service: ConsumerService, remote: RemoteDevice) -> tuple[str, str]:
    descriptors, states, version = remote.get_mdib()
    systems = sum(1 for descriptor in descriptors if descriptor.NODETYPE == pm.MdsDescriptor)
    if not systems:
        return FAILED, "GetMdib returned no MDS."
    if version is None:
        return FAILED, f"GetMdib returned {len(descriptors)} descriptors but no MdibVersion."
    return PASSED, f"GetMdib returned {len(descriptors)} descriptors and {len(states)} states at MdibVersion {version}."


def _report_versions(_service: ConsumerService, remote: RemoteDevice, wait: float = REPORT_WAIT) -> tuple[str, str]:
    log = remote.messages
    if log is None:
        return SKIPPED, "This connection keeps no message log."
    # Every report kept so far, and those that arrive while waiting for enough of them.
    deadline = time.monotonic() + wait
    while True:
        reports = [m for m in log.messages() if m.kind == "report" and m.detail.startswith("MdibVersion ")]
        if len(reports) >= 3 or time.monotonic() >= deadline:  # noqa: PLR2004 - a few show the order
            break
        time.sleep(0.2)
    if len(reports) < 2:  # noqa: PLR2004
        return SKIPPED, f"Only {len(reports)} report(s) arrived in {wait:g} s; change a value and run again."
    last: dict[str, int] = {}
    for message in reports:
        version = int(message.detail.split()[1])
        if version < last.get(message.action, version):
            return FAILED, f"{message.action} went back from MdibVersion {last[message.action]} to {version}."
        last[message.action] = version
    return PASSED, f"MdibVersion never went back in {len(reports)} reports."


def _out_of_range(_service: ConsumerService, remote: RemoteDevice) -> tuple[str, str]:
    metric = next(
        (m for m in remote.metrics().values()
         if m.kind is MetricKind.NUMBER and m.selected_operation_handle
         and (m.maximum is not None or m.technical_maximum is not None)),
        None,
    )
    if metric is None:
        return SKIPPED, "No number can be set and has a range."
    top = metric.maximum if metric.maximum is not None else metric.technical_maximum
    value = top + (metric.resolution or Decimal(1))
    try:
        state = remote.set_value(metric.handle, value)
    except (PeerConnectionLost, OperationReportsMissing):
        raise  # not an answer to the value
    except ConsumerError as exc:
        return PASSED, f"Refused {value} for {metric.handle} (allowed up to {top}): {exc}"
    if state not in ACCEPTED:
        why = f": {remote.last_invocation_detail}" if remote.last_invocation_detail else ""
        return PASSED, f"Refused {value} for {metric.handle} (allowed up to {top}) with {state.value}{why}."
    if isinstance(metric.value, Decimal):
        remote.set_value(metric.handle, metric.value)  # put back what was there
    return FAILED, f"Accepted {value} for {metric.handle}, above its maximum {top}."


def _renew(_service: ConsumerService, remote: RemoteDevice) -> tuple[str, str]:
    granted = remote.renew_subscriptions()
    if not granted:
        return SKIPPED, "There is no subscription to renew."
    refused = sum(1 for seconds in granted if seconds <= 0)
    if refused:
        return FAILED, f"{refused} of {len(granted)} subscriptions were not renewed."
    return PASSED, f"Renewed {len(granted)} subscriptions, for {min(granted):g} s or more."


def _alert_state(_service: ConsumerService, remote: RemoteDevice) -> tuple[str, str]:
    mdib = remote.mdib
    with mdib.mdib_lock:
        found = None
        for operation in mdib.descriptions.NODETYPE.get(pm.SetAlertStateOperationDescriptor, []):
            target = mdib.descriptions.handle.get_one(operation.OperationTarget, allow_none=True)
            state = mdib.states.descriptor_handle.get_one(operation.OperationTarget, allow_none=True)
            if target is not None and target.NODETYPE == pm.AlertSignalDescriptor and state is not None:
                found = operation.Handle, operation.OperationTarget, state.mk_copy()
                break
    if found is None:
        return SKIPPED, "The device offers no SetAlertState operation on an alarm signal."
    operation, target, state = found
    # The signal's own state, unchanged: the device should accept it without anything changing.
    result = remote.set_alert_state(operation, state)
    if result in ACCEPTED:
        return PASSED, f"Accepted the current state of {target} through {operation}."
    why = f": {remote.last_invocation_detail}" if remote.last_invocation_detail else ""
    return FAILED, f"Answered {result.value} to {operation} for {target}{why}."


# (test, standard, check)
TESTS: list[tuple[str, str, Callable[[ConsumerService, RemoteDevice], tuple[str, str]]]] = [
    ("Discovery", "IEEE 11073-20702: WS-Discovery Probe", _discovery),
    ("Read the MDIB", "IEEE 11073-20701: GetMdib", _read_mdib),
    ("Versions go up", "IEEE 11073-10207: MdibVersion", _report_versions),
    ("Set a value out of range", "IEEE 11073-10207: AllowedRange", _out_of_range),
    ("Renew a subscription", "IEEE 11073-20702: WS-Eventing Renew", _renew),
    ("Switch an alarm signal", "IEEE 11073-10207: SetAlertState", _alert_state),
]


def run(service: ConsumerService, remote: RemoteDevice) -> list[Result]:
    """Every test in order; one that raises counts as Failed with the reason."""
    results = []
    for test, standard, check in TESTS:
        try:
            result, happened = check(service, remote)
        except Exception as exc:  # noqa: BLE001 - a broken device is what this is for
            result, happened = FAILED, f"{type(exc).__name__}: {exc}"
        results.append(Result(result, test, happened, standard))
    return results


def summary(results: list[Result]) -> str:
    count = {name: sum(1 for r in results if r.result == name) for name in (PASSED, FAILED, SKIPPED)}
    return f"{count[PASSED]} passed · {count[FAILED]} failed · {count[SKIPPED]} skipped"
