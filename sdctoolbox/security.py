"""TLS configuration and X.509 inspection at the toolbox boundary.

The SDC stack accepts ready-made :class:`ssl.SSLContext` instances. Keeping their
construction here makes the application's TLS policy explicit and keeps PEM paths,
passwords, and certificate parsing out of provider and consumer code.
"""

from __future__ import annotations

import hashlib
import ipaddress
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtensionOID, NameOID
from sdc11073.certloader import SSLContextContainer


class TlsConfigError(ValueError):
    """TLS files or policy do not describe a usable participant identity."""


def _required_path(value: str | Path | None, field_name: str) -> Path:
    if value is None or not str(value).strip():
        raise TlsConfigError(f"{field_name} is required when TLS is enabled")
    path = Path(value).expanduser()
    if not path.is_file():
        raise TlsConfigError(f"{field_name} is not a readable file: {path}")
    return path


def _load_certificate(path: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise TlsConfigError(f"certificate is not a PEM X.509 certificate: {path}") from exc


def _load_private_key(path: Path, password: str | bytes | None) -> Any:
    encoded_password = password.encode("utf-8") if isinstance(password, str) else password
    try:
        return serialization.load_pem_private_key(path.read_bytes(), password=encoded_password)
    except TypeError as exc:
        raise TlsConfigError(f"private key password is required or incorrect: {path}") from exc
    except (OSError, ValueError) as exc:
        raise TlsConfigError(f"private key is not a readable PEM private key: {path}") from exc


def normalize_fingerprint(value: str) -> str:
    """Normalize a SHA-256 certificate fingerprint to uppercase hexadecimal."""
    compact = value.replace(":", "").replace(" ", "").strip()
    if len(compact) != 64 or any(character not in "0123456789abcdefABCDEF" for character in compact):
        raise TlsConfigError("peer fingerprint must be a SHA-256 value with 64 hexadecimal digits")
    return compact.upper()


def _name_value(name: x509.Name, oid: x509.ObjectIdentifier) -> str | None:
    attributes = name.get_attributes_for_oid(oid)
    return attributes[0].value if attributes else None


def _subject_alt_names(certificate: x509.Certificate) -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        names = certificate.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except x509.ExtensionNotFound:
        return (), ()
    return (
        tuple(str(name) for name in names.get_values_for_type(x509.DNSName)),
        tuple(str(name) for name in names.get_values_for_type(x509.IPAddress)),
    )


@dataclass(frozen=True)
class CertificateInfo:
    """Safe, user-facing metadata copied from a peer or local certificate."""

    subject: str
    common_name: str | None
    issuer: str
    serial_number: str
    sha256_fingerprint: str
    not_valid_before: datetime
    not_valid_after: datetime
    dns_names: tuple[str, ...]
    ip_addresses: tuple[str, ...]
    extended_key_usages: tuple[str, ...]

    @classmethod
    def from_certificate(cls, certificate: x509.Certificate) -> CertificateInfo:
        try:
            usages = certificate.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE).value
            extended_key_usages = tuple(usage.dotted_string for usage in usages)
        except x509.ExtensionNotFound:
            extended_key_usages = ()
        dns_names, ip_addresses = _subject_alt_names(certificate)
        return cls(
            subject=certificate.subject.rfc4514_string(),
            common_name=_name_value(certificate.subject, NameOID.COMMON_NAME),
            issuer=certificate.issuer.rfc4514_string(),
            serial_number=f"{certificate.serial_number:X}",
            sha256_fingerprint=certificate.fingerprint(hashes.SHA256()).hex().upper(),
            not_valid_before=certificate.not_valid_before_utc,
            not_valid_after=certificate.not_valid_after_utc,
            dns_names=dns_names,
            ip_addresses=ip_addresses,
            extended_key_usages=extended_key_usages,
        )

    @classmethod
    def from_der(cls, der_certificate: bytes) -> CertificateInfo:
        try:
            certificate = x509.load_der_x509_certificate(der_certificate)
        except ValueError as exc:
            raise TlsConfigError("peer did not present a valid DER X.509 certificate") from exc
        return cls.from_certificate(certificate)

    @property
    def is_currently_valid(self) -> bool:
        now = datetime.now(UTC)
        return self.not_valid_before <= now <= self.not_valid_after


