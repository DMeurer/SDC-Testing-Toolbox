"""Deterministic offscreen checks for consumer GUI background-work lifetime."""

from __future__ import annotations

import os
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sdc11073.xml_types import msg_types  # noqa: E402

from sdctoolbox.consumer_service import DiscoveredDevice  # noqa: E402
from sdctoolbox.gui import consumer_pane as consumer_module  # noqa: E402
from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.model import MetricKind, RemoteMetric  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402


class FakeBridge(QObject):
    metrics_changed = Signal(dict)
    descriptors_added = Signal(dict)
    descriptors_deleted = Signal(dict)
    descriptors_updated = Signal(dict)
    operations_changed = Signal(dict)
    alerts_changed = Signal(dict)
    contexts_changed = Signal(dict)
    waveforms_changed = Signal(dict)
    peer_restarted = Signal()

    def __init__(self, _mdib, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)


class BlockingCall:
    def __init__(self, result=None) -> None:  # noqa: ANN001
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = result

    def __call__(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN204
        self.entered.set()
        self.release.wait(5.0)
        return self.result


class FakeRemote:
    def __init__(self, epr: str) -> None:
        self.epr = epr
        self.mdib = SimpleNamespace(entities={})
        self.close_count = 0
        self.set_call = BlockingCall(msg_types.InvocationState.FINISHED)
        self.action_call = BlockingCall(msg_types.InvocationState.FINISHED)
        self.metric_values: dict[str, RemoteMetric] = {}
        self.metrics_count = 0

    def close(self) -> None:
        self.close_count += 1

    def set_value(self, *_args) -> object:
        return self.set_call()

    def run_action(self, *_args) -> object:
        return self.action_call()

    def metrics(self) -> dict[str, RemoteMetric]:
        self.metrics_count += 1
        return dict(self.metric_values)

    @staticmethod
    def actions() -> dict:
        return {}

    @staticmethod
    def alerts() -> dict:
        return {}

    @staticmethod
    def patient_contexts() -> dict:
        return {}


class FakeConsumerService:
    def __init__(self, **_kwargs) -> None:
        self.scan_call = BlockingCall([])
        self.connect_call = BlockingCall()
        self.stop_count = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.stop_count += 1

    def scan(self, **kwargs):  # noqa: ANN003, ANN201
        return self.scan_call(**kwargs)

    def connect(self, device):  # noqa: ANN001, ANN201
        return self.connect_call(device)


def pump(app: QApplication, seconds: float = 0.05) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_for(app: QApplication, predicate, timeout: float = 2.0) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


def check(condition: bool, message: str) -> None:  # noqa: FBT001
    if not condition:
        raise AssertionError(message)
    print(f"  PASS  {message}")


def new_window(provider: ProviderService) -> tuple[MainWindow, consumer_module.ConsumerPane, FakeConsumerService]:
    window = MainWindow(provider)
    window.show()
    pane = window.network_pane
    return window, pane, pane.service


def attach(pane: consumer_module.ConsumerPane, remote: FakeRemote) -> None:
    pane._on_connected(remote, pane._generation)  # noqa: SLF001


def close_during_scan(app: QApplication, provider: ProviderService) -> None:
    window, pane, service = new_window(provider)
    pane._on_scan()  # noqa: SLF001
    check(service.scan_call.entered.wait(1.0), "scan starts in the worker")
    before = pane.status_label.text()
    window.close()
    check(service.stop_count == 0, "discovery remains open while scan uses it")
    service.scan_call.release.set()
    check(wait_for(app, lambda: service.stop_count == 1), "discovery closes once after scan")
    pump(app)
    check(pane.status_label.text() == before, "scan completion does not mutate a closed window")


def close_during_connect(app: QApplication, provider: ProviderService) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("late-connect")
    service.connect_call.result = remote
    pane.devices = [DiscoveredDevice("peer", (), (), service=object())]
    pane.device_list.addItem("peer")
    pane.device_list.setCurrentRow(0)
    pane._on_connect()  # noqa: SLF001
    check(service.connect_call.entered.wait(1.0), "connect starts in the worker")
    before = pane.status_label.text()
    window.close()
    check(service.stop_count == 0, "discovery remains open while connect uses it")
    service.connect_call.release.set()
    check(
        wait_for(app, lambda: remote.close_count == 1 and service.stop_count == 1),
        "late connection and discovery close once",
    )
    pump(app)
    check(
        pane.remote is None and pane.status_label.text() == before,
        "connect completion does not mutate a closed window",
    )


def close_with_queued_connection(app: QApplication, provider: ProviderService) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("queued-connect")
    service.connect_call.result = remote
    service.connect_call.release.set()
    pane.devices = [DiscoveredDevice("peer", (), (), service=object())]
    pane.device_list.addItem("peer")
    pane.device_list.setCurrentRow(0)
    pane._on_connect()  # noqa: SLF001
    check(service.connect_call.entered.wait(1.0), "connect result is queued for the GUI")
    time.sleep(0.02)
    window.close()
    check(remote.close_count == 1, "queued connection closes once during window close")
    check(service.stop_count == 1, "queued connection does not delay discovery close")
    pump(app)
    check(pane.remote is None, "queued connection cannot attach after window close")


def close_during_invocation(app: QApplication, provider: ProviderService, *, action: bool) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("action" if action else "set")
    attach(pane, remote)
    if action:
        pane._on_run_action("action")  # noqa: SLF001
        call = remote.action_call
        name = "action"
    else:
        pane._on_widget_value_requested("metric", 4)  # noqa: SLF001
        call = remote.set_call
        name = "set"
    check(call.entered.wait(1.0), f"{name} starts in the worker")
    before = pane.invocation_label.text()
    window.close()
    check(remote.close_count == 0, f"remote remains open while {name} uses it")
    check(service.stop_count == 1, f"discovery closes once during {name}")
    call.release.set()
    check(wait_for(app, lambda: remote.close_count == 1), f"remote closes once after {name}")
    pump(app)
    check(pane.invocation_label.text() == before, f"{name} completion does not mutate a closed window")


def stale_set_after_reconnect(app: QApplication, provider: ProviderService) -> None:
    window, pane, service = new_window(provider)
    old = FakeRemote("old")
    new = FakeRemote("new")
    attach(pane, old)
    pane._on_widget_value_requested("metric", 7)  # noqa: SLF001
    check(old.set_call.entered.wait(1.0), "old-session set starts")

    pane._on_disconnect()  # noqa: SLF001
    pane.devices = [DiscoveredDevice("new", (), (), service=object())]
    pane.device_list.addItem("new")
    pane.device_list.setCurrentRow(0)
    service.connect_call.result = new
    service.connect_call.release.set()
    pane._on_connect()  # noqa: SLF001
    check(wait_for(app, lambda: pane.remote is new), "pane reconnects while old set is pending")
    before = pane.invocation_label.text()
    old.set_call.release.set()
    check(wait_for(app, lambda: old.close_count == 1), "old remote closes once after its set returns")
    pump(app)
    check(pane.remote is new, "old set completion cannot replace the new session")
    check(pane.invocation_label.text() == before, "old set completion cannot mutate the new session UI")
    window.close()
    check(new.close_count == 1 and service.stop_count == 1, "new remote and discovery close once")


def waveform_reports_are_scoped(app: QApplication, provider: ProviderService) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("samples")
    distribution = [Decimal("10"), Decimal("30"), Decimal("20")]
    remote.metric_values = {
        "distribution": RemoteMetric(
            handle="distribution",
            node_type_name="DistributionSampleArrayMetricDescriptor",
            kind=MetricKind.DISTRIBUTION,
            samples=tuple(distribution),
        ),
        "waveform": RemoteMetric(
            handle="waveform",
            node_type_name="RealTimeSampleArrayMetricDescriptor",
            kind=MetricKind.WAVEFORM,
        ),
    }
    attach(pane, remote)
    pane.set_use_widgets(True)
    distribution_plot = pane.board.card("distribution").control.plot
    waveform_plot = pane.board.card("waveform").control.plot
    snapshots_before = remote.metrics_count
    emitted: list[Decimal] = []

    timer = QTimer(window)
    timer.setInterval(15)

    def emit_waveform() -> None:
        start = len(emitted)
        block = [Decimal(start), Decimal(start + 1), Decimal(start + 2)]
        emitted.extend(block)
        pane.bridge.waveforms_changed.emit({"waveform": block})

    timer.timeout.connect(emit_waveform)
    timer.start()
    settled = wait_for(
        app,
        lambda: len(emitted) >= 30
        and distribution_plot.samples == [float(value) for value in distribution]
        and not distribution_plot._timer.isActive(),  # noqa: SLF001
        timeout=0.8,
    )
    timer.stop()
    app.processEvents()

    check(settled, "waveform reports do not keep an unchanged distribution moving")
    check(remote.metrics_count == snapshots_before, "waveform reports do not reread the MDIB")
    check(waveform_plot.samples == [float(value) for value in emitted], "waveform blocks remain continuous")
    waveform_row = next(
        row
        for row in range(pane.table.rowCount())
        if pane.table.item(row, consumer_module.COL_HANDLE).text() == "waveform"
    )
    check(
        pane.table.item(waveform_row, consumer_module.COL_VALUE).text() == "3 sample(s)",
        "the waveform table summary follows the copied report block",
    )
    window.close()
    check(remote.close_count == 1 and service.stop_count == 1, "sample session resources close once")


def main() -> int:
    app = QApplication(sys.argv)
    provider = ProviderService(instance_name="consumer-lifecycle")
    provider.start()
    old_service = consumer_module.ConsumerService
    old_bridge = consumer_module.MdibBridge
    consumer_module.ConsumerService = FakeConsumerService
    consumer_module.MdibBridge = FakeBridge
    try:
        print("Consumer lifecycle test (offscreen)")
        close_during_scan(app, provider)
        close_during_connect(app, provider)
        close_with_queued_connection(app, provider)
        close_during_invocation(app, provider, action=False)
        close_during_invocation(app, provider, action=True)
        stale_set_after_reconnect(app, provider)
        waveform_reports_are_scoped(app, provider)
    finally:
        consumer_module.ConsumerService = old_service
        consumer_module.MdibBridge = old_bridge
        provider.stop()
    print("RESULT: all consumer lifecycle checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
