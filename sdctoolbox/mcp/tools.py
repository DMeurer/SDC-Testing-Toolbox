"""The MCP tools: what an agent can do with the toolbox, which is what the UI can do.

Arguments and results use the same shapes as a toolbox profile (``config.metric_from_dict``
and friends), so an agent's input gets exactly the validation a loaded file gets.

What differs with a window is behind ``Hooks``: where the work runs (the GUI thread), how the
views catch up, and what only a window has (view options, card settings, the activity log).
Without a window the base class does the plain thing.
"""

from __future__ import annotations

import dataclasses
import enum
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import config, mdib_check
from ..consumer_service import ConsumerService, DiscoveredDevice, RemoteDevice
from ..mdib_io import NativeMdibConfig, mdib_bytes, parse_mdib
from ..model import (
    AlertPriority,
    CardSettings,
    CARD_ATTRIBUTES,
    coerce_metric_value,
)
from . import guide
from .server import Resource, Tool, ToolError

if TYPE_CHECKING:
    from ..provider_service import ProviderService

# Waveforms carry hundreds of samples per second; an agent needs the latest few, not all.
MAX_SAMPLES = 32
CONNECT_TIMEOUT_SECONDS = 30.0


def plain(value: Any) -> Any:
    """JSON-safe copy of the toolbox's dataclasses, enums and Decimals."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [plain(item) for item in value]
    return value


def card_to_dict(settings: CardSettings) -> dict[str, Any]:
    return plain(settings)


def card_from_dict(changes: dict[str, Any], base: CardSettings | None = None) -> CardSettings:
    """Card settings from JSON, on top of `base`; checked by the same rules a loaded file meets."""
    unknown = set(changes) - set(CARD_ATTRIBUTES)
    if unknown:
        raise ToolError(f"unknown card setting(s) {sorted(unknown)}; known: {sorted(CARD_ATTRIBUTES)}")
    attributes = (base or CardSettings()).to_attributes()
    for name, value in changes.items():
        attribute = CARD_ATTRIBUTES[name]
        if value is None:
            attributes.pop(attribute, None)
        else:
            attributes[attribute] = str(value).lower() if isinstance(value, bool) else str(value)
    return CardSettings.from_attributes(attributes)


# --------------------------------------------------------------------------------------
# Headless defaults; the GUI overrides these in sdctoolbox.gui.mcp_bridge
# --------------------------------------------------------------------------------------


class Hooks:
    """What changes when there is a window. This base is the headless behaviour."""

    has_window = False

    def __init__(self, service: ProviderService) -> None:
        self.service = service

    def on_gui(self, function: Callable[[], Any]) -> Any:
        """Run something that touches My device or the window. Headless: right here."""
        return function()

    def changed(self, note: str) -> None:
        """My device changed; refresh what shows it and log `note`."""

    def set_card_settings(self, handle: str, settings: CardSettings) -> None:
        self.service.card_settings[handle] = settings

    def replace_device(self, device: config.DeviceConfig | NativeMdibConfig, source: str) -> tuple[int, int]:
        return config.apply_to(self.service, device, replace=True)

    def window_only(self, what: str) -> ToolError:
        return ToolError(f"{what} needs the window; this toolbox runs without one (--headless)")


class Network:
    """The Network side without a window: one consumer, connections numbered from 1."""

    def __init__(self, service: ProviderService) -> None:
        self.consumer = ConsumerService(ip=service.ip, own_epr=service.epr.urn, tls_config=service.tls_config)
        self.consumer.start()
        self.remotes: dict[int, RemoteDevice] = {}
        self._next = 1
        self._lock = threading.Lock()

    def scan(self, timeout: float) -> list[DiscoveredDevice]:
        return self.consumer.scan(timeout=timeout, expected=99)

    def known(self) -> list[DiscoveredDevice]:
        return self.consumer.known_devices()

    def connect(self, device: DiscoveredDevice) -> int:
        with self._lock:
            for number, remote in self.remotes.items():
                if remote.epr == device.epr:
                    return number
        remote = self.consumer.connect(device)
        with self._lock:
            number, self._next = self._next, self._next + 1
            self.remotes[number] = remote
        return number

    def connections(self) -> dict[int, RemoteDevice | None]:
        with self._lock:
            return dict(self.remotes)

    def disconnect(self, number: int) -> None:
        with self._lock:
            remote = self.remotes.pop(number, None)
        if remote is None:
            raise ToolError(f"no connection {number}")
        remote.close()

    def status(self, number: int) -> str:
        return "connected" if number in self.remotes else "not connected"

    def close(self) -> None:
        for number in list(self.remotes):
            self.disconnect(number)
        self.consumer.stop()


# --------------------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------------------


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _object(description: str) -> dict[str, Any]:
    return {"type": "object", "description": description}


def _boolean(description: str) -> dict[str, Any]:
    return {"type": "boolean", "description": description}


def _value(description: str) -> dict[str, Any]:
    return {"type": ["string", "number"], "description": description}


CONNECTION = {"type": "integer", "description": "Connection number, as list_connections shows it (the Tab N in the window)."}

METRIC_SHAPE = (
    "A data source in toolbox profile form: label (required), kind (number, text, choice, waveform, distribution), "
    "unit (\"mmHg\" or {code, system, label}; system 'mdc' for IEEE 11073 codes), type (coding of what it measures), "
    "minimum/maximum (the technical range), resolution, controllable (remote control by consumers), "
    "allowed_values (choice), initial_value, section (a channel name), handle (optional, stable id). "
    "Waveforms take sample_period, shape (sine, sawtooth, square, noise, pulse, ecg, arterial, respiration, flow) and "
    "cycle_seconds; distributions take domain_unit, domain_minimum, domain_maximum and distribution_shape "
    "(bell, spectrum, bimodal, decay, flat). Numbers as numbers or decimal strings."
)
ALARM_SHAPE = (
    "An alarm: label, watches (handle of the data source), kind (Phy physiological, Tec technical, Oth other), "
    "priority (Lo, Me, Hi, None), lower_limit/upper_limit (makes it follow its source by itself; numbers only), "
    "signals (list of {manifestation: Vis|Aud|Tan|Oth, latching: bool}; default visible and audible), "
    "delegable (signals may be announced elsewhere), handle (optional)."
)
ACTION_SHAPE = (
    "An action (ActivateOperation): label, target (handle of the MDS, VMD or channel it acts on), "
    "effects ({metric handle: value} it sets when run), note, handle (optional)."
)
CARD_SHAPE = (
    "Card settings to change: control (control class name, see explain_gui topic 'cards'), small_steps, large_steps, "
    "update_on (release or change), show_ranges, legend, seconds_per_width, split, multiline. null resets one."
)


class ToolboxTools:
    """Every tool, bound to one provider, one network side and the hooks for the window."""

    def __init__(self, service: ProviderService, network: Network | Any, hooks: Hooks) -> None:
        self.service = service
        self.network = network
        self.hooks = hooks

    # -- what to tell the agent ------------------------------------------------------

    def instructions(self) -> str:
        mode = "with its window open; the user watches every change" if self.hooks.has_window else "without a window"
        return (
            f"You control the SDC Testing Toolbox, running {mode}. It publishes one IEEE 11073 SDC device "
            "('My device') and can connect to other SDC devices ('Network'). Start with overview. "
            "Build a device with add_metric/add_alarm/add_action or all at once with build_device, then "
            "get_mdib or export_mdib for the MDIB XML; check_mdib before exporting, and fix what it finds. explain_gui answers what something in the window means; "
            "explain_sdc explains the SDC concept behind it. Values are decimal strings or numbers. "
            "This is a learning tool, not a medical device."
        )

    def resources(self) -> list[Resource]:
        return [
            Resource(f"toolbox://guide/{topic}", f"GUI guide: {topic}", guide.TOPICS[topic][0],
                     lambda topic=topic: guide.TOPICS[topic][1])
            for topic in guide.TOPICS
        ]

    def tools(self) -> list[Tool]:  # noqa: PLR0915 - one entry per tool reads best as one list
        t = Tool
        return [
            # -- orientation
            t("overview", "What is running: My device (identity, counts), connections, MCP mode, and where to start.",
              self.overview),
            t("explain_gui", "Explain the toolbox window: panels, buttons, icons, chips, columns, card controls, menus. "
                             "Without a topic, lists the topics.",
              self.explain_gui, {"topic": _string("A topic name, or words to search for, such as 'signal icons'.")}),
            t("explain_sdc", "Explain an SDC/BICEPS concept as the toolbox uses it (MDIB, metric kinds, alarms, "
                             "operations, contexts, handles, discovery). Without a topic, lists them.",
              self.explain_sdc, {"topic": _string("A concept, such as 'alarms' or 'handles'.")}),
            # -- My device: data sources
            t("list_metrics", "My device's data sources with their definition, current value and card settings.",
              self.list_metrics),
            t("add_metric", "Create a data source on My device. " + METRIC_SHAPE, self.add_metric,
              {"metric": _object(METRIC_SHAPE), "card": _object(CARD_SHAPE)}, ("metric",)),
            t("update_metric", "Change a data source the way its Edit dialog does: allowed range, allowed values, "
                               "remote control, active, waveform curve, card settings.",
              self.update_metric,
              {"handle": _string("The data source's handle."),
               "controllable": _boolean("Offer remote control (a set operation) to consumers."),
               "active": _boolean("Switch the metric on (true) or off (false)."),
               "allowed_minimum": _value("Lower end of what a consumer may set; null removes it."),
               "allowed_maximum": _value("Upper end of what a consumer may set; null removes it."),
               "allowed_values": {"type": "array", "items": {"type": "string"},
                                  "description": "For a choice: the values a consumer may set."},
               "shape": _string("Waveform shape."), "cycle_seconds": _value("Waveform cycle length in seconds."),
               "distribution_shape": _string("Distribution shape."),
               "card": _object(CARD_SHAPE)},
              ("handle",)),
            t("set_value", "Set a data source's value on My device, as typing it in the window does.",
              self.set_value, {"handle": _string("Data source handle."), "value": _value("The new value.")},
              ("handle", "value")),
            t("remove_metric", "Remove a data source, and the alarms and operations that depend on it.",
              self.remove_metric, {"handle": _string("Data source handle.")}, ("handle",)),
            # -- My device: alarms
            t("list_alarms", "My device's alarms: definition, whether present, and each signal's state.",
              self.list_alarms),
            t("add_alarm", "Create an alarm on My device. " + ALARM_SHAPE, self.add_alarm,
              {"alarm": _object(ALARM_SHAPE)}, ("alarm",)),
            t("update_alarm", "Change an alarm: limits, priority, raise or clear it by hand, acknowledge, "
                              "stop latched signals, or delegate its signals.",
              self.update_alarm,
              {"handle": _string("Alarm handle."),
               "lower_limit": _value("New lower limit; null removes it."),
               "upper_limit": _value("New upper limit; null removes it."),
               "priority": _string("Lo, Me, Hi or None."),
               "present": _boolean("Raise (true) or clear (false) by hand."),
               "acknowledge": _boolean("Acknowledge the announcing signals."),
               "stop_latched": _boolean("Stop signals still latching after the condition cleared."),
               "delegated": _boolean("Announce the delegable signals elsewhere (true) or here (false).")},
              ("handle",)),
            t("remove_alarm", "Remove an alarm and its signals.", self.remove_alarm,
              {"handle": _string("Alarm handle.")}, ("handle",)),
            # -- My device: actions
            t("list_actions", "My device's actions (ActivateOperations) and what each sets.", self.list_actions),
            t("add_action", "Create an action. " + ACTION_SHAPE, self.add_action,
              {"action": _object(ACTION_SHAPE)}, ("action",)),
            t("run_action", "Run one of My device's actions, as its button does.", self.run_action,
              {"handle": _string("Action handle.")}, ("handle",)),
            t("remove_action", "Remove an action.", self.remove_action, {"handle": _string("Action handle.")}, ("handle",)),
            # -- My device: contexts and generator
            t("set_contexts", "Set My device's location and/or patient, as 'Patient and location...' does. "
                              "An empty patient object detaches the patient.",
              self.set_contexts,
              {"location": _object("facility, building, floor, point_of_care, room, bed (strings)."),
               "patient": _object("given_name, family_name, sex (M, F, Unkn, Unspec), patient_type (Ad, Ado, Ped, "
                                  "Inf, Neo, Unkn), date_of_birth (YYYY-MM-DD), height/weight ({value, unit: {code, "
                                  "system}}), race ({code, system})."),
               }),
            t("set_generator", "Start or stop generating samples for waveforms and distributions.",
              self.set_generator, {"running": _boolean("true to generate, false to stop.")}, ("running",)),
            # -- My device: whole device, MDIB in and out
            t("build_device", "Replace My device with a whole device in one go: {name, description, metrics: [...], "
                              "alerts: [...], actions: [...], contexts: {location, patient}} with the shapes of "
                              "add_metric, add_alarm and add_action. Validated completely before anything changes. "
                              "{} gives an empty device.",
              self.build_device, {"device": _object("The device, as described.")}, ("device",)),
            t("get_mdib", "My device as MDIB XML (MdDescription and MdState in one GetMdibResponse).",
              self.get_mdib, {"behaviour": _boolean("Include the toolbox behaviour extensions (default true), so the "
                                                    "file loads back complete.")}),
            t("export_mdib", "Write My device to an .xml file, as File > Export config does.", self.export_mdib,
              {"path": _string("Target .xml path."),
               "behaviour": _boolean("Include the toolbox behaviour (default true).")}, ("path",)),
            t("load_mdib", "Replace My device with an MDIB, from a file or XML text. Restarts the provider: "
                           "connected consumers must reconnect.",
              self.load_mdib, {"path": _string("An .xml file."), "xml": _string("MDIB XML text.")}),
            t("check_mdib", "Check an MDIB against what SDC asks beyond the schema (IEEE 11073-10700/-10701 rules, "
                            "MDC coding, toolbox conventions). Returns errors, warnings and hints, each with the "
                            "element's handle, what is wrong and why it matters to a consumer, and the rule. "
                            "My device without connection; a connected device's MDIB with it. Check before exporting.",
              self.check_mdib, {"connection": CONNECTION}),
            t("list_presets", "The shipped example devices.", self.list_presets),
            t("load_preset", "Replace My device with a preset, by name or file name.", self.load_preset,
              {"name": _string("Preset name, such as 'Insufflator'.")}, ("name",)),
            # -- Network
            t("scan", "Search the network for SDC devices (Scan).", self.scan,
              {"timeout": {"type": "number", "description": "Seconds to search, default 3."}}),
            t("connect", "Connect to a device by its EPR (Connect); returns its connection number.", self.connect,
              {"epr": _string("The device's endpoint reference, as scan lists it.")}, ("epr",)),
            t("list_connections", "The open connections and their state.", self.list_connections),
            t("disconnect", "Close a connection.", self.disconnect, {"connection": CONNECTION}, ("connection",)),
            t("read_metrics", "A connected device's metrics: definition, live value, range and whether writable.",
              self.read_metrics,
              {"connection": CONNECTION, "handles": {"type": "array", "items": {"type": "string"},
                                                     "description": "Only these handles (default: all)."}},
              ("connection",)),
            t("read_alarms", "A connected device's alarms and signal states.", self.read_alarms,
              {"connection": CONNECTION}, ("connection",)),
            t("read_actions", "A connected device's actions (activate operations).", self.read_actions,
              {"connection": CONNECTION}, ("connection",)),
            t("remote_set_value", "Ask a connected device to set a metric (Set on device). Returns the device's own "
                                  "answer, which may be a refusal.",
              self.remote_set_value,
              {"connection": CONNECTION, "handle": _string("Metric handle."), "value": _value("Requested value.")},
              ("connection", "handle", "value")),
            t("remote_run_action", "Ask a connected device to run one of its actions.", self.remote_run_action,
              {"connection": CONNECTION, "handle": _string("Action (operation) handle.")}, ("connection", "handle")),
            t("remote_mdib", "A connected device's MDIB as XML.", self.remote_mdib,
              {"connection": CONNECTION}, ("connection",)),
            # -- The window
            t("get_view", "How the window is set up: tab, split view, widgets or tables, dark mode, menu bar.",
              self.get_view),
            t("set_view", "Change the window: tab (my_device or network), split_view, widgets (cards instead of "
                          "tables), dark_mode, connection (bring a Network tab to the front), activity_log (open it).",
              self.set_view,
              {"tab": _string("my_device or network", enum=["my_device", "network"]),
               "split_view": _boolean("Both panels side by side."), "widgets": _boolean("Cards instead of tables."),
               "dark_mode": _boolean("Dark colours."), "connection": CONNECTION,
               "activity_log": _boolean("Open the activity log window.")}),
            t("get_card_settings", "How one metric's card looks, on My device or on a connection.",
              self.get_card_settings, {"handle": _string("Metric handle."), "connection": CONNECTION}, ("handle",)),
            t("set_card_settings", "Change how one metric's card looks, on My device (stored in MDIB exports with "
                                   "toolbox behaviour) or on a connection. " + CARD_SHAPE,
              self.set_card_settings,
              {"handle": _string("Metric handle."), "settings": _object(CARD_SHAPE), "connection": CONNECTION},
              ("handle", "settings")),
            t("read_activity_log", "The activity log: what changed on My device and connected devices, newest last.",
              self.read_activity_log, {"last": {"type": "integer", "description": "How many entries (default 50)."}}),
        ]

    # -- orientation ------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        def collect() -> dict[str, Any]:
            service = self.service
            return {
                "mode": "window" if self.hooks.has_window else "headless",
                "my_device": {
                    "name": service.instance_name,
                    "friendly_name": service.friendly_name,
                    "epr": service.epr.urn,
                    "manufacturer": service.device.manufacturer,
                    "model": f"{service.device.model_name} {service.device.model_number}".strip(),
                    "preset": (service.preset or {}).get("name"),
                    "loaded_from_mdib": service.from_mdib_file,
                    "metrics": len(service.list_metrics()),
                    "alarms": len(service.list_alerts()),
                    "actions": len(service.list_actions()),
                    "generator_running": service.generator_running,
                    "location": plain(service.get_location()),
                },
                "connections": self.list_connections()["connections"],
                "start_here": [
                    "list_presets / load_preset for a ready-made device",
                    "add_metric, add_alarm, add_action, or build_device to build one",
                    "check_mdib, then get_mdib or export_mdib to get the MDIB",
                    "scan, connect, read_metrics for other devices",
                    "explain_gui and explain_sdc when the user asks what something means",
                ],
            }

        return self.hooks.on_gui(collect)

    def explain_gui(self, topic: str | None = None) -> str:
        return guide.lookup(guide.TOPICS, topic, "GUI")

    def explain_sdc(self, topic: str | None = None) -> str:
        return guide.lookup(guide.CONCEPTS, topic, "SDC")

    # -- My device: data sources -------------------------------------------------------

    def _metric(self, handle: str, spec) -> dict[str, Any]:  # noqa: ANN001
        entry = config.metric_to_dict(handle, spec)
        if not spec.is_sample_array:
            entry["value"] = plain(self.service.get_value(handle))
        entry["remote_control"] = self.service.operation_handle_for(handle) is not None
        entry["card"] = card_to_dict(self.service.card_settings.get(handle, CardSettings()))
        return entry

    def list_metrics(self) -> dict[str, Any]:
        return self.hooks.on_gui(lambda: {"metrics": [
            self._metric(handle, spec) for handle, spec in sorted(self.service.list_metrics().items())
        ]})

    def add_metric(self, metric: dict[str, Any], card: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = config.metric_from_dict(metric)
        settings = card_from_dict(card) if card else None

        def work() -> dict[str, Any]:
            handle = self.service.add_metric(spec)
            if settings is not None:
                self.hooks.set_card_settings(handle, settings)
            self.hooks.changed(f"added data source {spec.label} ({handle})")
            return self._metric(handle, self.service.list_metrics()[handle])

        return self.hooks.on_gui(work)

    def update_metric(self, handle: str, **changes: Any) -> dict[str, Any]:  # noqa: C901
        def work() -> dict[str, Any]:
            spec = self._spec(handle)
            if "controllable" in changes:
                (self.service.enable_control if changes["controllable"] else self.service.disable_control)(handle)
            if "allowed_minimum" in changes or "allowed_maximum" in changes:
                lower = changes.get("allowed_minimum", spec.allowed_minimum)
                upper = changes.get("allowed_maximum", spec.allowed_maximum)
                self.service.set_allowed_range(handle, _decimal(lower), _decimal(upper))
            if "allowed_values" in changes:
                self.service.set_allowed_values(handle, changes["allowed_values"])
            curve = {}
            if "shape" in changes:
                curve["shape"] = _enum(type(spec.shape), changes["shape"])
            if "cycle_seconds" in changes:
                curve["cycle_seconds"] = _decimal(changes["cycle_seconds"])
            if "distribution_shape" in changes:
                curve["distribution_shape"] = _enum(type(spec.distribution_shape), changes["distribution_shape"])
            if curve:
                self.service.set_generator(handle, **curve)
            if "active" in changes:
                self.service.set_metric_active(handle, active=changes["active"])
            if changes.get("card") is not None:
                base = self.service.card_settings.get(handle, CardSettings())
                self.hooks.set_card_settings(handle, card_from_dict(changes["card"], base))
            self.hooks.changed(f"changed data source {spec.label} ({handle})")
            return self._metric(handle, self.service.list_metrics()[handle])

        return self.hooks.on_gui(work)

    def set_value(self, handle: str, value: Any) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            spec = self._spec(handle)
            self.service.set_value(handle, coerce_metric_value(spec, _text(value), handle))
            self.hooks.changed(f"set {spec.label} ({handle}) to {value}")
            return {"handle": handle, "value": plain(self.service.get_value(handle))}

        return self.hooks.on_gui(work)

    def remove_metric(self, handle: str) -> str:
        def work() -> str:
            spec = self._spec(handle)
            self.service.remove_metric(handle)
            self.hooks.changed(f"removed data source {spec.label} ({handle})")
            return f"removed {handle}"

        return self.hooks.on_gui(work)

    def _spec(self, handle: str):  # noqa: ANN202
        spec = self.service.list_metrics().get(handle)
        if spec is None:
            known = ", ".join(sorted(self.service.list_metrics())) or "none"
            raise ToolError(f"no data source {handle!r}; known: {known}")
        return spec

    # -- My device: alarms -------------------------------------------------------------

    def _alarm(self, handle: str, spec) -> dict[str, Any]:  # noqa: ANN001
        entry = config.alert_to_dict(handle, spec)
        entry["present"] = self.service.alert_present(handle)
        entry["signal_states"] = plain(self.service.signal_states(handle))
        return entry

    def list_alarms(self) -> dict[str, Any]:
        return self.hooks.on_gui(lambda: {"alarms": [
            self._alarm(handle, spec) for handle, spec in sorted(self.service.list_alerts().items())
        ]})

    def add_alarm(self, alarm: dict[str, Any]) -> dict[str, Any]:
        spec = config.alert_from_dict(alarm)

        def work() -> dict[str, Any]:
            handle = self.service.add_alert(spec)
            self.hooks.changed(f"added alarm {spec.label} ({handle})")
            return self._alarm(handle, self.service.list_alerts()[handle])

        return self.hooks.on_gui(work)

    def update_alarm(self, handle: str, **changes: Any) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            spec = self.service.list_alerts().get(handle)
            if spec is None:
                raise ToolError(f"no alarm {handle!r}; known: {', '.join(sorted(self.service.list_alerts())) or 'none'}")
            if "lower_limit" in changes or "upper_limit" in changes:
                lower = changes.get("lower_limit", spec.lower_limit)
                upper = changes.get("upper_limit", spec.upper_limit)
                self.service.set_alert_limits(handle, _decimal(lower), _decimal(upper))
            if "priority" in changes:
                self.service.set_alert_priority(handle, _enum(AlertPriority, changes["priority"]))
            if "present" in changes:
                self.service.set_alert_presence(handle, changes["present"])
            if changes.get("acknowledge"):
                self.service.acknowledge_alert(handle)
            if changes.get("stop_latched"):
                self.service.stop_latched_signals(handle)
            if "delegated" in changes:
                signals = [signal for signal in self.service.signal_states(handle) if signal.delegable]
                if not signals:
                    raise ToolError(f"{handle!r} has no delegable signal; create the alarm with delegable true")
                for signal in signals:
                    self.service.set_signal_delegated(signal.handle, delegated=changes["delegated"])
            self.hooks.changed(f"changed alarm {spec.label} ({handle})")
            return self._alarm(handle, self.service.list_alerts()[handle])

        return self.hooks.on_gui(work)

    def remove_alarm(self, handle: str) -> str:
        def work() -> str:
            self.service.remove_alert(handle)
            self.hooks.changed(f"removed alarm {handle}")
            return f"removed {handle}"

        return self.hooks.on_gui(work)

    # -- My device: actions ------------------------------------------------------------

    def list_actions(self) -> dict[str, Any]:
        return self.hooks.on_gui(lambda: {"actions": [
            config.action_to_dict(handle, spec) for handle, spec in sorted(self.service.list_actions().items())
        ]})

    def add_action(self, action: dict[str, Any]) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            spec = config.action_from_dict(action, self.service.list_metrics())
            handle = self.service.add_action(spec)
            self.hooks.changed(f"added action {spec.label} ({handle})")
            return config.action_to_dict(handle, self.service.list_actions()[handle])

        return self.hooks.on_gui(work)

    def run_action(self, handle: str) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            self.service.run_action(handle)
            self.hooks.changed(f"ran action {handle}")
            effects = self.service.list_actions()[handle].effects
            return {"ran": handle, "values": {h: plain(self.service.get_value(h)) for h in effects}}

        return self.hooks.on_gui(work)

    def remove_action(self, handle: str) -> str:
        def work() -> str:
            self.service.remove_action(handle)
            self.hooks.changed(f"removed action {handle}")
            return f"removed {handle}"

        return self.hooks.on_gui(work)

    # -- My device: contexts and generator ---------------------------------------------

    def set_contexts(self, location: dict[str, Any] | None = None, patient: dict[str, Any] | None = None) -> dict[str, Any]:
        new_location, new_patient = config._contexts_from_dict(  # noqa: SLF001 - the profile's own reader
            {key: value for key, value in (("location", location), ("patient", patient)) if value is not None},
        )

        def work() -> dict[str, Any]:
            if new_location is not None:
                self.service.set_location(new_location)
            if patient is not None:
                if new_patient is None or new_patient == type(new_patient)():
                    self.service.clear_patient()
                else:
                    self.service.set_patient(new_patient)
            self.hooks.changed("changed patient and location")
            return {"location": plain(self.service.get_location()), "patient": plain(self.service.get_patient())}

        return self.hooks.on_gui(work)

    def set_generator(self, running: bool) -> str:  # noqa: FBT001 - a tool argument
        def work() -> str:
            (self.service.start_generator if running else self.service.stop_generator)()
            self.hooks.changed("started the generator" if running else "stopped the generator")
            return "generating" if self.service.generator_running else "stopped"

        return self.hooks.on_gui(work)

    # -- My device: whole device -------------------------------------------------------

    def build_device(self, device: dict[str, Any]) -> dict[str, Any]:
        parsed = config.parse(device)
        return self.hooks.on_gui(lambda: self._replaced(parsed, "the device built over MCP"))

    def _replaced(self, device: config.DeviceConfig | NativeMdibConfig, source: str) -> dict[str, Any]:
        metrics, alarms = self.hooks.replace_device(device, source)
        self.hooks.changed(f"replaced My device with {source}")
        return {"metrics": metrics, "alarms": alarms, "actions": len(self.service.list_actions()),
                "note": "connected consumers must reconnect" if isinstance(device, NativeMdibConfig) else ""}

    def get_mdib(self, behaviour: bool = True) -> str:  # noqa: FBT001, FBT002 - a tool argument
        def work() -> str:
            extensions = self.service.behaviour_extensions() if behaviour else None
            return mdib_bytes(self.service.mdib, extensions).decode("utf-8")

        return self.hooks.on_gui(work)

    def export_mdib(self, path: str, behaviour: bool = True) -> dict[str, Any]:  # noqa: FBT001, FBT002
        written = self.hooks.on_gui(lambda: config.save(self.service, Path(path), behaviour=behaviour))
        return {"written": str(written.resolve()), "behaviour": behaviour}

    def load_mdib(self, path: str | None = None, xml: str | None = None) -> dict[str, Any]:
        if (path is None) == (xml is None):
            raise ToolError("give either path or xml")
        if path is not None:
            device = config.load_file(path)
            source = Path(path).name
        else:
            data = xml.encode("utf-8")
            parse_mdib(data)
            device, source = NativeMdibConfig(data), "MDIB XML"
        return self.hooks.on_gui(lambda: self._replaced(device, source))

    def check_mdib(self, connection: int | None = None) -> dict[str, Any]:
        if connection is None:
            checked, findings = "My device", self.hooks.on_gui(lambda: mdib_check.check(self.service.mdib))
        else:
            checked, findings = f"connection {connection}", mdib_check.check(self._remote(connection).mdib)
        return {"checked": checked, "summary": mdib_check.summary(findings), "sources": mdib_check.SOURCES,
                "findings": plain(findings)}

    def list_presets(self) -> dict[str, Any]:
        return {"presets": [
            {"name": preset.name, "file": preset.path.name, "description": preset.description,
             "metrics": preset.metrics, "alarms": preset.alerts}
            for preset in config.list_presets()
        ]}

    def load_preset(self, name: str) -> dict[str, Any]:
        wanted = name.strip().lower()
        presets = config.list_presets()
        preset = next((p for p in presets if wanted in (p.name.lower(), p.path.name.lower(), p.path.stem.lower())), None)
        if preset is None:
            raise ToolError(f"no preset {name!r}; there are: {', '.join(p.name for p in presets)}")
        device = config.load_file(preset.path)
        return self.hooks.on_gui(lambda: self._replaced(device, preset.name))

    # -- Network -----------------------------------------------------------------------

    def scan(self, timeout: float | Decimal = 3) -> dict[str, Any]:
        found = self.network.scan(float(timeout))
        return {"devices": [_device(device) for device in found]}

    def connect(self, epr: str) -> dict[str, Any]:
        device = next((d for d in self.network.known() if d.epr == epr), None)
        if device is None:
            device = next((d for d in self.network.scan(3.0) if d.epr == epr), None)
        if device is None:
            raise ToolError(f"no device {epr!r} found; scan first and use an EPR it lists")
        number = self.network.connect(device)
        return {"connection": number, "epr": epr, "status": self.network.status(number)}

    def list_connections(self) -> dict[str, Any]:
        return {"connections": [
            {"connection": number, "epr": getattr(remote, "epr", None),
             "friendly_name": remote.friendly_name if remote is not None else None,
             "status": self.network.status(number)}
            for number, remote in sorted(self.network.connections().items())
        ]}

    def disconnect(self, connection: int) -> str:
        self.network.disconnect(connection)
        return f"closed connection {connection}"

    def _remote(self, connection: int) -> RemoteDevice:
        remote = self.network.connections().get(connection)
        if remote is None:
            raise ToolError(f"connection {connection} is not connected; see list_connections")
        return remote

    def read_metrics(self, connection: int, handles: list[str] | None = None) -> dict[str, Any]:
        metrics = self._remote(connection).metrics(handles)
        result = []
        for metric in metrics.values():
            entry = plain(metric)
            if entry.get("samples"):
                entry["samples"] = entry["samples"][-MAX_SAMPLES:]
            result.append(entry)
        return {"metrics": result}

    def read_alarms(self, connection: int) -> dict[str, Any]:
        return {"alarms": plain(list(self._remote(connection).alerts().values()))}

    def read_actions(self, connection: int) -> dict[str, Any]:
        return {"actions": plain(list(self._remote(connection).actions().values()))}

    def remote_set_value(self, connection: int, handle: str, value: Any) -> dict[str, Any]:
        remote = self._remote(connection)
        state = remote.set_value(handle, _text(value))
        return {"handle": handle, "requested": _text(value), "invocation_state": plain(state),
                "accepted": plain(state) in ("Fin", "FinMod"), "device_said": remote.last_invocation_detail}

    def remote_run_action(self, connection: int, handle: str) -> dict[str, Any]:
        state = self._remote(connection).run_action(handle)
        return {"action": handle, "invocation_state": plain(state), "accepted": plain(state) in ("Fin", "FinMod")}

    def remote_mdib(self, connection: int) -> str:
        return mdib_bytes(self._remote(connection).mdib).decode("utf-8")

    # -- The window (GUI hooks only) ---------------------------------------------------

    def get_view(self) -> dict[str, Any]:
        raise self.hooks.window_only("get_view")

    def set_view(self, **_options: Any) -> dict[str, Any]:
        raise self.hooks.window_only("set_view")

    def get_card_settings(self, handle: str, connection: int | None = None) -> dict[str, Any]:
        if connection is not None:
            raise self.hooks.window_only("cards on a connection")
        def work() -> dict[str, Any]:
            self._spec(handle)
            return card_to_dict(self.service.card_settings.get(handle, CardSettings()))

        return self.hooks.on_gui(work)

    def set_card_settings(self, handle: str, settings: dict[str, Any], connection: int | None = None) -> dict[str, Any]:
        if connection is not None:
            raise self.hooks.window_only("cards on a connection")

        def work() -> dict[str, Any]:
            self._spec(handle)
            changed = card_from_dict(settings, self.service.card_settings.get(handle, CardSettings()))
            self.hooks.set_card_settings(handle, changed)
            self.hooks.changed(f"changed the card of {handle}")
            return card_to_dict(changed)

        return self.hooks.on_gui(work)

    def read_activity_log(self, last: int = 50) -> dict[str, Any]:
        raise self.hooks.window_only("the activity log")


def _device(device: DiscoveredDevice) -> dict[str, Any]:
    return {"epr": device.epr, "addresses": list(device.x_addrs), "location": device.location_scope,
            "scopes": list(device.scopes)}


def _text(value: Any) -> str:
    """A value as the string the toolbox's own coercion reads, whatever JSON type it came in."""
    if isinstance(value, bool):
        raise ToolError("values are numbers or strings, not true/false")
    return value if isinstance(value, str) else str(value)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(_text(value))
    except ArithmeticError as exc:
        raise ToolError(f"{value!r} is not a number") from exc


def _enum(cls: type[enum.Enum], value: Any) -> enum.Enum:
    try:
        return cls(value)
    except ValueError as exc:
        raise ToolError(f"{value!r} is not one of {', '.join(member.value for member in cls)}") from exc


def wait_for(condition: Callable[[], bool], timeout: float) -> bool:
    """Poll until `condition` holds; for state another thread sets."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()
