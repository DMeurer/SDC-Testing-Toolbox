"""Request handling at the toolbox-owned SDC server boundary."""

from __future__ import annotations

import socket
import ssl
from http.server import BaseHTTPRequestHandler

from sdc11073.httpserver.httprequesthandler import DispatchingRequestHandler


class ShutdownAwareRequestHandler(DispatchingRequestHandler):
    """Treat an interrupted next-request read as EOF only after local TLS shutdown."""

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except BrokenPipeError as exc:
            traceback = exc.__traceback__.tb_next
            # Windows SSL reads can report WSAESHUTDOWN after sdc11073 server_close.
            # shortcut: CPython/sdc11073 3.0 shutdown markers; recheck when their server lifecycle changes.
            if not (
                exc.errno == 10058
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
    """Use the standard per-server request-handler hook, without changing stop semantics."""
    if isinstance(http_server.httpd.socket, ssl.SSLSocket):
        http_server.httpd.RequestHandlerClass = ShutdownAwareRequestHandler
