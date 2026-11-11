"""Own suite descendants before they can run, using only native OS facilities."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from typing import Any

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    class _JobAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_int64),
            ("TotalKernelTime", ctypes.c_int64),
            ("ThisPeriodTotalUserTime", ctypes.c_int64),
            ("ThisPeriodTotalKernelTime", ctypes.c_int64),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _kernel.SetInformationJobObject.restype = wintypes.BOOL
    _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel.TerminateJobObject.restype = wintypes.BOOL
    _kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    _kernel.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.CloseHandle.restype = wintypes.BOOL
    _kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)]
    _kernel.Thread32First.restype = wintypes.BOOL
    _kernel.Thread32Next.argtypes = _kernel.Thread32First.argtypes
    _kernel.Thread32Next.restype = wintypes.BOOL
    _kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel.OpenThread.restype = wintypes.HANDLE
    _kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel.ResumeThread.restype = wintypes.DWORD
    _kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel.WaitForSingleObject.restype = wintypes.DWORD

    class _WindowsJob:
        def __init__(self) -> None:
            self.handle = _kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                limits = _ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not _kernel.SetInformationJobObject(
                    self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException:
                self.close()
                raise

        def assign_and_resume(self, process: subprocess.Popen[str]) -> None:
            if not _kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            # Popen closes the primary thread handle; the suspended process cannot
            # spawn children while we find and resume that thread after assignment.
            snapshot = _kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
            if snapshot == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                entry = _ThreadEntry()
                entry.dwSize = ctypes.sizeof(entry)
                found = _kernel.Thread32First(snapshot, ctypes.byref(entry))
                while found:
                    if entry.th32OwnerProcessID == process.pid:
                        thread = _kernel.OpenThread(
                            0x2, False, entry.th32ThreadID,  # THREAD_SUSPEND_RESUME
                        )
                        if not thread:
                            raise ctypes.WinError(ctypes.get_last_error())
                        try:
                            if _kernel.ResumeThread(thread) == 0xFFFFFFFF:
                                raise ctypes.WinError(ctypes.get_last_error())
                        finally:
                            _kernel.CloseHandle(thread)
                        return
                    found = _kernel.Thread32Next(snapshot, ctypes.byref(entry))
                raise OSError("suspended suite has no primary thread")
            finally:
                _kernel.CloseHandle(snapshot)

        def close(self) -> None:
            if self.handle:
                try:
                    if not _kernel.TerminateJobObject(self.handle, 1):
                        raise ctypes.WinError(ctypes.get_last_error())
                    deadline = time.monotonic() + 2
                    accounting = _JobAccounting()
                    while True:
                        if not _kernel.QueryInformationJobObject(
                            self.handle, 1, ctypes.byref(accounting),
                            ctypes.sizeof(accounting), None,
                        ):
                            raise ctypes.WinError(ctypes.get_last_error())
                        if not accounting.ActiveProcesses:
                            break
                        if time.monotonic() >= deadline:
                            raise TimeoutError("suite job did not stop within 2 seconds")
                        time.sleep(0.01)
                finally:
                    _kernel.CloseHandle(self.handle)
                    self.handle = None


def _wait_for_exit(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        # GetExitCodeProcess can report an exit before the handle is signaled.
        result = _kernel.WaitForSingleObject(int(process._handle), 2000)
        if result == 258:  # WAIT_TIMEOUT
            raise subprocess.TimeoutExpired(process.args, 2)
        if result == 0xFFFFFFFF:  # WAIT_FAILED
            raise ctypes.WinError(ctypes.get_last_error())
    process.wait(timeout=2)


@contextmanager
def _defer_sigint():
    handler = signal.getsignal(signal.SIGINT)
    if threading.current_thread() is not threading.main_thread() or not callable(handler):
        yield lambda: None
        return
    pending = []

    def defer(signum, frame):
        pending[:] = [(signum, frame)]

    def deliver():
        if pending:
            handler(*pending.pop())

    signal.signal(signal.SIGINT, defer)
    try:
        yield deliver
    finally:
        signal.signal(signal.SIGINT, handler)
        deliver()


def start_process(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
    """Start a suite in a new POSIX session or a non-breakaway Windows job."""
    job = None
    process = None
    try:
        with _defer_sigint() as check_interrupt:
            job = _WindowsJob() if os.name == "nt" else None
            # Retain the instance even when __init__ raises after native creation.
            process = subprocess.Popen.__new__(subprocess.Popen)
            process.__init__(
                command, **kwargs,
                start_new_session=os.name != "nt",
                creationflags=(
                    0x4 | subprocess.CREATE_NEW_PROCESS_GROUP  # CREATE_SUSPENDED
                ) if job else 0,
            )
            check_interrupt()
            if job:
                process._suite_job = job
                job.assign_and_resume(process)
        return process
    except BaseException:
        try:
            if job:
                job.close()
        finally:
            if process is not None:
                try:
                    if getattr(process, "_child_created", False):
                        if os.name == "nt":
                            process.kill()
                            _wait_for_exit(process)
                        else:
                            terminate_process_tree(process)
                finally:
                    for stream in (
                        getattr(process, name, None) for name in ("stdin", "stdout", "stderr")
                    ):
                        if stream is not None:
                            stream.close()
                    handle = getattr(process, "_handle", None)
                    if handle is not None:
                        handle.Close()
        raise


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop owned descendants even if the group leader has already exited."""
    if os.name == "nt":
        process._suite_job.close()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        _wait_for_exit(process)
    except subprocess.TimeoutExpired:
        process.kill()
        _wait_for_exit(process)
