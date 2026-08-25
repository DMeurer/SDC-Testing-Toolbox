"""Interactive console for driving an SDC device by hand.

Two modes. Open a terminal for each:

    .venv/Scripts/python.exe examples/console.py provider --name alpha
    .venv/Scripts/python.exe examples/console.py consumer

The provider shell creates data sources and changes their values. The consumer shell finds
providers on the network, lists what they expose and remote-controls the parts that allow it.

Type `help` in either shell for the command list.
"""

from __future__ import annotations

import argparse
import logging
import shlex
import sys
import time
from cmd import Cmd
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdc11073.loghelper import basic_logging_setup  # noqa: E402
from sdc11073.xml_types import msg_types  # noqa: E402

from sdctoolbox import config, constants  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.model import AlertSpec, MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

KIND_BY_NAME = {
    "number": MetricKind.NUMBER,
    "text": MetricKind.TEXT,
    "choice": MetricKind.CHOICE,
}


def show(value: object) -> str:
    """Render a metric value for the console."""
    return "-" if value is None else str(value)


class ProviderShell(Cmd):
    """Commands for the device we publish ourselves."""

    prompt = "provider> "

    def __init__(self, service: ProviderService) -> None:
        super().__init__()
        self.service = service
        self.intro = (
            f"\nPublishing as {service.friendly_name!r} on {service.ip}\n"
            f"EPR {service.epr.urn}\n\n"
            "Try:  add number Zoom level\n"
            "      add choice Mode IDLE RUN PAUSE\n"
            "      set m.zoom_level 5\n"
            "      list\n"
            "Type help for everything else, quit to stop.\n"
        )

    # -- commands ------------------------------------------------------------------

    def do_add(self, line: str) -> None:
        """add <number|text|choice> <label...> [choice values...] [min..max]

        Creates a data source, remote-controllable by default.

            add number Zoom level
            add number Zoom level 1..100
            add text Patient note
            add choice Mode IDLE RUN PAUSE
        """
        parts = shlex.split(line)
        if len(parts) < 2:  # noqa: PLR2004
            print("usage: add <number|text|choice> <label...>")
            return

        kind_name, *rest = parts
        kind = KIND_BY_NAME.get(kind_name.lower())
        if kind is None:
            print(f"unknown kind {kind_name!r}, expected one of {', '.join(KIND_BY_NAME)}")
            return

        minimum = maximum = None
        if rest and ".." in rest[-1] and kind is MetricKind.NUMBER:
            low, _, high = rest.pop().partition("..")
            try:
                minimum = Decimal(low) if low else None
                maximum = Decimal(high) if high else None
            except InvalidOperation:
                print(f"could not read a range from {low!r}..{high!r}")
                return

        if kind is MetricKind.CHOICE:
            # Everything from the first ALL-CAPS token onwards is a choice value.
            split_at = next(
                (i for i, token in enumerate(rest) if token.isupper() and len(rest) - i > 1),
                None,
            )
            if split_at is None or split_at == 0:
                print("usage: add choice <label...> VALUE VALUE [VALUE ...]  (values in CAPS)")
                return
            label = " ".join(rest[:split_at])
            values = tuple(rest[split_at:])
        else:
            label = " ".join(rest)
            values = ()

        try:
            spec = MetricSpec(
                label=label,
                kind=kind,
                allowed_values=values,
                minimum=minimum,
                maximum=maximum,
                controllable=True,
                initial_value=values[0] if values else None,
            )
            handle = self.service.add_metric(spec)
        except (ValueError, TypeError) as exc:
            print(f"rejected: {exc}")
            return

        details = []
        if values:
            details.append("values " + ", ".join(values))
        if spec.has_range:
            details.append(spec.range_text())
        suffix = f", {'; '.join(details)}" if details else ""
        print(f"created {handle}  ({kind.value}{suffix})")

    def do_set(self, line: str) -> None:
        """set <handle> <value>  -  change the value of one of our data sources."""
        parts = shlex.split(line)
        if len(parts) < 2:  # noqa: PLR2004
            print("usage: set <handle> <value>")
            return
        handle, raw = parts[0], " ".join(parts[1:])
        try:
            value: Decimal | str = Decimal(raw)
        except InvalidOperation:
            value = raw
        try:
            self.service.set_value(handle, value)
        except KeyError:
            print(f"no such handle: {handle}")
            return
        print(f"{handle} = {show(self.service.get_value(handle))}")

    def do_control(self, line: str) -> None:
        """control <handle> <on|off>  -  allow or refuse remote writes."""
        parts = shlex.split(line)
        if len(parts) != 2:  # noqa: PLR2004
            print("usage: control <handle> <on|off>")
            return
        handle, state = parts[0], parts[1].lower()
        try:
            if state == "on":
                operation = self.service.enable_control(handle)
                print(f"{handle} is controllable via {operation}")
            elif state == "off":
                self.service.disable_control(handle)
                print(f"{handle} now refuses remote writes")
            else:
                print("usage: control <handle> <on|off>")
        except (KeyError, ValueError) as exc:
            print(f"rejected: {exc}")

    def do_remove(self, line: str) -> None:
        """remove <handle>  -  delete a data source and its operation."""
        handle = line.strip()
        if not handle:
            print("usage: remove <handle>")
            return
        try:
            self.service.remove_metric(handle)
        except KeyError:
            print(f"no such handle: {handle}")
            return
        print(f"removed {handle}")

    def do_list(self, _line: str) -> None:
        """list  -  show our data sources, their values and whether control is on."""
        specs = self.service.list_metrics()
        if not specs:
            print("nothing published yet, try: add number Zoom level")
            return
        print(f"  {'handle':<24} {'kind':<8} {'value':<14} {'range':<14} control")
        for handle, spec in sorted(specs.items()):
            operation = self.service.operation_handle_for(handle)
            control = "-" if operation is None else operation
            value = show(self.service.get_value(handle))
            print(f"  {handle:<24} {spec.kind.value:<8} {value:<14} {spec.range_text():<14} {control}")

    def do_alert(self, line: str) -> None:
        """alert <source handle> <label...> [min..max]

        Adds an alarm condition watching one of our metrics, plus a visual and an audible
        signal for it. With limits the alarm follows the metric by itself; without them use
        `raise` and `clear`.

            alert m.pressure Pressure out of range 20..100
            alert m.pressure Service due
        """
        parts = shlex.split(line)
        if len(parts) < 2:  # noqa: PLR2004
            print("usage: alert <source handle> <label...> [min..max]")
            return

        source, *rest = parts
        lower = upper = None
        if rest and ".." in rest[-1]:
            low, _, high = rest.pop().partition("..")
            try:
                lower = Decimal(low) if low else None
                upper = Decimal(high) if high else None
            except InvalidOperation:
                print(f"could not read limits from {low!r}..{high!r}")
                return
        if not rest:
            print("usage: alert <source handle> <label...> [min..max]")
            return

        try:
            spec = AlertSpec(
                label=" ".join(rest),
                source_handle=source,
                lower_limit=lower,
                upper_limit=upper,
            )
            handle = self.service.add_alert(spec)
        except (KeyError, ValueError, TypeError) as exc:
            print(f"rejected: {exc}")
            return
        print(f"created {handle}  ({spec.limit_text() or 'manual'})")
        print(f"  signals: {', '.join(self.service.signal_handles_for(handle))}")

    def do_raise(self, line: str) -> None:
        """raise <alarm handle>  -  raise an alarm that has no limits."""
        self._set_alert(line, present=True)

    def do_clear(self, line: str) -> None:
        """clear <alarm handle>  -  clear an alarm that has no limits."""
        self._set_alert(line, present=False)

    def _set_alert(self, line: str, *, present: bool) -> None:
        handle = line.strip()
        if not handle:
            print("usage: raise|clear <alarm handle>")
            return
        try:
            self.service.set_alert_presence(handle, present)
        except KeyError:
            print(f"no such alarm: {handle}")
            return
        print(f"{handle} is now {'present' if present else 'clear'}")

    def do_alerts(self, _line: str) -> None:
        """alerts  -  show our alarms, what they watch and whether they are raised."""
        alerts = self.service.list_alerts()
        if not alerts:
            print("no alarms yet, try: alert m.pressure Pressure high 0..100")
            return
        print(f"  {'handle':<22} {'watches':<18} {'when':<24} state")
        for handle, spec in sorted(alerts.items()):
            state = "PRESENT" if self.service.alert_present(handle) else "clear"
            print(f"  {handle:<22} {spec.source_handle:<18} {spec.limit_text() or 'manual':<24} {state}")

    def do_export(self, line: str) -> None:
        """export <file>  -  write the current data sources and alarms to a config file."""
        path = line.strip()
        if not path:
            print("usage: export <file>")
            return
        try:
            written = config.save(self.service, path)
        except OSError as exc:
            print(f"could not write: {exc}")
            return
        print(
            f"wrote {len(self.service.list_metrics())} data source(s) "
            f"and {len(self.service.list_alerts())} alarm(s) to {written}",
        )

    def do_import(self, line: str) -> None:
        """import <file>  -  replace everything with the device described in a config file."""
        path = line.strip()
        if not path:
            print("usage: import <file>")
            return
        try:
            metrics, alarms = config.load_into(self.service, path)
        except config.ConfigError as exc:
            print(f"could not import: {exc}")
            return
        print(f"loaded {metrics} data source(s) and {alarms} alarm(s)")

    def do_quit(self, _line: str) -> bool:
        """quit  -  stop the provider and exit."""
        return True

    do_EOF = do_quit  # noqa: N815 - Cmd looks for this exact name

    def emptyline(self) -> None:
        """Do nothing on an empty line, rather than repeating the last command."""


