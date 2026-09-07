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
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from script_support import Report, wait_until  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QSizePolicy  # noqa: E402
from sdc11073.loghelper import basic_logging_setup  # noqa: E402

from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.gui.new_metric_dialog import (  # noqa: E402
    OFFERED_DISTRIBUTIONS,
    OFFERED_SHAPES,
    NewMetricDialog,
)
from sdctoolbox.gui.widgets import (  # noqa: E402
    WidgetSpec,
    build_widget,
    from_remote_metric,
)
from sdctoolbox.gui.widgets.controls import (  # noqa: E402
    MAX_SLIDER_STEPS,
    STEP_SMALL,
    ChoiceWidget,
    ReadoutWidget,
    SampleArrayWidget,
    SliderWidget,
    StepperWidget,
    TextWidget,
)
from sdctoolbox.gui.widgets.factory import CONTROLS, pick_control  # noqa: E402
from sdctoolbox.model import (  # noqa: E402
    ActionSpec,
    AlertSpec,
    DistributionShape,
    MetricKind,
    MetricSpec,
    RemoteMetric,
    RemoteRange,
    WaveformShape,
)
from sdctoolbox.provider_service import ProviderService  # noqa: E402


def pump(app: QApplication, seconds: float = 0.3) -> None:
    wait_until(
        lambda: False,
        timeout=seconds,
        interval=0.02,
        pump=app.processEvents,
        check_boundary=False,
    )


def number(**kwargs) -> WidgetSpec:  # noqa: ANN003
    return WidgetSpec(handle="m.n", label="N", kind=MetricKind.NUMBER, **kwargs)


# (description, spec, expected control)
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
        "multiple allowed ranges get a stepper rather than a misleading slider",
        number(
            minimum=Decimal("0"),
            maximum=Decimal("10"),
            allowed_ranges=(
                RemoteRange(Decimal("0"), Decimal("10"), Decimal("2")),
                RemoteRange(Decimal("20"), Decimal("30"), Decimal("5")),
            ),
        ),
        StepperWidget,
    ),
    (
        "text gets a field",
        WidgetSpec("m.t", "Note", MetricKind.TEXT),
        TextWidget,
    ),
    (
        "a waveform gets a plot",
        WidgetSpec("m.w", "ECG", MetricKind.WAVEFORM),
        SampleArrayWidget,
    ),
    (
        "a distribution gets a plot too",
        WidgetSpec("m.d", "Dist", MetricKind.DISTRIBUTION),
        SampleArrayWidget,
    ),
    (
        "a kind this build has never heard of still gets a card",
        WidgetSpec("m.x", "Mystery", None),
        ReadoutWidget,
    ),
    (
        "an unknown kind still gets something",
        WidgetSpec("m.x", "Mystery", None),
        ReadoutWidget,
    ),
]


def check_shapes_are_reachable(report: Report) -> None:
    """Every shape the model defines has to be creatable from the window.

    The dialog fell behind the model once already: five physiological curves and all five
    distribution shapes existed and were reachable only from a preset, because OFFERED_SHAPES
    was written before they were and nobody went back. This fails the moment that happens
    again.
    """
    print("\nEvery shape is reachable from the dialog")
    dialog = NewMetricDialog()

    offered_waveforms = {shape for _, shape in OFFERED_SHAPES}
    missing = sorted(s.value for s in set(WaveformShape) - offered_waveforms)
    report.check(not missing, "the dialog offers every waveform shape", str(missing))

    offered_distributions = {shape for _, shape in OFFERED_DISTRIBUTIONS}
    missing = sorted(s.value for s in set(DistributionShape) - offered_distributions)
    report.check(not missing, "and every distribution shape", str(missing))

    # A separator sits between the geometric and physiological groups and takes an index
    # of its own, so reading the combo's index straight off the list would be wrong for
    # everything past it.
    kinds = [dialog.kind_box.itemText(i) for i in range(dialog.kind_box.count())]
    dialog.kind_box.setCurrentIndex(kinds.index("Waveform"))
    dialog._on_kind_changed()  # noqa: SLF001
    reached = set()
    for index in range(dialog.shape_box.count()):
        if not dialog.shape_box.itemText(index):
            continue
        dialog.shape_box.setCurrentIndex(index)
        reached.add(dialog._shape)  # noqa: SLF001
    report.check(
        reached == set(WaveformShape),
        "and picking each one yields it, separator and all",
        str(sorted(s.value for s in set(WaveformShape) - reached)),
    )
    dialog.deleteLater()


