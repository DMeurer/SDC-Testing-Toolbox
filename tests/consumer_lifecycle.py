"""Deterministic offscreen checks for consumer GUI background-work lifetime."""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
import weakref
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QEvent,
    QObject,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import QApplication  # noqa: E402
from script_support import Report, wait_until  # noqa: E402
from sdc11073.xml_types import msg_types  # noqa: E402

from sdctoolbox.consumer_service import DiscoveredDevice  # noqa: E402
from sdctoolbox.gui import consumer_pane as consumer_module  # noqa: E402
from sdctoolbox.gui.async_call import AsyncCall  # noqa: E402
from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    MetricKind,
    RemoteAction,
    RemoteAlert,
    RemoteMetric,
    RemoteRange,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402

REPORT = Report()


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
        self.error: Exception | None = None

    def __call__(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN204
        self.entered.set()
        self.release.wait(5.0)
        if self.error is not None:
            raise self.error
        return self.result


class FakeRemote:
    def __init__(self, epr: str) -> None:
        self.epr = epr
        self.mdib = SimpleNamespace(entities={})
        self.close_count = 0
        self.set_call = BlockingCall(msg_types.InvocationState.FINISHED)
        self.action_call = BlockingCall(msg_types.InvocationState.FINISHED)
        self.metric_values: dict[str, RemoteMetric] = {}
        self.action_values: dict[str, RemoteAction] = {}
        self.alert_values: dict[str, RemoteAlert] = {}
        self.context_values: dict[str, object] = {}
        self.metrics_count = 0
        self.metrics_requests: list[frozenset[str] | None] = []

    def close(self) -> None:
        self.close_count += 1

    def set_value(self, *_args) -> object:
        return self.set_call()

    def run_action(self, *_args) -> object:
        return self.action_call()

    def metrics(self, handles: Iterable[str] | None = None) -> dict[str, RemoteMetric]:
        self.metrics_count += 1
        wanted = None if handles is None else frozenset(handles)
        self.metrics_requests.append(wanted)
        if wanted is None:
            return dict(self.metric_values)
        return {handle: metric for handle, metric in self.metric_values.items() if handle in wanted}

    def actions(self) -> dict[str, RemoteAction]:
        return dict(self.action_values)

    def alerts(self) -> dict[str, RemoteAlert]:
        return dict(self.alert_values)

    def patient_contexts(self) -> dict[str, object]:
        return dict(self.context_values)


class EqualResource:
    def __init__(self, key: str) -> None:
        self.key = key

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EqualResource):
            return NotImplemented
        return self.key == other.key


class UnhashableResource:
    __hash__ = None


class CollectableMdib:
    def __init__(self) -> None:
        self.entities: dict[str, object] = {}
        self.nested: CollectableNode | None = None


class CollectableNode:
    def __init__(self, mdib: CollectableMdib) -> None:
        self.mdib = mdib


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
    wait_until(
        lambda: False,
        timeout=seconds,
        interval=0.005,
        pump=app.processEvents,
        check_boundary=False,
    )


def wait_for(app: QApplication, predicate, timeout: float = 2.0) -> bool:  # noqa: ANN001
    return wait_until(
        predicate,
        timeout=timeout,
        interval=0.005,
        pump=app.processEvents,
        check_boundary=False,
    )


def check(condition: bool, message: str) -> None:  # noqa: FBT001
    REPORT.require(condition, message)


def new_window(provider: ProviderService) -> tuple[MainWindow, consumer_module.ConsumerPane, FakeConsumerService]:
    window = MainWindow(provider)
    window.show()
    pane = window.network_pane
    return window, pane, pane.service


def attach(pane: consumer_module.ConsumerPane, remote: FakeRemote) -> None:
    pane._on_connected(remote, pane._generation)  # noqa: SLF001


def close_idle_window(app: QApplication, provider: ProviderService) -> None:
    window, pane, service = new_window(provider)
    window.close()
    pump(app)
    check(not window.isVisible(), "a real idle window closes through its normal close event")
    check(pane.remote is None and service.stop_count == 1, "natural window close releases discovery once")


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
    # Do not pump Qt here: the test needs the queued completion to remain pending.
    time.sleep(0.02)
    window.close()
    check(remote.close_count == 1, "queued connection closes once during window close")
    check(service.stop_count == 1, "queued connection does not delay discovery close")
    pump(app)
    check(pane.remote is None, "queued connection cannot attach after window close")


