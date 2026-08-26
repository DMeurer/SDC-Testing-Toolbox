"""A small hand-painted plot for sample arrays.

Kept separate from controls.py because it is the only thing in the project that does its own
painting, and because both a waveform and a distribution use it.

There is no charting dependency. This draws a polyline and a couple of guide lines, and
adding QtCharts for that would be the largest dependency in the project by some margin, for
a widget that is a hundred lines.

Colours come from the palette rather than being named, for the same reason as styling.py:
the tool has to stay legible on a light theme and a dark one.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..styling import muted_colour

# How many samples a scrolling plot keeps. At the default 0.1s period that is half a minute
# of history, which is enough to see a shape without making the repaint expensive.
HISTORY = 300

PLOT_HEIGHT = 76

# Padding inside the frame, so the trace never touches the border.
MARGIN = 3


class SamplePlot(QWidget):
    """Draws a list of samples, either as a scrolling trace or as a static bar chart."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        scrolling: bool = True,
        minimum: Decimal | None = None,
        maximum: Decimal | None = None,
    ) -> None:
        super().__init__(parent)
        self.scrolling = scrolling
        self.setMinimumHeight(PLOT_HEIGHT)
        self.setAutoFillBackground(False)

        self._fixed_low = float(minimum) if minimum is not None else None
        self._fixed_high = float(maximum) if maximum is not None else None
        self._samples: deque[float] = deque(maxlen=HISTORY if scrolling else None)

    # -- content -------------------------------------------------------------------

    @property
    def samples(self) -> list[float]:
        """What is currently on the plot."""
        return list(self._samples)

    def add_samples(self, samples) -> None:  # noqa: ANN001 - any sequence of numbers
        """Take a block of samples.

        A waveform appends, because each block continues the last. A distribution replaces,
        because each one is a whole picture of the same domain.
        """
        try:
            values = [float(sample) for sample in samples]
        except (TypeError, ValueError):
            # A peer may publish something that is not a number at all. Showing the last
            # good trace beats blanking the card.
            return
        if not self.scrolling:
            self._samples.clear()
        self._samples.extend(values)
        self.update()

    def clear(self) -> None:
        """Forget every sample."""
        self._samples.clear()
        self.update()

    # -- painting ------------------------------------------------------------------

    def _bounds(self) -> tuple[float, float]:
        """The vertical extent to draw, preferring the declared range over the data.

        Using the declared TechnicalRange keeps the trace still: scaling to whatever
        happens to be on screen makes a flat signal look like noise, because the axis
        rescales to the last few decimal places.
        """
        if self._fixed_low is not None and self._fixed_high is not None and self._fixed_high > self._fixed_low:
            return self._fixed_low, self._fixed_high
        if not self._samples:
            return 0.0, 1.0
        low, high = min(self._samples), max(self._samples)
        if high <= low:
            # A constant signal still needs a height, or it divides by zero and vanishes.
            return low - 0.5, high + 0.5
        return low, high

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        """Draw the frame, a mid line, and the samples."""
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        palette = self.palette()
        frame = muted_colour(self)
        trace = palette.color(QPalette.ColorRole.Highlight)

        width = self.width() - 2 * MARGIN
        height = self.height() - 2 * MARGIN
        if width <= 0 or height <= 0:
            return

        painter.setPen(QPen(frame, 1))
        painter.drawRect(MARGIN, MARGIN, width - 1, height - 1)

        if not self._samples:
            painter.setPen(QPen(frame, 1))
            painter.drawText(self.rect(), Qt.AlignCenter, "waiting for samples")
            return

        low, high = self._bounds()
        span = high - low

        def y_for(value: float) -> float:
            fraction = (value - low) / span
            fraction = min(1.0, max(0.0, fraction))
            # Screen y grows downwards, so the top of the box is the high value.
            return MARGIN + (1.0 - fraction) * (height - 1)

        # A line at the middle of the range, so a flat trace still has something to sit
        # against and an eye can judge amplitude.
        painter.setPen(QPen(frame, 1, Qt.DotLine))
        middle = y_for(low + span / 2.0)
        painter.drawLine(QPointF(MARGIN, middle), QPointF(MARGIN + width - 1, middle))

        count = len(self._samples)
        step = (width - 1) / max(1, count - 1) if count > 1 else 0.0
        painter.setPen(QPen(trace, 1.5))

        if self.scrolling:
            polygon = QPolygonF(
                [QPointF(MARGIN + index * step, y_for(value)) for index, value in enumerate(self._samples)],
            )
            painter.drawPolyline(polygon)
        else:
            # A distribution is a set of readings across a domain, not a continuous signal,
            # so bars say what it is more honestly than a line joining the tops.
            base = y_for(low)
            bar = max(1.0, step * 0.6)
            for index, value in enumerate(self._samples):
                x = MARGIN + index * step
                painter.drawLine(QPointF(x, base), QPointF(x, y_for(value)))
                if bar > 2:  # noqa: PLR2004
                    painter.drawLine(QPointF(x + 1, base), QPointF(x + 1, y_for(value)))
        painter.end()
