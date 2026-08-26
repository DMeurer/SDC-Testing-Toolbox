"""Tests for the metric widgets.

Two halves. The first checks the factory picks the right control for a given metric, which
is pure logic and needs nothing running. The second drives the real controls inside the real
window and checks a click reaches the device.

Exit code 0 means all checks passed.

Usage:  .venv/Scripts/python.exe tests/widget_controls.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from sdc11073.loghelper import basic_logging_setup  # noqa: E402

from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.gui.widgets import WidgetSpec, build_widget  # noqa: E402
from sdctoolbox.gui.widgets.controls import (  # noqa: E402
    MAX_SLIDER_STEPS,
    ChoiceWidget,
    ReadoutWidget,
    SliderWidget,
    StepperWidget,
    TextWidget,
)
from sdctoolbox.gui.widgets.factory import CONTROLS, pick_control  # noqa: E402
from sdctoolbox.model import MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402


class Report:
    """Collects pass/fail results and prints them as they happen."""

    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, description: str, detail: str = "") -> bool:  # noqa: FBT001
        self.checks += 1
        if not ok:
            self.failures += 1
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {description}{suffix}", flush=True)
        return ok

    def summary(self) -> int:
        print("-" * 74)
        if self.failures:
            print(f"RESULT: {self.failures} of {self.checks} checks FAILED")
            return 1
        print(f"RESULT: all {self.checks} checks passed")
        return 0


def pump(app: QApplication, seconds: float = 0.3) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def number(**kwargs) -> WidgetSpec:  # noqa: ANN003
    return WidgetSpec(handle="m.n", label="N", kind=MetricKind.NUMBER, **kwargs)


#: (description, spec, expected control)
CHOICES = [
    (
        "a choice with allowed values gets a dropdown",
        WidgetSpec("m.c", "Mode", MetricKind.CHOICE, allowed_values=("A", "B")),
        ChoiceWidget,
    ),
    (
        "a choice with no allowed values falls back",
        WidgetSpec("m.c", "Mode", MetricKind.CHOICE),
        ReadoutWidget,
    ),
    (
        "a number with both limits gets a slider",
        number(minimum=Decimal("1"), maximum=Decimal("100"), resolution=Decimal("1")),
        SliderWidget,
    ),
    (
        "a fractional resolution still gets a slider",
        number(minimum=Decimal("0"), maximum=Decimal("40"), resolution=Decimal("0.1")),
        SliderWidget,
    ),
    (
        "a number with only a minimum gets a stepper",
        number(minimum=Decimal("0")),
        StepperWidget,
    ),
    (
        "a number with only a maximum gets a stepper",
        number(maximum=Decimal("50")),
        StepperWidget,
    ),
    (
        "an unbounded number gets a stepper",
        number(),
        StepperWidget,
    ),
    (
        "a range needing too many slider steps gets a stepper",
        number(minimum=Decimal("0"), maximum=Decimal("1000000"), resolution=Decimal("0.001")),
        StepperWidget,
    ),
    (
        "an inverted range gets a stepper rather than a broken slider",
        number(minimum=Decimal("100"), maximum=Decimal("1")),
        StepperWidget,
    ),
    (
        "a zero-width range gets a stepper",
        number(minimum=Decimal("5"), maximum=Decimal("5")),
        StepperWidget,
    ),
    (
        "text gets a field",
        WidgetSpec("m.t", "Note", MetricKind.TEXT),
        TextWidget,
    ),
    (
        "a waveform falls back to a read-out",
        WidgetSpec("m.w", "ECG", MetricKind.WAVEFORM),
        ReadoutWidget,
    ),
    (
        "a distribution falls back to a read-out",
        WidgetSpec("m.d", "Dist", MetricKind.DISTRIBUTION),
        ReadoutWidget,
    ),
    (
        "an unknown kind still gets something",
        WidgetSpec("m.x", "Mystery", None),
        ReadoutWidget,
    ),
]


def main() -> int:  # noqa: PLR0915 - a linear test reads better in one piece
    basic_logging_setup(level=logging.WARNING)
    report = Report()

    print("=" * 74)
    print("Metric widgets")
    print("=" * 74)

    app = QApplication(sys.argv)

    print("\n1. Which control represents which metric")
    for description, spec, expected in CHOICES:
        got = pick_control(spec)
        report.check(got is expected, description, got.__name__)

    report.check(
        CONTROLS[-1] is ReadoutWidget,
        "the catch-all is asked last, so nothing is ever dropped",
        CONTROLS[-1].__name__,
    )
    report.check(
        [c.priority for c in CONTROLS] == sorted(c.priority for c in CONTROLS),
        "the registry is in priority order",
    )

    print("\n2. Controls in isolation")
    slider = build_widget(
        number(minimum=Decimal("1"), maximum=Decimal("100"), resolution=Decimal("1"), editable=True),
    )
    sent: list[tuple[str, object]] = []
    slider.value_requested.connect(lambda h, v: sent.append((h, v)))

    slider.show_value(Decimal("50"))
    report.check(slider.readout.text() == "50", "slider shows the value it is given", slider.readout.text())
    report.check(not sent, "and displaying a value does not request one")

    slider.slider.setValue(slider._value_to_position(Decimal("80")))  # noqa: SLF001
    slider._on_released()  # noqa: SLF001
    report.check(sent == [("m.n", Decimal("80"))], "releasing the slider requests that value", str(sent))

    fine = build_widget(number(minimum=Decimal("0"), maximum=Decimal("40"), resolution=Decimal("0.1")))
    fine.show_value(Decimal("12.5"))
    report.check(fine.readout.text() == "12.5", "a fractional value survives the slider", fine.readout.text())

    stepper = build_widget(number(editable=True))
    sent.clear()
    stepper.value_requested.connect(lambda h, v: sent.append((h, v)))
    stepper.show_value(Decimal("7"))
    report.check(
        [b.text() for b in stepper.buttons] == ["-10", "-1", "+1", "+10"],
        "the stepper has the four buttons",
        str([b.text() for b in stepper.buttons]),
    )
    stepper.buttons[3].click()
    stepper.buttons[1].click()
    report.check(
        sent == [("m.n", Decimal("17")), ("m.n", Decimal("16"))],
        "+10 then -1 from 7 gives 17 then 16",
        str(sent),
    )

    bounded_stepper = build_widget(number(minimum=Decimal("0"), editable=True))
    sent.clear()
    bounded_stepper.value_requested.connect(lambda h, v: sent.append((h, v)))
    bounded_stepper.show_value(Decimal("3"))
    bounded_stepper.buttons[0].click()  # -10 from 3, with a floor of 0
    report.check(
        sent == [("m.n", Decimal("0"))],
        "the stepper respects a one-sided limit",
        str(sent),
    )

    choice = build_widget(
        WidgetSpec("m.c", "Mode", MetricKind.CHOICE, allowed_values=("IDLE", "RUN"), editable=True),
    )
    sent.clear()
    choice.value_requested.connect(lambda h, v: sent.append((h, v)))
    choice.show_value("RUN")
    report.check(choice.box.currentText() == "RUN", "the dropdown shows the current value")
    report.check(not sent, "and selecting it programmatically requests nothing")
    choice._on_activated(0)  # noqa: SLF001
    report.check(sent == [("m.c", "IDLE")], "picking one requests it", str(sent))

    read_only = build_widget(number(minimum=Decimal("1"), maximum=Decimal("10")))
    report.check(not read_only.slider.isEnabled(), "a metric we may not write is disabled")

    print("\n3. In the window")
    service = ProviderService(instance_name="widget-test")
    service.start()
    try:
        window = MainWindow(service)
        window.show()
        pump(app)
        pane = window.provider_pane

        report.check(window.widgets_enabled, "widgets are on by default")
        report.check(pane.use_widgets, "and the pane agrees")

        zoom = service.add_metric(
            MetricSpec(
                label="Zoom level",
                kind=MetricKind.NUMBER,
                unit_label="steps",
                minimum=Decimal("1"),
                maximum=Decimal("100"),
                controllable=True,
                initial_value=Decimal("50"),
            ),
        )
        free = service.add_metric(
            MetricSpec(label="Free number", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal("7")),
        )
        mode = service.add_metric(
            MetricSpec(
                label="Mode",
                kind=MetricKind.CHOICE,
                allowed_values=("IDLE", "RUN", "PAUSE"),
                controllable=True,
                initial_value="RUN",
            ),
        )
        note = service.add_metric(MetricSpec(label="Note", kind=MetricKind.TEXT, controllable=True))
        pane.refresh()
        pump(app)

        report.check(sorted(pane.board.handles) == sorted([zoom, free, mode, note]), "a card per metric")
        for handle, expected in ((zoom, SliderWidget), (free, StepperWidget), (mode, ChoiceWidget), (note, TextWidget)):
            card = pane.board.card(handle)
            report.check(
                card is not None and isinstance(card.control, expected),
                f"{handle} uses {expected.__name__}",
                type(card.control).__name__ if card else "no card",
            )

        report.check(
            pane.board.card(zoom).control.readout.text() == "50",
            "the card shows the current value",
        )

        pane.board.card(free).control.buttons[3].click()
        pump(app)
        report.check(service.get_value(free) == Decimal("17"), "+10 reaches the device", str(service.get_value(free)))

        pane.board.card(mode).control._on_activated(2)  # noqa: SLF001
        pump(app)
        report.check(service.get_value(mode) == "PAUSE", "the dropdown reaches the device", str(service.get_value(mode)))

        slider_card = pane.board.card(zoom).control
        slider_card.slider.setValue(slider_card._value_to_position(Decimal("80")))  # noqa: SLF001
        slider_card._on_released()  # noqa: SLF001
        pump(app)
        report.check(service.get_value(zoom) == Decimal("80"), "the slider reaches the device", str(service.get_value(zoom)))

        service.set_value(zoom, Decimal("30"))
        pump(app)
        report.check(
            pane.board.card(zoom).control.readout.text() == "30",
            "a change made elsewhere shows up on the card",
            pane.board.card(zoom).control.readout.text(),
        )

        print("\n4. Out-of-range writes from a control")
        pane.board.card(zoom).control.slider.setValue(0)
        service.set_value(zoom, Decimal("1"))
        pump(app)
        report.check(service.get_value(zoom) == Decimal("1"), "the boundary itself is fine")

        print("\n5. Switching views")
        window.set_use_widgets(False)
        pump(app)
        report.check(not pane.use_widgets, "unchecking shows the table")
        report.check(pane.table.rowCount() == 4, "the table has every metric", str(pane.table.rowCount()))  # noqa: PLR2004
        report.check(not window.network_pane.use_widgets, "both panes switch together")

        window.set_use_widgets(True)
        pump(app)
        report.check(pane.use_widgets, "checking brings the controls back")
        report.check(len(pane.board.handles) == 4, "with every metric still there", str(len(pane.board.handles)))  # noqa: PLR2004

        print("\n6. The board keeps up with the device")
        late = service.add_metric(
            MetricSpec(label="Late arrival", kind=MetricKind.CHOICE, allowed_values=("ON", "OFF"), controllable=True),
        )
        pump(app, seconds=0.8)
        report.check(late in pane.board.handles, "a metric added at runtime gets a card")

        service.remove_metric(late)
        pump(app, seconds=0.8)
        report.check(late not in pane.board.handles, "and loses it when removed")

        window.network_pane.shutdown()
        window.close()
    finally:
        service.stop()

    print()
    print(f"(slider declines above {MAX_SLIDER_STEPS} steps)")
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
