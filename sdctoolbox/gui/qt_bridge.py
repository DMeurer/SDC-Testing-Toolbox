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

    #: handle -> state, for metrics whose value changed
    metrics_changed = Signal(dict)
    #: handle -> descriptor, for descriptors created at runtime
    descriptors_added = Signal(dict)
    #: handle -> descriptor, for descriptors whose definition changed
    descriptors_updated = Signal(dict)
    #: handle -> descriptor, for descriptors that disappeared
    descriptors_deleted = Signal(dict)
    #: handle -> state, for operations whose OperatingMode changed
    operations_changed = Signal(dict)
    #: handle -> state, for alerts (unused until alerts are implemented)
    alerts_changed = Signal(dict)

    def __init__(self, mdib: MdibBase, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._mdib = mdib
        observableproperties.bind(
            mdib,
            metrics_by_handle=self._on_metrics,
            new_descriptors_by_handle=self._on_descriptors_added,
            updated_descriptors_by_handle=self._on_descriptors_updated,
            deleted_descriptors_by_handle=self._on_descriptors_deleted,
            operation_by_handle=self._on_operations,
            alert_by_handle=self._on_alerts,
        )

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
