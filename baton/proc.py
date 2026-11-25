"""Process-tree lifecycle helpers: POSIX process groups and Windows Job Objects.

Why Job Objects on Windows instead of only `taskkill /T /F`:
On Windows, Python venv launchers (`.venv/Scripts/python.exe`) and Node/npm
shims spawn a child process and can exit or race before `taskkill /T` walks the
ParentProcessId tree. A Windows Job Object configured with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` tracks every descendant process in the
kernel regardless of intermediate exits. Calling `TerminateJobObject` and
waiting until `ActiveProcesses == 0` guarantees that every child and grandchild
has exited and released its worktree file handles before Baton deletes the
worktree.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from .ping import _NO_WINDOW

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectBasicAccountingInformation = 1
JobObjectExtendedLimitInformation = 9

_jobs_lock = threading.Lock()
_proc_state: weakref.WeakKeyDictionary[Any, dict[str, Any]] = weakref.WeakKeyDictionary()


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


_kernel32_cache: Any = None


def _win32_kernel32():
    """Return a configured kernel32 WinDLL handle (Windows only)."""
    global _kernel32_cache
    if _kernel32_cache is not None:
        return _kernel32_cache
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    k32.QueryInformationJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    _kernel32_cache = k32
    return k32


def _create_win32_job(proc: subprocess.Popen) -> int | None:
    """Create a kill-on-close Job Object and assign `proc` to it."""
    handle = getattr(proc, "_handle", None)
    if not handle:
        return None
    k32 = _win32_kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(
        job,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok or not k32.AssignProcessToJobObject(job, int(handle)):
        k32.CloseHandle(job)
        return None
    return int(job)


def _wait_win32_job_empty(job: int, timeout: float = 5.0) -> None:
    """Wait until the Windows kernel reports zero active processes in `job`."""
    k32 = _win32_kernel32()
    info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    returned = ctypes.c_uint32(0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok = k32.QueryInformationJobObject(
            job,
            JobObjectBasicAccountingInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
        if not ok or info.ActiveProcesses == 0:
            return
        time.sleep(0.05)


def _state_for(proc: subprocess.Popen) -> dict[str, Any]:
    with _jobs_lock:
        state = _proc_state.get(proc)
        if state is None:
            state = {"lock": threading.Lock(), "job": None, "killed": False}
            _proc_state[proc] = state
        return state


def attach_job(proc: subprocess.Popen) -> bool:
    """Attach `proc` to a Windows Job Object (no-op on POSIX).

    Returns True when a Job Object was created and assigned, False otherwise.
    """
    state = _state_for(proc)
    if os.name != "nt":
        return False
    with state["lock"]:
        if state["job"] is not None:
            return True
        try:
            job = _create_win32_job(proc)
        except OSError:
            job = None
        state["job"] = job
        return job is not None


def close_job(proc: subprocess.Popen) -> None:
    """Release any Windows Job Object handle for `proc` after normal exit."""
    with _jobs_lock:
        state = _proc_state.get(proc)
    if state is None:
        return
    with state["lock"]:
        job = state["job"]
        state["job"] = None
        if job and os.name == "nt":
            try:
                _win32_kernel32().CloseHandle(job)
            except OSError:
                pass


def _taskkill_fallback(pid: int) -> None:
    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    try:
        subprocess.run(
            [str(taskkill), "/T", "/F", "/PID", str(pid)],
            capture_output=True, timeout=30, stdin=subprocess.DEVNULL, **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop `proc` and every descendant process, waiting for teardown to finish.

    Serialized per process so that if `Runner.cancel()` and `Runner._run()` both
    call `kill_tree(proc)`, the second caller waits until termination is
    complete before attempting worktree cleanup.
    """
    state = _state_for(proc)
    with state["lock"]:
        if state["killed"]:
            return
        state["killed"] = True
        job = state["job"]
        state["job"] = None
        if os.name == "nt":
            if job:
                try:
                    k32 = _win32_kernel32()
                    k32.TerminateJobObject(job, 1)
                    _wait_win32_job_empty(job, timeout=5.0)
                    k32.CloseHandle(job)
                except OSError:
                    _taskkill_fallback(proc.pid)
            else:
                _taskkill_fallback(proc.pid)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()  # last resort: at least the direct child
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
