"""Choosing which control represents a metric.

The list is the whole registry. To add a kind of control, write the class and put it here;
the panes never mention any control by name.
"""

from __future__ import annotations

from .base import MetricWidget, WidgetSpec
from .controls import ChoiceWidget, ReadoutWidget, SliderWidget, StepperWidget, TextWidget

#: Asked in order of priority, first match wins. ReadoutWidget accepts anything and so must
#: stay last; it is what makes an unknown metric type show up rather than disappear.
CONTROLS: list[type[MetricWidget]] = sorted(
    [
        ChoiceWidget,
        SliderWidget,
        StepperWidget,
        TextWidget,
        ReadoutWidget,
    ],
    key=lambda control: control.priority,
)


def pick_control(spec: WidgetSpec) -> type[MetricWidget]:
    """Return the most specific control that can represent this metric."""
    for control in CONTROLS:
        if control.matches(spec):
            return control
    return ReadoutWidget


def build_widget(spec: WidgetSpec, parent=None) -> MetricWidget:  # noqa: ANN001
    """Create the control for a metric."""
    return pick_control(spec)(spec, parent)
