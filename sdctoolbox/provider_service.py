"""Provider side: publish an SDC device whose data sources are created at runtime.

This module owns everything that turns a MetricSpec into BICEPS descriptors and keeps the
SCO in sync. It has no GUI dependency; stage 2 puts a PySide6 skin on top of it.
"""

from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from sdc11073.location import SdcLocation
from sdc11073.mdib import ProviderMdib
from sdc11073.provider import SdcProvider
from sdc11073.provider.baseproduct import BaseProduct
from sdc11073.provider.providerimpl import RoleProviderComponents
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.dpws_types import ThisDeviceType, ThisModelType

from . import constants
from .handlers import apply_metric_value, make_set_handler
from .model import DEFAULT_MANIFESTATIONS, AlertSpec, MetricKind, MetricSpec

if TYPE_CHECKING:
    from sdc11073.provider.sco import AbstractScoOperationsRegistry

logger = logging.getLogger("sdctoolbox.provider")


class ProviderService:
    """An SDC provider whose MDIB grows while it is running.

    Usage::

        service = ProviderService(instance_name="alpha")
        service.start()
        handle = service.add_metric(MetricSpec("Zoom level", MetricKind.NUMBER, controllable=True))
        service.set_value(handle, Decimal("3"))
        service.stop()
    """

    def __init__(
            self,
            ip: str = constants.DEFAULT_IP,
            instance_name: str = constants.DEFAULT_INSTANCE_NAME,
            friendly_name: str | None = None,
    ) -> None:
        self.ip = ip
        self.instance_name = instance_name
        self.friendly_name = friendly_name or f"Toolbox {instance_name}"
        self.epr = constants.epr_for(instance_name)

        self._discovery: WSDiscovery | None = None
        self._provider: SdcProvider | None = None
        self._mdib: ProviderMdib | None = None
        self._sco: AbstractScoOperationsRegistry | None = None
        self._handler = None
        # metric handle -> operation handle, for metrics that have a set operation
        self._operations: dict[str, str] = {}
        # metric handle -> the spec it was created from
        self._specs: dict[str, MetricSpec] = {}
        # alarm handle -> the spec it was created from
        self._alerts: dict[str, AlertSpec] = {}
        # alarm handle -> the signals announcing it
        self._alert_signals: dict[str, list[str]] = {}
        # Guards the metric observer against re-entering itself.
        self._evaluating_alerts = False
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------------------

    @property
    def mdib(self) -> ProviderMdib:
        """The live provider MDIB. Raises if the service is not started."""
        if self._mdib is None:
            msg = "provider is not started"
            raise RuntimeError(msg)
        return self._mdib

    def start(self) -> None:
        """Bring the provider up and announce it on the network."""
        if self._provider is not None:
            msg = "provider is already started"
            raise RuntimeError(msg)

        self._discovery = WSDiscovery(self.ip)
        self._discovery.start()

        self._mdib = ProviderMdib.from_mdib_file(str(constants.BOOTSTRAP_MDIB_PATH))
        # The handler tells us which metric it touched once its transaction has closed, so a
        # value written by a remote consumer raises a limit alarm exactly as a local edit
        # does. It cannot be done from a metrics observable: sdc11073 fires those while
        # holding the transaction lock, and opening the alert transaction there deadlocks.
        self._handler = make_set_handler(self._mdib, on_applied=self._on_metric_applied)

        this_model = ThisModelType(
            manufacturer=constants.MANUFACTURER,
            manufacturer_url=constants.MANUFACTURER_URL,
            model_name=constants.MODEL_NAME,
            model_number=constants.MODEL_NUMBER,
            model_url=constants.MANUFACTURER_URL,
            presentation_url=constants.MANUFACTURER_URL,
        )
        this_device = ThisDeviceType(
            friendly_name=self.friendly_name,
            firmware_version=constants.FIRMWARE_VERSION,
            serial_number=self.instance_name,
        )

        # SdcProvider only builds SCO registries when a role_provider_class is present
        # (see providerimpl._setup_components). We therefore always supply one, and use the
        # factory to capture the registry instance instead of reaching into private state.
        def role_provider_factory(mdib, sco, log_prefix):  # noqa: ANN001, ANN202
            self._sco = sco
            return BaseProduct(mdib, sco, log_prefix)

        self._provider = SdcProvider(
            ws_discovery=self._discovery,
            epr=self.epr,
            this_model=this_model,
            this_device=this_device,
            device_mdib_container=self._mdib,
            role_provider_components=RoleProviderComponents(role_provider_class=role_provider_factory),
        )

        # No waveform provider configured, so the real-time sample loop must stay off.
        self._provider.start_all(start_rtsample_loop=False)
        self._provider.set_location(SdcLocation(**constants.DEFAULT_LOCATION))

        if self._sco is None:
            msg = "no SCO registry was created - the bootstrap MDIB is missing its Sco element"
            raise RuntimeError(msg)

        logger.info("provider %r up on %s, EPR %s", self.instance_name, self.ip, self.epr.urn)

    def stop(self) -> None:
        """Take the provider off the network."""
        if self._provider is not None:
            self._provider.stop_all()
            self._provider = None
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        self._mdib = None
        self._sco = None
        self._operations.clear()
        self._specs.clear()
        self._alerts.clear()
        self._alert_signals.clear()
        logger.info("provider %r stopped", self.instance_name)

    def __enter__(self) -> ProviderService:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- data sources --------------------------------------------------------------

    def add_metric(self, spec: MetricSpec) -> str:
        """Create a data source at runtime and return its handle.

        The descriptor is written inside a descriptor transaction, which makes sdc11073 emit
        a DescriptionModificationReport, so connected consumers see the new metric appear.
        """
        with self._lock:
            handle = spec.handle or self._unique_handle(constants.METRIC_HANDLE_PREFIX + spec.slug)
            if self.mdib.entities.by_handle(handle) is not None:
                msg = f"handle {handle!r} already exists"
                raise ValueError(msg)

            entity = self.mdib.entities.new_entity(
                spec.kind.descriptor_qname,
                handle,
                constants.CHANNEL_HANDLE,
            )
            self._apply_spec_to_descriptor(entity.descriptor, spec)

            with self.mdib.descriptor_transaction() as mgr:
                mgr.write_entity(entity)

            self._specs[handle] = spec
            logger.info("added %s metric %r (%s)", spec.kind.value, spec.label, handle)

            if spec.initial_value is not None:
                self.set_value(handle, spec.initial_value)
            if spec.controllable:
                self.enable_control(handle)
            return handle

    def remove_metric(self, handle: str) -> None:
        """Delete a data source and its operation, if any."""
        with self._lock:
            operation_handle = self._operations.pop(handle, None)
            entities = []
            if operation_handle is not None:
                self._sco.unregister_operation_by_handle(operation_handle)
                operation_entity = self.mdib.entities.by_handle(operation_handle)
                if operation_entity is not None:
                    entities.append(operation_entity)
            metric_entity = self.mdib.entities.by_handle(handle)
            if metric_entity is not None:
                entities.append(metric_entity)

            # Drop the bookkeeping before committing. Leaving the transaction fires
            # deleted_descriptors_by_handle synchronously, and any observer that reacts by
            # listing our metrics would otherwise see a handle whose entity is already gone.
            self._specs.pop(handle, None)

            with self.mdib.descriptor_transaction() as mgr:
                for entity in entities:
                    mgr.remove_entity(entity)

            logger.info("removed metric %s", handle)

    def set_value(self, handle: str, value: Decimal | str) -> None:
        """Set the current value of one of our own metrics."""
        if isinstance(value, float):
            msg = "use Decimal, never float"
            raise TypeError(msg)
        with self._lock:
            spec = self._specs.get(handle)
            if spec is not None and isinstance(value, Decimal):
                if spec.minimum is not None and value < spec.minimum:
                    msg = f"{value} is below the minimum {spec.minimum} of {handle!r}"
                    raise ValueError(msg)
                if spec.maximum is not None and value > spec.maximum:
                    msg = f"{value} is above the maximum {spec.maximum} of {handle!r}"
                    raise ValueError(msg)

            entity = self.mdib.entities.by_handle(handle)
            if entity is None:
                msg = f"no metric with handle {handle!r}"
                raise KeyError(msg)
            apply_metric_value(entity.state, value)
            with self.mdib.metric_state_transaction() as mgr:
                mgr.write_entity(entity)

        # Outside the transaction on purpose: writing the alarm state needs a transaction of
        # its own, and the metric one still holds the lock until the with block closes.
        self._evaluate_alerts({handle})

    def get_value(self, handle: str):  # noqa: ANN201 - the value type depends on the metric kind
        """Read back the current value of one of our own metrics.

        Returns None both when the metric has no value yet and when it no longer exists, so
        that observers reacting to a deletion do not have to guard against a race.
        """
        entity = self.mdib.entities.by_handle(handle)
        if entity is None:
            return None
        return getattr(getattr(entity.state, "MetricValue", None), "Value", None)

    def list_metrics(self) -> dict[str, MetricSpec]:
        """Return the specs of all data sources we created, keyed by handle."""
        return dict(self._specs)

    # -- remote control ------------------------------------------------------------

    def enable_control(self, handle: str) -> str:
        """Make a metric remote-controllable and return the operation handle.

        On first call this registers a new operation with the SCO, which creates the
        operation descriptor and emits a DescriptionModificationReport. On later calls it
        merely flips OperatingMode back to En.
        """
        with self._lock:
            spec = self._specs.get(handle)
            if spec is None:
                msg = f"no metric with handle {handle!r}"
                raise KeyError(msg)
            operation_class = spec.kind.operation_class
            if operation_class is None:
                msg = f"{spec.kind.value} metrics cannot be remote-controlled"
                raise ValueError(msg)

            operation_handle = self._operations.get(handle)
            if operation_handle is None:
                operation_handle = constants.OPERATION_HANDLE_PREFIX + handle.removeprefix(
                    constants.METRIC_HANDLE_PREFIX,
                )
                operation_handle = self._unique_handle(operation_handle)
                operation = operation_class(
                    handle=operation_handle,
                    operation_target_handle=handle,
                    operation_handler=self._handler,
                    coded_value=pm_types.CodedValue(
                        code=f"set_{spec.effective_type_code()}",
                        coding_system=constants.CODING_SYSTEM_PRIVATE,
                    ),
                )
                # register_operation calls set_mdib, which creates the descriptor for us.
                self._sco.register_operation(operation)
                self._operations[handle] = operation_handle
                logger.info("registered operation %s targeting %s", operation_handle, handle)

            self._set_operating_mode(operation_handle, pm_types.OperatingMode.ENABLED)
            self._set_allowed_range(operation_handle, spec)
            self._set_metric_category(handle, pm_types.MetricCategory.SETTING)
            return operation_handle

    def disable_control(self, handle: str) -> None:
        """Switch remote control off without deleting the operation.

        Deliberately not unregister_operation_by_handle: that only drops the operation from
        an internal dict and leaves its descriptor advertised to consumers, which would show
        an editor with nothing behind it.
        """
        with self._lock:
            operation_handle = self._operations.get(handle)
            if operation_handle is None:
                return
            self._set_operating_mode(operation_handle, pm_types.OperatingMode.DISABLED)

    def operation_handle_for(self, handle: str) -> str | None:
        """Return the operation handle controlling a metric, if there is one."""
        return self._operations.get(handle)

    # -- alarms --------------------------------------------------------------------

    def add_alert(self, spec: AlertSpec) -> str:
        """Create an alarm condition and the signals that announce it.

        With limits, the condition becomes a LimitAlertCondition and its presence follows
        the source metric from then on. Without them it stays a plain AlertCondition that
        only ``set_alert_presence`` moves.
        """
        with self._lock:
            if self.mdib.entities.by_handle(spec.source_handle) is None:
                msg = f"no metric with handle {spec.source_handle!r} to watch"
                raise KeyError(msg)

            handle = spec.handle or self._unique_handle(constants.ALERT_HANDLE_PREFIX + spec.slug)
            node_type = pm.LimitAlertConditionDescriptor if spec.has_limits else pm.AlertConditionDescriptor
            entity = self.mdib.entities.new_entity(node_type, handle, constants.ALERT_SYSTEM_HANDLE)

            descriptor = entity.descriptor
            descriptor.Type = pm_types.CodedValue(
                code=spec.slug,
                coding_system=constants.CODING_SYSTEM_PRIVATE,
                concept_descriptions=[pm_types.LocalizedText(spec.label, lang="en-US")],
            )
            descriptor.Source = [spec.source_handle]
            descriptor.Kind = spec.kind
            descriptor.Priority = spec.priority
            if spec.has_limits:
                descriptor.MaxLimits = pm_types.Range(lower=spec.lower_limit, upper=spec.upper_limit)

            entity.state.ActivationState = pm_types.AlertActivation.ON
            entity.state.Presence = False
            if spec.has_limits:
                entity.state.Limits = pm_types.Range(lower=spec.lower_limit, upper=spec.upper_limit)

            signal_entities = []
            for manifestation in DEFAULT_MANIFESTATIONS:
                signal_handle = self._unique_handle(
                    f"{constants.SIGNAL_HANDLE_PREFIX}{spec.slug}.{manifestation.value.lower()}",
                )
                signal = self.mdib.entities.new_entity(
                    pm.AlertSignalDescriptor,
                    signal_handle,
                    constants.ALERT_SYSTEM_HANDLE,
                )
                signal.descriptor.ConditionSignaled = handle
                signal.descriptor.Manifestation = manifestation
                signal.descriptor.Latching = False
                signal.state.ActivationState = pm_types.AlertActivation.ON
                signal.state.Presence = pm_types.AlertSignalPresence.OFF
                signal_entities.append(signal)

            with self.mdib.descriptor_transaction() as mgr:
                mgr.write_entity(entity)
                for signal in signal_entities:
                    mgr.write_entity(signal)

            self._alerts[handle] = spec
            self._alert_signals[handle] = [s.handle for s in signal_entities]
            logger.info(
                "added alarm %r watching %s (%s)",
                spec.label,
                spec.source_handle,
                spec.limit_text() or "manual",
            )

            if spec.has_limits:
                self._evaluate_alerts({spec.source_handle})
            return handle

    def remove_alert(self, handle: str) -> None:
        """Delete an alarm condition together with its signals."""
        with self._lock:
            signal_handles = self._alert_signals.pop(handle, [])
            self._alerts.pop(handle, None)
            entities = [
                entity
                for entity in (self.mdib.entities.by_handle(h) for h in [*signal_handles, handle])
                if entity is not None
            ]
            with self.mdib.descriptor_transaction() as mgr:
                for entity in entities:
                    mgr.remove_entity(entity)
            logger.info("removed alarm %s", handle)

    def set_alert_presence(self, handle: str, present: bool) -> None:  # noqa: FBT001
        """Raise or clear an alarm by hand.

        Only meaningful for a condition without limits; one with limits is recomputed from
        its source metric and would overwrite this on the next change.
        """
        with self._lock:
            self._write_alert_presence(handle, present=present)

    def alert_present(self, handle: str) -> bool:
        """Whether the condition is currently raised."""
        entity = self.mdib.entities.by_handle(handle)
        return bool(getattr(getattr(entity, "state", None), "Presence", False))

    def list_alerts(self) -> dict[str, AlertSpec]:
        """The specs of every alarm we created, keyed by handle."""
        return dict(self._alerts)

    def signal_handles_for(self, handle: str) -> list[str]:
        """Handles of the signals announcing one condition."""
        return list(self._alert_signals.get(handle, []))

    def _on_metric_applied(self, metric_handle: str) -> None:
        """A metric changed, by whatever route. Recompute the alarms watching it."""
        self._evaluate_alerts({metric_handle})

    def _evaluate_alerts(self, source_handles: set[str]) -> None:
        if self._evaluating_alerts or self._mdib is None:
            return
        self._evaluating_alerts = True
        try:
            for handle, spec in list(self._alerts.items()):
                if not spec.has_limits or spec.source_handle not in source_handles:
                    continue
                value = self.get_value(spec.source_handle)
                self._write_alert_presence(handle, present=spec.breached_by(value))
        finally:
            self._evaluating_alerts = False

    def _write_alert_presence(self, handle: str, *, present: bool) -> None:
        entity = self.mdib.entities.by_handle(handle)
        if entity is None:
            msg = f"no alarm with handle {handle!r}"
            raise KeyError(msg)
        if bool(getattr(entity.state, "Presence", False)) == present:
            return

        entity.state.Presence = present
        entity.state.DeterminationTime = time.time()
        signals = [
            signal
            for signal in (
                self.mdib.entities.by_handle(h) for h in self._alert_signals.get(handle, [])
            )
            if signal is not None
        ]
        for signal in signals:
            signal.state.Presence = (
                pm_types.AlertSignalPresence.ON if present else pm_types.AlertSignalPresence.OFF
            )

        with self.mdib.alert_state_transaction() as mgr:
            mgr.write_entity(entity)
            for signal in signals:
                mgr.write_entity(signal)
        logger.info("alarm %s is now %s", handle, "present" if present else "clear")

    # -- internals -----------------------------------------------------------------

    def _unique_handle(self, candidate: str) -> str:
        if self.mdib.entities.by_handle(candidate) is None:
            return candidate
        counter = 2
        while self.mdib.entities.by_handle(f"{candidate}.{counter}") is not None:
            counter += 1
        return f"{candidate}.{counter}"

    def _apply_spec_to_descriptor(self, descriptor, spec: MetricSpec) -> None:  # noqa: ANN001
        descriptor.Type = pm_types.CodedValue(
            code=spec.effective_type_code(),
            coding_system=spec.type_coding_system,
            concept_descriptions=[pm_types.LocalizedText(spec.label, lang="en-US")],
        )
        # A dimensionless metric still needs a Unit; it just gets no concept description,
        # rather than a made-up one such as "no unit".
        descriptor.Unit = pm_types.CodedValue(
            code=spec.unit_code,
            coding_system=spec.unit_coding_system,
            concept_descriptions=(
                [pm_types.LocalizedText(spec.unit_label, lang="en-US")] if spec.unit_label else None
            ),
        )
        descriptor.MetricCategory = (
            pm_types.MetricCategory.SETTING if spec.controllable else pm_types.MetricCategory.MEASUREMENT
        )
        descriptor.MetricAvailability = pm_types.MetricAvailability.INTERMITTENT

        if spec.kind is MetricKind.NUMBER:
            descriptor.Resolution = spec.resolution
            if spec.has_range:
                # What the metric itself can produce. Distinct from the AllowedRange we put
                # on the set operation, which is what a remote caller may ask for.
                descriptor.TechnicalRange = [
                    pm_types.Range(
                        lower=spec.minimum,
                        upper=spec.maximum,
                        step_width=spec.resolution,
                    ),
                ]
        if spec.kind is MetricKind.CHOICE:
            descriptor.AllowedValue = [pm_types.AllowedValue(value=value) for value in spec.allowed_values]

    def _set_operating_mode(self, operation_handle: str, mode: pm_types.OperatingMode) -> None:
        entity = self.mdib.entities.by_handle(operation_handle)
        if entity is None:
            msg = f"operation {operation_handle!r} is not in the mdib"
            raise KeyError(msg)
        entity.state.OperatingMode = mode
        with self.mdib.operational_state_transaction() as mgr:
            mgr.write_entity(entity)
        logger.info("operation %s is now %s", operation_handle, mode)

    def _set_metric_category(self, handle: str, category: pm_types.MetricCategory) -> None:
        entity = self.mdib.entities.by_handle(handle)
        if entity is None or entity.descriptor.MetricCategory == category:
            return
        entity.descriptor.MetricCategory = category
        with self.mdib.descriptor_transaction() as mgr:
            mgr.write_entity(entity)

    def _set_allowed_range(self, operation_handle: str, spec: MetricSpec) -> None:
        """Publish the limits a remote caller must respect.

        AllowedRange lives on the operation *state*, not its descriptor, so the permitted
        window can be narrowed at runtime without a description change - the same trick
        OperatingMode uses.
        """
        if not spec.has_range:
            return
        entity = self.mdib.entities.by_handle(operation_handle)
        if entity is None:
            return
        entity.state.AllowedRange = [
            pm_types.Range(lower=spec.minimum, upper=spec.maximum, step_width=spec.resolution),
        ]
        with self.mdib.operational_state_transaction() as mgr:
            mgr.write_entity(entity)
