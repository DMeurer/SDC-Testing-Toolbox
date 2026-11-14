"""Entry point for the graphical toolbox.

Run it with no arguments and it asks how to start. Give it any argument and it does not,
so scripts and shortcuts keep working unattended:

    .venv/Scripts/python.exe run_toolbox.py
    .venv/Scripts/python.exe run_toolbox.py --name alpha
    .venv/Scripts/python.exe run_toolbox.py --name beta --config presets/insufflator.xml

Use --theme {light,dark} and --fullscreen; F11 toggles fullscreen later.
SSH launches need access to a desktop display/session or graphical forwarding.

Start it twice under different names to watch two devices find each other. The name decides
the EPR, so restarting under the same name keeps that device's identity on the network.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from sdc11073.loghelper import basic_logging_setup

from sdctoolbox import config, constants, native_mdib
from sdctoolbox.gui.main_window import MainWindow
from sdctoolbox.gui.startup_dialog import StartupDialog, StartupSettings
from sdctoolbox.gui.icons import app_icon
from sdctoolbox.gui.styling import apply_theme_style, set_dark_mode
from sdctoolbox.model import validate_xml_text
from sdctoolbox.network import normalize_ipv4
from sdctoolbox.provider_service import ProviderService
from sdctoolbox.mdib_io import NativeMdibConfig, parse_mdib
from sdctoolbox.security import TlsConfig, TlsConfigError


def ipv4_argument(value: str) -> str:
    """Normalize an argparse IPv4 value and provide a field-specific error."""
    try:
        return normalize_ipv4(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid IPv4 address") from exc


def instance_name_argument(value: str) -> str:
    """Refuse a name XML cannot carry here, where the alternative is a traceback at startup."""
    try:
        validate_xml_text(value, "instance name")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def friendly_name_argument(value: str) -> str:
    """Refuse a friendly name XML cannot carry, like the instance name."""
    try:
        validate_xml_text(value, "friendly name")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--ip",
        default=constants.DEFAULT_IP,
        type=ipv4_argument,
        help="interface to bind discovery to",
    )
    parser.add_argument(
        "--name",
        default=constants.DEFAULT_INSTANCE_NAME,
        type=instance_name_argument,
        help="instance name, decides the EPR",
    )
    parser.add_argument(
        "--friendly-name",
        type=friendly_name_argument,
        help="name the device announces; overrides the config file (default: the preset's, or --name for an MDIB)",
    )
    parser.add_argument("--config", help="config file to load on startup")
    parser.add_argument("--theme", choices=("light", "dark"), help="initial GUI colour theme")
    parser.add_argument("--fullscreen", action="store_true", help="start fullscreen; F11 restores the window")
    parser.add_argument("--verbose", action="store_true", help="show sdc11073 logging")
    parser.add_argument("--tls-cert", help="local PEM certificate or certificate chain")
    parser.add_argument("--tls-key", help="local PEM private key")
    parser.add_argument("--tls-ca", help="trusted PEM CA certificate bundle")
    parser.add_argument("--tls-peer-fingerprint", help="optional SHA-256 pin for connected peers")
    parser.add_argument("--tls-server-name", help="optional local DNS name to advertise for the provider and event callbacks")
    parser.add_argument("--tls-trusted-dir", help="folder of trusted PEM certificates, self-signed ones included")
    parser.add_argument(
        "--tls-allow-self-signed",
        action="store_true",
        help="accept valid self-signed peer certificates (lab only)",
    )
    parser.add_argument(
        "--tls-no-hostname-check",
        action="store_true",
        help="do not require the peer certificate to name the advertised host or IP",
    )
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def settings_from_args(args: argparse.Namespace) -> StartupSettings:
    """Turn parsed arguments into the same shape the dialog produces."""
    tls_values = (args.tls_cert, args.tls_key, args.tls_ca, args.tls_trusted_dir)
    tls_options = (
        args.tls_peer_fingerprint,
        args.tls_server_name,
        args.tls_allow_self_signed,
        args.tls_no_hostname_check,
    )
    if any(tls_values):
        tls_config = TlsConfig.from_paths(
            args.tls_cert,
            args.tls_key,
            args.tls_ca,
            private_key_password=os.getenv("SDC_TOOLBOX_TLS_KEY_PASSWORD"),
            peer_fingerprint=args.tls_peer_fingerprint,
            server_name=args.tls_server_name,
            trusted_folder=args.tls_trusted_dir,
            allow_self_signed=args.tls_allow_self_signed,
            verify_hostname=not args.tls_no_hostname_check,
        )
    elif any(tls_options):
        raise TlsConfigError("TLS peer options require --tls-cert and --tls-key")
    else:
        tls_config = None
    return StartupSettings(
        name=args.name,
        ip=args.ip,
        config_path=args.config,
        friendly_name=args.friendly_name or None,
        verbose=args.verbose,
        tls_config=tls_config,
    )


def make_service(
    settings: StartupSettings,
    device_config: config.DeviceConfig | NativeMdibConfig | None,
) -> ProviderService:
    """Build the provider with the identity the settings and the config file give it.

    Who made an MDIB preset comes from its own MDS MetaData, and its friendly name, firmware
    and URL from its stored toolbox behaviour. Without a stored friendly name it is announced
    exactly as named on the command line: no prefix is added.
    """
    serial_number = None
    friendly_name = settings.friendly_name
    if isinstance(device_config, NativeMdibConfig):
        device, serial_number = native_mdib.device_identity(parse_mdib(device_config.xml))
        friendly_name = friendly_name or device.friendly_name or settings.name
    else:
        device = device_config.device if device_config is not None else None
    return ProviderService(
        ip=settings.ip,
        instance_name=settings.name,
        friendly_name=friendly_name,
        device=device,
        tls_config=settings.tls_config,
        serial_number=serial_number,
    )


def ask_how_to_start(args: argparse.Namespace) -> StartupSettings | None:
    """Show the startup dialog. None means the user cancelled.

    Needs a QApplication to exist already, which is why the caller creates it first.
    """
    dialog = StartupDialog(name=args.name, ip=args.ip)
    if dialog.exec() != QDialog.Accepted:
        return None
    return dialog.settings()


def configure_application_identity() -> None:
    """Name the application the way the installed Linux desktop entry expects."""
    QCoreApplication.setApplicationName(constants.APP_NAME)
    QGuiApplication.setDesktopFileName(constants.DESKTOP_ID)


@contextlib.contextmanager
def closing_on_ctrl_c() -> Iterator[None]:
    """Make Ctrl+C in the console close every window, as the close button would.

    Python's default handler raises KeyboardInterrupt in whatever Python code Qt calls next,
    and PySide reports and swallows exceptions from such callbacks, so the toolbox never quit.
    Python also runs handlers only between bytecodes, so an idle Qt loop needs a timer to wake it.
    """

    def close_windows(_signum: int, _frame: object) -> None:
        # A second Ctrl+C interrupts a shutdown that hangs instead of closing windows again.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        app = QCoreApplication.instance()
        if app is not None:
            app.closeAllWindows()

    previous = signal.signal(signal.SIGINT, close_windows)
    wake = QTimer()
    if QCoreApplication.instance() is not None:
        wake.timeout.connect(lambda: None)
        wake.start(200)
    try:
        yield
    finally:
        wake.stop()
        signal.signal(signal.SIGINT, previous)


def create_application(theme: Literal["light", "dark"] | None) -> QApplication:
    """Apply an explicit theme before any dialog or main window is constructed."""
    app = QApplication(sys.argv)
    QGuiApplication.setWindowIcon(app_icon())
    if theme is not None:
        set_dark_mode(theme == "dark")
    else:
        apply_theme_style()
    return app


def main(argv: list[str] | None = None) -> int:
    configure_application_identity()
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)

    # Nothing on the command line means nobody has said what they want yet, so ask. Any
    # argument at all means this was started deliberately and must not stop for a dialog.
    interactive = not argv

    if interactive:
        app = create_application(args.theme)
        with closing_on_ctrl_c():
            settings = ask_how_to_start(args)
        if settings is None:
            return 0
    else:
        try:
            settings = settings_from_args(args)
            if settings.tls_config is not None:
                settings.tls_config.create_contexts()
        except TlsConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        app = create_application(args.theme)

    basic_logging_setup(level=logging.INFO if settings.verbose else logging.WARNING)

    # Read the config before the provider exists, not after. A preset can say which machine
    # it describes, and sdc11073 fixes ThisModel and ThisDevice when the provider is built -
    # so a device loaded afterwards would still announce itself as the toolbox.
    device_config = settings.device_config
    if settings.config_path and device_config is None:
        try:
            device_config = config.load_file(settings.config_path)
        except config.ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    native_config = isinstance(device_config, NativeMdibConfig)
    service = make_service(settings, device_config)
    window = None
    try:
        if native_config:
            service.start(parse_mdib(device_config.xml))
        else:
            service.start()
        window = MainWindow(service)
        if settings.config_path and device_config is not None and not native_config:
            if not window.apply_config(device_config, settings.config_path):
                return 2
        if args.fullscreen:
            window.fullscreen_action.trigger()
        else:
            window.show()
        if args.smoke_test:
            bundled_files = {path.name for path in constants.PRESET_DIR.iterdir() if path.suffix.lower() in (".json", ".xml")}
            if bundled_files != constants.SHIPPED_PRESET_FILES:
                msg = f"packaged presets differ: found {sorted(bundled_files)}"
                raise RuntimeError(msg)
            presets = config.list_presets()
            usable_files = {preset.path.name for preset in presets}
            if usable_files != constants.SHIPPED_PRESET_FILES:
                msg = f"packaged presets are invalid: loaded {sorted(usable_files)}"
                raise RuntimeError(msg)
            startup_path = (
                Path(settings.config_path).resolve() if settings.config_path else None
            )
            for preset in presets:
                # The startup profile was already validated and applied from its snapshot.
                if preset.path.resolve() != startup_path:
                    config.load_file(preset.path)
            QTimer.singleShot(1000, window.close)
        with closing_on_ctrl_c():
            return app.exec()
    except Exception as exc:  # noqa: BLE001 - a crash here should still say why
        if args.smoke_test:
            detail = f"smoke test failed: {exc}"
            Path("smoke-test-error.txt").write_text(detail, encoding="utf-8")
            if sys.stderr is not None:
                print(detail, file=sys.stderr)
            return 1
        QMessageBox.critical(None, "SDC testing toolbox", str(exc))
        raise
    finally:
        try:
            if window is not None:
                window.close()
        finally:
            service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
