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
from typing import Any

from sdc11073.provider.operations import OperationDefinitionBase, SetStringOperation, SetValueOperation
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types

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

    @property
    def creatable(self) -> bool:
        """Whether this build can actually put such a descriptor on the wire."""
        return self not in MISSING_MANDATORY_FIELDS

    @property
    def missing_fields(self) -> tuple[str, ...]:
        """Mandatory descriptor fields this build never fills in, if any."""
        return MISSING_MANDATORY_FIELDS.get(self, ())


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

# A gap in this build, not a rule of the standard. Both sample-array descriptors carry
# mandatory fields that nothing here fills in, and BICEPS only notices when the descriptor
# is serialised - by which point it is already in the MDIB. Refuse them at the door instead.
#
# Verified against the installed sdc11073 rather than taken from the schema: the order below
# is the order serialisation complains in, so the first entry is the error you actually see.
MISSING_MANDATORY_FIELDS: dict[MetricKind, tuple[str, ...]] = {
    MetricKind.WAVEFORM: ("Resolution", "SamplePeriod"),
    MetricKind.DISTRIBUTION: ("DomainUnit", "Resolution"),
}


# The alarm vocabulary is taken straight from BICEPS rather than reinvented, so the values
# that go on the wire are the standard's own:
#   AlertKind      PHYSIOLOGICAL=Phy  TECHNICAL=Tec  OTHER=Oth
#   AlertPriority  NONE=None  LOW=Lo  MEDIUM=Me  HIGH=Hi
#   Manifestation  AUD=Aud  VIS=Vis  TAN=Tan  OTH=Oth
AlertKind = pm_types.AlertConditionKind
AlertPriority = pm_types.AlertConditionPriority
AlertManifestation = pm_types.AlertSignalManifestation

# Every condition this tool creates gets one signal per manifestation listed here. One
# condition driving several signals is the whole point of keeping them separate.
DEFAULT_MANIFESTATIONS = (AlertManifestation.VIS, AlertManifestation.AUD)


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
    # Human readable unit. Empty means dimensionless; the descriptor still carries the
    # MDC_DIM_DIMLESS code, we simply do not invent a description for it.
    unit_label: str = ""
    unit_code: str = constants.CODE_DIMENSIONLESS
    unit_coding_system: str = constants.CODING_SYSTEM_MDC
    allowed_values: tuple[str, ...] = ()
    resolution: Decimal | None = None
    # Lower and upper limit for a number. Either may be left open.
    #
    # These become two different things in BICEPS, because the standard distinguishes what
    # a metric can produce from what a remote caller may ask for:
    #   * NumericMetricDescriptor/TechnicalRange on the metric itself
    #   * SetValueOperationState/AllowedRange on its set operation
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    controllable: bool = False
    # Explicit handle. When None, ProviderService derives one from the label.
    handle: str | None = None
    # Free-form code for the metric's own Type. Defaults to the slug in our private system.
    type_code: str | None = None
    type_coding_system: str = constants.CODING_SYSTEM_PRIVATE
    # Value applied right after creation. None leaves the metric without a MetricValue.
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
        for name in ("minimum", "maximum"):
            limit = getattr(self, name)
            if limit is None:
                continue
            if not isinstance(limit, Decimal):
                msg = f"{name} must be a Decimal, never a float"
                raise TypeError(msg)
            if self.kind is not MetricKind.NUMBER:
                msg = f"{name} is only meaningful for {MetricKind.NUMBER.value} metrics"
                raise ValueError(msg)
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            msg = f"minimum {self.minimum} is greater than maximum {self.maximum}"
            raise ValueError(msg)
        if isinstance(self.initial_value, float):
            msg = "initial_value must be a Decimal or str, never a float"
            raise TypeError(msg)
        if self.kind is MetricKind.NUMBER and isinstance(self.initial_value, Decimal):
            if self.minimum is not None and self.initial_value < self.minimum:
                msg = f"initial_value {self.initial_value} is below minimum {self.minimum}"
                raise ValueError(msg)
            if self.maximum is not None and self.initial_value > self.maximum:
                msg = f"initial_value {self.initial_value} is above maximum {self.maximum}"
                raise ValueError(msg)
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

    @property
    def has_range(self) -> bool:
        """Whether either limit was given."""
        return self.minimum is not None or self.maximum is not None

    def range_text(self) -> str:
        """The limits as something readable, or an empty string when unbounded."""
        return format_range(self.minimum, self.maximum)


def _coerce_enum(enum_cls: Any, value: Any, field_name: str) -> Any:
    """Turn a wire value back into its enum member, and say so clearly if it will not.

    The BICEPS enums subclass str, so a member survives being treated as text and comes
    back looking correct while no longer being an enum. sdc11073 then refuses it with
    "Value can only be X, got <class 'str'>", which does not point at where it went wrong.
    """
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(f"{member.name}={member.value}" for member in enum_cls)
        msg = f"{field_name}: {value!r} is not a {enum_cls.__name__}. Allowed: {allowed}"
        raise ValueError(msg) from exc


