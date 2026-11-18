"""Interactive console for driving an SDC device by hand.

Two modes. Open a terminal for each:

    .venv/Scripts/python.exe examples/console.py provider --name alpha
    .venv/Scripts/python.exe examples/console.py consumer

The provider shell creates data sources and changes their values. The consumer shell finds
providers on the network, lists what they expose and remote-controls the parts that allow it.

Type `help` in either shell for the command list. The provider shell's `tool` command runs any
of the toolbox's MCP tools, so whatever an agent can do to My device can be typed here too.
"""

from __future__ import annotations

import argparse
import json
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

from sdctoolbox import config, constants, device_test, mdib_check, native_mdib, nomenclature  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.mcp.server import EXPECTED_ERRORS  # noqa: E402
from sdctoolbox.mcp.tools import Hooks, ToolboxTools, plain  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    AlertSpec,
    Coding,
    DistributionShape,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    PatientMeasurement,
    WaveformShape,
    patient_measurement_wire_value,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402
from sdctoolbox.mdib_io import NativeMdibConfig, parse_mdib  # noqa: E402

def _take_option(parts: list[str], name: str) -> tuple[str | None, list[str]]:
    """Pull "--name value" out of a token list, returning the value and what is left."""
    if name not in parts:
        return None, parts
    index = parts.index(name)
    value = parts[index + 1] if index + 1 < len(parts) else None
    return value, parts[:index] + parts[index + 2 :]


WAVEFORM_SHAPES = {shape.value: shape for shape in WaveformShape}
DISTRIBUTION_SHAPES = {shape.value: shape for shape in DistributionShape}

KIND_BY_NAME = {
    "number": MetricKind.NUMBER,
    "text": MetricKind.TEXT,
    "choice": MetricKind.CHOICE,
    "waveform": MetricKind.WAVEFORM,
    "distribution": MetricKind.DISTRIBUTION,
}


def show(value: object) -> str:
    """Render a metric value for the console."""
    return "-" if value is None else str(value)


def print_findings(findings: list[mdib_check.Finding]) -> None:
    """The MDIB check's findings, as the MDIB check window lists them."""
    print(f"  {mdib_check.summary(findings)}")
    for finding in findings:
        print(f"  {finding.level:<8} {finding.area:<15} {finding.where}")
        rule = f"  [{finding.rule}]" if finding.rule else ""
        print(f"      {finding.text}{rule}")
        if finding.fix is not None:
            print(f"      fix: {finding.fix.label}")


def print_codes(line: str) -> None:
    """MDC codes by name, number or reference id, as Pick an MDC code finds them."""
    kind, parts = _take_option(shlex.split(line), "--kind")
    if not parts:
        print("usage: codes <text> [--kind K]")
        return
    try:
        found = nomenclature.search(" ".join(parts), kind, 25)
    except ValueError as exc:
        print(f"rejected: {exc}")
        return
    for term in found:
        print(f"  {term.code:<8} {term.ref_id:<34} {term.label}")
    if not found:
        print("  no code matches")


def key_values(parts: list[str]) -> dict[str, str] | None:
    """name=value pairs; None (after saying so) when one has no '='."""
    bad = [part for part in parts if "=" not in part]
    if bad:
        print(f"expected name=value, found {' '.join(bad)}")
        return None
    return dict(part.partition("=")[::2] for part in parts)


def on_off(value: str) -> bool:
    if value.lower() in ("on", "true", "yes", "1"):
        return True
    if value.lower() in ("off", "false", "no", "0"):
        return False
    msg = f"{value!r} is not on or off"
    raise ValueError(msg)


