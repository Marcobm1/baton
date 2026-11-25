"""Tests for process-tree teardown (Windows Job Objects + worktree fallback)."""

from __future__ import annotations

import ctypes
import os
import stat
import subprocess
import threading
import time

from baton import proc as proc_mod
from baton.runner import LocalRepos, Runner, Task, TaskStore
from test_runner import make_repo


class _FakeProc:
    def __init__(self, pid: int = 4321, handle: int = 999):
        self.pid = pid
        self._handle = handle
        self.waits = 0
        self.kills = 0

    def wait(self, timeout=None):
        self.waits += 1
        return 0

    def kill(self):
        self.kills += 1


def test_win32_job_struct_sizes_match_abi():
    ptr_size = ctypes.sizeof(ctypes.c_size_t)
    expected_extended = 144 if ptr_size == 8 else 112
    assert ctypes.sizeof(proc_mod._JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == expected_extended
    assert ctypes.sizeof(proc_mod._JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) == 48


def test_win32_job_attach_terminate_wait_and_close(monkeypatch):
    calls: list[tuple] = []
    active_counts = [2, 1, 0]

    class FakeKernel32:
        def CreateJobObjectW(self, attrs, name):
            calls.append(("create",))
            return 777

        def SetInformationJobObject(self, job, info_class, info_ptr, size):
            info = ctypes.cast(info_ptr, ctypes.POINTER(proc_mod._JOBOBJECT_EXTENDED_LIMIT_INFORMATION)).contents
            calls.append(("set_info", job, info_class, info.BasicLimitInformation.LimitFlags, size))
            return 1

        def AssignProcessToJobObject(self, job, proc_handle):
            calls.append(("assign", job, proc_handle))
            return 1

        def TerminateJobObject(self, job, exit_code):
            calls.append(("terminate", job, exit_code))
            return 1

        def QueryInformationJobObject(self, job, info_class, info_ptr, size, ret_ptr):
            info = ctypes.cast(info_ptr, ctypes.POINTER(proc_mod._JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)).contents
            info.ActiveProcesses = active_counts.pop(0) if active_counts else 0
            calls.append(("query", job, info_class, info.ActiveProcesses))
            return 1

        def CloseHandle(self, handle):
            calls.append(("close", handle))
            return 1

    monkeypatch.setattr(proc_mod.os, "name", "nt")
    monkeypatch.setattr(proc_mod, "_win32_kernel32", lambda: FakeKernel32())
    monkeypatch.setattr(proc_mod.time, "sleep", lambda _s: None)
    taskkill_called = []
    monkeypatch.setattr(proc_mod, "_taskkill_fallback", lambda pid: taskkill_called.append(pid))

    p = _FakeProc(pid=1234, handle=555)
    assert proc_mod.attach_job(p) is True
    proc_mod.kill_tree(p)
    # Second call is a no-op (idempotent, never re-kills a recycled PID).
    proc_mod.kill_tree(p)

    assert not taskkill_called
    assert p.waits == 1
    assert ("create",) in calls
    assert ("set_info", 777, proc_mod.JobObjectExtendedLimitInformation,
            proc_mod.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            ctypes.sizeof(proc_mod._JOBOBJECT_EXTENDED_LIMIT_INFORMATION)) in calls
    assert ("assign", 777, 555) in calls
    assert ("terminate", 777, 1) in calls
    assert [c for c in calls if c[0] == "query"] == [
        ("query", 777, proc_mod.JobObjectBasicAccountingInformation, 2),
        ("query", 777, proc_mod.JobObjectBasicAccountingInformation, 1),
        ("query", 777, proc_mod.JobObjectBasicAccountingInformation, 0),
    ]
    assert calls[-1] == ("close", 777)


def test_win32_job_assign_failure_closes_handle_and_falls_back_to_taskkill(monkeypatch):
    closed: list[int] = []
    taskkilled: list[int] = []

    class FailingAssignKernel32:
        def CreateJobObjectW(self, attrs, name):
            return 888

        def SetInformationJobObject(self, job, info_class, info_ptr, size):
            return 1

        def AssignProcessToJobObject(self, job, proc_handle):
            return 0  # simulate failure

        def CloseHandle(self, handle):
            closed.append(handle)
            return 1

    monkeypatch.setattr(proc_mod.os, "name", "nt")
    monkeypatch.setattr(proc_mod, "_win32_kernel32", lambda: FailingAssignKernel32())
    monkeypatch.setattr(proc_mod, "_taskkill_fallback", lambda pid: taskkilled.append(pid))

    p = _FakeProc(pid=2468, handle=666)
    assert proc_mod.attach_job(p) is False
    assert closed == [888]
    proc_mod.kill_tree(p)
    assert taskkilled == [2468]


def test_win32_close_job_releases_handle_on_normal_exit(monkeypatch):
    closed: list[int] = []

    class NormalKernel32:
        def CreateJobObjectW(self, attrs, name):
            return 991

        def SetInformationJobObject(self, job, info_class, info_ptr, size):
            return 1

        def AssignProcessToJobObject(self, job, proc_handle):
            return 1

        def CloseHandle(self, handle):
            closed.append(handle)
            return 1

    monkeypatch.setattr(proc_mod.os, "name", "nt")
    monkeypatch.setattr(proc_mod, "_win32_kernel32", lambda: NormalKernel32())

    p = _FakeProc(pid=1111, handle=222)
    assert proc_mod.attach_job(p) is True
    proc_mod.close_job(p)
    proc_mod.close_job(p)  # idempotent
    assert closed == [991]


def test_concurrent_kill_tree_waits_for_first_teardown_to_finish(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    class SlowProc(_FakeProc):
        def wait(self, timeout=None):
            started.set()
            release.wait(2.0)
            order.append("first_done")
            return 0

    monkeypatch.setattr(proc_mod.os, "name", "posix")
    monkeypatch.setattr(proc_mod.os, "killpg", lambda pid, sig: None)

    p = SlowProc()
    t1 = threading.Thread(target=proc_mod.kill_tree, args=(p,))
    t1.start()
    assert started.wait(2.0)

    def second_caller():
        proc_mod.kill_tree(p)
        order.append("second_returned")

    t2 = threading.Thread(target=second_caller)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join(2.0)
    t2.join(2.0)
    assert order == ["first_done", "second_returned"]


def test_discard_fallback_removes_readonly_worktree_when_git_remove_fails(tmp_path, monkeypatch):
    work, _origin = make_repo(tmp_path)
    store = TaskStore(tmp_path / "data")
    runner = Runner(
        store,
        LocalRepos("", f"x/demo={work}"),
        timeout=60,
        local_model="",
        local_provider="ollama",
        git_name="T",
        git_email="t@example.com",
        open_pr=lambda *a: "",
    )
    wt = tmp_path / "data" / "worktrees" / "demo-ro"
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(work), "worktree", "add", "-b", "baton/ro", str(wt), "origin/main"],
        check=True, capture_output=True,
    )
    locked_file = wt / "readonly.txt"
    locked_file.write_text("locked", encoding="utf-8")
    os.chmod(locked_file, stat.S_IREAD)

    real_write_git = runner._write_git

    def flaky_write_git(task, repo_dir, *args, **kwargs):
        # Simulate git worktree remove failing transiently on Windows.
        if args[:2] == ("worktree", "remove"):
            return ""
        return real_write_git(task, repo_dir, *args, **kwargs)

    monkeypatch.setattr(runner, "_write_git", flaky_write_git)
    monkeypatch.setattr("baton.runner.time.sleep", lambda _s: None)
    task = Task(
        id="ro", repo="x/demo", title="RO", details="", agent="codex",
        where="local", created_at=0, branch="baton/ro", base="main", worktree=str(wt),
    )

    msg = runner._discard(task, work, wt)
    assert "removed" in msg
    assert not wt.exists()
    assert task.worktree == ""
    branch = subprocess.run(
        ["git", "-C", str(work), "branch", "--list", "baton/ro"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert branch.strip() == ""
