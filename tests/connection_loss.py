"""Network acceptance: the network pane notices a provider that went away.

Two ways a provider disappears: a clean shutdown (Bye and SubscriptionEnd arrive) and a
killed process (nothing arrives; the next operation finds it gone). In both the pane must
say so, drop the peer's views, and keep tracebacks off the console.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from PySide6.QtWidgets import QApplication  # noqa: E402
from script_support import ACCEPTANCE_PROVIDER_READY, Report, wait_until  # noqa: E402

from sdctoolbox.constants import epr_for  # noqa: E402
from sdctoolbox.gui.consumer_pane import ConsumerPane  # noqa: E402
from sdctoolbox.model import MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

KILLED_INSTANCE = "connection-loss-killed"


class TracebackCollector(logging.Handler):
    """Every log record that carries a traceback, from any logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.exc_info:
            self.records.append(record)


def connect(app: QApplication, pane: ConsumerPane, epr: str) -> bool:
    def wait_for(predicate, timeout: float) -> bool:  # noqa: ANN001
        return wait_until(predicate, timeout=timeout, interval=0.01, pump=app.processEvents, check_boundary=False)

    if not wait_for(lambda: any(device.epr == epr for device in pane.devices), 20.0):
        return False
    pane.device_list.setCurrentRow(next(row for row, device in enumerate(pane.devices) if device.epr == epr))
    pane._on_connect()  # noqa: SLF001 - driving the button's slot
    return wait_for(lambda: pane.remote is not None, 30.0)


def clean_shutdown(app: QApplication, report: Report, pane: ConsumerPane) -> None:
    print("Clean provider shutdown")
    provider = ProviderService(instance_name="connection-loss-clean")
    provider.start()
    try:
        provider.add_metric(
            MetricSpec(label="Setting", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal(5)),
        )
        if not report.check(connect(app, pane, provider.epr.urn), "connects to the provider"):
            return
        report.check(bool(pane.board.handles), "the provider's widgets are shown")
    finally:
        stopped = time.monotonic()
        provider.stop()
    lost = wait_until(lambda: pane.remote is None, timeout=10.0, interval=0.01, pump=app.processEvents)
    report.check(lost, "a clean shutdown is noticed without any user action")
    print(f"    (noticed after {time.monotonic() - stopped:.2f}s)")
    report.check(pane.status_label.text().startswith("Connection lost:"), "the status says the connection was lost")
    report.check(not pane.board.handles and pane.tree.topLevelItemCount() == 0, "the provider's views are removed")


def killed_process(app: QApplication, report: Report, pane: ConsumerPane) -> None:
    print("Killed provider process")
    peer = subprocess.Popen(  # noqa: S603
        [sys.executable, str(TESTS / "acceptance_provider.py"), "--seconds", "120", "--instance", KILLED_INSTANCE],
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60.0
        ready = False
        while time.monotonic() < deadline and peer.poll() is None:
            if ACCEPTANCE_PROVIDER_READY in peer.stdout.readline():
                ready = True
                break
        if not report.check(ready, "the provider process reports ready"):
            return
        if not report.check(connect(app, pane, epr_for(KILLED_INSTANCE).urn), "connects to the provider process"):
            return
        remote = pane.remote
        handle, metric = next(
            (
                (handle, metric)
                for handle, metric in remote.metrics().items()
                if metric.kind is MetricKind.NUMBER and metric.controllable_now and not metric.is_sample_array
            ),
            (None, None),
        )
        if not report.check(handle is not None, "the provider offers a controllable number"):
            return
    finally:
        peer.kill()
        peer.wait()
    report.check(pane.remote is not None, "a killed provider leaves no immediate trace (no Bye, no SubscriptionEnd)")

    pane.board.card(handle).control.request((metric.value or Decimal(0)) + 1)
    killed = time.monotonic()
    lost = wait_until(lambda: pane.remote is None, timeout=20.0, interval=0.01, pump=app.processEvents)
    report.check(lost, "the next set finds the provider gone and disconnects")
    print(f"    (noticed after {time.monotonic() - killed:.2f}s)")
    report.check(
        pane.status_label.text().startswith("Connection lost: the device could not be reached"),
        "the status says the device could not be reached",
    )
    report.check(not pane.board.handles and pane.tree.topLevelItemCount() == 0, "the provider's views are removed")


def main() -> int:
    app = QApplication(sys.argv)
    report = Report()
    collector = TracebackCollector()
    logging.getLogger().addHandler(collector)
    pane = ConsumerPane("127.0.0.1")
    try:
        clean_shutdown(app, report, pane)
        killed_process(app, report, pane)
    finally:
        pane.shutdown()
        logging.getLogger().removeHandler(collector)
    report.check(
        not collector.records,
        "no tracebacks were logged",
        "; ".join(f"{record.name}: {record.getMessage()}" for record in collector.records),
    )
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
