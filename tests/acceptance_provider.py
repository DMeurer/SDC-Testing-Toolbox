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
from sdctoolbox.model import (  # noqa: E402
    ActionSpec,
    AlertKind,
    AlertPriority,
    AlertSpec,
    Coding,
    MetricKind,
    MetricSpec,
    PatientInfo,
    PatientMeasurement,
    WaveformShape,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402

# Handles are pinned so the acceptance script can assert on them.
ZOOM = "m.zoom_level"
MODE = "m.mode"
NOTE = "m.patient_note"
LOCKED = "m.locked_setting"
LATE = "m.late_arrival"
WAVE = "m.pleth"
DIST = "m.spectrum"
SAW = "m.saw"
# The generator advances a fixed fraction of a cycle per sample, so a sawtooth from 0 to
# 100 rises by exactly 100/40 each time. acceptance_core relies on that being exact.
SAW_CYCLE = 40
HOME_ACTION = "act.home_axes"
INVALID_CHOICE_ACTION = "act.invalid_choice"
INVALID_EFFECT_ACTION = "act.invalid_effect"
MISSING_EFFECT_ACTION = "act.missing_effect"
LIMIT_ALARM = "al.zoom_out_of_range"
MANUAL_ALARM = "al.service_due"

# Deliberately different from DEFAULT_INSTANCE_NAME: EPRs are derived from the name, so an
# application left open with defaults must not publish the same EPR as this test peer.
PEER_INSTANCE = "acceptance-peer"
UPDATED_PATIENT = "Grace Hopper"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ip", default=constants.DEFAULT_IP)
    parser.add_argument("--instance", default=PEER_INSTANCE)
    parser.add_argument("--late-after", type=float, default=8.0, help="seconds before adding the late metric")
    parser.add_argument(
        "--context-update-after",
        type=float,
        default=20.0,
        help="seconds before replacing the patient context",
    )
    parser.add_argument("--seconds", type=float, default=70.0, help="total run time")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    basic_logging_setup(level=logging.WARNING)

    service = ProviderService(ip=args.ip, instance_name=args.instance)
    service.start()
    print(f"[provider] up, EPR {service.epr.urn}", flush=True)

    # A complete BICEPS PatientDemographicsCoreData fixture for consumer acceptance checks.
    # These are explicitly private test codes, not a claim about real nomenclature values.
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

    # A waveform starts its generator by itself, so the consumer should see blocks of
    # samples arriving without anybody asking for them.
    service.add_metric(
        MetricSpec(
            label="Pleth",
            kind=MetricKind.WAVEFORM,
            unit_label="%",
            minimum=Decimal("0"),
            maximum=Decimal("100"),
            sample_period=Decimal("0.1"),
            shape=WaveformShape.SINE,
            handle=WAVE,
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
            handle=DIST,
        ),
    )
    # A second waveform, and the one the stream-continuity check watches. Two of them is
    # the case that used to go wrong: each report made the consumer re-push the other's
    # latest block into its trace.
    service.add_metric(
        MetricSpec(
            label="Saw",
            kind=MetricKind.WAVEFORM,
            unit_label="mmHg",
            minimum=Decimal("0"),
            maximum=Decimal("100"),
            sample_period=Decimal("0.1"),
            shape=WaveformShape.SAWTOOTH,
            handle=SAW,
        ),
    )
    # An ActivateOperation: something the device does, rather than a value it holds.
    service.add_action(
        ActionSpec(
            label="Home axes",
            target_handle=constants.MDS_HANDLE,
            effects={ZOOM: "1", MODE: "IDLE", NOTE: "001"},
            handle=HOME_ACTION,
            note="Return the device to its reference state",
        ),
    )
    service.add_action(
        ActionSpec(
            label="Invalid effect",
            target_handle=constants.MDS_HANDLE,
            effects={MODE: "PAUSE", ZOOM: "101"},
            handle=INVALID_EFFECT_ACTION,
        ),
    )
    service.add_action(
        ActionSpec(
            label="Invalid choice",
            target_handle=constants.MDS_HANDLE,
            effects={ZOOM: "5", MODE: "INVALID"},
            handle=INVALID_CHOICE_ACTION,
        ),
    )
    service.add_action(
        ActionSpec(
            label="Missing effect",
            target_handle=constants.MDS_HANDLE,
            effects={MODE: "PAUSE", "m.missing_effect": "1"},
            handle=MISSING_EFFECT_ACTION,
        ),
    )
    # A distribution has nothing driving it, so it gets one block and keeps it.
    service.set_samples(DIST, [Decimal(index) for index in range(32)])

    # A limit alarm that follows the zoom metric, plus one raised only by hand.
    service.add_alert(
        AlertSpec(
            label="Zoom out of range",
            source_handle=ZOOM,
            kind=AlertKind.TECHNICAL,
            priority=AlertPriority.HIGH,
            upper_limit=Decimal("90"),
            handle=LIMIT_ALARM,
        ),
    )
    service.add_alert(
        AlertSpec(
            label="Service due",
            source_handle=ZOOM,
            kind=AlertKind.OTHER,
            priority=AlertPriority.LOW,
            handle=MANUAL_ALARM,
        ),
    )

    print(f"[provider] initial metrics: {sorted(service.list_metrics())}", flush=True)
    print(f"[provider] alarms: {sorted(service.list_alerts())}", flush=True)
    print("[provider] READY", flush=True)

    started = time.monotonic()
    late_added = False
    context_updated = False
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
        if not context_updated and time.monotonic() - started >= args.context_update_after:
            service.set_patient(
                PatientInfo(
                    given_name="Grace",
                    family_name="Hopper",
                    height=PatientMeasurement(
                        value=Decimal("1E-7"),
                        unit=Coding(code="demo-m", system="private", label="m"),
                    ),
                    race=Coding(
                        code="updated-race",
                        system="urn:example:race",
                        label="Updated race",
                    ),
                ),
            )
            context_updated = True
            print(f"[provider] patient changed to {UPDATED_PATIENT}", flush=True)
        time.sleep(0.5)

    service.stop()
    print("[provider] stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