def async_resources_retire_once() -> None:
    worker = AsyncCall()
    resource = EqualResource("unused")
    close_count = 0

    def close_resource() -> None:
        nonlocal close_count
        close_count += 1

    worker.retire(resource, close_resource)
    check(close_count == 1, "an unused resource closes immediately")
    check(
        not worker._resources,  # noqa: SLF001
        "an unused retired resource leaves no active bookkeeping",
    )
    worker.retire(resource, close_resource)
    worker.close()
    worker.close()
    check(
        close_count == 1,
        "repeated retirement and shutdown do not close an unused resource again",
    )


def non_weakrefable_resources_are_rejected() -> None:
    worker = AsyncCall()
    call_started = threading.Event()
    active_call = BlockingCall()
    active_resource = EqualResource("valid")
    resource_closed = threading.Event()
    close_count = 0

    def call() -> None:
        call_started.set()

    def close_resource() -> None:
        nonlocal close_count
        close_count += 1
        resource_closed.set()

    try:
        worker.start_managed("invalid", object(), call)
    except TypeError as exc:
        start_error = str(exc)
    else:
        start_error = None

    check(
        start_error == "managed resource must support weak references",
        "a non-weak-referenceable managed resource is rejected synchronously",
    )
    check(
        not call_started.is_set()
        and not worker._threads  # noqa: SLF001
        and not worker._resources,  # noqa: SLF001
        "rejected managed work starts no callable and creates no bookkeeping",
    )

    check(
        worker.start_managed("active", active_resource, active_call),
        "a weak-referenceable managed resource starts normally",
    )
    check(active_call.entered.wait(1.0), "the valid managed call becomes active")
    try:
        worker.start_managed("invalid-active", object(), call)
    except TypeError as exc:
        active_error = str(exc)
    else:
        active_error = None

    check(
        active_error == "managed resource must support weak references"
        and not call_started.is_set(),
        "a non-weak-referenceable resource is rejected before work while another call is active",
    )
    check(
        set(worker._threads) == {"active"}  # noqa: SLF001
        and set(worker._resources) == {active_resource},  # noqa: SLF001
        "active-call rejection creates no worker or resource entry",
    )

    try:
        worker.retire(object(), close_resource)
    except TypeError as exc:
        retire_error = str(exc)
    else:
        retire_error = None

    check(
        retire_error == "managed resource must support weak references",
        "a non-weak-referenceable retirement is rejected synchronously",
    )
    check(
        close_count == 0
        and not resource_closed.is_set()
        and set(worker._resources) == {active_resource}  # noqa: SLF001
        and not worker._retired_resources,  # noqa: SLF001
        "rejected retirement calls no closer and creates no additional resource entry",
    )
    worker.retire(active_resource, close_resource)
    active_call.release.set()
    check(worker.wait(1.0), "the valid managed call finishes after rejection checks")
    check(
        resource_closed.wait(1.0) and close_count == 1,
        "the valid managed resource still retires after its call",
    )


def unhashable_keys_and_resources_are_rejected() -> None:
    worker = AsyncCall()
    call_started = threading.Event()
    resource = UnhashableResource()

    try:
        worker.start_managed([], None, call_started.set)
    except TypeError as exc:
        key_error = str(exc)
    else:
        key_error = None

    check(
        key_error == "managed key must be hashable",
        "an unhashable managed key is rejected synchronously",
    )

    try:
        worker.start_managed("unhashable-resource", resource, call_started.set)
    except TypeError as exc:
        start_error = str(exc)
    else:
        start_error = None

    check(
        start_error == "managed resource must be hashable",
        "an unhashable weak-referenceable resource is rejected synchronously",
    )

    try:
        worker.retire(resource, lambda: None)
    except TypeError as exc:
        retire_error = str(exc)
    else:
        retire_error = None

    check(
        retire_error == "managed resource must be hashable",
        "retirement rejects an unhashable resource synchronously",
    )
    check(
        not call_started.is_set()
        and not worker._threads  # noqa: SLF001
        and not worker._resources  # noqa: SLF001
        and not worker._retired_resources,  # noqa: SLF001
        "hashability rejection starts no work and creates no bookkeeping",
    )


