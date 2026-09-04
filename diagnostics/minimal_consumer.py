"""A bare SDC consumer for diagnosing network problems.

Counterpart to minimal_provider.py, using none of this project's own code. Connects to the
first provider it finds - any provider, not just ours - and prints metric updates.

Usage:
    .venv/Scripts/python.exe diagnostics/minimal_consumer.py --ip 127.0.0.1
"""

from __future__ import annotations

import argparse
import logging
import time

from sdc11073 import observableproperties
from sdc11073.consumer.consumerimpl import SdcConsumer
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.loghelper import basic_logging_setup
from sdc11073.mdib import ConsumerMdib
from sdc11073.wsdiscovery import WSDiscovery, WSDiscoverySingleAdapter
from sdc11073.xml_types.actions import periodic_actions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ip", help="IPv4 address of the interface to use")
    group.add_argument("--adapter", help="display name of the adapter to use")
    parser.add_argument("--scan-seconds", type=int, default=25, help="search duration (default 25)")
    parser.add_argument("--watch-seconds", type=int, default=15, help="observation time (default 15)")
    return parser.parse_args()


class MetricUpdateObserver:
    """Print metric reports while retaining the number of callbacks actually invoked."""

    def __init__(self) -> None:
        self.callback_count = 0

    def __call__(self, metrics_by_handle: dict) -> None:
        self.callback_count += 1
        for handle, state in metrics_by_handle.items():
            value = getattr(getattr(state, "MetricValue", None), "Value", None)
            print(f"[consumer]   UPDATE {handle} = {value}", flush=True)


def result_lines(metric_callback_count: int) -> tuple[str, str]:
    """Describe connection health and metric receipt as independent outcomes."""
    connection = "[consumer] RESULT: connection health: connection held"
    if metric_callback_count:
        receipt = (
            "[consumer] RESULT: metric update receipt: "
            f"{metric_callback_count} callback(s) received"
        )
    else:
        receipt = "[consumer] RESULT: metric update receipt: no callbacks received"
    return connection, receipt


def main() -> int:
    args = parse_args()
    basic_logging_setup(level=logging.INFO)

    discovery = WSDiscovery(args.ip) if args.ip else WSDiscoverySingleAdapter(args.adapter)
    target = args.ip or args.adapter
    print(f"[consumer] discovery on: {target}")

    with discovery:
        deadline = time.monotonic() + args.scan_seconds
        services = []
        while time.monotonic() < deadline and not services:
            print("[consumer] searching for SDC providers ...", flush=True)
            services = discovery.search_services(types=SdcV1Definitions.MedicalDeviceTypesFilter)
            if not services:
                time.sleep(2)

        if not services:
            print("[consumer] RESULT: no provider found")
            return 1

        print(f"[consumer] found {len(services)} provider(s):")
        for service in services:
            print(f"[consumer]   EPR {service.epr}")
            for addr in getattr(service, "x_addrs", []) or []:
                print(f"[consumer]     XAddr {addr}")

        service = services[0]
        print(f"[consumer] connecting to {service.epr}")
        consumer = SdcConsumer.from_wsd_service(service, ssl_context_container=None)
        consumer.start_all(not_subscribed_actions=periodic_actions)

        mdib = ConsumerMdib(consumer)
        mdib.init_mdib()
        print(f"[consumer] MDIB loaded: {len(mdib.entities)} entities")

        metric_observer = MetricUpdateObserver()
        observableproperties.bind(mdib, metrics_by_handle=metric_observer)
        print(f"[consumer] observing for {args.watch_seconds}s ...", flush=True)
        time.sleep(args.watch_seconds)

        consumer.stop_all()
        for line in result_lines(metric_observer.callback_count):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
