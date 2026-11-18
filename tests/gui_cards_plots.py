"""Deterministic offscreen checks for metric cards and sample-array plots."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_test_support import Report, WindowFixture, application, pump, wait_for
from PySide6.QtGui import QColor, QImage, QPalette

from sdctoolbox.gui.widgets import plot as plot_module
from sdctoolbox.gui.widgets.controls import SampleArrayWidget
from sdctoolbox.gui.widgets.plot import SamplePlot
from sdctoolbox.model import MetricKind, MetricSpec


class PaintProbe(SamplePlot):
    """Keep Qt-dispatched Python paint failures inside the regression, not stderr."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.paint_errors = []
        self.resize(240, 76)
        palette = self.palette()
        palette.setColor(QPalette.Highlight, QColor(240, 0, 180))
        self.setPalette(palette)

    def paintEvent(self, event) -> None:  # noqa: N802
        try:
            super().paintEvent(event)
        except Exception as exc:
            # Do not retain the traceback's live QPainter past its paint device.
            self.paint_errors.append(f"{type(exc).__name__}: {exc}")


def painted(plot: PaintProbe, report: Report, description: str) -> QImage:
    image = QImage(plot.size(), QImage.Format_ARGB32)
    image.fill(QColor("white"))
    plot.render(image)
    report.check(not plot.paint_errors, f"{description} paints without Qt callback errors", str(plot.paint_errors))
    return image


def check_exact_plot_painting(report: Report) -> None:
    """Lossy pre-scaling or a fake flat fallback changes these real rendered pixels."""
    legacy = PaintProbe()
    try:
        legacy.add_samples([Decimal("0"), Decimal("2.5"), Decimal("5")])
        drawn = legacy.samples
        try:
            breaks = [abs(drawn[index] - drawn[index - 1] - 2.5) for index in range(1, len(drawn))]
        except TypeError as exc:
            report.check(False, "plot snapshots still support the existing float-based continuity reader", str(exc))
        else:
            report.check(breaks == [0.0, 0.0], "plot snapshots still support the existing float-based continuity reader")
    finally:
        legacy.clear()
        legacy.deleteLater()

    values = [Decimal("1000000000000000000000000000000"), Decimal("1000000000000000000000000000050"), Decimal("1000000000000000000000000000100")]
    small = [Decimal("0"), Decimal("50"), Decimal("100")]
    for scrolling in (True, False):
        name = "waveform" if scrolling else "distribution"
        for bounded_low, bounded_high in ((True, True), (True, False), (False, True), (False, False)):
            plots = [
                PaintProbe(scrolling=scrolling, minimum=block[0] if bounded_low else None, maximum=block[-1] if bounded_high else None)
                for block in (small, values)
            ]
            try:
                images = []
                for plot, block in zip(plots, (small, values)):
                    plot.add_samples(block)
                    plot.flush()
                    images.append(painted(plot, report, f"{name} bounds={bounded_low, bounded_high}"))
                pixels = {
                    (x, y) for y in range(images[0].height()) for x in range(images[0].width())
                    if (colour := images[0].pixelColor(x, y)).red() - colour.green() > 60
                    and colour.blue() - colour.green() > 40
                }
                tops = [min((y for x, y in pixels if abs(x - column) <= 2), default=76) for column in (3, 120, 236)]
                report.check(tops[0] - tops[1] > 20 and tops[1] - tops[2] > 20, f"the reference {name} has three distinct sample heights", str(tops))
                report.check(images[0] == images[1], f"offsetting {name} by 10^30 preserves actual painted geometry")
                report.check(list(plots[1]._samples) == values, f"the {name} keeps distinct source values")  # noqa: SLF001
            finally:
                for plot in plots:
                    plot.clear()
                    plot.deleteLater()

        for block, description in (
            ([values[0]] * 3, "a constant large-offset signal"),
            ([Decimal("1e400"), Decimal("1.5e400"), Decimal("2e400")], "finite values larger than floats"),
        ):
            plot = PaintProbe(scrolling=scrolling)
            reference = PaintProbe(scrolling=scrolling)
            try:
                plot.add_samples(block)
                reference.add_samples([Decimal("0")] * 3 if block[0] == block[-1] else small)
                plot.flush()
                reference.flush()
                image = painted(plot, report, f"{name}: {description}")
                expected = painted(reference, report, f"{name}: reference for {description}")
                report.check(image == expected, f"{name}: {description} retains its geometry")
            finally:
                for item in (plot, reference):
                    item.clear()
                    item.deleteLater()

        for invalid in ("NaN", "sNaN", "Infinity", "-Infinity", "not-a-number"):
            plot = PaintProbe(scrolling=scrolling)
            try:
                plot.add_samples(small)
                plot.flush()
                before = painted(plot, report, f"{name}: last good block")
                plot.add_samples([invalid, "50", "100"])
                plot.flush()
                after = painted(plot, report, f"{name}: rejected {invalid}")
                report.check(plot.samples == small and after == before, f"{name}: {invalid} preserves the whole last good trace")
            finally:
                plot.clear()
                plot.deleteLater()

        plot = PaintProbe(scrolling=scrolling, minimum=Decimal("0"), maximum=Decimal("Infinity"))
        reference = PaintProbe(scrolling=scrolling)
        try:
            for item in (plot, reference):
                item.add_samples(small)
                item.flush()
            report.check(
                painted(plot, report, f"{name}: a non-finite bound") == painted(reference, report, f"{name}: automatic bounds"),
                f"{name}: a non-finite descriptor bound does not flatten valid samples",
            )
        finally:
            for item in (plot, reference):
                item.clear()
                item.deleteLater()

    paced = PaintProbe(sample_period=Decimal("0.01"))
    try:
        with patch.object(plot_module.time, "monotonic", return_value=10.0) as clock:
            paced.add_samples(values)
            report.check(paced.samples == [] and list(paced._pending) == values, "pacing queues exact samples without revealing a block early")  # noqa: SLF001
            clock.return_value = 10.025
            paced._on_frame()  # noqa: SLF001
            report.check(list(paced._samples) == values[:2] and list(paced._pending) == values[2:], "pacing reveals only the samples due at the declared period")  # noqa: SLF001
            clock.return_value = 10.031
            paced._on_frame()  # noqa: SLF001
            report.check(list(paced._samples) == values, "fractional pacing credit carries into the next frame")  # noqa: SLF001
        paced.flush()
        report.check(list(paced._samples) == values and not paced._timer.isActive(), "flush preserves exact paced samples and stops its timer")  # noqa: SLF001
    finally:
        paced.clear()
        paced.deleteLater()

    plots = [PaintProbe(scrolling=False, minimum=block[0], maximum=block[-1]) for block in (small, values)]
    try:
        with patch.object(plot_module.time, "monotonic", return_value=10.0) as clock:
            for plot, block in zip(plots, (small, values)):
                plot.add_samples([block[0]] * 3)
                plot.flush()
                plot.add_samples(block)
            clock.return_value = 10.09
            for plot in plots:
                plot._on_frame()  # noqa: SLF001
            report.check(list(plots[1]._samples) != values and plots[1]._target == values, "large-offset distributions still tween towards the exact target")  # noqa: SLF001
            report.check(painted(plots[0], report, "small mid-tween") == painted(plots[1], report, "large mid-tween"), "the in-between tween retains offset-independent geometry")
            clock.return_value = 10.2
            for plot in plots:
                plot._on_frame()  # noqa: SLF001
            report.check(list(plots[1]._samples) == values and not plots[1]._timer.isActive(), "a completed tween settles on the exact target and stops")  # noqa: SLF001
    finally:
        for plot in plots:
            plot.clear()
            plot.deleteLater()


