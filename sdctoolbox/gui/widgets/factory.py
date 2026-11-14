"""Choosing which control represents a metric.

The list is the whole registry. To add a kind of control, write the class and put it here;
the panes never mention any control by name.
"""

from __future__ import annotations

from .base import CardSettings, MetricWidget, WidgetSpec
from .controls import (
    ChoiceWidget,
    ReadoutWidget,
    SampleArrayWidget,
    SliderWidget,
    StepperWidget,
    TextWidget,
)

# Asked in order of priority, first match wins. ReadoutWidget accepts anything and so must
# stay last; it is what makes an unknown metric type show up rather than disappear.
CONTROLS: list[type[MetricWidget]] = sorted(
    [
        ChoiceWidget,
        SampleArrayWidget,
        SliderWidget,
        StepperWidget,
        TextWidget,
        ReadoutWidget,
    ],
    key=lambda control: control.priority,
)


def matching_controls(spec: WidgetSpec) -> list[type[MetricWidget]]:
    """Every control that can represent this metric, the default first."""
    return [control for control in CONTROLS if control.matches(spec)]


def pick_control(spec: WidgetSpec) -> type[MetricWidget]:
    """Return the most specific control that can represent this metric."""
    for control in CONTROLS:
        if control.matches(spec):
            return control
    raise RuntimeError("the control registry must end with a universal catch-all")


def chosen_control(spec: WidgetSpec, settings: CardSettings | None) -> type[MetricWidget]:
    """The card type the settings ask for, if it can show this metric, else the default."""
    if settings is not None and settings.control:
        for control in matching_controls(spec):
            if control.__name__ == settings.control:
                return control
    return pick_control(spec)


def build_widget(spec: WidgetSpec, parent=None, *, settings: CardSettings | None = None) -> MetricWidget:  # noqa: ANN001
    """Create the control for a metric."""
    return chosen_control(spec, settings)(spec, parent, settings=settings)
