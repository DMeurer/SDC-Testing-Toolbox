"""Every shipped preset has to build into a working device.

A preset is data, and data with no test is a guess. This builds each one on a real provider,
which is the only way to find a dangling alarm source, an action naming a section that no
metric created, or a metric type the descriptor will not serialise.

Exit code 0 means all checks passed.

Usage:  .venv/Scripts/python.exe tests/presets.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report  # noqa: E402
from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.mdib.descriptorcontainers import NumericMetricDescriptorContainer  # noqa: E402
from sdc11073.mdib.statecontainers import NumericMetricStateContainer  # noqa: E402
from sdc11073.xml_types import pm_qnames as pm, pm_types  # noqa: E402

from sdctoolbox import config, constants  # noqa: E402
from sdctoolbox.model import CODING_SYSTEMS, MetricKind  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402


EXPECTED_PRESET_FILES = {
    "endoscopic-camera.json",
    "hf-generator.json",
    "infusion-pump.json",
    "insufflator.json",
    "operating-table.json",
    "patient-monitor.json",
    "surgical-microscope.json",
    "ventilation-pressure.json",
    "ventilator.json",
}


def check_focused_profile(report: Report, path: Path, service: ProviderService) -> None:
    """The small numeric examples retain their exact semantics on the wire."""
    if path.stem == "operating-table":
        section, channel, vmd = "Position", "ch.position", "vmd.position"
        expected = [
            ("m.height", "Height", "780", "500", "1100", "1", "263442", "mm", "table_height", constants.CODING_SYSTEM_PRIVATE),
            ("m.trendelenburg", "Trendelenburg", "-8", "-30", "30", "0.1", "262880", "°", "table_trendelenburg", constants.CODING_SYSTEM_PRIVATE),
            ("m.lateral_angle", "Lateral Angle", "7.6", "-30", "30", "0.1", "262880", "°", "table_lateral_angle", constants.CODING_SYSTEM_PRIVATE),
        ]
    elif path.stem == "ventilation-pressure":
        section, channel, vmd = "Pressure", "ch.pressure", "vmd.pressure"
        expected = [
            ("m.ventilation_pressure", "Ventilation Pressure", "18", "0", "40", "1", "266048", "cmH₂O", "151956", constants.CODING_SYSTEM_MDC),
        ]
    else:
        return

    specs = service.list_metrics()
    report.check(
        set(specs) == {row[0] for row in expected}
        and all(spec.kind is MetricKind.NUMBER and not spec.controllable for spec in specs.values())
        and not service.list_alerts()
        and not service.list_actions()
        and not service.mdib.entities.by_node_type(pm.SetValueOperationDescriptor),
        "focused profile has only its read-only numeric metrics, without alarms or actions",
    )
    report.check(service.sections() == {section: channel}, "focused profile keeps its declared section")
    node = etree.fromstring(etree.tostring(service.mdib.reconstruct_mdib_with_context_states()[0]))
    for handle, label, value, lower, upper, resolution, unit_code, unit_label, type_code, type_system in expected:
        descriptors = node.xpath(".//*[@Handle=$handle]", handle=handle)
        states = node.xpath(".//*[@DescriptorHandle=$handle]", handle=handle)
        if not report.check(len(descriptors) == len(states) == 1, f"{handle} serializes one descriptor and state"):
            continue
        descriptor_node = descriptors[0]
        parent = descriptor_node.getparent()
        descriptor = NumericMetricDescriptorContainer.from_node(descriptor_node, parent.get("Handle"))
        state = NumericMetricStateContainer.from_node(states[0], descriptor)
        report.check(
            descriptor_node.tag == pm.Metric
            and descriptor_node.get("{http://www.w3.org/2001/XMLSchema-instance}type", "").split(":")[-1] == "NumericMetricDescriptor"
            and descriptor.parent_handle == channel
            and parent.getparent().get("Handle") == vmd
            and parent.getparent().getparent().get("Handle") == constants.MDS_HANDLE,
            f"{handle} serializes under its declared Channel and VMD",
        )
        report.check(
            descriptor.Type.Code == type_code
            and descriptor.Type.CodingSystem == type_system
            and [text.text for text in descriptor.Type.ConceptDescription] == [label]
            and descriptor.Unit.Code == unit_code
            and descriptor.Unit.CodingSystem == constants.CODING_SYSTEM_MDC
            and [text.text for text in descriptor.Unit.ConceptDescription] == [unit_label]
            and specs[handle].unit_label == unit_label
            and descriptor.MetricCategory == pm_types.MetricCategory.MEASUREMENT
            and descriptor.Resolution == Decimal(resolution)
            and [(r.Lower, r.Upper, r.StepWidth) for r in descriptor.TechnicalRange]
            == [(Decimal(lower), Decimal(upper), Decimal(resolution))],
            f"{handle} parses its exact type, literal unit, resolution and technical range",
        )
        report.check(
            state.MetricValue is not None
            and state.MetricValue.Value == Decimal(value)
            and state.MetricValue.MetricQuality.Validity == pm_types.MeasurementValidity.VALID
            and state.ActivationState == pm_types.ComponentActivation.ON,
            f"{handle} parses its exact initial value with valid, active state",
        )


def check_preset(report: Report, path: Path) -> None:
    """Build one preset and look at what it produced."""
    print(f"\n{path.name}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        device = config.load_file(path)
    except (OSError, UnicodeError, ValueError, config.ConfigError) as exc:
        report.check(False, f"{path.name} parses", str(exc)[:70])  # noqa: FBT003
        return
    report.check(True, "parses")  # noqa: FBT003
    report.check(
        data.get("version") == config.CONFIG_VERSION,
        f"uses current profile version {config.CONFIG_VERSION}",
        str(data.get("version")),
    )
    alerts_with_implicit_signals = [
        alert.get("handle", alert.get("label", "?"))
        for alert in data.get("alerts", [])
        if not alert.get("signals")
    ]
    report.check(
        not alerts_with_implicit_signals,
        "records every alarm's version-3 signal definitions explicitly",
        str(alerts_with_implicit_signals),
    )

    report.check(
        device.device is not None and bool(device.device.friendly_name),
        "names the machine it describes, rather than inheriting the toolbox's identity",
        device.device.friendly_name if device.device else "no device block",
    )

    service = ProviderService(instance_name=f"preset-{path.stem}", device=device.device)
    service.start()
    try:
        metrics, alerts = config.apply_to(service, device)
        report.check(
            metrics == len(device.metrics) and alerts == len(device.alerts),
            "builds on a real provider",
            f"{metrics} metrics, {alerts} alarms, {len(service.list_actions())} actions",
        )

        sections = service.sections()
        declared_sections = {spec.section for spec in device.metrics}
        report.check(
            bool(declared_sections) and "" not in declared_sections and set(sections) == declared_sections,
            "builds every nonempty declared section, without flattening containment",
            ", ".join(sorted(sections)),
        )
        wrong_parents = []
        for spec in device.metrics:
            entity = service.mdib.entities.by_handle(spec.handle)
            channel = service.mdib.entities.by_handle(sections.get(spec.section, ""))
            vmd = service.mdib.entities.by_handle(channel.parent_handle) if channel is not None else None
            if (
                entity is None or channel is None or vmd is None
                or entity.parent_handle != channel.handle
                or channel.node_type != pm.ChannelDescriptor
                or vmd.node_type != pm.VmdDescriptor
                or vmd.parent_handle != constants.MDS_HANDLE
            ):
                wrong_parents.append(spec.handle)
        report.check(not wrong_parents, "every metric belongs to its declared Channel and VMD", str(wrong_parents))
        check_focused_profile(report, path, service)

        # Every descriptor has to serialise, and a mandatory field missing only shows up
        # when it does. Reading them all back is the cheap way to force that.
        unserialisable = []
        for handle in service.list_metrics():
            entity = service.mdib.entities.by_handle(handle)
            if entity is None or entity.descriptor.Type is None:
                unserialisable.append(handle)
        report.check(not unserialisable, "every metric descriptor is complete", str(unserialisable))

        # An action naming a target that does not exist would have raised in apply_to, so
        # reaching here means every one resolved. Check they point somewhere real anyway,
        # because a preset can name mds0 and a section handle alike.
        dangling = [
            spec.target_handle
            for spec in service.list_actions().values()
            if service.mdib.entities.by_handle(spec.target_handle) is None
        ]
        report.check(not dangling, "every action acts on something that exists", str(dangling))

        effects_missing = sorted(
            {
                target
                for spec in service.list_actions().values()
                for target in spec.effects
                if target not in service.list_metrics()
            },
        )
        report.check(
            not effects_missing,
            "and only changes metrics the preset defines",
            str(effects_missing),
        )

        # Waveforms and distributions have to actually produce something, or the card is
        # blank and the preset is a description of a device rather than one.
        sample_arrays = [h for h, s in service.list_metrics().items() if s.is_sample_array]
        if sample_arrays:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and not all(
                service.get_samples(h) for h in sample_arrays
            ):
                time.sleep(0.3)
            empty = [h for h in sample_arrays if not service.get_samples(h)]
            report.check(
                not empty,
                f"all {len(sample_arrays)} sample array(s) produce samples",
                str(empty),
            )

        # Limits that cannot be reached make an alarm decorative.
        unreachable = []
        for handle, alert in service.list_alerts().items():
            source = service.list_metrics().get(alert.source_handle)
            if source is None or not alert.has_limits:
                continue
            if alert.upper_limit is not None and source.maximum is not None:
                if alert.upper_limit > source.maximum:
                    unreachable.append(f"{handle} > {source.maximum}")
            if alert.lower_limit is not None and source.minimum is not None:
                if alert.lower_limit < source.minimum:
                    unreachable.append(f"{handle} < {source.minimum}")
        report.check(
            not unreachable,
            "every alarm limit sits inside its source's range",
            str(unreachable),
        )
    finally:
        service.stop()


def check_nomenclature(report: Report, paths: list[Path]) -> None:
    """Where the standard has a term the presets use it, and where it does not they say so.

    This is the check with something to say. Vitals have IEEE 11073-10101 terms; surgical
    devices largely do not, which is the gap the work on extending the 1010X nomenclature
    exists to close. A preset that quietly invented MDC codes for a microscope would hide
    exactly the thing worth seeing.
    """
    print("\nNomenclature across all presets")

    totals: dict[str, dict[str, int]] = {}
    non_numeric_mdc: list[str] = []
    for path in paths:
        device = config.load_file(path)
        counts = {"mdc": 0, "private": 0}
        for spec in device.metrics:
            codings = [spec.effective_type(), spec.effective_unit()]
            if spec.kind is MetricKind.DISTRIBUTION:
                codings.append(spec.effective_domain_unit())
            for coding in codings:
                counts[coding.system] = counts.get(coding.system, 0) + 1
                if coding.system == "mdc" and not coding.code.isdecimal():
                    non_numeric_mdc.append(f"{path.stem}:{spec.handle}:{coding.code}")
        totals[path.stem] = counts

    for name, counts in sorted(totals.items()):
        total = counts["mdc"] + counts["private"]
        share = 100 * counts["mdc"] / total if total else 0
        print(f"      {name:<22} {counts['mdc']:>3} mdc, {counts['private']:>3} private  ({share:.0f}% standard)")

    monitor = totals.get("patient-monitor", {})
    scope = totals.get("surgical-microscope", {})
    if monitor and scope:
        monitor_share = monitor["mdc"] / max(1, monitor["mdc"] + monitor["private"])
        scope_share = scope["mdc"] / max(1, scope["mdc"] + scope["private"])
        report.check(
            monitor_share > scope_share,
            "the monitor leans on the standard more than the microscope does",
            f"{monitor_share:.0%} vs {scope_share:.0%}",
        )

    systems = {system for counts in totals.values() for system in counts}
    report.check(
        systems <= set(CODING_SYSTEMS),
        "no preset invents a coding system",
        str(sorted(systems)),
    )
    report.check(
        not non_numeric_mdc,
        "every MDC coding uses a decimal context-free code",
        str(non_numeric_mdc),
    )


def check_version_filter(report: Report) -> None:
    """Preset discovery omits files with versions below the supported range."""
    with tempfile.TemporaryDirectory(prefix="sdctoolbox-presets-") as directory:
        folder = Path(directory)
        for name, version in (("supported", config.LEGACY_CONFIG_VERSION), ("zero", 0), ("negative", -1)):
            (folder / f"{name}.json").write_text(
                json.dumps({"version": version, "name": name.title()}),
                encoding="utf-8",
            )
        listed = config.list_presets(folder)
    report.check(
        [preset.name for preset in listed] == ["Supported"],
        "preset discovery skips unsupported low config versions",
        str([preset.name for preset in listed]),
    )


def main() -> int:
    basic_logging_setup(level=logging.ERROR)
    report = Report()

    print("=" * 74)
    print("Shipped presets")
    print("=" * 74)

    paths = sorted((ROOT / "presets").glob("*.json"))
    actual_files = {path.name for path in paths}
    report.check(
        constants.SHIPPED_PRESET_FILES == EXPECTED_PRESET_FILES,
        "the manifest includes seven device profiles and two focused examples",
    )
    report.check(
        actual_files == EXPECTED_PRESET_FILES,
        "the exact canonical preset inventory is shipped",
        f"missing {sorted(EXPECTED_PRESET_FILES - actual_files)}, "
        f"extra {sorted(actual_files - EXPECTED_PRESET_FILES)}",
    )

    for name in sorted(EXPECTED_PRESET_FILES):
        check_preset(report, ROOT / "presets" / name)

    check_nomenclature(report, paths)

    print("\nThe menu builds from these")
    listed = config.list_presets()
    report.check(
        {preset.path.name for preset in listed} == constants.SHIPPED_PRESET_FILES,
        "exactly the shipped files are offered, so none is silently unreadable",
        str(sorted(preset.path.name for preset in listed)),
    )
    for preset in listed:
        report.check(bool(preset.description), f"{preset.name} says what it is", preset.description[:44])
    check_version_filter(report)

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
