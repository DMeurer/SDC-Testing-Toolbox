"""Focused checks that application and acceptance instance-name defaults stay aligned."""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from acceptance_provider import PEER_INSTANCE
from script_support import Report

import run_toolbox
from examples import console
from sdctoolbox import constants
from sdctoolbox.gui import main_window
from sdctoolbox.gui.startup_dialog import StartupDialog, StartupSettings

REPORT = Report()


def check(condition: bool, message: str) -> None:
    REPORT.require(condition, message)


def parse_failure(argv: list[str]) -> tuple[int | None, str]:
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            run_toolbox.parse_args(argv)
    except SystemExit as exc:
        return exc.code, stderr.getvalue()
    return None, stderr.getvalue()


def check_ip_arguments() -> None:
    check(
        run_toolbox.parse_args(["--ip", " 192.0.2.10 "]).ip == "192.0.2.10",
        "the graphical entry point strips and canonicalizes an IPv4 argument",
    )

    for description, address in (
        ("hostname", "localhost"),
        ("IPv6 address", "::1"),
        ("malformed address", "999.1.2.3"),
        ("whitespace-only value", "   "),
        ("address with a suffix", "192.0.2.10/24"),
    ):
        code, stderr = parse_failure(["--ip", address])
        check(
            code == 2 and "argument --ip: must be a valid IPv4 address" in stderr,
            f"the graphical entry point rejects {description} with an --ip error",
        )


def check_tls_arguments() -> None:
    code, stderr = parse_failure(["--tls-peer-fingerprint", "AA" * 32])
    check(
        code is None and not stderr,
        "TLS peer arguments parse before their required TLS files are checked",
    )
    try:
        run_toolbox.settings_from_args(run_toolbox.parse_args(["--tls-peer-fingerprint", "AA" * 32]))
    except Exception as exc:  # noqa: BLE001 - the command boundary must report any validation problem
        error = str(exc)
    else:
        error = ""
    check(
        error == "TLS peer options require --tls-cert, --tls-key, and --tls-ca",
        "TLS peer policy requires a complete local mTLS identity",
    )


def check_invalid_ip_precedes_construction() -> None:
    constructions: list[str] = []

    def record_application(*_args: object, **_kwargs: object) -> None:
        constructions.append("QApplication")

    def record_provider(*_args: object, **_kwargs: object) -> None:
        constructions.append("ProviderService")

    with patch.object(run_toolbox, "QApplication", record_application), patch.object(
        run_toolbox,
        "ProviderService",
        record_provider,
    ):
        code, stderr = None, io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                run_toolbox.main(["--ip", "not-an-address"])
        except SystemExit as exc:
            code = exc.code

    check(
        code == 2
        and "argument --ip: must be a valid IPv4 address" in stderr.getvalue()
        and not constructions,
        "invalid --ip exits before QApplication or ProviderService construction",
    )


def check_gui_startup_reuses_profile(*, interactive: bool) -> None:
    events: list[object] = []
    load_calls: list[Path] = []
    provider_arguments: list[dict[str, object]] = []
    applications: list[tuple[object, str]] = []
    device = object()
    device_config = SimpleNamespace(device=device)

    class FakeApplication:
        def __init__(self, _argv: list[str]) -> None:
            events.append("application")

        def exec(self) -> int:
            events.append("exec")
            return 0

    class FakeProvider:
        def __init__(self, **kwargs: object) -> None:
            provider_arguments.append(kwargs)
            events.append("provider")

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    class FakeWindow:
        def __init__(self, _service: FakeProvider) -> None:
            events.append("window")

        def apply_config(self, parsed: object, source: str) -> bool:
            applications.append((parsed, source))
            events.append("apply")
            return True

        def show(self) -> None:
            events.append("show")

    originals = (
        run_toolbox.QApplication,
        run_toolbox.ProviderService,
        run_toolbox.MainWindow,
        run_toolbox.config.load_file,
        run_toolbox.ask_how_to_start,
        run_toolbox.basic_logging_setup,
    )
    with TemporaryDirectory(prefix="sdc-startup-profile-") as raw_temporary:
        temporary = Path(raw_temporary)
        source = temporary / "profile.json"
        source.write_text("parsed by the test double", encoding="utf-8")

        def load_once(path: str) -> object:
            load_calls.append(Path(path))
            source.unlink()
            events.append("load")
            return device_config

        def choose_settings(_args: object) -> StartupSettings:
            parsed = run_toolbox.config.load_file(source)
            return StartupSettings(
                name="dialog-authority",
                ip="127.0.0.1",
                config_path=str(source),
                verbose=False,
                device_config=parsed,
            )

        run_toolbox.QApplication = FakeApplication
        run_toolbox.ProviderService = FakeProvider
        run_toolbox.MainWindow = FakeWindow
        run_toolbox.config.load_file = load_once
        run_toolbox.ask_how_to_start = choose_settings
        run_toolbox.basic_logging_setup = lambda **_kwargs: None
        try:
            argv = (
                []
                if interactive
                else ["--name", "cli-authority", "--config", str(source)]
            )
            result = run_toolbox.main(argv)
        finally:
            (
                run_toolbox.QApplication,
                run_toolbox.ProviderService,
                run_toolbox.MainWindow,
                run_toolbox.config.load_file,
                run_toolbox.ask_how_to_start,
                run_toolbox.basic_logging_setup,
            ) = originals

    authority = "dialog-authority" if interactive else "cli-authority"
    mode = "dialog" if interactive else "command-line"
    check(
        result == 0
        and load_calls == [source]
        and not source.exists()
        and len(applications) == 1
        and applications[0][0] is device_config
        and applications[0][1] == str(source),
        f"{mode} GUI startup parses once and applies its snapshot after deletion",
    )
    check(
        provider_arguments
        == [{"ip": "127.0.0.1", "instance_name": authority, "device": device, "tls_config": None}],
        f"{mode} GUI startup uses snapshot metadata without overriding its instance name",
    )
    check(
        events.index("provider")
        < events.index("start")
        < events.index("apply")
        < events.index("stop"),
        f"{mode} GUI startup applies after start and still stops the provider",
    )


