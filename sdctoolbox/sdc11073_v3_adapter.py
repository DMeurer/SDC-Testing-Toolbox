"""Private sdc11073 3.0.0 mechanics used by the managed provider boundary.

Nothing outside :mod:`provider_service` should depend on these implementation details.
"""

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import version

from sdc11073.location import SdcLocation
from sdc11073.mdib import mdibbase
from sdc11073.mdib.transactions import mk_transaction
from sdc11073.xml_types import pm_types


@dataclass(frozen=True)
class Sdc11073V3Snapshot:
    descriptors: list
    states: list
    registered_operations: dict
    provider_location: SdcLocation


class Sdc11073V3Adapter:
    """Contain the private APIs needed for report-safe live graph compensation."""

    SUPPORTED_VERSION = "3.0.0"

    def __init__(self, mdib) -> None:  # noqa: ANN001 - pinned third-party type
        installed = version("sdc11073")
        if installed != self.SUPPORTED_VERSION:
            msg = f"profile import requires sdc11073 {self.SUPPORTED_VERSION}, found {installed}"
            raise RuntimeError(msg)
        self._mdib = mdib
        self._provider = None
        self._sco = None
        self._descriptor_types = {
            descriptor.Handle: descriptor.NODETYPE
            for descriptor in mdib.descriptions.objects
        }
        self._install_transaction_adapter()

    def bind(self, provider, sco) -> None:  # noqa: ANN001 - pinned third-party types
        self._provider = provider
        self._sco = sco

    def _install_transaction_adapter(self) -> None:
        """Correct aggregate versions and normalize v3 description report parts."""
        mdib = self._mdib

        def transaction_factory(device_mdib, transaction_type, logger):  # noqa: ANN001, ANN202
            transaction = mk_transaction(device_mdib, transaction_type, logger)
            process = transaction.process_transaction

            def process_transaction(set_determination_time):  # noqa: ANN001, ANN202
                for item in transaction.descriptor_updates.values():
                    descriptor = item.new
                    if descriptor is not None:
                        self.validate_descriptor_type(descriptor.Handle, descriptor.NODETYPE)
                old_mdib_version = device_mdib.mdib_version
                try:
                    result = process(set_determination_time)
                finally:
                    committed = device_mdib.mdib_version > old_mdib_version
                    if committed:
                        if transaction.descriptor_updates:
                            device_mdib.mddescription_version += 1
                        if any(
                            (
                                transaction.descriptor_updates,
                                transaction.metric_state_updates,
                                transaction.alert_state_updates,
                                transaction.component_state_updates,
                                transaction.context_state_updates,
                                transaction.operational_state_updates,
                                transaction.rt_sample_state_updates,
                            ),
                        ):
                            device_mdib.mdstate_version += 1
                        for item in transaction.descriptor_updates.values():
                            descriptor = item.new
                            if descriptor is not None:
                                self._descriptor_types[descriptor.Handle] = descriptor.NODETYPE

                # v3 emits one report part per descriptor. Keep create parts in containment
                # order and omit ParentDescriptor from delete parts as required by BICEPS.
                result.descr_created.sort(key=self._descriptor_depth)
                for descriptor in result.descr_deleted:
                    descriptor.parent_handle = None
                return result

            transaction.process_transaction = process_transaction
            return transaction

        # ProviderMdib exposes no public transaction-factory setter in 3.0.0.
        mdib._transaction_factory = transaction_factory  # noqa: SLF001

    def _descriptor_depth(self, descriptor) -> int:  # noqa: ANN001
        depth = 0
        parent = descriptor.parent_handle
        seen = set()
        while parent is not None and parent not in seen:
            seen.add(parent)
            depth += 1
            entity = self._mdib.entities.by_handle(parent)
            parent = entity.parent_handle if entity is not None else None
        return depth

    def validate_descriptor_type(self, handle: str, node_type) -> None:  # noqa: ANN001
        """Reject reuse of a descriptor handle for another XML Schema datatype."""
        previous = self._descriptor_types.get(handle)
        if previous is not None and previous != node_type:
            msg = f"descriptor handle {handle!r} was previously {previous}, not {node_type}"
            raise ValueError(msg)

    def snapshot(self) -> Sdc11073V3Snapshot:
        self._require_bound()
        return Sdc11073V3Snapshot(
            descriptors=deepcopy(list(self._mdib.descriptions.objects)),
            states=deepcopy(list(self._mdib.states.objects)),
            registered_operations=dict(self._sco._registered_operations),  # noqa: SLF001
            provider_location=deepcopy(self._provider._location),  # noqa: SLF001
        )

    def restore_registered_operations(self, operations: dict) -> None:
        self._require_bound()
        self._sco._registered_operations = dict(operations)  # noqa: SLF001
        for operation in operations.values():
            operation._operation_entity = self._mdib.entities.by_handle(operation.handle)  # noqa: SLF001

    def restore_location(self, location: SdcLocation) -> None:
        """Publish a new associated location state and refresh discovery scopes."""
        self._require_bound()
        if self._provider._location == location:  # noqa: SLF001
            return
        self._provider.set_location(deepcopy(location))

    def recreate_entities(self, descriptors: list, states: list) -> None:
        """Reinsert entities through v3 transactions, preserving historical maxima."""
        states_by_handle = {state.DescriptorHandle: state for state in states}
        entities = []
        for saved in descriptors:
            descriptor = deepcopy(saved)
            self.validate_descriptor_type(descriptor.Handle, descriptor.NODETYPE)
            state = deepcopy(states_by_handle[descriptor.Handle])
            entities.append(mdibbase.Entity(self._mdib, descriptor, state))
        if entities:
            with self._mdib.descriptor_transaction() as manager:
                manager.write_entities(entities)

    def compensate_context(self, descriptor_handle: str, saved_associated_state) -> None:  # noqa: ANN001
        """Disassociate current context and create a fresh valid prior association."""
        entity = self._mdib.entities.by_handle(descriptor_handle)
        if entity is None:
            msg = f"context descriptor {descriptor_handle!r} is missing during compensation"
            raise RuntimeError(msg)
        with self._mdib.context_state_transaction() as manager:
            manager.disassociate_all(descriptor_handle)
            if saved_associated_state is None:
                return
            restored = deepcopy(saved_associated_state)
            restored.Handle = uuid.uuid4().hex
            restored.StateVersion = 0
            restored.DescriptorVersion = entity.descriptor.DescriptorVersion
            restored.descriptor_container = entity.descriptor
            restored.ContextAssociation = pm_types.ContextAssociation.ASSOCIATED
            restored.BindingMdibVersion = manager.new_mdib_version
            restored.BindingStartTime = time.time()
            restored.UnbindingMdibVersion = None
            restored.BindingEndTime = None
            manager.add_state(restored)

    def _require_bound(self) -> None:
        if self._provider is None or self._sco is None:
            raise RuntimeError("sdc11073 adapter is not bound to a running provider")