def thread_start_failure_restores_bookkeeping() -> None:
    worker = AsyncCall()
    original_start = threading.Thread.start
    new_resource = EqualResource("new")

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError("forced thread start failure")

    threading.Thread.start = fail_start
    try:
        try:
            worker.start_managed("retry-new", new_resource, lambda: None)
        except RuntimeError as exc:
            new_error = str(exc)
        else:
            new_error = None
    finally:
        threading.Thread.start = original_start

    check(
        new_error == "forced thread start failure",
        "a new-resource thread start failure is re-raised unchanged",
    )
    check(
        not worker._threads and not worker._resources,  # noqa: SLF001
        "a new-resource thread start failure restores empty bookkeeping",
    )
    retried_new = threading.Event()
    check(
        worker.start_managed("retry-new", new_resource, retried_new.set),
        "the same key and new resource can be retried after start failure",
    )
    check(
        retried_new.wait(1.0) and worker.wait(1.0),
        "the new-resource retry completes",
    )
    worker.retire(new_resource, lambda: None)

    active_call = BlockingCall()
    active_resource = EqualResource("shared")
    equal_resource = EqualResource("shared")
    check(
        worker.start_managed("active", active_resource, active_call),
        "the shared resource has a pre-existing user",
    )
    check(active_call.entered.wait(1.0), "the pre-existing resource user starts")
    active_thread = worker._threads["active"]  # noqa: SLF001
    active_use = worker._resources[active_resource]  # noqa: SLF001

    threading.Thread.start = fail_start
    try:
        try:
            worker.start_managed("retry-shared", equal_resource, lambda: None)
        except RuntimeError as exc:
            shared_error = str(exc)
        else:
            shared_error = None
    finally:
        threading.Thread.start = original_start

    check(
        shared_error == "forced thread start failure",
        "an existing-resource thread start failure is re-raised unchanged",
    )
    check(
        worker._threads == {"active": active_thread}  # noqa: SLF001
        and len(worker._resources) == 1  # noqa: SLF001
        and next(iter(worker._resources)) is active_resource  # noqa: SLF001
        and worker._resources[active_resource] is active_use  # noqa: SLF001
        and active_use.users == 1,
        "an existing equal resource retains its exact pre-call bookkeeping",
    )

    retried_shared = BlockingCall()
    check(
        worker.start_managed("retry-shared", equal_resource, retried_shared),
        "the same key and equal resource can be retried after start failure",
    )
    check(retried_shared.entered.wait(1.0), "the existing-resource retry starts")
    check(
        active_use.users == 2 and len(worker._resources) == 1,  # noqa: SLF001
        "the retry shares the existing equal resource entry",
    )
    retried_shared.release.set()
    active_call.release.set()
    check(worker.wait(1.0), "both shared-resource users finish")
    check(
        active_use.users == 0 and len(worker._resources) == 1,  # noqa: SLF001
        "completed shared-resource calls preserve idle resource bookkeeping",
    )
    worker.retire(equal_resource, lambda: None)
    check(not worker._resources, "the shared resource still retires after retry")  # noqa: SLF001


def active_equal_resource_retires_after_release() -> None:
    worker = AsyncCall()
    call = BlockingCall()
    resource = EqualResource("active")
    equal_resource = EqualResource("active")
    closed: list[EqualResource] = []

    check(
        worker.start_managed("active", resource, call),
        "a managed call starts with its resource",
    )
    check(call.entered.wait(1.0), "the managed resource is active in the worker")
    worker.retire(equal_resource, lambda: closed.append(equal_resource))
    worker.retire(resource, lambda: closed.append(resource))
    check(
        not closed and len(worker._resources) == 1,  # noqa: SLF001
        "an equal resource key stays tracked and open while its worker is active",
    )
    check(
        not worker.start_managed("late", equal_resource, lambda: None),
        "an equal key cannot start new work after retirement",
    )
    check(
        not worker.close(timeout=0.0),
        "shutdown may leave an active retired call to finish",
    )
    check(
        not worker.close(timeout=0.0),
        "repeated shutdown still waits for the active call",
    )

    call.release.set()
    check(worker.wait(1.0), "the retired managed call finishes")
    check(
        len(closed) == 1
        and closed[0] is equal_resource
        and not worker._resources,  # noqa: SLF001
        "the active resource closes once and is removed after its final release",
    )
    worker.retire(resource, lambda: closed.append(resource))
    check(closed == [equal_resource], "retiring the released identity again is a no-op")


