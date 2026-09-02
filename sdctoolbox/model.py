"""Data model for the toolbox: what a "data source" is, before it becomes BICEPS.

BICEPS defines exactly five metric descriptor types and MetricKind mirrors them. Three of
them can be remote-controlled; the two sample-array kinds cannot, because no set operation
takes a sample array as its argument.
"""

from __future__ import annotations

import enum
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from sdc11073.provider.operations import (
    OperationDefinitionBase,
    SetStringOperation,
    SetValueOperation,
)
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.dataconverters import DecimalConverter

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

# Kinds this build cannot put on the wire, and the mandatory descriptor fields it fails to
# fill in for them. Empty: every kind BICEPS defines can now be created. Kept as the place
# to record such a gap if one ever reappears, because the failure mode is nasty - BICEPS
# only notices a missing mandatory field when the descriptor is serialised, which happens
# after the transaction has already committed it. See ProviderService._create_entities.
MISSING_MANDATORY_FIELDS: dict[MetricKind, tuple[str, ...]] = {}

# The two sample-array kinds carry many values per state rather than one.
SAMPLE_ARRAY_KINDS = (MetricKind.WAVEFORM, MetricKind.DISTRIBUTION)


class WaveformShape(enum.Enum):
    """The curve a generated waveform follows.

    Not a BICEPS concept at all - the standard carries samples and says nothing about their
    shape. This exists so the tool has something recognisable to send, and so a consumer
    being tested against it can be checked against a curve somebody can identify by eye.

    The first four are geometric and are for testing rendering: a sawtooth makes a dropped
    or duplicated block obvious in a way a sine does not. The rest are shaped like the
    signals real devices publish, so a preset can look like the machine it names. They are
    caricatures drawn to be recognisable, not clinical models, and nothing here should be
    read as a statement about physiology.
    """

    SINE = "sine"
    SAWTOOTH = "sawtooth"
    SQUARE = "square"
    NOISE = "noise"
    # A pulse oximeter's plethysmogram: a steep upstroke, a dicrotic notch on the way down,
    # then a slow diastolic decay.
    PULSE = "pulse"
    # The QRS complex an ECG is recognised by, with its P and T waves.
    ECG = "ecg"
    # Arterial blood pressure: the same beat as the pulse but riding on a baseline, since
    # it never returns to zero.
    ARTERIAL = "arterial"
    # Ventilation: a slow rise, a plateau, a passive fall, then a pause.
    RESPIRATION = "respiration"
    # Airway flow: inspiratory positive, expiratory negative, crossing zero between.
    FLOW = "flow"


class DistributionShape(enum.Enum):
    """The shape a generated distribution takes across its domain.

    Same standing as WaveformShape: nothing in BICEPS, and here so a distribution card
    shows something a person can recognise as belonging to the device that publishes it.
    """

    # One peak, drifting. The generic distribution.
    BELL = "bell"
    # A fundamental with decaying harmonics, which is what a power spectrum looks like.
    SPECTRUM = "spectrum"
    # Two peaks, e.g. a tissue impedance spectrum with a second population.
    BIMODAL = "bimodal"
    # Falling from the low end of the domain, e.g. a particle size or intensity histogram.
    DECAY = "decay"
    # Flat with noise on it, for a baseline or a reference channel.
    FLAT = "flat"


# A tenth of a second: fast enough to look alive on screen, slow enough that two instances
# on one machine are not spending all their time serialising sample arrays.
DEFAULT_SAMPLE_PERIOD = Decimal("0.1")

# How many samples a waveform card keeps and draws.
WAVEFORM_HISTORY = 300


# The alarm vocabulary is taken straight from BICEPS rather than reinvented, so the values
# that go on the wire are the standard's own:
#   AlertKind      PHYSIOLOGICAL=Phy  TECHNICAL=Tec  OTHER=Oth
#   AlertPriority  NONE=None  LOW=Lo  MEDIUM=Me  HIGH=Hi
#   Manifestation  AUD=Aud  VIS=Vis  TAN=Tan  OTH=Oth
AlertKind = pm_types.AlertConditionKind
AlertPriority = pm_types.AlertConditionPriority
AlertManifestation = pm_types.AlertSignalManifestation

# How a signal is currently announcing itself.
#   ON=On  OFF=Off  LATCH=Latch  ACK=Ack
# ACK is the acknowledgement: the user has seen the alarm. The condition stays present, so
# the fact is not erased - only the way it is being announced changes.
AlertSignalPresence = pm_types.AlertSignalPresence

# Where a signal is announced. LOCAL=Loc means here, REMOTE=Rem means another device has
# taken it over. That hand-over is what BICEPS calls signal delegation, and a signal may
# only be delegated when its descriptor says SignalDelegationSupported.
AlertSignalLocation = pm_types.AlertSignalPrimaryLocation

