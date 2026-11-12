"""Common main-window actions."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMainWindow


class FullscreenAction(QAction):
    """Toggle fullscreen without losing the preceding normal/maximized state."""

    def __init__(self, window: QMainWindow) -> None:
        super().__init__("&Fullscreen", window)
        self._window = window
        self._windowed_state = window.windowState()
        self.setCheckable(True)
        self.setChecked(window.isFullScreen())
        self.setShortcut("F11")
        self.setAutoRepeat(False)
        self.triggered.connect(self._toggle)
        window.addAction(self)
        window.installEventFilter(self)

    def _toggle(self) -> None:
        if self._window.isFullScreen():
            self._window.setWindowState(self._windowed_state)
            self._window.show()
        else:
            self._windowed_state = self._window.windowState()
            self._window.showFullScreen()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if watched is self._window and event.type() == QEvent.WindowStateChange:
            self.setChecked(self._window.isFullScreen())
        return super().eventFilter(watched, event)