def retired_remote_graph_is_collectable(
    app: QApplication,
    provider: ProviderService,
) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("collectable")
    mdib = CollectableMdib()
    nested = CollectableNode(mdib)
    mdib.nested = nested
    remote.mdib = mdib
    remote_ref = weakref.ref(remote)
    mdib_ref = weakref.ref(mdib)
    nested_ref = weakref.ref(nested)

    attach(pane, remote)
    pane._on_disconnect()  # noqa: SLF001
    check(remote.close_count == 1, "disconnect closes an idle remote exactly once")
    del remote, mdib, nested
    for _ in range(3):
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
        gc.collect()
    check(
        remote_ref() is None and mdib_ref() is None and nested_ref() is None,
        "retirement and queued Qt cleanup release the remote and nested MDIB graph",
    )
    window.close()
    check(service.stop_count == 1, "collectable session closes discovery once")


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


def peer_restart_requires_manual_reconnect(app: QApplication, provider: ProviderService) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("restarting")
    remote.mdib.entities = {
        "mds": SimpleNamespace(
            node_type=SimpleNamespace(localname="MdsDescriptor"),
            parent_handle=None,
        ),
    }
    remote.metric_values = {
        "metric": RemoteMetric(
            handle="metric",
            node_type_name="NumericMetricDescriptor",
            kind=MetricKind.NUMBER,
            label="Peer metric",
            value=Decimal(7),
        ),
    }
    attach(pane, remote)
    bridge = pane.bridge
    generation = pane._generation  # noqa: SLF001
    check(pane.tree.topLevelItemCount() == 1 and pane.table.rowCount() == 1, "peer state starts populated")

    bridge.peer_restarted.emit()
    pump(app)

    check(pane._generation == generation + 1, "peer restart retires the current session")  # noqa: SLF001
    check(pane.remote is None and pane.bridge is None, "peer restart disconnects instead of reloading")
    check(remote.close_count == 1, "restarted peer connection closes once")
    check(
        pane.tree.topLevelItemCount() == 0
        and pane.table.rowCount() == 0
        and not pane.board.handles,
        "peer restart clears cached views",
    )
    check(
        pane.status_label.text() == "The device restarted. Reconnect to see it again.",
        "peer restart requests a manual reconnect",
    )
    check(
        pane.scan_button.isEnabled()
        and not pane.disconnect_button.isEnabled(),
        "peer restart leaves disconnected controls available",
    )
    window.close()
    check(service.stop_count == 1, "restart session closes discovery once")


