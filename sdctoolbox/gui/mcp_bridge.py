"""The MCP server with a window: agent calls run on the GUI thread and show up in the window.

The server answers each request on its own thread. Anything touching My device or a widget is
handed to the GUI thread and waited for, so an agent's change goes through the same paths a
click does and the window redraws itself. Slow network work (scanning, connecting, a remote
write) stays on the request thread, so the window never freezes while it waits.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt, QThread, Signal

from .. import config
from ..mcp.server import McpApplication, McpHttpServer, ToolError
from ..mcp.tools import CONNECT_TIMEOUT_SECONDS, Hooks, ToolboxTools, card_from_dict, card_to_dict, wait_for
from .activity_log import activity_log
from .provider_pane import OWN_DEVICE

if TYPE_CHECKING:
    from ..consumer_service import DiscoveredDevice, RemoteDevice
    from ..mdib_io import NativeMdibConfig
    from ..model import CardSettings
    from .consumer_pane import ConsumerPane
    from .main_window import MainWindow


class _Invoker(QObject):
    """Runs a function on the thread this object lives on and hands back what it returned."""

    _request = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._request.connect(self._run, Qt.BlockingQueuedConnection)

    def run(self, function: Callable[[], Any]) -> Any:
        if QThread.currentThread() is self.thread():
            return function()  # a blocking emit to our own thread would wait forever
        box: dict[str, Any] = {}
        self._request.emit((function, box))
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _run(self, job: tuple[Callable[[], Any], dict[str, Any]]) -> None:
        function, box = job
        try:
            box["result"] = function()
        except Exception as exc:  # noqa: BLE001 - carried back to the calling thread
            box["error"] = exc


class GuiHooks(Hooks):
    has_window = True

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window.service)
        self.window = window
        self.invoker = _Invoker()

    def on_gui(self, function: Callable[[], Any]) -> Any:
        return self.invoker.run(function)

    def changed(self, note: str) -> None:
        pane = self.window.provider_pane
        pane.refresh()
        pane.refresh_alerts()
        pane.refresh_actions()
        pane.refresh_contexts()
        activity_log().add(OWN_DEVICE, f"MCP: {note}")
        self.window.statusBar().showMessage(f"MCP: {note}", 8000)

    def set_card_settings(self, handle: str, settings: CardSettings) -> None:
        self.window.provider_pane._set_card_settings(handle, settings)  # noqa: SLF001 - the pane's own path

    def replace_device(self, device: config.DeviceConfig | NativeMdibConfig, source: str) -> tuple[int, int]:
        # MainWindow.apply_config would report a failure in a message box the agent never sees.
        previous = getattr(self.service, "_mdib", None)
        try:
            return config.apply_to(self.service, device, replace=True)
        finally:
            if getattr(self.service, "_mdib", None) is not previous:
                self.window._replace_provider_pane()  # noqa: SLF001 - the window's own path


class GuiNetwork:
    """The Network side as the window's connection tabs, numbered like their Tab N chips."""

    def __init__(self, window: MainWindow, hooks: GuiHooks) -> None:
        self.window = window
        self.on_gui = hooks.on_gui

    @property
    def tabs(self):  # noqa: ANN201
        return self.window.network_tabs

    def _pane(self, number: int) -> ConsumerPane:
        for pane in self.tabs.panes:
            if pane.property("connectionNumber") == number:
                return pane
        raise ToolError(f"no connection {number}; see list_connections")

    def scan(self, timeout: float) -> list[DiscoveredDevice]:
        pane = self.on_gui(lambda: self.tabs.current_pane)
        found = pane.service.scan(timeout=timeout, expected=99)
        self.on_gui(pane._refresh_devices)  # noqa: SLF001 - show what was found in the list
        return found

    def known(self) -> list[DiscoveredDevice]:
        return self.on_gui(lambda: self.tabs.current_pane.service.known_devices())

    def connect(self, device: DiscoveredDevice) -> int:
        def start() -> tuple[ConsumerPane, bool]:
            for pane in self.tabs.panes:
                if pane.remote is not None and pane.remote.epr == device.epr:
                    self.tabs.setCurrentWidget(pane)
                    return pane, False
            pane = self.tabs.current_pane
            if pane.remote is not None:
                pane = self.tabs.add_connection()
            pane.connect_to(device)
            return pane, True

        pane, started = self.on_gui(start)
        if started and not wait_for(lambda: self.on_gui(lambda: pane.remote is not None or self._failed(pane)),
                                    CONNECT_TIMEOUT_SECONDS):
            raise ToolError(f"connecting to {device.epr} took longer than {CONNECT_TIMEOUT_SECONDS:.0f} s")
        if self.on_gui(lambda: pane.remote is None):
            raise ToolError(self.on_gui(lambda: pane.status_label.text()))
        return self.on_gui(lambda: pane.property("connectionNumber"))

    @staticmethod
    def _failed(pane: ConsumerPane) -> bool:
        return pane.status_label.text().startswith("Could not connect")

    def connections(self) -> dict[int, RemoteDevice | None]:
        return self.on_gui(lambda: {pane.property("connectionNumber"): pane.remote for pane in self.tabs.panes})

    def status(self, number: int) -> str:
        return self.on_gui(lambda: self._pane(number).status_label.text())

    def disconnect(self, number: int) -> None:
        def work() -> None:
            pane = self._pane(number)
            self.tabs.close_connection(self.tabs.indexOf(pane))

        self.on_gui(work)

    def close(self) -> None:
        """The window owns its connections and closes them itself."""


