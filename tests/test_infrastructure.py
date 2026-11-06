"""Focused checks for shared test helpers, suite metadata, and subprocess runner."""

from __future__ import annotations

import contextlib
import ctypes
import gc
import io
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))

import run_suites  # noqa: E402
import process_control  # noqa: E402
from script_support import CallbackRecorder, ProcessOutput, Report, stop_process, wait_until  # noqa: E402


def polling_checks(report: Report) -> None:
    calls: list[str] = []

    def boundary_predicate() -> bool:
        calls.append("predicate")
        return len(calls) == 1

    report.check(
        wait_until(boundary_predicate, timeout=0, check_boundary=True),
        "boundary checking evaluates a predicate when the timeout is already reached",
    )
    calls.clear()
    report.check(
        not wait_until(boundary_predicate, timeout=0, check_boundary=False)
        and calls == [],
        "boundary checking can be explicitly disabled",
    )

    pump_calls: list[str] = []
    report.check(
        wait_until(
            lambda: len(pump_calls) == 1,
            timeout=0.1,
            pump=lambda: pump_calls.append("pump"),
        ),
        "an event pump runs before each predicate check",
    )

    sentinel = RuntimeError("predicate failed")
    try:
        wait_until(lambda: (_ for _ in ()).throw(sentinel), timeout=0.1)
    except RuntimeError as exc:
        propagated = exc is sentinel
    else:
        propagated = False
    report.check(propagated, "predicate exceptions propagate unchanged")


def recorder_checks(report: Report) -> None:
    source = {"metric": [1]}
    recorder = CallbackRecorder(
        lambda value: {key: tuple(items) for key, items in dict(value).items()},
    )
    cursor = recorder.cursor()
    recorder(source)
    source["metric"].append(2)
    event = recorder.wait_for(
        lambda payload: payload["metric"] == (1,),
        after=cursor,
        timeout=0,
    )
    report.check(
        event is not None and event.sequence == 1 and event.payload == {"metric": (1,)},
        "recorders retain copied payloads with monotonic sequence numbers",
    )
    report.check(
        recorder.wait_for(
            lambda _payload: True,
            after=event.sequence,
            timeout=0.02,
        )
        is None,
        "a baseline cursor excludes earlier callback delivery",
    )

    callback_returned = threading.Event()

    def invoke() -> None:
        recorder({"other": [3]})
        callback_returned.set()

    thread = threading.Thread(target=invoke)
    thread.start()
    thread.join(timeout=0.2)
    report.check(
        callback_returned.is_set(),
        "recorder callbacks copy, signal, and return promptly",
    )

    for report_first in (True, False):
        ordered = CallbackRecorder(lambda value: str(value))
        baseline = ordered.cursor()
        invocation_complete = threading.Event()

        if report_first:
            ordered("delivered")
            invocation_complete.set()
        else:
            invocation_complete.set()
            ordered("delivered")
        delivered = ordered.wait_for(
            lambda payload: payload == "delivered",
            after=baseline,
            timeout=0,
        )
        report.check(
            invocation_complete.is_set() and delivered is not None,
            "invocation completion and report delivery remain independent when the "
            f"report arrives {'first' if report_first else 'last'}",
        )


