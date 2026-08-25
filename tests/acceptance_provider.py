"""The provider half of the core acceptance test.

Started as a subprocess by acceptance_core.py; not useful on its own, though it can be run
by hand to have a device on the network to poke at.

Creates four data sources up front, disables remote control on one of them, and after a
delay adds a fifth one - that late arrival is what proves runtime descriptor creation is
visible to an already-connected consumer.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdc11073.loghelper import basic_logging_setup  # noqa: E402

from sdctoolbox import constants  # noqa: E402
from sdctoolbox.model import MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

# Handles are pinned so the acceptance script can assert on them.
ZOOM = "m.zoom_level"
MODE = "m.mode"
NOTE = "m.patient_note"
LOCKED = "m.locked_setting"
LATE = "m.late_arrival"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", default=constants.DEFAULT_IP)
    parser.add_argument("--instance", default="alpha")
    parser.add_argument("--late-after", type=float, default=8.0, help="seconds before adding the late metric")
    parser.add_argument("--seconds", type=float, default=70.0, help="total run time")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    basic_logging_setup(level=logging.WARNING)

    service = ProviderService(ip=args.ip, instance_name=args.instance)
    service.start()
    print(f"[provider] up, EPR {service.epr.urn}", flush=True)

    service.add_metric(
        MetricSpec(
            label="Zoom level",
            kind=MetricKind.NUMBER,
            unit_label="steps",
            resolution=Decimal("1"),
            minimum=Decimal("1"),
            maximum=Decimal("100"),
            controllable=True,
            handle=ZOOM,
            initial_value=Decimal("1"),
        ),
    )
    service.add_metric(
        MetricSpec(
            label="Mode",
            kind=MetricKind.CHOICE,
            allowed_values=("IDLE", "RUN", "PAUSE"),
            controllable=True,
            handle=MODE,
            initial_value="IDLE",
        ),
    )
    service.add_metric(
        MetricSpec(
            label="Patient note",
            kind=MetricKind.TEXT,
            controllable=True,
            handle=NOTE,
            initial_value="none",
        ),
    )
    service.add_metric(
        MetricSpec(
            label="Locked setting",
            kind=MetricKind.NUMBER,
            resolution=Decimal("1"),
            controllable=True,
            handle=LOCKED,
            initial_value=Decimal("5"),
        ),
    )
    # This one stays in the MDIB but must refuse every write.
    service.disable_control(LOCKED)

    print(f"[provider] initial metrics: {sorted(service.list_metrics())}", flush=True)
    print("[provider] READY", flush=True)

    started = time.monotonic()
    late_added = False
    deadline = started + args.seconds

    while time.monotonic() < deadline:
        if not late_added and time.monotonic() - started >= args.late_after:
            service.add_metric(
                MetricSpec(
                    label="Late arrival",
                    kind=MetricKind.NUMBER,
                    unit_label="units",
                    resolution=Decimal("1"),
                    controllable=True,
                    handle=LATE,
                    initial_value=Decimal("42"),
                ),
            )
            late_added = True
            print(f"[provider] added {LATE} at runtime", flush=True)
        time.sleep(0.5)

    service.stop()
    print("[provider] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
