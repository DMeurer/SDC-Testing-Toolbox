"""Keep the toolbox's listeners inside constants.PORT_RANGE.

The packaged firewall definitions open exactly that range, so every socket that peers
connect to must be bound inside it. sdc11073 binds its HTTP servers and the WS-Discovery
reply socket to port 0; the classes here bind the same sockets in the range instead.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import threading
from typing import TYPE_CHECKING

from sdc11073.consumer import consumerimpl
from sdc11073.httpserver.httpserverimpl import HttpServerThreadBase, _ThreadingHTTPServer
from sdc11073.provider import providerimpl
from sdc11073.wsdiscovery import WSDiscovery, networkingthread

from . import constants

if TYPE_CHECKING:
    from collections.abc import Callable

PORT_LOGGER = logging.getLogger("sdctoolbox.ports")

# "Someone else has this port": try the next one. Windows reports WSAEADDRINUSE and WSAEACCES
# (the latter for ports held exclusively or reserved by the system).
_BUSY = frozenset({errno.EADDRINUSE, errno.EACCES, 10048, 10013})

_lock = threading.RLock()
_warnings: list[str] = []
_subscribers: list[Callable[[str], None]] = []


def bind_in_range(sock: socket.socket, ip: str, protocol: str) -> int:
    """Bind ``sock`` to the first free port of the range on ``ip`` and return the port.

    If every port is taken, bind to a port the OS picks, log a warning and notify subscribers.
    Any error other than "port taken" (a wrong address, for example) is raised at once.
    """
    if os.name == "nt":
        # Without this, a later socket with SO_REUSEADDR could take over our port on Windows.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    port_range = constants.PORT_RANGE
    for port in port_range:
        try:
            sock.bind((ip, port))
        except OSError as exc:
            if exc.errno in _BUSY or getattr(exc, "winerror", None) in _BUSY:
                continue
            raise
        return port
    sock.bind((ip, 0))
    port = sock.getsockname()[1]
    message = (
        f"{protocol} port {port} is outside {port_range[0]}-{port_range[-1]} because no port in that "
        "range was free; the shipped firewall definitions do not cover it"
    )
    PORT_LOGGER.warning("%s", message)
    with _lock:
        _warnings.append(message)
        for callback in list(_subscribers):
            callback(message)
    return port


def fallback_warnings() -> tuple[str, ...]:
    """Every fallback warning raised in this process, oldest first."""
    with _lock:
        return tuple(_warnings)


def subscribe_fallback_warnings(callback: Callable[[str], None]) -> Callable[[], None]:
    """Call ``callback`` (on the binding thread) for each future warning; return the unsubscribe."""
    with _lock:
        _subscribers.append(callback)

    def unsubscribe() -> None:
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)

    return unsubscribe


class _RangeThreadingHTTPServer(_ThreadingHTTPServer):
    # Lets a restarted listener reuse a port with connections in TIME_WAIT. On Windows the same
    # option would let two listeners share a port, so it stays off there.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if self.allow_reuse_address:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_in_range(self.socket, self.server_address[0], "TCP")
        self.server_address = self.socket.getsockname()

    def server_activate(self) -> None:
        for attempt in range(len(constants.PORT_RANGE) + 1):
            try:
                super().server_activate()
                return
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE or attempt == len(constants.PORT_RANGE):
                    raise
                # A competing SO_REUSEADDR socket can win between bind() and listen().
                self.socket.close()
                self.socket = socket.socket(self.address_family, self.socket_type)
                self.server_bind()


class RangeHttpServerThread(HttpServerThreadBase):
    """sdc11073's HTTP server thread, bound inside the port range.

    The server is created in ``start()`` on the caller's thread, so a bind error surfaces there
    instead of after the library's start timeout.
    """

    def start(self) -> None:
        self.httpd = _RangeThreadingHTTPServer(
            self.logger,
            (self._my_ipaddress, 0),
            self.chunk_size,
            self.supported_encodings,
        )
        super().start()

    def run(self) -> None:
        # HttpServerThreadBase.run without creating the server.
        self._stop_requested = False
        try:
            self.logger.info("starting http server on %s:%s", self._my_ipaddress, self.server_port)
            if self._ssl_context:
                self.httpd.socket = self._ssl_context.wrap_socket(self.httpd.socket, server_side=True)
                self.base_url = f"https://{self._my_ipaddress}:{self.server_port}/"
            else:
                self.base_url = f"http://{self._my_ipaddress}:{self.server_port}/"
            self.started_evt.set()
            self.httpd.serve_forever()
        except Exception:
            if not self._stop_requested:
                self.logger.exception("Unhandled Exception at thread runtime. Thread will abort!")
            raise
        finally:
            self.logger.info("http server stopped.")


class _RangeNetworkingThread(networkingthread.NetworkingThread):
    def _create_multi_out_uni_in_out_socket(self, addr: str, multicast_ttl: int) -> socket.SocketType:
        # shortcut: copy of sdc11073 3.0's method with the bind replaced; recheck on upgrade.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast_ttl)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
            bind_in_range(sock, addr, "UDP")
        except OSError:
            sock.close()
            raise
        self._register_outbound_socket(sock)
        self._register_inbound_socket(sock)
        return sock


class RangeWSDiscovery(WSDiscovery):
    """WS-Discovery whose unicast reply socket is bound inside the port range.

    Probe matches and resolve matches come back to this socket, so a firewall must let it in.
    """

    def _start_threads(self) -> None:
        # shortcut: copy of sdc11073 3.0's method with our thread class; recheck on upgrade.
        if self._networking_thread is not None:
            return
        self._networking_thread = _RangeNetworkingThread(
            self._adapter_ip,
            self,
            self._logger,
            self.multicast_port,
            self.multicast_ttl,
        )
        self._networking_thread.start()


def install() -> None:
    """Make sdc11073's provider and consumer create range-bound HTTP servers."""
    # shortcut: sdc11073 3.0 has no port or server-factory parameter; recheck on upgrade.
    providerimpl.HttpServerThreadBase = RangeHttpServerThread
    consumerimpl.HttpServerThreadBase = RangeHttpServerThread
