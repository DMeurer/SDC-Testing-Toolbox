"""Focused checks that application and acceptance instance-name defaults stay aligned."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_toolbox  # noqa: E402
from examples import console  # noqa: E402
from sdctoolbox import constants  # noqa: E402
from tests.acceptance_provider import PEER_INSTANCE  # noqa: E402


def check(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise AssertionError(message)
    print(f"  PASS  {message}")


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
    print("RESULT: all application default checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