def main() -> int:  # noqa: PLR0915 - a linear test reads better in one piece
    basic_logging_setup(level=logging.WARNING)
    report = Report()

    print("=" * 74)
    print("Metric widgets")
    print("=" * 74)

    app = QApplication(sys.argv)

    # Modal dialogs would block with nobody to answer them.
    asked: list[tuple[str, str]] = []

    def fake_question(_parent, title, text, *_args, **_kwargs):  # noqa: ANN001, ANN202
        asked.append((title, text))
        return QMessageBox.StandardButton.Yes

    QMessageBox.question = staticmethod(fake_question)
    QMessageBox.warning = staticmethod(lambda *_a, **_k: QMessageBox.StandardButton.Ok)

    check_shapes_are_reachable(report)

    print("\n1. Which control represents which metric")
    for description, spec, expected in CHOICES:
        got = pick_control(spec)
        report.check(got is expected, description, got.__name__)

    report.check(
        CONTROLS[-1] is ReadoutWidget,
        "the catch-all is asked last, so nothing is ever dropped",
        CONTROLS[-1].__name__,
    )
    unknown = WidgetSpec("m.unknown", "Unknown", None)
    report.check(
        [control for control in CONTROLS if control.matches(unknown)] == [ReadoutWidget],
        "exactly one universal fallback exists and it is the final registry entry",
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

    report.check(
        slider.readout.textFormat() == Qt.PlainText
        and slider.readout.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
        and all(
            label.textFormat() == Qt.PlainText and label.maximumWidth() <= 90
            for label in (slider.low_label, slider.high_label)
        ),
        "slider values and range endpoints use bounded plain-text labels",
    )

    print("\nRemote fractional sliders")
    for resolution, expected_values in (
        (Decimal("0.1"), tuple(Decimal(index) / Decimal("10") for index in range(11))),
        (
            Decimal("0.3"),
            (Decimal("0"), Decimal("0.3"), Decimal("0.6"), Decimal("0.9"), Decimal("1")),
        ),
    ):
        remote = RemoteMetric(
            handle=f"remote.{resolution}",
            node_type_name="NumericMetricDescriptor",
            kind=MetricKind.NUMBER,
            minimum=Decimal("0"),
            maximum=Decimal("1"),
            resolution=resolution,
            operation_handles=("operation",),
            selected_operation_handle="operation",
            controllable_now=True,
        )
        spec = from_remote_metric(remote)
        control = build_widget(spec)
        values = tuple(  # noqa: SLF001
            control._position_to_value(position) for position in range(control.slider.maximum() + 1)
        )
        report.check(spec.resolution == resolution, f"remote resolution {resolution} reaches the widget exactly")
        report.check(values == expected_values, f"resolution {resolution} exposes truthful positions", str(values))

        emitted = []
        control.value_requested.connect(lambda _h, value, values=emitted: values.append(value))
        for endpoint, position in ((Decimal("0"), 0), (Decimal("1"), control.slider.maximum())):
            control.show_value(endpoint)
            report.check(
                control.slider.value() == position and control.readout.text() == str(endpoint),
                f"resolution {resolution} displays endpoint {endpoint} at its endpoint position",
                f"position={control.slider.value()}, readout={control.readout.text()}",
            )
            control._on_released()  # noqa: SLF001
            report.check(emitted[-1] == endpoint, f"releasing endpoint {endpoint} emits it exactly")

        keyboard_values = []
        control.value_requested.connect(lambda _h, value, values=keyboard_values: values.append(value))
        control.show()
        control.slider.setFocus()
        pump(app, seconds=0.1)
        control.show_value(expected_values[-2])
        QTest.keyClick(control.slider, Qt.Key_Right)
        QTest.keyClick(control.slider, Qt.Key_Left)
        report.check(
            keyboard_values == [expected_values[-1], expected_values[-2]],
            f"resolution {resolution} keyboard steps emit exact final-position values",
            str(keyboard_values),
        )
        report.check(
            control.slider.value() == control.slider.maximum() - 1
            and control.readout.text() == str(expected_values[-2]),
            f"resolution {resolution} keyboard position and readout stay synchronized",
            f"position={control.slider.value()}, readout={control.readout.text()}",
        )
        control.hide()

    print("\nRemote choice domains")
    for description, descriptor_values, operation_values, expected_values in (
        (
            "an operation can narrow the descriptor choices",
            ("IDLE", "RUN", "PAUSE"),
            ("IDLE", "RUN"),
            ("IDLE", "RUN"),
        ),
        (
            "an operation-only value is offered even when the descriptor differs",
            ("descriptor-a", "descriptor-b"),
            ("operation-a", "operation-b"),
            ("operation-a", "operation-b"),
        ),
    ):
        remote = RemoteMetric(
            handle="remote.choice",
            node_type_name="EnumStringMetricDescriptor",
            kind=MetricKind.CHOICE,
            allowed_values=descriptor_values,
            operation_allowed_values=operation_values,
            operation_handles=("operation",),
            selected_operation_handle="operation",
            controllable_now=True,
        )
        spec = from_remote_metric(remote)
        control = build_widget(spec)
        offered = tuple(control.box.itemText(index) for index in range(control.box.count()))
        report.check(
            spec.allowed_values == expected_values and offered == expected_values,
            description,
            f"spec={spec.allowed_values}, offered={offered}",
        )
        report.check(
            remote.allowed_values == descriptor_values,
            f"{description}, without replacing descriptor metadata",
            str(remote.allowed_values),
        )

    read_only_remote = RemoteMetric(
        handle="remote.read-only-choice",
        node_type_name="EnumStringMetricDescriptor",
        kind=MetricKind.CHOICE,
        allowed_values=("STANDBY", "ACTIVE"),
        value="ACTIVE",
    )
    read_only_choice = build_widget(from_remote_metric(read_only_remote))
    read_only_choice.show_value(read_only_remote.value)
    report.check(
        isinstance(read_only_choice, ChoiceWidget)
        and tuple(
            read_only_choice.box.itemText(index)
            for index in range(read_only_choice.box.count())
        )
        == read_only_remote.allowed_values
        and read_only_choice.box.currentText() == "ACTIVE"
        and not read_only_choice.box.isEnabled(),
        "a read-only metric still displays its descriptor choices",
    )

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

    print("\nFinite numeric entry")
    for invalid in ("NaN", "sNaN", "Infinity", "-Infinity"):
        for action, invoke in (
            ("typed", bounded_stepper._on_typed),  # noqa: SLF001
            ("stepped", lambda: bounded_stepper._step(STEP_SMALL)),  # noqa: SLF001
        ):
            sent.clear()
            bounded_stepper.edit.setText(invalid)
            invoke()
            message = bounded_stepper.error_label.text()
            report.check(
                not sent and "value" in message and "finite number" in message,
                f"the numeric widget rejects {invalid} when {action}",
                message,
            )
    report.check(
        bounded_stepper.error_label.textFormat() == Qt.PlainText
        and bounded_stepper.error_label.sizePolicy().horizontalPolicy()
        == QSizePolicy.Ignored,
        "numeric errors use a bounded plain-text label",
    )

    print("\nAllowedRange numeric domains")
    ranged = build_widget(
        number(
            editable=True,
            allowed_ranges=(
                RemoteRange(Decimal("0"), Decimal("10"), Decimal("2")),
                RemoteRange(Decimal("20"), Decimal("30"), Decimal("5")),
            ),
        ),
    )
    ranged_values = []
    ranged.value_requested.connect(lambda _h, value: ranged_values.append(value))
    for text, accepted, description in (
        ("25", True, "a value in the second allowed range is accepted"),
        ("15", False, "a value in the gap between ranges is rejected"),
        ("3", False, "a value off the range StepWidth is rejected"),
    ):
        ranged.edit.setText(text)
        ranged._on_typed()  # noqa: SLF001
        report.check(
            (ranged_values[-1:] == [Decimal(text)]) == accepted,
            description,
            ranged.error_label.text(),
        )
        if not accepted:
            report.check(ranged_values == [Decimal("25")], f"{description} without emitting a write")

    for spec, text, accepted, description in (
        (
            number(allowed_ranges=(RemoteRange(None, Decimal("0"), Decimal("0.5")),)),
            "-0.3",
            False,
            "an upper-only range rejects values off the Upper-based StepWidth",
        ),
        (
            number(allowed_ranges=(RemoteRange(None, Decimal("0"), Decimal("0.5")),)),
            "-0.5",
            True,
            "an upper-only range accepts values aligned to its Upper-based StepWidth",
        ),
        (
            number(allowed_ranges=(RemoteRange(Decimal("10"), None, Decimal("2")),)),
            "14",
            True,
            "a lower-only range accepts aligned values above its open end",
        ),
        (
            number(allowed_ranges=(RemoteRange(Decimal("10"), None, Decimal("2")),)),
            "15",
            False,
            "a lower-only range still enforces StepWidth",
        ),
        (
            number(),
            "123.456",
            True,
            "no allowed ranges leaves numeric input unrestricted",
        ),
        (
            number(allowed_ranges=(RemoteRange(None, None, Decimal("0.5")),)),
            "123.456",
            True,
            "an unbounded range cannot apply StepWidth without an anchor",
        ),
    ):
        control = build_widget(spec)
        values = []
        control.value_requested.connect(lambda _h, value, target=values: target.append(value))
        control.edit.setText(text)
        control._on_typed()  # noqa: SLF001
        report.check(bool(values) == accepted, description, control.error_label.text())

    stepped_slider = build_widget(
        number(
            allowed_ranges=(
                RemoteRange(Decimal("0"), Decimal("1"), Decimal("0.3")),
            ),
        ),
    )
    stepped_values = tuple(
        stepped_slider._position_to_value(position)  # noqa: SLF001
        for position in range(stepped_slider.slider.maximum() + 1)
    )
    report.check(
        stepped_values
        == (Decimal("0"), Decimal("0.3"), Decimal("0.6"), Decimal("0.9"))
        and stepped_slider.high_label.text() == "0.9",
        "a single stepped range slider exposes only aligned values",
        str(stepped_values),
    )

    hostile_value = "<img src=not-found width=10000 height=10000>"
    fallback = build_widget(WidgetSpec("m.hostile", "Hostile", None))
    fallback.show_value(hostile_value)
    report.check(
        fallback.readout.textFormat() == Qt.PlainText
        and fallback.readout.text() == hostile_value
        and fallback.readout.sizePolicy().horizontalPolicy() == QSizePolicy.Ignored
        and fallback.minimumSizeHint().width() < 260,
        "fallback values render literally without expanding their control",
        f"minimum={fallback.minimumSizeHint().width()}",
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

        print("\n8. Deleting from a card")
        # There is no row to select on the board, so the Remove button cannot work there.
        window.set_use_widgets(True)
        pump(app)
        report.check(
            not pane.remove_button.isVisible(),
            "the Remove button is hidden in widget mode",
        )

        doomed = service.add_metric(
            MetricSpec(label="Doomed", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal("1")),
        )
        service.add_alert(
            AlertSpec(label="Doomed too high", source_handle=doomed, upper_limit=Decimal("5")),
        )
        doomed_action = service.add_action(ActionSpec(label="Reset doomed", target_handle=doomed))
        pane.refresh()
        pane.refresh_alerts()
        pane.refresh_actions()
        pump(app)

        card = pane.board.card(doomed)
        report.check(card is not None and card.delete_button is not None, "every card carries a bin")
        report.check(
            card.delete_button.toolTip() == f"Remove {doomed}",
            "the bin says what it removes",
            card.delete_button.toolTip(),
        )
        report.check(not card.delete_button.icon().isNull(), "the bin has an icon")

        asked.clear()
        card.delete_button.click()
        pump(app, seconds=0.6)
        report.check(doomed not in service.list_metrics(), "clicking it removes the metric")
        report.check(doomed not in pane.board.handles, "and the card goes with it")
        report.check(
            not service.list_alerts(),
            "the alarm watching it is removed too, rather than left dangling",
            str(sorted(service.list_alerts())),
        )
        report.check(
            doomed_action not in service.list_actions() and doomed_action not in pane.action_buttons,
            "a dependent action is removed from the service and window too",
            str(sorted(service.list_actions())),
        )
        report.check(
            bool(asked) and "alarms watch it" in asked[-1][1] and "actions depend on it" in asked[-1][1],
            "and the user was warned about both dependencies",
            asked[-1][1].replace("\n", " ")[:70] if asked else "nothing asked",
        )

        window.set_use_widgets(False)
        pump(app)
        report.check(
            pane.remove_button.isVisible(),
            "the Remove button comes back in table mode",
        )
        report.check(
            window.network_pane.board.deletable is False,
            "a peer's metrics are not ours to delete",
        )
        window.set_use_widgets(True)
        pump(app)

        print("\n9. The board reflows both ways")
        for index in range(8):
            service.add_metric(
                MetricSpec(
                    label=f"Filler {index}",
                    kind=MetricKind.NUMBER,
                    controllable=True,
                    initial_value=Decimal(index),
                ),
            )
        pane.refresh()
        window.set_use_widgets(True)
        window.split_view_action.setChecked(False)
        pump(app, seconds=0.5)

        board = pane.board

        def occupied_columns() -> int:
            """How many distinct grid columns actually hold a card."""
            columns = set()
            for index in range(board._grid.count()):  # noqa: SLF001
                _, column, _, _ = board._grid.getItemPosition(index)  # noqa: SLF001
                columns.add(column)
            return len(columns)

        def stale_stretch() -> list[int]:
            """Columns that still stretch despite holding nothing.

            This is what made narrowing fail: QGridLayout never shrinks its column count,
            so a stretch left on an emptied column keeps reserving space for it.
            """
            used = set()
            for index in range(board._grid.count()):  # noqa: SLF001
                _, column, _, _ = board._grid.getItemPosition(index)  # noqa: SLF001
                used.add(column)
            return [
                column
                for column in range(board._grid.columnCount())  # noqa: SLF001
                if column not in used and board._grid.columnStretch(column) > 0  # noqa: SLF001
            ]

        window.resize(1600, 640)
        pump(app, seconds=0.5)
        wide_columns = occupied_columns()
        report.check(wide_columns > 1, "a wide window uses several columns", str(wide_columns))
        report.check(not stale_stretch(), "no empty column is stretched when wide")

        window.resize(600, 640)
        pump(app, seconds=0.5)
        narrow_columns = occupied_columns()
        report.check(
            narrow_columns < wide_columns,
            "narrowing reduces the column count again",
            f"{wide_columns} -> {narrow_columns}",
        )
        report.check(
            not stale_stretch(),
            "and leaves no stretched empty columns behind",
            str(stale_stretch()),
        )

        window.resize(1600, 640)
        pump(app, seconds=0.5)
        report.check(
            occupied_columns() == wide_columns,
            "widening again gets the columns back",
            f"{occupied_columns()} vs {wide_columns}",
        )

        report.check(
            all(board.card(handle) is not None for handle in board.handles),
            "every card survived the reflowing",
        )
        window.split_view_action.setChecked(True)
        pump(app, seconds=0.3)

        window.network_pane.shutdown()
        window.close()
    finally:
        service.stop()

    print()
    print(f"(slider declines above {MAX_SLIDER_STEPS} steps)")
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