# Default signal definitions preserve the original visual and audible behavior.
DEFAULT_MANIFESTATIONS = (AlertManifestation.VIS, AlertManifestation.AUD)


def slugify(label: str) -> str:
    """Turn a human label into a handle-safe slug.

    Handles end up in URLs and XML attributes, so restrict them to ASCII word characters.
    """
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", label).strip("_").lower()
    return slug or "metric"


# Which coding system a code belongs to. Presets name these rather than repeating an OID.
CODING_SYSTEMS = {
    "mdc": constants.CODING_SYSTEM_MDC,
    "private": constants.CODING_SYSTEM_PRIVATE,
}


def _xml_compatible(value: str) -> bool:
    """Whether a string can be written in an XML 1.0 document."""
    return all(
        codepoint in (0x9, 0xA, 0xD)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
        for codepoint in map(ord, value)
    )


_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_URI_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~:/?#[]@!$&'()*+,;=%",
)
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_absolute_uri(value: str) -> bool:
    """Check the URI form accepted for explicit coding-system identifiers."""
    if _URI_SCHEME.match(value) is None or not value.partition(":")[2]:
        return False
    if any(character not in _URI_CHARACTERS for character in value):
        return False
    if any(
        character == "%"
        and (index + 2 >= len(value) or value[index + 1] not in _HEX_DIGITS or value[index + 2] not in _HEX_DIGITS)
        for index, character in enumerate(value)
    ):
        return False
    try:
        return bool(urlsplit(value).scheme)
    except ValueError:
        return False


@dataclass(frozen=True)
class Coding:
    """A BICEPS CodedValue: what a thing *is*, as opposed to what it is called.

    The label is for people and the code is for machines, and only the code makes a metric
    mean the same thing to a device that has never met this one. That distinction is the
    reason SDC exists, so the two are kept apart here rather than a label being allowed to
    stand in for semantics.

    ``system`` is either a key of CODING_SYSTEMS or an explicit coding-system URI:

    * ``mdc`` — IEEE 11073-10101. Use it only where a term genuinely exists. The codes this
      project ships are the standard's *reference IDs* (``MDC_PULS_OXIM_SAT_O2``), not its
      numeric CF codes, because 11073-10101 itself was not available to check them against.
      Anything claiming to interoperate for real has to substitute the numbers.
    * ``private`` — ``urn:sdc-testing-toolbox:private``. Everything with no standard term,
      which for surgical devices is most of it. Marked rather than disguised: that gap is
      real and is the subject of active work on extending the nomenclature.
    * An explicit URI lets a profile preserve a coding system not represented by the two
      toolbox aliases. This matters for externally defined patient demographics such as race.
    """

    code: str
    system: str = "private"
    label: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            msg = "a coding needs a non-empty string code"
            raise ValueError(msg)
        if not _xml_compatible(self.code):
            msg = "a coding code contains characters not allowed in XML 1.0"
            raise ValueError(msg)
        if not isinstance(self.system, str):
            msg = "a coding system must be a string"
            raise TypeError(msg)
        if not _xml_compatible(self.system):
            msg = "a coding system contains characters not allowed in XML 1.0"
            raise ValueError(msg)
        if not isinstance(self.label, str):
            msg = "a coding label must be a string"
            raise TypeError(msg)
        if not _xml_compatible(self.label):
            msg = "a coding label contains characters not allowed in XML 1.0"
            raise ValueError(msg)
        if self.system not in CODING_SYSTEMS and not _is_absolute_uri(self.system):
            allowed = ", ".join(sorted(CODING_SYSTEMS))
            msg = f"unknown coding system {self.system!r}. Use {allowed}, or a valid coding-system URI"
            raise ValueError(msg)

    @property
    def coding_system(self) -> str:
        """The URI that goes on the wire."""
        return CODING_SYSTEMS.get(self.system, self.system)

    @property
    def is_standard(self) -> bool:
        """Whether this claims a term from the standard nomenclature."""
        return self.coding_system == constants.CODING_SYSTEM_MDC

    def summary(self) -> str:
        """Something short enough for a table cell."""
        if self.is_standard:
            return self.code
        if self.system == "private":
            return f"{self.code} (private)"
        return f"{self.code} ({self.system})"

    @classmethod
    def from_wire(
        cls,
        *,
        code: str,
        coding_system: str | None,
        label: str = "",
    ) -> Coding:
        """Build a Coding from a BICEPS CodedValue's wire fields.

        A missing CodingSystem has BICEPS's IEEE 11073-10101 default. Known toolbox URIs
        become their concise aliases; every other URI is retained verbatim.
        """
        system = next(
            (name for name, uri in CODING_SYSTEMS.items() if uri == coding_system),
            coding_system or "mdc",
        )
        return cls(code=code, system=system, label=label)


