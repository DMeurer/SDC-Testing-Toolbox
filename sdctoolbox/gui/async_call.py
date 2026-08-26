"""Running blocking work without freezing the interface.

Discovery waits for probe responses, connecting fetches a whole MDIB over SOAP, and a remote
set waits for the provider to report back. Every one of those takes seconds. Called from a
Qt slot they would lock the window solid.

AsyncCall runs one such function on a worker thread and reports the outcome back through Qt
signals. Because the object is created on the GUI thread, Qt queues those emissions and
delivers them there, so the slots may touch widgets freely.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("sdctoolbox.gui.async_call")


class AsyncCall(QObject):
    """Runs one function at a time on a worker thread.

    Create one per kind of operation and wire its signals up front::

        self._scan = AsyncCall(self)
        self._scan.finished.connect(self._on_scan_finished)
        self._scan.failed.connect(self._on_scan_failed)
        ...
        self._scan.start(service.scan, timeout=10)

    A second start() while the first is still running is refused rather than queued, which
    keeps a leaning-on-the-button user from stacking up discovery runs.
    """

    # The return value of the call.
    finished = Signal(object)
    # The message from whatever went wrong.
    failed = Signal(str)
    # True when work begins, False when it ends either way. Useful for disabling buttons.
    busy_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        """Whether a call is in flight."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """Begin the call. Returns False if one is already running."""
        if self.busy:
            logger.debug("ignoring %s, previous call still running", getattr(function, "__name__", function))
            return False

        def worker() -> None:
            try:
                result = function(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - the whole point is to report anything
                logger.exception("background call failed")
                self.failed.emit(str(exc) or exc.__class__.__name__)
            else:
                self.finished.emit(result)
            finally:
                self.busy_changed.emit(False)

        self.busy_changed.emit(True)
        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        return True
