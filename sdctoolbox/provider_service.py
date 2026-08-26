"""Provider side: publish an SDC device whose data sources are created at runtime.

This module owns everything that turns a MetricSpec into BICEPS descriptors and keeps the
SCO in sync. It has no GUI dependency; stage 2 puts a PySide6 skin on top of it.
"""

from __future__ import annotations

import logging
import math
import random
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
from .model import (
    DEFAULT_MANIFESTATIONS,
    DEFAULT_SAMPLE_PERIOD,
    AlertSpec,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    SignalInfo,
    WaveformShape,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sdc11073.provider.sco import AbstractScoOperationsRegistry

logger = logging.getLogger("sdctoolbox.provider")

# How much waveform data goes out per report. Samples are generated in blocks rather than
# one at a time, because a report per sample would be all overhead: a 0.1s sample period
# would mean ten SOAP messages a second per waveform.
WAVEFORM_BLOCK_SECONDS = 0.5

# Samples per full cycle of the generated curve. Fixed rather than derived from the sample
# period, so a slow waveform and a fast one look the same on screen and only differ in how
# quickly they get there.
WAVEFORM_CYCLE_SAMPLES = 40


def _shape_sample(shape: WaveformShape, phase: float, low: float, high: float) -> Decimal:
    """One sample of a curve, as a Decimal between low and high.

    ``phase`` counts cycles, so its fractional part is the position within one.
    """
    position = phase % 1.0
    if shape is WaveformShape.SINE:
        fraction = (math.sin(position * 2.0 * math.pi) + 1.0) / 2.0
    elif shape is WaveformShape.SAWTOOTH:
        fraction = position
    elif shape is WaveformShape.SQUARE:
        fraction = 1.0 if position < 0.5 else 0.0  # noqa: PLR2004
    else:
        fraction = random.random()  # noqa: S311 - a test signal, not a secret
    # Two decimal places: enough to draw a smooth curve, short enough that a block of
    # samples does not bloat the report.
    return Decimal(str(round(low + fraction * (high - low), 2)))


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
        # Waveform generation: one thread for every waveform, and where each curve had
        # got to, so a new block continues rather than restarting.
        self._waveform_thread: threading.Thread | None = None
        self._waveform_stop = threading.Event()
        self._waveform_phase: dict[str, float] = {}
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
        self.set_location(LocationInfo(**constants.DEFAULT_LOCATION))

        if self._sco is None:
            msg = "no SCO registry was created - the bootstrap MDIB is missing its Sco element"
            raise RuntimeError(msg)

        logger.info("provider %r up on %s, EPR %s", self.instance_name, self.ip, self.epr.urn)

    def stop(self) -> None:
        """Take the provider off the network."""
        self.stop_waveforms()
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
        self._waveform_phase.clear()
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
            if not spec.kind.creatable:
                missing = ", ".join(spec.kind.missing_fields)
                msg = (
                    f"this build cannot create {spec.kind.value} metrics yet: it never sets "
                    f"{missing}, which BICEPS makes mandatory"
                )
                raise ValueError(msg)

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

            self._create_entities([entity])

            self._specs[handle] = spec
            logger.info("added %s metric %r (%s)", spec.kind.value, spec.label, handle)

            if spec.initial_value is not None:
                self.set_value(handle, spec.initial_value)
            if spec.controllable:
                self.enable_control(handle)
            if spec.kind is MetricKind.WAVEFORM:
                # A waveform with nothing driving it publishes an empty descriptor and
                # never a sample, which looks like a broken device rather than an idle one.
                self.start_waveforms()
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
                signal.descriptor.SignalDelegationSupported = spec.delegable
                signal.state.ActivationState = pm_types.AlertActivation.ON
                signal.state.Presence = pm_types.AlertSignalPresence.OFF
                signal.state.Location = pm_types.AlertSignalPrimaryLocation.LOCAL
                signal_entities.append(signal)

            self._create_entities([entity, *signal_entities])

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

    def signal_states(self, handle: str) -> list[SignalInfo]:
        """How each signal of one condition is currently announcing it."""
        infos = []
        for signal_handle in self._alert_signals.get(handle, []):
            entity = self.mdib.entities.by_handle(signal_handle)
            if entity is None:
                continue
            infos.append(
                SignalInfo(
                    handle=signal_handle,
                    manifestation=str(entity.descriptor.Manifestation),
                    presence=str(entity.state.Presence),
                    location=str(entity.state.Location),
                    delegable=bool(entity.descriptor.SignalDelegationSupported),
                ),
            )
        return infos

    def acknowledge_alert(self, handle: str) -> int:
        """Acknowledge every signal of a condition. Returns how many were acknowledged.

        Acknowledging changes how the alarm is announced, not whether it is true: the
        condition keeps its Presence, and only the signals move to Ack. That distinction is
        the reason conditions and signals are separate objects, so the tool has to honour it
        rather than quietly clear the condition.
        """
        with self._lock:
            if handle not in self._alert_signals:
                msg = f"no alarm with handle {handle!r}"
                raise KeyError(msg)
            if not self.alert_present(handle):
                msg = f"{handle!r} is not raised, so there is nothing to acknowledge"
                raise ValueError(msg)
            acknowledged = [
                entity
                for entity in (
                    self.mdib.entities.by_handle(h) for h in self._alert_signals[handle]
                )
                if entity is not None and entity.state.Presence != pm_types.AlertSignalPresence.ACK
            ]
            if not acknowledged:
                return 0
            for entity in acknowledged:
                entity.state.Presence = pm_types.AlertSignalPresence.ACK
            with self.mdib.alert_state_transaction() as mgr:
                for entity in acknowledged:
                    mgr.write_entity(entity)
            logger.info("acknowledged %d signal(s) of %s", len(acknowledged), handle)
            return len(acknowledged)

    def set_signal_delegated(self, signal_handle: str, *, delegated: bool) -> None:
        """Hand a signal over to another device, or take it back.

        Delegation moves the signal's Location from Loc to Rem. BICEPS only allows it where
        the descriptor says SignalDelegationSupported, and nothing in sdc11073 enforces that,
        so this does.

        Note what this is and is not. It records that the announcement now belongs somewhere
        else. It does not arrange for anybody to pick it up: that needs a device offering a
        delegable signal of its own and an operation to drive it, which is beyond a two-role
        toolbox talking to itself.
        """
        with self._lock:
            entity = self.mdib.entities.by_handle(signal_handle)
            if entity is None:
                msg = f"no alarm signal with handle {signal_handle!r}"
                raise KeyError(msg)
            if delegated and not entity.descriptor.SignalDelegationSupported:
                msg = (
                    f"{signal_handle!r} is not delegable: its descriptor does not set "
                    f"SignalDelegationSupported"
                )
                raise ValueError(msg)
            location = (
                pm_types.AlertSignalPrimaryLocation.REMOTE
                if delegated
                else pm_types.AlertSignalPrimaryLocation.LOCAL
            )
            if entity.state.Location == location:
                return
            entity.state.Location = location
            with self.mdib.alert_state_transaction() as mgr:
                mgr.write_entity(entity)
            logger.info("signal %s is now announced %s", signal_handle, location)

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
            # Also what keeps an acknowledgement alive: alarms are re-evaluated on every
            # change to the source metric, and without this an Ack would be overwritten with
            # On by the very next value that is still out of range.
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
            # A fresh occurrence has not been acknowledged, so Ack does not survive the
            # condition going away and coming back.
            signal.state.Presence = (
                pm_types.AlertSignalPresence.ON if present else pm_types.AlertSignalPresence.OFF
            )

        with self.mdib.alert_state_transaction() as mgr:
            mgr.write_entity(entity)
            for signal in signals:
                mgr.write_entity(signal)
        logger.info("alarm %s is now %s", handle, "present" if present else "clear")

    # -- contexts ------------------------------------------------------------------

    def get_location(self) -> LocationInfo:
        """Where this device says it is."""
        state = self._associated_context_state(constants.LOCATION_CONTEXT_HANDLE)
        detail = getattr(state, "LocationDetail", None) if state is not None else None
        if detail is None:
            return LocationInfo()
        return LocationInfo(
            facility=detail.Facility or "",
            building=detail.Building or "",
            floor=detail.Floor or "",
            point_of_care=detail.PoC or "",
            room=detail.Room or "",
            bed=detail.Bed or "",
        )

    def set_location(self, info: LocationInfo) -> None:
        """Move the device, and tell the network.

        Deliberately routed through SdcProvider.set_location rather than written as a context
        state by hand: a location is also a WS-Discovery scope, so changing it has to
        re-announce the device or consumers would keep filtering on the old one.
        """
        with self._lock:
            if self._provider is None:
                msg = "provider is not started"
                raise RuntimeError(msg)
            self._provider.set_location(
                SdcLocation(
                    fac=info.facility or None,
                    bldng=info.building or None,
                    flr=info.floor or None,
                    poc=info.point_of_care or None,
                    rm=info.room or None,
                    bed=info.bed or None,
                ),
            )
            logger.info("location is now %s", info.summary() or "unset")

    def get_patient(self) -> PatientInfo:
        """Who the device says it is attached to."""
        state = self._associated_context_state(constants.PATIENT_CONTEXT_HANDLE)
        core = getattr(state, "CoreData", None) if state is not None else None
        if core is None:
            return PatientInfo()
        birth = getattr(core, "DateOfBirth", None)
        return PatientInfo(
            given_name=core.Givenname or "",
            family_name=core.Familyname or "",
            sex=str(core.Sex) if core.Sex else "",
            patient_type=str(core.PatientType) if core.PatientType else "",
            # XsdDateInformation renders itself back to the xsd form it was parsed from,
            # so a date entered as 1815-12-10 comes back as 1815-12-10 rather than a
            # datetime with a made-up midnight on the end.
            date_of_birth=str(birth) if birth is not None else "",
        )

    def set_patient(self, info: PatientInfo) -> None:
        """Associate a patient with the device, replacing whoever was there before.

        A context is a multi-state entity: the old state is disassociated rather than
        overwritten, so the MDIB keeps the history of who was attached when. That is why
        this cannot simply write over one state the way a metric does.
        """
        with self._lock:
            entity = self.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE)
            if entity is None:
                msg = f"the bootstrap MDIB has no {constants.PATIENT_CONTEXT_HANDLE}"
                raise RuntimeError(msg)

            state = entity.new_state()
            core = pm_types.PatientDemographicsCoreData()
            core.Givenname = info.given_name or None
            core.Familyname = info.family_name or None
            core.Sex = pm_types.Sex(info.sex) if info.sex else None
            core.PatientType = pm_types.PatientType(info.patient_type) if info.patient_type else None
            if info.date_of_birth:
                # Raises ValueError on anything xsd:date cannot express, which is what we want:
                # a silently dropped birth date would look like the field does not work.
                core.set_birthdate(info.date_of_birth)
            state.CoreData = core
            state.ContextAssociation = pm_types.ContextAssociation.ASSOCIATED
            state.BindingStartTime = time.time()

            with self.mdib.context_state_transaction() as mgr:
                mgr.disassociate_all(entity.handle, ignored_handle=state.Handle)
                mgr.write_entity(entity, [state.Handle])
            logger.info("patient is now %s", info.summary() or "unset")

    def clear_patient(self) -> None:
        """Detach whoever is currently associated, without putting anyone in their place."""
        with self._lock:
            entity = self.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE)
            if entity is None:
                return
            with self.mdib.context_state_transaction() as mgr:
                disassociated = mgr.disassociate_all(entity.handle)
            logger.info("detached %d patient context state(s)", len(disassociated))

    def _associated_context_state(self, descriptor_handle: str):  # noqa: ANN202 - an sdc11073 state
        """The currently associated state of a context, or None when there is none.

        A context descriptor holds many states and at most one of them is associated. The
        rest are the history, which is exactly what makes a context different from a metric.
        """
        entity = self.mdib.entities.by_handle(descriptor_handle)
        if entity is None:
            return None
        entity.update()
        for state in entity.states.values():
            if state.ContextAssociation == pm_types.ContextAssociation.ASSOCIATED:
                return state
        return None

    # -- sample arrays -------------------------------------------------------------

    def set_samples(self, handle: str, samples: Sequence[Decimal]) -> None:
        """Publish a fresh block of samples for a waveform or a distribution.

        A sample array state carries many values where an ordinary metric carries one, so
        this is the sample-array equivalent of set_value rather than an addition to it.

        The transaction picked here decides which report goes out, and sdc11073 chooses it
        from the *state* rather than from anything we pass: a RealTimeSampleArrayMetricState
        lands in TransactionResult.rt_updates and leaves as a WaveformStream, everything
        else lands in metric_updates and leaves as an EpisodicMetricReport. So a waveform
        needs the rt transaction and a distribution must not use it.
        """
        with self._lock:
            spec = self._specs.get(handle)
            if spec is None:
                msg = f"no metric with handle {handle!r}"
                raise KeyError(msg)
            if not spec.is_sample_array:
                msg = f"{handle!r} is a {spec.kind.value}, which holds one value; use set_value"
                raise ValueError(msg)
            for sample in samples:
                if isinstance(sample, float):
                    msg = "samples must be Decimal, never float"
                    raise TypeError(msg)

            entity = self.mdib.entities.by_handle(handle)
            if entity is None:
                msg = f"no metric with handle {handle!r}"
                raise KeyError(msg)

            # mk_metric_value raises if there already is one, so only ever call it once.
            if entity.state.MetricValue is None:
                entity.state.mk_metric_value()
            entity.state.MetricValue.Samples = list(samples)
            entity.state.MetricValue.DeterminationTime = time.time()

            if spec.kind is MetricKind.WAVEFORM:
                with self.mdib.rt_sample_state_transaction() as mgr:
                    mgr.write_entity(entity)
            else:
                with self.mdib.metric_state_transaction() as mgr:
                    mgr.write_entity(entity)

    def get_samples(self, handle: str) -> list[Decimal]:
        """The samples currently published for a sample-array metric."""
        entity = self.mdib.entities.by_handle(handle)
        if entity is None:
            return []
        value = getattr(entity.state, "MetricValue", None)
        return list(getattr(value, "Samples", None) or [])

    def start_waveforms(self) -> None:
        """Begin generating samples for every waveform this device publishes.

        One thread drives all of them. sdc11073 ships a WaveformProviderProtocol and no
        implementation of it, and a provider configured with none must be started with
        start_rtsample_loop=False - so rather than write that protocol, this walks the
        waveforms itself and writes their states. The report comes out either way, because
        the provider builds it from the transaction result.
        """
        with self._lock:
            if self._waveform_thread is not None:
                return
            self._waveform_stop.clear()
            self._waveform_thread = threading.Thread(
                target=self._run_waveforms,
                name=f"waveforms-{self.instance_name}",
                daemon=True,
            )
            self._waveform_thread.start()
            logger.info("waveform generator started")

    def stop_waveforms(self) -> None:
        """Stop generating samples and wait for the thread to notice."""
        thread = self._waveform_thread
        if thread is None:
            return
        self._waveform_stop.set()
        thread.join(timeout=5.0)
        self._waveform_thread = None
        logger.info("waveform generator stopped")

    @property
    def waveforms_running(self) -> bool:
        """Whether the generator thread is alive."""
        return self._waveform_thread is not None and self._waveform_thread.is_alive()

    def _run_waveforms(self) -> None:
        """Push one block of samples per waveform, per tick, until asked to stop."""
        while not self._waveform_stop.is_set():
            started = time.monotonic()
            for handle, spec in list(self._specs.items()):
                if spec.kind is not MetricKind.WAVEFORM:
                    continue
                try:
                    self.set_samples(handle, self._next_block(handle, spec))
                except (KeyError, ValueError, TypeError):
                    # The metric was removed between listing and writing, or something
                    # about it is unusable. Neither is worth killing the thread over.
                    logger.debug("skipped a waveform block for %s", handle, exc_info=True)
                except Exception:
                    logger.exception("waveform generation failed for %s", handle)
            # Sleep the remainder of the block, so generation keeps real time rather than
            # drifting by however long the writes took.
            self._waveform_stop.wait(max(0.0, WAVEFORM_BLOCK_SECONDS - (time.monotonic() - started)))

    def _next_block(self, handle: str, spec: MetricSpec) -> list[Decimal]:
        """The next block of samples for one waveform, continuing where the last left off."""
        period = float(spec.sample_period or DEFAULT_SAMPLE_PERIOD)
        count = max(1, int(round(WAVEFORM_BLOCK_SECONDS / period)))
        low = float(spec.minimum if spec.minimum is not None else 0)
        high = float(spec.maximum if spec.maximum is not None else 100)
        if high <= low:
            high = low + 1.0

        phase = self._waveform_phase.get(handle, 0.0)
        samples = []
        for index in range(count):
            samples.append(_shape_sample(spec.shape, phase + index / WAVEFORM_CYCLE_SAMPLES, low, high))
        self._waveform_phase[handle] = (phase + count / WAVEFORM_CYCLE_SAMPLES) % 1.0
        return samples

    # -- internals -----------------------------------------------------------------

    def _create_entities(self, entities: list) -> None:
        """Write brand new entities in one descriptor transaction, undoing them if it fails.

        A descriptor BICEPS considers incomplete is not rejected by the transaction. The
        transaction commits it, and only then does sdc11073 serialise the
        DescriptionModificationReport from an observer of mdib.transaction - which is where
        the complaint comes from. By that point descriptions.add_object_no_lock has already
        run, so letting the exception out unhandled leaves a handle in the MDIB that is
        advertised to consumers while this service has no record of it.

        Undoing it is best effort by design: see _discard_entities.
        """
        handles = [entity.handle for entity in entities]
        try:
            with self.mdib.descriptor_transaction() as mgr:
                for entity in entities:
                    mgr.write_entity(entity)
        except Exception:
            logger.warning("creating %s failed, undoing it", ", ".join(handles))
            self._discard_entities(handles)
            raise

    def _discard_entities(self, handles: list[str]) -> None:
        """Remove entities that were committed but could not be announced.

        One transaction per handle, so that one that will not come out does not strand the
        rest. The removal report serialises the same descriptor that could not be serialised
        on the way in and therefore fails in the same place - but process_transaction has
        already dropped the descriptor by then, which is the part that matters. Swallow that
        second failure and verify the outcome instead of trusting it.
        """
        for handle in handles:
            entity = self.mdib.entities.by_handle(handle)
            if entity is None:
                continue
            try:
                with self.mdib.descriptor_transaction() as mgr:
                    mgr.remove_entity(entity)
            except Exception:  # noqa: BLE001 - the removal is committed before this fires
                logger.debug("announcing the removal of %s failed too", handle, exc_info=True)
            if self.mdib.entities.by_handle(handle) is None:
                logger.info("undid %s", handle)
            else:
                logger.error("could not undo %s: it is still in the mdib", handle)

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

        if spec.is_sample_array:
            # Resolution is mandatory on both sample-array descriptors as well, and it is
            # the one serialisation complains about first when it is missing.
            descriptor.Resolution = spec.resolution
            if spec.has_range:
                descriptor.TechnicalRange = [
                    pm_types.Range(lower=spec.minimum, upper=spec.maximum, step_width=spec.resolution),
                ]

        if spec.kind is MetricKind.WAVEFORM:
            # An xsd:duration in seconds. sdc11073 takes a float here and renders it.
            descriptor.SamplePeriod = float(spec.sample_period)

        if spec.kind is MetricKind.DISTRIBUTION:
            # What the samples are spread *over*, as opposed to Unit, which is what each
            # sample measures. Mandatory, and the field this kind fails on first.
            descriptor.DomainUnit = pm_types.CodedValue(
                code=spec.domain_unit_code,
                coding_system=spec.domain_unit_coding_system,
                concept_descriptions=(
                    [pm_types.LocalizedText(spec.domain_unit_label, lang="en-US")]
                    if spec.domain_unit_label
                    else None
                ),
            )
            descriptor.DistributionRange = pm_types.Range(
                lower=spec.domain_minimum,
                upper=spec.domain_maximum,
                step_width=spec.resolution,
            )

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
