"""The SOAP messages of each connection, for the Messages section of a Network tab.

sdc11073 writes every SOAP message it sends or receives to its ``sdc_comm.*`` loggers (the
names of its deprecated commlog module). One handler on them gives each message to the
connection it belongs to: a request by the address it goes to, its response by the thread
that sent the request, and a report by the consumer's own notification path. Messages of no
watched connection (My device's own traffic, for one) are dropped unread.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

from lxml import etree
from sdc11073.httpserver.httpreader import HTTPReader
from sdc11073.pysoap import soapclient

SENT, RECEIVED = "sent", "received"
# Logger name -> direction. Responses this toolbox sends and discovery are not per connection.
LOGGERS = {
    "sdc_comm.soap.request.out": SENT,
    "sdc_comm.soap.response.in": RECEIVED,
    "sdc_comm.soap.request.in": RECEIVED,
}
# shortcut: the newest 500 per connection; enough to read a session's start and its latest reports.
KEEP = 500
WSA = "{http://www.w3.org/2005/08/addressing}"
_PARSER = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)
_PRETTY_PARSER = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, remove_blank_text=True)


@dataclass(frozen=True)
class Message:
    """One SOAP message as it went over the wire."""

    number: int  # counts up across all connections, so "newer than" is a comparison
    time: float  # time.time() when it was logged
    direction: str  # SENT | RECEIVED
    action: str  # the last part of its WS-Addressing Action, e.g. "GetMdibResponse"
    detail: str  # e.g. "MdibVersion 481"
    data: bytes

    @property
    def kind(self) -> str:
        """request, response or report: what the Show filter picks by."""
        if self.action.endswith("Response") or self.action.lower() == "fault":  # WS-Addressing's .../addressing/fault
            return "response"
        return "report" if self.direction == RECEIVED else "request"

    @property
    def is_waveform(self) -> bool:
        return "Waveform" in self.action

    @property
    def summary(self) -> str:
        return f"{self.action}  ·  {self.detail}" if self.detail else self.action

    @property
    def size(self) -> str:
        count = len(self.data)
        return f"{count} B" if count < 1000 else f"{count / 1000:.1f} kB"  # noqa: PLR2004 - SI kilo

    def pretty(self) -> str:
        """The message's XML, indented; as it came if it does not parse."""
        try:
            return etree.tostring(etree.fromstring(self.data, _PRETTY_PARSER), pretty_print=True, encoding=str)
        except etree.LxmlError:
            return self.data.decode("utf-8", "replace")


class MessageLog:
    """One connection's messages, oldest first, at most KEEP of them."""

    def __init__(self) -> None:
        self._messages: deque[Message] = deque(maxlen=KEEP)
        self._lock = threading.Lock()
        self.total = 0  # every message seen, not only the ones kept
        self.addresses: set[str] = set()  # host:port of the peer's endpoints
        self.prefixes: set[str] = set()  # this connection's notification path

    def add(self, message: Message) -> None:
        with self._lock:
            self._messages.append(message)
            self.total += 1

    def messages(self, after: int = 0) -> list[Message]:
        """The kept messages numbered above `after`, oldest first."""
        with self._lock:
            return [message for message in self._messages if message.number > after]


def _header(root: etree._Element, name: str) -> str:
    node = root.find(f"{{*}}Header/{WSA}{name}")
    return (node.text or "").strip() if node is not None else ""


def _short_action(action: str) -> str:
    """The name in an action URI: ".../GetService/GetMdib" -> GetMdib, ".../GetMetadata/Request" -> GetMetadata Request."""
    parts = action.rstrip("/").split("/")
    if len(parts) > 1 and parts[-1] in ("Request", "Response"):
        return f"{parts[-2]} {parts[-1]}"
    return parts[-1] or "(no action)"


