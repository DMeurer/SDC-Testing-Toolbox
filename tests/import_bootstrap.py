"""Verify direct-run test scripts ignore an unrelated installed tests package."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTS))

from script_support import Report


def check_script(
    report: Report,
    script: Path,
    *,
    optimized: bool,
    environment: dict[str, str],
    marker: Path,
) -> None:
    marker.unlink(missing_ok=True)
    command = [sys.executable]
    if optimized:
        command.append("-O")
    command.append(str(script))
    result = subprocess.run(
        command,
        cwd=marker.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    passed = result.returncode == 0 and marker.is_file()
    if not passed:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
    mode = "optimized" if optimized else "normal"
    report.check(
        passed,
        f"{script.name} runs by absolute path in {mode} mode despite tests package shadowing",
        f"exit {result.returncode}; shadow loaded: {marker.is_file()}",
    )


def main() -> int:
    print("Direct test import bootstrap")
    report = Report()
    with TemporaryDirectory(prefix="sdc-import-shadow-") as raw_temporary:
        temporary = Path(raw_temporary)
        shadow = temporary / "shadow"
        fake_tests = shadow / "tests"
        fake_tests.mkdir(parents=True)
        marker = temporary / "shadow-package-loaded"
        (fake_tests / "__init__.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('loaded', encoding='utf-8')\n",
            encoding="utf-8",
        )
        for module in ("script_support", "acceptance_provider"):
            (fake_tests / f"{module}.py").write_text(
                f"raise RuntimeError('imported shadow tests.{module}')\n",
                encoding="utf-8",
            )
        (shadow / "sitecustomize.py").write_text("import tests\n", encoding="utf-8")

        environment = os.environ.copy()
        python_path = [str(shadow)]
        if environment.get("PYTHONPATH"):
            python_path.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_path)
        environment.setdefault("QT_QPA_PLATFORM", "offscreen")

        for script_name in ("licensing.py", "application_defaults.py"):
            for optimized in (False, True):
                check_script(
                    report,
                    TESTS / script_name,
                    optimized=optimized,
                    environment=environment,
                    marker=marker,
                )
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