# Bound expansion before fixed-point formatting: Decimal('1E+999999') is legal in Python but
# is not practical to materialize or send as an xsd:decimal attribute.
MAX_DECIMAL_WIRE_CHARS = 1024


class _FixedPointDecimal(Decimal):
    """A Decimal that bypasses sdc11073's lossy exponent-to-float conversion."""

    def __str__(self) -> str:
        return format(self, "f")


def _fixed_point_length(value: Decimal) -> int:
    """Length of ``format(value, 'f')`` without expanding an unbounded exponent."""
    sign, digits, exponent = value.as_tuple()
    if not any(digits):
        return 1 + sign
    if exponent >= 0:
        return sign + len(digits) + exponent
    decimal_index = len(digits) + exponent
    if decimal_index > 0:
        return sign + len(digits) + 1
    return sign + 2 + len(digits) - decimal_index


def patient_measurement_wire_value(value: Decimal) -> Decimal:
    """Return a lossless BICEPS-serializable Decimal for a demographic Measurement.

    sdc11073 3.0.0 sends exponent-form Decimals through ``float``. Force fixed-point
    formatting, reject unbounded expansions, and verify the library will not truncate the
    value before it is allowed into a context state.
    """
    validate_decimal(value, "patient measurement value")
    if _fixed_point_length(value) > MAX_DECIMAL_WIRE_CHARS:
        msg = f"patient measurement value exceeds {MAX_DECIMAL_WIRE_CHARS} wire characters"
        raise ValueError(msg)
    wire_value = _FixedPointDecimal("0") if value.is_zero() else _FixedPointDecimal(value)
    serialized = DecimalConverter.to_xml(wire_value)
    try:
        lossless = Decimal(serialized) == value
    except (ArithmeticError, ValueError):
        lossless = False
    if not lossless:
        msg = "patient measurement value cannot be serialized without losing precision"
        raise ValueError(msg)
    return wire_value


def validate_decimal(
    value: object,
    field: str,
    *,
    positive: bool = False,
    float_representable: bool = False,
) -> Decimal:
    """Validate a Decimal before it is serialized or used in numeric logic."""
    if not isinstance(value, Decimal):
        msg = f"{field} must be a Decimal, never a float"
        raise TypeError(msg)
    if not value.is_finite():
        msg = f"{field} must be finite"
        raise ValueError(msg)
    if positive and value <= 0:
        msg = f"{field} must be positive, not {value}"
        raise ValueError(msg)
    if float_representable:
        try:
            float_value = float(value)
        except (OverflowError, ValueError) as exc:
            msg = f"{field} must be representable as a finite positive float"
            raise ValueError(msg) from exc
        if not math.isfinite(float_value) or float_value <= 0:
            msg = f"{field} must be representable as a finite positive float, not {value}"
            raise ValueError(msg)
    return value


def fixed_point_decimal(value: Decimal, field: str) -> Decimal:
    """Return a bounded Decimal whose string form is safe for xsd:decimal."""
    validate_decimal(value, field)
    if _fixed_point_length(value) > MAX_DECIMAL_WIRE_CHARS:
        msg = f"{field} exceeds {MAX_DECIMAL_WIRE_CHARS} wire characters"
        raise ValueError(msg)
    return _FixedPointDecimal("0") if value.is_zero() else _FixedPointDecimal(value)


# MDC_DIM_DIMLESS, for a metric that measures a bare number.
DIMENSIONLESS = Coding(code=constants.CODE_DIMENSIONLESS, system="mdc", label="")


@dataclass(frozen=True)
class DeviceInfo:
    """Who the device says it is, in DPWS terms.

    This is ThisModel and ThisDevice, which a consumer fetches as metadata before it has
    looked at a single metric. A preset that describes a ventilator and then announces
    itself as "SDC Toolbox" is not modelling a ventilator, and until this existed that is
    exactly what every preset did: the block was written to the file and never read back.

    Fixed at construction on purpose. sdc11073 takes ThisModel and ThisDevice when the
    provider is built, so changing them afterwards would not re-announce anything - see
    ProviderService.start.
    """

    friendly_name: str = ""
    manufacturer: str = constants.MANUFACTURER
    manufacturer_url: str = constants.MANUFACTURER_URL
    model_name: str = constants.MODEL_NAME
    model_number: str = constants.MODEL_NUMBER
    firmware_version: str = constants.FIRMWARE_VERSION

    def is_empty(self) -> bool:
        """Whether this says anything the defaults do not."""
        return self == DeviceInfo()


