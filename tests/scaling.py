"""Work per edit must not grow with the size of the MDIB.

sdc11073 deep-copies every entity it hands out, so one whole-MDIB scan per metric turns a
large import quadratic. These tests count those copies instead of timing, so they are stable.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from perf_stress import big_device
from sdc11073.consumer.consumerimpl import SdcConsumer
from sdc11073.definitions_sdc import SdcV1Definitions
from sdc11073.mdib import ConsumerMdib
from sdc11073.mdib.mdibbase import EntityGetter

from sdctoolbox.consumer_service import RemoteDevice
from sdctoolbox.provider_service import ProviderService


def entity_copies(action) -> int:  # noqa: ANN001
    """Entities copied by this thread; the in-process provider and consumer copy on their own threads."""
    original = EntityGetter._mk_entity  # noqa: SLF001
    caller = threading.get_ident()
    count = 0

    def counting(self, descriptor):  # noqa: ANN001, ANN202
        nonlocal count
        count += threading.get_ident() == caller
        return original(self, descriptor)

    with patch.object(EntityGetter, "_mk_entity", counting):
        action()
    return count


class ScalingTests(unittest.TestCase):
    def per_metric(self, size: int, *, reset: bool) -> float:
        with ProviderService(ip="127.0.0.1", instance_name=f"scaling-{size}") as service:
            if reset:
                service.import_profile(big_device(size))
            return entity_copies(lambda: service.import_profile(big_device(size))) / size

    def assert_linear(self, *, reset: bool) -> None:
        small, large = self.per_metric(40, reset=reset), self.per_metric(160, reset=reset)
        # Quadratic work would make the large import copy about four times as much per metric.
        self.assertLess(large, small * 1.5, f"entity copies per metric: {small:.1f} at 40, {large:.1f} at 160")

    def test_import_copies_a_constant_number_of_entities_per_metric(self) -> None:
        self.assert_linear(reset=False)

    def test_reset_copies_a_constant_number_of_entities_per_metric(self) -> None:
        self.assert_linear(reset=True)

    def test_consumer_reads_do_not_copy_the_remote_mdib(self) -> None:
        # The GUI polls these; each used to deep-copy every entity, even for one metric.
        with ProviderService(ip="127.0.0.1", instance_name="scaling-consumer") as service:
            service.import_profile(big_device(200))
            handle = next(iter(service.list_metrics()))
            consumer = SdcConsumer(service._provider.get_xaddrs()[0], SdcV1Definitions, None)  # noqa: SLF001
            consumer.start_all()
            try:
                mdib = ConsumerMdib(consumer)
                mdib.init_mdib()
                remote = RemoteDevice(consumer, mdib, service.epr.urn)
                self.assertEqual(remote.metrics((handle,))[handle].value, service.get_value(handle))
                self.assertEqual(len(remote.alerts()), len(service.list_alerts()))
                for name, read in (("one metric", lambda: remote.metrics((handle,))), ("metrics", remote.metrics),
                                   ("alerts", remote.alerts), ("actions", remote.actions)):
                    self.assertEqual(entity_copies(read), 0, name)
            finally:
                consumer.stop_all()


if __name__ == "__main__":
    unittest.main()