class GuiTools(ToolboxTools):
    """The tools that need a window: view options, cards on connections, the activity log."""

    hooks: GuiHooks
    network: GuiNetwork

    @property
    def window(self) -> MainWindow:
        return self.hooks.window

    def get_view(self) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            window = self.window
            current = window.network_tabs.current_pane
            return {
                "tab": "network" if window.tabs.currentIndex() == 1 else "my_device",
                "split_view": window.split_view_enabled,
                "widgets": window.widgets_enabled,
                "dark_mode": window.dark_mode_action.isChecked(),
                "always_show_menu_bar": window.always_show_menu,
                "fullscreen": window.isFullScreen(),
                "current_connection": current.property("connectionNumber") if current is not None else None,
                "activity_log_open": window.log_window is not None and window.log_window.isVisible(),
            }

        return self.hooks.on_gui(work)

    def set_view(self, **options: Any) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            window = self.window
            # Through the menu actions, so their check marks stay true.
            for name, action in (("split_view", window.split_view_action), ("widgets", window.widgets_action),
                                 ("dark_mode", window.dark_mode_action)):
                if options.get(name) is not None:
                    action.setChecked(bool(options[name]))
            if options.get("connection") is not None:
                window.network_tabs.setCurrentWidget(self.network._pane(options["connection"]))  # noqa: SLF001
                if not window.split_view_enabled:
                    window.tabs.setCurrentIndex(1)
            if options.get("tab") is not None and not window.split_view_enabled:
                window.tabs.setCurrentIndex(1 if options["tab"] == "network" else 0)
            if options.get("activity_log"):
                window.show_activity_log()
            return self.get_view()

        return self.hooks.on_gui(work)

    def _network_board(self, connection: int, handle: str):  # noqa: ANN202
        board = self.network._pane(connection).board  # noqa: SLF001
        if handle not in board.handles:
            raise ToolError(f"connection {connection} shows no metric {handle!r}")
        return board

    def get_card_settings(self, handle: str, connection: int | None = None) -> dict[str, Any]:
        if connection is None:
            return super().get_card_settings(handle)
        return self.hooks.on_gui(lambda: card_to_dict(self._network_board(connection, handle).card_settings(handle)))

    def set_card_settings(self, handle: str, settings: dict[str, Any], connection: int | None = None) -> dict[str, Any]:
        if connection is None:
            return super().set_card_settings(handle, settings)

        def work() -> dict[str, Any]:
            board = self._network_board(connection, handle)
            changed = card_from_dict(settings, board.card_settings(handle))
            board.set_card_settings(handle, changed)
            return card_to_dict(changed)

        return self.hooks.on_gui(work)

    def read_activity_log(self, last: int = 50) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            entries = list(activity_log().entries)[-max(1, int(last)):]
            return {"entries": [
                {"time": entry.time, "device": entry.device, "text": entry.text, "alarm": entry.alarm}
                for entry in entries
            ]}

        return self.hooks.on_gui(work)


def start_mcp_server(window: MainWindow, port: int) -> McpHttpServer:
    """Serve MCP for this window on the loopback address; the caller stops it on exit."""
    hooks = GuiHooks(window)
    tools = GuiTools(window.service, GuiNetwork(window, hooks), hooks)
    server = McpHttpServer(McpApplication(tools.tools(), tools.resources(), instructions=tools.instructions()), port)
    server.start()
    window.mcp_url = server.url
    window._show_status_hint()  # noqa: SLF001 - the window's own status line
    return server
