"""Network acceptance for passive discovery: Hello and Bye update the consumer without a scan."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from script_support import Report, wait_until  # noqa: E402

from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402


def main() -> int:
    report = Report()
    print("Passive discovery")
    changes = threading.Event()
    consumer = ConsumerService()
    consumer.set_discovery_listener(changes.set)
    consumer.start()
    provider = ProviderService(instance_name="passive-discovery-provider")
    try:
        # Started after the consumer, so its Hello is the only way the consumer learns of it.
        started = time.monotonic()
        provider.start()
        epr = provider.epr.urn

        def listed() -> bool:
            return any(device.epr == epr for device in consumer.known_devices())

        report.check(wait_until(listed, timeout=10.0), "a provider starting later is known without a probe")
        report.check(changes.is_set(), "its Hello notifies the discovery listener")
        print(f"    (Hello seen after {time.monotonic() - started:.2f}s)")

        # Hello is multicast a few times; let those repeats settle before judging age.
        time.sleep(2.0)
        report.check(
            not any(device.epr == epr for device in consumer.known_devices(max_age=1.0))
            and listed(),
            "entries older than max_age are left out",
        )

        changes.clear()
        provider.stop()
        report.check(wait_until(lambda: not listed(), timeout=10.0), "a clean provider stop is dropped via Bye")
        report.check(changes.is_set(), "its Bye notifies the discovery listener")

        changes.clear()
        consumer.probe()
        time.sleep(1.0)
        report.check(not listed(), "a probe does not resurrect a stopped provider")
    finally:
        provider.stop()
        consumer.stop()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
