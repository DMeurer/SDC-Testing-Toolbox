"""Saving a configured virtual device to a file and building it again from one.

A config records descriptors, scalar current values, associated patient/location contexts,
and device metadata. It does not capture all live state. Loading one into a running provider
replaces tracked metrics, alerts, and actions by default, or appends them with ``replace=False``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.xml_structure import DateOfBirthProperty

from .constants import (
    ACTION_HANDLE_PREFIX,
    ALERT_HANDLE_PREFIX,
    ALERT_SYSTEM_HANDLE,
    CHANNEL_HANDLE,
    CHANNEL_HANDLE_PREFIX,
    LOCATION_CONTEXT_HANDLE,
    MDS_HANDLE,
    METRIC_HANDLE_PREFIX,
    OPERATION_HANDLE_PREFIX,
    PATIENT_CONTEXT_HANDLE,
    PRESET_DIR,
    SCO_HANDLE,
    SIGNAL_HANDLE_PREFIX,
    SYSTEM_CONTEXT_HANDLE,
    VMD_HANDLE,
    VMD_HANDLE_PREFIX,
)
from .model import (
    ActionSpec,
    AlertKind,
    AlertManifestation,
    AlertPriority,
    AlertSignalSpec,
    AlertSpec,
    Coding,
    DeviceInfo,
    DistributionShape,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    PatientMeasurement,
    WaveformShape,
    coerce_metric_value,
    patient_measurement_wire_value,
    slugify,
    validate_decimal,
)

if TYPE_CHECKING:
    from .provider_service import ProviderService

# Version 3 adds per-alert signal manifestation and latching settings. Earlier profiles remain
# readable because absent signal definitions retain the original visual/audible defaults.
CONFIG_VERSION = 3
LEGACY_CONFIG_VERSION = 1

FILE_SUFFIX = ".sdcprofile.json"

_TOP_LEVEL_KEYS = {"version", "name", "description", "device", "metrics", "alerts", "actions", "contexts"}
_METRIC_KEYS = {
    "handle",
    "label",
    "kind",
    "section",
    "unit",
    "type",
    "allowed_values",
    "resolution",
    "minimum",
    "maximum",
    "controllable",
    "initial_value",
    "sample_period",
    "shape",
    "cycle_samples",
    "domain_unit",
    "domain_minimum",
    "domain_maximum",
    "distribution_shape",
}
_ALERT_KEYS = {
    "handle",
    "label",
    "watches",
    "kind",
    "priority",
    "signals",
    "lower_limit",
    "upper_limit",
    "delegable",
}
_ACTION_KEYS = {"handle", "label", "target", "type", "note", "effects"}
_FIXED_DESCRIPTOR_HANDLES = {
    MDS_HANDLE,
    SCO_HANDLE,
    ALERT_SYSTEM_HANDLE,
    VMD_HANDLE,
    CHANNEL_HANDLE,
    SYSTEM_CONTEXT_HANDLE,
    PATIENT_CONTEXT_HANDLE,
    LOCATION_CONTEXT_HANDLE,
}


class ConfigError(Exception):
    """A config file could not be read, or does not describe a usable device."""


_JSON_TYPE_NAMES = {
    dict: "object",
    list: "array",
    str: "string",
    bool: "boolean",
    int: "integer",
}


def _expect_type(value: Any, expected: type, field: str) -> Any:
    """Require an exact JSON type, without treating booleans as integers."""
    if type(value) is not expected:
        msg = f"{field}: expected a JSON {_JSON_TYPE_NAMES[expected]}, found {type(value).__name__}"
        raise ConfigError(msg)
    return value


def _check_keys(data: dict[str, Any], allowed: set[str], field: str) -> None:
    """Reject unsupported keys in one persisted object."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        msg = f"{field}: does not understand {', '.join(unknown)}. It takes: {', '.join(sorted(allowed))}"
        raise ConfigError(msg)


def _check_types(data: dict[str, Any], schema: dict[str, type], field: str) -> None:
    """Validate exact types for fields present in one persisted object."""
    for name, expected in schema.items():
        if name in data:
            _expect_type(data[name], expected, f"{field}.{name}")


@dataclass(frozen=True)
class DeviceConfig:
    """Everything a config file describes.

    A dataclass rather than a tuple because there are now four parts and a caller reading
    ``metrics, alerts, location, patient = parse(...)`` would have to get the order right
    every time.

    ``location`` and ``patient`` are None when the file does not mention them at all, which
    is not the same as an empty one: the first leaves the device's current context alone,
    the second would replace it.
    """

    metrics: list[MetricSpec] = field(default_factory=list)
    alerts: list[AlertSpec] = field(default_factory=list)
    actions: list[ActionSpec] = field(default_factory=list)
    location: LocationInfo | None = None
    patient: PatientInfo | None = None
    # Who the device claims to be. Applied when the provider is constructed, not on import:
    # sdc11073 fixes ThisModel and ThisDevice at that point.
    device: DeviceInfo | None = None
    instance_name: str = ""