def check_startup_dialog_retains_profile() -> None:
    load_calls: list[Path] = []
    device_config = SimpleNamespace()
    accepted: list[bool] = []
    original_load = run_toolbox.config.load_file

    with TemporaryDirectory(prefix="sdc-dialog-profile-") as raw_temporary:
        temporary = Path(raw_temporary)
        source = temporary / "profile.json"
        source.write_text("parsed by the test double", encoding="utf-8")

        def load_once(path: str) -> object:
            load_calls.append(Path(path))
            return device_config

        dialog = SimpleNamespace(
            error_label=SimpleNamespace(hide=lambda: None),
            name_edit=SimpleNamespace(text=lambda: "dialog-authority"),
            chosen_ip=lambda: "127.0.0.1",
            chosen_config=lambda: str(source),
            verbose_box=SimpleNamespace(isChecked=lambda: True),
            accept=lambda: accepted.append(True),
        )
        run_toolbox.config.load_file = load_once
        try:
            StartupDialog._on_accept(dialog)
        finally:
            run_toolbox.config.load_file = original_load

    settings = dialog._settings
    check(
        load_calls == [source]
        and accepted == [True]
        and settings.device_config is device_config
        and settings.config_path == str(source),
        "startup dialog retains the exact profile object it validates",
    )


def check_console_startup_reuses_profile() -> None:
    events: list[str] = []
    load_calls: list[Path] = []
    provider_arguments: list[dict[str, object]] = []
    applied: list[tuple[object, object, bool]] = []
    device = object()
    device_config = SimpleNamespace(device=device)

    class FakeProvider:
        def __init__(self, **kwargs: object) -> None:
            provider_arguments.append(kwargs)
            events.append("provider")

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    class FakeShell:
        def __init__(self, _service: FakeProvider) -> None:
            events.append("shell")

        def cmdloop(self) -> None:
            events.append("cmdloop")

    originals = (
        console.ProviderService,
        console.ProviderShell,
        console.config.load_file,
        console.config.apply_to,
    )
    with TemporaryDirectory(prefix="sdc-console-profile-") as raw_temporary:
        temporary = Path(raw_temporary)
        source = temporary / "profile.json"
        source.write_text("parsed by the test double", encoding="utf-8")

        def load_once(path: str) -> object:
            load_calls.append(Path(path))
            source.unlink()
            events.append("load")
            return device_config

        def apply_once(
            service: object,
            parsed: object,
            *,
            replace: bool,
        ) -> tuple[int, int]:
            applied.append((service, parsed, replace))
            events.append("apply")
            return 3, 2

        console.ProviderService = FakeProvider
        console.ProviderShell = FakeShell
        console.config.load_file = load_once
        console.config.apply_to = apply_once
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                result = console.run_provider(
                    SimpleNamespace(
                        config=str(source),
                        ip="127.0.0.1",
                        name="console-authority",
                    ),
                )
        finally:
            (
                console.ProviderService,
                console.ProviderShell,
                console.config.load_file,
                console.config.apply_to,
            ) = originals

    check(
        result == 0
        and load_calls == [source]
        and not source.exists()
        and len(applied) == 1
        and applied[0][1] is device_config
        and applied[0][2] is True,
        "console startup parses once and applies the same replacement snapshot",
    )
    check(
        provider_arguments
        == [{"ip": "127.0.0.1", "instance_name": "console-authority", "device": device}],
        "console startup uses snapshot metadata without overriding its instance name",
    )
    check(
        events == ["load", "provider", "start", "apply", "shell", "cmdloop", "stop"]
        and "loaded 3 data source(s) and 2 alarm(s)" in output.getvalue()
        and output.getvalue().rstrip().endswith("provider stopped"),
        "console startup retains status output, ordering, and cleanup",
    )


