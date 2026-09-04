"""Verify GitHub Actions dependencies remain pinned and maintained."""

from __future__ import annotations

import re
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from script_support import Report

USES_PATTERN = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<target>\S+)(?:\s+#\s*(?P<comment>.+))?$"
)
USES_KEY_PATTERN = re.compile(r"^\s*(?:-\s*)?uses\s*:")
PIN_PATTERN = re.compile(r"^[^@\s]+@[0-9a-fA-F]{40}$")
VERSION_PATTERN = re.compile(r"^v\d+(?:\.\d+){1,2}$")


def main() -> int:
    print("Workflow security")
    report = Report()
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    report.require(bool(workflows), "at least one workflow is present")

    external_references = 0
    for workflow in workflows:
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if USES_KEY_PATTERN.match(line) is None:
                continue
            match = USES_PATTERN.match(line)
            location = f"{workflow.relative_to(ROOT)}:{line_number}"
            report.require(match is not None, f"{location} has a parseable uses reference", line)
            if match.group("target").startswith("./"):
                continue
            external_references += 1
            target = match.group("target")
            report.check(
                PIN_PATTERN.fullmatch(target) is not None,
                f"{location} uses a full commit SHA",
                target,
            )
            comment = (match.group("comment") or "").strip()
            report.check(
                VERSION_PATTERN.fullmatch(comment) is not None,
                f"{location} records the reviewed release",
                comment or "missing comment",
            )
    report.check(external_references > 0, "external action references were inspected")

    dependabot_path = ROOT / ".github" / "dependabot.yml"
    report.require(dependabot_path.is_file(), "Dependabot configuration is present")
    dependabot = dependabot_path.read_text(encoding="utf-8")
    for setting in (
        "package-ecosystem: github-actions",
        'directory: "/"',
        "target-branch: develop",
        "interval: monthly",
    ):
        report.check(setting in dependabot, f"Dependabot declares {setting}")

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
