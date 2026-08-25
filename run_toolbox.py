"""Entry point for the graphical toolbox.

Start it twice to watch two devices find each other:

    .venv/Scripts/python.exe run_toolbox.py --name alpha
    .venv/Scripts/python.exe run_toolbox.py --name beta

The name decides the EPR, so restarting an instance under the same name keeps its identity
on the network.

Pass --config to come up with a device already configured:

    .venv/Scripts/python.exe run_toolbox.py --config presets/pump.json

The same file can be loaded later from File > Import config.
"""

from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from sdc11073.loghelper import basic_logging_setup

from sdctoolbox import config, constants
from sdctoolbox.gui.main_window import MainWindow
from sdctoolbox.provider_service import ProviderService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", default=constants.DEFAULT_IP, help="interface to bind discovery to")
    parser.add_argument("--name", default="alpha", help="instance name, decides the EPR")
    parser.add_argument("--config", help="config file to load on startup")
    parser.add_argument("--verbose", action="store_true", help="show sdc11073 logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    basic_logging_setup(level=logging.INFO if args.verbose else logging.WARNING)

    # Check the file before starting anything, so a typo fails immediately and visibly
    # rather than after a window has already appeared.
    if args.config:
        try:
            config.load_file(args.config)
        except config.ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    app = QApplication(sys.argv)
    service = ProviderService(ip=args.ip, instance_name=args.name)
    service.start()
    try:
        window = MainWindow(service)
        if args.config:
            window.load_config(args.config)
        window.show()
        return app.exec()
    except Exception as exc:  # noqa: BLE001 - a crash here should still say why
        QMessageBox.critical(None, "SDC testing toolbox", str(exc))
        raise
    finally:
        service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
