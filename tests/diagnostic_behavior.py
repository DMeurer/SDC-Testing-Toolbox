"""Focused offline checks for diagnostic contract and result reporting."""

from __future__ import annotations

import sys
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sdc11073.xml_types import pm_qnames as pm
from sdc11073.xml_types import pm_types

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from diagnostics import minimal_provider
from diagnostics.check_api import CallShape, signature_incompatibility
from diagnostics.minimal_consumer import MetricUpdateObserver, result_lines
from script_support import Report

REPORT = Report()


def check(condition: bool, message: str) -> None:
    REPORT.require(condition, message)


def signature_mismatch_is_detected() -> None:
    def incompatible(required: object) -> None:
        del required

    detail = signature_incompatibility(incompatible, CallShape(1, ("renamed",)))
    check(detail is not None, "a rejected required call shape is incompatible")
    check("renamed" in detail, "signature mismatch identifies the rejected keyword")


def callback_results_are_evidence_based() -> None:
    connection, receipt = result_lines(0)
    check("connection held" in connection, "connection health has its own result")
    check("no callbacks received" in receipt, "zero callbacks produce a distinct no-update result")
    check("updates received" not in receipt, "elapsed observation does not claim updates")

    observer = MetricUpdateObserver()
    observer({})
    observer({})
    _, receipt = result_lines(observer.callback_count)
    check(observer.callback_count == 2, "actual metric callback invocations are counted")
    check("2 callback(s) received" in receipt, "positive receipt reports the callback count")


def check_diagnostic_wire_validity() -> None:
    """The independent diagnostic repairs actual SDK quality before publishing values."""
    mdib = minimal_provider.ProviderMdib.from_mdib_file(str(minimal_provider.MDIB_PATH))
    numeric = mdib.entities.by_node_type(pm.NumericMetricDescriptor)
    check(bool(numeric), "the diagnostic fixture contains real numeric metric states")
    state_class = type(numeric[0].state)
    original_mk_value = state_class.mk_metric_value

    def mk_invalid_value(state) -> None:
        original_mk_value(state)
        state.MetricValue.MetricQuality.Validity = pm_types.MeasurementValidity.INVALID

    offline_provider = SimpleNamespace(
        start_all=lambda **_: None, set_location=lambda _: None, stop_all=lambda: None,
    )
    with (
        patch.object(minimal_provider, "parse_args", return_value=SimpleNamespace(ip="127.0.0.1", adapter=None, seconds=0)),
        patch.object(minimal_provider, "WSDiscovery", return_value=nullcontext()),
        patch.object(minimal_provider, "SdcProvider", return_value=offline_provider),
        patch.object(minimal_provider.ProviderMdib, "from_mdib_file", return_value=mdib),
        patch.object(state_class, "mk_metric_value", mk_invalid_value),
        redirect_stdout(StringIO()),
    ):
        result = minimal_provider.main()
    check(result == 0, "the diagnostic completes its offline initialization")
    node = mdib.reconstruct_mdib_with_context_states()[0]
    for entity in numeric:
        validity = node.xpath(
            "//*[@DescriptorHandle=$handle]/*[local-name()='MetricValue']/*[local-name()='MetricQuality']/@Validity",
            handle=entity.handle,
        )
        check(validity == ["Vld"], f"diagnostic metric {entity.handle} serializes MetricQuality/@Validity=Vld")


def main() -> int:
    print("Diagnostic behavior test (offline)")
    signature_mismatch_is_detected()
    callback_results_are_evidence_based()
    check_diagnostic_wire_validity()
    return REPORT.summary()


if __name__ == "__main__":
    raise SystemExit(main())
