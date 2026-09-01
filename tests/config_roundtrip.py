"""Round-trip and validation tests for config files.

Nothing here touches the network beyond starting a provider on loopback, so it is quick
compared with the other suites.

Exit code 0 means all checks passed.

Usage:  .venv/Scripts/python.exe tests/config_roundtrip.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sdc11073.loghelper import basic_logging_setup  # noqa: E402

from sdctoolbox import config  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    AlertKind,
    AlertManifestation,
    AlertPriority,
    AlertSignalSpec,
    AlertSpec,
    Coding,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    PatientMeasurement,
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


def snapshot(service: ProviderService) -> dict:
    """Everything about a device that a config file is supposed to preserve."""
    return {
        "metrics": {
            handle: (
                spec.label,
                spec.kind.value,
                spec.unit_label,
                spec.allowed_values,
                spec.resolution,
                spec.minimum,
                spec.maximum,
                spec.controllable,
                service.get_value(handle),
                spec.sample_period,
                spec.shape,
                spec.domain_unit_label,
                spec.domain_minimum,
                spec.domain_maximum,
            )
            for handle, spec in service.list_metrics().items()
        },
        "alerts": {
            handle: (
                spec.label,
                spec.source_handle,
                spec.kind.value,
                spec.priority.value,
                spec.lower_limit,
                spec.upper_limit,
                tuple((signal.manifestation.value, signal.latching) for signal in spec.signals),
            )
            for handle, spec in service.list_alerts().items()
        },
        "location": service.get_location(),
        "patient": service.get_patient(),
    }


def build_reference(service: ProviderService) -> None:
    """A device using every feature the config format has to carry."""
    service.add_metric(
        MetricSpec(
            label="Zoom level",
            kind=MetricKind.NUMBER,
            unit_label="steps",
            resolution=Decimal("1"),
            minimum=Decimal("1"),
            maximum=Decimal("100"),
            controllable=True,
            initial_value=Decimal("7"),
        ),
    )
    service.add_metric(
        MetricSpec(
            label="Mode",
            kind=MetricKind.CHOICE,
            allowed_values=("IDLE", "RUN", "PAUSE"),
            controllable=True,
            initial_value="RUN",
        ),
    )
    service.add_metric(MetricSpec(label="Patient note", kind=MetricKind.TEXT, controllable=False))
    service.add_metric(
        MetricSpec(
            label="Pleth",
            kind=MetricKind.WAVEFORM,
            unit_label="%",
            minimum=Decimal("0"),
            maximum=Decimal("100"),
            sample_period=Decimal("0.05"),
            shape=WaveformShape.SQUARE,
        ),
    )
    service.add_metric(
        MetricSpec(
            label="Spectrum",
            kind=MetricKind.DISTRIBUTION,
            unit_label="dB",
            domain_unit_label="Hz",
            domain_minimum=Decimal("0"),
            domain_maximum=Decimal("500"),
        ),
    )
    service.add_alert(
        AlertSpec(
            label="Zoom high",
            source_handle="m.zoom_level",
            kind=AlertKind.PHYSIOLOGICAL,
            priority=AlertPriority.HIGH,
            upper_limit=Decimal("90"),
            signals=(
                AlertSignalSpec(AlertManifestation.VIS, latching=True),
                AlertSignalSpec(AlertManifestation.TAN),
            ),
        ),
    )
    service.add_alert(AlertSpec(label="Service due", source_handle="m.zoom_level"))
    service.set_location(LocationInfo(facility="HOSP", point_of_care="OR1", bed="A"))
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
            race=Coding(code="demo-race", system="urn:example:race", label="Demo race"),
        ),
    )


def check_preflight_preserves_device(report: Report, service: ProviderService) -> None:
    """Reference failures must be reported before replace=True removes live descriptors."""
    service.add_metric(MetricSpec(label="Existing", kind=MetricKind.NUMBER, initial_value=Decimal("7")))
    before = set(service.list_metrics())
    invalid = config.parse(
        {
            "metrics": [{"label": "New", "kind": "number"}],
            "actions": [{"label": "Broken", "target": "missing", "effects": {"m.new": "1"}}],
        },
    )
    try:
        config.apply_to(service, invalid)
    except config.ConfigError as exc:
        report.check("target" in str(exc), "an invalid action target is rejected before import", str(exc))
    else:
        report.check(False, "an invalid action target is rejected before import", "it was accepted")  # noqa: FBT003
    report.check(set(service.list_metrics()) == before, "a rejected import leaves existing metrics intact")

    missing_effect = config.parse(
        {
            "metrics": [{"label": "New", "kind": "number"}],
            "actions": [{"label": "Broken", "target": "mds0", "effects": {"m.missing": "1"}}],
        },
    )
    try:
        config.apply_to(service, missing_effect)
    except config.ConfigError as exc:
        report.check("effects" in str(exc), "a missing effect metric is rejected before import", str(exc))
    else:
        report.check(False, "a missing effect metric is rejected before import", "it was accepted")  # noqa: FBT003
    report.check(set(service.list_metrics()) == before, "a rejected effect import leaves existing metrics intact")
    empty_location = config.parse({"contexts": {"location": {}}})
    try:
        config.apply_to(service, empty_location)
    except config.ConfigError as exc:
        report.check("location" in str(exc), "an empty location is rejected before import", str(exc))
    else:
        report.check(False, "an empty location is rejected before import", "it was accepted")  # noqa: FBT003
    report.check(set(service.list_metrics()) == before, "a rejected location import leaves existing metrics intact")
    service.remove_metric("m.existing")


BAD_FILES = [
    ('{"metrics": [{"label": "x"}]}', "a metric with no kind"),
    ('{"metrics": [{"label": "x", "kind": "nope"}]}', "an unknown kind"),
    ('{"metrics": [{"kind": "number"}]}', "a metric with no label"),
    ('{"metrics": [{"label": "x", "kind": "number", "minimum": "abc"}]}', "a non-numeric limit"),
    (
        '{"metrics": [{"label": "x", "kind": "number", "minimum": "10", "maximum": "1"}]}',
        "a minimum above its maximum",
    ),
    (
        '{"metrics": [{"label": "x", "kind": "distribution", "domain_minimum": "0", "domain_maximum": "0"}]}',
        "a zero-width distribution domain",
    ),
    ('{"metrics": [{"label": "x", "kind": "choice"}]}', "a choice with no values"),
    ('{"alerts": [{"label": "a", "watches": "m.nothing"}]}', "an alarm watching nothing"),
    ('{"alerts": [{"label": "a"}]}', "an alarm with no source"),
    (
        '{"metrics": [{"handle": "m.x", "label": "x", "kind": "number"}], "alerts": [{"label": "a", "watches": "m.x", "signals": []}]}',
        "an alarm with no signals",
    ),
    (
        '{"metrics": [{"handle": "m.x", "label": "x", "kind": "number"}], "alerts": [{"label": "a", "watches": "m.x", "signals": [{"manifestation": "Vis", "latching": "false"}]}]}',
        "a non-boolean signal latching value",
    ),
    (
        '{"contexts": {"patient": {"height": {"value": "170"}}}}',
        "a patient height without a unit",
    ),
    (
        '{"contexts": {"patient": {"weight": {"unit": {"code": "kg", "system": "private"}}}}}',
        "a patient weight without a value",
    ),
    (
        '{"contexts": {"patient": {"race": {"code": "demo-race"}}}}',
        "a patient race without a coding system",
    ),
    (
        '{"contexts": {"patient": {"height": {"value": "1e999999", "unit": {"code": "m", "system": "private"}}}}}',
        "an impractical patient measurement exponent",
    ),
    (
        '{"contexts": {"patient": {"height": {"value": 1e-1000, "unit": {"code": "m", "system": "private"}}}}}',
        "an underflowing patient measurement literal",
    ),
    (
        '{"contexts": {"patient": {"race": {"code": "bad\\u0001", "system": "private"}}}}',
        "an XML-invalid patient race code",
    ),
    (
        '{"contexts": {"patient": {"race": {"code": "demo-race", "system": "urn:%"}}}}',
        "a malformed patient race coding-system URI",
    ),
    (
        '{"contexts": {"patient": {"given_name": "bad\\u0001"}}}',
        "an XML-invalid patient name",
    ),
    ("not json at all", "a file that is not JSON"),
    ('{"version": "2"}', "a non-integer version"),
    ('{"version": 99}', "a file from a newer build"),
    ("[]", "a top level list"),
]


def main() -> int:
    basic_logging_setup(level=logging.WARNING)
    report = Report()
    workdir = Path(tempfile.mkdtemp(prefix="sdctoolbox-config-"))
    path = workdir / f"reference{config.FILE_SUFFIX}"

    print("=" * 74)
    print("Config round trip")
    print("=" * 74)

    print("\n1. Export")
    source = ProviderService(instance_name="config-source")
    source.start()
    try:
        build_reference(source)
        before = snapshot(source)
        config.save(source, path)
        report.check(path.exists(), "the file is written", f"{path.stat().st_size} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
        report.check(data.get("version") == 3, "new profiles use version 3")  # noqa: PLR2004
        report.check(len(data.get("metrics", [])) == 5, "all data sources are in it")  # noqa: PLR2004
        report.check(len(data.get("alerts", [])) == 2, "and both alarms")  # noqa: PLR2004
        report.check(
            all("handle" in entry for entry in data["metrics"]),
            "handles are recorded for stable metric references",
        )
        zoom_alert = next(alert for alert in data["alerts"] if alert["handle"] == "al.zoom_high")
        report.check(
            zoom_alert["signals"] == [
                {"manifestation": "Vis", "latching": True},
                {"manifestation": "Tan", "latching": False},
            ],
            "signal manifestation and latching settings are exported",
            str(zoom_alert["signals"]),
        )
        patient_data = data.get("contexts", {}).get("patient", {})
        report.check(
            patient_data.get("height", {}).get("value") == "170.5"
            and patient_data.get("height", {}).get("unit", {}).get("code") == "demo-cm"
            and patient_data.get("weight", {}).get("value") == "72.4"
            and patient_data.get("race", {}).get("code") == "demo-race"
            and patient_data.get("race", {}).get("system") == "urn:example:race",
            "patient measurements and race are exported as coded structures",
            str(patient_data),
        )
        precise = workdir / "precise-patient.json"
        precise.write_text(
            '{"contexts": {"patient": {"height": {"value": 0.12345678901234567, '
            '"unit": {"code": "demo-unit", "system": "private"}}}}}',
            encoding="utf-8",
        )
        parsed_patient = config.load_file(precise).patient
        report.check(
            parsed_patient is not None
            and parsed_patient.height is not None
            and parsed_patient.height.value == Decimal("0.12345678901234567"),
            "JSON measurement literals retain Decimal precision",
            str(parsed_patient.height.value) if parsed_patient and parsed_patient.height else "none",
        )
        try:
            config.parse(
                {
                    "contexts": {
                        "patient": {
                            "height": {
                                "value": 0.12345678901234567,
                                "unit": {"code": "demo-unit", "system": "private"},
                            },
                        },
                    },
                },
            )
        except config.ConfigError as exc:
            report.check("never float" in str(exc), "programmatic float measurements are refused", str(exc))
        else:
            report.check(False, "programmatic float measurements are refused", "it was accepted")  # noqa: FBT003
        source.set_value("m.zoom_level", Decimal("42"))
        config.save(source, workdir / "with-values.json")
        with_values = json.loads((workdir / "with-values.json").read_text(encoding="utf-8"))
        zoom = next(m for m in with_values["metrics"] if m["handle"] == "m.zoom_level")
        report.check(
            zoom.get("initial_value") == "42",
            "the current value is captured as the initial value",
            str(zoom.get("initial_value")),
        )
    finally:
        source.stop()

    print("\n2. Import into a fresh device")
    target = ProviderService(instance_name="config-target")
    target.start()
    try:
        metrics, alerts = config.load_into(target, path)
        report.check(metrics == 5, "five data sources created", str(metrics))  # noqa: PLR2004
        report.check(alerts == 2, "two alarms created", str(alerts))  # noqa: PLR2004
        after = snapshot(target)
        report.check(after == before, "everything matches the original")
        if after != before:
            for key in ("metrics", "alerts"):
                for handle in sorted(set(before[key]) | set(after[key])):
                    if before[key].get(handle) != after[key].get(handle):
                        print(f"      {handle}\n        before {before[key].get(handle)}")
                        print(f"        after  {after[key].get(handle)}")

        report.check(
            len(target.signal_handles_for("al.zoom_high")) == 2,  # noqa: PLR2004
            "the alarm's signals are rebuilt too",
            str(target.signal_handles_for("al.zoom_high")),
        )
        report.check(
            target.mdib.entities.by_handle("al.zoom_high").descriptor.NODETYPE.localname
            == "LimitAlertConditionDescriptor",
            "an alarm with limits comes back as a LimitAlertCondition",
        )

        legacy = config.parse(
            {
                "version": 1,
                "contexts": {"patient": {"given_name": "Legacy"}},
                "metrics": [{"label": "Legacy metric", "kind": "number"}],
            },
        )
        report.check(
            legacy.patient is not None
            and legacy.patient.given_name == "Legacy"
            and len(legacy.metrics) == 1,
            "version 1 profiles remain readable",
        )

        print("\n3. Import replaces rather than appends")
        metrics, _ = config.load_into(target, path)
        report.check(
            len(target.list_metrics()) == 5,  # noqa: PLR2004
            "loading twice does not duplicate anything",
            str(sorted(target.list_metrics())),
        )
        report.check(
            sorted(target.list_metrics()) == sorted(before["metrics"]),
            "and the handles are unchanged",
        )
    finally:
        target.stop()

    print("\n4. Invalid imports do not mutate the device")
    guarded = ProviderService(instance_name="config-preflight")
    guarded.start()
    try:
        check_preflight_preserves_device(report, guarded)
    finally:
        guarded.stop()

    print("\n5. Bad files are refused with a usable message")
    for text, description in BAD_FILES:
        bad = workdir / "bad.json"
        bad.write_text(text, encoding="utf-8")
        try:
            config.load_file(bad)
        except config.ConfigError as exc:
            report.check(bool(str(exc)), f"refuses {description}", str(exc)[:70])
        else:
            report.check(False, f"refuses {description}", "it was accepted")  # noqa: FBT003

    print("\n6. A missing file says so")
    try:
        config.load_file(workdir / "does-not-exist.json")
    except config.ConfigError as exc:
        report.check("cannot read" in str(exc), "missing file reported clearly", str(exc)[:60])
    else:
        report.check(False, "missing file reported clearly", "it was accepted")  # noqa: FBT003

    print()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
