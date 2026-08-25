"""The application window: one tab per role.

The tool is meant to be started twice on one machine, so that one instance publishes a
device and the other discovers it. Each instance is therefore both a provider and a
consumer, and each role gets a tab.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from .provider_pane import ProviderPane

if TYPE_CHECKING:
    from ..provider_service import ProviderService


class PlaceholderPane(QWidget):
    """Stands in for a tab that is not implemented yet."""

    def __init__(self, message: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setStyleSheet("color: palette(mid);")
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(label)
        layout.addStretch(1)


class MainWindow(QMainWindow):
    """Hosts the provider and consumer tabs."""

    def __init__(self, service: ProviderService) -> None:
        super().__init__()
        self.service = service
        self.setWindowTitle(f"SDC testing toolbox \u2014 {service.friendly_name}")
        self.resize(900, 520)

        self.provider_pane = ProviderPane(service, self)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.provider_pane, "My device")
        self.tabs.addTab(
            PlaceholderPane(
                "Discovery and browsing other devices lands here next.\n\n"
                "Until then, use the console consumer:\n"
                "    examples/console.py consumer",
            ),
            "Network",
        )
        self.setCentralWidget(self.tabs)

        self.statusBar().showMessage(f"Publishing on {service.ip}   \u00b7   {service.epr.urn}")
