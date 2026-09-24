"""Deterministic TLS configuration and certificate-inspection checks."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import sys
import threading
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


def self_signed_identity(directory: Path, name: str, ip_name: str | None = "127.0.0.1") -> tuple[Path, Path, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    certificate = issue_certificate(name, key, subject, key, ip_name=ip_name)
    certificate_path = directory / f"{name}.pem"
    key_path = directory / f"{name}-key.pem"
    write_certificate(certificate_path, certificate)
    write_key(key_path, key)
    return certificate_path, key_path, certificate


def handshake(server: TlsConfig, client: TlsConfig, server_hostname: str = "127.0.0.1") -> str | None:
    """Run one TLS handshake over loopback. None on success, else the client-side error."""
    server_contexts = server.create_contexts()
    client_contexts = client.create_contexts()
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            connection, _ = listener.accept()
            with server_contexts.server_context.wrap_socket(connection, server_side=True) as tls:
                tls.recv(1)
        except (OSError, ssl.SSLError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
            client_contexts.client_context.wrap_socket(raw, server_hostname=server_hostname) as tls,
        ):
            client.verify_peer_certificate(tls.getpeercert(binary_form=True), server_hostname)
            tls.sendall(b"x")
    except (OSError, ssl.SSLError, TlsConfigError) as exc:
        return str(exc) or type(exc).__name__
    finally:
        listener.close()
        thread.join(5)
    return None


def relaxed_policy_checks(report: Report, temporary: Path) -> None:
    print("Relaxed certificate policies")
    alpha_cert, alpha_key, _ = self_signed_identity(temporary, "alpha")
    beta_cert, beta_key, _ = self_signed_identity(temporary, "beta")
    wrong_cert, wrong_key, _ = self_signed_identity(temporary, "wrong-ip", ip_name="10.9.8.7")

    try:
        TlsConfig.from_paths(alpha_cert, alpha_key, None)
    except TlsConfigError:
        needs_trust = True
    else:
        needs_trust = False
    report.check(needs_trust, "TLS without CA, folder, or self-signed permission is rejected")

    lenient_alpha = TlsConfig.from_paths(alpha_cert, alpha_key, None, allow_self_signed=True)
    lenient_beta = TlsConfig.from_paths(beta_cert, beta_key, None, allow_self_signed=True)
    report.check(handshake(lenient_alpha, lenient_beta) is None, "self-signed peers connect when allowed")

    folder = temporary / "trusted"
    folder.mkdir()
    (folder / "alpha.pem").write_bytes(alpha_cert.read_bytes())
    (folder / "beta.crt").write_bytes(beta_cert.read_bytes())
    (folder / "alpha-key.pem").write_bytes(alpha_key.read_bytes())  # non-certificates are skipped
    folder_alpha = TlsConfig.from_paths(alpha_cert, alpha_key, None, trusted_folder=folder)
    folder_beta = TlsConfig.from_paths(beta_cert, beta_key, None, trusted_folder=folder)
    report.check(
        folder_alpha.create_contexts().server_context.verify_mode == ssl.CERT_REQUIRED,
        "trusted folder keeps mutual authentication",
    )
    report.check(handshake(folder_alpha, folder_beta) is None, "self-signed certificates in the trusted folder connect")

    stranger = TlsConfig.from_paths(wrong_cert, wrong_key, None, trusted_folder=folder)
    trust_only_beta = TlsConfig.from_paths(beta_cert, beta_key, None, trusted_folder=folder, verify_hostname=False)
    report.check(handshake(stranger, trust_only_beta) is not None, "a certificate outside the trusted folder is refused")

    (folder / "wrong-ip.pem").write_bytes(wrong_cert.read_bytes())
    strict_beta = TlsConfig.from_paths(beta_cert, beta_key, None, trusted_folder=folder)
    report.check(handshake(stranger, strict_beta) is not None, "trusted peer naming another IP fails the hostname check")
    loose_beta = TlsConfig.from_paths(beta_cert, beta_key, None, trusted_folder=folder, verify_hostname=False)
    report.check(handshake(stranger, loose_beta) is None, "disabled hostname check accepts a trusted peer naming another IP")

    wrong_self_signed = TlsConfig.from_paths(wrong_cert, wrong_key, None, allow_self_signed=True)
    report.check(
        handshake(wrong_self_signed, lenient_beta) is not None,
        "self-signed mode still checks the advertised IP",
    )
    loose_self_signed = TlsConfig.from_paths(beta_cert, beta_key, None, allow_self_signed=True, verify_hostname=False)
    report.check(
        handshake(wrong_self_signed, loose_self_signed) is None,
        "self-signed mode with disabled hostname check accepts any IP",
    )

    empty = temporary / "empty"
    empty.mkdir()
    try:
        TlsConfig.from_paths(alpha_cert, alpha_key, None, trusted_folder=empty).create_contexts()
    except TlsConfigError:
        empty_rejected = True
    else:
        empty_rejected = False
    report.check(empty_rejected, "an empty trusted folder as the only trust source is rejected")


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
            and contexts.client_context.verify_mode == ssl.CERT_REQUIRED
            and contexts.client_context.cert_store_stats()["x509_ca"] == 1,
            "client context trusts only the configured CA and requires TLS 1.2+ hostname-checked peers",
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

        relaxed_policy_checks(report, temporary)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