@dataclass
class MetricSpec:
    """A data source as the user describes it, before it is turned into BICEPS descriptors."""

    label: str
    kind: MetricKind
    # Human readable unit. Empty means dimensionless; the descriptor still carries the
    # MDC_DIM_DIMLESS code, we simply do not invent a description for it.
    unit_label: str = ""
    # What the metric *is* and what its values are *in*, as codes rather than words. Left
    # as None they fall back to a private code derived from the label and to
    # MDC_DIM_DIMLESS - fine for a scratch metric, not enough to model a real device.
    type_coding: Coding | None = None
    unit_coding: Coding | None = None
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
    # Value applied right after creation. None leaves the metric without a MetricValue.
    initial_value: Decimal | str | None = None

    # -- waveform only -----------------------------------------------------------------
    # RealTimeSampleArrayMetricDescriptor/SamplePeriod: the time between two samples, as a
    # duration in seconds. Mandatory, so it gets a default rather than being left unset.
    sample_period: Decimal | None = None
    # The curve to generate. Nothing in BICEPS; see WaveformShape.
    shape: WaveformShape = WaveformShape.SINE
    # How many samples make one full cycle of that curve. A heartbeat and a breath are the
    # same shape machinery at very different rates, and this is what separates them.
    cycle_samples: int = 40

    # -- distribution only -------------------------------------------------------------
    # DistributionSampleArrayMetricDescriptor/DomainUnit: what the samples are distributed
    # *over*, as opposed to Unit, which is what the samples themselves measure. A power
    # spectrum is measured in dB (Unit) across a range of Hz (DomainUnit). Mandatory.
    domain_unit_label: str = ""
    domain_unit_coding: Coding | None = None
    # DistributionRange: the extent of that domain, i.e. the x axis. Distinct from
    # minimum/maximum, which describe the samples themselves.
    domain_minimum: Decimal | None = None
    domain_maximum: Decimal | None = None
    # The shape to generate across that domain. Nothing in BICEPS; see DistributionShape.
    distribution_shape: DistributionShape = DistributionShape.BELL
    # Which subsystem of the device this belongs to. Empty puts it in the default channel;
    # a name gives it a Vmd and Channel of its own, so a consumer browsing the containment
    # tree sees a device with parts rather than one flat list of metrics.
    section: str = ""

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
        if self.kind in SAMPLE_ARRAY_KINDS and self.resolution is None:
            # Mandatory on both sample-array descriptors too, and easy to miss because the
            # complaint only arrives when the descriptor is serialised.
            self.resolution = Decimal("0.1")
        if self.resolution is not None and not isinstance(self.resolution, Decimal):
            msg = "resolution must be a Decimal, never a float"
            raise TypeError(msg)
        if self.resolution is not None:
            validate_decimal(self.resolution, "resolution", positive=True)
            fixed_point_decimal(self.resolution, "resolution")

        if self.kind is MetricKind.WAVEFORM:
            if self.sample_period is None:
                self.sample_period = DEFAULT_SAMPLE_PERIOD
            validate_decimal(
                self.sample_period,
                "sample_period",
                positive=True,
                float_representable=True,
            )
            self.shape = _coerce_enum(WaveformShape, self.shape, "shape")
            if not isinstance(self.cycle_samples, int) or self.cycle_samples < 2:  # noqa: PLR2004
                msg = f"cycle_samples must be an integer of at least 2, not {self.cycle_samples!r}"
                raise ValueError(msg)
        elif self.sample_period is not None:
            msg = f"sample_period is only meaningful for {MetricKind.WAVEFORM.value} metrics"
            raise ValueError(msg)

        if self.kind is MetricKind.DISTRIBUTION:
            self.distribution_shape = _coerce_enum(
                DistributionShape,
                self.distribution_shape,
                "distribution_shape",
            )
            if self.domain_minimum is None:
                self.domain_minimum = Decimal("0")
            if self.domain_maximum is None:
                self.domain_maximum = Decimal("1")
        for name in ("domain_minimum", "domain_maximum"):
            limit = getattr(self, name)
            if limit is None:
                continue
            if not isinstance(limit, Decimal):
                msg = f"{name} must be a Decimal, never a float"
                raise TypeError(msg)
            fixed_point_decimal(limit, name)
            if self.kind is not MetricKind.DISTRIBUTION:
                msg = f"{name} is only meaningful for {MetricKind.DISTRIBUTION.value} metrics"
                raise ValueError(msg)
        if (
            self.domain_minimum is not None
            and self.domain_maximum is not None
            and self.domain_minimum >= self.domain_maximum
        ):
            msg = f"domain_minimum {self.domain_minimum} must be below domain_maximum {self.domain_maximum}"
            raise ValueError(msg)
        if self.domain_unit_label and self.kind is not MetricKind.DISTRIBUTION:
            msg = f"domain_unit_label is only meaningful for {MetricKind.DISTRIBUTION.value} metrics"
            raise ValueError(msg)

        for name in ("minimum", "maximum"):
            limit = getattr(self, name)
            if limit is None:
                continue
            if not isinstance(limit, Decimal):
                msg = f"{name} must be a Decimal, never a float"
                raise TypeError(msg)
            fixed_point_decimal(limit, name)
            # A sample array's TechnicalRange describes its samples, so limits mean the
            # same thing there as on a number. Only text and choice have no use for them.
            if self.kind is MetricKind.TEXT or self.kind is MetricKind.CHOICE:
                msg = f"{name} is not meaningful for {self.kind.value} metrics"
                raise ValueError(msg)
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            msg = f"minimum {self.minimum} is greater than maximum {self.maximum}"
            raise ValueError(msg)
        if self.kind in SAMPLE_ARRAY_KINDS and self.initial_value is not None:
            msg = f"initial_value is not meaningful for {self.kind.value} metrics; use samples instead"
            raise ValueError(msg)
        if self.initial_value is not None:
            self.initial_value = coerce_metric_value(self, self.initial_value)

    @property
    def slug(self) -> str:
        """Slug used to build handles."""
        return slugify(self.label)

    def effective_type(self) -> Coding:
        """What this metric is, as a code.

        Without one it falls back to the slug in the private system, which is honest: a
        metric nobody has given a term to does not have one.
        """
        return self.type_coding or Coding(code=self.slug, system="private", label=self.label)


    def effective_unit(self) -> Coding:
        """What this metric's values are in, as a code."""
        if self.unit_coding is not None:
            return self.unit_coding
        return Coding(code=constants.CODE_DIMENSIONLESS, system="mdc", label=self.unit_label)

    def effective_domain_unit(self) -> Coding:
        """What a distribution's samples are spread over, as a code."""
        if self.domain_unit_coding is not None:
            return self.domain_unit_coding
        return Coding(code=constants.CODE_DIMENSIONLESS, system="mdc", label=self.domain_unit_label)

    @property
    def has_range(self) -> bool:
        """Whether either limit was given."""
        return self.minimum is not None or self.maximum is not None

    @property
    def is_sample_array(self) -> bool:
        """Whether one state of this metric carries many values rather than one."""
        return self.kind in SAMPLE_ARRAY_KINDS

    def domain_text(self) -> str:
        """The distribution's domain as something readable, e.g. '0 to 100 Hz'."""
        if self.kind is not MetricKind.DISTRIBUTION:
            return ""
        extent = format_range(self.domain_minimum, self.domain_maximum)
        return f"{extent} {self.domain_unit_label}".strip()

    def range_text(self) -> str:
        """The limits as something readable, or an empty string when unbounded."""
        return format_range(self.minimum, self.maximum)


