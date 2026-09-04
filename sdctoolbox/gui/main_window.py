"""The application window: both roles, either side by side or as tabs.

The tool is meant to be started twice on one machine, so that one instance publishes a
device and the other discovers it. Each instance is therefore both a provider and a
consumer.

By default the two sit in panels either side of a movable divider, so you can watch one
react to the other. View > Split view turns that off and stacks them as tabs instead, which
is easier on a narrow screen.

The menu bar is visible and pinned by default. View > Always show menu bar can unpin it so
that it hides when unused and Alt reveals it again.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from .consumer_pane import ConsumerPane
from .no_wheel import NoWheelTabBar
from .provider_pane import ProviderPane

if TYPE_CHECKING:
    from ..provider_service import ProviderService

PROVIDER_TITLE = "My device"
NETWORK_TITLE = "Network"

CONFIG_FILE_FILTER = "SDC toolbox config (*.json);;All files (*)"

# Where the divider sits when the window opens, as a share of the width.
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
        self.network_pane = ConsumerPane(service.ip, self, own_epr=service.epr.urn)

        self.provider_panel = TitledPanel(PROVIDER_TITLE, self.provider_pane)
        self.network_panel = TitledPanel(NETWORK_TITLE, self.network_pane)

        # Both layouts exist all the time; the panels move between them on demand.
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.tabs = QTabWidget()
        # A tab bar flicks between tabs on wheel by default, so scrolling near the top of
        # the window swaps the panels under the pointer.
        self.tabs.setTabBar(NoWheelTabBar())

        self.stack = QStackedWidget()
        self.stack.addWidget(self.splitter)
        self.stack.addWidget(self.tabs)
        self.setCentralWidget(self.stack)

        # Remembered across a trip through tab mode so the divider comes back where it was.
        self._splitter_sizes: list[int] = []

        self._build_menus()
        self.set_split_view(enabled=True)
        self.set_use_widgets(enabled=True)

        self._show_status_hint()

    # -- layout --------------------------------------------------------------------

    def set_split_view(self, enabled: bool) -> None:  # noqa: FBT001 - matches the Qt signal
        """Choose between panels side by side and stacked tabs."""
        if enabled:
            self._detach_panels()
            for panel in (self.provider_panel, self.network_panel):
                panel.set_heading_visible(True)
                self.splitter.addWidget(panel)
                # Needed here, unlike in tab mode: _detach_panels hid these by dropping
                # their parent, and a QSplitter does not unhide what it is given.
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
            # Deliberately no setVisible here. A QTabWidget shows only its current page,
            # and forcing both visible draws them on top of each other until the first tab
            # switch hands visibility back to the tab widget.
            self.tabs.setCurrentIndex(0)
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

    def set_use_widgets(self, enabled: bool) -> None:  # noqa: FBT001 - matches the Qt signal
        """Show metrics as controls where one fits, or fall back to the table.

        "If possible" is the operative part: a metric with no control that suits it still
        gets a card, showing its value read-only, rather than vanishing from the view.
        """
        for pane in (self.provider_pane, self.network_pane):
            pane.set_use_widgets(enabled)
        if self.widgets_action.isChecked() != enabled:
            self.widgets_action.setChecked(enabled)

    @property
    def widgets_enabled(self) -> bool:
        """Whether the panes are showing controls rather than tables."""
        return self.widgets_action.isChecked()

    @property
    def split_view_enabled(self) -> bool:
        """Whether the panels are currently side by side."""
        return self.stack.currentWidget() is self.splitter

    # -- menus ---------------------------------------------------------------------

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")

        self.import_action = QAction("&Import config\u2026", self)
        self.import_action.setShortcut(QKeySequence("Ctrl+O"))
        self.import_action.setStatusTip("Replace this device with one described in a file")
        self.import_action.triggered.connect(self.import_config)
        file_menu.addAction(self.import_action)

        self.export_action = QAction("&Export config\u2026", self)
        self.export_action.setShortcut(QKeySequence("Ctrl+S"))
        self.export_action.setStatusTip("Save this device's data sources and alarms to a file")
        self.export_action.triggered.connect(self.export_config)
        file_menu.addAction(self.export_action)

        self.presets_menu = file_menu.addMenu("Load &preset")
        self.presets_menu.setStatusTip("Ready-made devices that ship with the tool")
        self._build_presets_menu()

        file_menu.addSeparator()

        self.exit_action = QAction("E&xit", self)
        # QKeySequence.Quit resolves to nothing on Windows, so name the shortcut outright.
        self.exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        self.exit_action.triggered.connect(self.close)
        file_menu.addAction(self.exit_action)

        view_menu = menu_bar.addMenu("&View")

        self.always_show_menu_action = QAction("Always show &menu bar", self)
        self.always_show_menu_action.setCheckable(True)
        self.always_show_menu_action.setChecked(True)
        self.always_show_menu_action.setStatusTip(
            "Keep the menu bar on screen, or hide it until Alt is pressed",
        )
        self.always_show_menu_action.toggled.connect(self.set_always_show_menu)
        view_menu.addAction(self.always_show_menu_action)

        view_menu.addSeparator()

        self.widgets_action = QAction("Use &widgets if possible", self)
        self.widgets_action.setCheckable(True)
        self.widgets_action.setChecked(True)
        self.widgets_action.setShortcut("F9")
        self.widgets_action.setStatusTip(
            "Show each metric as a control that suits it, falling back to the table",
        )
        self.widgets_action.toggled.connect(self.set_use_widgets)
        view_menu.addAction(self.widgets_action)

        self.split_view_action = QAction("&Split view", self)
        self.split_view_action.setCheckable(True)
        self.split_view_action.setChecked(True)
        self.split_view_action.setShortcut("F8")
        self.split_view_action.setStatusTip("Panels side by side, or stacked as tabs")
        self.split_view_action.toggled.connect(self.set_split_view)
        view_menu.addAction(self.split_view_action)

        # Hiding is opt-in. The actions keep working through their shortcuts either way.
        for menu in (file_menu, view_menu):
            menu.aboutToHide.connect(self._maybe_hide_menu_bar)
        menu_bar.setVisible(True)

    # -- configuration files -------------------------------------------------------

    def _build_presets_menu(self) -> None:
        """Fill the preset submenu from the presets folder.

        Built once at startup rather than on every open: the folder ships with the tool and
        does not change while it runs. An empty or unreadable folder leaves one disabled
        entry saying so, which is more use than a menu that opens onto nothing.
        """
        self.presets_menu.clear()
        self.preset_actions: list[QAction] = []

        presets = config.list_presets()
        if not presets:
            empty = QAction("No presets found", self)
            empty.setEnabled(False)
            self.presets_menu.addAction(empty)
            return

        for preset in presets:
            action = QAction(preset.name, self)
            action.setStatusTip(preset.description or preset.summary())
            action.setToolTip(f"{preset.description}\n{preset.summary()}".strip())
            # default=preset, or every entry would close over the last one.
            action.triggered.connect(lambda _checked=False, chosen=preset: self.load_preset(chosen))
            self.presets_menu.addAction(action)
            self.preset_actions.append(action)

    def load_preset(self, preset: config.Preset) -> bool:
        """Replace this device with a shipped preset, asking first if anything would be lost."""
        return self.load_config(preset.path, confirm=True)

    def export_config(self) -> Path | None:
        """Ask for a filename and write this device's configuration to it."""
        suggested = f"{self.service.instance_name}{config.FILE_SUFFIX}"
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export config",
            suggested,
            CONFIG_FILE_FILTER,
        )
        if not filename:
            return None
        try:
            written = config.save(self.service, filename)
        except OSError as exc:
            QMessageBox.warning(self, "Could not export", str(exc))
            return None
        metrics = len(self.service.list_metrics())
        alarms = len(self.service.list_alerts())
        self.statusBar().showMessage(
            f"Exported {metrics} data source(s) and {alarms} alarm(s) to {written.name}",
            8000,
        )
        return written

    def import_config(self) -> bool:
        """Ask for a filename and rebuild this device from it."""
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Import config",
            "",
            CONFIG_FILE_FILTER,
        )
        if not filename:
            return False
        return self.load_config(filename, confirm=True)

    def load_config(self, path: str | Path, *, confirm: bool = False) -> bool:
        """Rebuild this device from a config file.

        Everything already configured is discarded, since a preset describes a whole device
        rather than an addition to one.
        """
        existing = len(self.service.list_metrics()) + len(self.service.list_alerts())
        if confirm and existing:
            answer = QMessageBox.question(
                self,
                "Replace this device?",
                f"Importing replaces the {existing} item(s) you have configured.\n\nContinue?",
            )
            if answer != QMessageBox.Yes:
                return False

        try:
            metrics, alarms = config.load_into(self.service, path)
        except config.ConfigError as exc:
            QMessageBox.warning(self, "Could not import", str(exc))
            return False

        self.provider_pane.refresh()
        self.provider_pane.refresh_alerts()
        self.provider_pane.refresh_actions()
        self.provider_pane.refresh_contexts()
        self.statusBar().showMessage(
            f"Imported {metrics} data source(s) and {alarms} alarm(s) from {Path(path).name}",
            8000,
        )
        return True

    # -- the menu bar --------------------------------------------------------------

    @property
    def always_show_menu(self) -> bool:
        """Whether the menu bar stays on screen rather than hiding until Alt."""
        return self.always_show_menu_action.isChecked()

    def set_always_show_menu(self, enabled: bool) -> None:  # noqa: FBT001 - matches the Qt signal
        """Keep the menu bar visible, or let it hide until Alt is pressed."""
        self.menuBar().setVisible(enabled)
        if self.always_show_menu_action.isChecked() != enabled:
            self.always_show_menu_action.setChecked(enabled)
        self._show_status_hint()

    def toggle_menu_bar(self) -> None:
        """Alt: reveal the menu bar, or put it away again.

        Does nothing while the menu bar is pinned, since there is nothing to reveal and
        hiding it would contradict the setting.
        """
        if self.always_show_menu:
            return
        menu_bar = self.menuBar()
        if menu_bar.isVisible():
            menu_bar.setVisible(False)
            return
        menu_bar.setVisible(True)
        menu_bar.setFocus(Qt.MenuBarFocusReason)

    def _maybe_hide_menu_bar(self) -> None:
        """Put the menu bar away once the user is finished with it."""
        if self.always_show_menu:
            return
        menu_bar = self.menuBar()
        if menu_bar.activeAction() is None:
            menu_bar.setVisible(False)

    def _show_status_hint(self) -> None:
        """Mention Alt only while it is the only way to reach the menu."""
        message = f"Publishing on {self.service.ip}   \u00b7   {self.service.epr.urn}"
        if not self.always_show_menu:
            message += "   \u00b7   press Alt for the menu"
        self.statusBar().showMessage(message)

    def keyReleaseEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Alt on its own reveals the menu bar; Escape puts it away."""
        if event.key() == Qt.Key_Alt and not (event.modifiers() & ~Qt.AltModifier):
            self.toggle_menu_bar()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and not self.always_show_menu and self.menuBar().isVisible():
            self.menuBar().setVisible(False)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Drop the consumer's subscriptions and discovery socket on the way out."""
        self.network_pane.shutdown()
        super().closeEvent(event)