@dataclass(frozen=True)
class Preset:
    """One ready-made config found in the presets folder."""

    path: Path
    name: str
    description: str
    metrics: int
    alerts: int

    def summary(self) -> str:
        """What the file contains, for a menu entry or a dropdown."""
        parts = [f"{self.metrics} data source(s)", f"{self.alerts} alarm(s)"]
        return ", ".join(parts)


def list_presets(directory: str | Path | None = None) -> list[Preset]:
    """Every usable config in the presets folder, in name order.

    A file that will not parse is left out rather than raising: one broken preset must not
    stop the menu being built. Whoever wants the reason can open it with Import config, which
    reports it properly.
    """
    folder = Path(directory) if directory is not None else PRESET_DIR
    if not folder.is_dir():
        return []

    found = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
            device = parse(data)
        except (OSError, UnicodeError, ValueError, RecursionError, ConfigError):
            continue
        found.append(
            Preset(
                path=path,
                name=str(data.get("name") or path.stem),
                description=str(data.get("description") or ""),
                metrics=len(device.metrics),
                alerts=len(device.alerts),
            ),
        )
    found.sort(key=lambda preset: preset.name.lower())
    return found


def _decimal_or_none(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float):
        msg = f"{field}: use a decimal string or Decimal, never float"
        raise ConfigError(msg)
    if type(value) not in (str, int, Decimal):
        msg = f"{field}: expected a JSON number or decimal string, found {type(value).__name__}"
        raise ConfigError(msg)
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        msg = f"{field}: {value!r} is not a number"
        raise ConfigError(msg) from exc
    try:
        validate_decimal(result, field)
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc
    return result


def _enum_or_default(enum_cls: Any, value: Any, default: Any, field: str) -> Any:
    if value is None:
        return default
    _expect_type(value, str, field)
    try:
        return enum_cls(value)
    except (ValueError, RecursionError) as exc:
        allowed = ", ".join(member.value for member in enum_cls)
        msg = f"{field}: {value!r} is not one of {allowed}"
        raise ConfigError(msg) from exc


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


def _coding_to_json(coding: Coding | None, label: str) -> Any:
    """A coding as the shortest thing that still says what it means.

    A bare string when there is no code worth recording, so simple presets stay readable;
    an object once semantics are involved, because a label alone is not semantics.
    """
    if coding is None:
        return label or None
    entry: dict[str, Any] = {"code": coding.code, "system": coding.system}
    if label or coding.label:
        entry["label"] = label or coding.label
    return entry


def _coding_from_json(raw: Any, field: str) -> tuple[Coding | None, str]:
    """Read either shape back, returning the coding and its label."""
    if raw is None:
        return None, ""
    if isinstance(raw, str):
        return None, raw
    if not isinstance(raw, dict):
        msg = f"{field}: expected a string or an object, found {type(raw).__name__}"
        raise ConfigError(msg)
    _check_keys(raw, {"code", "system", "label"}, field)
    _check_types(raw, {"code": str, "system": str, "label": str}, field)
    label = raw.get("label") or ""
    if "code" not in raw:
        return None, label
    try:
        return Coding(
            code=raw["code"],
            system=raw.get("system") or "private",
            label=label,
        ), label
    except (TypeError, ValueError) as exc:
        msg = f"{field}: {exc}"
        raise ConfigError(msg) from exc


def metric_to_dict(handle: str, spec: MetricSpec) -> dict[str, Any]:
    """One data source as plain JSON types."""
    entry: dict[str, Any] = {
        "handle": handle,
        "label": spec.label,
        "kind": spec.kind.value,
        "controllable": spec.controllable,
    }
    if spec.section:
        entry["section"] = spec.section
    unit = _coding_to_json(spec.unit_coding, spec.unit_label)
    if unit is not None:
        entry["unit"] = unit
    if spec.type_coding is not None:
        entry["type"] = _coding_to_json(spec.type_coding, "")
    if spec.allowed_values:
        entry["allowed_values"] = list(spec.allowed_values)
    if spec.resolution is not None:
        entry["resolution"] = str(spec.resolution)
    if spec.minimum is not None:
        entry["minimum"] = str(spec.minimum)
    if spec.maximum is not None:
        entry["maximum"] = str(spec.maximum)
    if spec.initial_value is not None:
        entry["initial_value"] = str(spec.initial_value)
    if spec.kind is MetricKind.WAVEFORM:
        entry["sample_period"] = str(spec.sample_period)
        entry["shape"] = spec.shape.value
        entry["cycle_samples"] = spec.cycle_samples
    if spec.kind is MetricKind.DISTRIBUTION:
        domain_unit = _coding_to_json(spec.domain_unit_coding, spec.domain_unit_label)
        if domain_unit is not None:
            entry["domain_unit"] = domain_unit
        entry["domain_minimum"] = str(spec.domain_minimum)
        entry["domain_maximum"] = str(spec.domain_maximum)
        entry["distribution_shape"] = spec.distribution_shape.value
    return entry


