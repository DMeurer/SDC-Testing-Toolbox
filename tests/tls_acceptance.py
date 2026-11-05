"""End-to-end mutual-TLS acceptance for the toolbox's provider and consumer services."""

from __future__ import annotations

import faulthandler
import io
import socket
import ssl
import sys
import threading
import time
from contextlib import redirect_stderr
from decimal import Decimal
from http.client import HTTPResponse
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from script_support import CallbackRecorder, Report  # noqa: E402
from sdc11073 import observableproperties  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService, DiscoveredDevice  # noqa: E402
from sdctoolbox.model import MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402
from security import issue_certificate, make_tls_configurations, write_certificate, write_key  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402


def wait_for_tls_reader(worker: threading.Thread) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        frame = sys._current_frames().get(worker.ident)
        if frame is not None and frame.f_code is ssl.SSLSocket.read.__code__:
            return
        time.sleep(0.01)
    raise AssertionError("request worker did not enter the blocking TLS read")


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
        for failure in (RuntimeError("unexpected request failure"), BrokenPipeError(10058, "unexpected response failure")):
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
            remote.close()
        finally:
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

        untrusted_tls = TlsConfig.from_paths(other_cert_path, other_key_path, other_ca_path)
        provider = ProviderService(instance_name="tls-rejection-provider", tls_config=provider_tls)
        consumer = ConsumerService(tls_config=untrusted_tls)
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
