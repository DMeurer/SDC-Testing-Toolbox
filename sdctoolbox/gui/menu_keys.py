"""Recognize bare Alt even when focused children consume the other keys."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QApplication, QWidget


class BareAltTracker(QObject):
    """Observe a window's complete Alt gesture without intercepting normal shortcuts."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.is_bare = False
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.parent() and event.type() == QEvent.WindowDeactivate:
            self.is_bare = False
        elif (event.type() == QEvent.ShortcutOverride and isinstance(watched, QWidget)
              and watched.window() is self.parent()):
            # ShortcutOverride arrives before editors or QAction consume a chord's key.
            self.is_bare = (event.key() == Qt.Key_Alt and not event.isAutoRepeat()
                            and not (event.modifiers() & ~Qt.AltModifier))
        return False
