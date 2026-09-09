"""Deterministic TLS configuration and certificate-inspection checks."""

from __future__ import annotations

import ipaddress
import ssl
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

from script_support import Report  # noqa: E402
from sdctoolbox.security import (  # noqa: E402
    CertificateInfo,
    TlsConfig,
    TlsConfigError,
    certificate_matches_endpoint,
    normalize_fingerprint,
)


def write_certificate(path: Path, certificate: x509.Certificate) -> None:
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def write_key(path: Path, key, password: bytes | None = None) -> None:  # noqa: ANN001
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            encryption,
        ),
    )


def issue_certificate(
    subject: str,
    subject_key,
    issuer: x509.Name,
    issuer_key,
    *,
    ca: bool = False,
    ip_name: str | None = None,
    expired: bool = False,
) -> x509.Certificate:  # noqa: ANN001
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(issuer)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if ip_name is not None:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip_name))]),
            critical=False,
        )
    if not ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


def make_tls_configurations(directory: Path) -> tuple[TlsConfig, TlsConfig]:
    """Create two mutually trusted participant identities under one self-signed test CA."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Toolbox Test CA")])
    ca_certificate = issue_certificate("Toolbox Test CA", ca_key, ca_name, ca_key, ca=True)
    ca_path = directory / "ca.pem"
    write_certificate(ca_path, ca_certificate)
    configurations = []
    for role in ("provider", "consumer"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = issue_certificate(role, key, ca_name, ca_key, ip_name="127.0.0.1")
        certificate_path = directory / f"{role}.pem"
        key_path = directory / f"{role}-key.pem"
        write_certificate(certificate_path, certificate)
        write_key(key_path, key)
        configurations.append(TlsConfig.from_paths(certificate_path, key_path, ca_path))
    return tuple(configurations)  # type: ignore[return-value]


def main() -> int:
    report = Report()
    print("TLS configuration")
    with TemporaryDirectory(prefix="sdc-tls-") as raw_temporary:
        temporary = Path(raw_temporary)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Toolbox Test CA")])
        ca_certificate = issue_certificate("Toolbox Test CA", ca_key, ca_name, ca_key, ca=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = issue_certificate("provider", key, ca_name, ca_key, ip_name="127.0.0.1")
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert_path = temporary / "participant.pem"
        key_path = temporary / "participant-key.pem"
        ca_path = temporary / "ca.pem"
        other_key_path = temporary / "other-key.pem"
        write_certificate(cert_path, certificate)
        write_key(key_path, key)
        write_key(other_key_path, other_key)
        write_certificate(ca_path, ca_certificate)

        config = TlsConfig.from_paths(cert_path, key_path, ca_path)
        contexts = config.create_contexts()
        report.check(
            contexts.client_context.minimum_version == ssl.TLSVersion.TLSv1_2
            and contexts.client_context.check_hostname
            and contexts.client_context.verify_mode == ssl.CERT_REQUIRED,
            "client context requires trusted TLS 1.2+ peers and hostname checking",
        )
        report.check(
            contexts.server_context.minimum_version == ssl.TLSVersion.TLSv1_2
            and contexts.server_context.verify_mode == ssl.CERT_REQUIRED,
            "server context requires a trusted client certificate",
        )

        info = config.local_certificate()
        report.check(
            info.common_name == "provider"
            and info.issuer == "CN=Toolbox Test CA"
            and certificate_matches_endpoint(info, "127.0.0.1")
            and not certificate_matches_endpoint(info, "127.0.0.2"),
            "certificate metadata exposes issuer and SAN endpoint identity",
        )
        report.check(
            config.verify_peer_certificate(certificate.public_bytes(serialization.Encoding.DER)) == info,
            "peer DER certificate can be inspected after the TLS handshake",
        )
        report.check(
            normalize_fingerprint(":".join(info.sha256_fingerprint[index : index + 2] for index in range(0, 64, 2)))
            == info.sha256_fingerprint,
            "fingerprints accept colon-separated SHA-256 notation",
        )

        try:
            TlsConfig.from_paths(cert_path, other_key_path, ca_path).create_contexts()
        except TlsConfigError as exc:
            mismatch_error = str(exc)
        else:
            mismatch_error = ""
        report.check("do not belong together" in mismatch_error, "mismatched certificate and key fail before service start")

        try:
            normalize_fingerprint("not-a-fingerprint")
        except TlsConfigError:
            invalid_fingerprint_rejected = True
        else:
            invalid_fingerprint_rejected = False
        report.check(invalid_fingerprint_rejected, "invalid fingerprint syntax is rejected")
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
