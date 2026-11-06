"""Reporting and subprocess helpers shared by direct-run test scripts."""

from __future__ import annotations

import codecs
import io
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

ACCEPTANCE_PROVIDER_READY = "[provider] READY"


T = TypeVar("T")


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float,
    interval: float = 0.01,
    pump: Callable[[], object] | None = None,
    check_boundary: bool = True,
) -> bool:
    """Poll until true with explicit timing and optional event-loop semantics."""
    if timeout < 0 or interval <= 0:
        raise ValueError("timeout must be non-negative and interval must be positive")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pump is not None:
            pump()
        if predicate():
            return True
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    if check_boundary:
        if pump is not None:
            pump()
        return bool(predicate())
    return False


@dataclass(frozen=True)
class RecordedEvent(Generic[T]):
    sequence: int
    payload: T


class CallbackRecorder(Generic[T]):
    """Copy callback payloads and support bounded waits after causal cursors."""

    def __init__(self, copy_payload: Callable[[object], T]) -> None:
        self._copy_payload = copy_payload
        self._events: list[RecordedEvent[T]] = []
        self._condition = threading.Condition()

    def __call__(self, value: object) -> None:
        payload = self._copy_payload(value)
        with self._condition:
            event = RecordedEvent(len(self._events) + 1, payload)
            self._events.append(event)
            self._condition.notify_all()

    def cursor(self) -> int:
        with self._condition:
            return len(self._events)

    def events_after(self, cursor: int) -> tuple[RecordedEvent[T], ...]:
        with self._condition:
            return tuple(event for event in self._events if event.sequence > cursor)

    def wait_for(
        self,
        predicate: Callable[[T], bool],
        *,
        after: int,
        timeout: float,
    ) -> RecordedEvent[T] | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                match = next(
                    (
                        event
                        for event in self._events
                        if event.sequence > after and predicate(event.payload)
                    ),
                    None,
                )
                if match is not None:
                    return match
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def history(self) -> tuple[RecordedEvent[T], ...]:
        with self._condition:
            return tuple(self._events)


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

    def __init__(self, process: subprocess.Popen[str], *, echo: bool = True) -> None:
        if process.stdout is None:
            raise ValueError("process stdout must be piped")
        self._stream = process.stdout
        os.set_blocking(self._stream.fileno(), False)
        self._echo = echo
        self._pending: queue.Queue[str | None] = queue.Queue()
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._pump,
            name="provider-output-reader",
            daemon=True,
        )
        self._thread.start()

    def _pump(self) -> None:
        decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder(self._stream.encoding)(self._stream.errors),
            translate=True,
        )
        partial = ""
        try:
            while not self._stop.is_set():
                try:
                    chunk = os.read(self._stream.fileno(), 65536)
                except BlockingIOError:
                    self._stop.wait(0.01)
                    continue
                text = decoder.decode(chunk, final=not chunk)
                with self._lock:
                    self._lines.append(text)
                lines = (partial + text).split("\n")
                partial = lines.pop()
                for line in lines:
                    self._publish(line + "\n")
                if not chunk:
                    break
            if partial:
                self._publish(partial)
        except Exception as exc:
            self._error = exc
        finally:
            self._stream.close()
            self._pending.put(None)

    def _publish(self, line: str) -> None:
        if self._echo:
            print(f"    {line.rstrip()}", flush=True)
        self._pending.put(line)

    @property
    def buffered_output(self) -> str:
        with self._lock:
            return "".join(self._lines)

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def next_line(self, timeout: float) -> str | None:
        return self._pending.get(timeout=timeout)

    def join(self, timeout: float = 2) -> bool:
        self._thread.join(timeout=timeout)
        if self._error is not None:
            raise self._error
        return not self.is_alive

    def close(self) -> None:
        """Cancel a stalled inherited pipe without blocking on a buffered read lock."""
        self._stop.set()
        self._thread.join(timeout=1)


def wait_for_output_line(output: ProcessOutput, expected: str, timeout: float) -> bool:
    """Wait for an exact stripped subprocess output line."""
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
        if line.strip() == expected:
            return True


def wait_for_ready(output: ProcessOutput, timeout: float) -> bool:
    """Wait at most timeout seconds for the acceptance provider's readiness marker."""
    return wait_for_output_line(output, ACCEPTANCE_PROVIDER_READY, timeout)


def stop_process(process: subprocess.Popen[str], output: ProcessOutput) -> None:
    """Stop and reap a subprocess, then join its output reader."""
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        output.join(timeout=2)
    finally:
        output.close()
