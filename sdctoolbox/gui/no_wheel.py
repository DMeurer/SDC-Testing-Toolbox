"""Controls that do not change their value when the wheel is turned over them.

Qt lets a combo box, a slider and a tab bar consume wheel events whether or not they have
focus. On the widget board that is actively harmful: the board is a scroll area full of
cards, so scrolling down the panel silently rewrites every value the pointer crosses on the
way - and on the provider's own board those writes go straight into the MDIB.

Ignoring the event is not enough. Qt's documentation says an ignored wheel event is offered
to the parent, but measured here it is not: the board stops scrolling and every card becomes
a dead zone. So these controls also hand the event to the nearest scrollable ancestor, which
leaves the wheel doing the one thing it should do over a card - scrolling past it.

Values are still reachable by keyboard, which is where a slider's arrow keys and a combo
box's typeahead already were.
"""

from __future__ import annotations

from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QComboBox, QSlider, QTabBar


def _scrollable_ancestor(widget) -> QAbstractScrollArea | None:  # noqa: ANN001 - any QWidget
    """The nearest enclosing scroll area, or None when nothing above can scroll."""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        parent = parent.parentWidget()
    return None


def _pass_to_scroller(widget, event) -> None:  # noqa: ANN001 - QWidget and QWheelEvent
    """Refuse the event, then offer it to whatever can usefully scroll."""
    event.ignore()
    area = _scrollable_ancestor(widget)
    if area is not None:
        QApplication.sendEvent(area.viewport(), event)


class NoWheelComboBox(QComboBox):
    """A combo box the wheel scrolls past rather than through."""

    def wheelEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Never change the selection; scroll the panel instead."""
        _pass_to_scroller(self, event)


class NoWheelSlider(QSlider):
    """A slider the wheel scrolls past rather than drags."""

    def wheelEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Never move the handle; scroll the panel instead."""
        _pass_to_scroller(self, event)


class NoWheelTabBar(QTabBar):
    """A tab bar the wheel does not flick through.

    Not a value, but the same surprise: a tab bar switches panels on wheel by default, so
    scrolling near the top of the window swaps My device for Network under the pointer.
    """

    def wheelEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Never change tab; offer the event upwards instead."""
        _pass_to_scroller(self, event)
