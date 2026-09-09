"""End-to-end mutual-TLS acceptance for the toolbox's provider and consumer services."""

from __future__ import annotations

import sys
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from script_support import Report  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService, DiscoveredDevice  # noqa: E402
from sdctoolbox.model import MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402
from security import make_tls_configurations  # noqa: E402


def check_tls_connection(report: Report) -> None:
    with TemporaryDirectory(prefix="sdc-tls-acceptance-") as raw_temporary:
        provider_tls, consumer_tls = make_tls_configurations(Path(raw_temporary))
        provider = ProviderService(instance_name="tls-provider", tls_config=provider_tls)
        consumer = ConsumerService(tls_config=consumer_tls)
        try:
            provider.start()
            handle = provider.add_metric(
                MetricSpec(
                    label="Secure setting",
                    kind=MetricKind.NUMBER,
                    controllable=True,
                    initial_value=Decimal("1"),
                ),
            )
            consumer.start()
            deadline = time.monotonic() + 10.0
            devices = []
            while time.monotonic() < deadline:
                devices = consumer.scan(timeout=1.0, expected=1)
                if devices:
                    break
            report.check(bool(devices), "TLS provider is discoverable")
            if not devices:
                return
            device = next((candidate for candidate in devices if candidate.epr == provider.epr.urn), None)
            report.check(device is not None and device.x_addrs[0].startswith("https://"), "TLS provider advertises HTTPS")
            if device is None:
                return
            remote = consumer.connect(device)
            report.check(
                remote.peer_certificate is not None and remote.peer_certificate.common_name == "provider",
                "consumer receives and verifies the provider certificate",
            )
            report.check(handle in remote.metrics(), "mutually authenticated consumer retrieves the MDIB")
            result = remote.set_value(handle, Decimal("2"))
            report.check(str(result).endswith("Fin"), "mutually authenticated consumer can invoke a control operation")
            remote.close()
        finally:
            consumer.stop()
            provider.stop()


def check_https_requirement(report: Report) -> None:
    with TemporaryDirectory(prefix="sdc-tls-http-") as raw_temporary:
        _, consumer_tls = make_tls_configurations(Path(raw_temporary))
        consumer = ConsumerService(tls_config=consumer_tls)
        insecure = DiscoveredDevice("insecure", ("http://127.0.0.1:1/device",), (), service=object())
        try:
            consumer.connect(insecure)
        except RuntimeError as exc:
            error = str(exc)
        else:
            error = ""
        report.check("TLS is required" in error, "TLS mode rejects HTTP endpoints before connection")


def main() -> int:
    report = Report()
    print("TLS acceptance")
    check_tls_connection(report)
    check_https_requirement(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
