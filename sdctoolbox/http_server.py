"""Request handling at the toolbox-owned SDC server boundary."""

from __future__ import annotations

import errno
import socket
import ssl
from http.server import BaseHTTPRequestHandler

from sdc11073.httpserver.httprequesthandler import DispatchingRequestHandler

TLS_HANDSHAKE_TIMEOUT = 5.0
WSAESHUTDOWN = 10058


class DeferredHandshakeSocket(ssl.SSLSocket):
    """Keep even connections arriving before the request-handler hook off the accept loop."""

    def accept(self) -> tuple[ssl.SSLSocket, tuple]:
        self.do_handshake_on_connect = False
        request, address = super().accept()
        request._sdc_request_timeout = request.gettimeout()
        # shortcut: pre-hook workers retain this read timeout; remove when the SDK offers a pre-listen handler hook.
        request.settimeout(TLS_HANDSHAKE_TIMEOUT)
        return request, address


class ShutdownAwareRequestHandler(DispatchingRequestHandler):
    """Treat an interrupted next-request read as EOF only after local TLS shutdown."""

    def handle(self) -> None:
        if isinstance(self.connection, ssl.SSLSocket):
            timeout = getattr(self.connection, "_sdc_request_timeout", self.connection.gettimeout())
            try:
                self.connection.settimeout(TLS_HANDSHAKE_TIMEOUT)
                self.connection.do_handshake()
                self.connection.settimeout(timeout)
            except OSError as exc:
                self.server.logger.info("TLS handshake with %s failed: %s", self.client_address, exc)
                return
            except AttributeError as exc:
                # setup's makefiles can retain the descriptor after SDK shutdown clears _sslobj.
                traceback = exc.__traceback__.tb_next
                if not (
                    self.server.dispatcher is None
                    and self.connection._sslobj is None  # noqa: SLF001 - cleared by local SSLSocket.shutdown
                    and exc.name == "do_handshake" and exc.obj is None
                    and traceback is not None
                    and traceback.tb_frame.f_code is ssl.SSLSocket.do_handshake.__code__
                    and traceback.tb_next is None
                ):
                    raise
                self.close_connection = True
                return
        super().handle()

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except BrokenPipeError as exc:
            traceback = exc.__traceback__.tb_next
            # After sdc11073 server_close, OpenSSL's failed alert write surfaces as
            # WSAESHUTDOWN on Windows and EPIPE on Linux.
            # shortcut: CPython/sdc11073 3.0 shutdown markers; recheck when their server lifecycle changes.
            if not (
                exc.errno in (WSAESHUTDOWN, errno.EPIPE)
                and isinstance(self.connection, ssl.SSLSocket)
                and self.server.dispatcher is None
                and self.connection._sslobj is None  # noqa: SLF001 - cleared by local SSLSocket.shutdown
                and traceback is not None
                and traceback.tb_frame.f_code is BaseHTTPRequestHandler.handle_one_request.__code__
                and traceback.tb_next is not None
                and traceback.tb_next.tb_frame.f_code is socket.SocketIO.readinto.__code__
            ):
                raise
            self.close_connection = True


def configure_request_handler(http_server) -> None:  # noqa: ANN001
    """Defer TLS handshakes to tracked request workers, keeping accept and stop responsive."""
    if isinstance(http_server.httpd.socket, ssl.SSLSocket):
        http_server.httpd.socket.do_handshake_on_connect = False
        http_server.httpd.RequestHandlerClass = ShutdownAwareRequestHandler