def failed_reconnect_clears_peer_ui(
    app: QApplication,
    provider: ProviderService,
) -> None:
    window, pane, service = new_window(provider)
    old = FakeRemote("populated")
    old.mdib.entities = {
        "mds": SimpleNamespace(
            node_type=SimpleNamespace(localname="MdsDescriptor"),
            parent_handle=None,
        ),
    }
    old.metric_values = {
        "metric": RemoteMetric(
            handle="metric",
            node_type_name="NumericMetricDescriptor",
            kind=MetricKind.NUMBER,
            label="Peer metric",
            value=Decimal(7),
            operation_handles=("set.metric",),
            controllable_now=True,
        ),
    }
    old.alert_values = {
        "alert": RemoteAlert(
            handle="alert",
            node_type_name="LimitAlertConditionDescriptor",
            label="Peer alert",
            present=True,
        ),
    }
    old.action_values = {
        "action": RemoteAction(
            handle="action",
            label="Peer action",
            target_handle="mds",
            enabled=True,
        ),
    }
    old.context_values = {
        "patient": SimpleNamespace(summary=lambda: "Populated Patient"),
    }
    attach(pane, old)
    pane.select_handle("metric")
    pane.invocation_label.setText("accepted")
    pump(app)

    check(pane.tree.topLevelItemCount() == 1, "old peer tree starts populated")
    check(
        pane.table.rowCount() == 1 and pane.board.handles == ["metric"],
        "old peer metrics start populated",
    )
    check(pane.alert_table.rowCount() == 1, "old peer alerts start populated")
    check(
        "Populated Patient" in pane.context_label.text(),
        "old peer context starts populated",
    )
    check(
        bool(pane.action_buttons) and pane.actions_widget.isVisible(),
        "old peer actions start populated",
    )
    check(pane.apply_button.isEnabled(), "old peer editor starts enabled")

    pane.devices = [DiscoveredDevice("failing", (), (), service=object())]
    pane.device_list.addItem("failing")
    pane.device_list.setCurrentRow(0)
    service.connect_call.error = RuntimeError("forced failure")
    generation = pane._generation  # noqa: SLF001
    pane._on_connect()  # noqa: SLF001
    check(
        service.connect_call.entered.wait(1.0),
        "replacement connection starts in the worker",
    )

    def remote_ui_is_empty() -> bool:
        return (
            pane.remote is None
            and pane.bridge is None
            and pane.tree.topLevelItemCount() == 0
            and pane.table.rowCount() == 0
            and pane.alert_table.rowCount() == 0
            and not pane.board.handles
            and not pane.context_label.text()
            and not pane.action_buttons
            and not pane.actions_widget.isVisible()
            and pane.selected_handle() is None
            and not pane.editor_stack.isEnabled()
            and not pane.apply_button.isEnabled()
            and not pane.value_edit.text()
            and not pane.value_edit.placeholderText()
            and pane.choice_box.count() == 0
            and not pane.invocation_label.text()
        )

    check(
        pane._generation == generation + 1,  # noqa: SLF001
        "replacement connection advances the session generation",
    )
    check(old.close_count == 1, "old peer is closed before replacement work continues")
    check(
        remote_ui_is_empty(),
        "old peer data is cleared while replacement connection is pending",
    )
    check(
        pane.status_label.text() == "Connecting to failing\u2026",
        "pending status names the replacement peer",
    )
    check(
        not pane.scan_button.isEnabled()
        and not pane.connect_button.isEnabled()
        and not pane.disconnect_button.isEnabled(),
        "connection controls agree while replacement connection is pending",
    )

    service.connect_call.release.set()
    check(
        wait_for(
            app,
            lambda: pane.status_label.text() == "Could not connect: forced failure",
        ),
        "replacement connection failure reaches the GUI",
    )
    check(
        remote_ui_is_empty(),
        "old peer data remains cleared after replacement connection fails",
    )
    check(
        pane.scan_button.isEnabled()
        and pane.connect_button.isEnabled()
        and not pane.disconnect_button.isEnabled(),
        "connection controls agree after replacement connection fails",
    )
    check(
        pane.editor_label.text() == "Connect to a device to control it",
        "editor caption agrees with the disconnected state",
    )
    window.close()
    check(service.stop_count == 1, "failed replacement session closes discovery once")


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


def metric_reports_are_scoped(app: QApplication, provider: ProviderService) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("metric-reports")
    initial_distribution = (Decimal("1"), Decimal("2"), Decimal("3"))
    target_distribution = (Decimal("9"), Decimal("7"), Decimal("8"))
    remote.metric_values = {
        "distribution": RemoteMetric(
            handle="distribution",
            node_type_name="DistributionSampleArrayMetricDescriptor",
            kind=MetricKind.DISTRIBUTION,
            samples=initial_distribution,
        ),
        "scalar": RemoteMetric(
            handle="scalar",
            node_type_name="NumericMetricDescriptor",
            kind=MetricKind.NUMBER,
            value=Decimal(0),
        ),
    }
    attach(pane, remote)
    pane.set_use_widgets(True)
    distribution_plot = pane.board.card("distribution").control.plot
    distribution_plot.flush()
    requests_before = len(remote.metrics_requests)

    remote.metric_values["distribution"].samples = target_distribution
    pane.bridge.metrics_changed.emit({"distribution": object()})
    tween_started = distribution_plot._tween_started  # noqa: SLF001
    scalar_updates = 0
    timer = QTimer(window)
    timer.setInterval(10)

    def emit_scalar() -> None:
        nonlocal scalar_updates
        scalar_updates += 1
        remote.metric_values["scalar"].value = Decimal(scalar_updates)
        pane.bridge.metrics_changed.emit({"scalar": object()})

    timer.timeout.connect(emit_scalar)
    timer.start()
    settled = wait_for(
        app,
        lambda: scalar_updates >= 25
        and distribution_plot.samples == [float(value) for value in target_distribution]
        and not distribution_plot._timer.isActive(),  # noqa: SLF001
        timeout=0.8,
    )
    timer.stop()
    app.processEvents()

    check(settled, "unrelated scalar reports do not keep a distribution tween alive")
    check(
        distribution_plot._tween_started == tween_started,  # noqa: SLF001
        "unrelated metric reports do not restart the distribution tween",
    )
    scalar_text = pane.board.card("scalar").control.edit.text()
    scalar_row = next(
        row
        for row in range(pane.table.rowCount())
        if pane.table.item(row, consumer_module.COL_HANDLE).text() == "scalar"
    )
    check(
        scalar_text == str(scalar_updates)
        and pane.table.item(scalar_row, consumer_module.COL_VALUE).text() == str(scalar_updates),
        "scalar reports update the affected card and table row",
    )
    requests = remote.metrics_requests[requests_before:]
    check(
        requests
        and all(request in {frozenset({"distribution"}), frozenset({"scalar"})} for request in requests),
        "metric reports request only their changed handles",
    )
    window.close()
    check(remote.close_count == 1 and service.stop_count == 1, "metric session resources close once")


