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
from urllib.parse import urlsplit

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
    ca_bundle_path: Path | None = None
    private_key_password: str | bytes | None = field(default=None, repr=False, compare=False)
    peer_fingerprint: str | None = None
    server_name: str | None = None
    # Directory of PEM certificates that are trusted as-is, self-signed ones included.
    trusted_folder: Path | None = None
    # Accept peers presenting a valid self-signed certificate. Disables TLS-level chain
    # validation, so the server side no longer requests a client certificate.
    allow_self_signed: bool = False
    # False turns off the check that the certificate names the host or IP connected to.
    verify_hostname: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "certificate_path", Path(self.certificate_path).expanduser())
        object.__setattr__(self, "private_key_path", Path(self.private_key_path).expanduser())
        if self.ca_bundle_path is not None:
            object.__setattr__(self, "ca_bundle_path", Path(self.ca_bundle_path).expanduser())
        if self.trusted_folder is not None:
            object.__setattr__(self, "trusted_folder", Path(self.trusted_folder).expanduser())
        if self.ca_bundle_path is None and self.trusted_folder is None and not self.allow_self_signed:
            raise TlsConfigError("TLS needs a CA bundle, a trusted certificate folder, or self-signed certificates enabled")
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
        trusted_folder: str | Path | None = None,
        allow_self_signed: bool = False,
        verify_hostname: bool = True,
    ) -> TlsConfig:
        """Validate required TLS fields before constructing a service."""
        has_ca = ca_bundle_path is not None and bool(str(ca_bundle_path).strip())
        has_folder = trusted_folder is not None and bool(str(trusted_folder).strip())
        folder = Path(trusted_folder).expanduser() if has_folder else None
        if folder is not None and not folder.is_dir():
            raise TlsConfigError(f"TLS trusted certificate folder is not a directory: {folder}")
        return cls(
            certificate_path=_required_path(certificate_path, "TLS certificate"),
            private_key_path=_required_path(private_key_path, "TLS private key"),
            ca_bundle_path=_required_path(ca_bundle_path, "TLS CA bundle") if has_ca else None,
            private_key_password=private_key_password,
            peer_fingerprint=peer_fingerprint,
            server_name=server_name,
            trusted_folder=folder,
            allow_self_signed=allow_self_signed,
            verify_hostname=verify_hostname,
        )

    def _trust_anchors(self) -> list[x509.Certificate]:
        """Certificates from the CA bundle and the trusted folder."""
        anchors: list[x509.Certificate] = []
        if self.ca_bundle_path is not None:
            try:
                anchors.extend(x509.load_pem_x509_certificates(self.ca_bundle_path.read_bytes()))
            except (OSError, ValueError) as exc:
                raise TlsConfigError(f"TLS CA bundle is not a PEM X.509 certificate bundle: {self.ca_bundle_path}") from exc
            if not anchors:
                raise TlsConfigError(f"TLS CA bundle contains no certificates: {self.ca_bundle_path}")
        if self.trusted_folder is not None:
            try:
                files = sorted(
                    entry
                    for entry in self.trusted_folder.iterdir()
                    if entry.is_file() and entry.suffix.lower() in TRUSTED_FOLDER_SUFFIXES
                )
            except OSError as exc:
                raise TlsConfigError(f"TLS trusted certificate folder is not readable: {self.trusted_folder}") from exc
            found = 0
            for entry in files:
                try:
                    certificates = x509.load_pem_x509_certificates(entry.read_bytes())
                except (OSError, ValueError):
                    continue  # private keys and other files may live next to the certificates
                anchors.extend(certificates)
                found += len(certificates)
            if found == 0 and self.ca_bundle_path is None and not self.allow_self_signed:
                raise TlsConfigError(f"TLS trusted certificate folder contains no PEM certificates: {self.trusted_folder}")
        return anchors

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
        anchors = self._trust_anchors()
        anchor_pem = "".join(anchor.public_bytes(serialization.Encoding.PEM).decode("ascii") for anchor in anchors)

        password = self.private_key_password.encode("utf-8") if isinstance(self.private_key_password, str) else self.private_key_password
        # The configured trust material is the complete trust policy. Do not inherit
        # operating-system roots, which could admit an unintended publicly trusted peer.
        client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client_context.minimum_version = ssl.TLSVersion.TLSv1_2
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.minimum_version = ssl.TLSVersion.TLSv1_2
        for context in (client_context, server_context):
            if anchor_pem:
                context.load_verify_locations(cadata=anchor_pem)
                # Lets a trusted leaf (e.g. a self-signed certificate in the folder) anchor the chain.
                context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            context.load_cert_chain(
                certfile=str(self.certificate_path),
                keyfile=str(self.private_key_path),
                password=password,
            )
        server_context.verify_mode = ssl.CERT_REQUIRED
        if self.allow_self_signed:
            # Python offers no verification callback, so the client's chain decision moves to
            # verify_peer_certificate and the server stops requesting client certificates.
            client_context.check_hostname = False
            client_context.verify_mode = ssl.CERT_NONE
            server_context.verify_mode = ssl.CERT_NONE
        elif not self.verify_hostname:
            client_context.check_hostname = False
        return SSLContextContainer(client_context=client_context, server_context=server_context)

    def verify_peer_certificate(self, der_certificate: bytes | None, endpoint: str | None = None) -> CertificateInfo:
        """Apply the leaf pin and, when TLS chain validation was relaxed, the trust policy.

        ``endpoint`` is the host the connection went to; it is checked against the
        certificate's names here only because the TLS stack no longer does it.
        """
        if der_certificate is None:
            raise TlsConfigError("TLS peer did not present a certificate")
        info = CertificateInfo.from_der(der_certificate)
        if self.peer_fingerprint is not None and info.sha256_fingerprint != self.peer_fingerprint:
            raise TlsConfigError("TLS peer certificate does not match the configured fingerprint")
        if self.allow_self_signed:
            certificate = x509.load_der_x509_certificate(der_certificate)
            if not info.is_currently_valid:
                raise TlsConfigError("TLS peer certificate is expired or not yet valid")
            if not self._is_trusted(certificate):
                raise TlsConfigError("TLS peer certificate is neither self-signed nor issued by a trusted certificate")
            if self.verify_hostname:
                host = self.server_name or endpoint
                if host is not None and not certificate_matches_endpoint(info, host):
                    raise TlsConfigError(f"TLS peer certificate does not name {host}")
        return info

    def _is_trusted(self, certificate: x509.Certificate) -> bool:
        if _is_self_signed(certificate):
            return True
        for anchor in self._trust_anchors():
            if anchor == certificate:
                return True
            try:
                certificate.verify_directly_issued_by(anchor)
            except Exception:  # noqa: BLE001, S112 - simply not issued by this anchor
                continue
            return True
        return False


TRUSTED_FOLDER_SUFFIXES = frozenset({".pem", ".crt", ".cer"})


def _is_self_signed(certificate: x509.Certificate) -> bool:
    try:
        certificate.verify_directly_issued_by(certificate)
    except Exception:  # noqa: BLE001 - any failure means "not self-signed"
        return False
    return True


def endpoint_host(address: str) -> str | None:
    """Host part of a service URL, for the hostname check."""
    return urlsplit(address).hostname


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
