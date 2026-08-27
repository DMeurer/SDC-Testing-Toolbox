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
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..styling import mute
from .factory import build_widget

if TYPE_CHECKING:
    from .base import MetricWidget, WidgetSpec

# Narrowest a card may be before the board drops to fewer columns.
MIN_CARD_WIDTH = 260


class MetricCard(QFrame):
    """One metric: heading, control, and the details underneath.

    A deletable card carries a bin in its top right. That is the only way to remove a
    metric while the board is showing, because there is no row to select.
    """

    # The bin was clicked. Carries the handle.
    delete_requested = Signal(str)

    def __init__(
        self,
        spec: WidgetSpec,
        parent: QWidget | None = None,
        *,
        deletable: bool = False,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self.setFrameShape(QFrame.StyledPanel)

        heading = QLabel(spec.caption)
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        heading.setWordWrap(True)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(4)
        title_row.addWidget(heading, 1)

        self.delete_button: QToolButton | None = None
        if deletable:
            self.delete_button = QToolButton()
            self.delete_button.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton),
            )
            self.delete_button.setAutoRaise(True)
            self.delete_button.setToolTip(f"Remove {spec.handle}")
            self.delete_button.setAccessibleName(f"Remove {spec.handle}")
            self.delete_button.clicked.connect(lambda: self.delete_requested.emit(spec.handle))
            title_row.addWidget(self.delete_button, 0, Qt.AlignTop)

        self.control: MetricWidget = build_widget(spec, self)

        footer_parts = [spec.handle]
        if spec.unit:
            footer_parts.append(spec.unit)
        if spec.domain:
            # A distribution's domain is what its x axis means, so it belongs on the card
            # rather than only in the table.
            footer_parts.append(f"over {spec.domain}")
        if spec.sample_period:
            footer_parts.append(f"{spec.sample_period}s/sample")
        if spec.note:
            footer_parts.append(spec.note)
        footer = QLabel("  \u00b7  ".join(footer_parts))
        footer.setWordWrap(True)
        mute(footer)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(title_row)
        layout.addWidget(self.control)
        layout.addWidget(footer)


class WidgetBoard(QScrollArea):
    """Shows a control per metric, reflowing into columns as the width allows."""

    # handle, value. Forwarded from whichever card asked.
    value_requested = Signal(str, object)
    # handle. A card's bin was clicked.
    delete_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, deletable: bool = False) -> None:
        super().__init__(parent)
        # Whether cards carry a bin. False for a peer's metrics, which are not ours to
        # delete.
        self.deletable = deletable
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
        # Highest column and row index we have ever stretched, so they can all be undone.
        self._stretched_columns = 0
        self._stretched_rows = 0

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
                card = MetricCard(spec, self._canvas, deletable=self.deletable)
                card.control.value_requested.connect(self.value_requested)
                card.delete_requested.connect(self.delete_requested)
                self._cards[spec.handle] = card

        self._order = [spec.handle for spec in specs]
        self._relayout(force=True)

    def show_values(self, values: dict[str, Any]) -> None:
        """Push values in, skipping any control the user is currently working with."""
        for handle, value in values.items():
            card = self._cards.get(handle)
            if card is not None and not card.control.busy_editing():
                card.control.show_value(value)

    def append_samples(self, blocks: dict[str, Any]) -> None:
        """Hand each named card a block of new samples.

        Deliberately keyed by handle and called only with the handles a report actually
        carried: pushing every card on every report is what made traces jagged.
        """
        for handle, samples in blocks.items():
            card = self._cards.get(handle)
            if card is not None:
                card.control.append_samples(samples)

    def clear(self) -> None:
        """Remove everything."""
        self.set_metrics([])

    # -- layout --------------------------------------------------------------------

    def _column_count(self) -> int:
        available = self.viewport().width() - 8
        return max(1, available // MIN_CARD_WIDTH)

    def _clear_grid(self) -> None:
        """Empty the grid and undo every stretch we have ever set on it.

        QGridLayout never shrinks: its columnCount and rowCount only grow, and a stretch
        set on a column stays there after the column is empty. Without this, going from
        four columns down to two leaves columns three and four still stretching, so the
        cards bunch up on the left and the rest of the panel stays blank.
        """
        while self._grid.count():
            self._grid.takeAt(0)

        for column in range(max(self._grid.columnCount(), self._stretched_columns)):
            self._grid.setColumnStretch(column, 0)
            self._grid.setColumnMinimumWidth(column, 0)
        for row in range(max(self._grid.rowCount(), self._stretched_rows)):
            self._grid.setRowStretch(row, 0)

    def _relayout(self, *, force: bool = False) -> None:
        columns = self._column_count()
        if not force and columns == self._columns:
            return
        self._columns = columns

        self._clear_grid()

        if not self._order:
            self._grid.addWidget(self._empty, 0, 0)
            self._empty.setVisible(True)
            return
        self._empty.setVisible(False)

        rows = 0
        for index, handle in enumerate(self._order):
            card = self._cards.get(handle)
            if card is None:
                continue
            row, column = divmod(index, columns)
            self._grid.addWidget(card, row, column)
            card.setVisible(True)
            rows = row + 1

        for column in range(columns):
            self._grid.setColumnStretch(column, 1)
        # Keep the cards packed at the top rather than spread down the panel.
        self._grid.setRowStretch(rows, 1)

        self._stretched_columns = max(self._stretched_columns, columns)
        self._stretched_rows = max(self._stretched_rows, rows + 1)

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        super().resizeEvent(event)
        self._relayout()
