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
import math
import sys
import tempfile
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report  # noqa: E402
from sdc11073 import observableproperties  # noqa: E402
from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import msg_types, pm_types  # noqa: E402
from sdc11073.xml_types import pm_qnames as pm  # noqa: E402

from sdctoolbox import config, constants  # noqa: E402
from sdctoolbox.consumer_service import RemoteDevice  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    ActionSpec,
    AlertManifestation,
    AlertSignalSpec,
    AlertSpec,
    Coding,
    DistributionShape,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    PatientMeasurement,
    WaveformShape,
)
from sdctoolbox.provider_service import (  # noqa: E402
    DISTRIBUTION_BINS,
    ProviderService,
    _distribution_samples,
)


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

    range_cases = (
        ("default", None, None, Decimal("0.1"), (Decimal("0"), Decimal("100"))),
        ("negative maximum", None, Decimal("-5"), Decimal("0.1"), (Decimal("-105"), Decimal("-5"))),
        ("positive minimum", Decimal("5"), None, Decimal("0.1"), (Decimal("5"), Decimal("105"))),
        ("equal", Decimal("7"), Decimal("7"), Decimal("0.1"), (Decimal("7"), Decimal("7"))),
        (
            "narrow",
            Decimal("0.001"),
            Decimal("0.002"),
            Decimal("0.0001"),
            (Decimal("0.001"), Decimal("0.002")),
        ),
    )
    range_failures = []
    for kind in (MetricKind.WAVEFORM, MetricKind.DISTRIBUTION):
        for case, minimum, maximum, resolution, expected_range in range_cases:
            spec = MetricSpec(
                label=f"Generated {kind.value} {case}",
                kind=kind,
                minimum=minimum,
                maximum=maximum,
                resolution=resolution,
                domain_minimum=(Decimal("1000") if kind is MetricKind.DISTRIBUTION else None),
                domain_maximum=(Decimal("2000") if kind is MetricKind.DISTRIBUTION else None),
            )
            low, high = spec.generated_sample_range()
            origin = high if minimum is None and maximum is not None else low
            blocks = []
            if kind is MetricKind.WAVEFORM:
                for shape in (
                    WaveformShape.SINE,
                    WaveformShape.SAWTOOTH,
                    WaveformShape.SQUARE,
                    WaveformShape.ECG,
                    WaveformShape.FLOW,
                ):
                    spec.shape = shape
                    for phase in (0.0, 0.137, 0.91):
                        service._waveform_phase["m.range_probe"] = phase
                        blocks.append(service._next_block("m.range_probe", spec)[0])
                service._waveform_phase.pop("m.range_probe", None)
            else:
                for shape in DistributionShape:
                    spec.distribution_shape = shape
                    blocks.extend(_distribution_samples(spec, phase) for phase in (0.0, 0.137, 0.91))
            values = [sample for block in blocks for sample in block]
            bounded = all(low <= sample <= high for sample in values)
            finite = all(sample.is_finite() and math.isfinite(float(sample)) for sample in values)
            quantized = all((sample - origin) % resolution == 0 for sample in values)
            equal_bounds = minimum is not None and minimum == maximum
            constant = not equal_bounds or all(sample == minimum for sample in values)
            if not (values and (low, high) == expected_range and bounded and finite and quantized and constant):
                range_failures.append(
                    (kind.value, case, (low, high), bounded, finite, quantized, constant),
                )
    report.check(
        not range_failures,
        "generated waveform and distribution blocks are finite, bounded, and resolution-aligned",
        repr(range_failures),
    )

    equal_handles = []
    for kind in (MetricKind.WAVEFORM, MetricKind.DISTRIBUTION):
        handle = service.add_metric(
            MetricSpec(
                label=f"Equal {kind.value} range",
                kind=kind,
                minimum=Decimal("2.5"),
                maximum=Decimal("2.5"),
                resolution=Decimal("0.1"),
            ),
        )
        equal_handles.append(handle)
    equal_ranges = [
        service.mdib.entities.by_handle(handle).descriptor.TechnicalRange[0]
        for handle in equal_handles
    ]
    report.check(
        all(
            item.Lower == item.Upper == Decimal("2.5")
            and item.StepWidth == service.mdib.entities.by_handle(handle).descriptor.Resolution == Decimal("0.1")
            for handle, item in zip(equal_handles, equal_ranges, strict=True)
        ),
        "provider accepts equal sample ranges without changing their TechnicalRange",
        repr([(item.Lower, item.Upper, item.StepWidth) for item in equal_ranges]),
    )
    for handle in equal_handles:
        service.remove_metric(handle)

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

    block = [Decimal(index) for index in range(DISTRIBUTION_BINS)]
    service.set_samples(dist, block)
    report.check(service.get_samples(dist) == block, "a distribution takes samples it is given")
    report.check(
        wait_until(lambda: service.get_samples(dist) != block, timeout=3.0) is False,
        "and setting one by hand takes it off the generator, so the block survives",
        str(service.get_samples(dist)),
    )
    replacement = [Decimal("9")] * DISTRIBUTION_BINS
    service.set_samples(dist, replacement)
    report.check(
        service.get_samples(dist) == replacement,
        "a second block replaces the first rather than appending",
        str(service.get_samples(dist)),
    )
    try:
        service.set_samples(dist, [Decimal("9")] * (DISTRIBUTION_BINS - 1))
    except ValueError as exc:
        report.check("exactly" in str(exc), "a distribution rejects a block with the wrong geometry", str(exc))
    else:
        report.check(False, "a distribution rejects a block with the wrong geometry", "it was accepted")  # noqa: FBT003

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