class ConsumerShell(Cmd):
    """Commands for looking at, and controlling, somebody else's device."""

    prompt = "consumer> "

    def __init__(self, service: ConsumerService) -> None:
        super().__init__()
        self.service = service
        self.devices: list = []
        self.remote = None
        self.updates: list[str] = []
        self.intro = (
            f"\nDiscovery on {service.ip}\n\n"
            "Try:  scan\n"
            "      connect 0\n"
            "      list\n"
            "      set m.zoom_level 9\n"
            "Type help for everything else, quit to stop.\n"
        )

    # -- commands ------------------------------------------------------------------

    def do_scan(self, line: str) -> None:
        """scan [seconds]  -  look for SDC providers on the network."""
        timeout = float(line.strip() or 10)
        print(f"searching for {timeout:.0f}s ...")
        self.devices = self.service.scan(timeout=timeout)
        if not self.devices:
            print("nothing found")
            return
        for index, device in enumerate(self.devices):
            location = device.location_scope or "no location"
            print(f"  [{index}] {device.epr}")
            print(f"      {location}")

    def do_connect(self, line: str) -> None:
        """connect <index>  -  connect to a provider from the last scan."""
        if not self.devices:
            print("run scan first")
            return
        try:
            device = self.devices[int(line.strip() or 0)]
        except (ValueError, IndexError):
            print(f"usage: connect <0..{len(self.devices) - 1}>")
            return

        if self.remote is not None:
            self.remote.close()
        self.remote = self.service.connect(device)
        self.updates.clear()
        self.remote.bind(
            metrics_by_handle=self._on_metrics,
            new_descriptors_by_handle=self._on_new_descriptors,
        )
        print(f"connected to {device.epr}")
        print(f"{len(self.remote.mdib.entities)} entities in its MDIB")

    def do_list(self, _line: str) -> None:
        """list  -  show the peer's metrics and which of them we may write."""
        if not self._require_connection():
            return
        metrics = self.remote.metrics()
        if not metrics:
            print("the peer publishes no metrics")
            return
        print(f"  {'handle':<24} {'kind':<8} {'value':<14} {'range':<14} {'unit':<10} writable")
        for handle, metric in sorted(metrics.items()):
            if metric.controllable_now:
                writable = "yes"
            elif metric.controllable:
                writable = "disabled"
            else:
                writable = "no"
            label = metric.label or "?"
            print(
                f"  {handle:<24} {metric.kind.value if metric.kind else '?':<8} "
                f"{show(metric.value):<14} {metric.range_text():<14} "
                f"{(metric.unit_label or ''):<10} {writable}",
            )
            if metric.allowed_values:
                print(f"      allowed: {', '.join(metric.allowed_values)}    ({label})")

    def do_set(self, line: str) -> None:
        """set <handle> <value>  -  remote-control a metric on the peer."""
        if not self._require_connection():
            return
        parts = shlex.split(line)
        if len(parts) < 2:  # noqa: PLR2004
            print("usage: set <handle> <value>")
            return
        handle, raw = parts[0], " ".join(parts[1:])
        try:
            value: Decimal | str = Decimal(raw)
        except InvalidOperation:
            value = raw

        state = self.remote.set_value(handle, value)
        if state in (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD):
            time.sleep(0.6)
            print(f"accepted, {handle} is now {show(self.remote.metrics()[handle].value)}")
        else:
            print(f"refused by the provider ({state})")

    def do_watch(self, line: str) -> None:
        """watch [seconds]  -  print changes as they arrive."""
        if not self._require_connection():
            return
        seconds = float(line.strip() or 15)
        print(f"watching for {seconds:.0f}s, Ctrl+C to stop early")
        start = len(self.updates)
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                while len(self.updates) > start:
                    print(f"  {self.updates[start]}")
                    start += 1
                time.sleep(0.2)
        except KeyboardInterrupt:
            print("  stopped")

    def do_alerts(self, _line: str) -> None:
        """alerts  -  show the peer's alarms, what they watch and how they are signalled."""
        if not self._require_connection():
            return
        alerts = self.remote.alerts()
        if not alerts:
            print("the peer publishes no alarms")
            return
        print(f"  {'handle':<22} {'kind':<5} {'prio':<5} {'limits':<16} state")
        for handle, alert in sorted(alerts.items()):
            state = "PRESENT" if alert.present else "clear"
            print(
                f"  {handle:<22} {(alert.kind or '?'):<5} {(alert.priority or '?'):<5} "
                f"{alert.limit_text():<16} {state}",
            )
            details = []
            if alert.label:
                details.append(alert.label)
            if alert.source_handles:
                details.append("watches " + ", ".join(alert.source_handles))
            if alert.signals:
                details.append("signals " + ", ".join(sorted(alert.signals.values())))
            if details:
                print(f"      {' | '.join(details)}")

    def do_quit(self, _line: str) -> bool:
        """quit  -  disconnect and exit."""
        if self.remote is not None:
            self.remote.close()
        return True

    do_EOF = do_quit  # noqa: N815 - Cmd looks for this exact name

    def emptyline(self) -> None:
        """Do nothing on an empty line."""

    # -- internals -----------------------------------------------------------------

    def _require_connection(self) -> bool:
        if self.remote is None:
            print("not connected, run scan and then connect")
            return False
        return True

    def _on_metrics(self, metrics_by_handle: dict) -> None:
        # Runs on an sdc11073 thread. Only append to a list; printing happens in `watch`.
        for handle, state in metrics_by_handle.items():
            value = getattr(getattr(state, "MetricValue", None), "Value", None)
            self.updates.append(f"{handle} = {show(value)}")

    def _on_new_descriptors(self, descriptors_by_handle: dict) -> None:
        for handle in descriptors_by_handle:
            self.updates.append(f"NEW {handle}")