def coerce_metric_value(spec: MetricSpec, value: object, handle: str | None = None) -> Decimal | str:
    """Normalize and validate one scalar metric value.

    Numbers accept ``Decimal`` values and decimal strings and are returned as ``Decimal``;
    text and choice values are returned as strings. Floats are always rejected. Numeric
    bounds and choice membership come from the target metric specification.
    """
    target = handle or spec.handle or spec.label
    if isinstance(value, float):
        msg = f"value for {target!r} must be a Decimal or str, never a float"
        raise TypeError(msg)

    if spec.kind is MetricKind.NUMBER:
        try:
            normalized: Decimal | str = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            msg = f"{value!r} is not a number for {target!r}"
            raise ValueError(msg) from exc
        validate_decimal(normalized, f"value for {target!r}")
        if spec.minimum is not None and normalized < spec.minimum:
            msg = f"{normalized} is below the minimum {spec.minimum} of {target!r}"
            raise ValueError(msg)
        if spec.maximum is not None and normalized > spec.maximum:
            msg = f"{normalized} is above the maximum {spec.maximum} of {target!r}"
            raise ValueError(msg)
        return fixed_point_decimal(normalized, f"value for {target!r}")

    if spec.kind is MetricKind.TEXT:
        return str(value)

    if spec.kind is MetricKind.CHOICE:
        normalized = str(value)
        if normalized not in spec.allowed_values:
            msg = f"{normalized!r} is not among the allowed values {list(spec.allowed_values)} of {target!r}"
            raise ValueError(msg)
        return normalized

    msg = f"{spec.kind.value} metrics do not hold one scalar value"
    raise ValueError(msg)


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


@dataclass(frozen=True)
class AlertSignalSpec:
    """The static descriptor settings for one signal of an alert condition."""

    manifestation: AlertManifestation
    latching: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifestation", _coerce_enum(AlertManifestation, self.manifestation, "manifestation"))
        if not isinstance(self.latching, bool):
            msg = "latching must be a bool"
            raise TypeError(msg)


def _default_alert_signals() -> tuple[AlertSignalSpec, ...]:
    return tuple(AlertSignalSpec(manifestation) for manifestation in DEFAULT_MANIFESTATIONS)


