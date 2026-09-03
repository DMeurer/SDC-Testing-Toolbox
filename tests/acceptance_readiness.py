"""Deterministic timeout and cleanup checks for provider startup output."""

from __future__ import annotations

import subprocess
import sys
import time

from script_support import (
    ACCEPTANCE_PROVIDER_READY,
    ProcessOutput,
    Report,
    stop_process,
    wait_for_ready,
)

TIMEOUT = 0.3


def start_child(code: str) -> tuple[subprocess.Popen[str], ProcessOutput]:
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    return process, ProcessOutput(process)


def check_deadline(
    report: Report,
    description: str,
    output_code: str,
    *,
    expect_output: bool = False,
) -> tuple[str, bool, bool]:
    process, output = start_child(output_code)
    try:
        if expect_output:
            startup_deadline = time.monotonic() + 5.0
            while not output.buffered_output and time.monotonic() < startup_deadline:
                time.sleep(0.01)
            report.check(bool(output.buffered_output), f"{description} emits startup output")

        started = time.monotonic()
        ready = wait_for_ready(output, timeout=TIMEOUT)
        elapsed = time.monotonic() - started
        report.check(not ready, f"{description} does not report readiness")
        report.check(
            TIMEOUT - 0.03 <= elapsed <= TIMEOUT + 1.0,
            f"{description} observes the requested timeout",
            f"requested {TIMEOUT:.2f}s, elapsed {elapsed:.3f}s",
        )
    finally:
        stop_process(process, output)

    return output.buffered_output, process.poll() is not None, output.is_alive


def check_ready(
    report: Report,
    description: str,
    output_code: str,
) -> tuple[str, bool, bool]:
    process, output = start_child(output_code)
    try:
        ready = wait_for_ready(output, timeout=TIMEOUT)
        report.check(ready, f"{description} reports readiness")
    finally:
        stop_process(process, output)

    return output.buffered_output, process.poll() is not None, output.is_alive


def main() -> int:
    report = Report()
    print("Acceptance provider readiness timeout")

    buffered, stopped, reader_alive = check_deadline(
        report,
        "a silent child",
        "import time; time.sleep(30)",
    )
    report.check(buffered == "", "the silent child has no buffered output", repr(buffered))
    report.check(stopped and not reader_alive, "the silent child and reader are cleaned up")

    expected = "first diagnostic\nsecond diagnostic\n"
    buffered, stopped, reader_alive = check_deadline(
        report,
        "an output-then-silent child",
        (
            "import time; "
            "print('first diagnostic', flush=True); "
            "print('second diagnostic', flush=True); "
            "time.sleep(30)"
        ),
        expect_output=True,
    )
    report.check(buffered == expected, "all child output is retained", repr(buffered))
    report.check(stopped and not reader_alive, "the output child and reader are cleaned up")

    misleading_lines = [
        "NOT READY",
        f"{ACCEPTANCE_PROVIDER_READY} check failed",
        f"prefix {ACCEPTANCE_PROVIDER_READY}",
        f"{ACCEPTANCE_PROVIDER_READY} suffix",
        ACCEPTANCE_PROVIDER_READY.lower(),
        ACCEPTANCE_PROVIDER_READY.upper(),
    ]
    misleading_output = "".join(f"{line}\n" for line in misleading_lines)
    buffered, stopped, reader_alive = check_deadline(
        report,
        "a misleading-output-then-silent child",
        (
            "import sys, time; "
            f"sys.stdout.write({misleading_output!r}); "
            "sys.stdout.flush(); "
            "time.sleep(30)"
        ),
        expect_output=True,
    )
    report.check(
        buffered == misleading_output,
        "all rejected readiness-like output is retained",
        repr(buffered),
    )
    report.check(
        stopped and not reader_alive,
        "the misleading child and reader are cleaned up",
    )

    ready_output = f"NOT READY\n\t {ACCEPTANCE_PROVIDER_READY} \t\n"
    buffered, stopped, reader_alive = check_ready(
        report,
        "a misleading-then-exact child",
        (
            "import sys, time; "
            f"sys.stdout.write({ready_output!r}); "
            "sys.stdout.flush(); "
            "time.sleep(30)"
        ),
    )
    report.check(
        buffered == ready_output,
        "misleading and normalized readiness output is retained",
        repr(buffered),
    )
    report.check(
        stopped and not reader_alive,
        "the ready child and reader are cleaned up",
    )

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