def check_delayed_plot_pacing(report: Report) -> None:
    values = [Decimal("1000000000000000000000000000000.1"), Decimal("1000000000000000000000000000000.2")]
    later = [Decimal("1000000000000000000000000000000.3")]
    for period in (Decimal("1e-307"), Decimal("1e-310")):
        plot = PaintProbe(sample_period=period)
        try:
            with patch.object(plot_module.time, "monotonic", return_value=100.0) as clock:
                plot.add_samples(values)
                try:
                    plot._on_frame()  # noqa: SLF001 - exercise backlog conversion before time advances
                    clock.return_value = 130.0
                    plot._on_frame()  # noqa: SLF001
                    plot._on_frame()  # noqa: SLF001 - an empty frame stops pacing
                    report.check(
                        list(plot._samples) == values and not plot._pending and not plot._timer.isActive(),  # noqa: SLF001
                        f"a delayed frame at period {period} drains exact received samples and stops",
                    )
                    clock.return_value = 160.0
                    plot.add_samples(later)
                    plot._on_frame()  # noqa: SLF001
                    clock.return_value = 190.0
                    plot._on_frame()  # noqa: SLF001
                    plot._on_frame()  # noqa: SLF001
                    report.check(
                        list(plot._samples) == values + later and not plot._pending  # noqa: SLF001
                        and not plot._timer.isActive() and plot._credit == 0.0,  # noqa: SLF001
                        f"a later block at period {period} retains exact continuity without poisoned credit",
                    )
                except (OverflowError, ValueError) as exc:
                    report.check(False, f"delayed pacing at period {period} stays bounded by received samples", f"{type(exc).__name__}: {exc}")
        finally:
            plot.clear()
            plot.deleteLater()

    for scrolling in (True, False):
        plot = PaintProbe(scrolling=scrolling, sample_period=Decimal("0.1"))
        try:
            plot.add_samples(values)
            pending, target = list(plot._pending), list(plot._target)  # noqa: SLF001
            plot.add_samples([])
            report.check(
                list(plot._pending) == pending and list(plot._target) == target,  # noqa: SLF001
                f"an empty {'waveform' if scrolling else 'distribution'} block retains in-flight samples",
            )
            plot.flush()
            plot.add_samples([])
            report.check(
                list(plot._samples) == values and not plot._timer.isActive(),  # noqa: SLF001
                f"an empty {'waveform' if scrolling else 'distribution'} block retains the settled trace",
            )
        finally:
            plot.clear()
            plot.deleteLater()


