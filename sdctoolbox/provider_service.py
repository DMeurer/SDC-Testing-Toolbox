"""Provider side: publish an SDC device whose data sources are created at runtime.

This module owns everything that turns a MetricSpec into BICEPS descriptors and keeps the
SCO in sync. It has no GUI dependency; stage 2 puts a PySide6 skin on top of it.
"""

from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sdc11073.location import SdcLocation
from sdc11073.mdib import ProviderMdib, mdibbase
from sdc11073.provider import SdcProvider
from sdc11073.provider.baseproduct import BaseProduct
from sdc11073.provider.operations import ActivateOperation
from sdc11073.provider.providerimpl import RoleProviderComponents
from sdc11073.wsdiscovery import WSDiscovery
from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types
from sdc11073.xml_types.dpws_types import ThisDeviceType, ThisModelType
from sdc11073.xml_types.xml_structure import DateOfBirthProperty

from . import constants
from .handlers import apply_metric_value, make_activate_handler, make_set_handler
from .model import (
    DEFAULT_PATIENT,
    ActionSpec,
    AlertSpec,
    Coding,
    DeviceInfo,
    LocationInfo,
    MetricKind,
    MetricSpec,
    PatientInfo,
    SignalInfo,
    coerce_metric_value,
    fixed_point_decimal,
    patient_info_from_biceps,
    patient_measurement_wire_value,
    slugify,
)
from .sample_generation import DemoSampleGenerator, SampleGeneratorSnapshot, domain_step
from .sdc11073_v3_adapter import Sdc11073V3Adapter, Sdc11073V3Snapshot
from .security import CertificateInfo, TlsConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sdc11073.provider.sco import AbstractScoOperationsRegistry

    from .config import DeviceConfig

logger = logging.getLogger("sdctoolbox.provider")


@dataclass
class _ConfigurationSnapshot:
    context_states: list
    operations: dict[str, str]
    specs: dict[str, MetricSpec]
    alerts: dict[str, AlertSpec]
    alert_truths: dict[str, bool]
    alert_signals: dict[str, list[str]]
    actions: dict[str, ActionSpec]
    sections: dict[str, str]
    library: Sdc11073V3Snapshot
    pending_alert_sources: set[str]
    mds_operating_mode: pm_types.MdsOperatingMode
    mds_mode_before_demo: pm_types.MdsOperatingMode | None
    sample_generator: SampleGeneratorSnapshot
    generator_running: bool


@dataclass(frozen=True)
class _SignalTransitionInput:
    activation: pm_types.AlertActivation
    presence: pm_types.AlertSignalPresence
    latching: bool


@dataclass(frozen=True)
class _AlertTransition:
    condition_presence: bool
    signal_presences: tuple[pm_types.AlertSignalPresence, ...]


def _reduce_alert_transition(
        *,
        condition_truth: bool,
        current_presence: bool,
        condition_activation: pm_types.AlertActivation,
        system_activation: pm_types.AlertActivation,
        signals: tuple[_SignalTransitionInput, ...],
        acknowledge: bool = False,
        stop_latches: bool = False,
) -> _AlertTransition:
    """Purely derive condition presence and each signal's next presence."""
    condition_present = (
        condition_truth
        and condition_activation is pm_types.AlertActivation.ON
        and system_activation is pm_types.AlertActivation.ON
    )
    annunciation_enabled = (
        condition_activation is pm_types.AlertActivation.ON
        and system_activation is pm_types.AlertActivation.ON
    )
    signal_presences = []
    for signal in signals:
        if not annunciation_enabled or signal.activation is pm_types.AlertActivation.OFF:
            presence = pm_types.AlertSignalPresence.OFF
        elif signal.activation is pm_types.AlertActivation.PAUSED:
            presence = (
                pm_types.AlertSignalPresence.ACK
                if condition_present and signal.presence is pm_types.AlertSignalPresence.ACK
                else pm_types.AlertSignalPresence.OFF
            )
        elif condition_present:
            if acknowledge and signal.presence is pm_types.AlertSignalPresence.ON:
                presence = pm_types.AlertSignalPresence.ACK
            elif current_presence:
                presence = signal.presence
            else:
                presence = pm_types.AlertSignalPresence.ON
        elif stop_latches and signal.presence is pm_types.AlertSignalPresence.LATCH:
            presence = pm_types.AlertSignalPresence.OFF
        elif signal.presence is pm_types.AlertSignalPresence.LATCH:
            presence = signal.presence
        elif signal.latching and signal.presence in (
            pm_types.AlertSignalPresence.ON,
            pm_types.AlertSignalPresence.ACK,
        ):
            presence = pm_types.AlertSignalPresence.LATCH
        else:
            presence = pm_types.AlertSignalPresence.OFF
        signal_presences.append(presence)
    return _AlertTransition(condition_present, tuple(signal_presences))