def manifest_checks(report: Report) -> None:
    suites = run_suites.load_manifest(TESTS / "suites.json")
    by_id = {suite["id"]: suite for suite in suites}
    report.check(
        len(by_id) == len(suites)
        and all(run_suites.REQUIRED_FIELDS <= suite.keys() for suite in suites),
        "the suite manifest has stable unique IDs and all isolation metadata",
    )
    report.check(
        all((ROOT / suite["argv"][0]).is_file() for suite in suites),
        "every manifest entry launches an existing direct-run script",
    )
    report.check(
        by_id["network-acceptance"]["resources"] == ["ws-discovery", "acceptance-epr"]
        and by_id["acceptance-provider"]["role"] == "fixture"
        and by_id["gui-smoke"]["role"] == "manual",
        "network locks, fixtures, and manual suites remain explicitly classified",
    )

    workflow = (ROOT / ".github" / "workflows" / "build-app.yml").read_text(
        encoding="utf-8",
    )
    network_jobs: dict[str, list[str]] = {}
    deterministic_platforms = []
    # shortcut: reads two-space jobs and an inline suite matrix; use a YAML parser if that layout expands.
    for match in re.finditer(r"^  ([\w-]+):\n(.*?)(?=^  [\w-]+:|\Z)", workflow, re.M | re.S):
        job_id, job = match.groups()
        matrix = re.search(r"^\s+suite: (\[[^\n]+\])", job, re.M)
        variants = json.loads(matrix[1]) if matrix else [None]
        for variant in variants:
            expanded = job.replace("${{ matrix.suite }}", variant or "")
            selected = []
            platform = "windows" if "runs-on: windows-" in job else "linux"

            def record(suite: dict[str, object]) -> int:
                selected.append(suite["id"])
                return 0

            for command in re.findall(r"(?:^|\n)\s*(?:run:\s*)?(python tests/[\w_]+\.py[^\n]*)", expanded):
                argv = shlex.split(command.split("2>&1", 1)[0].split("|", 1)[0])
                if argv[1] == "tests/run_suites.py":
                    with patch.object(run_suites, "run_suite", side_effect=record), patch.object(
                        run_suites, "os", SimpleNamespace(name="nt" if platform == "windows" else "posix"),
                    ), contextlib.redirect_stdout(io.StringIO()):
                        code = run_suites.main(argv[2:])
                    if "--role" in argv:
                        deterministic_platforms.append(platform)
                        expected = {
                            suite["id"] for suite in suites
                            if suite["role"] in {"deterministic", "policy"}
                            and platform in suite["platforms"]
                        }
                        report.check(
                            code == 0 and set(selected) == expected,
                            f"CI {job_id} selects every platform-compatible deterministic/policy suite",
                        )
                else:
                    selected.extend(suite["id"] for suite in suites if suite["argv"] == argv[1:])
            network = [suite_id for suite_id in selected if by_id[suite_id]["role"] == "network"]
            if network:
                label = f"{job_id}/{variant}" if variant else job_id
                network_jobs[label] = network
                report.check(
                    len(network) == 1 and platform == "linux"
                    and "github.event_name != 'schedule'" not in job,
                    f"CI {label} isolates one Linux network suite, including scheduled runs",
                )
                suite_id = network[0]
                report.check(
                    f"tee {suite_id}.log" in expanded and "if: failure()" in expanded
                    and f"path: {suite_id}.log" in expanded,
                    f"CI {label} retains its own failure output",
                )
                if suite_id == "connection-loss":
                    report.check(
                        "QT_QPA_PLATFORM: offscreen" in job and "apt-get install -y libegl1" in job,
                        "connection-loss CI installs its Qt runtime and uses offscreen rendering",
                    )
    report.check(
        sorted(deterministic_platforms) == ["linux", "windows"],
        "CI selects manifest-driven deterministic/policy suites on both platforms",
    )
    expected_network = {"network-acceptance", "tls-acceptance", "passive-discovery", "operation-subscription", "connection-loss"}
    report.check(
        {suite_id for selected in network_jobs.values() for suite_id in selected} == expected_network,
        "CI selects all five intended network suites in isolated jobs", repr(network_jobs),
    )


@contextlib.contextmanager
def child_guard(pid: int):
    """Independent cleanup keeps the baseline regression runs from leaking children."""
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100001, False, pid)
        alive = lambda: bool(handle) and kernel.WaitForSingleObject(handle, 0) == 258
        try:
            yield alive
        finally:
            if alive():
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 2000)
            if handle:
                kernel.CloseHandle(handle)
    else:
        def alive() -> bool:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0]
                return state != "Z"
            except FileNotFoundError:
                return False
        try:
            yield alive
        finally:
            if alive():
                os.kill(pid, signal.SIGKILL)


