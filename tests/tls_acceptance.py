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

from script_support import CallbackRecorder, Report  # noqa: E402
from sdc11073 import observableproperties  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService, DiscoveredDevice  # noqa: E402
from sdctoolbox.model import MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402
from security import issue_certificate, make_tls_configurations, write_certificate, write_key  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402


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
            metric_reports = CallbackRecorder(
                lambda values: {
                    metric_handle: getattr(getattr(state, "MetricValue", None), "Value", None)
                    for metric_handle, state in values.items()
                },
            )
            observableproperties.bind(remote.mdib, metrics_by_handle=metric_reports)
            cursor = metric_reports.cursor()
            result = remote.set_value(handle, Decimal("2"))
            report.check(str(result).endswith("Fin"), "mutually authenticated consumer can invoke a control operation")
            provider.set_value(handle, Decimal("3"))
            metric_event = metric_reports.wait_for(
                lambda values: values.get(handle) == Decimal("3"),
                after=cursor,
                timeout=5.0,
            )
            report.check(
                metric_event is not None,
                "provider event callback reaches the mutually authenticated consumer",
            )
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


def check_untrusted_client_rejected(report: Report) -> None:
    with TemporaryDirectory(prefix="sdc-tls-untrusted-") as raw_temporary:
        temporary = Path(raw_temporary)
        provider_tls, _ = make_tls_configurations(temporary)
        other_ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Other Test CA")])
        other_ca = issue_certificate("Other Test CA", other_ca_key, other_ca_name, other_ca_key, ca=True)
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_cert = issue_certificate("untrusted", other_key, other_ca_name, other_ca_key, ip_name="127.0.0.1")
        other_ca_path = temporary / "other-ca.pem"
        other_cert_path = temporary / "untrusted.pem"
        other_key_path = temporary / "untrusted-key.pem"
        write_certificate(other_ca_path, other_ca)
        write_certificate(other_cert_path, other_cert)
        write_key(other_key_path, other_key)
        from sdctoolbox.security import TlsConfig  # noqa: PLC0415

        untrusted_tls = TlsConfig.from_paths(other_cert_path, other_key_path, other_ca_path)
        provider = ProviderService(instance_name="tls-rejection-provider", tls_config=provider_tls)
        consumer = ConsumerService(tls_config=untrusted_tls)
        try:
            provider.start()
            device = DiscoveredDevice(
                provider.epr.urn,
                (provider._provider.get_xaddrs()[0],),  # noqa: SLF001 - obtain a direct local test endpoint
                (),
                service=object(),
            )
            try:
                consumer.connect(device)
            except Exception:  # noqa: BLE001 - OpenSSL error wording is platform-specific
                rejected = True
            else:
                rejected = False
            report.check(rejected, "provider rejects a client certificate from an untrusted CA")
        finally:
            consumer.stop()
            provider.stop()


def main() -> int:
    report = Report()
    print("TLS acceptance")
    check_tls_connection(report)
    check_https_requirement(report)
    check_untrusted_client_rejected(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