@dataclass
class AlertSpec:
    """An alarm condition as the user describes it.

    BICEPS keeps two things apart that are easy to confuse:

    * the **condition** is the fact - "the pressure is too high". It has a kind and a
      priority, and it is either present or it is not.
    * the **signal** is how that fact is announced - visually, audibly, or by vibration.

    One condition can drive several signals, which is why they are separate objects rather
    than flags on one. This tool creates a visual and an audible signal for every condition,
    so the split is visible in the tree.
    """

    label: str
    # Handle of the metric this alarm watches. The condition's Source points at it.
    source_handle: str
    kind: AlertKind = AlertKind.TECHNICAL
    priority: AlertPriority = AlertPriority.MEDIUM
    # When either limit is given, the condition becomes a LimitAlertCondition and its
    # presence follows the source metric automatically.
    lower_limit: Decimal | None = None
    upper_limit: Decimal | None = None
    handle: str | None = None

    def __post_init__(self) -> None:
        # AlertKind and AlertPriority are the BICEPS enums, and those subclass str. Anything
        # that round-trips a value through a string - Qt's QVariant, a JSON file, a console
        # argument - hands back a plain str that looks right but is not the enum. Coerce
        # here so every entry point gets the same treatment.
        self.kind = _coerce_enum(AlertKind, self.kind, "kind")
        self.priority = _coerce_enum(AlertPriority, self.priority, "priority")

        for name in ("lower_limit", "upper_limit"):
            limit = getattr(self, name)
            if limit is not None and not isinstance(limit, Decimal):
                msg = f"{name} must be a Decimal, never a float"
                raise TypeError(msg)
        if (
            self.lower_limit is not None
            and self.upper_limit is not None
            and self.lower_limit > self.upper_limit
        ):
            msg = f"lower_limit {self.lower_limit} is above upper_limit {self.upper_limit}"
            raise ValueError(msg)
        if not self.label.strip():
            msg = "an alarm needs a label"
            raise ValueError(msg)

    @property
    def slug(self) -> str:
        """Slug used to build handles."""
        return slugify(self.label)

    @property
    def has_limits(self) -> bool:
        """Whether presence is derived from the source metric rather than set by hand."""
        return self.lower_limit is not None or self.upper_limit is not None

    def limit_text(self) -> str:
        """The limits as something readable, or an empty string when set manually."""
        if not self.has_limits:
            return ""
        if self.lower_limit is not None and self.upper_limit is not None:
            return f"outside {self.lower_limit} to {self.upper_limit}"
        if self.lower_limit is not None:
            return f"below {self.lower_limit}"
        return f"above {self.upper_limit}"

    def breached_by(self, value: object) -> bool:
        """Whether a source value puts this condition into the present state."""
        if not self.has_limits or not isinstance(value, Decimal):
            return False
        if self.lower_limit is not None and value < self.lower_limit:
            return True
        return self.upper_limit is not None and value > self.upper_limit


@dataclass
class RemoteAlert:
    """An alarm observed on a peer device.

    As with RemoteMetric, every field is optional: a foreign provider may publish a bare
    condition with no type, no source and no signals at all.
    """

    handle: str
    node_type_name: str
    label: str | None = None
    kind: str | None = None
    priority: str | None = None
    present: bool = False
    activation: str | None = None
    source_handles: tuple[str, ...] = field(default_factory=tuple)
    lower_limit: Decimal | None = None
    upper_limit: Decimal | None = None
    # Handle -> manifestation for the signals that announce this condition.
    signals: dict[str, str] = field(default_factory=dict)

    def limit_text(self) -> str:
        """The monitored limits, or an empty string when there are none."""
        return format_range(self.lower_limit, self.upper_limit)


def format_range(minimum: Decimal | None, maximum: Decimal | None) -> str:
    """Render a pair of optional limits, e.g. '1 to 100', 'from 0', 'up to 50'."""
    if minimum is not None and maximum is not None:
        return f"{minimum} to {maximum}"
    if minimum is not None:
        return f"from {minimum}"
    if maximum is not None:
        return f"up to {maximum}"
    return ""


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
    # Limits the peer publishes. `minimum`/`maximum` come from the set operation's
    # AllowedRange when there is one, otherwise from the metric's TechnicalRange.
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    # The metric's own TechnicalRange, kept separately because it describes what the device
    # can produce rather than what we are allowed to ask for.
    technical_minimum: Decimal | None = None
    technical_maximum: Decimal | None = None
    value: object = None
    parent_handle: str | None = None
    # Handles of set operations whose OperationTarget is this metric.
    operation_handles: tuple[str, ...] = field(default_factory=tuple)
    # True when at least one of those operations currently has OperatingMode == En.
    controllable_now: bool = False

    @property
    def controllable(self) -> bool:
        """Whether any operation targets this metric at all."""
        return bool(self.operation_handles)

    @property
    def has_range(self) -> bool:
        """Whether the peer publishes a limit we should respect."""
        return self.minimum is not None or self.maximum is not None

    def range_text(self) -> str:
        """The limits as something readable, or an empty string when unbounded."""
        return format_range(self.minimum, self.maximum)