def check_metric_removal_dependencies(report: Report, service: ProviderService) -> None:
    print("\n4. Removing a metric removes everything that depends on it")

    metric = service.add_metric(
        MetricSpec(
            label="Dependency source",
            kind=MetricKind.NUMBER,
            section="Dependency section",
            controllable=True,
            initial_value=Decimal("1"),
        ),
    )
    survivor = service.add_metric(MetricSpec(label="Dependency survivor", kind=MetricKind.NUMBER))
    operation = service.operation_handle_for(metric)
    alert = service.add_alert(AlertSpec(label="Dependent alarm", source_handle=metric))
    signals = service.signal_handles_for(alert)
    channel = "ch.dependency_section"
    vmd = "vmd.dependency_section"
    target_actions = [
        service.add_action(ActionSpec(label="Metric target dependency", target_handle=metric)),
        service.add_action(ActionSpec(label="Operation target dependency", target_handle=operation)),
        service.add_action(ActionSpec(label="Condition target dependency", target_handle=alert)),
        *(
            service.add_action(ActionSpec(label=f"Signal {index} target dependency", target_handle=signal))
            for index, signal in enumerate(signals)
        ),
        service.add_action(ActionSpec(label="Channel target dependency", target_handle=channel)),
        service.add_action(ActionSpec(label="VMD target dependency", target_handle=vmd)),
    ]
    effect_action = service.add_action(
        ActionSpec(
            label="Effect dependency",
            target_handle=constants.MDS_HANDLE,
            effects={metric: Decimal("2")},
        ),
    )
    cascading_action = service.add_action(
        ActionSpec(label="Cascading action dependency", target_handle=target_actions[0]),
    )
    surviving_action = service.add_action(
        ActionSpec(label="Unrelated action", target_handle=constants.MDS_HANDLE, effects={survivor: Decimal("3")}),
    )

    observer_failures = []

    def check_observer_state(_deleted: dict) -> None:
        live_handles = {entity.handle for _, entity in service.mdib.entities.items()}
        operation_targets = {
            entity.handle: entity.descriptor.OperationTarget
            for _, entity in service.mdib.entities.items()
            if getattr(entity.descriptor, "OperationTarget", None) is not None
        }
        dangling_actions = {
            action_handle: spec.target_handle
            for action_handle, spec in service.list_actions().items()
            if spec.target_handle not in live_handles
        }
        dangling_operations = {
            operation_handle: target
            for operation_handle, target in operation_targets.items()
            if target not in live_handles
        }
        dangling_registered = {
            operation_handle: operation.operation_target_handle
            for operation_handle, operation in service._sco._registered_operations.items()  # noqa: SLF001
            if operation.operation_target_handle not in live_handles
        }
        if dangling_actions or dangling_operations or dangling_registered:
            observer_failures.append((dangling_actions, dangling_operations, dangling_registered))

    observableproperties.bind(service.mdib, deleted_descriptors_by_handle=check_observer_state)
    try:
        service.remove_metric(metric)
    finally:
        observableproperties.unbind(service.mdib, deleted_descriptors_by_handle=check_observer_state)

    removed_handles = [
        metric,
        operation,
        alert,
        *signals,
        channel,
        vmd,
        *target_actions,
        effect_action,
        cascading_action,
    ]
    report.check(
        all(handle is None or service.mdib.entities.by_handle(handle) is None for handle in removed_handles),
        "the complete metric descriptor dependency closure leaves the MDIB",
        str([handle for handle in removed_handles if handle and service.mdib.entities.by_handle(handle) is not None]),
    )
    report.check(
        metric not in service.list_metrics() and service.operation_handle_for(metric) is None,
        "metric and operation bookkeeping is cleared",
    )
    report.check(
        alert not in service.list_alerts() and service.signal_handles_for(alert) == [],
        "alert source bookkeeping is cleared",
    )
    report.check(
        all(action not in service.list_actions() for action in [*target_actions, effect_action, cascading_action]),
        "target, effect, and cascading action bookkeeping is cleared",
    )
    report.check(
        surviving_action in service.list_actions()
        and service.mdib.entities.by_handle(surviving_action) is not None
        and survivor in service.list_metrics(),
        "unrelated actions and metrics survive",
    )
    report.check(not observer_failures, "deletion observers never see a dangling operation target", str(observer_failures))
    report.check("Dependency section" not in service.sections(), "the emptied section bookkeeping is cleared")

    mdib_node, _ = service.mdib.reconstruct_mdib_with_context_states()
    reconstructed_handles = set(mdib_node.xpath("//*[@Handle]/@Handle"))
    reconstructed_targets = set(mdib_node.xpath("//*[@OperationTarget]/@OperationTarget"))
    report.check(
        reconstructed_targets <= reconstructed_handles
        and all(spec.target_handle in reconstructed_handles for spec in service.list_actions().values()),
        "the reconstructed MDIB and service actions have no dangling targets",
        str(sorted(reconstructed_targets - reconstructed_handles)),
    )

    direct_alert = service.add_alert(AlertSpec(label="Direct removal alarm", source_handle=survivor))
    direct_signals = service.signal_handles_for(direct_alert)
    direct_actions = [
        service.add_action(ActionSpec(label="Direct condition target", target_handle=direct_alert)),
        *(
            service.add_action(ActionSpec(label=f"Direct signal {index} target", target_handle=signal))
            for index, signal in enumerate(direct_signals)
        ),
    ]
    direct_cascade = service.add_action(
        ActionSpec(label="Direct cascading target", target_handle=direct_actions[0]),
    )

    direct_observer_failures = []
    observer_failures = direct_observer_failures
    observableproperties.bind(service.mdib, deleted_descriptors_by_handle=check_observer_state)
    try:
        service.remove_alert(direct_alert)
    finally:
        observableproperties.unbind(service.mdib, deleted_descriptors_by_handle=check_observer_state)

    directly_removed = [direct_alert, *direct_signals, *direct_actions, direct_cascade]
    report.check(
        all(service.mdib.entities.by_handle(handle) is None for handle in directly_removed)
        and all(handle not in service.list_actions() for handle in [*direct_actions, direct_cascade]),
        "direct alert removal deletes actions targeting its condition and every signal",
        str([handle for handle in directly_removed if service.mdib.entities.by_handle(handle) is not None]),
    )
    report.check(
        survivor in service.list_metrics() and service.mdib.entities.by_handle(survivor) is not None,
        "direct alert removal leaves its source metric in place",
    )
    report.check(
        not direct_observer_failures,
        "direct alert deletion observers never see a dangling operation target",
        str(direct_observer_failures),
    )

    mdib_node, _ = service.mdib.reconstruct_mdib_with_context_states()
    reconstructed_handles = set(mdib_node.xpath("//*[@Handle]/@Handle"))
    reconstructed_targets = set(mdib_node.xpath("//*[@OperationTarget]/@OperationTarget"))
    report.check(
        reconstructed_targets <= reconstructed_handles
        and all(spec.target_handle in reconstructed_handles for spec in service.list_actions().values()),
        "direct alert removal leaves no dangling target in the reconstructed MDIB or service",
        str(sorted(reconstructed_targets - reconstructed_handles)),
    )

    service.remove_action(surviving_action)
    service.remove_metric(survivor)