def alert_to_dict(handle: str, spec: AlertSpec) -> dict[str, Any]:
    """One alarm as plain JSON types."""
    entry: dict[str, Any] = {
        "handle": handle,
        "label": spec.label,
        "watches": spec.source_handle,
        "kind": spec.kind.value,
        "priority": spec.priority.value,
        "signals": [
            {"manifestation": signal.manifestation.value, "latching": signal.latching}
            for signal in spec.signals
        ],
    }
    if spec.lower_limit is not None:
        entry["lower_limit"] = str(spec.lower_limit)
    if spec.upper_limit is not None:
        entry["upper_limit"] = str(spec.upper_limit)
    if spec.delegable:
        entry["delegable"] = True
    return entry


def action_to_dict(handle: str, spec: ActionSpec) -> dict[str, Any]:
    """One action as plain JSON types."""
    entry: dict[str, Any] = {
        "handle": handle,
        "label": spec.label,
        "target": spec.target_handle,
    }
    if spec.type_coding is not None:
        entry["type"] = _coding_to_json(spec.type_coding, "")
    if spec.note:
        entry["note"] = spec.note
    if spec.effects:
        entry["effects"] = {h: str(v) for h, v in sorted(spec.effects.items())}
    return entry


def action_from_dict(entry: dict[str, Any], metrics: dict[str, MetricSpec]) -> ActionSpec:
    """Rebuild one action. Raises ConfigError on anything unusable."""
    if not isinstance(entry, dict):
        msg = f"actions: expected an object, found {type(entry).__name__}"
        raise ConfigError(msg)
    label = entry.get("label")
    field = f"actions[{label}]" if type(label) is str and label else "actions"
    _check_keys(entry, _ACTION_KEYS, field)
    _check_types(
        entry,
        {"handle": str, "label": str, "target": str, "note": str, "effects": dict},
        field,
    )
    if not label:
        msg = "actions: an entry has no label"
        raise ConfigError(msg)
    target = entry.get("target")
    if not target:
        msg = f"actions[{label}]: no target to act on"
        raise ConfigError(msg)

    raw_effects = entry.get("effects", {})
    effects: dict[str, Any] = {}
    for handle, value in raw_effects.items():
        effect_handle = _expect_type(handle, str, f"actions[{label}].effects key")
        _expect_type(value, str, f"actions[{label}].effects[{effect_handle}]")
        metric = metrics.get(effect_handle)
        if metric is None:
            # Append-mode imports may refer to a metric already in the provider. Preflight
            # resolves those once it has the complete prospective metric set.
            effects[effect_handle] = value
            continue
        try:
            effects[effect_handle] = coerce_metric_value(metric, value, effect_handle)
        except (TypeError, ValueError) as exc:
            msg = f"actions[{label}].effects[{effect_handle}]: {exc}"
            raise ConfigError(msg) from exc

    type_coding, _ = _coding_from_json(entry.get("type"), f"actions[{label}].type")
    try:
        return ActionSpec(
            label=label,
            target_handle=target,
            effects=effects,
            handle=entry.get("handle") or None,
            type_coding=type_coding,
            note=entry.get("note") or "",
        )
    except (ValueError, TypeError) as exc:
        msg = f"actions[{label}]: {exc}"
        raise ConfigError(msg) from exc


def _context_to_dict(service: ProviderService) -> dict[str, Any]:
    """Patient and location, omitted entirely when neither was filled in.

    Location is always present in practice, since every provider publishes a default one as
    a discovery scope, but a config that only describes data sources should not grow a
    patient block full of empty strings.
    """
    contexts: dict[str, Any] = {}
    location = service.get_location()
    if not location.is_empty():
        contexts["location"] = {
            key: value
            for key, value in {
                "facility": location.facility,
                "building": location.building,
                "floor": location.floor,
                "point_of_care": location.point_of_care,
                "room": location.room,
                "bed": location.bed,
            }.items()
            if value
        }
    patient = service.get_patient()
    if not patient.is_empty():
        patient_data = {
            key: value
            for key, value in {
                "given_name": patient.given_name,
                "family_name": patient.family_name,
                "sex": patient.sex,
                "patient_type": patient.patient_type,
                "date_of_birth": patient.date_of_birth,
            }.items()
            if value
        }
        for name in ("height", "weight"):
            measurement = getattr(patient, name)
            if measurement is not None:
                patient_data[name] = {
                    "value": str(measurement.value),
                    "unit": _coding_to_json(measurement.unit, ""),
                }
        if patient.race is not None:
            patient_data["race"] = _coding_to_json(patient.race, "")
        contexts["patient"] = patient_data
    return contexts


