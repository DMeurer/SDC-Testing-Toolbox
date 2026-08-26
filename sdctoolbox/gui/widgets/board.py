"""A board of metric controls.

Each metric gets a card: its name, its control, its unit and handle. The cards reflow into
as many columns as the width allows, so the board is usable both in a half-window panel and
across the whole thing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..styling import mute
from .factory import build_widget

if TYPE_CHECKING:
    from .base import MetricWidget, WidgetSpec

#: Narrowest a card may be before the board drops to fewer columns.
MIN_CARD_WIDTH = 260


class MetricCard(QFrame):
    """One metric: heading, control, and the details underneath."""

    def __init__(self, spec: WidgetSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.setFrameShape(QFrame.StyledPanel)

        heading = QLabel(spec.caption)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        heading.setWordWrap(True)

        self.control: MetricWidget = build_widget(spec, self)

        footer_parts = [spec.handle]
        if spec.unit:
            footer_parts.append(spec.unit)
        if spec.note:
            footer_parts.append(spec.note)
        footer = QLabel("  \u00b7  ".join(footer_parts))
        footer.setWordWrap(True)
        mute(footer)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addWidget(heading)
        layout.addWidget(self.control)
        layout.addWidget(footer)


class WidgetBoard(QScrollArea):
    """Shows a control per metric, reflowing into columns as the width allows."""

    #: handle, value. Forwarded from whichever card asked.
    value_requested = Signal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)

        self._canvas = QWidget()
        self._grid = QGridLayout(self._canvas)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setSpacing(8)
        self.setWidget(self._canvas)

        self._empty = QLabel("Nothing to show yet")
        self._empty.setAlignment(Qt.AlignCenter)
        mute(self._empty)

        self._cards: dict[str, MetricCard] = {}
        self._order: list[str] = []
        self._columns = 0

    # -- content -------------------------------------------------------------------

    @property
    def handles(self) -> list[str]:
        """Handles currently on the board, in display order."""
        return list(self._order)

    def card(self, handle: str) -> MetricCard | None:
        """The card for a handle, or None."""
        return self._cards.get(handle)

    def set_metrics(self, specs: list[WidgetSpec]) -> None:
        """Rebuild the board.

        Cards are recreated when the metric's definition changes, and kept otherwise, so a
        refresh does not interrupt somebody using a control.
        """
        wanted = {spec.handle: spec for spec in specs}

        for handle in list(self._cards):
            if handle not in wanted or self._cards[handle].spec != wanted[handle]:
                card = self._cards.pop(handle)
                card.setParent(None)
                card.deleteLater()

        for spec in specs:
            if spec.handle not in self._cards:
                card = MetricCard(spec, self._canvas)
                card.control.value_requested.connect(self.value_requested)
                self._cards[spec.handle] = card

        self._order = [spec.handle for spec in specs]
        self._relayout(force=True)

    def show_values(self, values: dict[str, Any]) -> None:
        """Push values in, skipping any control the user is currently working with."""
        for handle, value in values.items():
            card = self._cards.get(handle)
            if card is not None and not card.control.busy_editing():
                card.control.show_value(value)

    def clear(self) -> None:
        """Remove everything."""
        self.set_metrics([])

    # -- layout --------------------------------------------------------------------

    def _column_count(self) -> int:
        available = self.viewport().width() - 8
        return max(1, available // MIN_CARD_WIDTH)

    def _relayout(self, *, force: bool = False) -> None:
        columns = self._column_count()
        if not force and columns == self._columns:
            return
        self._columns = columns

        while self._grid.count():
            self._grid.takeAt(0)

        if not self._order:
            self._grid.addWidget(self._empty, 0, 0)
            self._empty.setVisible(True)
            return
        self._empty.setVisible(False)

        for index, handle in enumerate(self._order):
            card = self._cards.get(handle)
            if card is None:
                continue
            self._grid.addWidget(card, index // columns, index % columns)
            card.setVisible(True)

        for column in range(columns):
            self._grid.setColumnStretch(column, 1)
        # Keep the cards packed at the top rather than spread down the panel.
        self._grid.setRowStretch(self._grid.rowCount(), 1)

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        super().resizeEvent(event)
        self._relayout()
