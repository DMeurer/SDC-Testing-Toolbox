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

import json
import logging
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import pm_qnames as pm  # noqa: E402

from sdctoolbox import config, constants  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    AlertSpec,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    WaveformShape,
)
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


def wait_until(predicate, timeout: float = 10.0) -> bool:
    """Poll until predicate() is true or the time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def broken_entity(service: ProviderService, handle: str):  # noqa: ANN201 - an sdc11073 Entity
    """A descriptor missing a field BICEPS makes mandatory, ready to be written.

    A numeric metric with no Resolution. Deliberately built by stripping a field rather than
    by asking for a metric kind the toolbox cannot make: every kind can be made now, and a
    test that depended on one being unimplementable would quietly stop testing the rollback
    the moment that changed - which is exactly what happened when waveforms were added.
    """
    entity = service.mdib.entities.new_entity(
        pm.NumericMetricDescriptor,
        handle,
        constants.CHANNEL_HANDLE,
    )
    service._apply_spec_to_descriptor(  # noqa: SLF001 - a normal descriptor, then broken
        entity.descriptor,
        MetricSpec(label=handle, kind=MetricKind.NUMBER),
    )
    entity.descriptor.Resolution = None
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
            "an incomplete descriptor is refused",
            "mandatory value Resolution missing",
        )
    else:
        report.check(False, "an incomplete descriptor is refused", "it was accepted")  # noqa: FBT003

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


def check_sample_arrays(report: Report, service: ProviderService) -> None:
    print("\n2. Waveforms and distributions")

    for kind in MetricKind:
        report.check(kind.creatable, f"{kind.value} can be created")

    wave = service.add_metric(
        MetricSpec(
            label="Pleth",
            kind=MetricKind.WAVEFORM,
            unit_label="%",
            minimum=Decimal("0"),
            maximum=Decimal("100"),
            sample_period=Decimal("0.05"),
            shape=WaveformShape.SAWTOOTH,
        ),
    )
    descriptor = service.mdib.entities.by_handle(wave).descriptor
    report.check(descriptor.Resolution is not None, "a waveform gets its Resolution", str(descriptor.Resolution))
    report.check(
        descriptor.SamplePeriod is not None,
        "and its SamplePeriod, the other mandatory field",
        str(descriptor.SamplePeriod),
    )
    report.check(
        [(r.Lower, r.Upper) for r in descriptor.TechnicalRange] == [(Decimal("0"), Decimal("100"))],
        "limits become a TechnicalRange, as on a number",
        str([(r.Lower, r.Upper) for r in descriptor.TechnicalRange]),
    )

    report.check(service.generator_running, "adding a waveform starts the generator")
    deadline = time.monotonic() + 10.0
    while not service.get_samples(wave) and time.monotonic() < deadline:
        time.sleep(0.2)
    samples = service.get_samples(wave)
    report.check(bool(samples), "samples are generated", f"{len(samples)} in the first block")
    report.check(
        all(Decimal("0") <= s <= Decimal("100") for s in samples),
        "and stay inside the declared range",
        f"{min(samples)} to {max(samples)}" if samples else "none",
    )
    report.check(
        all(isinstance(s, Decimal) for s in samples),
        "as Decimal, never float",
    )

    # A block must continue the curve rather than restarting it, or a consumer sees a saw
    # edge every half second whatever shape was asked for.
    first = list(samples)
    deadline = time.monotonic() + 10.0
    while service.get_samples(wave) == first and time.monotonic() < deadline:
        time.sleep(0.2)
    report.check(service.get_samples(wave) != first, "the next block differs from the last")

    dist = service.add_metric(
        MetricSpec(
            label="Spectrum",
            kind=MetricKind.DISTRIBUTION,
            unit_label="dB",
            domain_unit_label="Hz",
            domain_minimum=Decimal("0"),
            domain_maximum=Decimal("500"),
        ),
    )
    descriptor = service.mdib.entities.by_handle(dist).descriptor
    report.check(descriptor.Resolution is not None, "a distribution gets its Resolution")
    report.check(
        descriptor.DomainUnit is not None,
        "and its DomainUnit, the field it fails on first",
        str(getattr(descriptor.DomainUnit, "Code", None)),
    )
    report.check(
        (descriptor.DistributionRange.Lower, descriptor.DistributionRange.Upper)
        == (Decimal("0"), Decimal("500")),
        "the domain becomes a DistributionRange, not a TechnicalRange",
        f"{descriptor.DistributionRange.Lower} to {descriptor.DistributionRange.Upper}",
    )
    # A distribution used to have nothing driving it, so its card said 'waiting for
    # samples' for ever - there was no way to fill one from the window at all.
    filled = wait_until(lambda: bool(service.get_samples(dist)), timeout=10.0)
    report.check(
        filled,
        "a distribution is filled by the generator, without being asked",
        f"{len(service.get_samples(dist))} samples",
    )
    first_distribution = list(service.get_samples(dist))
    report.check(
        bool(first_distribution)
        and wait_until(lambda: service.get_samples(dist) != first_distribution, timeout=10.0),
        "and keeps moving, so a still card means something is wrong",
        f"{len(first_distribution)} samples to start",
    )

    step = descriptor.DistributionRange.StepWidth
    report.check(
        step is not None and step != descriptor.Resolution,
        "StepWidth is the domain spacing, not the value Resolution",
        f"StepWidth {step}, Resolution {descriptor.Resolution}",
    )
    implied = (descriptor.DistributionRange.Upper - descriptor.DistributionRange.Lower) / step + 1
    report.check(
        abs(implied - len(service.get_samples(dist))) < 1,
        "and it agrees with how many samples are actually sent",
        f"descriptor implies {implied:.1f}, {len(service.get_samples(dist))} sent",
    )

    block = [Decimal(str(v)) for v in ("1.5", "2.5", "3.5")]
    service.set_samples(dist, block)
    report.check(service.get_samples(dist) == block, "a distribution takes samples it is given")
    report.check(
        wait_until(lambda: service.get_samples(dist) != block, timeout=3.0) is False,
        "and setting one by hand takes it off the generator, so the block survives",
        str(service.get_samples(dist)),
    )
    service.set_samples(dist, [Decimal("9")])
    report.check(
        service.get_samples(dist) == [Decimal("9")],
        "a second block replaces the first rather than appending",
        str(service.get_samples(dist)),
    )

    plain = service.add_metric(MetricSpec(label="Plain", kind=MetricKind.NUMBER))
    for handle, samples_in, why in [
        (plain, [Decimal("1")], "samples on a number"),
        (wave, [0.5], "float samples"),
    ]:
        try:
            service.set_samples(handle, samples_in)
        except (ValueError, TypeError) as exc:
            report.check(True, f"refuses {why}", str(exc)[:56])  # noqa: FBT003
        else:
            report.check(False, f"refuses {why}", "it was accepted")  # noqa: FBT003

    service.stop_generator()
    report.check(not service.generator_running, "the generator can be stopped")
    service.remove_metric(plain)
    service.remove_metric(dist)
    service.remove_metric(wave)

def check_alarm_rollback(report: Report, service: ProviderService) -> None:
    print("\n3. An alarm is written whole or not at all")

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


def check_signals(report: Report, service: ProviderService) -> None:
    print("\n4. Acknowledging and delegating a signal")

    service.add_metric(
        MetricSpec(label="Pressure", kind=MetricKind.NUMBER, initial_value=Decimal("5")),
    )
    alarm = service.add_alert(
        AlertSpec(
            label="Pressure high",
            source_handle="m.pressure",
            upper_limit=Decimal("10"),
            delegable=True,
        ),
    )

    def summaries() -> list[str]:
        return [signal.summary() for signal in service.signal_states(alarm)]

    report.check(summaries() == ["Vis:Off", "Aud:Off"], "signals start off", str(summaries()))

    try:
        service.acknowledge_alert(alarm)
    except ValueError as exc:
        report.check(
            "not raised" in str(exc),
            "acknowledging an alarm that is not raised is refused",
            str(exc)[:60],
        )
    else:
        report.check(False, "acknowledging a clear alarm is refused", "it was accepted")  # noqa: FBT003

    service.set_value("m.pressure", Decimal("20"))
    report.check(summaries() == ["Vis:On", "Aud:On"], "a breach turns them on", str(summaries()))

    report.check(service.acknowledge_alert(alarm) == 2, "both signals are acknowledged")  # noqa: PLR2004
    report.check(summaries() == ["Vis:Ack", "Aud:Ack"], "and report Ack", str(summaries()))
    report.check(
        service.alert_present(alarm),
        "the condition is still present: acknowledging changes how it is announced, "
        "not whether it is true",
    )

    # The interesting one. Alarms are re-evaluated on every change to the source, so an
    # acknowledgement that did not survive the next out-of-range value would be useless.
    service.set_value("m.pressure", Decimal("25"))
    report.check(
        summaries() == ["Vis:Ack", "Aud:Ack"],
        "a further breach does not undo the acknowledgement",
        str(summaries()),
    )

    service.set_value("m.pressure", Decimal("1"))
    report.check(summaries() == ["Vis:Off", "Aud:Off"], "clearing turns them off", str(summaries()))
    service.set_value("m.pressure", Decimal("30"))
    report.check(
        summaries() == ["Vis:On", "Aud:On"],
        "but a fresh occurrence is not acknowledged",
        str(summaries()),
    )

    signal = service.signal_handles_for(alarm)[0]
    service.set_signal_delegated(signal, delegated=True)
    report.check(
        service.signal_states(alarm)[0].delegated,
        "a delegable signal moves to Rem",
        summaries()[0],
    )
    service.set_signal_delegated(signal, delegated=False)
    report.check(not service.signal_states(alarm)[0].delegated, "and back to Loc")

    plain = service.add_alert(AlertSpec(label="Plain", source_handle="m.pressure"))
    try:
        service.set_signal_delegated(service.signal_handles_for(plain)[0], delegated=True)
    except ValueError as exc:
        report.check(
            "SignalDelegationSupported" in str(exc),
            "delegating a signal that does not support it is refused",
            str(exc)[:60],
        )
    else:
        report.check(False, "delegating an undelegable signal is refused", "accepted")  # noqa: FBT003

    service.remove_alert(plain)
    service.remove_alert(alarm)
    service.remove_metric("m.pressure")


def check_contexts(report: Report, service: ProviderService) -> None:
    print("\n5. Patient and location contexts")

    default = service.get_location()
    report.check(
        default.facility == "HOSP" and default.bed == "Toolbox",
        "a provider starts with the default location associated",
        default.summary(),
    )
    report.check(service.get_patient().is_empty(), "and with nobody attached")

    service.set_location(
        LocationInfo(facility="HOSP", building="B2", floor="3", point_of_care="OR1", bed="A"),
    )
    report.check(
        service.get_location().summary() == "HOSP / B2 / 3 / OR1 / A",
        "every part of a location survives the round trip",
        service.get_location().summary(),
    )

    service.set_patient(
        PatientInfo(
            given_name="Ada",
            family_name="Lovelace",
            sex="F",
            patient_type="Ad",
            date_of_birth="1815-12-10",
        ),
    )
    patient = service.get_patient()
    report.check(patient.given_name == "Ada", "given name survives", patient.given_name)
    report.check(patient.sex == "F", "sex survives as its BICEPS code", patient.sex)
    report.check(patient.patient_type == "Ad", "and patient type", patient.patient_type)
    report.check(
        patient.date_of_birth == "1815-12-10",
        "the date of birth comes back in the form it was given",
        patient.date_of_birth,
    )

    # A context is a multi-state entity: the old state is disassociated, not overwritten.
    service.set_patient(PatientInfo(given_name="Grace", family_name="Hopper"))
    report.check(
        service.get_patient().summary() == "Grace Hopper",
        "a new patient replaces the old one",
        service.get_patient().summary(),
    )
    entity = service.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE)
    entity.update()
    associations = sorted(str(state.ContextAssociation) for state in entity.states.values())
    report.check(
        associations == ["Assoc", "Dis"],
        "the previous patient is kept as a disassociated state, not deleted",
        str(associations),
    )

    service.clear_patient()
    report.check(service.get_patient().is_empty(), "clearing detaches everyone")
    entity.update()
    report.check(
        all(
            str(state.ContextAssociation) != "Assoc"
            for state in entity.states.values()
        ),
        "and leaves no associated state behind",
    )

    bad = PatientInfo(date_of_birth="not a date")
    try:
        service.set_patient(bad)
    except (ValueError, TypeError) as exc:
        report.check(bool(str(exc)), "an unusable date of birth is refused", str(exc)[:60])
    else:
        report.check(False, "an unusable date of birth is refused", "it was accepted")  # noqa: FBT003


def check_presets(report: Report) -> None:
    print("\n6. Presets")

    presets = config.list_presets()
    report.check(bool(presets), "the shipped presets are found", f"{len(presets)} found")
    for preset in presets:
        report.check(preset.path.exists(), f"{preset.name} points at a real file")
        report.check(bool(preset.name), "and has a name", preset.name)
        report.check(preset.metrics > 0, f"{preset.name} defines data sources", str(preset.metrics))

    with tempfile.TemporaryDirectory(prefix="sdctoolbox-presets-") as raw:
        folder = Path(raw)
        (folder / "broken.json").write_text("{ not json", encoding="utf-8")
        (folder / "invalid.json").write_text('{"metrics": [{"label": "x"}]}', encoding="utf-8")
        (folder / "good.json").write_text(
            '{"name": "Good one", "metrics": [{"label": "v", "kind": "number"}]}',
            encoding="utf-8",
        )
        found = config.list_presets(folder)
        report.check(
            [preset.name for preset in found] == ["Good one"],
            "an unreadable preset is skipped rather than breaking the list",
            str([preset.name for preset in found]),
        )

    report.check(
        config.list_presets(Path(tempfile.gettempdir()) / "no-such-preset-folder") == [],
        "a missing presets folder is not an error",
    )


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
        check_sample_arrays(report, service)
        check_alarm_rollback(report, service)
        check_signals(report, service)
        check_contexts(report, service)
        check_presets(report)
    finally:
        service.stop()

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