def check_fixed_time_window(report: Report) -> None:
    """A trace moves at the same width per second whatever its sample period."""
    speeds = {}
    for period, expected in ((Decimal("0.1"), 100), (Decimal("0.02"), 500)):
        plot = PaintProbe(sample_period=period)
        try:
            report.check(plot.window_samples == expected,
                         f"at {period} s per sample the plot keeps {plot_module.PLOT_SECONDS:g} s", str(plot.window_samples))
            plot.add_samples([Decimal(index % 7) for index in range(20)])
            plot.flush()
            xs = plot.trace_x(plot.width() - 2 * plot_module.MARGIN)
            report.check(xs[-1] == plot_module.MARGIN + plot.width() - 2 * plot_module.MARGIN - 1,
                         f"at {period} s per sample the newest sample sits at the right edge")
            speeds[period] = (xs[-1] - xs[-2]) / float(period)
        finally:
            plot.clear()
            plot.deleteLater()
    slow, fast = speeds.values()
    report.check(abs(slow - fast) < 1e-6, "the trace scrolls the same pixels per second at both periods", str(speeds))


def main() -> int:
    report = Report()
    app = application()
    print("GUI cards and plots (offscreen)")
    check_exact_plot_painting(report)
    check_delayed_plot_pacing(report)
    check_fixed_time_window(report)
    with WindowFixture("gui-cards-plots") as fixture:
        app = fixture.app
        service = fixture.service
        window = fixture.window
        assert window is not None
        pane = window.provider_pane

        waveform = service.add_metric(
            MetricSpec(
                label="Pleth",
                kind=MetricKind.WAVEFORM,
                minimum=Decimal(0),
                maximum=Decimal(100),
                sample_period=Decimal("0.02"),
            ),
        )
        distribution = service.add_metric(
            MetricSpec(
                label="Spectrum",
                kind=MetricKind.DISTRIBUTION,
                domain_unit_label="Hz",
                domain_minimum=Decimal(0),
                domain_maximum=Decimal(500),
            ),
        )
        pane.refresh()
        pump(app)

        waveform_card = pane.board.card(waveform)
        distribution_card = pane.board.card(distribution)
        report.check(
            waveform_card is not None and distribution_card is not None,
            "waveform and distribution metrics each get a card",
        )
        report.check(
            isinstance(waveform_card.control, SampleArrayWidget)
            and isinstance(distribution_card.control, SampleArrayWidget),
            "sample-array cards use plot controls",
        )
        report.check(
            waveform_card.control.plot.scrolling and not distribution_card.control.plot.scrolling,
            "waveforms scroll while distributions replace a frame",
        )
        report.check(
            wait_for(app, lambda: bool(waveform_card.control.plot.samples), timeout=5.0),
            "generated waveform samples reach the plot",
        )

        service.stop_generator()
        waveform_plot = waveform_card.control.plot
        waveform_plot.clear()
        service.set_samples(waveform, [Decimal(10), Decimal(20), Decimal(30)])
        pump(app)
        waveform_plot.flush()
        report.check(waveform_plot.samples == [10.0, 20.0, 30.0], "a waveform block is drawn once")
        before = list(waveform_plot.samples)
        pane.refresh()
        pump(app)
        waveform_plot.flush()
        report.check(waveform_plot.samples == before, "refreshing cards does not duplicate waveform samples")

        distribution_plot = distribution_card.control.plot
        service.set_samples(distribution, [Decimal(1)] * 32)
        pump(app)
        distribution_plot.flush()
        service.set_samples(distribution, [Decimal(7)] * 32)
        pump(app)
        distribution_plot.flush()
        report.check(
            distribution_plot.samples == [7.0] * 32,
            "a distribution frame replaces rather than appends",
            str(distribution_plot.samples),
        )

        distribution_plot.add_samples([9.0] * 32)
        tween_started = distribution_plot._tween_started  # noqa: SLF001
        distribution_plot.add_samples([9.0] * 32)
        report.check(
            distribution_plot._timer.isActive()  # noqa: SLF001
            and distribution_plot._tween_started == tween_started,  # noqa: SLF001
            "repeating an active distribution target does not restart its timer",
        )
        report.check(
            wait_for(
                app,
                lambda: distribution_plot.samples == [9.0] * 32
                and not distribution_plot._timer.isActive(),  # noqa: SLF001
                timeout=0.5,
            ),
            "the unchanged distribution target still settles",
        )
        distribution_plot.add_samples([9.0] * 32)
        report.check(
            not distribution_plot._timer.isActive(),  # noqa: SLF001
            "repeating a settled distribution target leaves its timer stopped",
        )

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