def refresh_snapshot_and_editor_lookups_are_scoped(
    app: QApplication,
    provider: ProviderService,
) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("metric-snapshot")
    remote.metric_values = {
        "first": RemoteMetric(
            handle="first",
            node_type_name="NumericMetricDescriptor",
            kind=MetricKind.NUMBER,
            label="First",
            value=Decimal(1),
            operation_handles=("set.first",),
            controllable_now=True,
        ),
        "second": RemoteMetric(
            handle="second",
            node_type_name="NumericMetricDescriptor",
            kind=MetricKind.NUMBER,
            label="Second",
            value=Decimal(2),
        ),
    }

    attach(pane, remote)
    check(
        remote.metrics_requests == [None],
        "one full metric snapshot supplies the consumer table and board",
    )
    first_row = next(
        row
        for row in range(pane.table.rowCount())
        if pane.table.item(row, consumer_module.COL_HANDLE).text() == "first"
    )
    check(
        pane.table.item(first_row, consumer_module.COL_VALUE).text() == "1"
        and pane.board.card("first").control.edit.text() == "1",
        "the shared snapshot gives the table and board the same value",
    )

    pane.select_handle("first")
    check(
        remote.metrics_requests[-1] == frozenset({"first"}),
        "selection requests only the selected metric",
    )
    requests_before_apply = len(remote.metrics_requests)
    remote.set_call.release.set()
    pane.value_edit.setText("3")
    pane._on_apply()  # noqa: SLF001
    check(
        remote.metrics_requests[requests_before_apply:] == [frozenset({"first"})],
        "apply requests only the selected metric before invoking",
    )
    check(wait_for(app, lambda: not pane._invocation_busy()), "the scoped apply finishes")  # noqa: SLF001

    window.close()
    check(remote.close_count == 1 and service.stop_count == 1, "snapshot session resources close once")


def table_editor_enforces_complete_allowed_domain(
    app: QApplication,
    provider: ProviderService,
) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("allowed-domain")
    remote.metric_values = {
        "number": RemoteMetric(
            handle="number",
            node_type_name="NumericMetricDescriptor",
            kind=MetricKind.NUMBER,
            label="Disjoint number",
            value=Decimal("0"),
            minimum=Decimal("0"),
            maximum=Decimal("10"),
            operation_handles=("set.number",),
            selected_operation_handle="set.number",
            controllable_now=True,
            allowed_ranges=(
                RemoteRange(Decimal("0"), Decimal("10"), Decimal("2")),
                RemoteRange(Decimal("20"), Decimal("30"), Decimal("5")),
            ),
        ),
    }
    remote.set_call.release.set()
    attach(pane, remote)
    pane.select_handle("number")
    check(
        "0 to 10 (step 2); 20 to 30 (step 5)" in pane.editor_label.text(),
        "the table editor displays each allowed range without merging the gap",
    )

    pane.value_edit.setText("25")
    pane._on_apply()  # noqa: SLF001
    check(
        wait_for(app, lambda: pane.invocation_label.text().startswith("accepted")),
        "the second-range write reports its accepted result",
    )

    remote.set_call = BlockingCall(msg_types.InvocationState.FINISHED)
    remote.set_call.release.set()
    for text in ("15", "3"):
        pane.value_edit.setText(text)
        pane._on_apply()  # noqa: SLF001
        check(
            "not permitted" in pane.invocation_label.text() and not remote.set_call.entered.is_set(),
            f"the table editor rejects {text} without invoking the remote",
        )

    window.close()
    check(remote.close_count == 1 and service.stop_count == 1, "allowed-domain session resources close once")


