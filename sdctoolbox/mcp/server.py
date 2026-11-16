"""A small MCP server over streamable HTTP, on the standard library.

It answers the part of the protocol a tool server needs: initialize, ping, tools and
resources. Every request gets a plain JSON response, which the transport allows, so there is
no event stream to keep open. The official SDK would bring a web framework, an ASGI server and
pydantic into every packaged build for exactly this.

It listens on the loopback address only and refuses requests from web pages (a foreign
Origin), because anything that can reach it can change the device.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .. import __version__
from ..config import ConfigError

logger = logging.getLogger("sdctoolbox.mcp")

PATH = "/mcp"
# Newest first; a client asking for another version is answered with the newest.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
MAX_BODY_BYTES = 8 * 1024 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ToolError(Exception):
    """A tool could not do what was asked; the message goes back to the agent as the result."""


# Refusals the UI would show in a message box. Anything else is also reported, and logged too.
EXPECTED_ERRORS = (ToolError, ConfigError, KeyError, ValueError, TypeError, OSError)


@dataclass(frozen=True)
class Tool:
    """One callable tool: what it is called, what it does, and the arguments it takes."""

    name: str
    description: str
    function: Callable[..., Any]
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    def descriptor(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "object", "properties": self.properties}
        if self.required:
            schema["required"] = list(self.required)
        return {"name": self.name, "description": self.description, "inputSchema": schema}


@dataclass(frozen=True)
class Resource:
    """A document the agent can read, such as one topic of the GUI guide."""

    uri: str
    name: str
    description: str
    read: Callable[[], str]


def run_directly(function: Callable[[], Any]) -> Any:
    return function()


class McpApplication:
    """The protocol itself: one JSON-RPC message in, at most one out. No sockets here."""

    def __init__(
        self,
        tools: list[Tool],
        resources: list[Resource] = (),
        *,
        instructions: str = "",
        run: Callable[[Callable[[], Any]], Any] = run_directly,
    ) -> None:
        self.tools = {tool.name: tool for tool in tools}
        self.resources = {resource.uri: resource for resource in resources}
        self.instructions = instructions
        # How a tool runs: directly when headless, on the GUI thread when there is a window.
        self.run = run

    def handle(self, message: Any) -> dict[str, Any] | None:
        """Answer one message; None for a notification or a response, which get no answer."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "expected a JSON-RPC 2.0 object")
        method = message.get("method")
        if not isinstance(method, str):
            return None  # a response to something we never send
        if "id" not in message:
            return None  # a notification, such as notifications/initialized
        request_id = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        handler = {
            "initialize": self._initialize,
            "ping": lambda _params: {},
            "tools/list": lambda _params: {"tools": [tool.descriptor() for tool in self.tools.values()]},
            "tools/call": self._call_tool,
            "resources/list": self._list_resources,
            "resources/read": self._read_resource,
        }.get(method)
        if handler is None:
            return _error(request_id, METHOD_NOT_FOUND, f"unknown method {method!r}")
        try:
            return {"jsonrpc": "2.0", "id": request_id, "result": handler(params)}
        except _ProtocolError as exc:
            return _error(request_id, exc.code, str(exc))
        except Exception as exc:
            logger.exception("MCP %s failed", method)
            return _error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
            "serverInfo": {"name": "sdc-testing-toolbox", "title": "SDC Testing Toolbox", "version": __version__},
            "instructions": self.instructions,
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self.tools.get(name)
        if tool is None:
            raise _ProtocolError(INVALID_PARAMS, f"unknown tool {name!r}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise _ProtocolError(INVALID_PARAMS, "arguments must be an object")
        unknown = set(arguments) - set(tool.properties)
        missing = [name for name in tool.required if name not in arguments]
        if unknown or missing:
            detail = "; ".join(filter(None, [
                f"unknown argument(s) {sorted(unknown)}" if unknown else "",
                f"missing argument(s) {missing}" if missing else "",
            ]))
            return _tool_result(f"{tool.name}: {detail}", error=True)
        try:
            result = self.run(lambda: tool.function(**arguments))
        # Whatever went wrong is the agent's to read and act on, as a message box is the user's.
        except Exception as exc:
            if not isinstance(exc, EXPECTED_ERRORS):
                logger.exception("MCP tool %s failed", tool.name)
            text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            return _tool_result(f"{type(exc).__name__}: {text}", error=True)
        return _tool_result(result)

    def _list_resources(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"resources": [
            {"uri": resource.uri, "name": resource.name, "description": resource.description,
             "mimeType": "text/markdown"}
            for resource in self.resources.values()
        ]}

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        resource = self.resources.get(params.get("uri"))
        if resource is None:
            raise _ProtocolError(INVALID_PARAMS, f"unknown resource {params.get('uri')!r}")
        return {"contents": [{"uri": resource.uri, "mimeType": "text/markdown", "text": resource.read()}]}


class _ProtocolError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_result(value: Any, *, error: bool = False) -> dict[str, Any]:
    if isinstance(value, str):
        return {"content": [{"type": "text", "text": value}], "isError": error}
    text = json.dumps(value, indent=1, default=str)
    result = {"content": [{"type": "text", "text": text}], "isError": error}
    if isinstance(value, dict):
        result["structuredContent"] = json.loads(text)
    return result


class _Handler(BaseHTTPRequestHandler):
    server: McpHttpServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class names it
        logger.debug("%s %s", self.address_string(), format % args)

    def _foreign_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return bool(origin) and urlparse(origin).hostname not in LOCAL_HOSTS

    def _send(self, status: HTTPStatus, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        if urlparse(self.path).path != PATH:
            self._send(HTTPStatus.NOT_FOUND, _error(None, INVALID_REQUEST, f"the MCP endpoint is {PATH}"))
            return
        if self._foreign_origin():
            self._send(HTTPStatus.FORBIDDEN, _error(None, INVALID_REQUEST, "requests from web pages are refused"))
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, _error(None, INVALID_REQUEST, "request too large"))
            return
        try:
            # Decimal, never float: the toolbox refuses floats so that 0.1 stays 0.1.
            message = json.loads(self.rfile.read(length) or b"null", parse_float=Decimal)
        except ValueError as exc:
            self._send(HTTPStatus.BAD_REQUEST, _error(None, PARSE_ERROR, f"not JSON: {exc}"))
            return
        reply = self.server.application.handle(message)
        if reply is None:
            self._send(HTTPStatus.ACCEPTED)
            return
        headers = {}
        if isinstance(message, dict) and message.get("method") == "initialize":
            headers["Mcp-Session-Id"] = uuid.uuid4().hex
        self._send(HTTPStatus.OK, reply, headers)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        # No server-initiated messages, so no event stream to open.
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, headers={"Allow": "POST"})

    do_DELETE = do_GET  # noqa: N815 - sessions hold no state worth ending


class McpHttpServer(ThreadingHTTPServer):
    """The HTTP side: POST /mcp on the loopback address, one thread per request."""

    daemon_threads = True

    def __init__(self, application: McpApplication, port: int, host: str = "127.0.0.1") -> None:
        super().__init__((host, port), _Handler)
        self.application = application
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}{PATH}"

    def start(self) -> None:
        """Serve in a background thread, for when the GUI owns the main thread."""
        self._thread = threading.Thread(target=self.serve_forever, name="mcp-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            # shutdown() waits for serve_forever, so only ask when start() began one.
            self.shutdown()
            self._thread.join(timeout=5)
        self.server_close()
