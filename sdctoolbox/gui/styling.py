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

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QAbstractItemView, QLabel, QSizePolicy, QTableWidget, QWidget

# The design's colour tokens as (light, dark). Every coloured part of the interface names one.
TOKENS: dict[str, tuple[str, str]] = {
    "bg/window": ("#EFEFEF", "#1E1E1E"),
    "bg/surface": ("#FFFFFF", "#2D2D2D"),
    "bg/field": ("#FFFFFF", "#393939"),
    "bg/button": ("#FAFAFA", "#2B2B2B"),
    "bg/button-disabled": ("#F2F2F2", "#282828"),
    "bg/hover": ("#F5F5F5", "#393939"),
    "bg/titlebar": ("#F3F3F3", "#202020"),
    "bg/header": ("#EFEFEF", "#3C3C3C"),
    "bg/inset": ("#EFEFEF", "#2D2D2D"),
    "border/default": ("#E2E2E2", "#3A3A3A"),
    "border/strong": ("#C8C8C8", "#454545"),
    "border/underline": ("#808080", "#A0A0A0"),
    "text/primary": ("#000000", "#FFFFFF"),
    "text/muted": ("#6B6B6B", "#9A9A9A"),
    "text/disabled": ("#A0A0A0", "#787878"),
    "text/on-accent": ("#FFFFFF", "#000000"),
    "text/error": ("#C0392B", "#FF7B72"),
    "accent": ("#0078D4", "#4CC2FF"),
    "accent/tint": ("#E6F0FA", "#1B2B3A"),
    "accent/text": ("#005A9E", "#99D9FF"),
    "highlight": ("#7B3FE4", "#B99CFF"),
    "highlight/text": ("#6430C4", "#C9B3FF"),
    "kind/mdib": ("#2E7D32", "#5FD37A"),
    "kind/toolbox": ("#A07800", "#F5C842"),
    "kind/toolbox-text": ("#7A5C00", "#F5C842"),
    "state/pending": ("#B25E00", "#FFB340"),
    "state/blocked": ("#D13438", "#FF6B6B"),
    "plot/grid": ("#9A9A9A", "#8A8A8A"),
}

BUTTON_KINDS = ("highlight", "highlight-outline", "design", "design-outline")

_CHIP = "border-radius: 8px; padding: 0px 6px; font-size: 11px;"


def app_is_dark() -> bool:
    """Whether the application palette is currently dark."""
    return QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128  # noqa: PLR2004


def token(name: str, dark: bool | None = None) -> QColor:  # noqa: FBT001
    """The colour a design token has in the current (or the given) mode."""
    light, dark_value = TOKENS[name]
    return QColor(dark_value if (app_is_dark() if dark is None else dark) else light)


def theme_style_sheet(dark: bool) -> str:  # noqa: FBT001
    """Application style sheet for what the tokens colour: buttons, status bar, tabs, sections, chips."""
    t = {name: pair[1] if dark else pair[0] for name, pair in TOKENS.items()}
    kinds = {
        "highlight": (t["highlight"], t["highlight"], t["text/on-accent"]),
        "highlight-outline": (t["highlight"], "transparent", t["highlight/text"]),
        "design": (t["accent"], t["accent"], t["text/on-accent"]),
        "design-outline": (t["accent"], "transparent", t["accent/text"]),
    }
    rules = [
        f'*[kind="{kind}"] {{ border-radius: 4px; padding: 4px 12px; border: 1px solid {line};'
        f" background: {fill}; color: {text}; }}"
        for kind, (line, fill, text) in kinds.items()
    ]
    rules += [
        f'*[kind="highlight-outline"]:pressed, *[kind="design-outline"]:pressed {{ background: {t["accent/tint"]}; }}',
        f'*[kind="design-outline"]:checked {{ background: {t["accent"]}; color: {t["text/on-accent"]}; }}',
        f'*[kind]:disabled {{ background: transparent; border: 1px solid {t["border/default"]};'
        f' color: {t["text/disabled"]}; }}',
        f'QFrame[role="card"] {{ background: {t["bg/surface"]}; border: 1px solid {t["border/strong"]};'
        " border-radius: 6px; }",
        f'QWidget#cardBoard {{ background: {t["bg/inset"]}; }}',
        f'QStatusBar {{ background: {t["accent"]}; color: {t["text/on-accent"]}; }}',
        f'QStatusBar QLabel {{ color: {t["text/on-accent"]}; }}',
        "QTabBar::tab { padding: 6px 18px; border: none; border-top: 2px solid transparent;"
        f' background: transparent; color: {t["text/muted"]}; }}',
        f'QTabBar::tab:selected {{ border-top: 2px solid {t["accent"]}; color: {t["accent/text"]}; font-weight: bold; }}',
        f'QFrame[role="section-header"] {{ background: {t["accent/tint"]}; border-radius: 4px; }}',
        f'QLabel[role="section-title"] {{ color: {t["accent/text"]}; font-weight: bold; }}',
        f'QLabel[role="chevron"] {{ color: {t["accent"]}; font-weight: bold; }}',
        f'QLabel[role="chip"] {{ background: {t["bg/surface"]}; color: {t["text/muted"]}; {_CHIP} }}',
        f'QLabel[role="chip-mdib"] {{ border: 1px solid {t["kind/mdib"]}; color: {t["kind/mdib"]}; {_CHIP} }}',
        f'QLabel[role="chip-toolbox"] {{ border: 1px solid {t["kind/toolbox"]};'
        f' color: {t["kind/toolbox-text"]}; {_CHIP} }}',
        f'QLabel[role="chip-tab"] {{ border: 1px solid {t["accent"]}; color: {t["accent/text"]}; {_CHIP} }}',
        f'QFrame[role="sect-mdib"] {{ border: none; border-left: 3px solid {t["kind/mdib"]}; }}',
        f'QFrame[role="sect-toolbox"] {{ border: none; border-left: 3px solid {t["kind/toolbox"]}; }}',
    ]
    return "\n".join(rules)


