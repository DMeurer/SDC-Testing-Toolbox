"""The toolbox listens only inside its fixed port range, so shipped firewall rules cover it.

Run under the shared network-test mutex: providers and consumers use loopback WS-Discovery.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import sys
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from security import make_tls_configurations
from consumer_lifecycle import FakeBridge, FakeConsumerService
from gui_test_support import application, pump, wait_for
import shiboken6
from sdc11073.consumer import consumerimpl
from sdc11073.provider import providerimpl
from sdc11073.wsdiscovery import networkingthread

from sdctoolbox import constants, ports
from sdctoolbox.consumer_service import ConsumerService
from sdctoolbox.gui import consumer_pane as cp
from sdctoolbox.gui.main_window import MainWindow
from sdctoolbox.provider_service import ProviderService

LOOPBACK = "127.0.0.1"


def provider_port(service: ProviderService) -> int:
    return service._provider._http_server.server_port  # noqa: SLF001


def free_port_pair() -> int:
    """Return a port p whose neighbour p + 1 still exists, both picked by the OS."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        port = probe.getsockname()[1]
    return min(port, 65534)


def occupy(port: int, kind: str, *, reuse: bool = False) -> socket.socket | None:
    """Hold a port the way another program would; None if it is already taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM if kind == "TCP" else socket.SOCK_DGRAM)
    if reuse:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((LOOPBACK, port))
        if kind == "TCP":
            sock.listen()
    except OSError:
        sock.close()
        return None
    return sock


@contextmanager
def occupied_range(first: int, count: int, kind: str, *, reuse: bool = False):
    """Patch the toolbox range to ``count`` ports from ``first`` and hold every one of them."""
    with ExitStack() as stack:
        for port in range(first, first + count):
            held = occupy(port, kind, reuse=reuse)
            if held is not None:
                stack.callback(held.close)
        patched = range(first, first + count)
        stack.enter_context(patch.object(constants, "PORT_RANGE", patched))
        yield patched


class TcpPortRange(unittest.TestCase):
    def start_provider(self, name: str, **kwargs) -> ProviderService:
        service = ProviderService(ip=LOOPBACK, instance_name=name, **kwargs)
        self.addCleanup(service.stop)
        service.start()
        return service

    def test_provider_http_server_binds_inside_range(self) -> None:
        service = self.start_provider("port-range-provider")
        self.assertIn(provider_port(service), constants.PORT_RANGE)

    def test_two_providers_get_distinct_ports(self) -> None:
        first = self.start_provider("port-range-first")
        second = self.start_provider("port-range-second")
        self.assertIn(provider_port(first), constants.PORT_RANGE)
        self.assertIn(provider_port(second), constants.PORT_RANGE)
        self.assertNotEqual(provider_port(first), provider_port(second))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux SO_REUSEADDR bind/listen race")
    def test_another_listener_winning_after_bind_does_not_abort_startup(self) -> None:
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted), ExitStack() as stack:
                if exhausted:
                    first = free_port_pair()
                    stack.enter_context(patch.object(constants, "PORT_RANGE", range(first, first + 1)))
                rival = stack.enter_context(socket.socket())
                rival.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                original_bind = ports.bind_in_range
                contested_port = None
                first_socket = None

                def bind_then_lose_race(sock, ip, protocol):
                    nonlocal contested_port, first_socket
                    port = original_bind(sock, ip, protocol)
                    if contested_port is None:
                        contested_port = port
                        first_socket = sock
                        rival.bind((ip, port))
                        rival.listen()
                    return port

                stack.enter_context(patch.object(ports, "bind_in_range", bind_then_lose_race))
                if exhausted:
                    logs = stack.enter_context(self.assertLogs("sdctoolbox.ports", "WARNING"))
                server = ports._RangeThreadingHTTPServer(  # noqa: SLF001
                    logging.getLogger("port-range-race"), (LOOPBACK, 0), 0, ["gzip"]
                )
                stack.callback(server.server_close)
                self.assertNotEqual(server.server_port, contested_port)
                self.assertEqual(first_socket.fileno(), -1, "the failed socket is closed")
                if exhausted:
                    self.assertNotIn(server.server_port, constants.PORT_RANGE)
                    self.assertIn(f"TCP port {server.server_port} is outside", logs.output[0])
                else:
                    self.assertIn(server.server_port, constants.PORT_RANGE)
                with socket.create_connection(server.server_address, timeout=2):
                    server.socket.settimeout(2)
                    accepted, _ = server.socket.accept()
                    accepted.close()

    def test_unrelated_listen_error_surfaces_and_closes_socket(self) -> None:
        error = OSError(errno.EBADF, "invalid socket")
        sockets = []

        def fail_listen(server):
            sockets.append(server.socket)
            raise error

        with patch.object(ports._ThreadingHTTPServer, "server_activate", fail_listen):  # noqa: SLF001
            with self.assertRaises(OSError) as raised:
                ports._RangeThreadingHTTPServer(  # noqa: SLF001
                    logging.getLogger("port-range-error"), (LOOPBACK, 0), 0, ["gzip"]
                )
        self.assertIs(raised.exception, error)
        self.assertEqual(len(sockets), 1, "an unrelated error is not retried")
        self.assertEqual(sockets[0].fileno(), -1)

    def test_tls_provider_binds_inside_range(self) -> None:
        with TemporaryDirectory(prefix="sdc-port-range-") as raw:
            provider_tls, _ = make_tls_configurations(Path(raw))
            service = self.start_provider("port-range-tls", tls_config=provider_tls)
            self.assertIn(provider_port(service), constants.PORT_RANGE)

    def test_consumer_event_sink_binds_inside_range(self) -> None:
        provider = self.start_provider("port-range-sink")
        consumer = ConsumerService(LOOPBACK)
        self.addCleanup(consumer.stop)
        consumer.start()
        device = None
        deadline = time.monotonic() + 10.0
        while device is None and time.monotonic() < deadline:
            devices = consumer.scan(timeout=1.0, expected=1)
            device = next((d for d in devices if d.epr == provider.epr.urn), None)
        self.assertIsNotNone(device, "the provider is discoverable")
        remote = consumer.connect(device)
        self.addCleanup(remote.close)
        sink_port = remote._consumer._http_server.server_port  # noqa: SLF001
        self.assertIn(sink_port, constants.PORT_RANGE)

    def test_library_hooks_use_range_server(self) -> None:
        self.assertIs(providerimpl.HttpServerThreadBase, ports.RangeHttpServerThread)
        self.assertIs(consumerimpl.HttpServerThreadBase, ports.RangeHttpServerThread)

    def test_restart_rebinds_inside_range(self) -> None:
        service = self.start_provider("port-range-restart")
        first_port = provider_port(service)
        # The server answers and closes first, so the old port lingers in TIME_WAIT on its side.
        with socket.create_connection((LOOPBACK, first_port), timeout=5) as client:
            client.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
            client.recv(1024)
        service.stop()
        started = time.monotonic()
        service.start()
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertIn(provider_port(service), constants.PORT_RANGE)

    def test_exhausted_tcp_range_falls_back_with_warning(self) -> None:
        with occupied_range(free_port_pair(), 2, "TCP") as patched, socket.socket() as sock:
            with self.assertLogs("sdctoolbox.ports", "WARNING") as logs:
                port = ports.bind_in_range(sock, LOOPBACK, "TCP")
        self.assertNotIn(port, patched)
        self.assertIn(str(port), logs.output[0])
        self.assertIn("firewall", logs.output[0])
        expected = (
            f"TCP port {port} is outside {patched[0]}-{patched[-1]} because no port in that range "
            "was free; the shipped firewall definitions do not cover it"
        )
        self.assertEqual(ports.fallback_warnings()[-1], expected)

    def test_occupied_tcp_port_is_not_shared(self) -> None:
        with occupied_range(free_port_pair(), 1, "TCP", reuse=True) as patched, socket.socket() as sock:
            with self.assertLogs("sdctoolbox.ports", "WARNING"):
                port = ports.bind_in_range(sock, LOOPBACK, "TCP")
        self.assertNotIn(port, patched)

    def test_non_local_address_fails_fast(self) -> None:
        before = ports.fallback_warnings()
        started = time.monotonic()
        with socket.socket() as sock, self.assertRaises(OSError) as raised:
            ports.bind_in_range(sock, "192.0.2.1", "TCP")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertNotIn(raised.exception.errno, (errno.EADDRINUSE, errno.EACCES))
        self.assertEqual(ports.fallback_warnings(), before)
        server = ports.RangeHttpServerThread("192.0.2.1", None, ["gzip"], logging.getLogger("port-range"))
        with self.assertRaises(OSError):
            server.start()
        self.assertLess(time.monotonic() - started, 2.0)


def discovery_port(discovery) -> int:  # noqa: ANN001
    return discovery._networking_thread.multi_out_uni_in_out.getsockname()[1]  # noqa: SLF001


class UdpPortRange(unittest.TestCase):
    def start_discovery(self) -> ports.RangeWSDiscovery:
        discovery = ports.RangeWSDiscovery(LOOPBACK)
        discovery.start()
        self.addCleanup(discovery.stop)
        return discovery

    def test_provider_discovery_socket_binds_inside_range(self) -> None:
        service = ProviderService(ip=LOOPBACK, instance_name="port-range-udp-provider")
        self.addCleanup(service.stop)
        service.start()
        self.assertIn(discovery_port(service._discovery), constants.PORT_RANGE)  # noqa: SLF001

    def test_consumer_discovery_socket_binds_inside_range(self) -> None:
        consumer = ConsumerService(LOOPBACK)
        self.addCleanup(consumer.stop)
        consumer.start()
        self.assertIn(discovery_port(consumer._discovery), constants.PORT_RANGE)  # noqa: SLF001

    def test_exhausted_udp_range_falls_back_with_warning(self) -> None:
        with occupied_range(free_port_pair(), 2, "UDP") as patched:
            with self.assertLogs("sdctoolbox.ports", "WARNING") as logs:
                port = discovery_port(self.start_discovery())
        self.assertNotIn(port, patched)
        self.assertTrue(any(f"UDP port {port} is outside" in line for line in logs.output))

    def test_occupied_udp_port_is_not_shared(self) -> None:
        with occupied_range(free_port_pair(), 1, "UDP", reuse=True) as patched:
            with self.assertLogs("sdctoolbox.ports", "WARNING"):
                port = discovery_port(self.start_discovery())
        self.assertNotIn(port, patched)

    def test_consumer_still_finds_provider(self) -> None:
        provider = ProviderService(ip=LOOPBACK, instance_name="port-range-udp-find")
        self.addCleanup(provider.stop)
        provider.start()
        consumer = ConsumerService(LOOPBACK)
        self.addCleanup(consumer.stop)
        consumer.start()
        found = False
        deadline = time.monotonic() + 10.0
        while not found and time.monotonic() < deadline:
            found = any(d.epr == provider.epr.urn for d in consumer.scan(timeout=1.0, expected=1))
        self.assertTrue(found)


def force_fallback() -> None:
    """Make one bind fall back by filling a one-port range."""
    with occupied_range(free_port_pair(), 1, "TCP"), socket.socket() as sock:
        ports.bind_in_range(sock, LOOPBACK, "TCP")


def discovery_datagram(action: str, message_id: str) -> bytes:
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://www.w3.org/2005/08/addressing">'
        f"<s:Header><a:Action>http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01/{action}</a:Action>"
        f"<a:MessageID>urn:uuid:{message_id}</a:MessageID></s:Header><s:Body/></s:Envelope>"
    ).encode()


class DiscoveryIntake(unittest.TestCase):
    """Every discovery socket on a host gets every multicast datagram, each sent up to five times.

    With hundreds of providers, parsing all of that is what made startup slow.
    """

    def received(self, discovery, datagrams: list[bytes]) -> list[bytes]:  # noqa: ANN001
        """The datagrams that are handed on for parsing, in order."""
        passed: list[bytes] = []
        with patch.object(networkingthread.NetworkingThread, "_add_to_recv_queue",
                          lambda _self, _addr, data: passed.append(data)):
            for data in datagrams:
                discovery._networking_thread._add_to_recv_queue((LOOPBACK, 3702), data)  # noqa: SLF001
        return [data for data in passed if data in datagrams]

    def start(self, discovery):  # noqa: ANN001, ANN201
        discovery.start()
        self.addCleanup(discovery.stop)
        return discovery

    def test_repeated_datagrams_are_parsed_once(self) -> None:
        discovery = self.start(ports.RangeWSDiscovery(LOOPBACK))
        hello, other = discovery_datagram("Hello", "repeat-1"), discovery_datagram("Hello", "repeat-2")
        self.assertEqual(self.received(discovery, [hello] * 5 + [other]), [hello, other])

    def test_consumer_discovery_still_reads_announcements(self) -> None:
        discovery = self.start(ports.RangeWSDiscovery(LOOPBACK))
        messages = [discovery_datagram(action, f"consumer-{action}") for action in ("Hello", "Bye", "ProbeMatches")]
        self.assertEqual(self.received(discovery, messages), messages)

    def test_provider_discovery_only_reads_what_it_answers(self) -> None:
        service = ProviderService(ip=LOOPBACK, instance_name="port-range-intake")
        self.addCleanup(service.stop)
        service.start()
        ignored = [discovery_datagram(action, f"provider-{action}")
                   for action in ("Hello", "Bye", "ProbeMatches", "ResolveMatches")]
        answered = [discovery_datagram(action, f"provider-{action}") for action in ("Probe", "Resolve")]
        unknown = [b"<not-discovery/>"]
        self.assertEqual(self.received(service._discovery, ignored + answered + unknown), answered + unknown)  # noqa: SLF001


class PortWarningInMainWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = application()

    def setUp(self) -> None:
        for replacement in (
            patch.object(cp, "ConsumerService", FakeConsumerService),
            patch.object(cp, "MdibBridge", FakeBridge),
            patch.object(ports, "_warnings", []),  # each test starts with no warning so far
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.provider = ProviderService(ip=LOOPBACK, instance_name="port-range-gui")
        self.addCleanup(self.provider.stop)
        self.provider.start()

    def open_window(self) -> MainWindow:
        window = MainWindow(self.provider)
        window.show()

        def dispose() -> None:
            if shiboken6.isValid(window):
                if window.isVisible():
                    window.close()
                for pane in window.network_tabs.panes:
                    pane._worker.wait(5.0)  # noqa: SLF001
                shiboken6.delete(window)
            self.app.processEvents()

        self.addCleanup(dispose)
        return window

    def test_main_window_shows_earlier_port_warning(self) -> None:
        with self.assertLogs("sdctoolbox.ports", "WARNING"):
            force_fallback()
        window = self.open_window()
        pump(self.app)
        label = window.port_warning_label
        self.assertTrue(label.isVisible())
        self.assertEqual(label.text(), ports.fallback_warnings()[-1])
        self.assertEqual(label.toolTip(), ports.fallback_warnings()[-1])

    def test_main_window_shows_warning_from_worker_thread(self) -> None:
        window = self.open_window()
        pump(self.app)
        label = window.port_warning_label
        self.assertFalse(label.isVisible(), "no warning, no label")
        with self.assertLogs("sdctoolbox.ports", "WARNING"):
            worker = threading.Thread(target=force_fallback)
            worker.start()
            worker.join(10.0)
        self.assertTrue(wait_for(self.app, lambda: label.text() == ports.fallback_warnings()[-1]))
        self.assertTrue(label.isVisible())

    def test_main_window_unsubscribes_on_close(self) -> None:
        before = len(ports._subscribers)  # noqa: SLF001
        window = self.open_window()
        self.assertEqual(len(ports._subscribers), before + 1)  # noqa: SLF001
        window.close()
        self.assertEqual(len(ports._subscribers), before)  # noqa: SLF001
        with self.assertLogs("sdctoolbox.ports", "WARNING"):
            force_fallback()


if __name__ == "__main__":
    unittest.main(verbosity=2)