def run_provider(args: argparse.Namespace) -> int:
    service = ProviderService(ip=args.ip, instance_name=args.name)
    service.start()
    try:
        if args.config:
            try:
                metrics, alarms = config.load_into(service, args.config)
            except config.ConfigError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            print(f"loaded {metrics} data source(s) and {alarms} alarm(s) from {args.config}")
        ProviderShell(service).cmdloop()
    except KeyboardInterrupt:
        print()
    finally:
        service.stop()
        print("provider stopped")
    return 0


def run_consumer(args: argparse.Namespace) -> int:
    with ConsumerService(ip=args.ip) as service:
        try:
            ConsumerShell(service).cmdloop()
        except KeyboardInterrupt:
            print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("provider", "consumer"))
    parser.add_argument("--ip", default=constants.DEFAULT_IP, help="interface to bind discovery to")
    parser.add_argument("--name", default="alpha", help="provider instance name, decides the EPR")
    parser.add_argument("--config", help="config file to load on startup (provider mode)")
    parser.add_argument("--verbose", action="store_true", help="show sdc11073 logging")
    args = parser.parse_args()

    basic_logging_setup(level=logging.INFO if args.verbose else logging.WARNING)

    return run_provider(args) if args.mode == "provider" else run_consumer(args)


if __name__ == "__main__":
    raise SystemExit(main())
