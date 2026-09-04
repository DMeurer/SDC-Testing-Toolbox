"""Small fixtures shared by the focused offscreen GUI checks."""

from __future__ import annotations

import os
import sys
import time
from typing import Self

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from script_support import Report as Report
from sdctoolbox.gui.main_window import MainWindow
from sdctoolbox.provider_service import ProviderService


def application() -> QApplication:
    return QApplication.instance() or QApplication(sys.argv)


def pump(app: QApplication, seconds: float = 0.2) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def wait_for(app: QApplication, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class WindowFixture:
    """Own a real provider window and always close it through MainWindow.close()."""

    def __init__(self, instance_name: str) -> None:
        self.app = application()
        self.service = ProviderService(instance_name=instance_name)
        self.window: MainWindow | None = None

    def __enter__(self) -> Self:
        self.service.start()
        try:
            self.window = MainWindow(self.service)
            self.window.show()
            pump(self.app)
            return self
        except BaseException:
            self.service.stop()
            raise

    def __exit__(self, *_exc_info: object) -> None:
        if self.window is not None:
            self.window.close()
            pump(self.app, 0.05)
            self.window.deleteLater()
        self.service.stop()
