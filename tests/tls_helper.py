"""Check that the standalone TLS helper creates usable and safe local credentials."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report  # noqa: E402
from sdctoolbox.security import TlsConfig  # noqa: E402


def collision_checks(report: Report, helper: Path, original: Path) -> None:
    """Overlapping output names must fail before creating or replacing any file."""
    batches = (("ca",), ("ca-key",), ("alpha", "alpha-key"), ("alpha", "ALPHA"))
    for index, names in enumerate(batches):
        output = original.parent / f"collision-{index}"
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(helper), "--output", str(output), "--participants", *names, "--no-password"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        report.check(
            result.returncode == 2 and not output.exists(),
            f"overlapping participant names {names} are rejected before output writes",
            result.stderr,
        )
    for index, names in enumerate((*batches, ("delta", "delta-key"), ("delta", "DELTA"), ("ALPHA",))):
        output = original.parent / f"addition-collision-{index}"
        copytree(original, output)
        before = {path.name: path.read_bytes() for path in output.iterdir()}
        result = subprocess.run(  # noqa: S603
            [sys.executable, str(helper), "--output", str(output), "--add", *names, "--no-password"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        report.check(
            result.returncode == 2 and before == {path.name: path.read_bytes() for path in output.iterdir()},
            f"overlapping addition {names} leaves every existing file unchanged",
            result.stderr,
        )


def key_suffix_identity_checks(report: Report, helper: Path, temporary: Path) -> None:
    """Private-key PEM filenames must not invent identities beside valid -key-key names."""
    def command_names(output: Path) -> tuple[str, ...]:
        readme = output / "README.txt"
        if not readme.is_file():
            return ()
        return tuple(line.split("--name ", 1)[1].split()[0]
                     for line in readme.read_text(encoding="utf-8").splitlines()
                     if "run_toolbox.py --ip" in line)

    initial = temporary / "valid-ca-key-key"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(helper), "--output", str(initial), "--participants", "ca-key-key", "--no-password"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    report.check(result.returncode == 0, "initial generation accepts the valid ca-key-key identity", result.stderr)
    report.check(
        command_names(initial) == ("ca-key-key",)
        and {path.name for path in initial.iterdir()} == {
            "ca.pem", "ca-key.pem", "ca-key-key.pem", "ca-key-key-key.pem", "README.txt",
        },
        "initial README contains exactly the certificate identity, not the CA private key",
        str(command_names(initial)),
    )

    output = temporary / "valid-alpha-key-key"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(helper), "--output", str(output), "--participants", "alpha", "--no-password"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    report.require(result.returncode == 0, "creates the original alpha identity for the suffix addition", result.stderr)
    before = {path.name: path.read_bytes() for path in output.glob("*.pem")}
    addition = subprocess.run(  # noqa: S603
        [sys.executable, str(helper), "--output", str(output), "--add", "alpha-key-key", "--no-password"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    report.check(addition.returncode == 0, "adding valid alpha-key-key to alpha succeeds", addition.stderr)
    report.check(
        command_names(output) == ("alpha", "alpha-key-key")
        and {path.name for path in output.iterdir()} == {
            *before, "alpha-key-key.pem", "alpha-key-key-key.pem", "README.txt",
        },
        "addition README contains exactly alpha and alpha-key-key, not either private key",
        str(command_names(output)),
    )
    report.check(
        all((output / name).read_bytes() == contents for name, contents in before.items()),
        "valid suffix addition leaves all original certificates and keys byte-for-byte unchanged",
    )


def main() -> int:
    report = Report()
    print("TLS helper")
    helper = ROOT / "helpers" / "tls" / "generate_certificates.py"
    with TemporaryDirectory(prefix="sdc-tls-helper-") as raw_temporary:
        key_suffix_identity_checks(report, helper, Path(raw_temporary))
        output = Path(raw_temporary) / "identities"
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(helper),
                "--output",
                str(output),
                "--ip",
                "127.0.0.1",
                "--participants",
                "alpha",
                "beta",
                "gamma",
                "--no-password",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        report.check(result.returncode == 0, "helper creates an isolated local PKI", result.stderr)
        expected = {
            "ca.pem",
            "ca-key.pem",
            "alpha.pem",
            "alpha-key.pem",
            "beta.pem",
            "beta-key.pem",
            "gamma.pem",
            "gamma-key.pem",
            "README.txt",
        }
        report.check({path.name for path in output.iterdir()} == expected, "helper writes the documented CA and identities")
        contexts = TlsConfig.from_paths(output / "alpha.pem", output / "alpha-key.pem", output / "ca.pem").create_contexts()
        report.check(contexts.client_context.check_hostname, "helper output is accepted by strict toolbox TLS contexts")
        readme = (output / "README.txt").read_text(encoding="utf-8")
        report.check(
            all(f"--name {name}" in readme for name in ("alpha", "beta", "gamma")),
            "helper writes one startup command per interchangeable participant identity",
        )
        collision_checks(report, helper, output)
        original_material = {path.name: path.read_bytes() for path in output.glob("*.pem")}
        addition = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(helper),
                "--output",
                str(output),
                "--add",
                "delta",
                "--ip",
                "10.9.8.7",
                "--no-password",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        report.check(
            addition.returncode == 0
            and (output / "delta.pem").is_file()
            and (output / "delta-key.pem").is_file()
            and TlsConfig.from_paths(
                output / "delta.pem",
                output / "delta-key.pem",
                output / "ca.pem",
            ).create_contexts(),
            "helper adds a matching participant without replacing the existing CA",
            addition.stderr,
        )
        report.check(
            all((output / name).read_bytes() == contents for name, contents in original_material.items()),
            "adding a participant at another address preserves every existing certificate and key",
        )
        commands = [line.split() for line in (output / "README.txt").read_text(encoding="utf-8").splitlines()
                    if "run_toolbox.py --ip" in line]
        advertised = {words[words.index("--name") + 1]: words[words.index("--ip") + 1] for words in commands}
        report.check(
            advertised == {"alpha": "127.0.0.1", "beta": "127.0.0.1", "gamma": "127.0.0.1", "delta": "10.9.8.7"}
            and all(
                address in TlsConfig.from_paths(output / f"{name}.pem", output / f"{name}-key.pem", output / "ca.pem")
                .local_certificate().ip_addresses
                for name, address in advertised.items()
            ),
            "each generated startup command uses an IP SAN from that participant's unchanged certificate",
            str(advertised),
        )
        repeat = subprocess.run(  # noqa: S603
            [sys.executable, str(helper), "--output", str(output), "--no-password"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        report.check(repeat.returncode == 2 and "not empty" in repeat.stderr, "helper refuses to overwrite key material")
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
