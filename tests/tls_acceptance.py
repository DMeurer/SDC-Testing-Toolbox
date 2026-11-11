"""End-to-end mutual-TLS acceptance for the toolbox's provider and consumer services."""

from __future__ import annotations

import asyncio
import faulthandler
import io
import logging
import socket
import ssl
import sys
import threading
import time
from contextlib import contextmanager, redirect_stderr
from dataclasses import replace
from decimal import Decimal
from http.client import HTTPResponse
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from script_support import CallbackRecorder, Report, wait_until  # noqa: E402
from sdc11073 import observableproperties  # noqa: E402
from sdc11073.httpserver.httpserverimpl import HttpServerThreadBase  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService, DiscoveredDevice  # noqa: E402
from sdctoolbox.http_server import ShutdownAwareRequestHandler, configure_request_handler  # noqa: E402
from sdctoolbox.model import MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402
from security import issue_certificate, make_tls_configurations, write_certificate, write_key  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402


@contextmanager
def scripted_tls_peer(contexts):  # noqa: ANN001, ANN201 - test-only wire peer
    """Return Connection: close, selecting a real certificate for each new connection."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(5)
    received = []
    sockets = []
    errors = []

    def serve() -> None:
        try:
            for context in contexts:
                raw, _ = listener.accept()
                raw.settimeout(3)
                data = b""
                try:
                    with context.wrap_socket(raw, server_side=True) as tls:
                        sockets.append(tls)
                        while b"\r\n\r\n" not in data:
                            chunk = tls.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                        if b"\r\n\r\n" in data:
                            headers, body = data.split(b"\r\n\r\n", 1)
                            length = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                                           if line.lower().startswith(b"content-length:")), 0)
                            while len(body) < length:
                                chunk = tls.recv(length - len(body))
                                if not chunk:
                                    break
                                body += chunk
                                data += chunk
                            tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except (OSError, ssl.SSLError):
                    raw.close()
                finally:
                    received.append(data)
        except OSError as exc:
            if listener.fileno() != -1:
                errors.append(exc)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield SimpleNamespace(address=f"https://127.0.0.1:{listener.getsockname()[1]}/device", received=received)
    finally:
        listener.close()
        for connection in sockets:
            connection.close()
        worker.join(6)
        assert not worker.is_alive(), "scripted TLS peer did not stop"
        assert not errors, errors


def check_policy_before_metadata(report: Report) -> None:
    with TemporaryDirectory(prefix="sdc-tls-metadata-") as raw_temporary:
        temporary = Path(raw_temporary)
        provider_tls, consumer_tls = make_tls_configurations(temporary)
        other = temporary / "other"
        other.mkdir()
        untrusted_tls, _ = make_tls_configurations(other)
        untrusted_server = untrusted_tls.create_contexts().server_context
        untrusted_server.verify_mode = ssl.CERT_NONE  # the external peer accepts the test client
        for config, context, description in (
            (replace(consumer_tls, peer_fingerprint=consumer_tls.local_certificate().sha256_fingerprint),
             provider_tls.create_contexts().server_context, "un-pinned certificate"),
            (replace(consumer_tls, allow_self_signed=True), untrusted_server, "untrusted non-self-signed certificate"),
        ):
            service = ConsumerService(tls_config=config)
            service._tls_contexts = config.create_contexts()  # no discovery is needed for a direct wire peer
            with scripted_tls_peer([context]) as peer:
                device = DiscoveredDevice("wire-peer", (peer.address,), (), service=object())
                try:
                    service.connect(device)
                except Exception:  # noqa: BLE001 - certificate errors may be wrapped by the SOAP stack
                    rejected = True
                else:
                    rejected = False
                assert wait_until(lambda: len(peer.received) == 1, timeout=5)
                report.check(
                    rejected and peer.received == [b""],
                    f"{description} receives no metadata or subscription payload",
                    repr(peer.received),
                )


def check_transport_pins(report: Report) -> None:
    with TemporaryDirectory(prefix="sdc-tls-reconnect-") as raw_temporary:
        provider_tls, consumer_tls = make_tls_configurations(Path(raw_temporary))
        pinned = replace(consumer_tls, peer_fingerprint=provider_tls.local_certificate().sha256_fingerprint)
        provider = ProviderService(instance_name="tls-pinned-provider", tls_config=provider_tls)
        service = ConsumerService(tls_config=pinned)
        remote = None
        try:
            provider.start()
            service.start()
            remote = service.connect(DiscoveredDevice(
                provider.epr.urn, (provider._provider.get_xaddrs()[0],), (), service=object(),
            ))
            consumer = remote._consumer
            good = provider_tls.create_contexts().server_context
            bad = consumer_tls.create_contexts().server_context  # same CA, different leaf
            for path in ("/metadata", "/Subscribe"):
                with scripted_tls_peer([bad]) as peer:
                    soap = consumer.get_soap_client(peer.address)
                    try:
                        soap.post_message_to(path, SimpleNamespace(serialize=lambda **_kwargs: b"<GetMetadata/>"))
                    except Exception:  # noqa: BLE001
                        rejected = True
                    else:
                        rejected = False
                    finally:
                        soap.close()
                    assert wait_until(lambda: len(peer.received) == 1, timeout=5)
                    report.check(rejected and peer.received == [b""], f"new hosted-service transport pins before {path}")
            for method in ("POST", "GET"):
                with scripted_tls_peer([good, bad]) as peer:
                    soap = consumer.get_soap_client(peer.address)
                    if method == "POST":
                        assert soap.post_message_to("/GetMdib", SimpleNamespace(serialize=lambda **_kwargs: b"<GetMdib/>")) is None
                    else:
                        assert soap.get_from_url("/wsdl", "first WSDL") == b""
                    try:
                        if method == "POST":
                            soap.post_message_to("/Subscribe", SimpleNamespace(serialize=lambda **_kwargs: b"<Subscribe/>"))
                        else:
                            soap.get_from_url("/wsdl", "reconnected WSDL")
                    except Exception:  # noqa: BLE001
                        rejected = True
                    else:
                        rejected = False
                    finally:
                        soap.close()
                    assert wait_until(lambda: len(peer.received) == 2, timeout=5)
                    report.check(
                        rejected and bool(peer.received[0]) and peer.received[1] == b"",
                        f"actual SOAP {method} reconnect after Connection: close rechecks the leaf pin before payload",
                    )
        finally:
            if remote is not None:
                remote.close()
            service.stop()
            provider.stop()


def check_callback_transport_policy(report: Report) -> None:
    """Exercise callback clients created by the actual production provider factory."""
    with TemporaryDirectory(prefix="sdc-tls-callback-") as raw_temporary:
        temporary = Path(raw_temporary)
        provider_tls, consumer_tls = make_tls_configurations(temporary)
        other = temporary / "other"
        other.mkdir()
        untrusted_tls, _ = make_tls_configurations(other)
        untrusted_server = untrusted_tls.create_contexts().server_context
        untrusted_server.verify_mode = ssl.CERT_NONE
        for config, contexts, description in (
            (replace(provider_tls, peer_fingerprint=consumer_tls.local_certificate().sha256_fingerprint),
             [consumer_tls.create_contexts().server_context, provider_tls.create_contexts().server_context],
             "async callback reconnect pins before notification payload"),
            (replace(provider_tls, allow_self_signed=True), [untrusted_server],
             "async callback verifies relaxed trust before its first notification"),
        ):
            with ProviderService(instance_name="tls-callback-transport-provider", tls_config=config) as provider:
                with scripted_tls_peer(contexts) as peer:
                    async def notify() -> bool:
                        soap = provider._provider._soap_client_pool.get_soap_client(
                            urlsplit(peer.address).netloc, [], "tls-policy-test",
                        )
                        message = SimpleNamespace(serialize=lambda **_kwargs: b"<?xml version='1.0' encoding='utf-8'?><MetricReport/>")
                        try:
                            if len(contexts) == 2:
                                assert await soap.async_post_message_to("/notify", message) is None
                            try:
                                await soap.async_post_message_to("/notify", message)
                            except Exception:  # noqa: BLE001 - certificate errors are transport-specific
                                return True
                            return False
                        finally:
                            await soap.async_close()

                    rejected = asyncio.run(notify())
                    assert wait_until(lambda: len(peer.received) == len(contexts), timeout=5)
                    report.check(
                        rejected and peer.received[-1] == b"" and (len(contexts) == 1 or bool(peer.received[0])),
                        description,
                    )


def check_incomplete_handshake_shutdown(report: Report) -> None:
    """An accepted TCP socket with no TLS bytes must not occupy the accept loop."""
    with TemporaryDirectory(prefix="sdc-tls-incomplete-") as raw_temporary:
        provider_tls, consumer_tls = make_tls_configurations(Path(raw_temporary))
        server = HttpServerThreadBase(
            "127.0.0.1", provider_tls.create_contexts().server_context, [], logging.getLogger("tls-incomplete-test"),
        )
        server.start()
        assert server.started_evt.wait(5)
        httpd = server.httpd
        raw = socket.create_connection(("127.0.0.1", server.server_port), timeout=3)
        stop_errors = []

        def stop() -> None:
            try:
                server.stop()
            except Exception as exc:  # noqa: BLE001
                stop_errors.append(exc)

        stopper = threading.Thread(target=stop, daemon=True)

        def handshake_started() -> bool:
            threads = [server, *(item.thread for item in httpd.threads)]
            frames = sys._current_frames()
            return any((frame := frames.get(worker.ident)) is not None
                       and frame.f_code in (ssl.SSLSocket.do_handshake.__code__, ssl.SSLSocket.read.__code__)
                       for worker in threads)

        try:
            assert wait_until(handshake_started, timeout=3), "first client did not enter TLS handshake"
            configure_request_handler(server)  # an early client may arrive before the existing startup hook
            workers = list(httpd.threads)
            report.check(bool(workers), "accepted socket is tracked before its incomplete TLS handshake")
            try:
                with (
                    socket.create_connection(("127.0.0.1", server.server_port), timeout=1) as second,
                    consumer_tls.create_contexts().client_context.wrap_socket(second, server_hostname="127.0.0.1"),
                ):
                    connected = True
            except (OSError, ssl.SSLError):
                connected = False
            report.check(connected, "a second valid TLS client connects while the first sends no TLS bytes")
            stopper.start()
            stopper.join(2)
            report.check(not stopper.is_alive() and not stop_errors, "server stop finishes while the incomplete client remains open")
            if not stopper.is_alive():
                for worker in workers:
                    worker.thread.join(6)
                report.check(
                    all(not worker.thread.is_alive() and worker.request.fileno() == -1 for worker in workers),
                    "shutdown closes the tracked incomplete-handshake socket and worker",
                )
        finally:
            raw.close()
            if stopper.ident is None:
                server.stop()
            else:
                stopper.join(6)
            server.join(6)
            assert not stopper.is_alive() and not server.is_alive(), "incomplete-handshake fixture did not stop"


def wait_for_tls_reader(worker: threading.Thread) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        frame = sys._current_frames().get(worker.ident)
        if frame is not None and frame.f_code is ssl.SSLSocket.read.__code__:
            return
        time.sleep(0.01)
    raise AssertionError("request worker did not enter the blocking TLS read")


def check_tls_shutdown_before_handle(report: Report) -> None:
    """Reproduce setup -> SDK local shutdown/close -> deferred handshake with real SSL sockets."""
    with TemporaryDirectory(prefix="sdc-tls-handler-order-") as raw_temporary:
        provider_tls, _ = make_tls_configurations(Path(raw_temporary))
        context = provider_tls.create_contexts().server_context
        unexpected = AttributeError("unexpected TLS handshake failure", name="do_handshake", obj=None)
        for dispatcher_live, injected_error, description in (
            (False, None, "confirmed local shutdown before handle is normal EOF"),
            (True, None, "missing local-shutdown marker preserves handshake AttributeError"),
            (False, unexpected, "unrelated handshake AttributeError survives local-shutdown markers"),
        ):
            raw, peer = socket.socketpair()
            connection = context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            handler = ShutdownAwareRequestHandler.__new__(ShutdownAwareRequestHandler)
            handler.request = connection
            handler.client_address = ("127.0.0.1", 0)
            handler.server = SimpleNamespace(dispatcher=object(), logger=logging.getLogger("tls-handler-order"))
            setup_done = False
            failure = None
            try:
                handler.setup()
                setup_done = True
                if not dispatcher_live:
                    handler.server.dispatcher = None
                connection.shutdown(socket.SHUT_RDWR)
                connection.close()
                assert connection._sslobj is None and connection.fileno() != -1  # setup's makefiles retain the descriptor
                if injected_error is not None:
                    def fail_handshake() -> None:
                        raise injected_error
                    connection.do_handshake = fail_handshake
                try:
                    handler.handle()
                except AttributeError as exc:
                    failure = exc
                if injected_error is not None:
                    report.check(failure is injected_error, description)
                elif dispatcher_live:
                    report.check(isinstance(failure, AttributeError), description)
                else:
                    report.check(failure is None and getattr(handler, "close_connection", False), description, repr(failure))
            finally:
                if setup_done:
                    handler.finish()
                connection.close()
                peer.close()
                raw.close()
            report.check(
                connection.fileno() == peer.fileno() == raw.fileno() == -1,
                f"all SSL ordering sockets and file references close: {description}",
            )


def check_tls_server_shutdown(report: Report) -> None:
    """An idle TLS reader is EOF on local shutdown, not a failed request."""
    with TemporaryDirectory(prefix="sdc-tls-shutdown-") as raw_temporary:
        provider_tls, consumer_tls = make_tls_configurations(Path(raw_temporary))
        provider = ProviderService(instance_name="tls-shutdown-provider", tls_config=provider_tls)
        provider.start()
        server = provider._provider._http_server  # noqa: SLF001 - inspect real network cleanup
        httpd = server.httpd
        idle = threading.Event()
        shutdown = threading.Event()
        errors = io.StringIO()
        original_handler = httpd.RequestHandlerClass
        httpd.dispatcher.register_instance(
            "shutdown-test", SimpleNamespace(do_get=lambda *_args: (200, "OK", b"ok", "text/plain")),
        )

        class ObservedHandler(original_handler):
            def handle_one_request(self) -> None:
                if hasattr(self, "raw_requestline"):
                    idle.set()
                super().handle_one_request()

        httpd.RequestHandlerClass = ObservedHandler
        stop_errors = []

        def stop_provider() -> None:
            try:
                provider.stop()
            except Exception as exc:  # noqa: BLE001 - retain background cleanup failures
                stop_errors.append(exc)

        stopper = threading.Thread(target=stop_provider, daemon=True)
        faulthandler.dump_traceback_later(15)
        try:
            with (
                redirect_stderr(errors),
                socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as raw,
                consumer_tls.create_contexts().client_context.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
            ):
                tls.sendall(b"GET /shutdown-test HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = HTTPResponse(tls)
                response.begin()
                assert response.status == 200 and response.read() == b"ok"
                response.close()
                assert idle.wait(5), "HTTP worker did not return to its next request line"
                workers = list(httpd.threads)
                assert len(workers) == 1
                wait_for_tls_reader(workers[0].thread)
                request = workers[0].request
                report.check(request.gettimeout() is None, "handshake timeout does not shorten persistent HTTP request reads")
                original_shutdown = request.shutdown

                def observe_shutdown(how: int) -> None:
                    original_shutdown(how)
                    if how == socket.SHUT_RDWR:
                        shutdown.set()

                request.shutdown = observe_shutdown
                stopper.start()
                assert shutdown.wait(10), "provider did not shut down the accepted TLS socket"
                # Release any OpenSSL read still waiting on the peer after local shutdown.
                tls.close()
                stopper.join(5)
                server.join(5)
                for worker in workers:
                    worker.thread.join(5)
            report.check(not stop_errors and not stopper.is_alive(), "provider shutdown completes without cleanup failure")
            report.check(
                not server.is_alive() and all(not worker.thread.is_alive() for worker in workers)
                and request.fileno() == -1 and httpd.socket.fileno() == -1,
                "TLS shutdown closes listener, accepted socket, and request worker",
            )
            report.check("Traceback" not in errors.getvalue(), "local TLS request-line shutdown is normal EOF")
            if errors.getvalue():
                print(errors.getvalue(), file=sys.stderr, end="")
        finally:
            faulthandler.cancel_dump_traceback_later()
            if stopper.ident is None:
                provider.stop()
            else:
                stopper.join(5)


def check_unexpected_server_errors(report: Report) -> None:
    """Even during local TLS shutdown, body/response failures retain tracebacks."""
    with TemporaryDirectory(prefix="sdc-tls-errors-") as raw_temporary:
        provider_tls, consumer_tls = make_tls_configurations(Path(raw_temporary))
        for failure in (
            RuntimeError("unexpected request failure"),
            AttributeError("unexpected request attribute failure"),
            BrokenPipeError(10058, "unexpected response failure"),
        ):
            provider = ProviderService(instance_name="tls-errors-provider", tls_config=provider_tls)
            provider.start()
            server = provider._provider._http_server  # noqa: SLF001
            httpd = server.httpd
            handled = threading.Event()
            errors = io.StringIO()
            original_handler = httpd.RequestHandlerClass

            class FailingHandler(original_handler):
                def do_GET(self) -> None:
                    # Recreate both shutdown conditions, but fail *inside* a request.
                    self.server.dispatcher = None
                    self.connection.shutdown(socket.SHUT_RDWR)
                    raise failure

            original_error_handler = httpd.handle_error

            def observe_error(request, address) -> None:  # noqa: ANN001
                original_error_handler(request, address)
                handled.set()

            httpd.RequestHandlerClass = FailingHandler
            httpd.handle_error = observe_error
            try:
                with (
                    redirect_stderr(errors),
                    socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as raw,
                    consumer_tls.create_contexts().client_context.wrap_socket(raw, server_hostname="127.0.0.1") as tls,
                ):
                    tls.sendall(b"GET /failure HTTP/1.1\r\nHost: localhost\r\n\r\n")
                    assert handled.wait(5), "unexpected request failure was not reported"
                    workers = list(httpd.threads)
                    for worker in workers:
                        worker.thread.join(5)
                report.check(
                    "Traceback" in errors.getvalue() and type(failure).__name__ in errors.getvalue()
                    and str(failure) in errors.getvalue(),
                    f"unexpected {type(failure).__name__} retains its traceback during TLS shutdown",
                )
            finally:
                provider.stop()
                server.join(5)


def check_tls_connection(report: Report) -> None:
    with TemporaryDirectory(prefix="sdc-tls-acceptance-") as raw_temporary:
        provider_tls, consumer_tls = make_tls_configurations(Path(raw_temporary))
        provider = ProviderService(instance_name="tls-provider", tls_config=provider_tls)
        consumer = ConsumerService(tls_config=consumer_tls)
        remote = None
        try:
            provider.start()
            handle = provider.add_metric(
                MetricSpec(
                    label="Secure setting",
                    kind=MetricKind.NUMBER,
                    controllable=True,
                    initial_value=Decimal("1"),
                ),
            )
            consumer.start()
            deadline = time.monotonic() + 10.0
            devices = []
            while time.monotonic() < deadline:
                devices = consumer.scan(timeout=1.0, expected=1)
                if devices:
                    break
            report.check(bool(devices), "TLS provider is discoverable")
            if not devices:
                return
            device = next((candidate for candidate in devices if candidate.epr == provider.epr.urn), None)
            report.check(device is not None and device.x_addrs[0].startswith("https://"), "TLS provider advertises HTTPS")
            if device is None:
                return
            remote = consumer.connect(device)
            report.check(
                remote.peer_certificate is not None and remote.peer_certificate.common_name == "provider",
                "consumer receives and verifies the provider certificate",
            )
            report.check(handle in remote.metrics(), "mutually authenticated consumer retrieves the MDIB")
            metric_reports = CallbackRecorder(
                lambda values: {
                    metric_handle: getattr(getattr(state, "MetricValue", None), "Value", None)
                    for metric_handle, state in values.items()
                },
            )
            observableproperties.bind(remote.mdib, metrics_by_handle=metric_reports)
            cursor = metric_reports.cursor()
            result = remote.set_value(handle, Decimal("2"))
            report.check(str(result).endswith("Fin"), "mutually authenticated consumer can invoke a control operation")
            provider.set_value(handle, Decimal("3"))
            metric_event = metric_reports.wait_for(
                lambda values: values.get(handle) == Decimal("3"),
                after=cursor,
                timeout=5.0,
            )
            report.check(
                metric_event is not None,
                "provider event callback reaches the mutually authenticated consumer",
            )
        finally:
            if remote is not None:
                remote.close()
            consumer.stop()
            provider.stop()


def check_https_requirement(report: Report) -> None:
    with TemporaryDirectory(prefix="sdc-tls-http-") as raw_temporary:
        _, consumer_tls = make_tls_configurations(Path(raw_temporary))
        consumer = ConsumerService(tls_config=consumer_tls)
        insecure = DiscoveredDevice("insecure", ("http://127.0.0.1:1/device",), (), service=object())
        try:
            consumer.connect(insecure)
        except RuntimeError as exc:
            error = str(exc)
        else:
            error = ""
        report.check("TLS is required" in error, "TLS mode rejects HTTP endpoints before connection")


def check_untrusted_client_rejected(report: Report) -> None:
    with TemporaryDirectory(prefix="sdc-tls-untrusted-") as raw_temporary:
        temporary = Path(raw_temporary)
        provider_tls, _ = make_tls_configurations(temporary)
        other_ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Other Test CA")])
        other_ca = issue_certificate("Other Test CA", other_ca_key, other_ca_name, other_ca_key, ca=True)
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_cert = issue_certificate("untrusted", other_key, other_ca_name, other_ca_key, ip_name="127.0.0.1")
        other_ca_path = temporary / "other-ca.pem"
        other_cert_path = temporary / "untrusted.pem"
        other_key_path = temporary / "untrusted-key.pem"
        write_certificate(other_ca_path, other_ca)
        write_certificate(other_cert_path, other_cert)
        write_key(other_key_path, other_key)
        from sdctoolbox.security import TlsConfig  # noqa: PLC0415

        untrusted_tls = TlsConfig.from_paths(other_cert_path, other_key_path, provider_tls.ca_bundle_path)
        provider = ProviderService(instance_name="tls-rejection-provider", tls_config=provider_tls)
        consumer = ConsumerService(tls_config=untrusted_tls)
        consumer._tls_contexts = untrusted_tls.create_contexts()  # trust the server, so rejection tests client authentication
        try:
            provider.start()
            device = DiscoveredDevice(
                provider.epr.urn,
                (provider._provider.get_xaddrs()[0],),  # noqa: SLF001 - obtain a direct local test endpoint
                (),
                service=object(),
            )
            try:
                consumer.connect(device)
            except Exception:  # noqa: BLE001 - OpenSSL error wording is platform-specific
                rejected = True
            else:
                rejected = False
            report.check(rejected, "provider rejects a client certificate from an untrusted CA")
        finally:
            consumer.stop()
            provider.stop()


def main() -> int:
    report = Report()
    print("TLS acceptance")
    check_policy_before_metadata(report)
    check_transport_pins(report)
    check_callback_transport_policy(report)
    check_incomplete_handshake_shutdown(report)
    check_tls_shutdown_before_handle(report)
    check_tls_server_shutdown(report)
    check_unexpected_server_errors(report)
    errors = io.StringIO()
    with redirect_stderr(errors):
        check_tls_connection(report)
    if errors.getvalue():
        print(errors.getvalue(), file=sys.stderr, end="")
    report.check("Traceback" not in errors.getvalue(), "provider and consumer TLS teardown emits no unexpected traceback")
    check_https_requirement(report)
    check_untrusted_client_rejected(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