def orphan_driver(root: Path, mode: str) -> int:
    """Prepare real processes before handing them to the unchanged timed runner."""
    suite = {"id": mode, "argv": [str(root / "parent.py")], "cwd": ".", "timeout_seconds": 0.8, "environment": {}}
    timer = None

    def prepare(process):
        nonlocal timer
        pid = None

        def child_ready():
            nonlocal pid
            try:
                pid = int((root / "child.pid").read_text())
            except (FileNotFoundError, PermissionError, ValueError):
                return False
            return True

        deadline = time.monotonic() + 5
        if not wait_until(child_ready, timeout=5):
            raise TimeoutError("fixture child did not finish startup")
        if mode != "interrupt" and process.wait(timeout=max(0, deadline - time.monotonic())) != 7:
            raise RuntimeError("fixture leader did not exit with status 7")
        print(f"FIXTURE READY child_pid={pid} leader_exit={process.poll()}", flush=True)
        ready = root / "runner-ready.json"
        ready.with_suffix(".tmp").write_text(json.dumps({"pid": pid, "leader_exit": process.poll()}))
        ready.with_suffix(".tmp").replace(ready)
        if not wait_until(
            lambda: (root / "measure").exists(), timeout=max(0, deadline - time.monotonic()),
        ):
            raise TimeoutError("fixture observer did not acknowledge readiness")
        if mode == "interrupt":
            import _thread

            timer = threading.Timer(.3, _thread.interrupt_main)
            timer.start()

    try:
        if mode == "output-helper":
            process = subprocess.Popen(
                [sys.executable, str(root / "parent.py")], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
            )
            output = None
            try:
                prepare(process)
                output = ProcessOutput(process)
                stop_process(process, output)
                print("READER_STOPPED=" + str(not output.is_alive), flush=True)
                return 0
            finally:
                if output is not None:
                    output.close()
                else:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=2)
                    process.stdout.close()

        real_start = run_suites.start_process

        def ready_start(*args, **kwargs):
            process = real_start(*args, **kwargs)
            try:
                prepare(process)
                return process
            except BaseException:
                try:
                    process_control.terminate_process_tree(process)
                finally:
                    process.stdout.close()
                raise

        with patch.object(run_suites, "start_process", side_effect=ready_start):
            return run_suites.run_suite(suite, root=root)
    except KeyboardInterrupt:
        return 130
    finally:
        if timer is not None:
            timer.cancel()
            timer.join(timeout=1)