def to_dict(service: ProviderService, *, include_values: bool = True) -> dict[str, Any]:
    """Capture a running provider's configuration.

    :param include_values: record each metric's current value as its initial_value, so that
        reloading the file reproduces what is on screen rather than an empty device.
    """
    metrics = []
    for handle, spec in sorted(service.list_metrics().items()):
        entry = metric_to_dict(handle, spec)
        # A sample array's samples are generated or pushed, not configured, so recording
        # a block of them as an initial value would be recording noise.
        if include_values and not spec.is_sample_array:
            current = service.get_value(handle)
            if current is not None:
                entry["initial_value"] = str(current)
            else:
                entry.pop("initial_value", None)
        metrics.append(entry)

    payload: dict[str, Any] = {
        "version": CONFIG_VERSION,
        "device": {
            "instance_name": service.instance_name,
            "friendly_name": service.friendly_name,
            "manufacturer": service.device.manufacturer,
            "manufacturer_url": service.device.manufacturer_url,
            "model_name": service.device.model_name,
            "model_number": service.device.model_number,
            "firmware_version": service.device.firmware_version,
        },
        "metrics": metrics,
        "alerts": [alert_to_dict(handle, spec) for handle, spec in sorted(service.list_alerts().items())],
        "actions": [action_to_dict(handle, spec) for handle, spec in sorted(service.list_actions().items())],
    }
    contexts = _context_to_dict(service)
    if contexts:
        payload["contexts"] = contexts
    return payload


def save(service: ProviderService, path: str | Path, *, include_values: bool = True) -> Path:
    """Write a running provider's configuration to a file."""
    target = Path(path)
    target.write_text(
        json.dumps(to_dict(service, include_values=include_values), indent=2) + "\n",
        encoding="utf-8",
    )
    return target


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def metric_from_dict(entry: dict[str, Any]) -> MetricSpec:
    """Rebuild one data source definition. Raises ConfigError on anything unusable."""
    if not isinstance(entry, dict):
        msg = f"metrics: expected an object, found {type(entry).__name__}"
        raise ConfigError(msg)

    label = entry.get("label")
    field = f"metrics[{label}]" if type(label) is str and label else "metrics"
    _check_keys(entry, _METRIC_KEYS, field)
    _check_types(
        entry,
        {
            "handle": str,
            "label": str,
            "kind": str,
            "section": str,
            "allowed_values": list,
            "controllable": bool,
            "shape": str,
            "cycle_samples": int,
            "distribution_shape": str,
        },
        field,
    )
    if not label:
        msg = "metrics: an entry has no label"
        raise ConfigError(msg)

    kind = _enum_or_default(MetricKind, entry.get("kind"), None, f"metrics[{label}].kind")
    if kind is None:
        msg = f"metrics[{label}]: no kind given"
        raise ConfigError(msg)
    initial = entry.get("initial_value")
    if initial is not None and kind is MetricKind.NUMBER:
        initial = _decimal_or_none(initial, f"metrics[{label}].initial_value")
    elif initial is not None:
        initial = _expect_type(initial, str, f"metrics[{label}].initial_value")

    allowed_values = entry.get("allowed_values", [])
    for index, value in enumerate(allowed_values):
        _expect_type(value, str, f"metrics[{label}].allowed_values[{index}]")

    unit_coding, unit_label = _coding_from_json(entry.get("unit"), f"metrics[{label}].unit")
    type_coding, _ = _coding_from_json(entry.get("type"), f"metrics[{label}].type")
    domain_coding, domain_label = _coding_from_json(
        entry.get("domain_unit"),
        f"metrics[{label}].domain_unit",
    )

    try:
        return MetricSpec(
            label=label,
            kind=kind,
            unit_label=unit_label,
            unit_coding=unit_coding,
            type_coding=type_coding,
            section=entry.get("section") or "",
            allowed_values=tuple(allowed_values),
            resolution=_decimal_or_none(entry.get("resolution"), f"metrics[{label}].resolution"),
            minimum=_decimal_or_none(entry.get("minimum"), f"metrics[{label}].minimum"),
            maximum=_decimal_or_none(entry.get("maximum"), f"metrics[{label}].maximum"),
            controllable=entry.get("controllable", False),
            handle=entry.get("handle") or None,
            initial_value=initial,
            sample_period=_decimal_or_none(entry.get("sample_period"), f"metrics[{label}].sample_period"),
            shape=_enum_or_default(
                WaveformShape,
                entry.get("shape"),
                WaveformShape.SINE,
                f"metrics[{label}].shape",
            ),
            cycle_samples=entry.get("cycle_samples", 40),
            distribution_shape=_enum_or_default(
                DistributionShape,
                entry.get("distribution_shape"),
                DistributionShape.BELL,
                f"metrics[{label}].distribution_shape",
            ),
            domain_unit_label=domain_label,
            domain_unit_coding=domain_coding,
            domain_minimum=_decimal_or_none(entry.get("domain_minimum"), f"metrics[{label}].domain_minimum"),
            domain_maximum=_decimal_or_none(entry.get("domain_maximum"), f"metrics[{label}].domain_maximum"),
        )
    except (ValueError, TypeError) as exc:
        msg = f"metrics[{label}]: {exc}"
        raise ConfigError(msg) from exc


def _alert_signal_from_dict(raw: Any, field: str) -> AlertSignalSpec:
    if not isinstance(raw, dict):
        msg = f"{field}: expected an object"
        raise ConfigError(msg)
    _check_keys(raw, {"manifestation", "latching"}, field)
    _check_types(raw, {"manifestation": str, "latching": bool}, field)
    if "manifestation" not in raw:
        msg = f"{field}: needs a manifestation"
        raise ConfigError(msg)
    latching = raw.get("latching", False)
    try:
        return AlertSignalSpec(
            manifestation=_enum_or_default(AlertManifestation, raw["manifestation"], None, f"{field}.manifestation"),
            latching=latching,
        )
    except (TypeError, ValueError) as exc:
        msg = f"{field}: {exc}"
        raise ConfigError(msg) from exc