@dataclass(frozen=True)
class TlsConfig:
    """Local mTLS identity, trust bundle, and optional individual peer pin."""

    certificate_path: Path
    private_key_path: Path
    ca_bundle_path: Path
    private_key_password: str | bytes | None = field(default=None, repr=False, compare=False)
    peer_fingerprint: str | None = None
    server_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "certificate_path", Path(self.certificate_path).expanduser())
        object.__setattr__(self, "private_key_path", Path(self.private_key_path).expanduser())
        object.__setattr__(self, "ca_bundle_path", Path(self.ca_bundle_path).expanduser())
        if self.peer_fingerprint is not None:
            object.__setattr__(self, "peer_fingerprint", normalize_fingerprint(self.peer_fingerprint))
        if self.server_name is not None and not self.server_name.strip():
            raise TlsConfigError("TLS server name cannot be blank")

    @classmethod
    def from_paths(
        cls,
        certificate_path: str | Path | None,
        private_key_path: str | Path | None,
        ca_bundle_path: str | Path | None,
        *,
        private_key_password: str | bytes | None = None,
        peer_fingerprint: str | None = None,
        server_name: str | None = None,
    ) -> TlsConfig:
        """Validate required TLS fields before constructing a service."""
        return cls(
            certificate_path=_required_path(certificate_path, "TLS certificate"),
            private_key_path=_required_path(private_key_path, "TLS private key"),
            ca_bundle_path=_required_path(ca_bundle_path, "TLS CA bundle"),
            private_key_password=private_key_password,
            peer_fingerprint=peer_fingerprint,
            server_name=server_name,
        )

    def local_certificate(self) -> CertificateInfo:
        """Load safe information for the local certificate without exposing its key."""
        return CertificateInfo.from_certificate(_load_certificate(self.certificate_path))

    def create_contexts(self) -> SSLContextContainer:
        """Create strict TLS 1.2+ contexts with reciprocal CA verification."""
        certificate = _load_certificate(self.certificate_path)
        private_key = _load_private_key(self.private_key_path, self.private_key_password)
        if certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ) != private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ):
            raise TlsConfigError("TLS certificate and private key do not belong together")
        if not CertificateInfo.from_certificate(certificate).is_currently_valid:
            raise TlsConfigError("TLS certificate is expired or not yet valid")
        try:
            ca_certificates = x509.load_pem_x509_certificates(self.ca_bundle_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise TlsConfigError(f"TLS CA bundle is not a PEM X.509 certificate bundle: {self.ca_bundle_path}") from exc
        if not ca_certificates:
            raise TlsConfigError(f"TLS CA bundle contains no certificates: {self.ca_bundle_path}")

        password = self.private_key_password.encode("utf-8") if isinstance(self.private_key_password, str) else self.private_key_password
        client_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(self.ca_bundle_path))
        client_context.minimum_version = ssl.TLSVersion.TLSv1_2
        client_context.load_cert_chain(
            certfile=str(self.certificate_path),
            keyfile=str(self.private_key_path),
            password=password,
        )

        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.minimum_version = ssl.TLSVersion.TLSv1_2
        server_context.load_cert_chain(
            certfile=str(self.certificate_path),
            keyfile=str(self.private_key_path),
            password=password,
        )
        server_context.verify_mode = ssl.CERT_REQUIRED
        server_context.load_verify_locations(cafile=str(self.ca_bundle_path))
        return SSLContextContainer(client_context=client_context, server_context=server_context)

    def verify_peer_certificate(self, der_certificate: bytes | None) -> CertificateInfo:
        """Apply the optional leaf-certificate pin after TLS chain validation."""
        if der_certificate is None:
            raise TlsConfigError("TLS peer did not present a certificate")
        info = CertificateInfo.from_der(der_certificate)
        if self.peer_fingerprint is not None and info.sha256_fingerprint != self.peer_fingerprint:
            raise TlsConfigError("TLS peer certificate does not match the configured fingerprint")
        return info


def certificate_matches_endpoint(certificate: CertificateInfo, endpoint: str) -> bool:
    """Return whether a certificate SAN covers a literal IP address or DNS endpoint."""
    try:
        address = ipaddress.ip_address(endpoint)
    except ValueError:
        return endpoint.casefold() in {name.casefold() for name in certificate.dns_names}
    return str(address) in certificate.ip_addresses


def fingerprint_from_der(der_certificate: bytes) -> str:
    """Calculate a SHA-256 pin when full certificate inspection is unnecessary."""
    return hashlib.sha256(der_certificate).hexdigest().upper()
