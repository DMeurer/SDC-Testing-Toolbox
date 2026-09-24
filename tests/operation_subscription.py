"""Network acceptance: operations need an active OperationInvokedReport subscription.

Some providers refuse set and activate requests from a consumer that is not subscribed to
OperationInvokedReport. sdc11073 only logs a rejected subscription, so the toolbox checks
it after connecting, retries with a dedicated subscription, and refuses to invoke without it.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from script_support import Report  # noqa: E402
from sdc11073.consumer.consumerimpl import SdcConsumer  # noqa: E402
from sdc11073.xml_types import msg_types  # noqa: E402
from sdc11073.xml_types.actions import Actions  # noqa: E402

from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.model import MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

OPERATION_INVOKED = Actions.OperationInvokedReport.value


def start_all_without_operation_reports(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
    """Stand-in for a provider that rejected the combined subscription containing the report."""
    kwargs["not_subscribed_actions"] = [OPERATION_INVOKED]
    return ORIGINAL_START_ALL(self, *args, **kwargs)


ORIGINAL_START_ALL = SdcConsumer.start_all


@contextmanager
def capture_warnings() -> Iterator[list[str]]:
    messages: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = Collect(level=logging.WARNING)
    logger = logging.getLogger("sdctoolbox.consumer")
    logger.addHandler(handler)
    try:
        yield messages
    finally:
        logger.removeHandler(handler)


def main() -> int:
    report = Report()
    print("OperationInvokedReport subscription")
    provider = ProviderService(instance_name="operation-subscription-provider")
    consumer = ConsumerService()
    remote = None
    try:
        provider.start()
        handle = provider.add_metric(
            MetricSpec(label="Setting", kind=MetricKind.NUMBER, controllable=True, initial_value=Decimal("1")),
        )
        consumer.start()
        device = next(
            (candidate for candidate in consumer.scan(timeout=10.0, expected=1) if candidate.epr == provider.epr.urn),
            None,
        )
        report.check(device is not None, "provider is discoverable")
        if device is None:
            return report.summary()

        with capture_warnings() as warnings:
            healthy = consumer.connect(device)
        report.check(
            healthy.operation_reports_subscribed() and not warnings,
            "a normal connection is recognized as subscribed without a retry",
        )
        healthy.close()

        with capture_warnings() as warnings, patch.object(SdcConsumer, "start_all", start_all_without_operation_reports):
            remote = consumer.connect(device)
        report.check(
            any("no active OperationInvokedReport subscription" in message for message in warnings),
            "the missing subscription is detected after connecting",
        )
        report.check(
            remote.operation_reports_subscribed()
            and remote._consumer.subscription_status.get(OPERATION_INVOKED) is True,  # noqa: SLF001
            "a missing OperationInvokedReport subscription is restored with a dedicated one",
        )
        report.check(
            remote.set_value(handle, Decimal("2")) in (msg_types.InvocationState.FINISHED, msg_types.InvocationState.FINISHED_MOD),
            "set operations work over the dedicated subscription",
        )

        # Simulate the provider ending that subscription (SubscriptionEnd or a failed renew).
        sdc_consumer = remote._consumer  # noqa: SLF001
        sdc_consumer.subscription_status = {
            subscription_filter: active and OPERATION_INVOKED not in subscription_filter.split()
            for subscription_filter, active in sdc_consumer.subscription_status.items()
        }
        report.check(not remote.operation_reports_subscribed(), "an ended subscription is noticed")
        try:
            remote.set_value(handle, Decimal("3"))
        except RuntimeError as exc:
            refusal = str(exc)
        else:
            refusal = ""
        report.check(
            "not subscribed to OperationInvokedReport" in refusal,
            "a set without the subscription fails locally with the reason",
        )
    finally:
        if remote is not None:
            remote.close()
        consumer.stop()
        provider.stop()
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
