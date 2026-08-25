"""Saving a configured device to a file and building it again from one.

A config records what the user set up - the data sources and the alarms - not the values
those data sources happen to be holding, except for an explicit initial value. Loading one
into a running provider replaces whatever it had.

The format is JSON with a version field. Handles are recorded so that a preset reproduces
the same MDIB every time, which matters when a consumer or a script refers to them by name.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import METRIC_HANDLE_PREFIX
from .model import AlertKind, AlertPriority, AlertSpec, MetricKind, MetricSpec

if TYPE_CHECKING:
    from .provider_service import ProviderService

#: Bumped when the layout changes in a way older files would not survive.
CONFIG_VERSION = 1

FILE_SUFFIX = ".sdcprofile.json"


class ConfigError(Exception):
    """A config file could not be read, or does not describe a usable device."""


def _decimal_or_none(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        msg = f"{field}: {value!r} is not a number"
        raise ConfigError(msg) from exc


def _enum_or_default(enum_cls: Any, value: Any, default: Any, field: str) -> Any:
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in enum_cls)
        msg = f"{field}: {value!r} is not one of {allowed}"
        raise ConfigError(msg) from exc


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


def metric_to_dict(handle: str, spec: MetricSpec) -> dict[str, Any]:
    """One data source as plain JSON types."""
    entry: dict[str, Any] = {
        "handle": handle,
        "label": spec.label,
        "kind": spec.kind.value,
        "controllable": spec.controllable,
    }
    if spec.unit_label:
        entry["unit"] = spec.unit_label
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
    return entry


def alert_to_dict(handle: str, spec: AlertSpec) -> dict[str, Any]:
    """One alarm as plain JSON types."""
    entry: dict[str, Any] = {
        "handle": handle,
        "label": spec.label,
        "watches": spec.source_handle,
        "kind": spec.kind.value,
        "priority": spec.priority.value,
    }
    if spec.lower_limit is not None:
        entry["lower_limit"] = str(spec.lower_limit)
    if spec.upper_limit is not None:
        entry["upper_limit"] = str(spec.upper_limit)
    return entry


def to_dict(service: ProviderService, *, include_values: bool = True) -> dict[str, Any]:
    """Capture a running provider's configuration.

    :param include_values: record each metric's current value as its initial_value, so that
        reloading the file reproduces what is on screen rather than an empty device.
    """
    metrics = []
    for handle, spec in sorted(service.list_metrics().items()):
        entry = metric_to_dict(handle, spec)
        if include_values:
            current = service.get_value(handle)
            if current is not None:
                entry["initial_value"] = str(current)
            else:
                entry.pop("initial_value", None)
        metrics.append(entry)

    return {
        "version": CONFIG_VERSION,
        "device": {"instance_name": service.instance_name, "friendly_name": service.friendly_name},
        "metrics": metrics,
        "alerts": [alert_to_dict(handle, spec) for handle, spec in sorted(service.list_alerts().items())],
    }


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
        initial = str(initial)

    try:
        return MetricSpec(
            label=str(label),
            kind=kind,
            unit_label=str(entry.get("unit") or ""),
            allowed_values=tuple(str(v) for v in entry.get("allowed_values") or ()),
            resolution=_decimal_or_none(entry.get("resolution"), f"metrics[{label}].resolution"),
            minimum=_decimal_or_none(entry.get("minimum"), f"metrics[{label}].minimum"),
            maximum=_decimal_or_none(entry.get("maximum"), f"metrics[{label}].maximum"),
            controllable=bool(entry.get("controllable", False)),
            handle=entry.get("handle") or None,
            initial_value=initial,
        )
    except (ValueError, TypeError) as exc:
        msg = f"metrics[{label}]: {exc}"
        raise ConfigError(msg) from exc


def alert_from_dict(entry: dict[str, Any]) -> AlertSpec:
    """Rebuild one alarm definition. Raises ConfigError on anything unusable."""
    if not isinstance(entry, dict):
        msg = f"alerts: expected an object, found {type(entry).__name__}"
        raise ConfigError(msg)

    label = entry.get("label")
    if not label:
        msg = "alerts: an entry has no label"
        raise ConfigError(msg)
    source = entry.get("watches")
    if not source:
        msg = f"alerts[{label}]: no metric to watch"
        raise ConfigError(msg)

    try:
        return AlertSpec(
            label=str(label),
            source_handle=str(source),
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
        )
    except (ValueError, TypeError) as exc:
        msg = f"alerts[{label}]: {exc}"
        raise ConfigError(msg) from exc


def parse(data: Any) -> tuple[list[MetricSpec], list[AlertSpec]]:
    """Turn a decoded config into specs, complaining clearly about anything wrong."""
    if not isinstance(data, dict):
        msg = f"expected an object at the top level, found {type(data).__name__}"
        raise ConfigError(msg)

    version = data.get("version")
    if version is not None and version > CONFIG_VERSION:
        msg = f"this file is version {version}, but this build only understands up to {CONFIG_VERSION}"
        raise ConfigError(msg)

    metrics = [metric_from_dict(entry) for entry in data.get("metrics") or []]
    alerts = [alert_from_dict(entry) for entry in data.get("alerts") or []]

    # Catch a dangling reference here rather than half way through building the device.
    defined = {spec.handle or (METRIC_HANDLE_PREFIX + spec.slug) for spec in metrics}
    for alert in alerts:
        if alert.source_handle not in defined:
            known = ", ".join(sorted(defined)) or "none"
            msg = (
                f"alerts[{alert.label}]: watches {alert.source_handle!r}, "
                f"which this file does not define. It defines: {known}"
            )
            raise ConfigError(msg)

    return metrics, alerts


def load_file(path: str | Path) -> tuple[list[MetricSpec], list[AlertSpec]]:
    """Read and validate a config file without touching any provider."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read {source}: {exc}"
        raise ConfigError(msg) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{source} is not valid JSON: {exc}"
        raise ConfigError(msg) from exc
    return parse(data)


def apply_to(
    service: ProviderService,
    metrics: list[MetricSpec],
    alerts: list[AlertSpec],
    *,
    replace: bool = True,
) -> tuple[int, int]:
    """Build the described device on a running provider.

    Metrics go in before alarms, since an alarm names the metric it watches. Returns how
    many of each were created.

    :param replace: clear whatever the provider already had first. Without it, handles that
        already exist would collide.
    """
    if replace:
        for handle in list(service.list_alerts()):
            service.remove_alert(handle)
        for handle in list(service.list_metrics()):
            service.remove_metric(handle)

    for spec in metrics:
        service.add_metric(spec)
    created_alerts = 0
    for spec in alerts:
        try:
            service.add_alert(spec)
        except KeyError as exc:
            msg = f"alarm {spec.label!r} watches {spec.source_handle!r}, which the file does not define"
            raise ConfigError(msg) from exc
        created_alerts += 1
    return len(metrics), created_alerts


def load_into(service: ProviderService, path: str | Path, *, replace: bool = True) -> tuple[int, int]:
    """Read a config file and build it on a running provider."""
    metrics, alerts = load_file(path)
    return apply_to(service, metrics, alerts, replace=replace)
