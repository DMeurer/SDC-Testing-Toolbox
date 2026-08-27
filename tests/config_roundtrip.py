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
    AlertPriority,
    AlertSpec,
    MetricKind,
    MetricSpec,
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
            )
            for handle, spec in service.list_alerts().items()
        },
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
        ),
    )
    service.add_alert(AlertSpec(label="Service due", source_handle="m.zoom_level"))


BAD_FILES = [
    ('{"metrics": [{"label": "x"}]}', "a metric with no kind"),
    ('{"metrics": [{"label": "x", "kind": "nope"}]}', "an unknown kind"),
    ('{"metrics": [{"kind": "number"}]}', "a metric with no label"),
    ('{"metrics": [{"label": "x", "kind": "number", "minimum": "abc"}]}', "a non-numeric limit"),
    (
        '{"metrics": [{"label": "x", "kind": "number", "minimum": "10", "maximum": "1"}]}',
        "a minimum above its maximum",
    ),
    ('{"metrics": [{"label": "x", "kind": "choice"}]}', "a choice with no values"),
    ('{"alerts": [{"label": "a", "watches": "m.nothing"}]}', "an alarm watching nothing"),
    ('{"alerts": [{"label": "a"}]}', "an alarm with no source"),
    ("not json at all", "a file that is not JSON"),
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
        report.check(data.get("version") == config.CONFIG_VERSION, "it records a version")
        report.check(len(data.get("metrics", [])) == 5, "all data sources are in it")  # noqa: PLR2004
        report.check(len(data.get("alerts", [])) == 2, "and both alarms")  # noqa: PLR2004
        report.check(
            all("handle" in entry for entry in data["metrics"]),
            "handles are recorded, so a preset reproduces the same MDIB",
        )
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

    print("\n4. Bad files are refused with a usable message")
    for text, description in BAD_FILES:
        bad = workdir / "bad.json"
        bad.write_text(text, encoding="utf-8")
        try:
            config.load_file(bad)
        except config.ConfigError as exc:
            report.check(bool(str(exc)), f"refuses {description}", str(exc)[:70])
        else:
            report.check(False, f"refuses {description}", "it was accepted")  # noqa: FBT003

    print("\n5. A missing file says so")
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
