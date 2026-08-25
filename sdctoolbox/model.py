"""Data model for the toolbox: what a "data source" is, before it becomes BICEPS.

BICEPS defines exactly five metric descriptor types and MetricKind mirrors them. Three of
them can be remote-controlled; the two sample-array kinds cannot, because no set operation
takes a sample array as its argument.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sdc11073.provider.operations import OperationDefinitionBase, SetStringOperation, SetValueOperation
from sdc11073.xml_types import pm_qnames as pm

from . import constants


class MetricKind(enum.Enum):
    """The five metric descriptor types BICEPS defines.

    NUMBER, TEXT and CHOICE can be remote-controlled; the two sample-array kinds cannot,
    because there is no set operation whose argument is a sample array.
    """

    NUMBER = "number"
    TEXT = "text"
    CHOICE = "choice"
    WAVEFORM = "waveform"
    DISTRIBUTION = "distribution"

    @property
    def descriptor_qname(self):
        """Return the BICEPS descriptor QName for this kind."""
        return _DESCRIPTOR_QNAMES[self]

    @property
    def operation_class(self) -> type[OperationDefinitionBase] | None:
        """Return the operation class used to control this kind, or None if uncontrollable."""
        return _OPERATION_CLASSES.get(self)

    @property
    def controllable(self) -> bool:
        """Whether a set operation exists for this kind."""
        return self in _OPERATION_CLASSES


_DESCRIPTOR_QNAMES = {
    MetricKind.NUMBER: pm.NumericMetricDescriptor,
    MetricKind.TEXT: pm.StringMetricDescriptor,
    MetricKind.CHOICE: pm.EnumStringMetricDescriptor,
    MetricKind.WAVEFORM: pm.RealTimeSampleArrayMetricDescriptor,
    MetricKind.DISTRIBUTION: pm.DistributionSampleArrayMetricDescriptor,
}

# Note there is no SetEnumOperation: a choice is controlled with SetStringOperation, and the
# permitted values come from the target descriptor's AllowedValue list.
_OPERATION_CLASSES: dict[MetricKind, type[OperationDefinitionBase]] = {
    MetricKind.NUMBER: SetValueOperation,
    MetricKind.TEXT: SetStringOperation,
    MetricKind.CHOICE: SetStringOperation,
}


def slugify(label: str) -> str:
    """Turn a human label into a handle-safe slug.

    Handles end up in URLs and XML attributes, so restrict them to ASCII word characters.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_").lower()
    return slug or "metric"


@dataclass
class MetricSpec:
    """A data source as the user describes it, before it is turned into BICEPS descriptors."""

    label: str
    kind: MetricKind
    #: Human readable unit. Empty means dimensionless; the descriptor still carries the
    #: MDC_DIM_DIMLESS code, we simply do not invent a description for it.
    unit_label: str = ""
    unit_code: str = constants.CODE_DIMENSIONLESS
    unit_coding_system: str = constants.CODING_SYSTEM_MDC
    allowed_values: tuple[str, ...] = ()
    resolution: Decimal | None = None
    controllable: bool = False
    #: Explicit handle. When None, ProviderService derives one from the label.
    handle: str | None = None
    #: Free-form code for the metric's own Type. Defaults to the slug in our private system.
    type_code: str | None = None
    type_coding_system: str = constants.CODING_SYSTEM_PRIVATE
    #: Value applied right after creation. None leaves the metric without a MetricValue.
    initial_value: Decimal | str | None = None

    def __post_init__(self) -> None:
        if self.controllable and not self.kind.controllable:
            msg = f"{self.kind.value} metrics cannot be remote-controlled"
            raise ValueError(msg)
        if self.kind is MetricKind.CHOICE and not self.allowed_values:
            msg = "a choice metric needs at least one allowed value"
            raise ValueError(msg)
        if self.kind is not MetricKind.CHOICE and self.allowed_values:
            msg = f"allowed_values is only meaningful for {MetricKind.CHOICE.value} metrics"
            raise ValueError(msg)
        if self.kind is MetricKind.NUMBER and self.resolution is None:
            # BICEPS requires Resolution on a NumericMetricDescriptor. Whole numbers use 1.
            self.resolution = Decimal("1")
        if self.resolution is not None and not isinstance(self.resolution, Decimal):
            msg = "resolution must be a Decimal, never a float"
            raise TypeError(msg)
        if isinstance(self.initial_value, float):
            msg = "initial_value must be a Decimal or str, never a float"
            raise TypeError(msg)
        if self.kind is MetricKind.CHOICE and self.initial_value is not None:
            if self.initial_value not in self.allowed_values:
                msg = f"initial_value {self.initial_value!r} is not among allowed_values"
                raise ValueError(msg)

    @property
    def slug(self) -> str:
        """Slug used to build handles."""
        return slugify(self.label)

    def effective_type_code(self) -> str:
        """Code for this metric's Type element."""
        return self.type_code or self.slug


@dataclass
class RemoteMetric:
    """A metric observed on a peer device, as the consumer side sees it.

    Everything here is optional on purpose: foreign providers routinely omit units, types
    and values, and the consumer has to render them anyway rather than discard them.
    """

    handle: str
    node_type_name: str
    kind: MetricKind | None = None
    label: str | None = None
    unit_label: str | None = None
    type_code: str | None = None
    allowed_values: tuple[str, ...] = ()
    value: object = None
    parent_handle: str | None = None
    #: Handles of set operations whose OperationTarget is this metric.
    operation_handles: tuple[str, ...] = field(default_factory=tuple)
    #: True when at least one of those operations currently has OperatingMode == En.
    controllable_now: bool = False

    @property
    def controllable(self) -> bool:
        """Whether any operation targets this metric at all."""
        return bool(self.operation_handles)
