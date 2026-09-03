"""Deterministic offscreen checks for metric cards and sample-array plots."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_test_support import Report, WindowFixture, pump, wait_for

from sdctoolbox.gui.widgets.controls import SampleArrayWidget
from sdctoolbox.model import MetricKind, MetricSpec


def main() -> int:
    report = Report()
    print("GUI cards and plots (offscreen)")
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