def alert_from_dict(entry: dict[str, Any]) -> AlertSpec:
    """Rebuild one alarm definition. Raises ConfigError on anything unusable."""
    if not isinstance(entry, dict):
        msg = f"alerts: expected an object, found {type(entry).__name__}"
        raise ConfigError(msg)

    label = entry.get("label")
    field = f"alerts[{label}]" if type(label) is str and label else "alerts"
    _check_keys(entry, _ALERT_KEYS, field)
    _check_types(
        entry,
        {
            "handle": str,
            "label": str,
            "watches": str,
            "kind": str,
            "priority": str,
            "signals": list,
            "delegable": bool,
        },
        field,
    )
    if not label:
        msg = "alerts: an entry has no label"
        raise ConfigError(msg)
    source = entry.get("watches")
    if not source:
        msg = f"alerts[{label}]: no metric to watch"
        raise ConfigError(msg)
    signals = None
    if "signals" in entry:
        raw_signals = entry["signals"]
        signals = tuple(
            _alert_signal_from_dict(raw, f"alerts[{label}].signals[{index}]")
            for index, raw in enumerate(raw_signals)
        )

    try:
        return AlertSpec(
            label=label,
            source_handle=source,
            kind=_enum_or_default(AlertKind, entry.get("kind"), AlertKind.TECHNICAL, f"alerts[{label}].kind"),
            priority=_enum_or_default(
                AlertPriority,
                entry.get("priority"),
                AlertPriority.MEDIUM,
                f"alerts[{label}].priority",
            ),
            lower_limit=_decimal_or_none(entry.get("lower_limit"), f"alerts[{label}].lower_limit"),
            upper_limit=_decimal_or_none(entry.get("upper_limit"), f"alerts[{label}].upper_limit"),
            handle=entry.get("handle") or None,
            delegable=entry.get("delegable", False),
            **({"signals": signals} if signals is not None else {}),
        )
    except (ValueError, TypeError) as exc:
        msg = f"alerts[{label}]: {exc}"
        raise ConfigError(msg) from exc


def _device_from_dict(data: Any) -> tuple[DeviceInfo | None, str]:
    """Read the device block, which used to be written and never read back."""
    if data is None:
        return None, ""
    if not isinstance(data, dict):
        msg = f"device: expected an object, found {type(data).__name__}"
        raise ConfigError(msg)
    known = {f.name for f in fields(DeviceInfo)} | {"instance_name"}
    _check_keys(data, known, "device")
    _check_types(data, {name: str for name in known}, "device")
    values = {key: value for key, value in data.items() if key != "instance_name"}
    return DeviceInfo(**values), data.get("instance_name") or ""


def _required_coding_from_json(raw: Any, field: str) -> Coding:
    """Read one required CodedValue without accepting a display-only string."""
    if not isinstance(raw, dict):
        msg = f"{field}: expected an object with code and system"
        raise ConfigError(msg)
    _check_keys(raw, {"code", "system", "label"}, field)
    _check_types(raw, {"code": str, "system": str, "label": str}, field)
    if "code" not in raw:
        msg = f"{field}: needs a code"
        raise ConfigError(msg)
    if "system" not in raw:
        msg = f"{field}: needs a coding system"
        raise ConfigError(msg)
    if not raw["code"].strip():
        msg = f"{field}: needs a code"
        raise ConfigError(msg)
    if not raw["system"].strip():
        msg = f"{field}: needs a coding system"
        raise ConfigError(msg)
    try:
        return Coding(
            code=raw["code"],
            system=raw["system"],
            label=raw.get("label") or "",
        )
    except (TypeError, ValueError) as exc:
        msg = f"{field}: {exc}"
        raise ConfigError(msg) from exc


def _patient_measurement_from_dict(raw: Any, field: str) -> PatientMeasurement:
    """Read a BICEPS Measurement from its JSON value/unit representation."""
    if not isinstance(raw, dict):
        msg = f"{field}: expected an object with value and unit"
        raise ConfigError(msg)
    _check_keys(raw, {"value", "unit"}, field)
    if "value" not in raw:
        msg = f"{field}: needs a value"
        raise ConfigError(msg)
    if "unit" not in raw:
        msg = f"{field}: needs a unit"
        raise ConfigError(msg)
    raw_value = raw["value"]
    value = _decimal_or_none(raw_value, f"{field}.value")
    if value is None:
        msg = f"{field}: needs a value"
        raise ConfigError(msg)
    try:
        patient_measurement_wire_value(value)
        return PatientMeasurement(
            value=value,
            unit=_required_coding_from_json(raw["unit"], f"{field}.unit"),
        )
    except (TypeError, ValueError) as exc:
        msg = f"{field}: {exc}"
        raise ConfigError(msg) from exc