def apply_theme_style() -> None:
    """Install the token style sheet for the application's current mode."""
    app = QApplication.instance()
    app.setStyleSheet(theme_style_sheet(app_is_dark()))


def set_button_kind(button: QWidget, kind: str) -> None:
    """Colour a button as one of the four kinds of the design (see BUTTON_KINDS)."""
    if kind not in BUTTON_KINDS:
        raise ValueError(f"unknown button kind {kind!r}")
    button.setProperty("kind", kind)
    button.style().unpolish(button)
    button.style().polish(button)


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
    widget.setProperty("_toolboxTextStyle", "muted")
    palette = widget.palette()
    colour = muted_colour(widget)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(group, QPalette.ColorRole.WindowText, colour)
        palette.setColor(group, QPalette.ColorRole.Text, colour)
    widget.setPalette(palette)


def error_colour(widget: QWidget) -> QColor:
    """A red that stays legible on either theme."""
    return token("text/error", is_dark(widget))


def mark_as_error(widget: QWidget) -> None:
    """Render a label as an error message."""
    widget.setProperty("_toolboxTextStyle", "error")
    palette = widget.palette()
    colour = error_colour(widget)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        palette.setColor(group, QPalette.ColorRole.WindowText, colour)
    widget.setPalette(palette)


def set_dark_mode(enabled: bool) -> None:
    """Switch the application palette, including existing hints and errors."""
    app = QApplication.instance()
    muted_items = []
    for table in app.allWidgets():
        if isinstance(table, QTableWidget):
            previous = muted_colour(table)
            for row in range(table.rowCount()):
                for column in range(table.columnCount()):
                    item = table.item(row, column)
                    if item is not None and item.foreground().color() == previous:
                        muted_items.append((table, item))
    app.styleHints().setColorScheme(Qt.ColorScheme.Dark if enabled else Qt.ColorScheme.Light)
    # Native scheme overrides are ignored by some Qt platform plugins, so build the palette from
    # the tokens: the native Windows dark values, with no surface left pure black.
    role = QPalette.ColorRole
    palette = QPalette(token("bg/button", enabled), token("bg/window", enabled))
    for part, name in ((role.Base, "bg/surface"), (role.AlternateBase, "bg/hover"),
                       (role.Window, "bg/window"), (role.Button, "bg/button"),
                       (role.WindowText, "text/primary"), (role.Text, "text/primary"),
                       (role.ButtonText, "text/primary"), (role.Highlight, "accent"),
                       (role.Link, "accent"), (role.HighlightedText, "text/on-accent"),
                       (role.ToolTipBase, "bg/field"), (role.ToolTipText, "text/primary"),
                       (role.PlaceholderText, "text/muted")):
        palette.setColor(part, token(name, enabled))
    for part in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, part, token("text/disabled", enabled))
    app.setPalette(palette)
    app.setStyleSheet(theme_style_sheet(enabled))
    for widget in app.allWidgets():
        # QSS gives views/headers explicit palettes that no longer inherit app changes.
        if isinstance(widget, QAbstractItemView):
            widget.setPalette(palette)
            widget.viewport().setPalette(palette)
            if widget.property("_toolboxRowSelection"):
                apply_row_selection_style(widget)
        text_style = widget.property("_toolboxTextStyle")
        if text_style in ("muted", "error"):
            widget.setPalette(palette)
            if text_style == "muted":
                mute(widget)
            else:
                mark_as_error(widget)
    for table, item in muted_items:
        with QSignalBlocker(table):
            item.setForeground(muted_colour(table))


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


# Draws the selection as one flat band across the row, in the design's tint.
#
# Without this, the Windows 11 style decorates each cell individually - rounded corners and
# an accent marker on the current cell - which reads as coloured fragments down the left of
# every column rather than as a single selected row. set_dark_mode re-applies it, so the
# tint follows the mode.
FLAT_SELECTION_QSS = """
QTableView, QTreeView {{
    outline: 0;
}}
QTableView::item {{
    border: 0px;
    margin: 0px;
}}
QTableView::item:selected,
QTableView::item:selected:active,
QTableView::item:selected:!active,
QTreeView::item:selected {{
    background: {tint};
    color: {text};
}}
"""


def apply_row_selection_style(view: QAbstractItemView) -> None:
    """Make a table highlight whole rows cleanly, with no per-cell decoration.

    Keyboard focus still works; only the painted focus ring goes away, via `outline: 0`.
    """
    view.setProperty("_toolboxRowSelection", True)
    view.setStyleSheet(FLAT_SELECTION_QSS.format(tint=token("accent/tint").name(), text=token("text/primary").name()))
