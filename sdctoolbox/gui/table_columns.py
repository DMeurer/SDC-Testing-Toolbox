"""Column sizing shared by every table in the tool.

All four tables want the same three things:

* sensible defaults, meaning each column just wide enough for what is in it, with one
  nominated column absorbing whatever is left over
* a total that always equals the viewport exactly, so nothing scrolls sideways and no blank
  strip appears down the right
* separators that trade width between the two columns they divide, rather than pushing
  everything along

Qt gives none of that by itself. Interactive columns can be dragged but grow the total,
ResizeToContents and Stretch cannot be dragged at all, and a QGridLayout-style stretch is
not available to a header. So this class owns the widths outright.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QHeaderView

if TYPE_CHECKING:
    from PySide6.QtWidgets import QTableWidget

# Nothing may be dragged narrower than this.
DEFAULT_MINIMUM = 40

# The flexible column gets a larger floor, since it holds prose.
DEFAULT_FLEXIBLE_MINIMUM = 110


class TableColumns(QObject):
    """Owns the column widths of one table.

    Construct it once per table and keep the reference alive; it installs itself as an event
    filter and connects to the header::

        self._columns = TableColumns(self.table, flexible=COL_LABEL)

    Call ``refit()`` after the contents change.
    """

    def __init__(
        self,
        table: QTableWidget,
        flexible: int,
        minimum: int = DEFAULT_MINIMUM,
        flexible_minimum: int = DEFAULT_FLEXIBLE_MINIMUM,
    ) -> None:
        super().__init__(table)
        self.table = table
        self.flexible = flexible
        self.minimum = minimum
        self.flexible_minimum = flexible_minimum

        # True while we are setting widths ourselves, so our own changes are not mistaken
        # for the user dragging a separator.
        self._adjusting = False
        # Until a separator is dragged we keep choosing widths from the contents. After
        # that the user's widths stand, and only the flexible column is touched.
        self._user_sized = False

        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(minimum)
        header.sectionResized.connect(self._on_section_resized)

        table.setHorizontalScrollBarPolicy(table.horizontalScrollBarPolicy().ScrollBarAlwaysOff)
        # The viewport is what the columns have to add up to, and it changes without the
        # table itself being resized, for instance when a vertical scrollbar appears.
        table.viewport().installEventFilter(self)

    # -- public --------------------------------------------------------------------

    def refit(self) -> None:
        """Re-apply the widths. Call after the contents change."""
        self._fit()

    def reset(self) -> None:
        """Forget the user's widths and go back to fitting the contents."""
        self._user_sized = False
        self._fit()

    @property
    def user_sized(self) -> bool:
        """Whether the user has dragged a separator."""
        return self._user_sized

    def total_width(self) -> int:
        """Current sum of all column widths."""
        return sum(self.table.columnWidth(c) for c in range(self.table.columnCount()))

    def available_width(self) -> int:
        """What the columns have to add up to."""
        return self.table.viewport().width()

    def minimum_total(self) -> int:
        """The narrowest the columns can legitimately be.

        Every column at its floor. Below this the table cannot fill its viewport without
        columns too narrow to read, so the table declares this as its minimum width and the
        surrounding layout is obliged to respect it.
        """
        count = self.table.columnCount()
        if count == 0:
            return 0
        return self.minimum * (count - 1) + self.flexible_minimum

    def _apply_minimum_width(self) -> None:
        """Tell the layout how narrow this table may get."""
        frame = self.table.width() - self.table.viewport().width()
        self.table.setMinimumWidth(self.minimum_total() + max(0, frame))

    # -- internals -----------------------------------------------------------------

    def _floor(self, column: int) -> int:
        return self.flexible_minimum if column == self.flexible else self.minimum

    def _set_width(self, column: int, width: int) -> None:
        self.table.setColumnWidth(column, max(self._floor(column), width))

    def _fit(self) -> None:
        """Make the columns add up to the viewport, no more and no less."""
        if self._adjusting:
            return
        count = self.table.columnCount()
        available = self.available_width()
        if count == 0 or available <= 0:
            return  # not laid out yet; a later resize will do the work

        self._apply_minimum_width()

        self._adjusting = True
        try:
            if not self._user_sized:
                self.table.resizeColumnsToContents()
                for column in range(count):
                    self._set_width(column, self.table.columnWidth(column))

            others = sum(
                self.table.columnWidth(c) for c in range(count) if c != self.flexible
            )
            slack = available - others
            if slack >= self.flexible_minimum:
                self.table.setColumnWidth(self.flexible, slack)
                return

            # Too narrow even with the flexible column at its floor. Claw the difference
            # back from the widest of the others so the total still fits.
            self.table.setColumnWidth(self.flexible, self.flexible_minimum)
            excess = others + self.flexible_minimum - available
            for column in sorted(
                (c for c in range(count) if c != self.flexible),
                key=self.table.columnWidth,
                reverse=True,
            ):
                if excess <= 0:
                    break
                spare = self.table.columnWidth(column) - self.minimum
                if spare <= 0:
                    continue
                take = min(excess, spare)
                self.table.setColumnWidth(column, self.table.columnWidth(column) - take)
                excess -= take
        finally:
            self._adjusting = False

    def _on_section_resized(self, index: int, old: int, new: int) -> None:
        """A separator was dragged: move the width to or from the next column.

        Dragging the separator on the right of a column should widen that column and narrow
        its neighbour, leaving the total alone. Qt's own behaviour is to grow the total and
        let the rest scroll off, which is not what a table pinned to its viewport can do.
        """
        if self._adjusting:
            return
        delta = new - old
        if delta == 0:
            return

        self._user_sized = True
        count = self.table.columnCount()
        donor = index + 1

        self._adjusting = True
        try:
            if donor >= count:
                # The last column has no neighbour to take from, and growing it would push
                # the total past the viewport. Put it back.
                self.table.setColumnWidth(index, old)
                return

            donor_floor = self._floor(donor)
            donor_width = self.table.columnWidth(donor)
            wanted = donor_width - delta

            if wanted < donor_floor:
                # The neighbour cannot give that much. Take only what it has.
                giveable = donor_width - donor_floor
                self.table.setColumnWidth(index, old + giveable)
                self.table.setColumnWidth(donor, donor_floor)
                return

            self.table.setColumnWidth(donor, wanted)
        finally:
            self._adjusting = False

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        """Refit whenever the viewport changes size."""
        if event.type() == QEvent.Resize and watched is self.table.viewport():
            self._fit()
        return super().eventFilter(watched, event)