def check_section_removal(report: Report, service: ProviderService) -> None:
    print("\n5. Sections follow their metric ownership")

    first = service.add_metric(
        MetricSpec(label="Shared section first", kind=MetricKind.NUMBER, section="Shared section"),
    )
    second = service.add_metric(
        MetricSpec(label="Shared section second", kind=MetricKind.NUMBER, section="Shared section"),
    )
    channel_handle = "ch.shared_section"
    vmd_handle = "vmd.shared_section"
    channel_action = service.add_action(ActionSpec(label="Channel action", target_handle=channel_handle))
    vmd_action = service.add_action(ActionSpec(label="VMD action", target_handle=vmd_handle))

    service.remove_metric(first)
    report.check(
        service.mdib.entities.by_handle(channel_handle) is not None
        and service.mdib.entities.by_handle(vmd_handle) is not None
        and service.mdib.entities.by_handle(second) is not None,
        "removing one of multiple metrics retains its Channel and VMD",
    )
    report.check(
        service.sections() == {"Shared section": channel_handle}
        and channel_action in service.list_actions()
        and vmd_action in service.list_actions(),
        "shared section and action bookkeeping remain while an owner exists",
    )

    service.remove_metric(second)
    removed = [second, channel_handle, vmd_handle, channel_action, vmd_action]
    report.check(
        all(service.mdib.entities.by_handle(handle) is None for handle in removed),
        "removing the final metric removes its Channel, VMD, and targeted action from the MDIB",
        str([handle for handle in removed if service.mdib.entities.by_handle(handle) is not None]),
    )
    report.check(
        "Shared section" not in service.sections()
        and channel_action not in service.list_actions()
        and vmd_action not in service.list_actions(),
        "final section and action bookkeeping are cleared",
    )
    mdib_xml = etree.tostring(service.mdib.reconstruct_mdib_with_context_states()[0])
    report.check(
        all(handle.encode() not in mdib_xml for handle in removed),
        "removed section descriptors are absent from the reconstructed MDIB",
    )


def check_section_handle_types(report: Report, service: ProviderService) -> None:
    """Generated section handles never reuse descriptors of another type."""
    section = "Occupied section"
    vmd_handle = "vmd.occupied_section"
    channel_handle = "ch.occupied_section"
    cases = (
        (vmd_handle, pm.ChannelDescriptor, constants.VMD_HANDLE, channel_handle),
        (channel_handle, pm.VmdDescriptor, constants.MDS_HANDLE, vmd_handle),
    )
    for occupied_handle, node_type, parent_handle, absent_handle in cases:
        witness = service.mdib.entities.new_entity(node_type, occupied_handle, parent_handle)
        service._create_entities([witness])
        before_handles = {handle for handle, _ in service.mdib.entities.items()}
        before_metrics = service.list_metrics()
        before_sections = service.sections()
        try:
            try:
                service.add_metric(MetricSpec(label="Blocked metric", kind=MetricKind.NUMBER, section=section))
            except ValueError as exc:
                report.check(
                    occupied_handle in str(exc),
                    f"a wrong descriptor type at {occupied_handle} is rejected",
                    str(exc),
                )
            else:
                report.check(False, f"a wrong descriptor type at {occupied_handle} is rejected", "it was reused")
            report.check(
                {handle for handle, _ in service.mdib.entities.items()} == before_handles
                and service.list_metrics() == before_metrics
                and service.sections() == before_sections
                and service.mdib.entities.by_handle(absent_handle) is None,
                f"rejecting {occupied_handle} leaves sections and metrics unchanged",
            )
        finally:
            existing = service.mdib.entities.by_handle(occupied_handle)
            if existing is not None:
                with service.mdib.descriptor_transaction() as mgr:
                    mgr.remove_entity(existing)


def check_metric_value_validation(report: Report, service: ProviderService) -> None:
    print("\n6. Metric writes and action effects share validation")

    number = service.add_metric(
        MetricSpec(
            label="Validated number",
            kind=MetricKind.NUMBER,
            minimum=Decimal("1"),
            maximum=Decimal("10"),
            initial_value=Decimal("2"),
        ),
    )
    text = service.add_metric(MetricSpec(label="Validated text", kind=MetricKind.TEXT, initial_value="old"))
    choice = service.add_metric(
        MetricSpec(
            label="Validated choice",
            kind=MetricKind.CHOICE,
            allowed_values=("1", "RUN"),
            initial_value="RUN",
        ),
    )

    service.set_value(number, "7")
    report.check(
        service.get_value(number) == Decimal("7"),
        "a local numeric string is stored as a Decimal",
        repr(service.get_value(number)),
    )
    try:
        service.set_value(choice, "INVALID")
    except ValueError as exc:
        report.check("allowed values" in str(exc), "a local invalid choice is rejected", str(exc))
    else:
        report.check(False, "a local invalid choice is rejected", "it was accepted")  # noqa: FBT003
    report.check(service.get_value(choice) == "RUN", "a rejected local choice leaves its value unchanged")

    mixed = service.add_action(
        ActionSpec(
            label="Mixed value action",
            target_handle=constants.MDS_HANDLE,
            effects={number: "3", text: "001", choice: "1"},
        ),
    )
    service.run_action(mixed)
    report.check(
        service.get_value(number) == Decimal("3")
        and service.get_value(text) == "001"
        and service.get_value(choice) == "1",
        "a mixed-kind local action resolves values from each target kind",
    )

    invalid_effects = [
        ("Out of range action", {text: "changed", number: "11"}, "out-of-range"),
        ("Invalid choice action", {text: "changed", choice: "INVALID"}, "invalid-choice"),
        ("Missing effect action", {text: "changed", "m.missing_effect": "1"}, "missing-target"),
    ]
    invalid_actions = []
    for label, effects, description in invalid_effects:
        action = service.add_action(ActionSpec(label=label, target_handle=constants.MDS_HANDLE, effects=effects))
        invalid_actions.append(action)
        before = (service.get_value(number), service.get_value(text), service.get_value(choice))
        try:
            service.run_action(action)
        except (KeyError, ValueError):
            pass
        else:
            report.check(False, f"a local {description} action fails", "it reported success")  # noqa: FBT003
            continue
        after = (service.get_value(number), service.get_value(text), service.get_value(choice))
        report.check(after == before, f"a local {description} action is all-or-nothing", str(after))

    for action in [mixed, *invalid_actions]:
        service.remove_action(action)
    for handle in (number, text, choice):
        service.remove_metric(handle)