def _patient_from_dict(raw: Any) -> PatientInfo | None:
    """Read the optional patient block, including BICEPS Measurements and CodedValue."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        msg = f"contexts.patient: expected an object, found {type(raw).__name__}"
        raise ConfigError(msg)
    simple = {"given_name", "family_name", "sex", "patient_type", "date_of_birth"}
    allowed = simple | {"height", "weight", "race"}
    _check_keys(raw, allowed, "contexts.patient")
    _check_types(raw, {name: str for name in simple}, "contexts.patient")
    try:
        return PatientInfo(
            **{name: raw[name] for name in simple if name in raw},
            height=(
                _patient_measurement_from_dict(raw["height"], "contexts.patient.height")
                if "height" in raw
                else None
            ),
            weight=(
                _patient_measurement_from_dict(raw["weight"], "contexts.patient.weight")
                if "weight" in raw
                else None
            ),
            race=(
                _required_coding_from_json(raw["race"], "contexts.patient.race")
                if "race" in raw
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        msg = f"contexts.patient: {exc}"
        raise ConfigError(msg) from exc


def _contexts_from_dict(data: Any) -> tuple[LocationInfo | None, PatientInfo | None]:
    """Rebuild the patient and location contexts, if the file carries any.

    None means "the file says nothing", which is different from an empty block: the first
    leaves whatever the device already had, the second would clear it.
    """
    if data is None:
        return None, None
    if not isinstance(data, dict):
        msg = f"contexts: expected an object, found {type(data).__name__}"
        raise ConfigError(msg)
    _check_keys(data, {"location", "patient"}, "contexts")
    _check_types(data, {"location": dict, "patient": dict}, "contexts")

    def block(name: str, cls: Any) -> Any:
        raw = data.get(name)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            msg = f"contexts.{name}: expected an object, found {type(raw).__name__}"
            raise ConfigError(msg)
        allowed = {f.name for f in fields(cls)}
        field = f"contexts.{name}"
        _check_keys(raw, allowed, field)
        _check_types(raw, {key: str for key in allowed}, field)
        try:
            return cls(**raw)
        except (TypeError, ValueError) as exc:
            msg = f"{field}: {exc}"
            raise ConfigError(msg) from exc

    return block("location", LocationInfo), _patient_from_dict(data.get("patient"))


def parse(data: Any) -> DeviceConfig:
    """Turn a decoded config into specs, treating an omitted version as legacy version 1."""
    if not isinstance(data, dict):
        msg = f"expected an object at the top level, found {type(data).__name__}"
        raise ConfigError(msg)
    _check_keys(data, _TOP_LEVEL_KEYS, "top level")
    _check_types(
        data,
        {
            "version": int,
            "name": str,
            "description": str,
            "device": dict,
            "metrics": list,
            "alerts": list,
            "actions": list,
            "contexts": dict,
        },
        "top level",
    )

    version = data.get("version", LEGACY_CONFIG_VERSION)
    if not LEGACY_CONFIG_VERSION <= version <= CONFIG_VERSION:
        msg = (
            f"top level.version: unsupported config version {version}; "
            f"supported versions are {LEGACY_CONFIG_VERSION} through {CONFIG_VERSION}"
        )
        raise ConfigError(msg)

    device, instance_name = _device_from_dict(data.get("device"))
    metrics = [metric_from_dict(entry) for entry in data.get("metrics", [])]
    alerts = [alert_from_dict(entry) for entry in data.get("alerts", [])]
    descriptors = _FIXED_DESCRIPTOR_HANDLES | _section_handles(metrics)
    metric_handles = _resolve_metric_handles(metrics, descriptors)
    actions = [action_from_dict(entry, metric_handles) for entry in data.get("actions", [])]
    location, patient = _contexts_from_dict(data.get("contexts"))

    # Catch a dangling reference here rather than half way through building the device.
    defined = set(metric_handles)
    for alert in alerts:
        if alert.source_handle not in defined:
            known = ", ".join(sorted(defined)) or "none"
            msg = (
                f"alerts[{alert.label}]: watches {alert.source_handle!r}, "
                f"which this file does not define. It defines: {known}"
            )
            raise ConfigError(msg)

    return DeviceConfig(
        metrics=metrics,
        alerts=alerts,
        actions=actions,
        location=location,
        patient=patient,
        device=device,
        instance_name=instance_name,
    )


def load_file(path: str | Path) -> DeviceConfig:
    """Read and validate a config file without touching any provider."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        msg = f"cannot read {source}: {exc}"
        raise ConfigError(msg) from exc
    try:
        data = json.loads(text, parse_float=Decimal)
    except ValueError as exc:
        msg = f"{source} is not valid JSON: {exc}"
        raise ConfigError(msg) from exc
    return parse(data)


def _unique_in(handles: set[str], candidate: str) -> str:
    """Mirror ProviderService's handle allocation without touching the live MDIB."""
    if candidate not in handles:
        return candidate
    counter = 2
    while f"{candidate}.{counter}" in handles:
        counter += 1
    return f"{candidate}.{counter}"


