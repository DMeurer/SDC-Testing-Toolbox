"""Running blocking work without freezing the interface.

Discovery waits for probe responses, connecting fetches a whole MDIB over SOAP, and a remote
set waits for the provider to report back. Every one of those takes seconds. Called from a
Qt slot they would lock the window solid.

AsyncCall runs keyed functions on worker threads and reports their outcomes back through Qt
signals. Because the object is created on the GUI thread, Qt queues those emissions and
delivers them there, so the slots may touch widgets freely.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("sdctoolbox.gui.async_call")


def _validate_hashable(value: object, name: str) -> None:
    try:
        hash(value)
    except TypeError as exc:
        raise TypeError(f"managed {name} must be hashable") from exc


def _validate_managed_resource(
    resource: Annotated[object, "hashable and supports weak references"],
) -> None:
    try:
        weakref.ref(resource)
    except TypeError as exc:
        raise TypeError("managed resource must support weak references") from exc
    _validate_hashable(resource, "resource")


@dataclass
class _ResourceUse:
    resource: object
    users: int = 0
    closer: Callable[[], None] | None = None


class AsyncCall(QObject):
    """Runs blocking functions on owned worker threads.

    Create one owner and wire its keyed signals up front::

        self._worker = AsyncCall(self)
        self._worker.managed_finished.connect(self._on_work_finished)
        self._worker.managed_failed.connect(self._on_work_failed)
        ...
        self._worker.start_managed(("scan", generation), service, service.scan)

    Keys and resources must be hashable, and resources must also support weak references.
    Reusing an active key is refused rather than queued.
    """

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

    def start_managed(
        self,
        key: Annotated[object, "hashable"],
        resource: Annotated[object, "hashable and supports weak references"] | None,
        function: Callable[..., Any],
        *args: Any,
        discard_result: Callable[[Any], None] | None = None,
        **kwargs: Any,
    ) -> bool:
        """Start one keyed call and keep its resource alive until the call returns.

        Different keys may run concurrently, while reusing an active key is refused. Keys and
        resources must be hashable. A non-None resource must also support weak references so
        completed retirement records do not retain it. Invalid keys or resources raise
        :class:`TypeError` synchronously. If starting the thread fails, all bookkeeping is
        rolled back and the original exception is re-raised, so the call may be retried.
        """
        _validate_hashable(key, "key")
        if resource is not None:
            _validate_managed_resource(resource)

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
                    if emit and error is None and discard_result is not None:
                        self._pending_results[id(result)] = result, discard_result
                for retirement in retirements:
                    self._close_resource(retirement[1])
                if emit:
                    if error is None:
                        self.managed_finished.emit(key, result)
                    else:
                        message = str(error) or error.__class__.__name__
                        self.managed_failed.emit(key, message)
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
            created_use = False
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
                    created_use = True
                use.users += 1
            self._threads[key] = thread
            try:
                thread.start()
            except BaseException:
                self._threads.pop(key)
                if resource is not None:
                    if created_use:
                        self._resources.pop(resource)
                    else:
                        use.users -= 1
                raise
        return True

    def retire(
        self,
        resource: Annotated[object, "hashable and supports weak references"],
        closer: Callable[[], None],
    ) -> None:
        """Close a weak-referenceable resource now or after its managed calls finish.

        A resource that is unhashable or does not support weak references raises
        :class:`TypeError` before ``closer`` is registered or called.
        """
        _validate_managed_resource(resource)
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