def check_decimal_boundaries(report: Report, service: ProviderService) -> None:
    print("\n7. Decimal wire boundaries")

    non_finite = (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"))
    rejected_resolutions = []
    for value in (*non_finite, Decimal("0"), Decimal("-0.1")):
        try:
            MetricSpec(label="Resolution boundary", kind=MetricKind.NUMBER, resolution=value)
        except ValueError:
            rejected_resolutions.append(value)
    report.check(
        rejected_resolutions == [*non_finite, Decimal("0"), Decimal("-0.1")],
        "resolution rejects non-finite, zero, and negative values",
        repr(rejected_resolutions),
    )

    invalid_limits = [
        lambda value: MetricSpec(label="Minimum boundary", kind=MetricKind.NUMBER, minimum=value),
        lambda value: MetricSpec(label="Maximum boundary", kind=MetricKind.NUMBER, maximum=value),
        lambda value: MetricSpec(
            label="Domain boundary",
            kind=MetricKind.DISTRIBUTION,
            domain_minimum=Decimal("0"),
            domain_maximum=value,
        ),
        lambda value: AlertSpec(label="Alert boundary", source_handle="m.none", upper_limit=value),
    ]
    limit_rejections = 0
    for build in invalid_limits:
        for value in non_finite:
            try:
                build(value)
            except ValueError:
                limit_rejections += 1
    report.check(
        limit_rejections == len(invalid_limits) * len(non_finite),
        "metric, domain, and alert limits reject NaN and both infinities",
        f"{limit_rejections} rejections",
    )

    for period in (
        Decimal("0"),
        Decimal("-1"),
        Decimal("1e-324"),
        Decimal("5e-324"),
        Decimal("1e-12"),
        *non_finite,
    ):
        try:
            MetricSpec(
                label="Period boundary",
                kind=MetricKind.WAVEFORM,
                sample_period=period,
            )
        except ValueError:
            continue
        report.check(False, f"sample period {period} is rejected", "it was accepted")  # noqa: FBT003
        break
    else:
        report.check(
            True,
            "sample periods reject invalid and oversized-block values",
        )

    boundary_period = constants.MIN_GENERATED_WAVEFORM_SAMPLE_PERIOD
    boundary_spec = MetricSpec(
        label="Boundary period",
        kind=MetricKind.WAVEFORM,
        sample_period=boundary_period,
    )
    boundary_block, _ = service._next_block("m.boundary_period", boundary_spec)
    report.check(
        len(boundary_block) == constants.MAX_GENERATED_WAVEFORM_BLOCK_SAMPLES,
        "the exact minimum sample period generates the maximum permitted block",
        f"{len(boundary_block)} samples",
    )

    boundary_spec.sample_period = boundary_period - Decimal("1e-30")
    try:
        service._next_block("m.below_boundary_period", boundary_spec)
    except ValueError:
        below_boundary_rejected = True
    else:
        below_boundary_rejected = False
    report.check(
        below_boundary_rejected,
        "a direct block request just below the period boundary is rejected",
    )

    mutated_period = MetricSpec(label="Mutated period", kind=MetricKind.WAVEFORM)
    mutated_period.handle = "m.mutated_period"
    mutated_period.section = "Unsafe period section"
    mutated_period.sample_period = Decimal("1e-12")
    period_before_handles = {handle for handle, _ in service.mdib.entities.items()}
    period_before_metrics = service.list_metrics()
    period_before_sections = service.sections()
    period_before_phase = dict(service._waveform_phase)
    period_before_pinned = set(service._pinned_samples)
    period_before_generator = service.generator_running
    try:
        service.add_metric(mutated_period)
    except ValueError:
        mutated_period_rejected = True
    else:
        mutated_period_rejected = False
    report.check(
        mutated_period_rejected
        and {
            handle for handle, _ in service.mdib.entities.items()
        }
        == period_before_handles
        and service.list_metrics() == period_before_metrics
        and service.sections() == period_before_sections
        and service._waveform_phase == period_before_phase
        and service._pinned_samples == period_before_pinned
        and service.generator_running == period_before_generator,
        "a mutated unsafe period leaves provider state unchanged",
    )

    before_handles = set(service.list_metrics())
    for upper in (Decimal("0"), Decimal("-1")):
        try:
            service.add_metric(
                MetricSpec(
                    label=f"Zero step {upper}",
                    kind=MetricKind.DISTRIBUTION,
                    domain_minimum=Decimal("0"),
                    domain_maximum=upper,
                ),
            )
        except ValueError:
            continue
        report.check(False, "a non-increasing distribution step is rejected", str(upper))  # noqa: FBT003
        break
    else:
        report.check(
            set(service.list_metrics()) == before_handles,
            "zero and negative distribution steps are rejected before MDIB mutation",
        )
    try:
        service.add_metric(
            MetricSpec(
                label="Wire zero step",
                kind=MetricKind.DISTRIBUTION,
                section="Uncreated decimal section",
                domain_minimum=Decimal("0"),
                domain_maximum=Decimal("1e-20"),
            ),
        )
    except ValueError as exc:
        wire_zero_rejected = "zero StepWidth on the wire" in str(exc)
    else:
        wire_zero_rejected = False
    report.check(
        wire_zero_rejected
        and service.mdib.entities.by_handle("m.wire_zero_step") is None
        and service.mdib.entities.by_handle("ch.uncreated_decimal_section") is None,
        "a step truncated to zero on the wire is rejected before section or metric mutation",
    )

    generated_limit_rejections = 0
    for kind in (MetricKind.WAVEFORM, MetricKind.DISTRIBUTION):
        for name, value in (("maximum", Decimal("1e400")), ("minimum", Decimal("-1e400"))):
            try:
                MetricSpec(label="Generated limit", kind=kind, **{name: value})
            except ValueError:
                generated_limit_rejections += 1
    report.check(
        generated_limit_rejections == 4,
        "waveform and distribution limits reject positive and negative float overflow",
        f"{generated_limit_rejections} rejections",
    )

    before_handles = {handle for handle, _ in service.mdib.entities.items()}
    before_metrics = service.list_metrics()
    before_sections = service.sections()
    add_rejections = 0
    for kind in (MetricKind.WAVEFORM, MetricKind.DISTRIBUTION):
        spec = MetricSpec(
            label=f"Mutated {kind.value}",
            kind=kind,
            handle=f"m.mutated_{kind.value}",
            section=f"Mutated {kind.value} section",
        )
        spec.maximum = Decimal("1e400")
        try:
            service.add_metric(spec)
        except ValueError:
            add_rejections += 1
    report.check(
        add_rejections == 2
        and {handle for handle, _ in service.mdib.entities.items()} == before_handles
        and service.list_metrics() == before_metrics
        and service.sections() == before_sections
        and not service._waveform_phase
        and not service._pinned_samples
        and not service.generator_running,
        "invalid generated ranges leave no section, MDIB, bookkeeping, or generator mutation",
        f"{add_rejections} rejections",
    )

    float_max = Decimal(str(sys.float_info.max))
    float_half = float_max / 2
    finite_blocks = []
    for kind in (MetricKind.WAVEFORM, MetricKind.DISTRIBUTION):
        for low, high in ((float_half, float_max), (-float_max, -float_half)):
            spec = MetricSpec(label="Float edge", kind=kind, minimum=low, maximum=high)
            if kind is MetricKind.WAVEFORM:
                block, _ = service._next_block("m.float_edge", spec)
            else:
                block = _distribution_samples(spec, 0.0)
            finite_blocks.append(bool(block) and all(sample.is_finite() and math.isfinite(float(sample)) for sample in block))
    report.check(
        all(finite_blocks),
        "accepted positive and negative finite-float edge ranges generate only finite samples",
        repr(finite_blocks),
    )

    recurring = [
        service.add_metric(
            MetricSpec(label=f"Recurring {kind.value} error", kind=kind),
        )
        for kind in (MetricKind.WAVEFORM, MetricKind.DISTRIBUTION)
    ]
    service.stop_generator()
    for handle in recurring:
        service.list_metrics()[handle].maximum = Decimal("1e400")
    generation_errors = []

    class GenerationErrorCounter(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage().startswith("sample generation failed for"):
                generation_errors.append(record)

    counter = GenerationErrorCounter()
    provider_logger = logging.getLogger("sdctoolbox.provider")
    provider_logger.addHandler(counter)
    try:
        service._publish_one_block()
        service._publish_one_block()
    finally:
        provider_logger.removeHandler(counter)
    report.check(
        all(handle in service._pinned_samples for handle in recurring) and len(generation_errors) == 2,
        "recurring waveform and distribution errors are each logged once and quarantined",
        f"{len(generation_errors)} errors",
    )
    for handle in recurring:
        service.remove_metric(handle)

    tiny_domain = service.add_metric(
        MetricSpec(
            label="Tiny domain",
            kind=MetricKind.DISTRIBUTION,
            domain_minimum=Decimal("0"),
            domain_maximum=Decimal("1e-10"),
        ),
    )
    tiny_range = service.mdib.entities.by_handle(tiny_domain).descriptor.DistributionRange
    mdib_xml = etree.tostring(service.mdib.reconstruct_mdib_with_context_states()[0], encoding="unicode")
    report.check(
        tiny_range.StepWidth != 0
        and tiny_range.Lower == Decimal("0")
        and tiny_range.Upper == Decimal("1e-10")
        and 'StepWidth="0.00000000000322581"' in mdib_xml,
        "a tiny valid domain keeps a non-zero relative-precision step on the wire",
        str(tiny_range.StepWidth),
    )

    number = service.add_metric(
        MetricSpec(label="Finite write", kind=MetricKind.NUMBER, initial_value=Decimal("7")),
    )
    for value in (*non_finite, "NaN", "+Infinity", "-Infinity"):
        try:
            service.set_value(number, value)
        except ValueError:
            continue
        report.check(False, f"direct write {value} is rejected", "it was accepted")  # noqa: FBT003
        break
    else:
        report.check(
            service.get_value(number) == Decimal("7"),
            "direct Decimal and parsed non-finite writes are rejected before metric mutation",
        )

    valid_samples = [Decimal(index) for index in range(DISTRIBUTION_BINS)]
    service.set_samples(tiny_domain, valid_samples)
    sample_rejections = 0
    for value in non_finite:
        invalid_samples = list(valid_samples)
        invalid_samples[0] = value
        try:
            service.set_samples(tiny_domain, invalid_samples)
        except ValueError:
            sample_rejections += 1
    report.check(
        sample_rejections == len(non_finite) and service.get_samples(tiny_domain) == valid_samples,
        "non-finite samples are rejected before sample state mutation",
        f"{sample_rejections} rejections",
    )

    effect_rejections = 0
    for value in non_finite:
        try:
            ActionSpec(label="Non-finite effect", target_handle=constants.MDS_HANDLE, effects={number: value})
        except ValueError:
            effect_rejections += 1
    action = service.add_action(
        ActionSpec(label="Parsed non-finite effect", target_handle=constants.MDS_HANDLE, effects={number: "NaN"}),
    )
    try:
        service.run_action(action)
    except ValueError:
        parsed_effect_rejected = True
    else:
        parsed_effect_rejected = False
    report.check(
        effect_rejections == len(non_finite)
        and parsed_effect_rejected
        and service.get_value(number) == Decimal("7"),
        "direct and parsed non-finite action effects are rejected before metric mutation",
        f"{effect_rejections} direct rejections",
    )

    service.remove_action(action)
    service.remove_metric(number)
    service.remove_metric(tiny_domain)


def check_concurrent_alert_evaluation(report: Report, service: ProviderService) -> None:
    print("\n8. Concurrent limit-alarm evaluation")

    first_source = service.add_metric(
        MetricSpec(label="First concurrent source", kind=MetricKind.NUMBER, initial_value=Decimal("0")),
    )
    second_source = service.add_metric(
        MetricSpec(label="Second concurrent source", kind=MetricKind.NUMBER, initial_value=Decimal("0")),
    )
    first_alarm = service.add_alert(
        AlertSpec(label="First concurrent alarm", source_handle=first_source, upper_limit=Decimal("10")),
    )
    second_alarm = service.add_alert(
        AlertSpec(label="Second concurrent alarm", source_handle=second_source, upper_limit=Decimal("10")),
    )

    first_evaluation_started = threading.Event()
    release_first_evaluation = threading.Event()
    worker_errors: list[Exception] = []
    original_write = service._write_alert_presence  # noqa: SLF001 - deterministic overlap fixture

    def overlapping_write(handle: str, *, present: bool) -> None:  # noqa: FBT001
        if handle == first_alarm and present and not first_evaluation_started.is_set():
            first_evaluation_started.set()
            if not release_first_evaluation.wait(timeout=5.0):
                raise TimeoutError("concurrent alert test did not release the first evaluation")
        original_write(handle, present=present)

    def update_source(handle: str) -> None:
        try:
            service.set_value(handle, Decimal("20"))
        except Exception as exc:  # noqa: BLE001 - report worker failures on the main thread
            worker_errors.append(exc)

    service._write_alert_presence = overlapping_write  # noqa: SLF001 - deterministic overlap fixture
    first_worker = threading.Thread(target=update_source, args=(first_source,), name="first-alarm-update")
    second_worker = threading.Thread(target=update_source, args=(second_source,), name="second-alarm-update")
    try:
        first_worker.start()
        first_started = first_evaluation_started.wait(timeout=5.0)
        if first_started:
            second_worker.start()
            second_worker.join(timeout=5.0)
        second_completed_during_first = first_started and not second_worker.is_alive()
    finally:
        release_first_evaluation.set()
        first_worker.join(timeout=5.0)
        if second_worker.ident is not None:
            second_worker.join(timeout=5.0)
        service._write_alert_presence = original_write  # noqa: SLF001 - restore the service method

    report.check(
        second_completed_during_first,
        "the second source updates while the first alarm evaluation is paused",
    )
    report.check(
        not first_worker.is_alive() and not second_worker.is_alive() and not worker_errors,
        "both concurrent evaluators finish without deadlock",
        str(worker_errors),
    )
    report.check(service.alert_present(first_alarm), "the first source raises its alarm")
    report.check(service.alert_present(second_alarm), "the overlapping second source raises its alarm")

    service.remove_alert(first_alarm)
    service.remove_alert(second_alarm)
    service.remove_metric(first_source)
    service.remove_metric(second_source)


def check_signals(report: Report, service: ProviderService) -> None:
    print("\n9. Acknowledging and delegating a signal")

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


def check_latching_signals(report: Report, service: ProviderService) -> None:
    print("\n10. Configurable signal manifestations and latching")
    service.add_metric(MetricSpec(label="Latch source", kind=MetricKind.NUMBER, initial_value=Decimal("0")))
    alarm = service.add_alert(
        AlertSpec(
            label="Latched alarm",
            source_handle="m.latch_source",
            upper_limit=Decimal("10"),
            signals=(
                AlertSignalSpec(AlertManifestation.VIS, latching=True),
                AlertSignalSpec(AlertManifestation.TAN),
            ),
        ),
    )
    states = service.signal_states(alarm)
    report.check(
        [(state.manifestation, state.latching) for state in states] == [("Vis", True), ("Tan", False)],
        "configured manifestations and latching reach their descriptors",
        str(states),
    )
    service.set_value("m.latch_source", Decimal("11"))
    service.set_value("m.latch_source", Decimal("0"))
    report.check(
        [state.summary() for state in service.signal_states(alarm)] == ["Vis:Latch", "Tan:Off"],
        "a clear condition latches only configured signals",
    )
    report.check(service.stop_latched_signals(alarm) == 1, "a latched signal can be stopped deliberately")
    report.check(
        [state.summary() for state in service.signal_states(alarm)] == ["Vis:Off", "Tan:Off"],
        "stopping a latched signal turns it off",
    )
    service.remove_alert(alarm)
    service.remove_metric("m.latch_source")


def check_contexts(report: Report, service: ProviderService) -> None:
    print("\n11. Patient and location contexts")

    default = service.get_location()
    report.check(
        default.facility == "HOSP" and default.bed == "Toolbox",
        "a provider starts with the default location associated",
        default.summary(),
    )
    report.check(
        service.get_patient().height is not None and service.get_patient().weight is not None,
        "and with a default metric patient attached",
        service.get_patient().summary(),
    )

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
            height=PatientMeasurement(
                value=Decimal("170.5"),
                unit=Coding(code="demo-cm", system="private", label="cm"),
            ),
            weight=PatientMeasurement(
                value=Decimal("72.4"),
                unit=Coding(code="demo-kg", system="private", label="kg"),
            ),
            race=Coding(code="demo-race", system="private", label="Demo race"),
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
    report.check(
        patient.height is not None
        and patient.height.value == Decimal("170.5")
        and patient.height.unit.code == "demo-cm",
        "height survives as a measured value with its coded unit",
        patient.height.summary() if patient.height else "none",
    )
    report.check(
        patient.weight is not None
        and patient.weight.value == Decimal("72.4")
        and patient.weight.unit.code == "demo-kg",
        "and weight does too",
        patient.weight.summary() if patient.weight else "none",
    )
    report.check(
        patient.race is not None and patient.race.code == "demo-race",
        "race survives as a coded value",
        patient.race.summary() if patient.race else "none",
    )

    entity = service.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE)
    entity.update()
    associated = next(
        state
        for state in entity.states.values()
        if state.ContextAssociation == pm_types.ContextAssociation.ASSOCIATED
    )
    core = associated.CoreData
    report.check(
        isinstance(core.Height, pm_types.Measurement)
        and core.Height.MeasuredValue == Decimal("170.5")
        and core.Height.MeasurementUnit.Code == "demo-cm"
        and isinstance(core.Weight, pm_types.Measurement)
        and core.Weight.MeasuredValue == Decimal("72.4")
        and core.Weight.MeasurementUnit.Code == "demo-kg"
        and isinstance(core.Race, pm_types.CodedValue)
        and core.Race.Code == "demo-race",
        "the MDIB uses BICEPS Measurement and CodedValue members",
    )
    mdib_node, _ = service.mdib.reconstruct_mdib_with_context_states()
    xml = etree.tostring(mdib_node, encoding="unicode")
    report.check(
        'Height MeasuredValue="170.5"' in xml
        and 'MeasurementUnit Code="demo-cm"' in xml
        and 'Weight MeasuredValue="72.4"' in xml
        and 'MeasurementUnit Code="demo-kg"' in xml
        and 'Race Code="demo-race"' in xml,
        "and those demographics serialise into the MDIB",
    )

    # A context is a multi-state entity: the old state is disassociated, not overwritten.
    service.set_patient(PatientInfo(given_name="Grace", family_name="Hopper"))
    report.check(
        service.get_patient().summary() == "Grace Hopper",
        "a new patient replaces the old one",
        service.get_patient().summary(),
    )
    entity.update()
    associations = sorted(str(state.ContextAssociation) for state in entity.states.values())
    report.check(
        associations.count("Assoc") == 1 and associations.count("Dis") >= 2,
        "previous patients are kept as disassociated states, not deleted",
        str(associations),
    )

    service.set_patient(PatientInfo())
    report.check(service.get_patient().is_empty(), "an empty patient detaches everyone")
    entity.update()
    report.check(
        all(
            str(state.ContextAssociation) != "Assoc"
            for state in entity.states.values()
        ),
        "and leaves no associated state behind",
    )

    # sdc11073 3.0.0 would route an exponent-form Decimal through float while serialising.
    # The provider must preserve a representable exponent value as fixed-point xsd:decimal.
    service.set_patient(
        PatientInfo(
            given_name="Tiny",
            height=PatientMeasurement(
                value=Decimal("1E-7"),
                unit=Coding(code="demo-m", system="private", label="m"),
            ),
        ),
    )
    tiny = service.get_patient()
    report.check(
        tiny.height is not None and tiny.height.value == Decimal("1E-7"),
        "an exponent-form demographic value survives without becoming zero",
        str(tiny.height.value) if tiny.height else "none",
    )
    mdib_node, _ = service.mdib.reconstruct_mdib_with_context_states()
    report.check(
        'Height MeasuredValue="0.0000001"' in etree.tostring(mdib_node, encoding="unicode"),
        "and serialises as lossless fixed-point xsd:decimal",
    )
    try:
        service.set_patient(
            PatientInfo(
                height=PatientMeasurement(
                    value=Decimal("1E+999999"),
                    unit=Coding(code="demo-m", system="private", label="m"),
                ),
            ),
        )
    except ValueError as exc:
        report.check("wire characters" in str(exc), "an impractical exponent is refused before commit", str(exc))
    else:
        report.check(False, "an impractical exponent is refused before commit", "it was accepted")  # noqa: FBT003
    report.check(
        service.get_patient().given_name == "Tiny",
        "a rejected demographic update leaves the associated patient unchanged",
        service.get_patient().summary(),
    )
    try:
        service.set_patient(
            PatientInfo(
                height=PatientMeasurement(
                    value=Decimal("1.234567890123456789012345E-7"),
                    unit=Coding(code="demo-m", system="private", label="m"),
                ),
            ),
        )
    except ValueError as exc:
        report.check("losing precision" in str(exc), "a truncating decimal is refused before commit", str(exc))
    else:
        report.check(False, "a truncating decimal is refused before commit", "it was accepted")  # noqa: FBT003

    invalid_coding_messages = []
    for kwargs in (
        {"code": "bad\x01", "system": "private"},
        {"code": "demo", "system": "urn:\x01"},
        {"code": "demo", "system": "private", "label": "bad\x01"},
        {"code": "demo", "system": "urn:%"},
    ):
        try:
            Coding(**kwargs)
        except ValueError as exc:
            invalid_coding_messages.append(str(exc))
    report.check(
        len(invalid_coding_messages) == 4,  # noqa: PLR2004
        "demographic coding rejects XML-invalid values and malformed system URIs",
        str(invalid_coding_messages),
    )
    try:
        PatientInfo(given_name="Broken\x01")
    except ValueError as exc:
        report.check(
            "not allowed in XML" in str(exc),
            "an XML-invalid patient is refused before its context transaction",
            str(exc)[:70],
        )
    else:
        report.check(False, "an XML-invalid patient is refused before its context transaction", "it was accepted")  # noqa: FBT003
    report.check(
        service.get_patient().given_name == "Tiny",
        "a rejected patient leaves the prior associated patient unchanged",
        service.get_patient().summary(),
    )

    # A peer can have one PatientContext below each MDS. The consumer must not select an
    # arbitrary context merely because entity iteration happens to return it first.
    def patient_entity(handle: str, name: str):  # noqa: ANN202 - minimal RemoteDevice fixture
        core = pm_types.PatientDemographicsCoreData()
        core.Givenname = name
        return SimpleNamespace(
            handle=handle,
            states={
                f"{handle}.state": SimpleNamespace(
                    ContextAssociation=pm_types.ContextAssociation.ASSOCIATED,
                    CoreData=core,
                ),
            },
        )

    first = patient_entity("PC.first", "First")
    second = patient_entity("PC.second", "Second")
    remote = RemoteDevice.__new__(RemoteDevice)
    remote._lock = threading.RLock()  # noqa: SLF001 - fixture for the public read API
    remote._mdib = SimpleNamespace(  # noqa: SLF001 - fixture for the public read API
        entities=SimpleNamespace(by_node_type=lambda _: [second, first]),
    )
    contexts = remote.patient_contexts()
    report.check(
        list(contexts) == ["PC.first", "PC.second"] and {p.given_name for p in contexts.values()} == {"First", "Second"},
        "multiple peer patient contexts stay distinct and deterministic",
        str(contexts),
    )
    report.check(
        remote.patient().is_empty(),
        "the singular peer-patient helper declines an ambiguous multi-context device",
    )

    bad = PatientInfo(date_of_birth="not a date")
    try:
        service.set_patient(bad)
    except (ValueError, TypeError) as exc:
        report.check(bool(str(exc)), "an unusable date of birth is refused", str(exc)[:60])
    else:
        report.check(False, "an unusable date of birth is refused", "it was accepted")  # noqa: FBT003


def check_presets(report: Report) -> None:
    print("\n12. Presets")

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
        (folder / "malformed-shape.json").write_text('{"metrics": 1}', encoding="utf-8")
        (folder / "good.json").write_text(
            '{"name": "Good one", "metrics": [{"label": "v", "kind": "number"}]}',
            encoding="utf-8",
        )
        found = config.list_presets(folder)
        report.check(
            [preset.name for preset in found] == ["Good one"],
            "unreadable and malformed presets are skipped rather than breaking the list",
            str([preset.name for preset in found]),
        )

    report.check(
        config.list_presets(Path(tempfile.gettempdir()) / "no-such-preset-folder") == [],
        "a missing presets folder is not an error",
    )


def check_foreign_consumer_operations(report: Report) -> None:
    print("\n13. Foreign consumer operation selection")

    class SetClient:
        def __init__(self) -> None:
            self.calls = []
            self.activate_results = {}

        def _future(self, state=msg_types.InvocationState.FINISHED):
            info = SimpleNamespace(InvocationState=state, InvocationErrorMessage=None)
            result = SimpleNamespace(InvocationInfo=info)
            return SimpleNamespace(result=lambda timeout: result)

        def set_numeric_value(self, handle, value):
            self.calls.append(("number", handle, value))
            return self._future()

        def set_string(self, handle, value):
            self.calls.append(("string", handle, value))
            return self._future()

        def activate(self, handle, arguments):
            self.calls.append(("activate", handle, arguments))
            return self._future(self.activate_results.get(handle, msg_types.InvocationState.FINISHED))

    def metric_entity(handle, node_type, *, lower="0", upper="100", resolution=None):
        descriptor = SimpleNamespace(
            AllowedValue=[],
            TechnicalRange=[SimpleNamespace(Lower=Decimal(lower), Upper=Decimal(upper))],
            Resolution=resolution,
        )
        state = SimpleNamespace(MetricValue=SimpleNamespace(Value=None))
        return SimpleNamespace(node_type=node_type, descriptor=descriptor, state=state, parent_handle=None)

    def operation_entity(
        target,
        node_type,
        lower,
        upper,
        mode=pm_types.OperatingMode.ENABLED,
    ):
        state = SimpleNamespace(
            AllowedRange=[SimpleNamespace(Lower=Decimal(lower), Upper=Decimal(upper))],
        )
        if mode is not None:
            state.OperatingMode = mode
        return SimpleNamespace(
            node_type=node_type,
            descriptor=SimpleNamespace(OperationTarget=target),
            state=state,
        )

    def remote_for(entities):
        client = SetClient()
        remote = RemoteDevice.__new__(RemoteDevice)
        remote._lock = threading.RLock()
        remote._mdib = SimpleNamespace(entities=entities)
        remote._consumer = SimpleNamespace(set_service_client=client)
        return remote, client

    cases = (
        (
            MetricKind.NUMBER,
            pm.NumericMetricDescriptor,
            pm.SetValueOperationDescriptor,
            pm.SetStringOperationDescriptor,
        ),
        (
            MetricKind.TEXT,
            pm.StringMetricDescriptor,
            pm.SetStringOperationDescriptor,
            pm.SetValueOperationDescriptor,
        ),
        (
            MetricKind.CHOICE,
            pm.EnumStringMetricDescriptor,
            pm.SetStringOperationDescriptor,
            pm.SetValueOperationDescriptor,
        ),
    )
    selections = []
    invocations = []
    for kind, metric_type, compatible_type, incompatible_type in cases:
        for enabled_first in (False, True):
            metric_handle = f"metric.{kind.value}.{'enabled' if enabled_first else 'disabled'}-first"
            disabled_handle = f"operation.{kind.value}.disabled"
            enabled_handle = f"operation.{kind.value}.enabled"
            disabled = operation_entity(
                metric_handle,
                compatible_type,
                "1",
                "2",
                pm_types.OperatingMode.DISABLED,
            )
            enabled = operation_entity(metric_handle, compatible_type, "10", "20")
            incompatible = operation_entity(metric_handle, incompatible_type, "90", "99")
            ordered = (
                [(enabled_handle, enabled), ("operation.incompatible", incompatible), (disabled_handle, disabled)]
                if enabled_first
                else [(disabled_handle, disabled), ("operation.incompatible", incompatible), (enabled_handle, enabled)]
            )
            entities = {metric_handle: metric_entity(metric_handle, metric_type), **dict(ordered)}
            remote, client = remote_for(entities)
            metric = remote.metrics()[metric_handle]
            selections.append(
                metric.kind is kind
                and metric.operation_handles == tuple(
                    handle for handle, entity in ordered if entity.node_type == compatible_type
                )
                and metric.selected_operation_handle == enabled_handle
                and metric.controllable_now
                and (metric.minimum, metric.maximum) == (Decimal("10"), Decimal("20"))
            )
            value = Decimal("15") if kind is MetricKind.NUMBER else "value"
            remote.set_value(metric_handle, value)
            method = "number" if kind is MetricKind.NUMBER else "string"
            invocations.append(client.calls == [(method, enabled_handle, value)])

    report.check(
        all(selections),
        "enabled kind-compatible operations keep their own ranges in either entity order",
        str(selections),
    )
    report.check(
        all(invocations),
        "the operations advertised as usable are the operations invoked",
        str(invocations),
    )

    remote_resolutions = []
    for resolution in (Decimal("0.1"), Decimal("0.3")):
        metric_handle = f"metric.fractional.{resolution}"
        entities = {
            metric_handle: metric_entity(
                metric_handle,
                pm.NumericMetricDescriptor,
                lower="0",
                upper="1",
                resolution=resolution,
            ),
        }
        remote, _ = remote_for(entities)
        metric = remote.metrics()[metric_handle]
        remote_resolutions.append(
            metric.resolution == resolution
            and metric.minimum == Decimal("0")
            and metric.maximum == Decimal("1")
        )
    report.check(
        all(remote_resolutions),
        "foreign numeric descriptor resolutions remain exact in metric snapshots",
        str(remote_resolutions),
    )

    absent_metric = "metric.absent-mode"
    absent_set = "operation.absent-mode"
    disabled_action = "action.disabled"
    enabled_action = "action.enabled"
    absent_action = "action.absent-mode"

    def action_entity(mode):
        state = SimpleNamespace()
        if mode is not None:
            state.OperatingMode = mode
        return SimpleNamespace(
            node_type=pm.ActivateOperationDescriptor,
            descriptor=SimpleNamespace(OperationTarget="mds"),
            state=state,
        )

    entities = {
        absent_metric: metric_entity(absent_metric, pm.NumericMetricDescriptor),
        absent_set: operation_entity(absent_metric, pm.SetValueOperationDescriptor, "30", "40", mode=None),
        disabled_action: action_entity(pm_types.OperatingMode.DISABLED),
        enabled_action: action_entity(pm_types.OperatingMode.ENABLED),
        absent_action: action_entity(None),
    }
    remote, client = remote_for(entities)
    metric = remote.metrics()[absent_metric]
    actions = remote.actions()
    client.activate_results = {
        enabled_action: msg_types.InvocationState.FINISHED_MOD,
        absent_action: msg_types.InvocationState.CANCELLED,
    }
    remote.set_value(absent_metric, Decimal("35"))
    disabled_result = remote.run_action(disabled_action)
    enabled_result = remote.run_action(enabled_action)
    absent_result = remote.run_action(absent_action)
    report.check(
        metric.controllable_now
        and metric.selected_operation_handle == absent_set
        and (metric.minimum, metric.maximum) == (Decimal("30"), Decimal("40"))
        and not actions[disabled_action].enabled
        and actions[enabled_action].enabled
        and actions[absent_action].enabled,
        "absent OperatingMode defaults to enabled for set and activate operations",
    )
    report.check(
        client.calls == [
            ("number", absent_set, Decimal("35")),
            ("activate", enabled_action, None),
            ("activate", absent_action, None),
        ],
        "disabled actions do not reach transport while enabled and default-enabled actions do",
        str(client.calls),
    )
    report.check(
        disabled_result is msg_types.InvocationState.FAILED
        and enabled_result is msg_types.InvocationState.FINISHED_MOD
        and absent_result is msg_types.InvocationState.CANCELLED,
        "action invocation returns local rejection or the enabled transport result",
        f"{disabled_result}, {enabled_result}, {absent_result}",
    )

    calls_before = list(client.calls)
    remote_write_rejections = 0
    for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        try:
            remote.set_value(absent_metric, value)
        except ValueError:
            remote_write_rejections += 1
    report.check(
        remote_write_rejections == 3 and client.calls == calls_before,  # noqa: PLR2004
        "non-finite outbound numeric writes are rejected before transport",
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
        check_metric_removal_dependencies(report, service)
        check_section_removal(report, service)
        check_section_handle_types(report, service)
        check_metric_value_validation(report, service)
        check_decimal_boundaries(report, service)
        check_concurrent_alert_evaluation(report, service)
        check_signals(report, service)
        check_latching_signals(report, service)
        check_contexts(report, service)
        check_presets(report)
        check_foreign_consumer_operations(report)
    finally:
        service.stop()

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