def _claim_handle(handles: set[str], explicit: str | None, generated: str, field: str) -> str:
    handle = explicit or _unique_in(handles, generated)
    if explicit and handle in handles:
        msg = f"{field}: handle {handle!r} already exists"
        raise ConfigError(msg)
    handles.add(handle)
    return handle


def _section_handles(metrics: list[MetricSpec]) -> set[str]:
    handles = set()
    for spec in metrics:
        if spec.section:
            section_slug = slugify(spec.section)
            handles.add(VMD_HANDLE_PREFIX + section_slug)
            handles.add(CHANNEL_HANDLE_PREFIX + section_slug)
    return handles


def _resolve_metric_handles(
    metrics: list[MetricSpec],
    descriptors: set[str],
) -> dict[str, MetricSpec]:
    """Claim profile metric handles in declaration order without suffixing collisions."""
    resolved: list[tuple[MetricSpec, str]] = []
    metrics_by_handle: dict[str, MetricSpec] = {}
    for spec in metrics:
        implicit = spec.handle is None
        handle = spec.handle or (METRIC_HANDLE_PREFIX + spec.slug)
        if handle in descriptors:
            if implicit and handle in metrics_by_handle:
                msg = (
                    f"metrics[{spec.label}]: implicit handle {handle!r} duplicates another metric; "
                    "give metrics with duplicate labels explicit unique handles"
                )
            else:
                msg = f"metrics[{spec.label}]: handle {handle!r} already exists"
            raise ConfigError(msg)
        descriptors.add(handle)
        metrics_by_handle[handle] = spec
        resolved.append((spec, handle))
        if spec.controllable:
            _claim_handle(
                descriptors,
                None,
                OPERATION_HANDLE_PREFIX + handle.removeprefix(METRIC_HANDLE_PREFIX),
                f"metrics[{spec.label}] control",
            )

    for spec, handle in resolved:
        spec.handle = handle
    return metrics_by_handle


def _preflight_apply(service: ProviderService, device: DeviceConfig, *, replace: bool) -> None:
    """Validate the final descriptor graph before changing any live descriptors.

    This is intentionally a pure namespace simulation. Calling the provider's creation
    methods to validate references would create sections and defeat the point of preflight.
    """
    descriptors = {handle for handle, _ in service.mdib.entities.items()}
    metric_specs = service.list_metrics()
    metrics = set(metric_specs)
    removed: set[str] = set()
    if replace:
        removed = set(service.list_actions()) | set(service.list_alerts()) | set(service.list_metrics())
        for alert_handle in service.list_alerts():
            removed.update(service.signal_handles_for(alert_handle))
        for metric_handle in service.list_metrics():
            operation_handle = service.operation_handle_for(metric_handle)
            if operation_handle is not None:
                removed.add(operation_handle)
        removed.update(service._section_descriptor_handles())  # noqa: SLF001 - simulate replacement cleanup
        descriptors.difference_update(removed)
        metric_specs.clear()
        metrics.clear()

    for section in {spec.section for spec in device.metrics if spec.section}:
        slug = slugify(section)
        vmd_handle = VMD_HANDLE_PREFIX + slug
        channel_handle = CHANNEL_HANDLE_PREFIX + slug
        vmd = service.mdib.entities.by_handle(vmd_handle)
        channel = service.mdib.entities.by_handle(channel_handle)
        if vmd_handle in removed:
            vmd = None
        if channel_handle in removed:
            channel = None
        if vmd is not None and (vmd.node_type != pm.VmdDescriptor or vmd.parent_handle != MDS_HANDLE):
            msg = f"sections[{section}]: {vmd_handle!r} must be a VmdDescriptor under {MDS_HANDLE!r}"
            raise ConfigError(msg)
        if channel is not None and (
            channel.node_type != pm.ChannelDescriptor or channel.parent_handle != vmd_handle
        ):
            msg = f"sections[{section}]: {channel_handle!r} must be a ChannelDescriptor under {vmd_handle!r}"
            raise ConfigError(msg)
        if channel is not None and vmd is None:
            msg = f"sections[{section}]: {channel_handle!r} requires live parent {vmd_handle!r}"
            raise ConfigError(msg)

    descriptors.update(_section_handles(device.metrics))
    for spec in device.metrics:
        if spec.handle is None:
            msg = f"metrics[{spec.label}]: metric handle was not resolved during parsing"
            raise ConfigError(msg)
        handle = _claim_handle(descriptors, spec.handle, "", f"metrics[{spec.label}]")
        metrics.add(handle)
        metric_specs[handle] = spec
        if spec.controllable:
            _claim_handle(
                descriptors,
                None,
                OPERATION_HANDLE_PREFIX + handle.removeprefix(METRIC_HANDLE_PREFIX),
                f"metrics[{spec.label}] control",
            )

    for spec in device.alerts:
        if spec.source_handle not in metrics:
            msg = f"alerts[{spec.label}]: watches {spec.source_handle!r}, which is not a metric available after import"
            raise ConfigError(msg)
        handle = _claim_handle(descriptors, spec.handle, ALERT_HANDLE_PREFIX + spec.slug, f"alerts[{spec.label}]")
        for signal in spec.signals:
            _claim_handle(
                descriptors,
                None,
                SIGNAL_HANDLE_PREFIX + spec.slug + "." + signal.manifestation.value.lower(),
                f"alerts[{spec.label}] signal",
            )

    normalized_effects: list[tuple[ActionSpec, dict[str, Decimal | str]]] = []
    for spec in device.actions:
        if spec.target_handle not in descriptors:
            msg = f"actions[{spec.label}]: target {spec.target_handle!r} is not available after import"
            raise ConfigError(msg)
        for effect_handle in spec.effects:
            if effect_handle not in metrics:
                msg = f"actions[{spec.label}].effects: {effect_handle!r} is not a metric available after import"
                raise ConfigError(msg)
        effects: dict[str, Decimal | str] = {}
        for effect_handle, value in spec.effects.items():
            try:
                effects[effect_handle] = coerce_metric_value(
                    metric_specs[effect_handle],
                    value,
                    effect_handle,
                )
            except (TypeError, ValueError) as exc:
                msg = f"actions[{spec.label}].effects[{effect_handle}]: {exc}"
                raise ConfigError(msg) from exc
        normalized_effects.append((spec, effects))
        _claim_handle(descriptors, spec.handle, ACTION_HANDLE_PREFIX + spec.slug, f"actions[{spec.label}]")

    if device.location is not None and device.location.is_empty():
        msg = "contexts.location: an explicit location needs at least one detail"
        raise ConfigError(msg)
    if device.patient is not None:
        try:
            if device.patient.sex:
                pm_types.Sex(device.patient.sex)
            if device.patient.patient_type:
                pm_types.PatientType(device.patient.patient_type)
            if device.patient.date_of_birth:
                DateOfBirthProperty.mk_value_object(device.patient.date_of_birth)
        except (TypeError, ValueError) as exc:
            msg = f"contexts.patient: {exc}"
            raise ConfigError(msg) from exc

    for spec, effects in normalized_effects:
        spec.effects = effects


