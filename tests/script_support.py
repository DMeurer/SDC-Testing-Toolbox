"""Reporting and subprocess helpers shared by direct-run test scripts."""

from __future__ import annotations

import queue
import subprocess
import threading
import time


class Report:
    """Collect pass/fail results and print them as they happen."""

    def __init__(self) -> None:
        self.checks = 0
        self.failures = 0

    def check(self, condition: bool, description: str, detail: str = "") -> bool:
        self.checks += 1
        if not condition:
            self.failures += 1
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {'PASS' if condition else 'FAIL'}  {description}{suffix}", flush=True)
        return condition

    def require(self, condition: bool, description: str, detail: str = "") -> None:
        """Report one check and stop immediately if it fails."""
        if not self.check(condition, description, detail):
            raise AssertionError(description)

    def summary(self) -> int:
        print("-" * 74)
        if self.failures:
            print(f"RESULT: {self.failures} of {self.checks} checks FAILED")
            return 1
        print(f"RESULT: all {self.checks} checks passed")
        return 0


class ProcessOutput:
    """Drain, echo, and retain a subprocess's combined output."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            raise ValueError("process stdout must be piped")
        self._stream = process.stdout
        self._pending: queue.Queue[str | None] = queue.Queue()
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, name="provider-output-reader")
        self._thread.start()

    def _pump(self) -> None:
        try:
            for line in self._stream:
                with self._lock:
                    self._lines.append(line)
                print(f"    {line.rstrip()}", flush=True)
                self._pending.put(line)
        finally:
            self._pending.put(None)

    @property
    def buffered_output(self) -> str:
        with self._lock:
            return "".join(self._lines)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def next_line(self, timeout: float) -> str | None:
        return self._pending.get(timeout=timeout)

    def join(self) -> None:
        self._thread.join()


def wait_for_ready(output: ProcessOutput, timeout: float) -> bool:
    """Wait at most timeout seconds for a READY line from a process."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            line = output.next_line(timeout=remaining)
        except queue.Empty:
            return False
        if line is None:
            return False
        if "READY" in line:
            return True


def stop_process(process: subprocess.Popen[str], output: ProcessOutput) -> None:
    """Stop and reap a subprocess, then join its output reader."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    output.join()