def call(function, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN201
    """Run a tool function; a refusal is printed and gives None."""
    try:
        return function(*args, **kwargs)
    except EXPECTED_ERRORS as exc:
        print(f"rejected: {exc.args[0] if isinstance(exc, KeyError) and exc.args else exc}")
        return None


def print_result(result: object) -> None:
    if result is not None:
        print(result if isinstance(result, str) else json.dumps(plain(result), indent=2, ensure_ascii=False))


class ProviderShell(Cmd):
    """Commands for the device we publish ourselves."""

    prompt = "provider> "

    def __init__(self, service: ProviderService) -> None:
        super().__init__()
        self.service = service
        self.tools = ToolboxTools(service, None, Hooks(service))
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
        """add <kind> <label...> [choice values...] [min..max] [--shape S] [--cycle SECONDS]

        Creates a data source, remote-controllable where the standard allows it. Both
        sample-array kinds start generating as soon as they exist.

            add number Zoom level
            add number Zoom level 1..100
            add text Patient note
            add choice Mode IDLE RUN PAUSE
            add waveform Pleth 0..100 --shape pulse --cycle 1
            add waveform ECG -1..2 --shape ecg
            add distribution Spectrum 0..60 --shape spectrum

        --shape names the curve for a waveform or the shape for a distribution; `shapes`
        lists what is available. --cycle is how many seconds one cycle of a waveform takes;
        the sample period only sets how finely that cycle is drawn.

        In every case min..max is the range of the *values*. A distribution's domain -
        what those values are spread over - defaults to 0..1; use the window to set it.
        """
        parts = shlex.split(line)
        if len(parts) < 2:  # noqa: PLR2004
            print(f"usage: add <{'|'.join(KIND_BY_NAME)}> <label...>")
            return

        shape_name, parts = _take_option(parts, "--shape")
        cycle_name, parts = _take_option(parts, "--cycle")

        kind_name, *rest = parts
        kind = KIND_BY_NAME.get(kind_name.lower())
        if kind is None:
            print(f"unknown kind {kind_name!r}, expected one of {', '.join(KIND_BY_NAME)}")
            return

        # --shape means a different enum depending on the kind, which is the honest way
        # round: a sawtooth distribution and a bimodal waveform are both nonsense.
        shape = WaveformShape.SINE
        distribution_shape = DistributionShape.BELL
        if shape_name:
            table = WAVEFORM_SHAPES if kind is MetricKind.WAVEFORM else DISTRIBUTION_SHAPES
            chosen = table.get(shape_name.lower())
            if chosen is None:
                print(f"unknown shape {shape_name!r} for a {kind.value}. Try: shapes")
                return
            if kind is MetricKind.WAVEFORM:
                shape = chosen
            else:
                distribution_shape = chosen

        cycle_seconds = Decimal(4)
        if cycle_name:
            try:
                cycle_seconds = Decimal(cycle_name)
            except InvalidOperation:
                print(f"--cycle wants a number of seconds, not {cycle_name!r}")
                return

        minimum = maximum = None
        if rest and ".." in rest[-1] and kind is not MetricKind.TEXT and kind is not MetricKind.CHOICE:
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
                controllable=kind.controllable,
                initial_value=values[0] if values else None,
                shape=shape,
                cycle_seconds=cycle_seconds,
                distribution_shape=distribution_shape,
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

    def do_shapes(self, _line: str) -> None:
        """shapes  -  the curves and distribution shapes `add --shape` accepts."""
        print("  waveform, for testing a renderer:")
        print("    " + ", ".join(s.value for s in list(WaveformShape)[:4]))
        print("  waveform, shaped like a real signal:")
        print("    " + ", ".join(s.value for s in list(WaveformShape)[4:]))
        print("  distribution:")
        print("    " + ", ".join(s.value for s in DistributionShape))
        print("  none of these is a BICEPS concept; the standard says nothing about shape")

    def do_samples(self, line: str) -> None:
        """samples <handle> [v1 v2 v3 ...]  -  show or set a sample array.

        With no values it prints what is published. With values it replaces them, which is
        how a distribution is driven; a waveform generates its own, so this shows the last
        block that went out.

            samples m.spectrum 0 1 2 ... 31
            samples m.pleth
        """
        parts = shlex.split(line)
        if not parts:
            print("usage: samples <handle> [v1 v2 ...] (distributions need exactly 32 values)")
            return
        handle, rest = parts[0], parts[1:]
        if rest:
            try:
                block = [Decimal(value) for value in rest]
            except InvalidOperation:
                print("every sample has to be a number")
                return
            try:
                self.service.set_samples(handle, block)
            except (KeyError, ValueError, TypeError) as exc:
                print(f"rejected: {exc}")
                return
        current = self.service.get_samples(handle)
        shown = " ".join(str(value) for value in current[:12])
        suffix = " ..." if len(current) > 12 else ""  # noqa: PLR2004
        print(f"  {len(current)} sample(s): {shown}{suffix}")

    def do_generator(self, line: str) -> None:
        """generator [on|off]  -  show or change whether sample generation is running.

        Drives waveforms and distributions both. Setting a block with `samples` takes that
        metric off the generator, so it keeps what you gave it.
        """
        text = line.strip().lower()
        if text == "on":
            try:
                self.service.start_generator()
            except ValueError as exc:
                print(f"rejected: {exc}")
                return
        elif text == "off":
            self.service.stop_generator()
        elif text:
            print("usage: generator [on|off]")
            return
        print(f"  generator {'running' if self.service.generator_running else 'stopped'}")

    def do_set(self, line: str) -> None:
        """set <handle> <value>  -  change the value of one of our data sources."""
        parts = shlex.split(line)
        if len(parts) < 2:  # noqa: PLR2004
            print("usage: set <handle> <value>")
            return
        handle, raw = parts[0], " ".join(parts[1:])
        try:
            self.service.set_value(handle, raw)
        except KeyError:
            print(f"no such handle: {handle}")
            return
        except (TypeError, ValueError) as exc:
            print(f"rejected: {exc}")
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
        """remove <handle>  -  delete a data source and its dependencies, or an action."""
        handle = line.strip()
        if not handle:
            print("usage: remove <handle>")
            return
        try:
            if handle in self.service.list_actions():
                self.service.remove_action(handle)
            else:
                self.service.remove_metric(handle)
        except KeyError:
            print(f"no such handle: {handle}")
            return
        except ValueError as exc:
            print(f"rejected: {exc}")
            return
        print(f"removed {handle}")

    def do_list(self, _line: str) -> None:
        """list  -  show our data sources, their values and whether control is on."""
        specs = self.service.list_metrics()
        if not specs:
            print("nothing published yet, try: add number Zoom level")
            return
        print(f"  {'handle':<24} {'kind':<13} {'value':<14} {'range':<16} control")
        for handle, spec in sorted(specs.items()):
            operation = self.service.operation_handle_for(handle)
            control = "-" if operation is None else operation
            if spec.is_sample_array:
                value = f"{len(self.service.get_samples(handle))} sample(s)"
                control = "-"
            else:
                value = show(self.service.get_value(handle))
            # Always the value range here, so the column means the same thing on every
            # row. A distribution's domain is a different axis and gets its own line.
            print(f"  {handle:<24} {spec.kind.value:<13} {value:<14} {spec.range_text():<16} {control}")
            if spec.kind is MetricKind.DISTRIBUTION:
                print(f"      spread over {spec.domain_text()}")
            if spec.kind is MetricKind.WAVEFORM:
                print(f"      {spec.sample_period}s per sample, {spec.shape.value}")

    def do_alert(self, line: str) -> None:
        """alert <source handle> <label...> [min..max] [--delegable]

        Adds an alarm condition watching one of our metrics, plus a visual and an audible
        signal for it. With limits the alarm follows the metric by itself; without them use
        `raise` and `clear`. `--delegable` lets another device take the signals over.

            alert m.pressure Pressure out of range 20..100
            alert m.pressure Overpressure 0..30 --delegable
            alert m.pressure Service due
        """
        parts = shlex.split(line)
        delegable = "--delegable" in parts
        parts = [part for part in parts if part != "--delegable"]
        if len(parts) < 2:  # noqa: PLR2004
            print("usage: alert <source handle> <label...> [min..max] [--delegable]")
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
            print("usage: alert <source handle> <label...> [min..max] [--delegable]")
            return

        try:
            spec = AlertSpec(
                label=" ".join(rest),
                source_handle=source,
                lower_limit=lower,
                upper_limit=upper,
                delegable=delegable,
            )
            handle = self.service.add_alert(spec)
        except (KeyError, ValueError, TypeError) as exc:
            print(f"rejected: {exc}")
            return
        print(f"created {handle}  ({spec.limit_text() or 'manual'})")
        print(f"  signals: {', '.join(self.service.signal_handles_for(handle))}")

    def do_ack(self, line: str) -> None:
        """ack <alarm handle>  -  acknowledge an alarm's signals.

        The condition stays present: acknowledging changes how an alarm is announced, not
        whether it is true. That is the whole reason BICEPS keeps conditions and signals
        apart, so watch `alerts` before and after.
        """
        handle = line.strip()
        if not handle:
            print("usage: ack <alarm handle>")
            return
        try:
            count = self.service.acknowledge_alert(handle)
        except (KeyError, ValueError) as exc:
            print(f"rejected: {exc}")
            return
        print(f"acknowledged {count} signal(s); {handle} is still present")

    def do_delegate(self, line: str) -> None:
        """delegate <alarm handle> [on|off]  -  hand its signals to another device.

        Moves each signal's Location from Loc to Rem. Only works where the alarm was
        created --delegable, because BICEPS allows it only where the descriptor says so.
        """
        parts = shlex.split(line)
        if not parts:
            print("usage: delegate <alarm handle> [on|off]")
            return
        handle = parts[0]
        delegated = parts[1].lower() != "off" if len(parts) > 1 else True

        signals = [signal for signal in self.service.signal_states(handle) if signal.delegable]
        if not signals:
            print(f"{handle} has no delegable signals; create it with --delegable")
            return
        try:
            for signal in signals:
                self.service.set_signal_delegated(signal.handle, delegated=delegated)
        except (KeyError, ValueError) as exc:
            print(f"rejected: {exc}")
            return
        print(f"  {' '.join(signal.summary() for signal in self.service.signal_states(handle))}")

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
        except ValueError as exc:
            print(f"rejected: {exc}")
            return
        print(f"{handle} is now {'present' if present else 'clear'}")

    def do_alerts(self, _line: str) -> None:
        """alerts  -  show our alarms, what they watch, and how their signals stand."""
        alerts = self.service.list_alerts()
        if not alerts:
            print("no alarms yet, try: alert m.pressure Pressure high 0..100")
            return
        print(f"  {'handle':<22} {'watches':<18} {'when':<20} {'state':<8} signals")
        for handle, spec in sorted(alerts.items()):
            state = "PRESENT" if self.service.alert_present(handle) else "clear"
            signals = " ".join(signal.summary() for signal in self.service.signal_states(handle))
            print(
                f"  {handle:<22} {spec.source_handle:<18} "
                f"{spec.limit_text() or 'manual':<20} {state:<8} {signals}",
            )

    def do_where(self, line: str) -> None:
        """where [facility/building/floor/poc/room/bed]  -  show or set the location.

        The location is also a WS-Discovery scope, so setting it re-announces this device
        and consumers filtering on the old one stop seeing it.

            where
            where HOSP/Surgery/2/OR1/1/Table
            where HOSP//     (only the parts you give; empty ones are cleared)
        """
        text = line.strip()
        if not text:
            print(f"  {self.service.get_location().summary() or 'nowhere'}")
            return
        parts = (text.split("/") + [""] * 6)[:6]
        try:
            self.service.set_location(
                LocationInfo(
                    facility=parts[0].strip(),
                    building=parts[1].strip(),
                    floor=parts[2].strip(),
                    point_of_care=parts[3].strip(),
                    room=parts[4].strip(),
                    bed=parts[5].strip(),
                ),
            )
        except (RuntimeError, ValueError) as exc:
            print(f"rejected: {exc}")
            return
        print(f"  {self.service.get_location().summary() or 'nowhere'}")

    def do_patient(self, line: str) -> None:
        """patient [given family [sex] [type] [birth]] [demographic options]  -  set patient.

        `patient` on its own shows who is attached, `patient off` detaches them. Setting one
        does not overwrite the last: the previous state is disassociated and kept, which is
        what makes a context different from a metric.

            patient Ada Lovelace F Ad 1815-12-10
            patient Ada Lovelace --height 170.5 --height-unit 264184 --height-system mdc --height-label cm
            patient Ada Lovelace --weight 72.4 --weight-unit 266016 --weight-system mdc --weight-label kg
            patient Ada Lovelace --race 2054-5 --race-system urn:oid:2.16.840.1.113883.6.238 --race-label "Black or African American"
            patient off
        """
        parts = shlex.split(line)
        if not parts:
            print(f"  {self.service.get_patient().summary() or 'nobody attached'}")
            return
        if parts[0].lower() == "off":
            try:
                self.service.clear_patient()
            except ValueError as exc:
                print(f"rejected: {exc}")
                return
            print("  nobody attached")
            return

        options: dict[str, str | None] = {}
        option_names = (
            "--height",
            "--height-unit",
            "--height-system",
            "--height-label",
            "--weight",
            "--weight-unit",
            "--weight-system",
            "--weight-label",
            "--race",
            "--race-system",
            "--race-label",
        )
        provided = {name for name in option_names if name in parts}
        for name in option_names:
            value, parts = _take_option(parts, name)
            options[name] = value
        missing = [name for name in provided if options[name] is None]
        if missing:
            print(f"usage: {missing[0]} needs a value")
            return
        unknown = [part for part in parts if part.startswith("--")]
        if unknown:
            print(f"usage: unknown patient option {unknown[0]}")
            return

        def measurement(name: str) -> PatientMeasurement | None:
            value = options[f"--{name}"]
            unit = options[f"--{name}-unit"]
            system = options[f"--{name}-system"]
            label = options[f"--{name}-label"] or ""
            if not any((value, unit, system, label)):
                return None
            if not value or not unit or not system:
                raise ValueError(f"{name} needs --{name}, --{name}-unit and --{name}-system")
            try:
                decimal_value = Decimal(value)
                patient_measurement_wire_value(decimal_value)
                return PatientMeasurement(
                    value=decimal_value,
                    unit=Coding(code=unit, system=system, label=label),
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"{name}: {exc}") from exc

        try:
            height = measurement("height")
            weight = measurement("weight")
            race_code = options["--race"]
            race_system = options["--race-system"]
            race_label = options["--race-label"] or ""
            if any((race_code, race_system, race_label)) and (not race_code or not race_system):
                raise ValueError("race needs --race and --race-system")
            race = Coding(code=race_code, system=race_system, label=race_label) if race_code else None
        except (TypeError, ValueError) as exc:
            print(f"rejected: {exc}")
            return

        if len(parts) > 5:  # noqa: PLR2004
            print("usage: patient [given family [sex] [type] [birth]] [demographic options]")
            return
        padded = (parts + [""] * 5)[:5]
        try:
            self.service.set_patient(
                PatientInfo(
                    given_name=padded[0],
                    family_name=padded[1],
                    sex=padded[2],
                    patient_type=padded[3],
                    date_of_birth=padded[4],
                    height=height,
                    weight=weight,
                    race=race,
                ),
            )
        except (RuntimeError, ValueError, TypeError) as exc:
            print(f"rejected: {exc}")
            return
        print(f"  {self.service.get_patient().summary()}")

    def do_presets(self, _line: str) -> None:
        """presets  -  list the ready-made configs, then load one with `import`."""
        presets = config.list_presets()
        if not presets:
            print("no presets found")
            return
        for preset in presets:
            print(f"  {preset.name:<18} {preset.summary()}")
            if preset.description:
                print(f"      {preset.description}")
            print(f"      import {preset.path}")

    def do_export(self, line: str) -> None:
        """export [--plain] <file.xml>  -  write the device as an MDIB with toolbox behaviour; --plain leaves the behaviour out."""
        path = line.strip()
        behaviour = not path.startswith("--plain ")
        path = path.removeprefix("--plain ").strip()
        if not path:
            print("usage: export [--plain] <file.xml>")
            return
        try:
            written = config.save(self.service, path, behaviour=behaviour)
        except (OSError, config.ConfigError) as exc:
            print(f"could not write: {exc}")
            return
        print(
            f"wrote {len(self.service.list_metrics())} data source(s), "
            f"{len(self.service.list_alerts())} alarm(s), and contexts to {written}",
        )

    def do_import(self, line: str) -> None:
        """import <file>  -  replace everything with the device described in a config file."""
        path = line.strip()
        if not path:
            print("usage: import <file>")
            return
        try:
            snapshot = config.load_file(path)
            restart = isinstance(snapshot, NativeMdibConfig) or self.service.from_mdib_file
            if restart:
                print("Replacing the MDIB restarts the provider; connected consumers must reconnect.")
            metrics, alarms = config.apply_to(self.service, snapshot)
        except config.ConfigError as exc:
            print(f"could not import: {exc}")
            return
        print(f"loaded {metrics} data source(s) and {alarms} alarm(s)")

    def do_actions(self, _line: str) -> None:
        """actions  -  list our actions (activate operations) and what each sets."""
        actions = self.service.list_actions()
        if not actions:
            print("no actions yet, try: action Reset zoom m.zoom_level=1")
            return
        for handle, spec in sorted(actions.items()):
            effects = ", ".join(f"{h}={v}" for h, v in sorted(spec.effects.items())) or "nothing"
            print(f"  {handle:<24} {spec.label:<20} on {spec.target_handle}, sets {effects}")

    def do_action(self, line: str) -> None:
        """action <label...> [handle=value ...] [--target HANDLE]  -  add an action that sets values when run.

            action Reset zoom m.zoom_level=1
            action Park m.mode=IDLE m.zoom_level=0 --target vmd0

        The target is what it acts on; the MDS when left out.
        """
        target, parts = _take_option(shlex.split(line), "--target")
        label = " ".join(part for part in parts if "=" not in part)
        effects = key_values([part for part in parts if "=" in part])
        if not label or effects is None:
            print("usage: action <label...> [handle=value ...] [--target HANDLE]")
            return
        target = target or self.service._anchor_mds()  # noqa: SLF001 - where the window adds actions too
        print_result(call(self.tools.add_action, {"label": label, "target": target, "effects": effects}))

    def do_run(self, line: str) -> None:
        """run <action handle>  -  run one of our actions, as its button does."""
        print_result(call(self.tools.run_action, line.strip()))

    def do_faults(self, line: str) -> None:
        """faults [reset]  -  show the simulated faults (Advanced mode in the window), or end them all."""
        result = call(self.tools.reset_faults if line.strip() == "reset" else self.tools.get_faults)
        if result is None:
            return
        print("  device: " + ", ".join(f"{name}={value}" for name, value in result["device"].items()))
        for metric in result["metrics"]:
            mark = "*" if metric["faulted"] else " "
            print(f" {mark}{metric['handle']:<24} {metric['activation']:<8} {metric['validity']:<6} "
                  f"{metric['mode']:<6} {metric['consumers_see']}")

    def do_fault(self, line: str) -> None:
        """fault <handle> [activation=X] [validity=Y] [mode=Z]  -  fake a sensor state or value quality.
        fault device [answer_delay_ms=N] [refuse_writes=on] [pause_reports=on] [leave_without_bye=on] [drop_subscriptions=on]

            fault m.pressure validity=Inv
            fault m.pressure activation=Fail
            fault device answer_delay_ms=2000 refuse_writes=on

        `faults` shows them, `faults reset` ends them all.
        """
        target, *rest = shlex.split(line) or [""]
        settings = key_values(rest)
        if not target or settings is None:
            print("usage: fault <handle|device> name=value ...")
            return
        if target != "device":
            print_result(call(self.tools.set_metric_fault, target, **settings))
            return
        try:
            device = {name: int(value) if name == "answer_delay_ms" else on_off(value)
                      for name, value in settings.items()}
        except ValueError as exc:
            print(f"rejected: {exc}")
            return
        print_result(call(self.tools.set_device_faults, **device))

    def do_check(self, _line: str) -> None:
        """check  -  check our MDIB against the SDC standards, as the MDIB check window does."""
        print_findings(mdib_check.check(self.service.mdib))

    def do_fix(self, line: str) -> None:
        """fix <handle> [fix label...]  -  make a fix `check` offers, e.g. fix m.pulse Use MDC_PULS_RATE."""
        handle, _, label = line.strip().partition(" ")
        if not handle:
            print("usage: fix <handle> [fix label]")
            return
        print_result(call(self.tools.fix_mdib, handle, label.strip() or None))

    def do_codes(self, line: str) -> None:
        """codes <text> [--kind K]  -  search the MDC codes, e.g. codes pulse rate."""
        print_codes(line)

    def do_tool(self, line: str) -> None:
        """tool [name] [JSON arguments]  -  run any of the toolbox's MCP tools; `tool` alone lists them.

            tool update_metric {"handle": "m.zoom_level", "unit": {"code": "262688"}}
            tool set_contexts {"patient": {"given_name": "Ada"}}
        """
        name, _, raw = line.strip().partition(" ")
        tools = {tool.name: tool for tool in self.tools.tools()}
        if name not in tools:
            if name:
                print(f"no tool {name!r}")
            for tool in tools.values():
                print(f"  {tool.name:<20} {tool.description.split('. ')[0]}")
            return
        try:
            arguments = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            print(f"the arguments are not JSON: {exc}")
            return
        if not isinstance(arguments, dict):
            print("the arguments are a JSON object, such as {\"handle\": \"m.zoom_level\"}")
            return
        print_result(call(tools[name].function, **arguments))

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
        self.remotes: dict[int, object] = {}  # connection number -> RemoteDevice, numbered like the window's tabs
        self.current: int | None = None
        self.updates: list[str] = []
        self.intro = (
            f"\nDiscovery on {service.ip}\n\n"
            "Try:  scan\n"
            "      connect 0\n"
            "      list\n"
            "      set m.zoom_level 9\n"
            "      test\n"
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

    @property
    def remote(self):  # noqa: ANN201 - the RemoteDevice commands act on
        return self.remotes.get(self.current)

    def do_connect(self, line: str) -> None:
        """connect <index>  -  connect to a provider from the last scan; earlier connections stay open."""
        if not self.devices:
            print("run scan first")
            return
        try:
            device = self.devices[int(line.strip() or 0)]
        except (ValueError, IndexError):
            print(f"usage: connect <0..{len(self.devices) - 1}>")
            return

        for number, remote in self.remotes.items():
            if remote.epr == device.epr:
                self.current = number
                print(f"already connected as connection {number}, now using it")
                return
        remote = self.service.connect(device)
        number = max(self.remotes, default=0) + 1
        self.remotes[number], self.current = remote, number
        remote.bind(
            metrics_by_handle=lambda metrics: self._on_metrics(number, metrics),
            new_descriptors_by_handle=lambda descriptors: self._on_new_descriptors(number, descriptors),
        )
        print(f"connection {number}: {device.epr}")
        print(f"{len(remote.mdib.entities)} entities in its MDIB")

    def do_connections(self, _line: str) -> None:
        """connections  -  list the open connections; * marks the one commands act on."""
        if not self.remotes:
            print("no connections, run scan and then connect")
        for number, remote in sorted(self.remotes.items()):
            print(f" {'*' if number == self.current else ' '}{number}  {remote.friendly_name or '?'}  {remote.epr}")

    def do_use(self, line: str) -> None:
        """use <connection>  -  make commands act on another open connection."""
        try:
            number = int(line.strip())
        except ValueError:
            number = None
        if number not in self.remotes:
            print(f"usage: use <{'|'.join(map(str, sorted(self.remotes))) or 'connection'}>")
            return
        self.current = number
        print(f"using connection {number}")

    def do_disconnect(self, line: str) -> None:
        """disconnect [connection]  -  close a connection, the current one by default."""
        try:
            number = int(line.strip()) if line.strip() else self.current
        except ValueError:
            number = None
        remote = self.remotes.pop(number, None)
        if remote is None:
            print("usage: disconnect [connection]")
            return
        remote.close()
        if self.current == number:
            self.current = max(self.remotes, default=None)
        print(f"closed connection {number}")

    def do_actions(self, _line: str) -> None:
        """actions  -  the peer's actions (activate operations)."""
        if not self._require_connection():
            return
        actions = self.remote.actions()
        if not actions:
            print("the peer publishes no actions")
        for handle, action in sorted(actions.items()):
            state = "enabled" if action.enabled else "disabled"
            print(f"  {handle:<24} {(action.label or '?'):<24} on {action.target_handle or '?'}  {state}")

    def do_run(self, line: str) -> None:
        """run <action handle>  -  ask the peer to run one of its actions."""
        if not self._require_connection():
            return
        handle = line.strip()
        if not handle:
            print("usage: run <action handle>")
            return
        state = call(self.remote.run_action, handle)
        if state in (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD):
            print("accepted")
        elif state is not None:
            print(f"refused by the provider ({state})")

    def do_check(self, _line: str) -> None:
        """check  -  check the peer's MDIB against the SDC standards."""
        if self._require_connection():
            print_findings(mdib_check.check(self.remote.mdib))

    def do_test(self, _line: str) -> None:
        """test  -  run the Device test against the peer. It changes values, so use a test device only."""
        if not self._require_connection():
            return
        print("testing ...")
        results = device_test.run(self.service, self.remote)
        for result in results:
            print(f"  {result.result:<8} {result.test:<26} {result.happened}")
            print(f"           {result.standard}")
        print(f"  {device_test.summary(results)}")

    def do_messages(self, line: str) -> None:
        """messages [count] [--kind request|response|report] [--xml]  -  the SOAP messages with the peer, newest last.

        message <number> shows one message's XML.
        """
        if not self._require_connection():
            return
        parts = shlex.split(line)
        kind, parts = _take_option(parts, "--kind")
        xml = "--xml" in parts
        parts = [part for part in parts if part != "--xml"]
        try:
            count = int(parts[0]) if parts else 20
        except ValueError:
            print("usage: messages [count] [--kind request|response|report] [--xml]")
            return
        log = self.remote.messages
        picked = [m for m in log.messages() if kind in (None, m.kind)] if log is not None else []
        for message in picked[-count:] if count > 0 else []:
            moment = time.strftime("%H:%M:%S", time.localtime(message.time))
            print(f"  {message.number:>6} {moment} {message.direction:<8} {message.kind:<8} "
                  f"{message.summary}  ({message.size})")
            if xml:
                print(message.pretty())
        print(f"  {log.total if log is not None else 0} message(s) so far")

    def do_message(self, line: str) -> None:
        """message <number>  -  one message's XML, by the number `messages` lists."""
        if not self._require_connection():
            return
        log = self.remote.messages
        found = next((m for m in (log.messages() if log else []) if str(m.number) == line.strip()), None)
        if found is None:
            print("usage: message <number>, as messages lists it")
            return
        print(found.pretty())

    def do_codes(self, line: str) -> None:
        """codes <text> [--kind K]  -  search the MDC codes, e.g. codes pulse rate."""
        print_codes(line)

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

    def do_patient(self, _line: str) -> None:
        """patient  -  show associated patient demographics on the peer."""
        if not self._require_connection():
            return
        patients = self.remote.patient_contexts()
        if not patients:
            print("  nobody attached")
            return
        for handle, patient in patients.items():
            print(f"  {handle}: {patient.summary() or 'no demographics'}")

    def do_set(self, line: str) -> None:
        """set <handle> <value>  -  remote-control a metric on the peer."""
        if not self._require_connection():
            return
        parts = shlex.split(line)
        if len(parts) < 2:  # noqa: PLR2004
            print("usage: set <handle> <value>")
            return
        handle, raw = parts[0], " ".join(parts[1:])
        state = self.remote.set_value(handle, raw)
        if state in (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD):
            time.sleep(0.6)
            print(f"accepted, {handle} is now {show(self.remote.metrics()[handle].value)}")
        else:
            reason = self.remote.last_invocation_detail
            print(f"refused by the provider ({state})" + (f" - {reason}" if reason else ""))

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
        for remote in self.remotes.values():
            remote.close()
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

    def _on_metrics(self, number: int, metrics_by_handle: dict) -> None:
        # Runs on an sdc11073 thread. Only append to a list; printing happens in `watch`.
        for handle, state in metrics_by_handle.items():
            value = getattr(getattr(state, "MetricValue", None), "Value", None)
            self.updates.append(f"[{number}] {handle} = {show(value)}")

    def _on_new_descriptors(self, number: int, descriptors_by_handle: dict) -> None:
        for handle in descriptors_by_handle:
            self.updates.append(f"[{number}] NEW {handle}")


def run_provider(args: argparse.Namespace) -> int:
    # Before the provider exists: a preset says which machine it is, and sdc11073 fixes
    # ThisModel and ThisDevice at construction.
    device_config = None
    if args.config:
        try:
            device_config = config.load_file(args.config)
        except config.ConfigError as exc:
            print(f"error: {exc}")
            return 2

    device = friendly_name = serial_number = None
    if isinstance(device_config, NativeMdibConfig):
        # As run_toolbox does: the file says who the device is, the name stays unprefixed.
        device, serial_number = native_mdib.device_identity(parse_mdib(device_config.xml))
        friendly_name = device.friendly_name or args.name
    elif device_config is not None:
        device = device_config.device
    try:
        service = ProviderService(ip=args.ip, instance_name=args.name, device=device,
                                  friendly_name=friendly_name, serial_number=serial_number)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if isinstance(device_config, NativeMdibConfig):
        service.start(parse_mdib(device_config.xml))
    else:
        service.start()
    try:
        if args.config and device_config is not None:
            try:
                if isinstance(device_config, NativeMdibConfig):
                    metrics, alarms = len(service.list_metrics()), len(service.list_alerts())
                else:
                    metrics, alarms = config.apply_to(service, device_config, replace=True)
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("provider", "consumer"))
    parser.add_argument("--ip", default=constants.DEFAULT_IP, help="interface to bind discovery to")
    parser.add_argument(
        "--name",
        default=constants.DEFAULT_INSTANCE_NAME,
        help="provider instance name, decides the EPR",
    )
    parser.add_argument("--config", help="config file to load on startup (provider mode)")
    parser.add_argument("--verbose", action="store_true", help="show sdc11073 logging")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    basic_logging_setup(level=logging.INFO if args.verbose else logging.WARNING)

    return run_provider(args) if args.mode == "provider" else run_consumer(args)


if __name__ == "__main__":
    raise SystemExit(main())
