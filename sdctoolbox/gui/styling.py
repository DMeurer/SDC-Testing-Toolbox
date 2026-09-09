"""Small theme helpers.

Everything here derives its colours from the widget's *live* palette rather than naming a
fixed shade, so the interface stays legible in a light theme and a dark one.

The obvious-looking shortcuts do not work:

* ``palette(mid)`` is a border shade. On a dark theme it is dark grey, which lands dark text
  on a dark background.
* ``QPalette.PlaceholderText`` is not filled in by every style - Fusion reports pure black -
  so it cannot be relied on for de-emphasised text either.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QAbstractItemView, QLabel, QSizePolicy, QWidget

# How far to blend foreground towards background for de-emphasised text, in percent.
# High enough to read comfortably, low enough to look secondary.
_MUTE_PERCENT = 45


def _blend(front: QColor, back: QColor, percent: int) -> QColor:
    """Mix two colours. percent is how much of `back` ends up in the result."""
    keep = 100 - percent
    return QColor(
        (front.red() * keep + back.red() * percent) // 100,
        (front.green() * keep + back.green() * percent) // 100,
        (front.blue() * keep + back.blue() * percent) // 100,
    )


def is_dark(widget: QWidget) -> bool:
    """Whether the widget currently sits on a dark background."""
    return widget.palette().color(QPalette.ColorRole.Window).lightness() < 128  # noqa: PLR2004


def muted_colour(widget: QWidget) -> QColor:
    """A readable secondary text colour for the widget's current theme."""
    palette = widget.palette()
    return _blend(
        palette.color(QPalette.ColorRole.WindowText),
        palette.color(QPalette.ColorRole.Window),
        _MUTE_PERCENT,
    )


def mute(widget: QWidget) -> None:
    """Render a label as secondary text without hard-coding a colour."""
    palette = widget.palette()
    colour = muted_colour(widget)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(group, QPalette.ColorRole.WindowText, colour)
        palette.setColor(group, QPalette.ColorRole.Text, colour)
    widget.setPalette(palette)


def error_colour(widget: QWidget) -> QColor:
    """A red that stays legible on either theme."""
    return QColor("#ff7b72") if is_dark(widget) else QColor("#c0392b")


def mark_as_error(widget: QWidget) -> None:
    """Render a label as an error message."""
    palette = widget.palette()
    colour = error_colour(widget)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(group, QPalette.ColorRole.WindowText, colour)
    widget.setPalette(palette)


def constrain_dynamic_label(
    label: QLabel,
    *,
    max_lines: int = 1,
    max_width: int | None = None,
) -> None:
    """Render variable content literally without letting it dictate layout dimensions."""
    label.setTextFormat(Qt.PlainText)
    horizontal = QSizePolicy.Ignored if max_width is None else QSizePolicy.Preferred
    label.setSizePolicy(horizontal, QSizePolicy.Preferred)
    label.setWordWrap(max_lines > 1)
    label.setMaximumHeight(label.fontMetrics().lineSpacing() * max_lines + 2)
    if max_width is not None:
        label.setMaximumWidth(max_width)


# Draws the selection as one flat band across the row.
#
# Without this, the Windows 11 style decorates each cell individually - rounded corners and
# an accent marker on the current cell - which reads as coloured fragments down the left of
# every column rather than as a single selected row. Colours come from palette() so this
# stays theme-correct.
FLAT_SELECTION_QSS = """
QTableView {
    outline: 0;
}
QTableView::item {
    border: 0px;
    margin: 0px;
}
QTableView::item:selected,
QTableView::item:selected:active,
QTableView::item:selected:!active {
    background: palette(highlight);
    color: palette(highlighted-text);
}
"""


def apply_row_selection_style(view: QAbstractItemView) -> None:
    """Make a table highlight whole rows cleanly, with no per-cell decoration.

    Keyboard focus still works; only the painted focus ring goes away, via `outline: 0`.
    """
    view.setStyleSheet(FLAT_SELECTION_QSS)
