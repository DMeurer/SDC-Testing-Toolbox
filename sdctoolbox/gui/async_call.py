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
import time
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("sdctoolbox.gui.async_call")


@dataclass
class _ResourceUse:
    resource: object
    users: int = 0
    closer: Callable[[], None] | None = None


class AsyncCall(QObject):
    """Runs blocking functions on owned worker threads.

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
    # Managed calls include their key so one owner can route and validate callbacks.
    managed_finished = Signal(object, object)
    managed_failed = Signal(object, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._threads: dict[object, threading.Thread] = {}
        self._resources: dict[object, _ResourceUse] = {}
        self._retired_resources: weakref.WeakSet[object] = weakref.WeakSet()
        self._pending_results: dict[int, tuple[Any, Callable[[Any], None]]] = {}
        self._closed = False
        self._cancelled = threading.Event()

    @property
    def busy(self) -> bool:
        """Whether a call is in flight."""
        with self._lock:
            return bool(self._threads)

    @property
    def cancellation_event(self) -> threading.Event:
        """An event cooperative operations may inspect while shutting down."""
        return self._cancelled

    def busy_for(self, key: object) -> bool:
        """Whether the named managed call is in flight."""
        with self._lock:
            return key in self._threads

    def busy_matching(self, predicate: Callable[[object], bool]) -> bool:
        """Whether any active managed call matches ``predicate``."""
        with self._lock:
            return any(predicate(key) for key in self._threads)

    def start(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        """Begin the call. Returns False if one is already running."""
        return self.start_managed(None, None, function, *args, **kwargs)

    def start_managed(
        self,
        key: object,
        resource: object | None,
        function: Callable[..., Any],
        *args: Any,
        discard_result: Callable[[Any], None] | None = None,
        **kwargs: Any,
    ) -> bool:
        """Start one keyed call and keep its resource alive until the call returns.

        Different keys may run concurrently. Reusing an active key is refused, preserving
        :meth:`start`'s one-call-at-a-time contract for existing users.
        """
        def worker() -> None:
            result: Any = None
            error: Exception | None = None
            try:
                result = function(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - the whole point is to report anything
                logger.exception("background call failed")
                error = exc
            finally:
                retirements: list[tuple[object, Callable[[], None]]] = []
                with self._lock:
                    self._threads.pop(key, None)
                    if resource is not None:
                        use = self._resources[resource]
                        use.users -= 1
                        if use.users == 0 and use.closer is not None:
                            retirement = (use.resource, use.closer)
                            self._resources.pop(resource)
                            retirements.append(retirement)
                    emit = not self._closed
                    now_idle = not self._threads
                    if emit and error is None and discard_result is not None:
                        self._pending_results[id(result)] = result, discard_result
                for retirement in retirements:
                    self._close_resource(retirement[1])
                if emit:
                    if error is None:
                        self.finished.emit(result)
                        self.managed_finished.emit(key, result)
                    else:
                        message = str(error) or error.__class__.__name__
                        self.failed.emit(message)
                        self.managed_failed.emit(key, message)
                    if now_idle:
                        self.busy_changed.emit(False)
                elif error is None and discard_result is not None:
                    self._close_resource(lambda: discard_result(result))

        thread = threading.Thread(target=worker, daemon=True)
        with self._lock:
            if self._closed or key in self._threads:
                logger.debug(
                    "ignoring %s, worker is closed or key is busy",
                    getattr(function, "__name__", function),
                )
                return False
            was_busy = bool(self._threads)
            if resource is not None:
                use = self._resources.get(resource)
                if self._resource_is_retired(resource) or (
                    use is not None and use.closer is not None
                ):
                    logger.debug(
                        "ignoring %s, its resource is retired",
                        getattr(function, "__name__", function),
                    )
                    return False
                if use is None:
                    use = _ResourceUse(resource)
                    self._resources[resource] = use
                use.users += 1
            self._threads[key] = thread
        if not was_busy:
            self.busy_changed.emit(True)
        thread.start()
        return True

    def retire(self, resource: object, closer: Callable[[], None]) -> None:
        """Close a resource once, immediately or after all calls using it finish."""
        retirement: tuple[object, Callable[[], None]] | None = None
        with self._lock:
            use = self._resources.get(resource)
            if self._resource_is_retired(resource) or (
                use is not None and use.closer is not None
            ):
                return
            retired_resource = resource if use is None else use.resource
            self._retired_resources.add(retired_resource)
            if use is None:
                retirement = (retired_resource, closer)
            else:
                use.closer = closer
                if use.users == 0:
                    self._resources.pop(resource)
                    retirement = (retired_resource, closer)
        if retirement is not None:
            self._close_resource(retirement[1])

    def claim_result(self, result: object) -> bool:
        """Transfer a managed result from the worker to its signal recipient."""
        with self._lock:
            return self._pending_results.pop(id(result), None) is not None

    def close(self, timeout: float = 0.25) -> bool:
        """Suppress future callbacks and wait at most ``timeout`` seconds for workers."""
        with self._lock:
            self._closed = True
            self._cancelled.set()
            pending = list(self._pending_results.values())
            self._pending_results.clear()
        for result, discard in pending:
            self._close_resource(lambda result=result, discard=discard: discard(result))
        return self.wait(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for current workers, returning whether all completed in time."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads.values())
        current = threading.current_thread()
        for thread in threads:
            if thread is current:
                continue
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            thread.join(remaining)
        with self._lock:
            return not self._threads

    def _resource_is_retired(self, resource: object) -> bool:
        return resource in self._retired_resources

    @staticmethod
    def _close_resource(closer: Callable[[], None]) -> None:
        try:
            closer()
        except Exception:  # noqa: BLE001 - one cleanup must not strand another worker
            logger.exception("error while retiring background-call resource")
