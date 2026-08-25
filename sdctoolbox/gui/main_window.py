"""The application window: both roles, either side by side or as tabs.

The tool is meant to be started twice on one machine, so that one instance publishes a
device and the other discovers it. Each instance is therefore both a provider and a
consumer.

By default the two sit in panels either side of a movable divider, so you can watch one
react to the other. View > Split view turns that off and stacks them as tabs instead, which
is easier on a narrow screen.

The menu bar stays hidden until Alt is pressed, the way Thunderbird and Firefox do it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .consumer_pane import ConsumerPane
from .provider_pane import ProviderPane

if TYPE_CHECKING:
    from ..provider_service import ProviderService

PROVIDER_TITLE = "My device"
NETWORK_TITLE = "Network"

#: Where the divider sits when the window opens, as a share of the width.
PROVIDER_SHARE = 3
NETWORK_SHARE = 2


class TitledPanel(QWidget):
    """A panel with a heading.

    The heading identifies the panel when the two sit side by side. In tab mode the tab
    already says the same thing, so it is hidden to avoid repeating it.
    """

    def __init__(self, title: str, content: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title

        self.heading = QLabel(title)
        font = self.heading.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 1)
        self.heading.setFont(font)

        self.rule = QFrame()
        self.rule.setFrameShape(QFrame.HLine)
        self.rule.setFrameShadow(QFrame.Sunken)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.heading)
        layout.addWidget(self.rule)
        layout.addWidget(content, 1)
        self.content = content

    def set_heading_visible(self, visible: bool) -> None:  # noqa: FBT001 - a plain flag reads fine here
        """Show or hide the heading and its rule."""
        self.heading.setVisible(visible)
        self.rule.setVisible(visible)


class MainWindow(QMainWindow):
    """Hosts the provider and consumer panels in whichever layout is selected."""

    def __init__(self, service: ProviderService) -> None:
        super().__init__()
        self.service = service
        self.setWindowTitle(f"SDC testing toolbox \u2014 {service.friendly_name}")
        self.resize(1100, 560)

        self.provider_pane = ProviderPane(service, self)
        self.network_pane = ConsumerPane(service.ip, self)

        self.provider_panel = TitledPanel(PROVIDER_TITLE, self.provider_pane)
        self.network_panel = TitledPanel(NETWORK_TITLE, self.network_pane)

        # Both layouts exist all the time; the panels move between them on demand.
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.tabs = QTabWidget()

        self.stack = QStackedWidget()
        self.stack.addWidget(self.splitter)
        self.stack.addWidget(self.tabs)
        self.setCentralWidget(self.stack)

        #: Remembered across a trip through tab mode so the divider comes back where it was.
        self._splitter_sizes: list[int] = []

        self._build_menus()
        self.set_split_view(enabled=True)

        self.statusBar().showMessage(
            f"Publishing on {service.ip}   \u00b7   {service.epr.urn}   \u00b7   press Alt for the menu",
        )

    # -- layout --------------------------------------------------------------------

    def set_split_view(self, enabled: bool) -> None:  # noqa: FBT001 - matches the Qt signal
        """Choose between panels side by side and stacked tabs."""
        if enabled:
            self._detach_panels()
            for panel in (self.provider_panel, self.network_panel):
                panel.set_heading_visible(True)
                self.splitter.addWidget(panel)
                panel.setVisible(True)
            self.splitter.setStretchFactor(0, PROVIDER_SHARE)
            self.splitter.setStretchFactor(1, NETWORK_SHARE)
            if self._splitter_sizes:
                self.splitter.setSizes(self._splitter_sizes)
            self.stack.setCurrentWidget(self.splitter)
        else:
            if self.splitter.count():
                self._splitter_sizes = self.splitter.sizes()
            self._detach_panels()
            for panel in (self.provider_panel, self.network_panel):
                panel.set_heading_visible(False)
                self.tabs.addTab(panel, panel.title)
                panel.setVisible(True)
            self.stack.setCurrentWidget(self.tabs)

        if self.split_view_action.isChecked() != enabled:
            self.split_view_action.setChecked(enabled)

    def _detach_panels(self) -> None:
        """Take the panels out of whichever container currently owns them.

        Neither removeTab nor setParent(None) destroys a widget, so the same two panel
        instances survive any number of trips between the layouts.
        """
        while self.tabs.count():
            self.tabs.removeTab(0)
        for panel in (self.provider_panel, self.network_panel):
            panel.setParent(None)

    @property
    def split_view_enabled(self) -> bool:
        """Whether the panels are currently side by side."""
        return self.stack.currentWidget() is self.splitter

    # -- menus ---------------------------------------------------------------------

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        self.exit_action = QAction("E&xit", self)
        # QKeySequence.Quit resolves to nothing on Windows, so name the shortcut outright.
        self.exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

        view_menu = menu_bar.addMenu("&View")
        self.split_view_action = QAction("&Split view", self)
        self.split_view_action.setCheckable(True)
        self.split_view_action.setChecked(True)
        self.split_view_action.setShortcut("F8")
        self.split_view_action.setStatusTip("Panels side by side, or stacked as tabs")
        self.split_view_action.toggled.connect(self.set_split_view)
        view_menu.addAction(self.split_view_action)

        # Hidden until Alt is pressed. The actions keep working through their shortcuts.
        for menu in (file_menu, view_menu):
            menu.aboutToHide.connect(self._maybe_hide_menu_bar)
        menu_bar.setVisible(False)

    # -- the Alt-revealed menu bar -------------------------------------------------

    def toggle_menu_bar(self) -> None:
        """Show the menu bar, or hide it again if it is already up."""
        menu_bar = self.menuBar()
        if menu_bar.isVisible():
            menu_bar.setVisible(False)
            return
        menu_bar.setVisible(True)
        menu_bar.setFocus(Qt.MenuBarFocusReason)

    def _maybe_hide_menu_bar(self) -> None:
        """Put the menu bar away once the user is finished with it."""
        menu_bar = self.menuBar()
        if menu_bar.activeAction() is None:
            menu_bar.setVisible(False)

    def keyReleaseEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Alt on its own reveals the menu bar; Escape puts it away."""
        if event.key() == Qt.Key_Alt and not (event.modifiers() & ~Qt.AltModifier):
            self.toggle_menu_bar()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self.menuBar().isVisible():
            self.menuBar().setVisible(False)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Drop the consumer's subscriptions and discovery socket on the way out."""
        self.network_pane.shutdown()
        super().closeEvent(event)
