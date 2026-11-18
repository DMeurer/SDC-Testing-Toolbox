"""The Signals column as four icons: visible, audible, tangible and other.

Each icon is red while its signal announces the condition and grey otherwise. They are drawn
as paths, not emoji, so they look the same on every platform and in both modes.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from .styling import token

# Item data role holding {manifestation: raised} for one alarm row.
SIGNALS_ROLE = Qt.UserRole + 1
ORDER = ("Vis", "Aud", "Tan", "Oth")
NAMES = {"Vis": "Visible", "Aud": "Audible", "Tan": "Tangible", "Oth": "Other"}
SIZE = 14
GAP = 6
# Presences that mean the signal is announcing; a latching signal still announces.
RAISED = frozenset({"On", "Latch"})


def _icon(kind: str) -> tuple[QPainterPath, QPainterPath]:
    """Outline and filled parts of one icon, in a 14 x 14 box."""
    line, fill = QPainterPath(), QPainterPath()
    if kind == "Vis":
        line.moveTo(1, 7)
        line.quadTo(7, 0, 13, 7)
        line.quadTo(7, 14, 1, 7)
        fill.addEllipse(QPointF(7, 7), 2.2, 2.2)
    elif kind == "Aud":
        fill.addPolygon([QPointF(1, 5), QPointF(4, 5), QPointF(7.5, 2), QPointF(7.5, 12),
                         QPointF(4, 9), QPointF(1, 9), QPointF(1, 5)])
        line.arcMoveTo(QRectF(5, 4, 6, 6), 60)
        line.arcTo(QRectF(5, 4, 6, 6), 60, -120)
        line.arcMoveTo(QRectF(3, 1.5, 10, 11), 60)
        line.arcTo(QRectF(3, 1.5, 10, 11), 60, -120)
    elif kind == "Tan":
        line.addRoundedRect(QRectF(4.5, 1.5, 5, 11), 1.2, 1.2)
        for x, step in ((2.5, -1.2), (11.5, 1.2)):
            line.moveTo(x, 4)
            line.lineTo(x + step, 5.5)
            line.lineTo(x, 7)
            line.lineTo(x + step, 8.5)
            line.lineTo(x, 10)
    else:
        line.addEllipse(QPointF(7, 7), 5.5, 5.5)
    return line, fill


def tooltip(raised: dict[str, str]) -> str:
    """'Visible: On' lines for the signals an alarm has."""
    return "\n".join(f"{NAMES.get(kind, kind)}: {presence}" for kind, presence in raised.items())


class SignalIconsDelegate(QStyledItemDelegate):
    """Paints the icons from SIGNALS_ROLE instead of text."""

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: ANN001
        presences = index.data(SIGNALS_ROLE)
        if presences is None:
            super().paint(painter, option, index)
            return
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        left = option.rect.left() + 4
        top = option.rect.center().y() - SIZE / 2
        for slot, kind in enumerate(ORDER):
            colour = token("text/error") if presences.get(kind) in RAISED else token("text/disabled")
            line, fill = _icon(kind)
            painter.save()
            painter.translate(left + slot * (SIZE + GAP), top)
            painter.setPen(QPen(colour, 1.3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(line)
            painter.setPen(Qt.NoPen)
            painter.setBrush(colour)
            painter.drawPath(fill)
            painter.restore()
        painter.restore()

    def sizeHint(self, option, index):  # noqa: ANN001, ANN201, N802 - Qt naming
        size = super().sizeHint(option, index)
        if index.data(SIGNALS_ROLE) is not None:
            size.setWidth(max(size.width(), 8 + len(ORDER) * SIZE + (len(ORDER) - 1) * GAP))
        return size
