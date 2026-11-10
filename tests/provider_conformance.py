"""Provider conformance: what the provider's MDIB says must match what the provider does.

Each check reads the provider's own MDIB and compares it with the BICEPS meaning of an
attribute, rather than with whatever the toolbox happens to write.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from script_support import Report  # noqa: E402
from sdc11073.xml_types import pm_types  # noqa: E402

from sdctoolbox.model import AlertSpec, MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

MONITORED = pm_types.AlertConditionMonitoredLimits


def check_limit_alarms_declare_the_limits_they_monitor(report: Report) -> None:
    """``None`` means that no limit is monitored, which is the opposite of what a limit alarm does."""
    cases = (
        ("two-sided", Decimal(10), Decimal(90), MONITORED.ALL_ON),
        ("upper-only", None, Decimal(90), MONITORED.LOW_OFF),
        ("lower-only", Decimal(10), None, MONITORED.HIGH_OFF),
    )
    service = ProviderService(instance_name="limit-alarm-conformance")
    try:
        service.start()
        metric = service.add_metric(MetricSpec(label="Pressure", kind=MetricKind.NUMBER, initial_value=Decimal(50)))
        for name, lower, upper, expected in cases:
            handle = service.add_alert(
                AlertSpec(label=f"{name} limit", source_handle=metric, lower_limit=lower, upper_limit=upper),
            )
            state = service.mdib.entities.by_handle(handle).state
            report.check(
                state.MonitoredAlertLimits == expected,
                f"a {name} limit alarm declares MonitoredAlertLimits {expected.value}",
                str(state.MonitoredAlertLimits),
            )
    finally:
        service.stop()


def main() -> int:
    report = Report()
    check_limit_alarms_declare_the_limits_they_monitor(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