def apply_to(
    service: ProviderService,
    device: DeviceConfig,
    *,
    replace: bool = True,
) -> tuple[int, int]:
    """Build the described device on a running provider.

    Metrics go in before alarms, since an alarm names the metric it watches. Returns how
    many of each were created.

    :param replace: clear tracked descriptors first. Without it, append to the live graph;
        collisions are rejected during preflight. Operational failures in either mode are
        compensated back to the pre-import graph.
    """
    _preflight_apply(service, device, replace=replace)
    try:
        snapshot = service._snapshot_configuration()  # noqa: SLF001 - config owns import rollback
    except Exception as exc:  # noqa: BLE001 - provider/library failures become profile errors
        msg = f"profile: could not prepare transactional import: {exc}"
        raise ConfigError(msg) from exc

    field = "profile"
    touched_contexts: set[str] = set()
    try:
        if replace:
            for handle in list(service.list_actions()):
                field = f"profile.replace.actions[{handle}]"
                service.remove_action(handle)
            for handle in list(service.list_alerts()):
                field = f"profile.replace.alerts[{handle}]"
                service.remove_alert(handle)
            for handle in list(service.list_metrics()):
                field = f"profile.replace.metrics[{handle}]"
                service.remove_metric(handle)
            service._remove_empty_sections()  # noqa: SLF001 - replacement owns containment cleanup

        for spec in device.metrics:
            field = f"metrics[{spec.label}]"
            service.add_metric(spec)
        created_alerts = 0
        for spec in device.alerts:
            field = f"alerts[{spec.label}]"
            service.add_alert(spec)
            created_alerts += 1

        # Actions last of the descriptors: one names the thing it acts on, and a section's
        # Vmd only exists once a metric has put it there.
        for action in device.actions:
            field = f"actions[{action.label}]"
            service.add_action(action)

        # Contexts last, and only when the file mentions them. A file that says nothing about
        # a patient leaves the one already attached alone rather than silently detaching them.
        if device.location is not None:
            field = "contexts.location"
            touched_contexts.add(LOCATION_CONTEXT_HANDLE)
            service.set_location(device.location)
        if device.patient is not None:
            field = "contexts.patient"
            touched_contexts.add(PATIENT_CONTEXT_HANDLE)
            service.set_patient(device.patient)
    except Exception as exc:  # noqa: BLE001 - all operational failures need profile context
        try:
            if replace:
                service._restore_configuration(snapshot)  # noqa: SLF001 - paired with snapshot above
            else:
                service._restore_appended_configuration(  # noqa: SLF001 - paired with snapshot above
                    snapshot,
                    touched_contexts,
                )
        except Exception as rollback_exc:  # noqa: BLE001 - preserve both failure causes
            msg = f"{field}: profile import failed ({exc}); rollback failed: {rollback_exc}"
            raise ConfigError(msg) from exc
        msg = f"{field}: profile import failed: {exc}"
        raise ConfigError(msg) from exc

    return len(device.metrics), created_alerts


def load_into(service: ProviderService, path: str | Path, *, replace: bool = True) -> tuple[int, int]:
    """Read a config file and build it on a running provider."""
    return apply_to(service, load_file(path), replace=replace)
