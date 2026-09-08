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
    wait_until,
    wait_for_ready,
)

SHORT_TIMEOUT = 0.3
POSITIVE_STARTUP_TIMEOUT = 4.0
POSITIVE_STARTUP_RUNS = 8


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
            wait_until(
                lambda: bool(output.buffered_output),
                timeout=POSITIVE_STARTUP_TIMEOUT,
                interval=0.01,
            )
            report.check(bool(output.buffered_output), f"{description} emits startup output")

        started = time.monotonic()
        ready = wait_for_ready(output, timeout=SHORT_TIMEOUT)
        elapsed = time.monotonic() - started
        report.check(not ready, f"{description} does not report readiness")
        report.check(
            SHORT_TIMEOUT - 0.03 <= elapsed <= SHORT_TIMEOUT + 1.0,
            f"{description} observes the requested timeout",
            f"requested {SHORT_TIMEOUT:.2f}s, elapsed {elapsed:.3f}s",
        )
    finally:
        stop_process(process, output)

    return output.buffered_output, process.poll() is not None, output.is_alive


def check_ready(
    output_code: str,
) -> tuple[bool, str, bool, bool]:
    process, output = start_child(output_code)
    try:
        ready = wait_for_ready(output, timeout=POSITIVE_STARTUP_TIMEOUT)
    finally:
        stop_process(process, output)

    return ready, output.buffered_output, process.poll() is not None, output.is_alive


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

    exact_output = f"{ACCEPTANCE_PROVIDER_READY}\n"
    startup_results = [
        check_ready(f"print({ACCEPTANCE_PROVIDER_READY!r}, flush=True)")
        for _ in range(POSITIVE_STARTUP_RUNS)
    ]
    report.check(
        all(ready for ready, _, _, _ in startup_results),
        f"all {POSITIVE_STARTUP_RUNS} exact-marker startup runs report readiness",
    )
    report.check(
        all(buffered == exact_output for _, buffered, _, _ in startup_results),
        "all exact-marker startup output is retained",
        repr([buffered for _, buffered, _, _ in startup_results]),
    )
    report.check(
        all(stopped and not reader_alive for _, _, stopped, reader_alive in startup_results),
        "all exact-marker children and readers are cleaned up",
    )

    ready_output = f"NOT READY\n\t {ACCEPTANCE_PROVIDER_READY} \t\n"
    ready, buffered, stopped, reader_alive = check_ready(
        (
            "import sys, time; "
            f"sys.stdout.write({ready_output!r}); "
            "sys.stdout.flush(); "
            "time.sleep(30)"
        ),
    )
    report.check(ready, "a misleading-then-exact child reports readiness")
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
