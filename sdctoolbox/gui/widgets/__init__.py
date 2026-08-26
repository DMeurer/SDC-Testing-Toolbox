"""Widget controls for metrics.

A metric can be shown as a row in a table, or as a control that suits what it actually is:
a dropdown for a choice, a slider for a bounded number, a stepper for an unbounded one.
The table is always available and always works; widgets are the nicer view when one fits.

Adding a new kind of control means writing a class in ``controls`` and putting it in the
list in ``factory``. Nothing else needs to know about it.
"""

from .base import MetricWidget, WidgetSpec, from_remote_metric, from_spec
from .board import WidgetBoard
from .factory import CONTROLS, build_widget

__all__ = [
    "CONTROLS",
    "MetricWidget",
    "WidgetBoard",
    "WidgetSpec",
    "build_widget",
    "from_remote_metric",
    "from_spec",
]
