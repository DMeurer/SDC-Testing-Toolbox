"""Independent consumer sessions; selecting a tab changes only what is visible."""

from __future__ import annotations

import logging
from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFontMetrics
from PySide6.QtWidgets import QMessageBox, QTabBar, QTabWidget, QToolButton, QWidget

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
        self._use_widgets = True
        self._shutdown = False
        self._next_number = 1
        self.setTabBar(NoWheelTabBar())
        self.setTabsClosable(True)
        self.setUsesScrollButtons(True)
        self.setAccessibleName("Provider connections")
        self.tabCloseRequested.connect(self.close_connection)

        add = QToolButton(self)
        add.setText("+")
        add.setAccessibleName("Add connection")
        add.setToolTip("Add connection (Ctrl+T)")
        add.setShortcut("Ctrl+T")
        add.clicked.connect(self._on_add_connection)
        self.setCornerWidget(add)

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
        pane = ConsumerPane(self._ip, own_epr=self._own_epr, tls_config=self._tls_config)
        try:
            pane.set_use_widgets(self._use_widgets)
            pane.setProperty("connectionNumber", self._next_number)
            pane.status_changed.connect(self._on_status_changed)
            index = self.addTab(pane, "")
            self._update_title(pane, pane.status_label.text())
            self.setCurrentIndex(index)
        except Exception:
            pane.shutdown()
            pane.deleteLater()
            raise
        self._next_number += 1
        return pane

    def _on_add_connection(self) -> None:
        try:
            self.add_connection()
        except Exception as exc:
            self._show_add_error(exc)

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
        pane.deleteLater()

    def _on_status_changed(self, status: str) -> None:
        self._update_title(self.sender(), status)

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
        self.cornerWidget().setEnabled(False)
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
