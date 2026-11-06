"""Run direct-script suites from suites.json without importing them."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from process_control import start_process, terminate_process_tree
from script_support import ProcessOutput

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
DEFAULT_MANIFEST = TESTS / "suites.json"
DEFAULT_ROLES = frozenset({"deterministic", "policy"})
REQUIRED_FIELDS = {
    "id",
    "argv",
    "cwd",
    "role",
    "platforms",
    "events",
    "timeout_seconds",
    "environment",
    "resources",
    "log",
}


def load_manifest(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("suites"), list):
        raise ValueError("suite manifest must have version 1 and a suites list")
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise TypeError("suite manifest defaults must be an object")
    suites = [{**defaults, **suite} for suite in data["suites"]]
    ids: set[str] = set()
    for suite in suites:
        missing = REQUIRED_FIELDS - suite.keys()
        if missing:
            raise ValueError(f"suite is missing fields: {sorted(missing)}")
        suite_id = suite["id"]
        if not isinstance(suite_id, str) or not suite_id or suite_id in ids:
            message = f"suite id must be a unique non-empty string: {suite_id!r}"
            raise ValueError(message)
        ids.add(suite_id)
        if not isinstance(suite["argv"], list) or not suite["argv"]:
            raise ValueError(f"suite {suite_id!r} needs a non-empty argv")
        timeout = suite["timeout_seconds"]
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"suite {suite_id!r} needs a positive timeout_seconds")
    return suites


def run_suite(suite: dict[str, Any], *, root: Path = ROOT) -> int:
    environment = os.environ.copy()
    environment.update(
        {str(key): str(value) for key, value in suite["environment"].items()},
    )
    command = [sys.executable, *map(str, suite["argv"])]
    working_directory = root / suite.get("cwd", ".")
    resources = ", ".join(suite.get("resources", ())) or "none"
    print(f"\n== {suite['id']} (resources: {resources}) ==", flush=True)
    try:
        process = start_process(
            command,
            cwd=working_directory,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        print(
            f"RUNNER ERROR: could not start {suite['id']}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    output = None
    try:
        output = ProcessOutput(process, echo=False)
        timeout = float(suite["timeout_seconds"])
        deadline = time.monotonic() + timeout
        try:
            process.wait(timeout=timeout)
            if not output.join(timeout=max(0, deadline - time.monotonic())):
                raise subprocess.TimeoutExpired(command, timeout)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process)
            output.join(timeout=2)
            print(output.buffered_output, end="", flush=True)
            print(f"RUNNER TIMEOUT: {suite['id']}", file=sys.stderr, flush=True)
            return 124
        except KeyboardInterrupt:
            print(f"RUNNER INTERRUPTED: {suite['id']}", file=sys.stderr, flush=True)
            raise
        print(output.buffered_output, end="", flush=True)
        return process.returncode
    finally:
        try:
            terminate_process_tree(process)
        finally:
            if output is not None:
                output.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite",
        nargs="*",
        help="stable suite IDs; defaults to deterministic and policy suites",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--role",
        action="append",
        choices=(
            "deterministic",
            "network",
            "manual",
            "fixture",
            "packaging",
            "policy",
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="include network and manual suites, but not fixtures",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        suites = load_manifest(args.manifest)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"RUNNER ERROR: {exc}", file=sys.stderr)
        return 2

    by_id = {suite["id"]: suite for suite in suites}
    unknown = set(args.suite) - by_id.keys()
    if unknown:
        print(f"RUNNER ERROR: unknown suite IDs: {sorted(unknown)}", file=sys.stderr)
        return 2
    if args.suite:
        selected = [by_id[suite_id] for suite_id in args.suite]
    else:
        all_roles = {suite["role"] for suite in suites} - {"fixture"}
        roles = set(args.role or (all_roles if args.all else DEFAULT_ROLES))
        platform = "windows" if os.name == "nt" else "linux"
        selected = [
            suite
            for suite in suites
            if suite["role"] in roles and platform in suite["platforms"]
        ]

    failures = 0
    try:
        for suite in selected:
            code = run_suite(suite)
            if code:
                failures += 1
                print(
                    f"RUNNER FAIL: {suite['id']} exited {code}",
                    file=sys.stderr,
                    flush=True,
                )
    except KeyboardInterrupt:
        return 130
    passed = len(selected) - failures
    print(f"\nRUNNER RESULT: {passed} passed, {failures} failed", flush=True)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
