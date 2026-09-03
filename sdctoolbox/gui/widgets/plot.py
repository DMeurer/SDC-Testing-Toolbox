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

import math
import time
from collections import deque
from decimal import Decimal

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import QWidget

from ..styling import muted_colour

# How many samples a scrolling plot keeps. At the default 0.1s period that is half a minute
# of history, which is enough to see a shape without making the repaint expensive.
HISTORY = 300

PLOT_HEIGHT = 76

# Padding inside the frame, so the trace never touches the border.
MARGIN = 3

# How often a paced plot moves samples out of the buffer and repaints. Thirty a second is
# smooth to the eye and cheap enough to run on every card at once.
FRAME_MS = 33

# How far behind real time the trace is allowed to fall before it starts catching up, in
# seconds of signal. A display that drifts further behind with every block is worse than a
# chunky one, so a backlog past this is drained faster than real time until it is gone.
MAX_BACKLOG_SECONDS = 1.5

# How long a distribution takes to move from one set of bar heights to the next. A whole
# block is replaced at once, so without this the bars jump. Deliberately shorter than the
# interval blocks arrive at, so the move finishes and settles rather than being permanently
# chased by the next one.
TWEEN_MS = 180


class SamplePlot(QWidget):
    """Draws a list of samples, either as a scrolling trace or as a static bar chart.

    A waveform arrives in blocks - half a second of signal, twice a second - and drawing a
    block the moment it lands makes the trace lurch rather than move. So a scrolling plot
    buffers what it is given and reveals it at the rate the samples were taken at, which is
    what SamplePeriod on the descriptor is for: the standard tells a consumer how to place
    samples in time, and this is a consumer doing that.

    Nothing is invented. When the buffer runs dry the trace simply stops until the next
    block, which is honest about a device that has gone quiet. When the buffer runs long -
    a hiccup, or a peer sending faster than declared - it drains faster than real time, so
    the display cannot drift further and further behind what the device is actually doing.

    A distribution has no time base to pace against - it is one picture of a domain, and the
    whole picture is replaced at once - so it eases from the old bar heights to the new ones
    instead. Same purpose, different mechanism: a value that moves is readable where one that
    jumps is not.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        scrolling: bool = True,
        minimum: Decimal | None = None,
        maximum: Decimal | None = None,
        sample_period: Decimal | None = None,
    ) -> None:
        super().__init__(parent)
        self.scrolling = scrolling
        self.setMinimumHeight(PLOT_HEIGHT)
        self.setAutoFillBackground(False)

        self._fixed_low = float(minimum) if minimum is not None else None
        self._fixed_high = float(maximum) if maximum is not None else None
        self._samples: deque[float] = deque(maxlen=HISTORY if scrolling else None)

        # Seconds between two samples, from the descriptor. Without it there is no way to
        # know how fast to reveal, so a plot that has none shows blocks as they arrive.
        self._sample_period = float(sample_period) if sample_period else 0.0
        self._pending: deque[float] = deque()
        # Fractional samples carried between frames, so a rate that does not divide evenly
        # into the frame interval does not round down to nothing every time.
        self._credit = 0.0
        # When the last frame ran. Timers fire late under load, and assuming every tick is
        # exactly FRAME_MS would make the trace run slow by however much they slipped.
        self._last_tick = 0.0

        # Where a distribution's bars are heading, where they set off from, and when.
        self._target: list[float] = []
        self._tween_from: list[float] = []
        self._tween_started = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._on_frame)

    # -- content -------------------------------------------------------------------

    @property
    def samples(self) -> list[float]:
        """What is currently drawn. Mid-move for a distribution, so not always the block."""
        return list(self._samples)

    @property
    def pending(self) -> list[float]:
        """Received but not yet revealed. Empty unless the plot is pacing."""
        return list(self._pending)

    @property
    def target(self) -> list[float]:
        """The block a distribution's bars are moving towards."""
        return list(self._target)

    @property
    def paced(self) -> bool:
        """Whether this plot reveals samples gradually over time."""
        return self.scrolling and self._sample_period > 0

    @property
    def tweening(self) -> bool:
        """Whether this plot eases between blocks rather than replacing them."""
        return not self.scrolling

    def add_samples(self, samples) -> None:  # noqa: ANN001 - any sequence of numbers
        """Take a block of samples.

        A waveform appends, because each block continues the last. A distribution moves
        towards the new block, because each one is a whole picture of the same domain.
        """
        try:
            values = [float(sample) for sample in samples]
        except (TypeError, ValueError):
            # A peer may publish something that is not a number at all. Showing the last
            # good trace beats blanking the card.
            return
        if not values:
            return

        if self.tweening:
            self._retarget(values)
            return

        if not self.paced:
            self._samples.extend(values)
            self.update()
            return

        self._pending.extend(values)
        if not self._timer.isActive():
            self._credit = 0.0
            self._last_tick = time.monotonic()
            self._timer.start()

    def _retarget(self, values: list[float]) -> None:
        """Aim a distribution's bars at a new block, starting from wherever they are now.

        Re-aiming mid-move rather than queueing: the newest block is the truth, and a
        distribution that worked through a backlog of stale pictures would be showing the
        wrong one on purpose.
        """
        # Compare with the destination, not the currently displayed in-between frame. The
        # same target during a tween must let that tween finish rather than restart it.
        if values == self._target:
            return
        self._target = values
        current = list(self._samples)
        if len(current) != len(values):
            # A different number of bins is not something to interpolate through. The very
            # first block grows up from the floor, which reads as the card filling in;
            # anything else simply takes effect.
            low, _ = self._bounds()
            current = [low] * len(values) if not current else list(values)
        self._tween_from = current
        self._tween_started = time.monotonic()
        if not self._timer.isActive():
            self._timer.start()
        self._advance_tween()

    def flush(self) -> None:
        """Show the newest block at once, abandoning the animation.

        For when there is nobody to watch it: a hidden card, or a test that wants to assert
        on a block rather than wait for it to arrive.
        """
        self._timer.stop()
        self._credit = 0.0
        changed = False
        if self._pending:
            self._samples.extend(self._pending)
            self._pending.clear()
            changed = True
        if self.tweening and self._target and list(self._samples) != self._target:
            self._samples.clear()
            self._samples.extend(self._target)
            self._tween_from = list(self._target)
            changed = True
        if changed:
            self.update()

    def clear(self) -> None:
        """Forget every sample, drawn or waiting."""
        self._timer.stop()
        self._samples.clear()
        self._pending.clear()
        self._target = []
        self._tween_from = []
        self._credit = 0.0
        self.update()

    def _on_frame(self) -> None:
        """One animation frame, for whichever kind of movement this plot does."""
        if self.tweening:
            self._advance_tween()
        else:
            self._reveal()

    def _advance_tween(self) -> None:
        """Move a distribution's bars a frame's worth towards the newest block."""
        if not self._target or len(self._tween_from) != len(self._target):
            self._timer.stop()
            return

        elapsed = (time.monotonic() - self._tween_started) * 1000.0
        progress = 1.0 if elapsed >= TWEEN_MS else elapsed / TWEEN_MS
        # Ease in and out, so the bars set off and arrive gently instead of sliding at a
        # constant speed and stopping dead.
        eased = (1.0 - math.cos(progress * math.pi)) / 2.0

        self._samples.clear()
        self._samples.extend(
            start + (end - start) * eased
            for start, end in zip(self._tween_from, self._target)
        )
        if progress >= 1.0:
            self._tween_from = list(self._target)
            self._timer.stop()
        self.update()

    def _reveal(self) -> None:
        """Move however many samples belong to one frame's worth of time."""
        if not self._pending:
            self._timer.stop()
            self._credit = 0.0
            return

        now = time.monotonic()
        elapsed = now - self._last_tick
        self._last_tick = now
        self._credit += elapsed / self._sample_period
        due = int(self._credit)
        self._credit -= due

        # If the buffer has run long, take the excess as well. Otherwise a peer sending a
        # little faster than it declared would push the trace further behind every second
        # until it was showing minutes-old data.
        backlog_limit = int(MAX_BACKLOG_SECONDS / self._sample_period)
        excess = len(self._pending) - backlog_limit
        if excess > 0:
            due += excess

        if due <= 0:
            return
        for _ in range(min(due, len(self._pending))):
            self._samples.append(self._pending.popleft())
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
