"""The application icon, pre-rendered from app_icon.svg at the sizes the platforms ask for."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon

ICON_DIR = Path(__file__).resolve().parent
ICON_SIZES = (16, 32, 48, 256)


def app_icon() -> QIcon:
    """The toolbox icon for title bars and the taskbar."""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addFile(str(ICON_DIR / f"app_icon_{size}.png"))
    return icon