def orphan_process_checks(report: Report) -> None:
    for mode in ("inherited-pipe", "closed-pipe", "interrupt", "output-helper"):
        with TemporaryDirectory(prefix="sdctoolbox-orphan-") as temporary:
            root = Path(temporary)
            pid_path = root / "child.pid"
            stop_path = root / "stop"
            child_code = (
                "import os, pathlib, signal, time\n"
                "time.sleep(1.2)\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"pid_path = pathlib.Path({str(pid_path)!r})\n"
                "pid_path.with_suffix('.tmp').write_text(str(os.getpid()))\n"
                "pid_path.with_suffix('.tmp').replace(pid_path)\n"
                "print('ORPHAN OUTPUT', flush=True)\n"
                "deadline = time.monotonic() + 20\n"
                f"while time.monotonic() < deadline and not pathlib.Path({str(stop_path)!r}).exists(): time.sleep(.01)\n"
            )
            parent = root / "parent.py"
            parent.write_text(
                "import pathlib, subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-u', '-c', {child_code!r}]"
                + (", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if mode == "closed-pipe" else "")
                + ")\n"
                + (f"while not pathlib.Path({str(pid_path)!r}).exists(): time.sleep(.01)\n" if mode in {"closed-pipe", "interrupt"} else "")
                + ("time.sleep(20)\n" if mode == "interrupt" else "sys.exit(7)\n"),
                encoding="utf-8",
            )
            driver_code = (
                f"import sys; sys.path.insert(0, {str(TESTS)!r})\n"
                "from pathlib import Path\nfrom test_infrastructure import orphan_driver\n"
                f"sys.exit(orphan_driver(Path({str(root)!r}), {mode!r}))\n"
            )
            with (root / "driver.log").open("w", encoding="utf-8") as log:
                driver = process_control.start_process(
                    [sys.executable, "-B", "-c", driver_code], stdout=log, stderr=subprocess.STDOUT,
                )
                pid = None
                ready = None

                def child_started() -> bool:
                    nonlocal pid, ready
                    try:
                        ready = json.loads((root / "runner-ready.json").read_text())
                        pid = int(ready["pid"])
                    except (FileNotFoundError, PermissionError, ValueError):
                        return False
                    return True

                try:
                    wait_until(lambda: child_started() or driver.poll() is not None, timeout=8)
                    started = child_started()
                    diagnostics = (
                        f"driver_exit={driver.poll()}, driver_log="
                        + (root / "driver.log").read_text(encoding="utf-8", errors="replace")
                    )
                    valid_ready = started and (
                        ready["leader_exit"] == 7 if mode != "interrupt" else ready["leader_exit"] is None
                    )
                    report.require(
                        valid_ready,
                        f"{mode} fixture starts its long-lived child before the measured phase",
                        diagnostics if not valid_ready else "",
                    )
                    with child_guard(pid) as alive:
                        (root / "measure").touch()
                        began = time.monotonic()
                        try:
                            code = driver.wait(timeout=4)
                        except subprocess.TimeoutExpired:
                            code = None
                        elapsed = time.monotonic() - began
                        if mode != "output-helper":
                            report.check(not alive(), f"{mode} cleanup stops descendants independently of leader exit")
                        expected = {"inherited-pipe": 124, "closed-pipe": 7, "interrupt": 130, "output-helper": 0}[mode]
                        report.check(code == expected and elapsed < 4, f"{mode} completes within its deadline and preserves exit status", f"code={code}, elapsed={elapsed:.3f}s")
                finally:
                    stop_path.touch()
                    process_control.terminate_process_tree(driver)
            text = (root / "driver.log").read_text(encoding="utf-8")
            if mode == "inherited-pipe":
                report.check("ORPHAN OUTPUT" in text and "RUNNER TIMEOUT: inherited-pipe" in text, "timeout retains inherited child output and the timeout marker", text)
            elif mode == "output-helper":
                report.check("ORPHAN OUTPUT" in text and "READER_STOPPED=True" in text, "output-helper bounds final draining and stops its reader", text)


def readiness_failure_checks(report: Report) -> None:
    original_start = process_control.start_process

    def failed_driver(command, **kwargs):
        return original_start(
            [*command[:3], "import sys; print('READINESS-PROBE', flush=True); sys.exit(9)"],
            **kwargs,
        )

    diagnostics = io.StringIO()
    with patch.object(process_control, "start_process", side_effect=failed_driver), contextlib.redirect_stdout(diagnostics):
        try:
            orphan_process_checks(Report())
        except AssertionError:
            pass
    text = diagnostics.getvalue()
    retained = "driver_exit=9" in text and "READINESS-PROBE" in text
    report.check(
        retained,
        "fixture readiness failure retains driver exit and log before temporary cleanup",
        text if not retained else "",
    )


def windows_resource_checks(report: Report) -> None:
    if os.name != "nt":
        return
    kernel = process_control._kernel
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    kernel.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]

    def handle_count() -> int:
        count = ctypes.c_ulong()
        if not kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return count.value

    with TemporaryDirectory(prefix="sdctoolbox-native-") as temporary:
        root = Path(temporary)
        marker = root / "ran-before-ownership"
        script = root / "probe.py"
        script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
        suite = {"id": "native-failure", "argv": [str(script)], "cwd": ".", "timeout_seconds": 2, "environment": {}}
        with contextlib.redirect_stdout(io.StringIO()):
            run_suites.run_suite(suite, root=root)
        marker.unlink()
        # Earlier fixture/mock cycles must not disappear during native measurement.
        gc.collect()
        before = handle_count()
        original_popen = subprocess.Popen
        original_execute = original_popen._execute_child
        original_create = subprocess._winapi.CreateProcess
        for boundary in ("constructor-return", "native-create"):
            created = []
            native = []
            with contextlib.ExitStack() as cleanup:
                watched = []

                def create(*args, **kwargs):
                    result = original_create(*args, **kwargs)
                    native.append(result)
                    watched.append(cleanup.enter_context(child_guard(result[2])))
                    if boundary == "native-create":
                        signal.raise_signal(signal.SIGINT)
                    return result

                def execute(self, *args, **kwargs):
                    original_execute(self, *args, **kwargs)
                    created.append(self)
                    if boundary == "constructor-return":
                        raise KeyboardInterrupt("post-native constructor interruption")

                old_handler = signal.getsignal(signal.SIGINT)
                interrupted = False
                try:
                    with patch.object(original_popen, "_execute_child", execute), patch.object(
                        subprocess._winapi, "CreateProcess", side_effect=create,
                    ):
                        try:
                            process_control.start_process(
                                [sys.executable, "-B", str(script)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            )
                        except KeyboardInterrupt:
                            interrupted = True
                    remaining = [alive() for alive in watched]
                    report.check(
                        interrupted and len(native) == 1 and not any(remaining)
                        and not marker.exists() and signal.getsignal(signal.SIGINT) is old_handler
                        and all(p.returncode is not None and p.stdout.closed for p in created),
                        f"post-creation {boundary} interruption reaps the real suspended child and restores SIGINT",
                        f"interrupted={interrupted}, alive={remaining}",
                    )
                finally:
                    for alive in watched:
                        if alive():
                            kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                            kernel.TerminateProcess(native[0][0], 1)
                            kernel.WaitForSingleObject(native[0][0], 2000)
                    for process in created:
                        process.wait(timeout=2)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None:
                                stream.close()
                        process._handle.Close()
                    if not created:
                        for process_handle, thread_handle, _pid, _tid in native:
                            kernel.CloseHandle(process_handle)
                            kernel.CloseHandle(thread_handle)
            created.clear()
            process = None
        for boundary, failure in (
            ("CreateJobObjectW", 0),
            ("SetInformationJobObject", 0),
            ("SetInformationJobObject", KeyboardInterrupt),
            ("AssignProcessToJobObject", 0),
            ("CreateToolhelp32Snapshot", ctypes.c_void_p(-1).value),
            ("OpenThread", 0),
            ("ResumeThread", 0xFFFFFFFF),
            ("ResumeThread", KeyboardInterrupt),
        ):
            with contextlib.ExitStack() as cleanup:
                watched = []

                def spawn(self, *args, **kwargs):
                    original_execute(self, *args, **kwargs)
                    watched.append(cleanup.enter_context(child_guard(self.pid)))

                def fail(*_args):
                    ctypes.set_last_error(5)
                    if failure is KeyboardInterrupt:
                        raise KeyboardInterrupt
                    return failure

                with patch.object(kernel, boundary, side_effect=fail), patch.object(
                    original_popen, "_execute_child", spawn,
                ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    try:
                        code = run_suites.run_suite(suite, root=root)
                    except KeyboardInterrupt:
                        code = 130
                expected = 130 if failure is KeyboardInterrupt else 1
                remaining = [alive() for alive in watched]
                report.check(
                    code == expected and not marker.exists() and not any(remaining),
                    f"native {boundary} {'interruption' if expected == 130 else 'failure'} closes ownership and never runs suite code",
                    f"code={code}, ran={marker.exists()}, alive={remaining}",
                )
        for _ in range(5):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                run_suites.run_suite(suite, root=root)
                run_suites.run_suite(suite, root=root / "missing")
        after = handle_count()
        report.check(after == before, "native success, spawn failures, and interrupted setup release all process/job/thread/pipe handles", f"before={before}, after={after}")


def runner_checks(report: Report) -> None:
    with TemporaryDirectory(prefix="sdctoolbox-runner-") as raw_temporary:
        temporary = Path(raw_temporary)
        noisy = temporary / "noisy.py"
        noisy.write_text(
            "import os\n"
            "print('NOISY-' + 'x' * 20000)\n"
            "print('CWD=' + os.getcwd())\n"
            "print('ENV=' + os.environ['RUNNER_TEST'])\n",
            encoding="utf-8",
        )
        suite = {
            "id": "noisy",
            "argv": [str(noisy)],
            "cwd": ".",
            "timeout_seconds": 5,
            "environment": {"RUNNER_TEST": "set"},
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run_suites.run_suite(suite, root=temporary)
        report.check(
            code == 0
            and "NOISY-" in output.getvalue()
            and len(output.getvalue()) > 20000
            and f"CWD={temporary}" in output.getvalue()
            and "ENV=set" in output.getvalue(),
            "runner applies cwd/env and drains noisy output without importing suites",
        )

        legacy = temporary / "legacy.py"
        legacy.write_text(
            "print('  PASS  legacy marker')\n"
            "print('RESULT: all 1 checks passed')\n",
            encoding="utf-8",
        )
        direct = subprocess.run(
            [sys.executable, str(legacy)],
            cwd=temporary,
            capture_output=True,
            text=True,
            check=False,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            driven_code = run_suites.run_suite(
                {
                    "id": "legacy",
                    "argv": [str(legacy)],
                    "cwd": ".",
                    "timeout_seconds": 5,
                    "environment": {},
                },
                root=temporary,
            )
        driven_output = output.getvalue()
        report.check(
            driven_code == direct.returncode
            and "legacy marker" in direct.stdout
            and "legacy marker" in driven_output
            and direct.stdout.count("  PASS  ") == driven_output.count("  PASS  ")
            and "RESULT: all 1 checks passed" in driven_output,
            "manifest execution preserves direct exit code, markers, and check total",
        )

        failed = temporary / "failed.py"
        failed.write_text(
            "import sys\nprint('stdout fragment', end='', flush=True)\n"
            "print('stderr fragment', end='', file=sys.stderr, flush=True)\nsys.exit(7)\n",
            encoding="utf-8",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = run_suites.run_suite({**suite, "argv": [str(failed)]}, root=temporary)
        report.check(
            code == 7 and output.getvalue().endswith("stdout fragmentstderr fragment"),
            "runner retains nonzero exit codes and unterminated combined stdout/stderr",
        )

        split_text = temporary / "split_text.py"
        split_text.write_text(
            "import os, time\nos.write(1, b'\\xc3')\ntime.sleep(.03)\n"
            "os.write(1, b'\\xa4\\r')\ntime.sleep(.03)\nos.write(1, b'\\nlast')\n",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, str(split_text)], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        )
        captured = ProcessOutput(process, echo=False)
        try:
            process.wait(timeout=2)
            report.check(
                captured.join(timeout=2)
                and captured.buffered_output == "\N{LATIN SMALL LETTER A WITH DIAERESIS}\nlast",
                "output capture incrementally decodes split multibyte text and universal newlines",
            )
        finally:
            stop_process(process, captured)

        missing_root = temporary / "missing-root"
        with contextlib.redirect_stderr(io.StringIO()):
            code = run_suites.run_suite(suite, root=missing_root)
        report.check(code == 1, "runner reports subprocess spawn failures")

        marker = temporary / "nested-child-survived"
        nested = temporary / "nested.py"
        nested_child = (
            "import time; time.sleep(1); "
            f"open({str(marker)!r}, 'w').write('alive')"
        )
        nested.write_text(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {nested_child!r}])\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        timed = {
            "id": "timed",
            "argv": [str(nested)],
            "cwd": ".",
            "timeout_seconds": 0.2,
            "environment": {},
        }
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO(),
        ):
            code = run_suites.run_suite(timed, root=temporary)
        report.check(code == 124, "runner returns a distinct timeout status")
        report.check(
            not wait_until(marker.exists, timeout=1.5, interval=0.02),
            "timeout terminates nested children before they can outlive the suite",
        )

    orphan_process_checks(report)
    readiness_failure_checks(report)
    windows_resource_checks(report)


def main() -> int:
    report = Report()
    print("Test infrastructure")
    polling_checks(report)
    recorder_checks(report)
    manifest_checks(report)
    runner_checks(report)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
