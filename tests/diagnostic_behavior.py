"""Focused offline checks for diagnostic contract and result reporting."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def main() -> int:
    print("Diagnostic behavior test (offline)")
    signature_mismatch_is_detected()
    callback_results_are_evidence_based()
    return REPORT.summary()


if __name__ == "__main__":
    raise SystemExit(main())
