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

from sdctoolbox import constants, message_log
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
        shell.current = 1
        shell.remotes[1] = SimpleNamespace(
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
        shell.current = 1
        shell.remotes[1] = SimpleNamespace(
            set_value=lambda _handle, _value: msg_types.InvocationState.FAILED,
            last_invocation_detail="Oth: above the maximum 30",
            close=lambda: None,
        )
        output = self.run_commands(shell, ["set m.number 80"])
        self.assertIn("refused by the provider (Fail)", output)
        self.assertIn("above the maximum 30", output)

    def test_provider_actions_faults_check_and_tools(self) -> None:
        provider = ProviderService(instance_name="console-actions")  # actions need a running provider
        provider.start()
        self.addCleanup(provider.stop)
        provider.add_metric(MetricSpec("Number", MetricKind.NUMBER, handle="m.number", minimum=Decimal(0),
                                       maximum=Decimal(30), initial_value=Decimal(7)))
        output = self.run_commands(console.ProviderShell(provider), [
            "action Reset number m.number=3", "actions",
            "set m.number 9",
        ])
        handle = next(iter(provider.list_actions()))
        self.assertIn("Reset number", output)
        output = self.run_commands(console.ProviderShell(provider), [
            f"run {handle}", f"remove {handle}",
            "fault m.number validity=Inv", "fault device answer_delay_ms=200 refuse_writes=on", "faults",
            "fault m.number colour=red", "fault device refuse_writes=maybe",
            "check", "codes pulse rate", "tool list_metrics", "tool no_such_tool", "tool set_value {bad",
        ])
        self.assertEqual(provider.get_value("m.number"), Decimal(3), "running the action set the value")
        self.assertEqual(provider.list_actions(), {})
        self.assertEqual(provider.metric_fault("m.number").validity, "Inv")
        self.assertTrue(provider.faults.refuse_writes)
        self.assertEqual(provider.faults.answer_delay_ms, 200)
        self.assertIn("is not on or off", output)
        self.assertIn("rejected", output)
        self.assertIn("MDC_PULS_RATE", output)
        self.assertIn('"handle": "m.number"', output)
        self.assertIn("no tool 'no_such_tool'", output)
        self.assertIn("not JSON", output)
        self.assertRegex(output, r"\d+ (error|warning|hint)s?|No findings")
        self.run_commands(console.ProviderShell(provider), ["faults reset"])
        self.assertFalse(provider.faults.refuse_writes)

    def test_consumer_keeps_several_connections_and_reads_their_messages(self) -> None:
        def remote(name: str) -> SimpleNamespace:
            log = message_log.MessageLog()
            for number, (direction, action) in enumerate(
                    [(message_log.SENT, "GetMdib"), (message_log.RECEIVED, "GetMdibResponse"),
                     (message_log.RECEIVED, "EpisodicMetricReport")], start=1):
                log.add(message_log.Message(number, 0.0, direction, action, "",
                                            b"<s:Envelope xmlns:s='urn:s'><s:Body/></s:Envelope>"))
            return SimpleNamespace(epr=f"urn:{name}", friendly_name=name, messages=log, close=lambda: None,
                                   run_action=lambda _handle: msg_types.InvocationState.FINISHED,
                                   actions=lambda: {"a.go": SimpleNamespace(label="Go", target_handle="mds0",
                                                                            enabled=True)})

        shell = console.ConsumerShell(SimpleNamespace(ip=constants.DEFAULT_IP))
        shell.remotes, shell.current = {1: remote("first"), 2: remote("second")}, 2
        output = self.run_commands(shell, [
            "connections", "use 1", "messages 5 --kind response", "message 3", "actions", "run a.go",
            "disconnect", "connections", "use 7",
        ])
        self.assertIn("*2  second", output)
        self.assertIn("GetMdibResponse", output)
        self.assertNotIn("EpisodicMetricReport  (", output, "--kind response leaves the report out")
        self.assertIn("<s:Envelope", output)
        self.assertIn("a.go", output)
        self.assertIn("accepted", output)
        self.assertIn("closed connection 1", output)
        self.assertEqual(list(shell.remotes), [2])
        self.assertIn("usage: use <2>", output)

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
