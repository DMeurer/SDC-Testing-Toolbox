"""Integrity checks for the provider core, on one process and one loopback provider.

The centre of this suite is the descriptor rollback. A descriptor BICEPS considers
incomplete is not rejected by the transaction that writes it: the transaction commits, and
sdc11073 only complains when it serialises the DescriptionModificationReport, from an
observer of mdib.transaction. Everything before that has already happened, so an unguarded
failure leaves a handle in the MDIB that is advertised to consumers while the toolbox has no
record of it.

Section 1 proves that by doing it wrong first and watching the orphan appear, so the check
that follows is known to mean something rather than merely passing.

Exit code 0 means all checks passed.

Usage:  .venv/Scripts/python.exe tests/provider_core.py
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import pm_qnames as pm  # noqa: E402

from sdctoolbox import constants  # noqa: E402
from sdctoolbox.model import AlertSpec, MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402


class Report:
    """Collects pass/fail results and prints them as they happen."""

    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, description: str, detail: str = "") -> bool:  # noqa: FBT001
        self.checks += 1
        if not ok:
            self.failures += 1
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {description}{suffix}", flush=True)
        return ok

    def summary(self) -> int:
        print("-" * 74)
        if self.failures:
            print(f"RESULT: {self.failures} of {self.checks} checks FAILED")
            return 1
        print(f"RESULT: all {self.checks} checks passed")
        return 0


def broken_entity(service: ProviderService, handle: str):  # noqa: ANN201 - an sdc11073 Entity
    """A waveform descriptor with none of its mandatory fields, ready to be written.

    Nothing here is exotic: this is exactly what add_metric builds for a waveform, because
    _apply_spec_to_descriptor sets Resolution only for a number and SamplePeriod never.
    """
    entity = service.mdib.entities.new_entity(
        pm.RealTimeSampleArrayMetricDescriptor,
        handle,
        constants.CHANNEL_HANDLE,
    )
    service._apply_spec_to_descriptor(  # noqa: SLF001 - reproducing the original failure exactly
        entity.descriptor,
        MetricSpec(label=handle, kind=MetricKind.WAVEFORM),
    )
    return entity


def check_rollback(report: Report, service: ProviderService) -> None:
    print("\n1. A descriptor that cannot be serialised leaves nothing behind")

    # -- the bug, on purpose ------------------------------------------------------
    unguarded = "m.orphan_probe"
    try:
        with service.mdib.descriptor_transaction() as mgr:
            mgr.write_entity(broken_entity(service, unguarded))
    except Exception as exc:  # noqa: BLE001 - this failure is the point
        report.check(
            "Resolution" in str(exc),
            "an incomplete waveform descriptor is refused",
            "mandatory value Resolution missing",
        )
    else:
        report.check(False, "an incomplete waveform descriptor is refused", "it was accepted")  # noqa: FBT003

    report.check(
        service.mdib.entities.by_handle(unguarded) is not None,
        "without a rollback the handle survives the failure, which is the defect",
    )
    service._discard_entities([unguarded])  # noqa: SLF001 - clean up after the demonstration
    report.check(
        service.mdib.entities.by_handle(unguarded) is None,
        "and the discard undoes it",
    )

    # -- the fix ------------------------------------------------------------------
    guarded = "m.rollback_probe"
    try:
        service._create_entities([broken_entity(service, guarded)])  # noqa: SLF001
    except Exception:  # noqa: BLE001 - expected, the entity is unserialisable
        report.check(True, "_create_entities lets the failure out")  # noqa: FBT003
    else:
        report.check(False, "_create_entities lets the failure out", "it did not raise")  # noqa: FBT003

    report.check(
        service.mdib.entities.by_handle(guarded) is None,
        "and no orphan descriptor is left in the mdib",
    )

    # A rollback that leaves the handle unusable would be its own trap.
    reused = service.add_metric(MetricSpec(label="Reused", kind=MetricKind.NUMBER, handle=guarded))
    report.check(reused == guarded, "the handle is free to use again afterwards", reused)
    service.remove_metric(guarded)

    # Several entities at once: an alarm writes a condition and two signals together.
    handles = ["m.multi_a", "m.multi_b"]
    entities = [broken_entity(service, handles[0]), broken_entity(service, handles[1])]
    try:
        service._create_entities(entities)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - expected
        pass
    report.check(
        all(service.mdib.entities.by_handle(h) is None for h in handles),
        "a failure part way through a multi-entity write undoes all of them",
        str([h for h in handles if service.mdib.entities.by_handle(h) is not None]),
    )


def check_alarm_rollback(report: Report, service: ProviderService) -> None:
    print("\n2. An alarm is written whole or not at all")

    service.add_metric(
        MetricSpec(label="Alarm source", kind=MetricKind.NUMBER, initial_value=Decimal("1")),
    )
    # add_alert writes a condition and its two signals in one transaction, so the same
    # rollback has to cover a partial failure across all three.
    entities = [
        broken_entity(service, "al.probe"),
        broken_entity(service, "sig.probe.vis"),
        broken_entity(service, "sig.probe.aud"),
    ]
    try:
        service._create_entities(entities)  # noqa: SLF001
    except Exception:  # noqa: BLE001 - expected
        pass
    stranded = [e.handle for e in entities if service.mdib.entities.by_handle(e.handle) is not None]
    report.check(not stranded, "a condition and its signals are undone together", str(stranded))

    # The real thing still works, which is what proves the rollback is not over-eager.
    handle = service.add_alert(
        AlertSpec(label="Source high", source_handle="m.alarm_source", upper_limit=Decimal("10")),
    )
    report.check(handle in service.list_alerts(), "a valid alarm is still created", handle)
    report.check(
        len(service.signal_handles_for(handle)) == 2,  # noqa: PLR2004
        "with both its signals",
        str(service.signal_handles_for(handle)),
    )
    service.remove_alert(handle)
    service.remove_metric("m.alarm_source")


def main() -> int:
    basic_logging_setup(level=logging.ERROR)
    report = Report()

    print("=" * 74)
    print("Provider core integrity")
    print("=" * 74)

    service = ProviderService(instance_name="provider-core")
    service.start()
    try:
        check_rollback(report, service)
        check_alarm_rollback(report, service)
    finally:
        service.stop()

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
