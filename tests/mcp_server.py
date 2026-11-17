"""The MCP server, over real HTTP: without a window, and driving a window whose views follow.

Usage:  .venv/Scripts/python.exe tests/mcp_server.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import run_toolbox  # noqa: E402
from sdctoolbox import config, mdib_check  # noqa: E402
from sdctoolbox.gui.activity_log import activity_log  # noqa: E402
from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.gui.mcp_bridge import start_mcp_server  # noqa: E402
from sdctoolbox.mcp import guide  # noqa: E402
from sdctoolbox.mdib_io import NativeMdibConfig  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

BEHAVIOUR_NS = "urn:sdc-testing-toolbox:behaviour"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Client:
    """Just enough of an MCP client: JSON-RPC over POST."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._id = 0

    def post(self, body: object, headers: dict[str, str] | None = None) -> tuple[int, dict | None]:
        request = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                     **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - a local test URL
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        status, reply = self.post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        assert status == 200, (status, reply)
        return reply

    def tool(self, tool_name: str, /, **arguments: object) -> dict:
        reply = self.rpc("tools/call", {"name": tool_name, "arguments": arguments})
        assert "result" in reply, reply
        return reply["result"]

    def ok(self, tool_name: str, /, **arguments: object) -> object:
        result = self.tool(tool_name, **arguments)
        text = result["content"][0]["text"]
        assert not result["isError"], f"{tool_name}: {text}"
        return result.get("structuredContent", text)