def _coded_value(coding: Coding, label: str = "") -> pm_types.CodedValue:
    """Turn one of our Codings into the BICEPS element.

    The label rides along as a ConceptDescription, which is documentation. The code and its
    coding system are the part another device can act on.
    """
    text = label or coding.label
    return pm_types.CodedValue(
        code=coding.code,
        coding_system=coding.coding_system,
        concept_descriptions=[pm_types.LocalizedText(text, lang="en-US")] if text else None,
    )


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
            device: DeviceInfo | None = None,
            tls_config: TlsConfig | None = None,
    ) -> None:
        self.ip = ip
        self.instance_name = instance_name
        # A preset describing a ventilator should announce itself as one. sdc11073 takes
        # ThisModel and ThisDevice when the provider is constructed, so this cannot be
        # changed later - run_toolbox reads the config before starting for that reason.
        self.device = device or DeviceInfo()
        self.friendly_name = friendly_name or self.device.friendly_name or f"Toolbox {instance_name}"
        self.epr = constants.epr_for(instance_name)
        self.tls_config = tls_config
        self._tls_contexts = None

        self._discovery: WSDiscovery | None = None
        self._provider: SdcProvider | None = None
        self._mdib: ProviderMdib | None = None
        self._sco: AbstractScoOperationsRegistry | None = None
        self._handler = None
        self._activate_handler = None
        # metric handle -> operation handle, for metrics that have a set operation
        self._operations: dict[str, str] = {}
        # metric handle -> the spec it was created from
        self._specs: dict[str, MetricSpec] = {}
        # alarm handle -> the spec it was created from
        self._alerts: dict[str, AlertSpec] = {}
        # alarm handle -> condition truth before activation predicates are applied
        self._alert_truths: dict[str, bool] = {}
        # alarm handle -> the signals announcing it
        self._alert_signals: dict[str, list[str]] = {}
        # action handle -> the spec it was created from
        self._actions: dict[str, ActionSpec] = {}
        # section name -> the Channel handle its metrics were put in
        self._sections: dict[str, str] = {}
        # Sources arriving during an alert evaluation are retained for its next pass. The
        # service lock protects the set; the non-blocking lock elects exactly one drainer.
        self._pending_alert_sources: set[str] = set()
        self._alert_evaluator_lock = threading.Lock()
        self._lock = threading.RLock()
        self._profile_import_active = False
        self._generator_start_deferred = False
        self._mds_mode_before_demo: pm_types.MdsOperatingMode | None = None
        self._adapter: Sdc11073V3Adapter | None = None
        self._sample_generator = DemoSampleGenerator(
            lambda: self.mdib,
            lambda: self._specs,
            self._lock,
            instance_name,
        )

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
        if self._discovery is not None or self._provider is not None or self._mdib is not None:
            msg = "provider is already started"
            raise RuntimeError(msg)

        try:
            # Validate key, certificate, CA bundle, and TLS policy before any listener exists.
            self._tls_contexts = self.tls_config.create_contexts() if self.tls_config is not None else None
            self._discovery = WSDiscovery(self.ip)
            self._discovery.start()

            self._mdib = ProviderMdib.from_mdib_file(str(constants.BOOTSTRAP_MDIB_PATH))
            # The handler tells us which metric it touched once its transaction has closed, so a
            # value written by a remote consumer raises a limit alarm exactly as a local edit
            # does. It cannot be done from a metrics observable: sdc11073 fires those while
            # holding the transaction lock, and opening the alert transaction there deadlocks.
            self._handler = make_set_handler(
                self._mdib,
                coerce_value=self._coerce_value,
                on_applied=self._on_metric_applied,
                execution_lock=self._lock,
            )
            self._activate_handler = make_activate_handler(
                self._mdib,
                execute_effects=self._execute_action_effects,
            )

            this_model = ThisModelType(
                manufacturer=self.device.manufacturer,
                manufacturer_url=self.device.manufacturer_url,
                model_name=self.device.model_name,
                model_number=self.device.model_number,
                model_url=self.device.manufacturer_url,
                presentation_url=self.device.manufacturer_url,
            )
            this_device = ThisDeviceType(
                friendly_name=self.friendly_name,
                firmware_version=self.device.firmware_version,
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
                ssl_context_container=self._tls_contexts,
                alternative_hostname=(self.tls_config.server_name if self.tls_config is not None else None),
            )

            # No waveform provider configured, so the real-time sample loop must stay off.
            self._provider.start_all(start_rtsample_loop=False)
            if self._sco is not None:
                self._adapter = Sdc11073V3Adapter(self._mdib)
                self._adapter.bind(self._provider, self._sco)
            self.set_location(LocationInfo(**constants.DEFAULT_LOCATION))
            self.set_patient(DEFAULT_PATIENT)

            if self._sco is None:
                msg = "no SCO registry was created - the bootstrap MDIB is missing its Sco element"
                raise RuntimeError(msg)
        except Exception:
            try:
                self.stop()
            except Exception:
                logger.exception("error while rolling back provider startup")
            raise

        logger.info("provider %r up on %s, EPR %s", self.instance_name, self.ip, self.epr.urn)

    def stop(self) -> None:
        """Take the provider off the network."""
        provider = self._provider
        discovery = self._discovery
        first_error: Exception | None = None
        first_traceback = None

        cleanups = [("sample generator", self.stop_generator)]
        if provider is not None:
            cleanups.append(("provider", provider.stop_all))
        if discovery is not None:
            cleanups.append(("discovery", discovery.stop))

        for name, cleanup in cleanups:
            try:
                cleanup()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                    first_traceback = exc.__traceback__
                logger.exception("error while stopping provider %s", name)

        with self._lock:
            self._discovery = None
            self._provider = None
            self._mdib = None
            self._sco = None
            self._handler = None
            self._activate_handler = None
            self._adapter = None
            self._tls_contexts = None
            self._operations.clear()
            self._specs.clear()
            self._alerts.clear()
            self._alert_truths.clear()
            self._alert_signals.clear()
            self._pending_alert_sources.clear()
            self._actions.clear()
            self._sections.clear()
            self._sample_generator.clear()
            self._mds_mode_before_demo = None
        logger.info("provider %r stopped", self.instance_name)
        if first_error is not None:
            raise first_error.with_traceback(first_traceback)

    @property
    def tls_enabled(self) -> bool:
        """Whether this provider is configured to serve SDC over mutually authenticated TLS."""
        return self.tls_config is not None

    @property
    def local_certificate(self) -> CertificateInfo | None:
        """Safe metadata for the configured local certificate, when TLS is enabled."""
        return self.tls_config.local_certificate() if self.tls_config is not None else None

    def _snapshot_configuration(self) -> _ConfigurationSnapshot:
        """Clone all mutable provider state needed to undo a profile import."""
        with self._lock:
            mdib = self.mdib
            if self._adapter is None:
                raise RuntimeError("provider adapter is not available")
            return _ConfigurationSnapshot(
                context_states=deepcopy(list(mdib.context_states.objects)),
                operations=dict(self._operations),
                specs=dict(self._specs),
                alerts=dict(self._alerts),
                alert_truths=dict(self._alert_truths),
                alert_signals={handle: list(signals) for handle, signals in self._alert_signals.items()},
                actions=dict(self._actions),
                sections=dict(self._sections),
                library=self._adapter.snapshot(),
                pending_alert_sources=set(self._pending_alert_sources),
                mds_operating_mode=mdib.entities.by_handle(constants.MDS_HANDLE).state.OperatingMode,
                mds_mode_before_demo=self._mds_mode_before_demo,
                sample_generator=self._sample_generator.snapshot(),
                generator_running=self.generator_running,
            )

    def _restore_configuration(
            self,
            snapshot: _ConfigurationSnapshot,
            touched_contexts: set[str],
    ) -> None:
        """Restore a snapshot with compensating reports for connected consumers."""
        with self._lock:
            mdib = self.mdib
            if self._adapter is None:
                raise RuntimeError("provider adapter is not available")
            self._operations = snapshot.operations
            self._specs = snapshot.specs
            self._alerts = snapshot.alerts
            self._alert_truths = snapshot.alert_truths
            self._alert_signals = snapshot.alert_signals
            self._actions = snapshot.actions
            self._sections = snapshot.sections
            self._pending_alert_sources = snapshot.pending_alert_sources
            self._mds_mode_before_demo = snapshot.mds_mode_before_demo
            self._sample_generator.restore(snapshot.sample_generator)

            saved_descriptors = {descriptor.Handle: descriptor for descriptor in snapshot.library.descriptors}
            managed_handles = (
                set(snapshot.specs)
                | set(snapshot.operations.values())
                | set(snapshot.alerts)
                | set(snapshot.actions)
                | {signal for signals in snapshot.alert_signals.values() for signal in signals}
                | set(snapshot.sections.values())
            )
            managed_handles.update(
                saved_descriptors[channel].parent_handle
                for channel in snapshot.sections.values()
            )
            while descendants := {
                descriptor.Handle
                for descriptor in snapshot.library.descriptors
                if descriptor.parent_handle in managed_handles and descriptor.Handle not in managed_handles
            }:
                managed_handles.update(descendants)
            current = {handle: entity for handle, entity in mdib.entities.items()}
            removal_candidates = (set(current) - set(saved_descriptors)) | managed_handles
            removal_handles = {
                handle
                for handle in removal_candidates
                if handle in current and current[handle].parent_handle not in removal_candidates
            }

            with mdib.descriptor_transaction() as mgr:
                for handle in removal_handles:
                    mgr.remove_entity(current[handle])

            restore_descriptors = []
            for handle in managed_handles:
                saved_descriptor = saved_descriptors[handle]
                restore_descriptors.append(saved_descriptor)
            self._adapter.recreate_entities(restore_descriptors, snapshot.library.states)
            self._adapter.restore_registered_operations(snapshot.library.registered_operations)
            self._compensate_contexts(snapshot, touched_contexts)
            self._coordinate_alert_transition()
            self._set_mds_operating_mode(snapshot.mds_operating_mode)

    def _restore_appended_configuration(
            self,
            snapshot: _ConfigurationSnapshot,
            touched_contexts: set[str],
    ) -> None:
        """Remove an interrupted append without recreating unchanged live descriptors."""
        with self._lock:
            mdib = self.mdib
            if self._adapter is None:
                raise RuntimeError("provider adapter is not available")
            saved_descriptors = {descriptor.Handle: descriptor for descriptor in snapshot.library.descriptors}
            added_handles = {handle for handle, _ in mdib.entities.items()} - set(saved_descriptors)
            added_roots = {
                handle
                for handle in added_handles
                if mdib.entities.by_handle(handle).parent_handle not in added_handles
            }

            self._operations = snapshot.operations
            self._specs = snapshot.specs
            self._alerts = snapshot.alerts
            self._alert_truths = snapshot.alert_truths
            self._alert_signals = snapshot.alert_signals
            self._actions = snapshot.actions
            self._sections = snapshot.sections
            self._pending_alert_sources = snapshot.pending_alert_sources
            self._mds_mode_before_demo = snapshot.mds_mode_before_demo
            self._sample_generator.restore(snapshot.sample_generator)
            self._adapter.restore_registered_operations(snapshot.library.registered_operations)

            if added_roots:
                with mdib.descriptor_transaction() as mgr:
                    for handle in added_roots:
                        mgr.remove_entity(mdib.entities.by_handle(handle))

            self._compensate_contexts(snapshot, touched_contexts)
            self._coordinate_alert_transition()
            self._set_mds_operating_mode(snapshot.mds_operating_mode)

    def _compensate_contexts(self, snapshot: _ConfigurationSnapshot, handles: set[str]) -> None:
        """Restore context meaning without deleting associated states from history."""
        if self._adapter is None:
            raise RuntimeError("provider adapter is not available")
        associated = {}
        for state in snapshot.context_states:
            if state.ContextAssociation == pm_types.ContextAssociation.ASSOCIATED:
                associated[state.DescriptorHandle] = state
        for handle in sorted(handles):
            if handle != constants.LOCATION_CONTEXT_HANDLE:
                self._adapter.compensate_context(handle, associated.get(handle))
        if constants.LOCATION_CONTEXT_HANDLE in handles:
            self._adapter.restore_location(snapshot.library.provider_location)

    def import_profile(self, device: DeviceConfig, *, replace: bool = True) -> tuple[int, int]:
        """Preflight, apply, and if needed compensate one profile as an indivisible operation."""
        was_running = self.generator_running
        if was_running:
            # The worker takes _lock. Join before taking the import gate to avoid shutdown deadlock.
            self.stop_generator()

        failure: Exception | None = None
        rollback_failure: Exception | None = None
        field = "profile"
        self._alert_evaluator_lock.acquire()
        try:
            with self._lock:
                self._profile_import_active = True
                self._generator_start_deferred = False
                snapshot = None
                touched_contexts: set[str] = set()
                try:
                    self._preflight_profile(device, replace=replace)
                    snapshot = self._snapshot_configuration()
                    if replace:
                        for handle in list(self.list_actions()):
                            field = f"profile.replace.actions[{handle}]"
                            self.remove_action(handle)
                        for handle in list(self.list_alerts()):
                            field = f"profile.replace.alerts[{handle}]"
                            self.remove_alert(handle)
                        for handle in list(self.list_metrics()):
                            field = f"profile.replace.metrics[{handle}]"
                            self.remove_metric(handle)
                        self._remove_empty_sections()

                    for spec in device.metrics:
                        field = f"metrics[{spec.label}]"
                        self.add_metric(spec)
                    for spec in device.alerts:
                        field = f"alerts[{spec.label}]"
                        self.add_alert(spec)
                    for action in device.actions:
                        field = f"actions[{action.label}]"
                        self.add_action(action)
                    if device.location is not None:
                        field = "contexts.location"
                        touched_contexts.add(constants.LOCATION_CONTEXT_HANDLE)
                        self.set_location(device.location)
                    if device.patient is not None:
                        field = "contexts.patient"
                        touched_contexts.add(constants.PATIENT_CONTEXT_HANDLE)
                        self.set_patient(device.patient)
                    pending = set(self._pending_alert_sources)
                    self._pending_alert_sources.clear()
                    if pending:
                        _, alert_error = self._apply_alert_updates(pending)
                        if alert_error is not None:
                            raise alert_error
                    if was_running or self._generator_start_deferred:
                        field = "profile.generator"
                        self._sample_generator.start()
                except Exception as exc:  # noqa: BLE001 - all live failures require compensation
                    failure = exc
                    if snapshot is not None:
                        try:
                            if replace:
                                self._restore_configuration(snapshot, touched_contexts)
                            else:
                                self._restore_appended_configuration(snapshot, touched_contexts)
                        except Exception as exc2:  # noqa: BLE001 - provider must fail closed
                            rollback_failure = exc2
                    if rollback_failure is None and (
                        was_running or snapshot is not None and snapshot.generator_running
                    ):
                        try:
                            self._sample_generator.start()
                        except Exception as exc2:  # noqa: BLE001 - provider must fail closed
                            rollback_failure = exc2
                finally:
                    self._profile_import_active = False
                    self._generator_start_deferred = False
        finally:
            self._alert_evaluator_lock.release()

        if rollback_failure is not None:
            try:
                self.stop()
            except Exception:
                logger.exception("failed to stop provider after profile compensation failure")
            msg = f"{field}: profile import failed ({failure}); rollback failed: {rollback_failure}; provider stopped"
            raise RuntimeError(msg) from failure
        if failure is not None:
            msg = f"{field}: profile import failed: {failure}"
            raise RuntimeError(msg) from failure
        return len(device.metrics), len(device.alerts)

    def _preflight_profile(self, device: DeviceConfig, *, replace: bool) -> None:  # noqa: C901, PLR0912
        """Validate the prospective graph and historical descriptor datatypes."""
        if self._adapter is None:
            raise RuntimeError("provider adapter is not available")
        descriptors = {handle for handle, _ in self.mdib.entities.items()}
        metric_specs = self.list_metrics()
        metrics = set(metric_specs)
        removed: set[str] = set()
        if replace:
            removed = set(self.list_actions()) | set(self.list_alerts()) | set(self.list_metrics())
            for alert_handle in self.list_alerts():
                removed.update(self.signal_handles_for(alert_handle))
            for metric_handle in self.list_metrics():
                operation_handle = self.operation_handle_for(metric_handle)
                if operation_handle is not None:
                    removed.add(operation_handle)
            removed.update(self._section_descriptor_handles())
            descriptors.difference_update(removed)
            metric_specs.clear()
            metrics.clear()

        section_handles = set()
        for section in {spec.section for spec in device.metrics if spec.section}:
            slug = slugify(section)
            vmd_handle = constants.VMD_HANDLE_PREFIX + slug
            channel_handle = constants.CHANNEL_HANDLE_PREFIX + slug
            section_handles.update((vmd_handle, channel_handle))
            vmd = self.mdib.entities.by_handle(vmd_handle)
            channel = self.mdib.entities.by_handle(channel_handle)
            if vmd_handle in removed:
                vmd = None
            if channel_handle in removed:
                channel = None
            if vmd is not None and (vmd.node_type != pm.VmdDescriptor or vmd.parent_handle != constants.MDS_HANDLE):
                raise ValueError(
                    f"sections[{section}]: {vmd_handle!r} must be a VmdDescriptor under {constants.MDS_HANDLE!r}",
                )
            if channel is not None and (
                channel.node_type != pm.ChannelDescriptor or channel.parent_handle != vmd_handle
            ):
                raise ValueError(
                    f"sections[{section}]: {channel_handle!r} must be a ChannelDescriptor under {vmd_handle!r}",
                )
            if channel is not None and vmd is None:
                raise ValueError(f"sections[{section}]: {channel_handle!r} requires live parent {vmd_handle!r}")
            self._adapter.validate_descriptor_type(vmd_handle, pm.VmdDescriptor)
            self._adapter.validate_descriptor_type(channel_handle, pm.ChannelDescriptor)

        descriptors.update(section_handles)
        for spec in device.metrics:
            if spec.handle is None:
                raise ValueError(f"metrics[{spec.label}]: metric handle was not resolved during parsing")
            self._claim_profile_handle(descriptors, spec.handle, f"metrics[{spec.label}]")
            self._adapter.validate_descriptor_type(spec.handle, spec.kind.descriptor_qname)
            metrics.add(spec.handle)
            metric_specs[spec.handle] = spec
            if spec.controllable:
                operation = constants.OPERATION_HANDLE_PREFIX + spec.handle.removeprefix(constants.METRIC_HANDLE_PREFIX)
                operation = self._claim_profile_handle(descriptors, operation, f"metrics[{spec.label}] control")
                operation_type = spec.kind.operation_class.OP_DESCR_QNAME
                self._adapter.validate_descriptor_type(operation, operation_type)

        for spec in device.alerts:
            if spec.source_handle not in metrics:
                raise ValueError(
                    f"alerts[{spec.label}]: watches {spec.source_handle!r}, which is not a metric available after import",
                )
            if spec.has_limits and metric_specs[spec.source_handle].kind is not MetricKind.NUMBER:
                raise ValueError(f"alerts[{spec.label}]: a limit alarm requires a numeric scalar source")
            handle = spec.handle or constants.ALERT_HANDLE_PREFIX + spec.slug
            handle = self._claim_profile_handle(descriptors, handle, f"alerts[{spec.label}]")
            alert_type = pm.LimitAlertConditionDescriptor if spec.has_limits else pm.AlertConditionDescriptor
            self._adapter.validate_descriptor_type(handle, alert_type)
            for signal in spec.signals:
                signal_handle = constants.SIGNAL_HANDLE_PREFIX + spec.slug + "." + signal.manifestation.value.lower()
                signal_handle = self._claim_profile_handle(descriptors, signal_handle, f"alerts[{spec.label}] signal")
                self._adapter.validate_descriptor_type(signal_handle, pm.AlertSignalDescriptor)

        normalized_effects = []
        for spec in device.actions:
            if spec.target_handle not in descriptors:
                raise ValueError(f"actions[{spec.label}]: target {spec.target_handle!r} is not available after import")
            effects = {}
            for effect_handle, value in spec.effects.items():
                if effect_handle not in metrics:
                    raise ValueError(
                        f"actions[{spec.label}].effects: {effect_handle!r} is not a metric available after import",
                    )
                effects[effect_handle] = coerce_metric_value(metric_specs[effect_handle], value, effect_handle)
            normalized_effects.append((spec, effects))
            handle = spec.handle or constants.ACTION_HANDLE_PREFIX + spec.slug
            handle = self._claim_profile_handle(descriptors, handle, f"actions[{spec.label}]")
            self._adapter.validate_descriptor_type(handle, pm.ActivateOperationDescriptor)

        if device.location is not None and device.location.is_empty():
            raise ValueError("contexts.location: an explicit location needs at least one detail")
        if device.patient is not None:
            if device.patient.sex:
                pm_types.Sex(device.patient.sex)
            if device.patient.patient_type:
                pm_types.PatientType(device.patient.patient_type)
            if device.patient.date_of_birth:
                DateOfBirthProperty.mk_value_object(device.patient.date_of_birth)
        for spec, effects in normalized_effects:
            spec.effects = effects

    @staticmethod
    def _claim_profile_handle(handles: set[str], handle: str, field: str) -> str:
        if handle in handles:
            raise ValueError(f"{field}: handle {handle!r} already exists")
        handles.add(handle)
        return handle

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
            if spec.is_sample_array:
                # MetricSpec is mutable, so repeat this preflight immediately before any
                # lazy section, descriptor, bookkeeping, or generator mutation.
                if spec.kind is MetricKind.WAVEFORM:
                    spec.waveform_cycle_sample_count()
                    spec.generated_waveform_block_sample_count()
                spec.generated_sample_range()
            if spec.kind is MetricKind.DISTRIBUTION:
                lower = spec.domain_minimum if spec.domain_minimum is not None else Decimal("0")
                upper = spec.domain_maximum if spec.domain_maximum is not None else Decimal("1")
                if lower >= upper:
                    msg = f"distribution domain must increase, not {lower} to {upper}"
                    raise ValueError(msg)
                # Compute this before lazily creating a section or any metric entity.
                domain_step(spec)
            if spec.is_sample_array and spec.initial_value is not None:
                msg = f"initial_value is not meaningful for {spec.kind.value} metrics; use samples instead"
                raise ValueError(msg)

            handle = spec.handle or self._unique_handle(constants.METRIC_HANDLE_PREFIX + spec.slug)
            if self.mdib.entities.by_handle(handle) is not None:
                msg = f"handle {handle!r} already exists"
                raise ValueError(msg)

            entity = self.mdib.entities.new_entity(
                spec.kind.descriptor_qname,
                handle,
                self._channel_for(spec.section),
            )
            self._apply_spec_to_descriptor(entity.descriptor, spec)

            self._create_entities([entity])
            if spec.is_sample_array:
                self._set_demo_mode()

            self._specs[handle] = spec
            logger.info("added %s metric %r (%s)", spec.kind.value, spec.label, handle)

            if spec.initial_value is not None:
                self.set_value(handle, spec.initial_value)
            if spec.controllable:
                self.enable_control(handle)
            if spec.is_sample_array:
                # A sample array with nothing driving it publishes a descriptor and never a
                # sample, which looks like a broken device rather than an idle one. That is
                # what a distribution used to do: there was no way to fill one from the
                # window at all, so its card said 'waiting for samples' for ever.
                self.start_generator()
            return handle

    def remove_metric(self, handle: str) -> None:
        """Delete a data source and everything that depends on it."""
        with self._lock:
            metric_entity = self.mdib.entities.by_handle(handle)
            section_entities = []
            dependent_handles = {handle}
            if metric_entity is not None and metric_entity.parent_handle in self._section_channels():
                channel_handle = metric_entity.parent_handle
                section_entities, section_handles = self._empty_section_entities(
                    channel_handle,
                    excluding_metric=handle,
                )
                dependent_handles.update(section_handles)

            alert_handles = sorted(
                alert_handle
                for alert_handle, spec in self._alerts.items()
                if spec.source_handle == handle
            )
            signal_handles = sorted(
                signal_handle
                for alert_handle in alert_handles
                for signal_handle in self._alert_signals.get(alert_handle, [])
            )
            dependent_handles.update(alert_handles)
            dependent_handles.update(signal_handles)

            operation_handle = self._operations.get(handle)
            if operation_handle is not None:
                dependent_handles.add(operation_handle)

            action_handles = self._dependent_action_handles(
                dependent_handles,
                effect_metric=handle,
            )
            if alert_handles:
                self._coordinate_alert_transition(retiring_handles=set(alert_handles))
            for action_handle in action_handles:
                self._sco.unregister_operation_by_handle(action_handle)
            if operation_handle is not None:
                self._sco.unregister_operation_by_handle(operation_handle)

            # Drop the bookkeeping before committing. Leaving the transaction fires
            # deleted_descriptors_by_handle synchronously, and any observer that reacts by
            # listing our metrics would otherwise see a handle whose entity is already gone.
            for action_handle in action_handles:
                self._actions.pop(action_handle, None)
            self._operations.pop(handle, None)
            for alert_handle in alert_handles:
                self._alerts.pop(alert_handle, None)
                self._alert_truths.pop(alert_handle, None)
                self._alert_signals.pop(alert_handle, None)
            self._specs.pop(handle, None)
            self._pending_alert_sources.discard(handle)
            self._sample_generator.forget(handle)
            for entity in section_entities:
                if entity.node_type == pm.ChannelDescriptor:
                    self._forget_section(entity.handle)

            removal_handles = [
                *action_handles,
                *([operation_handle] if operation_handle is not None else []),
                *signal_handles,
                *alert_handles,
                handle,
                *(entity.handle for entity in section_entities),
            ]
            with self.mdib.descriptor_transaction() as mgr:
                for removal_handle in removal_handles:
                    entity = self.mdib.entities.by_handle(removal_handle)
                    if entity is not None:
                        mgr.remove_entity(entity)
            self._restore_mds_mode_without_demo_metrics()

            logger.info("removed metric %s", handle)

    def _remove_empty_sections(self) -> None:
        """Delete section containment left without any owned metrics."""
        with self._lock:
            for channel_handle in self._section_channels():
                if self.mdib.entities.by_handle(channel_handle) is None:
                    self._forget_section(channel_handle)
                    continue
                entities, handles = self._empty_section_entities(channel_handle)
                if not entities:
                    continue
                for action_handle, spec in list(self._actions.items()):
                    if spec.target_handle in handles:
                        self.remove_action(action_handle)
                self._forget_section(channel_handle)
                with self.mdib.descriptor_transaction() as mgr:
                    for entity in entities:
                        mgr.remove_entity(entity)

    def set_value(self, handle: str, value: Decimal | str) -> None:
        """Set the current value of one of our own metrics."""
        with self._lock:
            spec = self._specs.get(handle)
            entity = self.mdib.entities.by_handle(handle)
            if spec is None or entity is None:
                msg = f"no metric with handle {handle!r}"
                raise KeyError(msg)
            normalized = coerce_metric_value(spec, value, handle)
            apply_metric_value(entity.state, normalized)
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
                    # The operation is our own construct, not a term from anywhere, so it
                    # stays in the private system even when its target carries an MDC code.
                    coded_value=pm_types.CodedValue(
                        code=f"set_{spec.effective_type().code}",
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
            source = self.mdib.entities.by_handle(spec.source_handle)
            if source is None:
                msg = f"no metric with handle {spec.source_handle!r} to watch"
                raise KeyError(msg)
            source_spec = self._specs.get(spec.source_handle)
            if spec.has_limits and (
                source_spec is None or source_spec.kind is not MetricKind.NUMBER
            ):
                msg = "a limit alarm requires a numeric scalar source"
                raise ValueError(msg)

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
                descriptor.MaxLimits = pm_types.Range(
                    lower=(
                        fixed_point_decimal(spec.lower_limit, "lower_limit")
                        if spec.lower_limit is not None
                        else None
                    ),
                    upper=(
                        fixed_point_decimal(spec.upper_limit, "upper_limit")
                        if spec.upper_limit is not None
                        else None
                    ),
                )

            entity.state.ActivationState = pm_types.AlertActivation.ON
            entity.state.Presence = False
            if spec.has_limits:
                entity.state.Limits = pm_types.Range(
                    lower=(
                        fixed_point_decimal(spec.lower_limit, "lower_limit")
                        if spec.lower_limit is not None
                        else None
                    ),
                    upper=(
                        fixed_point_decimal(spec.upper_limit, "upper_limit")
                        if spec.upper_limit is not None
                        else None
                    ),
                )

            signal_entities = []
            for signal_spec in spec.signals:
                manifestation = signal_spec.manifestation
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
                signal.descriptor.Latching = signal_spec.latching
                signal.descriptor.SignalDelegationSupported = spec.delegable
                signal.state.ActivationState = pm_types.AlertActivation.ON
                signal.state.Presence = pm_types.AlertSignalPresence.OFF
                signal.state.Location = pm_types.AlertSignalPrimaryLocation.LOCAL
                signal_entities.append(signal)

            self._create_entities([entity, *signal_entities])

            self._alerts[handle] = spec
            self._alert_truths[handle] = False
            self._alert_signals[handle] = [s.handle for s in signal_entities]
            logger.info(
                "added alarm %r watching %s (%s)",
                spec.label,
                spec.source_handle,
                spec.limit_text() or "manual",
            )

            if spec.has_limits:
                self._evaluate_alerts({spec.source_handle})
            else:
                self._coordinate_alert_transition()
            return handle

    def remove_alert(self, handle: str) -> None:
        """Delete an alarm condition together with its signals."""
        with self._lock:
            signal_handles = sorted(self._alert_signals.get(handle, []))
            dependent_handles = {handle, *signal_handles}
            action_handles = self._dependent_action_handles(dependent_handles)
            if handle in self._alerts:
                self._coordinate_alert_transition(retiring_handles={handle})
            for action_handle in action_handles:
                self._sco.unregister_operation_by_handle(action_handle)
                self._actions.pop(action_handle, None)

            self._alert_signals.pop(handle, None)
            self._alerts.pop(handle, None)
            self._alert_truths.pop(handle, None)
            entities = [
                entity
                for entity in (
                    self.mdib.entities.by_handle(h)
                    for h in [*action_handles, *signal_handles, handle]
                )
                if entity is not None
            ]
            with self.mdib.descriptor_transaction() as mgr:
                for entity in entities:
                    mgr.remove_entity(entity)
            logger.info("removed alarm %s", handle)

    def set_alert_presence(self, handle: str, present: bool) -> None:  # noqa: FBT001
        """Raise or clear an alarm by hand.

        Limit-condition truth is always derived from its numeric source and cannot be
        overridden manually.
        """
        with self._lock:
            spec = self._alerts.get(handle)
            if spec is None:
                msg = f"no alarm with handle {handle!r}"
                raise KeyError(msg)
            if spec.has_limits:
                msg = f"{handle!r} is a limit alarm; change its source metric instead"
                raise ValueError(msg)
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
                    latching=bool(entity.descriptor.Latching),
                ),
            )
        return infos

    def acknowledge_alert(self, handle: str) -> int:
        """Acknowledge each currently generated ``On`` signal of a present condition."""
        with self._lock:
            if handle not in self._alert_signals:
                msg = f"no alarm with handle {handle!r}"
                raise KeyError(msg)
            if not self.alert_present(handle):
                msg = f"{handle!r} is not raised, so there is nothing to acknowledge"
                raise ValueError(msg)
            _, acknowledged = self._coordinate_alert_transition(acknowledge_handle=handle)
            logger.info("acknowledged %d signal(s) of %s", acknowledged, handle)
            return acknowledged

    def stop_latched_signals(self, handle: str) -> int:
        """Deliberately stop signals that are currently latching after a cleared condition."""
        with self._lock:
            if handle not in self._alert_signals:
                msg = f"no alarm with handle {handle!r}"
                raise KeyError(msg)
            _, stopped = self._coordinate_alert_transition(stop_latches_handle=handle)
            logger.info("stopped %d latched signal(s) of %s", stopped, handle)
            return stopped

    def set_signal_delegated(self, signal_handle: str, *, delegated: bool) -> None:
        """Simulate primary location without claiming a normative delegation handoff.

        The legacy method name describes the learning scenario, not a complete Clause 6
        handoff. This only writes ``Location`` and derives local system-signal activation.
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
            self._coordinate_alert_transition(signal_locations={signal_handle: location})
            logger.info("signal %s location simulation is now %s", signal_handle, location)

    def _on_metric_applied(self, metric_handle: str) -> None:
        """A metric changed, by whatever route. Recompute the alarms watching it."""
        self._evaluate_alerts({metric_handle})

    def _evaluate_alerts(self, source_handles: set[str]) -> None:
        if not source_handles:
            return

        with self._lock:
            if self._mdib is None:
                return
            self._pending_alert_sources.update(source_handles)
            if not self._alert_evaluator_lock.acquire(blocking=False):
                return

        owns_evaluator = True
        first_error: Exception | None = None
        try:
            while True:
                with self._lock:
                    if self._mdib is None:
                        self._pending_alert_sources.clear()
                    if not self._pending_alert_sources:
                        # Release while holding the queue lock. A later caller therefore
                        # either sees this evaluator or becomes the next one itself.
                        self._alert_evaluator_lock.release()
                        owns_evaluator = False
                        break
                    pending = set(self._pending_alert_sources)
                    self._pending_alert_sources.clear()
                _, error = self._apply_alert_updates(pending)
                first_error = first_error or error

            if first_error is not None:
                raise first_error
        finally:
            if owns_evaluator:
                with self._lock:
                    self._alert_evaluator_lock.release()

    def _apply_alert_updates(
            self,
            source_handles: set[str],
    ) -> tuple[mdibbase.MdibVersionGroup | None, Exception | None]:
        """Apply all derived alert changes in one transaction and retain its exact version."""
        with self._lock:
            alerts = [
                (handle, spec)
                for handle, spec in self._alerts.items()
                if spec.has_limits and spec.source_handle in source_handles
            ]

        try:
            condition_truths = {}
            for handle, spec in alerts:
                value = self.get_value(spec.source_handle)
                condition_truths[handle] = spec.breached_by(value)
            version, _ = self._coordinate_alert_transition(condition_truths=condition_truths)
            return version, None
        except Exception as exc:  # noqa: BLE001 - preserve a post-commit report failure
            # The alert transaction may commit before report serialization raises.
            # Action execution holds the service lock, so this cannot include a later
            # unrelated update when it is used as the final invocation version.
            return self.mdib.mdib_version_group, exc

    def _coordinate_alert_transition(
            self,
            *,
            condition_truths: dict[str, bool] | None = None,
            acknowledge_handle: str | None = None,
            stop_latches_handle: str | None = None,
            signal_locations: dict[str, pm_types.AlertSignalPrimaryLocation] | None = None,
            retiring_handles: set[str] | None = None,
    ) -> tuple[mdibbase.MdibVersionGroup | None, int]:
        """Reduce requested alert changes and commit conditions, signals, and parent once."""
        with self._lock:
            return self._coordinate_alert_transition_locked(
                condition_truths=condition_truths,
                acknowledge_handle=acknowledge_handle,
                stop_latches_handle=stop_latches_handle,
                signal_locations=signal_locations,
                retiring_handles=retiring_handles,
            )

    def _coordinate_alert_transition_locked(
            self,
            *,
            condition_truths: dict[str, bool] | None,
            acknowledge_handle: str | None,
            stop_latches_handle: str | None,
            signal_locations: dict[str, pm_types.AlertSignalPrimaryLocation] | None,
            retiring_handles: set[str] | None,
    ) -> tuple[mdibbase.MdibVersionGroup | None, int]:
        condition_truths = condition_truths or {}
        signal_locations = signal_locations or {}
        retiring_handles = retiring_handles or set()
        unknown_conditions = (set(condition_truths) | retiring_handles) - self._alerts.keys()
        if unknown_conditions:
            handle = sorted(unknown_conditions)[0]
            msg = f"no alarm with handle {handle!r}"
            raise KeyError(msg)
        self._alert_truths.update(condition_truths)
        self._alert_truths.update(dict.fromkeys(retiring_handles, False))
        alert_system = self.mdib.entities.by_handle(constants.ALERT_SYSTEM_HANDLE)
        if alert_system is None:
            raise RuntimeError("the MDIB has no parent AlertSystem")

        handles = set(self._alerts)

        changed = []
        affected_signals = 0
        next_condition_presence = {}
        for handle in sorted(handles):
            condition = self.mdib.entities.by_handle(handle)
            if condition is None or handle not in self._alerts:
                msg = f"no alarm with handle {handle!r}"
                raise KeyError(msg)
            signals = [
                signal
                for signal in (
                    self.mdib.entities.by_handle(signal_handle)
                    for signal_handle in self._alert_signals.get(handle, [])
                )
                if signal is not None
            ]
            if handle in retiring_handles:
                if condition.state.ActivationState is not pm_types.AlertActivation.OFF:
                    condition.state.ActivationState = pm_types.AlertActivation.OFF
                    changed.append(condition)
                for signal in signals:
                    if signal.state.ActivationState is not pm_types.AlertActivation.OFF:
                        signal.state.ActivationState = pm_types.AlertActivation.OFF
                        changed.append(signal)
            transition = _reduce_alert_transition(
                condition_truth=self._alert_truths[handle],
                current_presence=bool(condition.state.Presence),
                condition_activation=condition.state.ActivationState,
                system_activation=alert_system.state.ActivationState,
                signals=tuple(
                    _SignalTransitionInput(
                        activation=signal.state.ActivationState,
                        presence=signal.state.Presence,
                        latching=bool(signal.descriptor.Latching),
                    )
                    for signal in signals
                ),
                acknowledge=handle == acknowledge_handle,
                stop_latches=handle == stop_latches_handle,
            )
            next_condition_presence[handle] = transition.condition_presence
            if transition.condition_presence != bool(condition.state.Presence):
                condition.state.Presence = transition.condition_presence
                condition.state.DeterminationTime = time.time()
                changed.append(condition)
            for signal, presence in zip(signals, transition.signal_presences, strict=True):
                if signal.state.Presence == presence:
                    continue
                if (
                    handle == acknowledge_handle
                    and signal.state.Presence is pm_types.AlertSignalPresence.ON
                    and presence is pm_types.AlertSignalPresence.ACK
                ):
                    affected_signals += 1
                if (
                    handle == stop_latches_handle
                    and signal.state.Presence is pm_types.AlertSignalPresence.LATCH
                    and presence is pm_types.AlertSignalPresence.OFF
                ):
                    affected_signals += 1
                signal.state.Presence = presence
                changed.append(signal)

        known_signals = {
            signal_handle
            for signal_handles in self._alert_signals.values()
            for signal_handle in signal_handles
        }
        for signal_handle, location in signal_locations.items():
            if signal_handle not in known_signals:
                msg = f"no alarm signal with handle {signal_handle!r}"
                raise KeyError(msg)
            signal = self.mdib.entities.by_handle(signal_handle)
            if signal is None:
                msg = f"no alarm signal with handle {signal_handle!r}"
                raise KeyError(msg)
            if signal.state.Location != location:
                signal.state.Location = location
                changed.append(signal)

        physiological, technical = self._present_alarm_conditions(
            alert_system,
            next_condition_presence,
        )
        system_signals = self._system_signal_activation(
            alert_system,
            signal_locations,
            excluding_conditions=retiring_handles,
        )
        current_system_signals = [
            (entry.Manifestation, entry.State)
            for entry in alert_system.state.SystemSignalActivation
        ]
        parent_changed = (
            list(alert_system.state.PresentPhysiologicalAlarmConditions or ()) != physiological
            or list(alert_system.state.PresentTechnicalAlarmConditions or ()) != technical
            or current_system_signals != system_signals
        )
        if parent_changed:
            alert_system.state.PresentPhysiologicalAlarmConditions = physiological
            alert_system.state.PresentTechnicalAlarmConditions = technical
            alert_system.state.SystemSignalActivation = [
                pm_types.SystemSignalActivation(manifestation=manifestation, state=activation)
                for manifestation, activation in system_signals
            ]
            changed.append(alert_system)

        if not changed:
            return None, affected_signals
        unique = {entity.handle: entity for entity in changed}
        with self.mdib.alert_state_transaction() as mgr:
            for entity in unique.values():
                mgr.write_entity(entity)
            version_group = self._transaction_version(mgr)
        return version_group, affected_signals

    def _present_alarm_conditions(
            self,
            alert_system,
            presence_overrides: dict[str, bool],
    ) -> tuple[list[str], list[str]]:
        physiological = []
        technical = []
        if alert_system.state.ActivationState is not pm_types.AlertActivation.ON:
            return physiological, technical
        applicable_priorities = {
            pm_types.AlertConditionPriority.LOW,
            pm_types.AlertConditionPriority.MEDIUM,
            pm_types.AlertConditionPriority.HIGH,
        }
        for handle in sorted(self._alerts):
            condition = self.mdib.entities.by_handle(handle)
            if condition is None or not presence_overrides.get(handle, bool(condition.state.Presence)):
                continue
            if condition.state.ActivationState is not pm_types.AlertActivation.ON:
                continue
            priority = (
                condition.state.ActualPriority
                if condition.state.ActualPriority is not None
                else condition.descriptor.Priority
            )
            if priority not in applicable_priorities:
                continue
            if condition.descriptor.Kind is pm_types.AlertConditionKind.PHYSIOLOGICAL:
                physiological.append(handle)
            elif condition.descriptor.Kind is pm_types.AlertConditionKind.TECHNICAL:
                technical.append(handle)
        return physiological, technical

    def _system_signal_activation(
            self,
            alert_system,
            location_overrides: dict[str, pm_types.AlertSignalPrimaryLocation],
            *,
            excluding_conditions: set[str] | None = None,
    ) -> list[tuple[pm_types.AlertSignalManifestation, pm_types.AlertActivation]]:
        activations: dict[pm_types.AlertSignalManifestation, pm_types.AlertActivation] = {}
        excluding_conditions = excluding_conditions or set()
        rank = {
            pm_types.AlertActivation.OFF: 0,
            pm_types.AlertActivation.PAUSED: 1,
            pm_types.AlertActivation.ON: 2,
        }
        for condition_handle, signal_handles in self._alert_signals.items():
            if condition_handle in excluding_conditions:
                continue
            for signal_handle in signal_handles:
                signal = self.mdib.entities.by_handle(signal_handle)
                if signal is None:
                    continue
                location = location_overrides.get(signal_handle, signal.state.Location)
                if location is pm_types.AlertSignalPrimaryLocation.REMOTE:
                    continue
                activation = signal.state.ActivationState
                if alert_system.state.ActivationState is not pm_types.AlertActivation.ON:
                    activation = pm_types.AlertActivation.OFF
                manifestation = signal.descriptor.Manifestation
                current = activations.get(manifestation)
                if current is None or rank[activation] > rank[current]:
                    activations[manifestation] = activation
        return sorted(activations.items(), key=lambda item: item[0].value)

    def _write_alert_presence(
            self,
            handle: str,
            *,
            present: bool,
    ) -> mdibbase.MdibVersionGroup | None:
        version, _ = self._coordinate_alert_transition(condition_truths={handle: present})
        if version is not None:
            logger.info("alarm %s is now %s", handle, "present" if present else "clear")
        return version

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
        return patient_info_from_biceps(core)

    def set_patient(self, info: PatientInfo) -> None:
        """Associate a patient with the device, replacing whoever was there before.

        A context is a multi-state entity: the old state is disassociated rather than
        overwritten, so the MDIB keeps the history of who was attached when. That is why
        this cannot simply write over one state the way a metric does.
        """
        if info.is_empty():
            self.clear_patient()
            return
        with self._lock:
            entity = self.mdib.entities.by_handle(constants.PATIENT_CONTEXT_HANDLE)
            if entity is None:
                msg = f"the bootstrap MDIB has no {constants.PATIENT_CONTEXT_HANDLE}"
                raise RuntimeError(msg)

            core = pm_types.PatientDemographicsCoreData()
            core.Givenname = info.given_name or None
            core.Familyname = info.family_name or None
            core.Sex = pm_types.Sex(info.sex) if info.sex else None
            core.PatientType = pm_types.PatientType(info.patient_type) if info.patient_type else None
            if info.date_of_birth:
                # Raises ValueError on anything xsd:date cannot express, which is what we want:
                # a silently dropped birth date would look like the field does not work.
                core.set_birthdate(info.date_of_birth)
            if info.height is not None:
                core.Height = pm_types.Measurement(
                    patient_measurement_wire_value(info.height.value),
                    _coded_value(info.height.unit),
                )
            if info.weight is not None:
                core.Weight = pm_types.Measurement(
                    patient_measurement_wire_value(info.weight.value),
                    _coded_value(info.weight.unit),
                )
            if info.race is not None:
                core.Race = _coded_value(info.race)

            # Context transactions commit before report serialization. Serialize the complete
            # demographics first so malformed user/profile data cannot disassociate the prior
            # patient and leave an unpublishable replacement in the MDIB.
            try:
                core.as_etree_node(pm.CoreData, {})
            except (TypeError, ValueError) as exc:
                msg = f"patient demographics cannot be serialized: {exc}"
                raise ValueError(msg) from exc

            state = entity.new_state()
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
            self._sample_generator.publish_manual(handle, spec, samples)

    def get_samples(self, handle: str) -> list[Decimal]:
        """The samples currently published for a sample-array metric."""
        entity = self.mdib.entities.by_handle(handle)
        if entity is None:
            return []
        value = getattr(entity.state, "MetricValue", None)
        return list(getattr(value, "Samples", None) or [])

    def start_generator(self) -> None:
        """Begin generating samples for every unpinned sample-array metric."""
        if self._profile_import_active:
            self._generator_start_deferred = True
            return
        self._sample_generator.start()

    def stop_generator(self) -> None:
        """Stop generating samples and wait for its worker thread."""
        self._sample_generator.stop()

    @property
    def generator_running(self) -> bool:
        """Whether the generator thread is alive."""
        return self._sample_generator.running

    def _publish_one_block(self) -> None:
        """Publish one immediately due block for each healthy generated source."""
        self._sample_generator.publish_once()

    def _next_block(self, handle: str, spec: MetricSpec) -> tuple[list[Decimal], float]:
        """Return the next waveform block without advancing its committed phase."""
        return self._sample_generator.next_block(handle, spec)

    # -- actions -------------------------------------------------------------------

    def add_action(self, spec: ActionSpec) -> str:
        """Publish something the device can be told to do, and return its handle.

        An ActivateOperation, which is the BICEPS way of saying "do the thing" as opposed
        to "take this value". Registering it with the SCO creates the descriptor and emits
        the report, the same route a set operation takes.
        """
        with self._lock:
            if self.mdib.entities.by_handle(spec.target_handle) is None:
                msg = f"no descriptor with handle {spec.target_handle!r} for action {spec.label!r} to act on"
                raise KeyError(msg)
            operation_target = self._action_target_for(spec)
            spec.target_handle = operation_target

            handle = spec.handle or self._unique_handle(constants.ACTION_HANDLE_PREFIX + spec.slug)
            operation = ActivateOperation(
                handle=handle,
                operation_target_handle=operation_target,
                operation_handler=self._activate_handler,
                coded_value=_coded_value(spec.effective_type(), spec.label),
            )
            self._sco.register_operation(operation)
            self._actions[handle] = spec
            self._set_operating_mode(handle, pm_types.OperatingMode.ENABLED)
            logger.info("added action %r (%s) on %s", spec.label, handle, operation_target)
            return handle

    def remove_action(self, handle: str) -> None:
        """Delete an action and every action that transitively targets it."""
        with self._lock:
            if handle not in self._actions:
                return

            action_handles = self._dependent_action_handles({handle})
            if handle not in action_handles:
                action_handles.append(handle)

            for action_handle in action_handles:
                self._sco.unregister_operation_by_handle(action_handle)
                self._actions.pop(action_handle, None)

            with self.mdib.descriptor_transaction() as mgr:
                for action_handle in action_handles:
                    entity = self.mdib.entities.by_handle(action_handle)
                    if entity is not None:
                        mgr.remove_entity(entity)
            logger.info("removed action %s and %d dependent action(s)", handle, len(action_handles) - 1)

    def list_actions(self) -> dict[str, ActionSpec]:
        """The specs of every action we published, keyed by handle."""
        return dict(self._actions)

    def run_action(self, handle: str) -> None:
        """Invoke one of our own actions locally, as a remote consumer would.

        Deliberately routed through the same effects the remote path uses, so a button here
        and an invocation over the network cannot drift apart.
        """
        _, alert_error = self._execute_action_effects(handle)
        if alert_error is not None:
            raise alert_error.with_traceback(alert_error.__traceback__)

    def _execute_action_effects(
            self,
            operation_handle: str,
    ) -> tuple[mdibbase.MdibVersionGroup, Exception | None]:
        """Validate, atomically commit, and causally process one action's effects."""
        self._alert_evaluator_lock.acquire()
        try:
            with self._lock:
                spec = self._actions.get(operation_handle)
                if spec is None:
                    msg = f"no action with handle {operation_handle!r}"
                    raise KeyError(msg)
                operation = self.mdib.entities.by_handle(operation_handle)
                operation_target = getattr(getattr(operation, "descriptor", None), "OperationTarget", None)
                valid_target = self._action_target_for(spec)
                if operation_target != valid_target:
                    msg = (
                        f"action {operation_handle!r} OperationTarget {operation_target!r} does not contain "
                        "all current effects"
                    )
                    raise ValueError(msg)

                prepared = []
                for target, value in spec.effects.items():
                    entity = self.mdib.entities.by_handle(target)
                    if entity is None:
                        msg = f"effect target {target!r} does not exist"
                        raise KeyError(msg)
                    prepared.append((entity, self._coerce_value(target, value)))

                # A setter can queue its source after this helper wins the evaluator but
                # before it wins the service lock. Finish that earlier causal work first.
                pending = set(self._pending_alert_sources)
                self._pending_alert_sources.clear()
                if pending:
                    _, pending_error = self._apply_alert_updates(pending)
                    if pending_error is not None:
                        logger.error(
                            "queued alert processing failed before action %s",
                            operation_handle,
                            exc_info=(type(pending_error), pending_error, pending_error.__traceback__),
                        )

                version_group = self.mdib.mdib_version_group
                transaction_error = None
                if prepared:
                    before_group = self.mdib.mdib_version_group
                    before_state_versions = tuple(
                        (entity.handle, entity.state.StateVersion) for entity, _ in prepared
                    )
                    for entity, value in prepared:
                        apply_metric_value(entity.state, value)
                    try:
                        with self.mdib.metric_state_transaction() as mgr:
                            for entity, _ in prepared:
                                mgr.write_entity(entity)
                            version_group = self._transaction_version(mgr)
                    except Exception as exc:  # noqa: BLE001 - distinguish commit from report failure
                        after_group = self.mdib.mdib_version_group
                        after_state_versions = tuple(
                            (
                                entity.handle,
                                self.mdib.entities.by_handle(entity.handle).state.StateVersion,
                            )
                            for entity, _ in prepared
                        )
                        committed = (
                            (
                                after_group.mdib_version,
                                after_group.sequence_id,
                                after_group.instance_id,
                            )
                            == (
                                before_group.mdib_version + 1,
                                before_group.sequence_id,
                                before_group.instance_id,
                            )
                            and after_state_versions
                            == tuple(
                                (handle, state_version + 1)
                                for handle, state_version in before_state_versions
                            )
                        )
                        if not committed:
                            raise
                        version_group = after_group
                        transaction_error = exc

                touched = {entity.handle for entity, _ in prepared}
                alert_version, alert_error = self._apply_alert_updates(touched)
                if transaction_error is not None and alert_error is not None:
                    logger.error(
                        "action %s alert processing also failed after its metric report failure",
                        operation_handle,
                        exc_info=(type(alert_error), alert_error, alert_error.__traceback__),
                    )
                logger.info(
                    "action %s ran, changing %d metric(s)",
                    operation_handle,
                    len(touched),
                )
                return alert_version or version_group, transaction_error or alert_error
        finally:
            self._alert_evaluator_lock.release()

    def _action_target_for(self, spec: ActionSpec) -> str:
        """Return a declared target or the nearest common subtree of its known effects."""
        effect_paths = []
        for effect_handle in spec.effects:
            entity = self.mdib.entities.by_handle(effect_handle)
            if entity is None:
                # Keep malformed-action fixtures publishable so execution can demonstrate
                # all-or-nothing remote failure. Existing effects still determine the target.
                continue
            path = []
            while entity is not None:
                path.append(entity.handle)
                entity = self.mdib.entities.by_handle(entity.parent_handle)
            effect_paths.append(path)

        if not effect_paths or all(spec.target_handle in path for path in effect_paths):
            return spec.target_handle

        common = set(effect_paths[0]).intersection(*effect_paths[1:])
        if not common:
            msg = f"action {spec.label!r} effects have no common containment subtree"
            raise ValueError(msg)
        target = next(handle for handle in effect_paths[0] if handle in common)
        logger.warning(
            "action %r target %s does not contain all effects; using common subtree %s",
            spec.label,
            spec.target_handle,
            target,
        )
        return target

    def _coerce_value(self, handle: str, value: object) -> Decimal | str:
        spec = self._specs.get(handle)
        if spec is None:
            msg = f"no metric with handle {handle!r}"
            raise KeyError(msg)
        return coerce_metric_value(spec, value, handle)

    def _transaction_version(self, manager) -> mdibbase.MdibVersionGroup:
        """Copy the version assigned to a transaction before later updates can race it."""
        return mdibbase.MdibVersionGroup(
            manager.new_mdib_version,
            self.mdib.sequence_id,
            self.mdib.instance_id,
        )

    # -- internals -----------------------------------------------------------------

    def _dependent_action_handles(
        self,
        descriptor_handles: set[str],
        *,
        effect_metric: str | None = None,
    ) -> list[str]:
        """Find actions depending on a descriptor closure, dependents before targets."""
        waves = []
        found: set[str] = set()
        while True:
            wave = []
            for action_handle, spec in sorted(self._actions.items()):
                if action_handle in found:
                    continue
                entity = self.mdib.entities.by_handle(action_handle)
                target = getattr(getattr(entity, "descriptor", None), "OperationTarget", spec.target_handle)
                if target in descriptor_handles or effect_metric is not None and effect_metric in spec.effects:
                    wave.append(action_handle)
            if not wave:
                break
            waves.append(wave)
            found.update(wave)
            descriptor_handles.update(wave)
        return [action_handle for wave in reversed(waves) for action_handle in wave]

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

    def _channel_for(self, section: str) -> str:
        """The Channel a metric belongs in, creating its subsystem if this is the first.

        A device is not a flat list of readings. A microscope has motion, optics and
        illumination; a monitor has a channel per parameter group. BICEPS models that with
        a Vmd per subsystem and a Channel inside it, and a consumer browsing the containment
        tree sees the difference immediately - which is the whole reason the tree is part of
        the standard rather than an afterthought.

        Sections are created lazily rather than declared, so a preset only has to name one
        against each metric.
        """
        if not section:
            return constants.CHANNEL_HANDLE

        slug = slugify(section)
        vmd_handle = f"{constants.VMD_HANDLE_PREFIX}{slug}"
        channel_handle = f"{constants.CHANNEL_HANDLE_PREFIX}{slug}"
        vmd = self.mdib.entities.by_handle(vmd_handle)
        channel = self.mdib.entities.by_handle(channel_handle)
        if vmd is not None and (vmd.node_type != pm.VmdDescriptor or vmd.parent_handle != constants.MDS_HANDLE):
            msg = f"section {section!r} requires {vmd_handle!r} to be a VmdDescriptor under {constants.MDS_HANDLE!r}"
            raise ValueError(msg)
        if channel is not None and (
            channel.node_type != pm.ChannelDescriptor or channel.parent_handle != vmd_handle
        ):
            msg = f"section {section!r} requires {channel_handle!r} to be a ChannelDescriptor under {vmd_handle!r}"
            raise ValueError(msg)
        if channel is not None:
            if vmd is None:
                msg = f"section {section!r} requires parent {vmd_handle!r} for {channel_handle!r}"
                raise ValueError(msg)
            self._sections[section] = channel_handle
            return channel_handle

        coding = Coding(code=slug, system="private", label=section)
        # The Vmd has to be committed before the Channel can name it: new_entity looks the
        # parent up in mdib.descriptions, so an uncommitted one is not there yet.
        if vmd is None:
            vmd = self.mdib.entities.new_entity(pm.VmdDescriptor, vmd_handle, constants.MDS_HANDLE)
            vmd.descriptor.Type = _coded_value(coding, section)
            self._create_entities([vmd])

        channel = self.mdib.entities.new_entity(pm.ChannelDescriptor, channel_handle, vmd_handle)
        channel.descriptor.Type = _coded_value(coding, section)
        self._create_entities([channel])

        self._sections[section] = channel_handle
        logger.info("created section %r as %s / %s", section, vmd_handle, channel_handle)
        return channel_handle

    def sections(self) -> dict[str, str]:
        """Section name -> the Channel handle its metrics live in."""
        return dict(self._sections)

    def _section_channels(self) -> set[str]:
        """Derive managed section Channels from bookkeeping and live metric parents."""
        channels = set(self._sections.values())
        for metric_handle, spec in self._specs.items():
            if not spec.section:
                continue
            entity = self.mdib.entities.by_handle(metric_handle)
            if entity is not None:
                channels.add(entity.parent_handle)
        return channels

    def _section_descriptor_handles(self) -> set[str]:
        """Section descriptors that removing every managed metric will empty."""
        channels = self._section_channels()
        metrics = set(self._specs)
        removable_channels = {
            channel_handle
            for channel_handle in channels
            if all(
                entity.handle in metrics or entity.parent_handle != channel_handle
                for _, entity in self.mdib.entities.items()
            )
        }
        handles = set(removable_channels)
        for channel_handle in removable_channels:
            channel = self.mdib.entities.by_handle(channel_handle)
            if channel is None:
                continue
            vmd_handle = channel.parent_handle
            if all(
                entity.handle in removable_channels or entity.parent_handle != vmd_handle
                for _, entity in self.mdib.entities.items()
            ):
                handles.add(vmd_handle)
        return handles

    def _forget_section(self, channel_handle: str) -> None:
        self._sections = {
            section: existing_handle
            for section, existing_handle in self._sections.items()
            if existing_handle != channel_handle
        }

    def _empty_section_entities(
        self,
        channel_handle: str,
        *,
        excluding_metric: str | None = None,
    ) -> tuple[list, set[str]]:
        """Return an unowned Channel then its VMD, plus their handles."""
        channel = self.mdib.entities.by_handle(channel_handle)
        if channel is None or channel.node_type != pm.ChannelDescriptor:
            return [], set()
        if any(
            entity.handle != excluding_metric and entity.parent_handle == channel_handle
            for _, entity in self.mdib.entities.items()
        ):
            return [], set()
        entities = [channel]
        handles = {channel_handle}
        vmd_handle = channel.parent_handle
        vmd = self.mdib.entities.by_handle(vmd_handle)
        if vmd is not None and vmd.node_type == pm.VmdDescriptor:
            other_children = any(
                entity.handle != channel_handle and entity.parent_handle == vmd_handle
                for _, entity in self.mdib.entities.items()
            )
            if not other_children:
                entities.append(vmd)
                handles.add(vmd_handle)
        return entities, handles

    def _unique_handle(self, candidate: str) -> str:
        if self.mdib.entities.by_handle(candidate) is None:
            return candidate
        counter = 2
        while self.mdib.entities.by_handle(f"{candidate}.{counter}") is not None:
            counter += 1
        return f"{candidate}.{counter}"

    def _apply_spec_to_descriptor(self, descriptor, spec: MetricSpec) -> None:  # noqa: ANN001
        descriptor.Type = _coded_value(spec.effective_type(), spec.label)
        # A dimensionless metric still needs a Unit; it just gets no concept description,
        # rather than a made-up one such as "no unit".
        descriptor.Unit = _coded_value(spec.effective_unit(), spec.unit_label)
        descriptor.MetricCategory = (
            pm_types.MetricCategory.SETTING if spec.controllable else pm_types.MetricCategory.MEASUREMENT
        )
        descriptor.MetricAvailability = (
            pm_types.MetricAvailability.CONTINUOUS
            if spec.kind is MetricKind.WAVEFORM
            else pm_types.MetricAvailability.INTERMITTENT
        )
        if spec.kind is MetricKind.WAVEFORM:
            descriptor.DeterminationPeriod = float(spec.sample_period)
        elif spec.kind is MetricKind.DISTRIBUTION:
            descriptor.DeterminationPeriod = constants.WAVEFORM_BLOCK_SECONDS

        if spec.kind is MetricKind.NUMBER or spec.is_sample_array:
            descriptor.Resolution = fixed_point_decimal(spec.resolution, "resolution")
            if spec.has_range or spec.is_sample_array:
                # What the metric itself can produce. Distinct from the AllowedRange we put
                # on the set operation, which is what a remote caller may ask for.
                lower, upper = (
                    spec.generated_sample_range()
                    if spec.is_sample_array
                    else (spec.minimum, spec.maximum)
                )
                descriptor.TechnicalRange = [
                    pm_types.Range(
                        lower=fixed_point_decimal(lower, "minimum") if lower is not None else None,
                        upper=fixed_point_decimal(upper, "maximum") if upper is not None else None,
                        step_width=fixed_point_decimal(spec.resolution, "resolution"),
                    ),
                ]
        if spec.kind is MetricKind.CHOICE:
            descriptor.AllowedValue = [pm_types.AllowedValue(value=value) for value in spec.allowed_values]

        if spec.kind is MetricKind.WAVEFORM:
            # An xsd:duration in seconds. sdc11073 takes a float here and renders it.
            descriptor.SamplePeriod = float(spec.sample_period)

        if spec.kind is MetricKind.DISTRIBUTION:
            # What the samples are spread *over*, as opposed to Unit, which is what each
            # sample measures. Mandatory, and the field this kind fails on first.
            descriptor.DomainUnit = _coded_value(spec.effective_domain_unit(), spec.domain_unit_label)
            descriptor.DistributionRange = pm_types.Range(
                lower=fixed_point_decimal(spec.domain_minimum, "domain_minimum"),
                upper=fixed_point_decimal(spec.domain_maximum, "domain_maximum"),
                # Not Resolution. Resolution is how finely a *sample value* is measured;
                # StepWidth is how far apart two samples sit along the domain, so it is
                # fixed by how many we send. Passing Resolution here made the descriptor
                # claim 501 samples across a 0..50 Hz domain while five were being sent.
                step_width=domain_step(spec),
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

    def _set_demo_mode(self) -> None:
        """Keep the containing MDS coherent with generated Demo metric values."""
        current = self.mdib.entities.by_handle(constants.MDS_HANDLE).state.OperatingMode
        if current is not pm_types.MdsOperatingMode.DEMO:
            self._mds_mode_before_demo = current
        self._set_mds_operating_mode(pm_types.MdsOperatingMode.DEMO)

    def _restore_mds_mode_without_demo_metrics(self) -> None:
        """Restore the mode displaced by generation after its last source is gone."""
        if any(spec.is_sample_array for spec in self._specs.values()):
            return
        prior_mode = self._mds_mode_before_demo
        self._mds_mode_before_demo = None
        if prior_mode is None:
            return
        mds = self.mdib.entities.by_handle(constants.MDS_HANDLE)
        if mds is not None and mds.state.OperatingMode is pm_types.MdsOperatingMode.DEMO:
            self._set_mds_operating_mode(prior_mode)

    def _set_mds_operating_mode(self, mode: pm_types.MdsOperatingMode) -> None:
        entity = self.mdib.entities.by_handle(constants.MDS_HANDLE)
        if entity is None or entity.state.OperatingMode is mode:
            return
        entity.state.OperatingMode = mode
        with self.mdib.component_state_transaction() as mgr:
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
            pm_types.Range(
                lower=(fixed_point_decimal(spec.minimum, "minimum") if spec.minimum is not None else None),
                upper=(fixed_point_decimal(spec.maximum, "maximum") if spec.maximum is not None else None),
                step_width=(fixed_point_decimal(spec.resolution, "resolution") if spec.resolution is not None else None),
            ),
        ]
        with self.mdib.operational_state_transaction() as mgr:
            mgr.write_entity(entity)
