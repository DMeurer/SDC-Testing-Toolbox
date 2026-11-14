"""Independent consumer sessions; selecting a tab changes only what is visible.

A tab is only created by connecting. While nothing is connected the tab bar is hidden and the
one idle session just offers discovery. Once a host adopts the discovery slot, the current
tab's Discovery section is shown there, above the tabs.
"""

from __future__ import annotations

import logging
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFontMetrics
from PySide6.QtWidgets import QMessageBox, QTabBar, QTabWidget, QVBoxLayout, QWidget

from ..consumer_service import SharedDiscovery
from .consumer_pane import ConsumerPane
from .no_wheel import NoWheelTabBar

logger = logging.getLogger("sdctoolbox.gui.consumer_tabs")


class ConsumerTabs(QTabWidget):
    """One existing ConsumerPane owns each connection, its work and its retirement."""

    def __init__(self, ip: str, parent: QWidget | None = None, *, own_epr=None, tls_config=None) -> None:
        super().__init__(parent)
        self._ip = ip
        self._own_epr = own_epr
        self._tls_config = tls_config
        # Every tab lists the same providers, so they share one discovery.
        self._discovery = SharedDiscovery(ip)
        self._use_widgets = True
        self._shutdown = False
        self._next_number = 1
        self.setTabBar(NoWheelTabBar())
        self.setTabsClosable(True)
        self.setDocumentMode(True)  # no frame around the page while the tab bar is hidden
        self.setUsesScrollButtons(True)
        self.setAccessibleName("Provider connections")
        self.tabCloseRequested.connect(self.close_connection)
        self.currentChanged.connect(self._on_current_changed)
        self.discovery_slot: QWidget | None = None

        close = QAction("Close current connection", self)
        close.setShortcut("Ctrl+W")
        close.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        close.triggered.connect(lambda: self.close_connection(self.currentIndex()))
        self.addAction(close)
        self.add_connection()

    @property
    def current_pane(self) -> ConsumerPane:
        """The selected pane, retained even after shutdown for existing callers."""
        return self.currentWidget()

    @property
    def panes(self) -> list[ConsumerPane]:
        """Read the page order from Qt rather than maintaining a second session list."""
        return [self.widget(index) for index in range(self.count())]

    def add_connection(self) -> ConsumerPane:
        """Start a new session before changing any existing tab or selection."""
        if self._shutdown:
            raise RuntimeError("Connection tabs are shut down")
        pane = ConsumerPane(self._ip, own_epr=self._own_epr, tls_config=self._tls_config, discovery=self._discovery)
        try:
            pane.set_use_widgets(self._use_widgets)
            pane.setProperty("connectionNumber", self._next_number)
            pane.status_changed.connect(self._on_status_changed)
            pane.connect_elsewhere.connect(self._connect_in_new_tab)
            index = self.addTab(pane, "")
            self._update_title(pane, pane.status_label.text())
            self.setCurrentIndex(index)
        except Exception:
            pane.shutdown()
            pane.deleteLater()
            raise
        self._next_number += 1
        self._update_connection_state()
        return pane

    def attach_discovery_slot(self, slot: QWidget) -> None:
        """Show the current tab's Discovery section in `slot` from now on."""
        self.discovery_slot = slot
        if slot.layout() is None:
            QVBoxLayout(slot).setContentsMargins(0, 0, 0, 0)
        self._on_current_changed(self.currentIndex())

    def _on_current_changed(self, _index: int) -> None:
        if self.discovery_slot is None:
            return
        current = self.current_pane
        for pane in self.panes:
            section = pane.discovery_section
            if pane is current:
                self.discovery_slot.layout().addWidget(section)
                section.show()
            elif section.parent() is not pane:
                section.hide()
                pane.layout().insertWidget(0, section)

    def _connect_in_new_tab(self, device) -> None:  # noqa: ANN001 - a DiscoveredDevice
        """Open a device in its own tab, or go to the tab that already shows it."""
        for pane in self.panes:
            if pane.remote is not None and getattr(pane.remote, "epr", None) == device.epr:
                self.setCurrentWidget(pane)
                return
        try:
            pane = self.add_connection()
        except Exception as exc:
            self._show_add_error(exc)
            return
        pane.connect_to(device)

    def _update_connection_state(self) -> None:
        """Tab bar only once something is connected; Tab N chips in every discovery list."""
        connected = {pane.remote.epr: pane.property("connectionNumber")
                     for pane in self.panes if pane.remote is not None and getattr(pane.remote, "epr", None)}
        self.tabBar().setVisible(self.count() > 1 or bool(connected))
        for pane in self.panes:
            pane.set_device_tabs(connected)

    def _show_add_error(self, exc: Exception) -> None:
        logger.exception("could not add consumer connection")
        QMessageBox.warning(self, "Could not add connection", str(exc))

    def close_connection(self, index: int) -> None:
        """Retire only this page; normal last-close leaves a usable empty session."""
        if self._shutdown or not 0 <= index < self.count():
            return
        pane = self.widget(index)
        if self.count() == 1:
            # Keep the existing session if its replacement cannot start.
            try:
                self.add_connection()
            except Exception as exc:
                self._show_add_error(exc)
                return
        try:
            pane.shutdown()
        except Exception as exc:
            logger.exception("could not close consumer connection")
            QMessageBox.warning(self, "Could not close connection", str(exc))
            return
        self.removeTab(index)
        pane.discovery_section.deleteLater()
        pane.deleteLater()
        self._update_connection_state()

    def _on_status_changed(self, status: str) -> None:
        self._update_title(self.sender(), status)
        self._update_connection_state()

    def _update_title(self, pane: ConsumerPane, status: str) -> None:
        index = self.indexOf(pane)
        if index < 0:
            return
        number = pane.property("connectionNumber")
        status = " ".join(status.split())
        epr = " ".join((getattr(pane.remote, "epr", None) or "").split())
        friendly_name = getattr(pane.remote, "friendly_name", None)
        caption = " ".join((friendly_name or epr or status.removeprefix("Connected to ")).split())
        title = QFontMetrics(self.font()).elidedText(f"{number} · {caption[:80]}", Qt.ElideRight, 200)
        self.setTabText(index, title.replace("&", "&&"))
        detail = status[:1024]
        if friendly_name:
            detail = f"{caption[:256]} — {status[:768]}"
        if epr and epr not in detail:
            detail = f"{epr[:512]} — {detail[:512]}"
        self.setTabToolTip(index, f"<qt>{escape(f'Connection {number}: {detail}')}</qt>")
        bar = self.tabBar()
        button = bar.tabButton(index, QTabBar.RightSide) or bar.tabButton(index, QTabBar.LeftSide)
        if button is not None:
            button.setAccessibleName(f"Close connection {number}")
            button.setToolTip(f"Close connection {number}")
            button.setFocusPolicy(Qt.StrongFocus)

    def set_use_widgets(self, enabled: bool) -> None:
        """Apply the global view to existing pages and remember it for new ones."""
        self._use_widgets = enabled
        for pane in self.panes:
            pane.set_use_widgets(enabled)

    def shutdown(self) -> None:
        """Reach every session even if one retirement fails; retain pages for retry."""
        self._shutdown = True
        error = None
        for pane in self.panes:
            try:
                pane.shutdown()
            except Exception as exc:
                logger.exception("consumer connection shutdown failed")
                if error is None:
                    error = exc
        if error is not None:
            raise error
