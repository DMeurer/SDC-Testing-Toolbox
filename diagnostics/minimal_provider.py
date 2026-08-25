"""A bare SDC provider for diagnosing network problems.

Uses the sdc11073 tutorial MDIB and none of this project's own code, so if this works but
the toolbox does not, the fault is ours; if this fails too, the fault is the environment.
Increments every numeric metric so a consumer has something to observe.

Usage:
    .venv/Scripts/python.exe diagnostics/minimal_provider.py --ip 127.0.0.1
    .venv/Scripts/python.exe diagnostics/minimal_provider.py --adapter "Software Loopback Interface 1"
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import time
import uuid
from decimal import Decimal

from sdc11073.location import SdcLocation
from sdc11073.loghelper import basic_logging_setup
from sdc11073.mdib import ProviderMdib
from sdc11073.provider import SdcProvider
from sdc11073.wsdiscovery import WSDiscovery, WSDiscoverySingleAdapter
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.dpws_types import ThisDeviceType, ThisModelType

# Fixed EPR so a consumer can look for this specific provider.
BASE_UUID = uuid.UUID("{cc013678-79f6-403c-998f-3cc0cc050230}")
PROVIDER_UUID = uuid.uuid5(BASE_UUID, "sdc-testing-toolbox-diagnostic")

MDIB_PATH = pathlib.Path(__file__).with_name("mdib_tutorial.xml")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ip", help="IPv4 address of the interface to use")
    group.add_argument("--adapter", help="display name of the adapter to use")
    parser.add_argument("--seconds", type=int, default=60, help="run time in seconds (default 60)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    basic_logging_setup(level=logging.INFO)

    if not MDIB_PATH.exists():
        print(f"ERROR: {MDIB_PATH} is missing.", file=sys.stderr)
        return 2

    discovery = WSDiscovery(args.ip) if args.ip else WSDiscoverySingleAdapter(args.adapter)
    target = args.ip or args.adapter
    print(f"[provider] discovery on: {target}")
    print(f"[provider] EPR: {PROVIDER_UUID.urn}")

    with discovery:
        mdib = ProviderMdib.from_mdib_file(str(MDIB_PATH))

        this_model = ThisModelType(
            manufacturer="SDC-testing-toolbox",
            manufacturer_url="https://example.invalid",
            model_name="DiagnosticDevice",
            model_number="0.1",
            model_url="https://example.invalid/model",
            presentation_url="https://example.invalid/presentation",
        )
        this_device = ThisDeviceType(
            friendly_name="Diagnostic Provider",
            firmware_version="0.1",
            serial_number="diag-0001",
        )

        # role_provider_components deliberately omitted. That means no SCO registry is built
        # at all (see PLANNING.md 7.1), so this device publishes values but accepts no remote
        # control. Fine here: this script only has to prove that packets flow.
        provider = SdcProvider(
            ws_discovery=discovery,
            epr=PROVIDER_UUID,
            this_model=this_model,
            this_device=this_device,
            device_mdib_container=mdib,
        )

        # start_rtsample_loop=False is mandatory as long as no waveform_provider_class is set:
        # otherwise start_all() unconditionally starts the waveform loop and raises ApiUsageError.
        provider.start_all(start_rtsample_loop=False)
        provider.set_location(SdcLocation(fac="HOSP", poc="CU1", bed="Diag"))
        print("[provider] started and visible on the network")

        numeric_handles = [
            entity.handle for entity in mdib.entities.by_node_type(pm.NumericMetricDescriptor)
        ]
        print(f"[provider] numeric metrics: {numeric_handles}")

        # Set initial values.
        with mdib.metric_state_transaction() as mgr:
            for handle in numeric_handles:
                state = mgr.get_state(handle)
                state.mk_metric_value()
                state.MetricValue.Value = Decimal("0")
                state.MetricValue.Validity = pm_types.MeasurementValidity.VALID
                state.ActivationState = pm_types.ComponentActivation.ON

        deadline = time.monotonic() + args.seconds
        value = 0
        while time.monotonic() < deadline:
            value += 1
            with mdib.metric_state_transaction() as mgr:
                for handle in numeric_handles:
                    mgr.get_state(handle).MetricValue.Value = Decimal(value)
            print(f"[provider] value = {value}", flush=True)
            time.sleep(2)

        print("[provider] run time elapsed, stopping")
        provider.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
