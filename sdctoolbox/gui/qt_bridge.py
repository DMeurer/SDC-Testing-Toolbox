"""Bridge between sdc11073 observables and Qt signals.

sdc11073 invokes its callbacks on whichever thread caused the change. A value we set from
the GUI arrives on the GUI thread, but a value a remote consumer sets arrives on the SCO
worker thread. Touching a widget from there would be a crash waiting to happen.

This object turns every callback into a Qt signal. Because the bridge is created on the GUI
thread, Qt automatically queues emissions that originate elsewhere and delivers them on the
GUI thread, so slots connected to these signals are always safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

from sdc11073 import observableproperties

if TYPE_CHECKING:
    from sdc11073.mdib.mdibbase import MdibBase


class MdibBridge(QObject):
    """Re-emits MDIB observables as Qt signals.

    Usage::

        bridge = MdibBridge(mdib, self)          # created on the GUI thread
        bridge.metrics_changed.connect(self.refresh)
    """

    # handle -> state, for metrics whose value changed
    metrics_changed = Signal(dict)
    # handle -> descriptor, for descriptors created at runtime
    descriptors_added = Signal(dict)
    # handle -> descriptor, for descriptors whose definition changed
    descriptors_updated = Signal(dict)
    # handle -> descriptor, for descriptors that disappeared
    descriptors_deleted = Signal(dict)
    # handle -> state, for operations whose OperatingMode changed
    operations_changed = Signal(dict)
    # handle -> state, for alerts
    alerts_changed = Signal(dict)
    # handle -> state, for waveforms. Separate from metrics_by_handle because sdc11073
    # routes a RealTimeSampleArrayMetricState down its own path, as a WaveformStream
    # rather than an EpisodicMetricReport - so a waveform never appears in the other one.
    waveforms_changed = Signal(dict)
    # The peer restarted: its sequence or instance id changed and the cached MDIB is stale.
    peer_restarted = Signal()

    def __init__(self, mdib: MdibBase, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mdib = mdib
        bindings = {
            "metrics_by_handle": self._on_metrics,
            "new_descriptors_by_handle": self._on_descriptors_added,
            "updated_descriptors_by_handle": self._on_descriptors_updated,
            "deleted_descriptors_by_handle": self._on_descriptors_deleted,
            "operation_by_handle": self._on_operations,
            "alert_by_handle": self._on_alerts,
            "waveform_by_handle": self._on_waveforms,
        }
        # Only a ConsumerMdib reports that the far end restarted; a provider has no peer.
        if hasattr(type(mdib), "sequence_or_instance_id_changed_event"):
            bindings["sequence_or_instance_id_changed_event"] = self._on_peer_restarted
        observableproperties.bind(mdib, **bindings)

    # The callbacks below may run on any thread. They must do nothing but emit.

    def _on_metrics(self, values: dict) -> None:
        self.metrics_changed.emit(dict(values))

    def _on_descriptors_added(self, values: dict) -> None:
        self.descriptors_added.emit(dict(values))

    def _on_descriptors_updated(self, values: dict) -> None:
        self.descriptors_updated.emit(dict(values))

    def _on_descriptors_deleted(self, values: dict) -> None:
        self.descriptors_deleted.emit(dict(values))

    def _on_operations(self, values: dict) -> None:
        self.operations_changed.emit(dict(values))

    def _on_alerts(self, values: dict) -> None:
        self.alerts_changed.emit(dict(values))

    def _on_waveforms(self, values: dict) -> None:
        self.waveforms_changed.emit(dict(values))

    def _on_peer_restarted(self, changed: bool) -> None:  # noqa: FBT001 - the observable is a flag
        if changed:
            self.peer_restarted.emit()