def _detail(root: etree._Element) -> str:
    body = root.find("{*}Body")
    first = next(iter(body), None) if body is not None else None
    if first is None:
        return ""
    version = first.get("MdibVersion")
    if version is not None:
        return f"MdibVersion {version}"
    text = first.findtext(".//{*}Text") if etree.QName(first).localname == "Fault" else None
    return text.strip() if text else ""


class _Recorder(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self._logs: list[MessageLog] = []
        self._numbers = itertools.count(1)
        self._sending = threading.local()  # the log of the request this thread sent last

    def watch(self, log: MessageLog) -> None:
        with self.lock:
            if log not in self._logs:
                self._logs.append(log)

    def forget(self, log: MessageLog) -> None:
        with self.lock:
            if log in self._logs:
                self._logs.remove(log)

    def _owner(self, logger: str, data: bytes) -> MessageLog | None:
        if logger == "sdc_comm.soap.response.in":
            log = getattr(self._sending, "log", None)
            self._sending.log = None
            return log
        with self.lock:
            logs = list(self._logs)
        if logger == "sdc_comm.soap.request.out":
            # A cheap look before parsing: most traffic belongs to no watched connection.
            log = next((log for log in logs if any(a.encode() in data for a in log.addresses)), None)
            self._sending.log = log
            return log
        return next((log for log in logs if any(p.encode() in data for p in log.prefixes)), None)

    def emit(self, record: logging.LogRecord) -> None:
        if not self._logs or record.name not in LOGGERS:
            return
        data = record.msg.encode("utf-8") if isinstance(record.msg, str) else bytes(record.msg)
        log = self._owner(record.name, data)
        if log is None:
            return
        try:
            root = etree.fromstring(data, _PARSER)
        except etree.LxmlError:
            action, detail = "(not XML)", ""
        else:
            to = urlparse(_header(root, "To"))
            if record.name == "sdc_comm.soap.request.out" and to.netloc not in log.addresses:
                self._sending.log = None
                return  # the address was only mentioned in the body
            if record.name == "sdc_comm.soap.request.in" and not any(p in to.path for p in log.prefixes):
                return  # a request to My device that names this consumer, e.g. its Subscribe
            action = _short_action(_header(root, "Action"))
            detail = _detail(root)
        log.add(Message(next(self._numbers), record.created or time.time(), LOGGERS[record.name], action, detail, data))


class _RefusalLoggingReader(HTTPReader):
    """sdc11073 logs a response only when its HTTP status is a success; this also logs a refusal's body."""

    @classmethod
    def read_response_body(cls, http_response, supported_encodings=None):  # noqa: ANN001, ANN206 - sdc11073's signature
        content = super().read_response_body(http_response, supported_encodings)
        if http_response.status >= 300:  # noqa: PLR2004 - sdc11073's own test; it raises before logging these
            logging.getLogger("sdc_comm.soap.response.in").debug(content, extra={"http_method": "POST"})
        return content


_recorder: _Recorder | None = None
_install_lock = threading.Lock()


def watch(log: MessageLog, *, addresses: list[str] = (), prefixes: list[str] = ()) -> MessageLog:
    """Record the messages to and from these peer addresses (host:port) and notification paths into `log`."""
    global _recorder  # noqa: PLW0603 - one handler per process
    with _install_lock:
        if _recorder is None:
            _recorder = _Recorder()
            for name in LOGGERS:
                logger = logging.getLogger(name)
                logger.addHandler(_recorder)
                logger.setLevel(logging.DEBUG)
                # Every message would otherwise reach the console's handlers as a DEBUG line.
                logger.propagate = False
            soapclient.HTTPReader = _RefusalLoggingReader  # the consumer's requests; a Fault is what Messages is for
    log.addresses.update(addresses)
    log.prefixes.update(prefixes)
    _recorder.watch(log)
    return log


def forget(log: MessageLog) -> None:
    """Stop recording into `log`; what it holds stays readable."""
    if _recorder is not None:
        _recorder.forget(log)