def choice_controls_use_operation_allowed_values(
    app: QApplication,
    provider: ProviderService,
) -> None:
    window, pane, service = new_window(provider)
    remote = FakeRemote("choice-domains")
    remote.metric_values = {
        "narrowed": RemoteMetric(
            handle="narrowed",
            node_type_name="EnumStringMetricDescriptor",
            kind=MetricKind.CHOICE,
            label="Narrowed choice",
            allowed_values=("IDLE", "RUN", "PAUSE"),
            operation_allowed_values=("IDLE", "RUN"),
            value="RUN",
            operation_handles=("set.narrowed",),
            selected_operation_handle="set.narrowed",
            controllable_now=True,
        ),
        "different": RemoteMetric(
            handle="different",
            node_type_name="EnumStringMetricDescriptor",
            kind=MetricKind.CHOICE,
            label="Different choice",
            allowed_values=("descriptor-a", "descriptor-b"),
            operation_allowed_values=("operation-a", "operation-b"),
            value="operation-b",
            operation_handles=("set.different",),
            selected_operation_handle="set.different",
            controllable_now=True,
        ),
        "read-only": RemoteMetric(
            handle="read-only",
            node_type_name="EnumStringMetricDescriptor",
            kind=MetricKind.CHOICE,
            label="Read-only choice",
            allowed_values=("STANDBY", "ACTIVE"),
            value="ACTIVE",
        ),
    }
    attach(pane, remote)

    for handle, expected in (
        ("narrowed", ("IDLE", "RUN")),
        ("different", ("operation-a", "operation-b")),
    ):
        card_box = pane.board.card(handle).control.box
        card_values = tuple(card_box.itemText(index) for index in range(card_box.count()))
        pane.select_handle(handle)
        editor_values = tuple(
            pane.choice_box.itemText(index) for index in range(pane.choice_box.count())
        )
        check(
            card_values == expected and editor_values == expected,
            f"the {handle} card and table editor use the operation choice domain",
        )

    read_only_box = pane.board.card("read-only").control.box
    pane.select_handle("read-only")
    read_only_editor_values = tuple(
        pane.choice_box.itemText(index) for index in range(pane.choice_box.count())
    )
    check(
        tuple(read_only_box.itemText(index) for index in range(read_only_box.count()))
        == ("STANDBY", "ACTIVE")
        and read_only_box.currentText() == "ACTIVE"
        and not read_only_box.isEnabled()
        and read_only_editor_values == ("STANDBY", "ACTIVE")
        and not pane.editor_stack.isEnabled(),
        "read-only card and table controls retain descriptor choices when no operation narrows them",
    )
    check(
        remote.metric_values["different"].allowed_values
        == ("descriptor-a", "descriptor-b"),
        "GUI conversion preserves descriptor choices in the remote snapshot",
    )

    window.close()
    check(remote.close_count == 1 and service.stop_count == 1, "choice-domain session resources close once")


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
        close_idle_window(app, provider)
        close_during_scan(app, provider)
        close_during_connect(app, provider)
        close_with_queued_connection(app, provider)
        non_weakrefable_resources_are_rejected()
        unhashable_keys_and_resources_are_rejected()
        thread_start_failure_restores_bookkeeping()
        async_resources_retire_once()
        active_equal_resource_retires_after_release()
        retired_remote_graph_is_collectable(app, provider)
        close_during_invocation(app, provider, action=False)
        close_during_invocation(app, provider, action=True)
        stale_set_after_reconnect(app, provider)
        peer_restart_requires_manual_reconnect(app, provider)
        failed_reconnect_clears_peer_ui(app, provider)
        waveform_reports_are_scoped(app, provider)
        metric_reports_are_scoped(app, provider)
        refresh_snapshot_and_editor_lookups_are_scoped(app, provider)
        table_editor_enforces_complete_allowed_domain(app, provider)
        choice_controls_use_operation_allowed_values(app, provider)
    finally:
        consumer_module.ConsumerService = old_service
        consumer_module.MdibBridge = old_bridge
        provider.stop()
    return REPORT.summary()


if __name__ == "__main__":
    raise SystemExit(main())
