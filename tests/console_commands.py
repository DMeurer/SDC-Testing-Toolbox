"""Console input must preserve metric types and survive rejected writes."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from examples import console
from sdc11073.mdib import ProviderMdib
from sdc11073.xml_types import msg_types

from sdctoolbox import constants
from sdctoolbox.model import MetricKind, MetricSpec
from sdctoolbox.provider_service import ProviderService


class ConsoleCommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = ProviderService(instance_name="console-regression")
        self.provider._mdib = ProviderMdib.from_mdib_file(str(constants.BOOTSTRAP_MDIB_PATH))
        for spec in (
            MetricSpec("Number", MetricKind.NUMBER, handle="m.number", minimum=Decimal(0),
                       maximum=Decimal(30), initial_value=Decimal(7)),
            MetricSpec("Text", MetricKind.TEXT, handle="m.text", initial_value="old"),
            MetricSpec("Choice", MetricKind.CHOICE, handle="m.choice", allowed_values=("001", "RUN"),
                       initial_value="RUN"),
        ):
            self.provider.add_metric(spec)
        self.addCleanup(self.provider.stop)

    def run_commands(self, shell, commands: list[str]) -> str:
        output = io.StringIO()
        shell.stdout = output
        shell.cmdqueue = [*commands, "quit"]
        try:
            with redirect_stdout(output):
                shell.cmdloop()
        except (KeyError, TypeError, ValueError) as exc:
            self.fail(f"an expected input rejection escaped the command loop: {exc}")
        return output.getvalue()

    def test_rejected_provider_write_keeps_command_loop_alive(self) -> None:
        output = self.run_commands(
            console.ProviderShell(self.provider),
            ["set m.number 80", "set m.number 9"],
        )
        self.assertIn("rejected", output.lower())
        self.assertEqual(self.provider.get_value("m.number"), Decimal(9))

    def test_waveform_cycle_is_given_in_seconds(self) -> None:
        self.provider.stop_generator = lambda: None
        output = self.run_commands(
            console.ProviderShell(self.provider),
            ["add waveform Pleth 0..100 --shape pulse --cycle 1.5", "add waveform Bad 0..1 --cycle x"],
        )
        waves = [spec for spec in self.provider.list_metrics().values() if spec.kind is MetricKind.WAVEFORM]
        self.assertEqual([spec.cycle_seconds for spec in waves], [Decimal("1.5")])
        self.assertIn("number of seconds", output)

    def test_provider_uses_target_metric_type(self) -> None:
        self.run_commands(
            console.ProviderShell(self.provider),
            ["set m.text 001", "set m.choice 001", "set m.number 7.5"],
        )
        self.assertEqual(self.provider.get_value("m.text"), "001")
        self.assertEqual(self.provider.get_value("m.choice"), "001")
        self.assertEqual(self.provider.get_value("m.number"), Decimal("7.5"))

    def test_consumer_uses_target_metric_type(self) -> None:
        def set_value(handle: str, value: object):
            self.provider.set_value(handle, value)
            return msg_types.InvocationState.FINISHED

        shell = console.ConsumerShell(SimpleNamespace(ip=constants.DEFAULT_IP))
        shell.remote = SimpleNamespace(
            set_value=set_value,
            metrics=lambda: {
                handle: SimpleNamespace(value=self.provider.get_value(handle))
                for handle in self.provider.list_metrics()
            },
            close=lambda: None,
        )
        with patch.object(console.time, "sleep"):
            self.run_commands(shell, ["set m.text 001", "set m.choice 001", "set m.number 7.5"])
        self.assertEqual(self.provider.get_value("m.text"), "001")
        self.assertEqual(self.provider.get_value("m.choice"), "001")
        self.assertEqual(self.provider.get_value("m.number"), Decimal("7.5"))

    def test_consumer_shows_why_the_provider_refused(self) -> None:
        shell = console.ConsumerShell(SimpleNamespace(ip=constants.DEFAULT_IP))
        shell.remote = SimpleNamespace(
            set_value=lambda _handle, _value: msg_types.InvocationState.FAILED,
            last_invocation_detail="Oth: above the maximum 30",
            close=lambda: None,
        )
        output = self.run_commands(shell, ["set m.number 80"])
        self.assertIn("refused by the provider (Fail)", output)
        self.assertIn("above the maximum 30", output)

    def test_unknown_provider_handle_does_not_stop_following_commands(self) -> None:
        output = self.run_commands(
            console.ProviderShell(self.provider),
            ["set m.missing 3", "set m.number 8"],
        )
        self.assertIn("no such handle", output)
        self.assertEqual(self.provider.get_value("m.number"), Decimal(8))

    def test_unusable_instance_name_is_reported_not_raised(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            code = console.run_provider(
                SimpleNamespace(config=None, ip=constants.DEFAULT_IP, name="bad\x01name"),
            )
        self.assertEqual(code, 2)
        self.assertIn("instance name contains characters not allowed in XML 1.0", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