@dataclass
class AlertSpec:
    """An alarm condition as the user describes it.

    BICEPS keeps two things apart that are easy to confuse:

    * the **condition** is the fact - "the pressure is too high". It has a kind and a
      priority, and it is either present or it is not.
    * the **signal** is how that fact is announced - visually, audibly, or by vibration.

    One condition can drive several signals, which is why they are separate objects rather
    than flags on one. Signal definitions preserve the current visual/audible defaults while
    allowing a profile to model the manifestation and latching of each one.
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
    # Whether another device may take this alarm's signals over. Sets
    # SignalDelegationSupported on every signal; without it a delegation is refused.
    delegable: bool = False
    signals: tuple[AlertSignalSpec, ...] = field(default_factory=_default_alert_signals)

    def __post_init__(self) -> None:
        # AlertKind and AlertPriority are the BICEPS enums, and those subclass str. Anything
        # that round-trips a value through a string - Qt's QVariant, a JSON file, a console
        # argument - hands back a plain str that looks right but is not the enum. Coerce
        # here so every entry point gets the same treatment.
        self.kind = _coerce_enum(AlertKind, self.kind, "kind")
        self.priority = _coerce_enum(AlertPriority, self.priority, "priority")
        self.signals = tuple(self.signals)
        if not self.signals:
            msg = "an alarm needs at least one signal"
            raise ValueError(msg)
        if any(not isinstance(signal, AlertSignalSpec) for signal in self.signals):
            msg = "signals must be AlertSignalSpec values"
            raise TypeError(msg)

        for name in ("lower_limit", "upper_limit"):
            limit = getattr(self, name)
            if limit is not None and not isinstance(limit, Decimal):
                msg = f"{name} must be a Decimal, never a float"
                raise TypeError(msg)
            if limit is not None:
                fixed_point_decimal(limit, name)
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


@dataclass(frozen=True)
class SignalInfo:
    """The live state of one signal announcing a condition."""

    handle: str
    manifestation: str
    presence: str
    location: str
    delegable: bool
    latching: bool = False

    @property
    def acknowledged(self) -> bool:
        """Whether the user has already acknowledged this signal."""
        return self.presence == AlertSignalPresence.ACK

    @property
    def delegated(self) -> bool:
        """Whether another device has taken this signal over."""
        return self.location == AlertSignalLocation.REMOTE

    @property
    def latched(self) -> bool:
        """Whether the signal is currently announcing a cleared condition."""
        return self.presence == AlertSignalPresence.LATCH

    def summary(self) -> str:
        """Short form for a table cell or a console line, e.g. 'Vis:Ack', 'Aud:On->Rem'.

        ASCII on purpose: this is printed by examples/console.py and by the test suites, and
        a Windows console on cp1252 cannot encode an arrow.
        """
        text = f"{self.manifestation}:{self.presence}"
        return f"{text}->Rem" if self.delegated else text


# Patient and location are BICEPS *contexts*: who and where, as opposed to what the device
# is measuring. They live in the SystemContext as multi-state entities, because a device may
# know about several and one of them is the associated one.
#
# The two are not symmetrical. A location is also published as a WS-Discovery scope, so a
# consumer can filter by it before connecting; a patient never leaves the MDIB.


@dataclass(frozen=True)
class LocationInfo:
    """Where the device is. Mirrors pm:LocationDetail."""

    facility: str = ""
    building: str = ""
    floor: str = ""
    point_of_care: str = ""
    room: str = ""
    bed: str = ""

    def is_empty(self) -> bool:
        """Whether nothing at all was given."""
        return not any(
            (self.facility, self.building, self.floor, self.point_of_care, self.room, self.bed),
        )

    def summary(self) -> str:
        """The parts that were filled in, in the hierarchy BICEPS orders them by."""
        parts = [
            self.facility,
            self.building,
            self.floor,
            self.point_of_care,
            self.room,
            self.bed,
        ]
        return " / ".join(part for part in parts if part)


@dataclass(frozen=True)
class PatientMeasurement:
    """One demographic Measurement: a Decimal value and its required coded unit."""

    value: Decimal
    unit: Coding

    def __post_init__(self) -> None:
        validate_decimal(self.value, "patient measurement value")
        if not isinstance(self.unit, Coding):
            msg = "patient measurement unit must be a Coding"
            raise TypeError(msg)

    def summary(self) -> str:
        """Short human representation using a label when the coding supplies one."""
        return f"{self.value} {self.unit.label or self.unit.code}"


@dataclass(frozen=True)
class PatientInfo:
    """Who the device is attached to. Mirrors pm:PatientDemographicsCoreData."""

    given_name: str = ""
    family_name: str = ""
    # 'Unspec', 'M', 'F' or 'Unkn' - pm:Sex. Empty leaves the element out.
    sex: str = ""
    # 'Unspec', 'Ad', 'Ado', 'Ped', 'Inf', 'Neo' or 'Oth' - pm:PatientType.
    patient_type: str = ""
    # An xsd:date, xsd:gYearMonth or xsd:gYear string, e.g. '1980-04-01'.
    date_of_birth: str = ""
    # Height and weight are BICEPS Measurements, each with a required coded unit.
    height: PatientMeasurement | None = None
    weight: PatientMeasurement | None = None
    # Race is a BICEPS CodedValue, not a free-text demographic field.
    race: Coding | None = None

    def __post_init__(self) -> None:
        for name in ("given_name", "family_name", "sex", "patient_type", "date_of_birth"):
            value = getattr(self, name)
            if not isinstance(value, str):
                msg = f"patient {name} must be a string"
                raise TypeError(msg)
            if not _xml_compatible(value):
                msg = f"patient {name} contains characters not allowed in XML 1.0"
                raise ValueError(msg)
        for name in ("height", "weight"):
            measurement = getattr(self, name)
            if measurement is not None and not isinstance(measurement, PatientMeasurement):
                msg = f"patient {name} must be a PatientMeasurement"
                raise TypeError(msg)
        if self.race is not None and not isinstance(self.race, Coding):
            msg = "patient race must be a Coding"
            raise TypeError(msg)

    def is_empty(self) -> bool:
        """Whether nothing at all was given."""
        return not any(
            (
                self.given_name,
                self.family_name,
                self.sex,
                self.patient_type,
                self.date_of_birth,
                self.height,
                self.weight,
                self.race,
            ),
        )

    def summary(self) -> str:
        """Something short enough for a status line."""
        name = " ".join(part for part in (self.given_name, self.family_name) if part)
        details = [part for part in (self.sex, self.patient_type, self.date_of_birth) if part]
        if self.height is not None:
            details.append(f"height {self.height.summary()}")
        if self.weight is not None:
            details.append(f"weight {self.weight.summary()}")
        if self.race is not None:
            details.append(f"race {self.race.label or self.race.code}")
        extras = ", ".join(details)
        if name and extras:
            return f"{name} ({extras})"
        return name or extras


# A visible, metric default makes a fresh toolbox useful for patient-context tests without
# requiring every user to enter the mandatory coded measurement units first.
DEFAULT_PATIENT = PatientInfo(
    given_name="Alex",
    family_name="Example",
    sex="Unspec",
    patient_type="Ad",
    date_of_birth="1990-01-01",
    height=PatientMeasurement(
        value=Decimal("170"),
        unit=Coding(code="264184", system="mdc", label="cm"),
    ),
    weight=PatientMeasurement(
        value=Decimal("70"),
        unit=Coding(code="266016", system="mdc", label="kg"),
    ),
)


def coding_from_biceps(coded_value: Any) -> Coding | None:
    """Convert a BICEPS CodedValue-like object into the toolbox model."""
    if coded_value is None:
        return None
    code = getattr(coded_value, "Code", None)
    if not code:
        return None
    label = ""
    for description in getattr(coded_value, "ConceptDescription", None) or []:
        text = getattr(description, "text", None)
        if text:
            label = str(text)
            break
    try:
        return Coding.from_wire(
            code=str(code),
            coding_system=getattr(coded_value, "CodingSystem", None),
            label=label,
        )
    except (TypeError, ValueError):
        return None


def patient_measurement_from_biceps(measurement: Any) -> PatientMeasurement | None:
    """Convert a BICEPS Measurement-like object when both required members are present."""
    if measurement is None:
        return None
    value = getattr(measurement, "MeasuredValue", None)
    unit = coding_from_biceps(getattr(measurement, "MeasurementUnit", None))
    if not isinstance(value, Decimal) or unit is None:
        return None
    return PatientMeasurement(value=value, unit=unit)


def patient_info_from_biceps(core_data: Any) -> PatientInfo:
    """Convert BICEPS PatientDemographicsCoreData into a stable toolbox value object."""
    if core_data is None:
        return PatientInfo()
    birth = getattr(core_data, "DateOfBirth", None)
    return PatientInfo(
        given_name=getattr(core_data, "Givenname", None) or "",
        family_name=getattr(core_data, "Familyname", None) or "",
        sex=str(getattr(core_data, "Sex", None) or ""),
        patient_type=str(getattr(core_data, "PatientType", None) or ""),
        date_of_birth=str(birth) if birth is not None else "",
        height=patient_measurement_from_biceps(getattr(core_data, "Height", None)),
        weight=patient_measurement_from_biceps(getattr(core_data, "Weight", None)),
        race=coding_from_biceps(getattr(core_data, "Race", None)),
    )


@dataclass
class ActionSpec:
    """Something the device *does*, as opposed to a value it holds.

    BICEPS calls this an ActivateOperation, and it is the difference between "set the zoom
    to 4" and "home the axes". A set operation names a metric and carries the value it
    should take; an activate operation names a target and means *do the thing*, with the
    device deciding what that involves.

    Real devices are full of them - home, park, calibrate, start coagulation, go to the
    fixpoint - and none of them is expressible as writing a number to a metric. Without
    them a robotic microscope modelled here would be a list of read-outs.

    ``effects`` is what this build does when one is invoked: a map of metric handle to the
    value it takes. That is a stand-in for machinery a real device would have, and it is
    deliberately visible rather than hidden, so what an action did is inspectable in the
    metrics it moved.
    """

    label: str
    # The descriptor the operation acts on. A real device usually points these at the
    # component being operated, so a Vmd or the Mds rather than a single metric.
    target_handle: str
    effects: dict[str, Decimal | str] = field(default_factory=dict)
    handle: str | None = None
    type_coding: Coding | None = None
    # Free text shown next to the button, for what the action is for.
    note: str = ""

    def __post_init__(self) -> None:
        if not self.label.strip():
            msg = "an action needs a label"
            raise ValueError(msg)
        if not str(self.target_handle).strip():
            msg = f"action {self.label!r} needs a target handle"
            raise ValueError(msg)
        for handle, value in self.effects.items():
            if isinstance(value, float):
                msg = f"action {self.label!r}: effect on {handle} must be a Decimal or str, never a float"
                raise TypeError(msg)
            if isinstance(value, Decimal):
                fixed_point_decimal(value, f"action {self.label!r}: effect on {handle}")

    @property
    def slug(self) -> str:
        """Slug used to build handles."""
        return slugify(self.label)

    def effective_type(self) -> Coding:
        """What this action is, as a code."""
        return self.type_coding or Coding(code=self.slug, system="private", label=self.label)

    def summary(self) -> str:
        """What invoking it will do, in a few words."""
        if not self.effects:
            return "reports success and changes nothing"
        return ", ".join(f"{handle} = {value}" for handle, value in sorted(self.effects.items()))


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


@dataclass(frozen=True)
class RemoteAction:
    """An ActivateOperation observed on a peer.

    Everything optional, as with RemoteMetric: a foreign device may publish an operation
    with no concept description and no readable code at all, and it still has to be listed
    rather than dropped.
    """

    handle: str
    label: str | None = None
    type_code: str | None = None
    target_handle: str | None = None
    enabled: bool = False

    @property
    def caption(self) -> str:
        """What to put on the button."""
        return self.label or self.type_code or self.handle


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
    # NumericMetricDescriptor/Resolution, which determines a numeric control's step size.
    resolution: Decimal | None = None
    # Limits the peer publishes. `minimum`/`maximum` come from the set operation's
    # AllowedRange when there is one, otherwise from the metric's TechnicalRange.
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    # The metric's own TechnicalRange, kept separately because it describes what the device
    # can produce rather than what we are allowed to ask for.
    technical_minimum: Decimal | None = None
    technical_maximum: Decimal | None = None
    value: object = None
    # A sample array reports many values per state instead of one. Empty for every other
    # kind, and also for a waveform whose peer has not sent a block yet.
    samples: tuple[Decimal, ...] = field(default_factory=tuple)
    # RealTimeSampleArrayMetricDescriptor/SamplePeriod, in seconds.
    sample_period: Decimal | None = None
    # DistributionSampleArrayMetricDescriptor/DomainUnit and /DistributionRange: what the
    # samples are spread over, as opposed to what each one measures.
    domain_unit_label: str | None = None
    domain_minimum: Decimal | None = None
    domain_maximum: Decimal | None = None
    parent_handle: str | None = None
    # Handles of kind-compatible set operations whose OperationTarget is this metric.
    operation_handles: tuple[str, ...] = field(default_factory=tuple)
    # The enabled, kind-compatible operation whose range is shown and which writes invoke.
    selected_operation_handle: str | None = None
    # True when at least one of those operations currently has OperatingMode == En.
    controllable_now: bool = False

    @property
    def controllable(self) -> bool:
        """Whether any operation targets this metric at all."""
        return bool(self.operation_handles)

    @property
    def is_sample_array(self) -> bool:
        """Whether one state of this metric carries many values rather than one."""
        return self.kind in SAMPLE_ARRAY_KINDS

    def domain_text(self) -> str:
        """The distribution's domain as something readable, e.g. '0 to 500 Hz'."""
        extent = format_range(self.domain_minimum, self.domain_maximum)
        return f"{extent} {self.domain_unit_label or ''}".strip()

    @property
    def has_range(self) -> bool:
        """Whether the peer publishes a limit we should respect."""
        return self.minimum is not None or self.maximum is not None

    def range_text(self) -> str:
        """The limits as something readable, or an empty string when unbounded."""
        return format_range(self.minimum, self.maximum)
