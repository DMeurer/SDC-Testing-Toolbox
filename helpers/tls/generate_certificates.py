"""Generate a local self-signed CA and mutual-TLS identities for the toolbox.

The result is suitable for any number of local toolbox instances. Each participant trusts
``ca.pem`` and uses its own ``<name>.pem`` certificate and ``<name>-key.pem``
private key. ``ca-key.pem`` stays with the test administrator and is needed only
when adding another participant later. Generated private keys are encrypted unless
``--no-password`` is selected deliberately.

Examples:

    python helpers/tls/generate_certificates.py
    python helpers/tls/generate_certificates.py --ip 192.168.1.42 --password-file secret.txt
    python helpers/tls/generate_certificates.py --participants alpha beta gamma
    python helpers/tls/generate_certificates.py --add gamma --ip 10.0.65.145
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


DEFAULT_OUTPUT = Path(__file__).resolve().parent / "generated"
VALIDITY_DAYS = 365
CA_VALIDITY_DAYS = 3650
KEY_SIZE = 3072


def participant_name(value: str) -> str:
    """Accept a safe filename and certificate common name for one toolbox instance."""
    name = value.strip()
    if not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in name):
        raise argparse.ArgumentTypeError("must contain only letters, numbers, hyphens, or underscores")
    return name


def ipv4_argument(value: str) -> ipaddress.IPv4Address:
    """Accept one IPv4 address, which becomes an IP subject alternative name."""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise argparse.ArgumentTypeError("must be an IPv4 address")
    return address


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"directory to create (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--ip", type=ipv4_argument, default=ipaddress.IPv4Address("127.0.0.1"), help="IPv4 SAN for both participants")
    participants = parser.add_mutually_exclusive_group()
    participants.add_argument(
        "--participants",
        nargs="+",
        type=participant_name,
        default=("alpha", "beta"),
        metavar="NAME",
        help="certificate names for toolbox instances (default: alpha beta)",
    )
    participants.add_argument(
        "--add",
        nargs="+",
        type=participant_name,
        metavar="NAME",
        help="add identities signed by the existing ca.pem and ca-key.pem",
    )
    parser.add_argument("--password-file", type=Path, help="read private-key password from a UTF-8 file")
    parser.add_argument("--no-password", action="store_true", help="write unencrypted private keys (not recommended)")
    parser.add_argument("--force", action="store_true", help="replace a previously generated directory")
    return parser.parse_args(argv)


def key_password(args: argparse.Namespace) -> bytes | None:
    """Get one local key password without writing it to command history or output."""
    if args.no_password and args.password_file is not None:
        raise ValueError("--no-password and --password-file cannot be used together")
    if args.no_password:
        return None
    if args.password_file is not None:
        try:
            password = args.password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read password file: {args.password_file}") from exc
    else:
        password = getpass.getpass("Private-key password: ")
        confirmation = getpass.getpass("Repeat private-key password: ")
        if password != confirmation:
            raise ValueError("private-key passwords do not match")
    if not password:
        raise ValueError("private-key password cannot be empty; use --no-password deliberately if required")
    return password.encode("utf-8")


def certificate_builder(subject: x509.Name, issuer: x509.Name, public_key, not_after: datetime) -> x509.CertificateBuilder:  # noqa: ANN001
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(not_after)
    )


def make_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Create the self-signed trust anchor shared by the generated participants."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SDC Testing Toolbox Local CA")])
    certificate = (
        certificate_builder(name, name, key.public_key(), datetime.now(UTC) + timedelta(days=CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def make_participant(name: str, ip: ipaddress.IPv4Address, ca_key, ca_certificate: x509.Certificate) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:  # noqa: ANN001
    """Create an mTLS client/server identity signed by the generated CA."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    certificate = (
        certificate_builder(
            subject,
            ca_certificate.subject,
            key.public_key(),
            min(datetime.now(UTC) + timedelta(days=VALIDITY_DAYS), ca_certificate.not_valid_after_utc),
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, certificate


def write_certificate(path: Path, certificate: x509.Certificate) -> None:
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def write_private_key(path: Path, key: rsa.RSAPrivateKey, password: bytes | None) -> None:
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            encryption,
        ),
    )


def read_ca(path: Path, password: bytes | None) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    """Load and validate the local signing CA required to add participants."""
    certificate_path = path / "ca.pem"
    private_key_path = path / "ca-key.pem"
    if not certificate_path.is_file() or not private_key_path.is_file():
        raise ValueError(
            f"adding a participant needs {certificate_path.name} and {private_key_path.name}; "
            "a CA certificate alone cannot sign a new participant certificate",
        )
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=password)
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError("cannot read the existing CA certificate/key with the supplied password") from exc
    try:
        basic_constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound as exc:
        raise ValueError("existing ca.pem is not a certificate authority") from exc
    if not basic_constraints.ca:
        raise ValueError("existing ca.pem is not a certificate authority")
    if certificate.public_key().public_numbers() != private_key.public_key().public_numbers():
        raise ValueError("existing ca.pem and ca-key.pem do not belong together")
    if certificate.not_valid_after_utc <= datetime.now(UTC):
        raise ValueError("existing CA certificate is expired")
    return private_key, certificate