class HeadlessTests(unittest.TestCase):
    """run_toolbox.py --headless --mcp, as an agent would start it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.port = free_port()
        cls.process = subprocess.Popen(
            [sys.executable, "run_toolbox.py", "--headless", "--mcp", str(cls.port), "--name", "mcp-headless",
             "--config", "presets/insufflator.xml"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 60
        cls.output = []
        while time.monotonic() < deadline:
            line = cls.process.stdout.readline()
            if not line:
                break
            cls.output.append(line)
            if line.startswith("MCP server:"):
                break
        assert any(line.startswith("MCP server:") for line in cls.output), "".join(cls.output)
        cls.client = Client(f"http://127.0.0.1:{cls.port}/mcp")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        cls.process.wait(timeout=30)
        cls.process.stdout.close()

    def test_handshake_lists_tools_and_guide_resources(self) -> None:
        reply = self.client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("SDC Testing Toolbox", reply["result"]["instructions"])
        status, body = self.client.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, None))
        names = {tool["name"] for tool in self.client.rpc("tools/list")["result"]["tools"]}
        self.assertLessEqual({"add_metric", "build_device", "get_mdib", "connect", "explain_gui", "set_view"}, names)
        resources = self.client.rpc("resources/list")["result"]["resources"]
        self.assertEqual(len(resources), len(guide.TOPICS))
        text = self.client.rpc("resources/read", {"uri": resources[0]["uri"]})["result"]["contents"][0]["text"]
        self.assertTrue(text.startswith("# "))

    def test_the_loaded_preset_is_the_device(self) -> None:
        overview = self.client.ok("overview")
        self.assertEqual(overview["mode"], "headless")
        self.assertEqual(overview["my_device"]["preset"], "Insufflator")

    def test_build_a_device_and_get_its_mdib(self) -> None:
        added = self.client.ok("add_metric", metric={
            "label": "Tidal volume", "kind": "number", "unit": "mL", "minimum": 0, "maximum": 2000,
            "resolution": 0.1, "controllable": True, "handle": "m.tidal_volume"})
        self.assertEqual((added["handle"], added["resolution"]), ("m.tidal_volume", "0.1"))
        self.client.ok("add_alarm", alarm={"label": "Volume high", "watches": "m.tidal_volume",
                                           "upper_limit": 900, "priority": "Hi"})
        self.client.ok("set_value", handle="m.tidal_volume", value="950")
        alarm = next(a for a in self.client.ok("list_alarms")["alarms"] if a["watches"] == "m.tidal_volume")
        self.assertTrue(alarm["present"], "a value past the limit raises the alarm")
        xml = self.client.ok("get_mdib")
        self.assertIn("m.tidal_volume", xml)
        self.assertIn(BEHAVIOUR_NS, xml)
        self.assertNotIn(BEHAVIOUR_NS, self.client.ok("get_mdib", behaviour=False))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "built.xml"
            self.assertTrue(self.client.ok("export_mdib", path=str(target))["behaviour"])
            self.assertIsInstance(config.load_file(target), NativeMdibConfig)
            refused = self.client.tool("export_mdib", path=str(Path(directory) / "built.json"))
            self.assertTrue(refused["isError"])

    def test_check_mdib_lists_findings_for_my_device(self) -> None:
        self.assertIn("check_mdib", self.client.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                  "clientInfo": {"name": "t", "version": "1"}})
                      ["result"]["instructions"])
        result = self.client.ok("check_mdib")
        self.assertEqual(result["checked"], "My device")
        self.assertIn("10700", result["sources"])
        self.assertTrue(result["findings"], "the insufflator preset has findings")
        first = result["findings"][0]
        self.assertLessEqual({"level", "area", "handle", "text", "rule", "label"}, set(first))
        self.assertEqual(first["level"], "error", "errors come first")
        refused = self.client.tool("check_mdib", connection=99)
        self.assertTrue(refused["isError"])
        self.assertIn("not connected", refused["content"][0]["text"])

    def test_search_codes_gives_the_type_and_unit_of_a_metric(self) -> None:
        measured = self.client.ok("search_codes", text="pulse rate", kind="measurement")["codes"]
        self.assertEqual(measured[0]["reference_id"], "MDC_PULS_RATE")
        self.assertTrue(all(code["kind"] == "measurement" for code in measured))
        unit = self.client.ok("search_codes", text="bpm", kind="unit")["codes"][0]
        self.assertEqual((unit["code"], unit["label"]), ("264864", "bpm"))
        pick = {key: measured[0][key] for key in ("code", "system", "label")}
        self.client.ok("add_metric", metric={
            "label": "Pulse", "kind": "number", "handle": "m.coded_pulse", "type": pick,
            "unit": {key: unit[key] for key in ("code", "system", "label")}})
        xml = self.client.ok("get_mdib", behaviour=False)
        descriptor = xml[xml.index('Handle="m.coded_pulse"'):]
        self.assertIn('Code="149514"', descriptor[:descriptor.index("</")])
        self.assertIn('Code="264864"', descriptor)
        self.assertIn("Load codes.csv", self.client.ok("explain_gui", topic="code picker"))

    def test_mistakes_come_back_as_tool_errors(self) -> None:
        for name, arguments, fragment in (
            ("set_value", {"handle": "m.nothing", "value": 1}, "no data source"),
            ("add_metric", {"metric": {"label": "x", "kind": "colour"}}, "colour"),
            ("set_view", {"widgets": False}, "needs the window"),
            ("load_preset", {"name": "toaster"}, "no preset"),
            ("overview", {"bogus": 1}, "unknown argument"),
            ("search_codes", {"text": "rate", "kind": "colour"}, "colour"),
        ):
            result = self.client.tool(name, **arguments)
            self.assertTrue(result["isError"], name)
            self.assertIn(fragment, result["content"][0]["text"], name)

    def test_web_pages_and_other_methods_are_refused(self) -> None:
        status, _ = self.client.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                     headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, reply = self.client.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                         headers={"Origin": "http://localhost:3000"})
        self.assertEqual((status, reply["result"]), (200, {}))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.client.url, timeout=10)  # noqa: S310 - a local test URL
        self.assertEqual(caught.exception.code, 405)
        self.assertEqual(self.client.rpc("no/such")["error"]["code"], -32601)

    def test_headless_needs_mcp(self) -> None:
        result = subprocess.run([sys.executable, "run_toolbox.py", "--headless"], cwd=ROOT, capture_output=True,
                                text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--headless needs --mcp", result.stderr)


class WindowTests(unittest.TestCase):
    """--mcp with a window: every change shows up in the window the user is looking at."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.service = ProviderService(instance_name="mcp-window")
        cls.service.start()
        cls.window = MainWindow(cls.service)
        cls.window.show()
        cls.server = start_mcp_server(cls.window, free_port())
        cls.client = Client(cls.server.url)
        cls.peer = ProviderService(instance_name="mcp-peer")
        cls.peer.start(config_mdib("insufflator.xml"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()
        cls.window.close()
        cls.peer.stop()
        cls.service.stop()

    def call(self, tool_name: str, /, **arguments: object) -> object:
        """Send from another thread and keep the GUI thread turning, as with a real agent."""
        box: dict[str, object] = {}
        thread = threading.Thread(target=lambda: box.update(result=self.client.tool(tool_name, **arguments)))
        thread.start()
        while thread.is_alive():
            self.app.processEvents()
            time.sleep(0.005)
        for _ in range(5):
            self.app.processEvents()
        result = box["result"]
        self.assertFalse(result["isError"], result["content"][0]["text"])
        return result.get("structuredContent", result["content"][0]["text"])

    def test_status_line_names_the_server(self) -> None:
        self.assertIn(f"MCP on {self.server.url}", self.window.status_hint.text())

    def test_my_device_views_follow_mcp_changes(self) -> None:
        self.call("add_metric", metric={"label": "Pulse", "kind": "number", "minimum": 0, "maximum": 250,
                                        "handle": "m.pulse"}, card={"control": "StepperWidget"})
        pane = self.window.provider_pane
        self.assertIn("m.pulse", pane.board.handles)
        self.assertEqual(pane.board.card_settings("m.pulse").control, "StepperWidget")
        self.call("set_value", handle="m.pulse", value=72)
        self.assertEqual(str(self.service.get_value("m.pulse")), "72")
        self.call("add_alarm", alarm={"label": "Pulse high", "watches": "m.pulse", "upper_limit": 150})
        self.assertEqual(pane.alert_table.rowCount(), len(self.service.list_alerts()))
        self.call("set_card_settings", handle="m.pulse", settings={"control": "SliderWidget", "show_ranges": False})
        self.assertEqual(pane.board.card_settings("m.pulse").control, "SliderWidget")
        self.assertFalse(self.service.card_settings["m.pulse"].show_ranges, "an export stores the card")
        texts = [entry.text for entry in activity_log().entries]
        self.assertIn("MCP: added data source Pulse (m.pulse)", texts)

    def test_a_preset_replaces_the_pane(self) -> None:
        before = self.window.provider_pane
        self.call("load_preset", name="Patient monitor")
        self.assertIsNot(self.window.provider_pane, before, "the window builds a new pane for the new MDIB")
        self.assertIn("m.heart_rate", self.window.provider_pane.board.handles)

    def test_view_options_go_through_the_menu(self) -> None:
        view = self.call("set_view", widgets=False, split_view=True)
        self.assertEqual((view["widgets"], view["split_view"]), (False, True))
        self.assertFalse(self.window.widgets_action.isChecked())
        self.assertTrue(self.window.split_view_action.isChecked())
        self.call("set_view", widgets=True, split_view=False, tab="network", activity_log=True)
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertTrue(self.window.log_window.isVisible())

    def test_explain_gui_answers_from_the_guide(self) -> None:
        self.assertIn("tangible (Tan)", self.call("explain_gui", topic="signal icons"))
        self.assertIn("SliderWidget", self.call("explain_gui", topic="what control types can a card have"))
        self.assertIn("topics", self.call("explain_sdc"))

    def test_network_tabs_follow_mcp(self) -> None:
        epr = self.peer.epr.urn
        devices = []
        deadline = time.monotonic() + 30
        while epr not in devices and time.monotonic() < deadline:
            devices = [device["epr"] for device in self.call("scan", timeout=3)["devices"]]
        self.assertIn(epr, devices)
        number = self.call("connect", epr=epr)["connection"]
        pane = next(p for p in self.window.network_tabs.panes if p.property("connectionNumber") == number)
        self.assertIsNotNone(pane.remote, "the connection opens in a Network tab")
        values = {m["handle"]: m for m in self.call("read_metrics", connection=number)["metrics"]}
        self.assertIn("m.target_pressure", values)
        refused = self.call("remote_set_value", connection=number, handle="m.target_pressure", value=500)
        self.assertFalse(refused["accepted"])
        self.assertIn("500", refused["device_said"])
        accepted = self.call("remote_set_value", connection=number, handle="m.target_pressure", value=14)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(str(self.peer.get_value("m.target_pressure")), "14")
        self.call("set_card_settings", connection=number, handle="m.target_pressure", settings={"control": "StepperWidget"})
        self.assertEqual(pane.board.card_settings("m.target_pressure").control, "StepperWidget")
        checked = self.call("check_mdib", connection=number)
        self.assertEqual(checked["checked"], f"connection {number}")
        self.assertEqual(len(checked["findings"]), len(mdib_check.check(pane.remote.mdib)))
        self.check_a_connection_in_the_window(pane)
        self.call("disconnect", connection=number)
        self.assertNotIn(pane, self.window.network_tabs.panes)

    def check_a_connection_in_the_window(self, pane) -> None:  # noqa: ANN001
        """The MDIB check window lists the open connection and shows its elements in its tab."""
        from PySide6.QtCore import Qt

        check_window = self.window.show_mdib_check()
        number = pane.property("connectionNumber")
        check_window.refresh_targets()
        index = check_window.target_box.findData(number)
        self.assertGreater(index, 0, "the connection is listed by its tab")
        self.assertTrue(check_window.target_box.itemText(index).startswith(f"{number} · "))
        check_window.target_box.setCurrentIndex(index)
        check_window.run()
        findings = mdib_check.check(pane.remote.mdib)
        self.assertEqual(check_window.tree.topLevelItemCount(), len(findings))
        handle = next(f.handle for f in findings if f.handle in pane.board.handles)
        item = next(check_window.tree.topLevelItem(i) for i in range(check_window.tree.topLevelItemCount())
                    if check_window.tree.topLevelItem(i).data(0, Qt.UserRole) == handle)
        self.window.tabs.setCurrentIndex(0)
        self.assertTrue(check_window.show_element(item))
        self.assertEqual(self.window.tabs.currentIndex(), 1, "the Network tab comes to the front")
        self.assertIs(self.window.network_tabs.current_pane, pane)
        self.assertEqual(pane.selected_handle(), handle)
        check_window.target_box.setCurrentIndex(0)

    def test_check_mdib_matches_the_status_bar(self) -> None:
        result = self.call("check_mdib")
        self.assertEqual(result["summary"], mdib_check.summary(mdib_check.check(self.service.mdib)))
        self.window.recheck_my_device()
        self.assertEqual(result["summary"], mdib_check.summary(self.window.mdib_findings))
        self.assertIn("MDIB check", self.call("explain_gui", topic="mdib check"))

    def test_a_port_in_use_is_reported_and_the_window_carries_on(self) -> None:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            with patch.object(QMessageBox, "warning") as warning:
                self.assertIsNone(run_toolbox.start_window_mcp(self.window, taken.getsockname()[1]))
        self.assertEqual(warning.call_args.args[1], "Could not start the MCP server")


def config_mdib(name: str):  # noqa: ANN201
    from sdctoolbox.mdib_io import parse_mdib

    return parse_mdib((ROOT / "presets" / name).read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
