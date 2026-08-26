"""What every metric control has in common.

The provider side describes its metrics with MetricSpec, the consumer side describes a
peer's with RemoteMetric. They carry the same facts in slightly different shapes, so both
are converted to a WidgetSpec and the controls only ever see that.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from ...model import MetricKind

if TYPE_CHECKING:
    from ...model import MetricSpec, RemoteMetric


@dataclass(frozen=True)
class WidgetSpec:
    """Everything a control needs to know about one metric.

    Deliberately a plain snapshot rather than a live reference: the panes rebuild these on
    every refresh, and a control that held a reference could show a metric that has since
    been deleted.
    """

    handle: str
    label: str
    kind: MetricKind | None
    unit: str = ""
    allowed_values: tuple[str, ...] = ()
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    resolution: Decimal | None = None
    #: Whether the user may change this from here. False renders the control read-only
    #: rather than hiding it, so a device's read-outs are still visible.
    editable: bool = False
    #: Shown under the control when it cannot be edited, e.g. why not.
    note: str = ""

    @property
    def caption(self) -> str:
        """What to show as the control's title."""
        return self.label or self.handle

    @property
    def bounded(self) -> bool:
        """Whether both ends of the range are known."""
        return self.minimum is not None and self.maximum is not None


def from_spec(handle: str, spec: MetricSpec, *, editable: bool, note: str = "") -> WidgetSpec:
    """Convert one of our own metrics."""
    return WidgetSpec(
        handle=handle,
        label=spec.label,
        kind=spec.kind,
        unit=spec.unit_label,
        allowed_values=spec.allowed_values,
        minimum=spec.minimum,
        maximum=spec.maximum,
        resolution=spec.resolution,
        editable=editable,
        note=note,
    )


def from_remote_metric(metric: RemoteMetric) -> WidgetSpec:
    """Convert a metric observed on a peer.

    A foreign device may publish no label, so the handle stands in for one. Everything else
    is optional too and simply arrives as None.
    """
    if metric.controllable_now:
        note = ""
    elif metric.controllable:
        note = "control disabled by the device"
    else:
        note = "read-only"

    return WidgetSpec(
        handle=metric.handle,
        label=metric.label or "",
        kind=metric.kind,
        unit=metric.unit_label or "",
        allowed_values=metric.allowed_values,
        minimum=metric.minimum,
        maximum=metric.maximum,
        editable=metric.controllable_now,
        note=note,
    )


class MetricWidget(QWidget):
    """Base class for a control that shows and possibly changes one metric.

    Subclasses implement ``matches`` to say which metrics they can handle, ``build`` to
    create their contents, and ``show_value`` to display one. They emit ``value_requested``
    when the user asks for a change; whoever created the widget decides what that means,
    which is why the provider and the consumer can share the same controls.
    """

    #: The user wants this metric set to this value. Decimal for numbers, str otherwise.
    value_requested = Signal(str, object)

    #: Lower number sorts first when the factory looks for a match.
    priority = 100

    def __init__(self, spec: WidgetSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self.build()
        self.set_editable(spec.editable)

    # -- to implement --------------------------------------------------------------

    @classmethod
    def matches(cls, spec: WidgetSpec) -> bool:  # noqa: ARG003
        """Whether this control can represent the metric."""
        return False

    def build(self) -> None:
        """Create the contents. Called once, from __init__."""
        raise NotImplementedError

    def show_value(self, value: Any) -> None:
        """Display a value that came from the device."""
        raise NotImplementedError

    def set_editable(self, editable: bool) -> None:  # noqa: FBT001
        """Enable or disable interaction."""
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------------

    def busy_editing(self) -> bool:
        """Whether the user is mid-interaction and should not be interrupted.

        A value arriving from the network must not yank a slider out from under a dragging
        mouse, or rewrite a field somebody is typing into.
        """
        return False

    def request(self, value: Any) -> None:
        """Ask for the metric to be set."""
        self.value_requested.emit(self.spec.handle, value)
