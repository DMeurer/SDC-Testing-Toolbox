"""Focused checks for shared test helpers, suite metadata, and subprocess runner."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))

import run_suites  # noqa: E402
from script_support import CallbackRecorder, Report, wait_until  # noqa: E402


def polling_checks(report: Report) -> None:
    calls: list[str] = []

    def boundary_predicate() -> bool:
        calls.append("predicate")
        return len(calls) == 1

    report.check(
        wait_until(boundary_predicate, timeout=0, check_boundary=True),
        "boundary checking evaluates a predicate when the timeout is already reached",
    )
    calls.clear()
    report.check(
        not wait_until(boundary_predicate, timeout=0, check_boundary=False)
        and calls == [],
        "boundary checking can be explicitly disabled",
    )

    pump_calls: list[str] = []
    report.check(
        wait_until(
            lambda: len(pump_calls) == 1,
            timeout=0.1,
            pump=lambda: pump_calls.append("pump"),
        ),
        "an event pump runs before each predicate check",
    )

    sentinel = RuntimeError("predicate failed")
    try:
        wait_until(lambda: (_ for _ in ()).throw(sentinel), timeout=0.1)
    except RuntimeError as exc:
        propagated = exc is sentinel
    else:
        propagated = False
    report.check(propagated, "predicate exceptions propagate unchanged")


def recorder_checks(report: Report) -> None:
    source = {"metric": [1]}
    recorder = CallbackRecorder(
        lambda value: {key: tuple(items) for key, items in dict(value).items()},
    )
    cursor = recorder.cursor()
    recorder(source)
    source["metric"].append(2)
    event = recorder.wait_for(
        lambda payload: payload["metric"] == (1,),
        after=cursor,
        timeout=0,
    )
    report.check(
        event is not None and event.sequence == 1 and event.payload == {"metric": (1,)},
        "recorders retain copied payloads with monotonic sequence numbers",
    )
    report.check(
        recorder.wait_for(
            lambda _payload: True,
            after=event.sequence,
            timeout=0.02,
        )
        is None,
        "a baseline cursor excludes earlier callback delivery",
    )

    callback_returned = threading.Event()

    def invoke() -> None:
        recorder({"other": [3]})
        callback_returned.set()

    thread = threading.Thread(target=invoke)
    thread.start()
    thread.join(timeout=0.2)
    report.check(
        callback_returned.is_set(),
        "recorder callbacks copy, signal, and return promptly",
    )

    for report_first in (True, False):
        ordered = CallbackRecorder(lambda value: str(value))
        baseline = ordered.cursor()
        invocation_complete = threading.Event()

        if report_first:
            ordered("delivered")
            invocation_complete.set()
        else:
            invocation_complete.set()
            ordered("delivered")
        delivered = ordered.wait_for(
            lambda payload: payload == "delivered",
            after=baseline,
            timeout=0,
        )
        report.check(
            invocation_complete.is_set() and delivered is not None,
            "invocation completion and report delivery remain independent when the "
            f"report arrives {'first' if report_first else 'last'}",
        )


def manifest_checks(report: Report) -> None:
    suites = run_suites.load_manifest(TESTS / "suites.json")
    by_id = {suite["id"]: suite for suite in suites}
    report.check(
        len(by_id) == len(suites)
        and all(run_suites.REQUIRED_FIELDS <= suite.keys() for suite in suites),
        "the suite manifest has stable unique IDs and all isolation metadata",
    )
    report.check(
        all((ROOT / suite["argv"][0]).is_file() for suite in suites),
        "every manifest entry launches an existing direct-run script",
    )
    report.check(
        by_id["network-acceptance"]["resources"] == ["ws-discovery", "acceptance-epr"]
        and by_id["acceptance-provider"]["role"] == "fixture"
        and by_id["gui-smoke"]["role"] == "manual",
        "network locks, fixtures, and manual suites remain explicitly classified",
    )

    workflow = (ROOT / ".github" / "workflows" / "build-app.yml").read_text(
        encoding="utf-8",
    )
    workflow_scripts = {
        line.split("script:", 1)[1].strip()
        for line in workflow.splitlines()
        if "script: tests/" in line or "script: diagnostics/" in line
    }
    workflow_scripts.add("tests/acceptance_core.py")
    manifested_ci_scripts = {
        suite["argv"][0]
        for suite in suites
        if suite["events"] and suite["role"] not in {"manual", "fixture"}
    }
    expected_additions = {"tests/test_infrastructure.py"}
    report.check(
        manifested_ci_scripts == workflow_scripts | expected_additions,
        "manifest CI inventory matches the legacy workflow plus the new harness test",
        f"manifest-only {sorted(manifested_ci_scripts - workflow_scripts)}, "
        f"workflow-only {sorted(workflow_scripts - manifested_ci_scripts)}",
    )


def runner_checks(report: Report) -> None:
    with TemporaryDirectory(prefix="sdctoolbox-runner-") as raw_temporary:
        temporary = Path(raw_temporary)
        noisy = temporary / "noisy.py"
        noisy.write_text(
            "import os\n"
            "print('NOISY-' + 'x' * 20000)\n"
            "print('CWD=' + os.getcwd())\n"
            "print('ENV=' + os.environ['RUNNER_TEST'])\n",
            encoding="utf-8",
        )
        suite = {
            "id": "noisy",
            "argv": [str(noisy)],
            "cwd": ".",
            "timeout_seconds": 5,
            "environment": {"RUNNER_TEST": "set"},
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run_suites.run_suite(suite, root=temporary)
        report.check(
            code == 0
            and "NOISY-" in output.getvalue()
            and len(output.getvalue()) > 20000
            and f"CWD={temporary}" in output.getvalue()
            and "ENV=set" in output.getvalue(),
            "runner applies cwd/env and drains noisy output without importing suites",
        )

        legacy = temporary / "legacy.py"
        legacy.write_text(
            "print('  PASS  legacy marker')\n"
            "print('RESULT: all 1 checks passed')\n",
            encoding="utf-8",
        )
        direct = subprocess.run(
            [sys.executable, str(legacy)],
            cwd=temporary,
            capture_output=True,
            text=True,
            check=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            driven_code = run_suites.run_suite(
                {
                    "id": "legacy",
                    "argv": [str(legacy)],
                    "cwd": ".",
                    "timeout_seconds": 5,
                    "environment": {},
                },
                root=temporary,
            )
        driven_output = output.getvalue()
        report.check(
            driven_code == direct.returncode
            and "legacy marker" in direct.stdout
            and "legacy marker" in driven_output
            and direct.stdout.count("  PASS  ") == driven_output.count("  PASS  ")
            and "RESULT: all 1 checks passed" in driven_output,
            "manifest execution preserves direct exit code, markers, and check total",
        )

        missing_root = temporary / "missing-root"
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_suites.run_suite(suite, root=missing_root)
        report.check(code == 1, "runner reports subprocess spawn failures")

        marker = temporary / "nested-child-survived"
        nested = temporary / "nested.py"
        nested_child = (
            "import time; time.sleep(1); "
            f"open({str(marker)!r}, 'w').write('alive')"
        )
        nested.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {nested_child!r}])\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        timed = {
            "id": "timed",
            "argv": [str(nested)],
            "cwd": ".",
            "timeout_seconds": 0.2,
            "environment": {},
        }
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO(),
        ):
            code = run_suites.run_suite(timed, root=temporary)
        report.check(code == 124, "runner returns a distinct timeout status")
        report.check(
            not wait_until(marker.exists, timeout=1.5, interval=0.02),
            "timeout terminates nested children before they can outlive the suite",
        )

    fake_process = MagicMock()
    fake_process.communicate.side_effect = KeyboardInterrupt
    with (
        patch.object(run_suites.subprocess, "Popen", return_value=fake_process),
        patch.object(run_suites, "terminate_process_tree") as terminate,
    ):
        interrupted = False
        try:
            run_suites.run_suite(
                {
                    "id": "interrupt",
                    "argv": ["ignored.py"],
                    "cwd": ".",
                    "timeout_seconds": 1,
                    "environment": {},
                },
            )
        except KeyboardInterrupt:
            interrupted = True
    report.check(
        interrupted and terminate.call_args.args == (fake_process,),
        "runner terminates the process tree and propagates interruption",
    )


def main() -> int:
    report = Report()
    print("Test infrastructure")
    polling_checks(report)
    recorder_checks(report)
    manifest_checks(report)
    runner_checks(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