def existing_participants(path: Path) -> tuple[str, ...]:
    """Return complete generated identities, sorted so the instruction file is stable."""
    names = []
    for certificate_path in path.glob("*.pem"):
        name = certificate_path.stem
        if name != "ca" and (path / f"{name}-key.pem").is_file():
            names.append(name)
    return tuple(sorted(names))


def prepare_output(path: Path, participants: tuple[str, ...], force: bool) -> None:
    """Avoid accidental key replacement; ``--force`` only clears known generated files."""
    expected = {"ca.pem", "ca-key.pem", "README.txt"}
    for name in participants:
        expected.update({f"{name}.pem", f"{name}-key.pem"})
    if path.exists():
        existing = {entry.name for entry in path.iterdir()}
        if existing and not force:
            raise ValueError(f"output directory is not empty: {path}; choose another directory or use --force")
        unexpected = existing - expected
        if unexpected:
            raise ValueError(f"refusing to replace non-generated files in {path}: {', '.join(sorted(unexpected))}")
    path.mkdir(parents=True, exist_ok=True)


def prepare_addition(path: Path, participants: tuple[str, ...]) -> None:
    """Refuse accidental replacement while preserving all existing local identities."""
    if not path.is_dir():
        raise ValueError(f"TLS output directory does not exist: {path}")
    duplicates = [
        name
        for name in participants
        if (path / f"{name}.pem").exists() or (path / f"{name}-key.pem").exists()
    ]
    if duplicates:
        raise ValueError(f"refusing to replace existing participant material: {', '.join(duplicates)}")


def write_instructions(
    path: Path,
    participants: tuple[str, ...],
    ip: ipaddress.IPv4Address,
    encrypted: bool,
) -> None:
    password_note = (
        "Enter the same password in the GUI for each instance, or set SDC_TOOLBOX_TLS_KEY_PASSWORD before CLI use."
        if encrypted
        else "The private keys are unencrypted; protect this directory and regenerate it for real use."
    )
    path.write_text(
        "SDC Testing Toolbox local mutual-TLS material\n"
        "==============================================\n\n"
        f"Generated for IPv4 address: {ip}\n"
        "The CA certificate is self-signed. It explicitly trusts every participant certificate below.\n\n"
        "Each toolbox instance is both an SDC provider and consumer. Every generated identity\n"
        "therefore has both TLS server and client authentication capabilities.\n\n"
        "Trust bundle for every participant: ca.pem\n\n"
        "Run one command per instance from the toolbox repository root:\n\n"
        + "".join(
            f"  python run_toolbox.py --ip {ip} --name {name} --tls-cert \"{path.parent / f'{name}.pem'}\" --tls-key \"{path.parent / f'{name}-key.pem'}\" --tls-ca \"{path.parent / 'ca.pem'}\"\n"
            for name in participants
        )
        + "\n"
        f"{password_note}\n\n"
        "Do not commit this directory. The private keys grant the generated participant identities.\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    participants = tuple(args.add if args.add is not None else args.participants)
    if len(set(participants)) != len(participants):
        print("error: participant names must be distinct", file=sys.stderr)
        return 2
    try:
        password = key_password(args)
        output = args.output.resolve()
        if args.add is None:
            prepare_output(output, participants, args.force)
            ca_key, ca_certificate = make_ca()
            write_certificate(output / "ca.pem", ca_certificate)
            write_private_key(output / "ca-key.pem", ca_key, password)
        else:
            if args.force:
                raise ValueError("--force only applies when creating a new CA; it cannot replace participant identities")
            prepare_addition(output, participants)
            ca_key, ca_certificate = read_ca(output, password)
        for name in participants:
            key, certificate = make_participant(name, args.ip, ca_key, ca_certificate)
            write_certificate(output / f"{name}.pem", certificate)
            write_private_key(output / f"{name}-key.pem", key, password)
        all_participants = existing_participants(output)
        write_instructions(output / "README.txt", all_participants, args.ip, password is not None)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.add is None:
        summary = f"Created self-signed local CA and {len(participants)} mTLS identities"
    else:
        summary = f"Added {len(participants)} mTLS identit{'y' if len(participants) == 1 else 'ies'}"
    print(f"{summary} in {output}")
    for name in participants:
        print(f"Certificate: {output / f'{name}.pem'}")
    print("Read README.txt there for the exact startup commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
