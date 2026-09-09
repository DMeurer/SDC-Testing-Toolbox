"""Check that the standalone TLS helper creates usable and safe local credentials."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report  # noqa: E402
from sdctoolbox.security import TlsConfig  # noqa: E402


def main() -> int:
    report = Report()
    print("TLS helper")
    helper = ROOT / "helpers" / "tls" / "generate_certificates.py"
    with TemporaryDirectory(prefix="sdc-tls-helper-") as raw_temporary:
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
        addition = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(helper),
                "--output",
                str(output),
                "--add",
                "delta",
                "--ip",
                "127.0.0.1",
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
