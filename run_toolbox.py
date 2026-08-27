"""Entry point for the graphical toolbox.

Run it with no arguments and it asks how to start. Give it any argument and it does not,
so scripts and shortcuts keep working unattended:

    .venv/Scripts/python.exe run_toolbox.py
    .venv/Scripts/python.exe run_toolbox.py --name alpha
    .venv/Scripts/python.exe run_toolbox.py --name beta --config presets/insufflator.json

Start it twice under different names to watch two devices find each other. The name decides
the EPR, so restarting under the same name keeps that device's identity on the network.
"""

from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from sdc11073.loghelper import basic_logging_setup

from sdctoolbox import config, constants
from sdctoolbox.gui.main_window import MainWindow
from sdctoolbox.gui.startup_dialog import StartupDialog, StartupSettings
from sdctoolbox.provider_service import ProviderService


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", default=constants.DEFAULT_IP, help="interface to bind discovery to")
    parser.add_argument("--name", default=constants.DEFAULT_INSTANCE_NAME, help="instance name, decides the EPR")
    parser.add_argument("--config", help="config file to load on startup")
    parser.add_argument("--verbose", action="store_true", help="show sdc11073 logging")
    return parser.parse_args(argv)


def settings_from_args(args: argparse.Namespace) -> StartupSettings:
    """Turn parsed arguments into the same shape the dialog produces."""
    return StartupSettings(name=args.name, ip=args.ip, config_path=args.config, verbose=args.verbose)


def ask_how_to_start(args: argparse.Namespace) -> StartupSettings | None:
    """Show the startup dialog. None means the user cancelled.

    Needs a QApplication to exist already, which is why the caller creates it first.
    """
    dialog = StartupDialog(name=args.name, ip=args.ip)
    if dialog.exec() != QDialog.Accepted:
        return None
    return dialog.settings()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)

    # Nothing on the command line means nobody has said what they want yet, so ask. Any
    # argument at all means this was started deliberately and must not stop for a dialog.
    interactive = not argv

    app = QApplication(sys.argv)

    if interactive:
        settings = ask_how_to_start(args)
        if settings is None:
            return 0
    else:
        settings = settings_from_args(args)
        # The dialog validates as you go; from the command line the first chance is here,
        # and failing before anything starts beats failing behind a window.
        if settings.config_path:
            try:
                config.load_file(settings.config_path)
            except config.ConfigError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2

    basic_logging_setup(level=logging.INFO if settings.verbose else logging.WARNING)

    # Read the config before the provider exists, not after. A preset can say which machine
    # it describes, and sdc11073 fixes ThisModel and ThisDevice when the provider is built -
    # so a device loaded afterwards would still announce itself as the toolbox.
    device = None
    if settings.config_path:
        try:
            device = config.load_file(settings.config_path).device
        except config.ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    service = ProviderService(ip=settings.ip, instance_name=settings.name, device=device)
    service.start()
    try:
        window = MainWindow(service)
        if settings.config_path:
            window.load_config(settings.config_path)
        window.show()
        return app.exec()
    except Exception as exc:  # noqa: BLE001 - a crash here should still say why
        QMessageBox.critical(None, "SDC testing toolbox", str(exc))
        raise
    finally:
        service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
