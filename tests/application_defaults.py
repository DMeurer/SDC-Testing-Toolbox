"""Focused checks that application and acceptance instance-name defaults stay aligned."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from acceptance_provider import PEER_INSTANCE
from script_support import Report

import run_toolbox
from examples import console
from sdctoolbox import constants

REPORT = Report()


def check(condition: bool, message: str) -> None:
    REPORT.require(condition, message)


def parse_failure(argv: list[str]) -> tuple[int | None, str]:
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            run_toolbox.parse_args(argv)
    except SystemExit as exc:
        return exc.code, stderr.getvalue()
    return None, stderr.getvalue()


def check_ip_arguments() -> None:
    check(
        run_toolbox.parse_args(["--ip", " 192.0.2.10 "]).ip == "192.0.2.10",
        "the graphical entry point strips and canonicalizes an IPv4 argument",
    )

    for description, address in (
        ("hostname", "localhost"),
        ("IPv6 address", "::1"),
        ("malformed address", "999.1.2.3"),
        ("whitespace-only value", "   "),
        ("address with a suffix", "192.0.2.10/24"),
    ):
        code, stderr = parse_failure(["--ip", address])
        check(
            code == 2 and "argument --ip: must be a valid IPv4 address" in stderr,
            f"the graphical entry point rejects {description} with an --ip error",
        )


def check_invalid_ip_precedes_construction() -> None:
    constructions: list[str] = []

    def record_application(*_args: object, **_kwargs: object) -> None:
        constructions.append("QApplication")

    def record_provider(*_args: object, **_kwargs: object) -> None:
        constructions.append("ProviderService")

    original_application = run_toolbox.QApplication
    original_provider = run_toolbox.ProviderService
    run_toolbox.QApplication = record_application
    run_toolbox.ProviderService = record_provider
    try:
        code, stderr = None, io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                run_toolbox.main(["--ip", "not-an-address"])
        except SystemExit as exc:
            code = exc.code
    finally:
        run_toolbox.QApplication = original_application
        run_toolbox.ProviderService = original_provider

    check(
        code == 2
        and "argument --ip: must be a valid IPv4 address" in stderr.getvalue()
        and not constructions,
        "invalid --ip exits before QApplication or ProviderService construction",
    )


def main() -> int:
    print("Application default tests")
    check(
        run_toolbox.parse_args([]).name == constants.DEFAULT_INSTANCE_NAME,
        "the graphical entry point uses the shared instance name",
    )
    check(
        console.parse_args(["provider"]).name == constants.DEFAULT_INSTANCE_NAME,
        "the console provider uses the shared instance name",
    )
    check(
        console.parse_args(["provider", "--name", "named-peer"]).name == "named-peer",
        "the console provider accepts an explicit instance name",
    )
    check(
        PEER_INSTANCE != constants.DEFAULT_INSTANCE_NAME,
        "the acceptance peer remains distinct from applications using defaults",
    )
    check_ip_arguments()
    check_invalid_ip_precedes_construction()
    return REPORT.summary()


if __name__ == "__main__":
    raise SystemExit(main())