def check_console_apply_failure_stops_provider() -> None:
    events: list[str] = []
    device_config = SimpleNamespace(device=object())

    class FakeProvider:
        def __init__(self, **_kwargs: object) -> None:
            events.append("provider")

        def start(self) -> None:
            events.append("start")

        def stop(self) -> None:
            events.append("stop")

    originals = (
        console.ProviderService,
        console.config.load_file,
        console.config.apply_to,
    )
    console.ProviderService = FakeProvider
    console.config.load_file = lambda _path: device_config

    def fail_apply(
        _service: object,
        parsed: object,
        *,
        replace: bool,
    ) -> tuple[int, int]:
        check(
            parsed is device_config and replace,
            "console failure applies the parsed replacement snapshot",
        )
        events.append("apply")
        raise console.config.ConfigError("application failed")

    console.config.apply_to = fail_apply
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = console.run_provider(
                SimpleNamespace(
                    config="profile.json",
                    ip="127.0.0.1",
                    name="failure-case",
                ),
            )
    finally:
        (
            console.ProviderService,
            console.config.load_file,
            console.config.apply_to,
        ) = originals

    check(
        result == 2
        and events == ["provider", "start", "apply", "stop"]
        and "error: application failed" in stderr.getvalue()
        and stdout.getvalue().rstrip().endswith("provider stopped"),
        "console application failure returns status 2 and stops the provider",
    )


def check_main_window_applies_parsed_profile() -> None:
    calls: list[object] = []
    warnings: list[tuple[str, str]] = []
    statuses: list[tuple[str, int]] = []
    device_config = SimpleNamespace()

    class FakePane:
        def refresh(self) -> None:
            calls.append("metrics")

        def refresh_alerts(self) -> None:
            calls.append("alerts")

        def refresh_actions(self) -> None:
            calls.append("actions")

        def refresh_contexts(self) -> None:
            calls.append("contexts")

    window = SimpleNamespace(
        service=object(),
        provider_pane=FakePane(),
        statusBar=lambda: SimpleNamespace(
            showMessage=lambda message, timeout: statuses.append((message, timeout)),
        ),
    )
    original_apply = main_window.config.apply_to
    original_message_box = main_window.QMessageBox

    def apply_success(
        service: object,
        parsed: object,
        *,
        replace: bool,
    ) -> tuple[int, int]:
        calls.append((service, parsed, replace))
        return 4, 1

    main_window.config.apply_to = apply_success
    main_window.QMessageBox = SimpleNamespace(
        warning=lambda _parent, title, text: warnings.append((title, text)),
    )
    try:
        result = main_window.MainWindow.apply_config(
            window,
            device_config,
            "deleted-profile.json",
        )
    finally:
        main_window.config.apply_to = original_apply
        main_window.QMessageBox = original_message_box

    check(
        result
        and calls
        and calls[0][0] is window.service
        and calls[0][1] is device_config
        and calls[0][2] is True
        and calls[1:] == ["metrics", "alerts", "actions", "contexts"],
        "GUI applies the parsed replacement and refreshes every provider view",
    )
    check(
        warnings == []
        and statuses
        == [
            (
                "Imported 4 data source(s) and 1 alarm(s) from deleted-profile.json",
                8000,
            ),
        ],
        "successful parsed GUI application retains its import status",
    )

    calls.clear()
    statuses.clear()

    def apply_failure(
        _service: object,
        parsed: object,
        *,
        replace: bool,
    ) -> tuple[int, int]:
        check(
            parsed is device_config and replace,
            "GUI failure applies the parsed replacement snapshot",
        )
        raise main_window.config.ConfigError("application failed")

    main_window.config.apply_to = apply_failure
    main_window.QMessageBox = SimpleNamespace(
        warning=lambda _parent, title, text: warnings.append((title, text)),
    )
    try:
        result = main_window.MainWindow.apply_config(
            window,
            device_config,
            "deleted-profile.json",
        )
    finally:
        main_window.config.apply_to = original_apply
        main_window.QMessageBox = original_message_box

    check(
        not result
        and calls == []
        and statuses == []
        and warnings == [("Could not import", "application failed")],
        "failed parsed GUI application warns without refreshing or reporting success",
    )


def main() -> int:
    print("Application default tests")
    check(
        run_toolbox.parse_args([]).name == constants.DEFAULT_INSTANCE_NAME,
        "the graphical entry point uses the shared instance name",
    )
    check(
        console.parse_args(["provider"]).name == constants.DEFAULT_INSTANCE_NAME,
        "the console provider uses the shared instance name",
    )
    check(
        console.parse_args(["provider", "--name", "named-peer"]).name == "named-peer",
        "the console provider accepts an explicit instance name",
    )
    check(
        PEER_INSTANCE != constants.DEFAULT_INSTANCE_NAME,
        "the acceptance peer remains distinct from applications using defaults",
    )
    check_ip_arguments()
    check_tls_arguments()
    check_invalid_ip_precedes_construction()
    check_startup_dialog_retains_profile()
    check_gui_startup_reuses_profile(interactive=False)
    check_gui_startup_reuses_profile(interactive=True)
    check_console_startup_reuses_profile()
    check_console_apply_failure_stops_provider()
    check_main_window_applies_parsed_profile()
    return REPORT.summary()


if __name__ == "__main__":
    raise SystemExit(main())
