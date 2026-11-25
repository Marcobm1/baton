"""Regressions for the queued task and self-update fetch race."""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from baton import git_lock, self_update
from test_queue import _open, _runner, _task
from test_runner import make_repo


def test_task_and_updater_fetch_in_turn(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    gates = tmp_path / "gates"
    _, runner = _runner(tmp_path, {"x/demo": work}, gates)
    _open(gates)
    task = _task("queued")
    update = self_update.SelfUpdate(work.parent / ".." / work.parent.name / work.name,
                                    runner, True, Mock())
    task_fetch = threading.Event()
    release_task = threading.Event()
    checking = threading.Event()
    updater_fetch = threading.Event()
    original_git = runner._git
    original_command = self_update.command

    def task_git(cwd, *args, **kwargs):
        if args[0] == "fetch":
            task_fetch.set()
            assert release_task.wait(5)
        return original_git(cwd, *args, **kwargs)

    def updater_command(root, argv, *args):
        if argv[1] == "fetch":
            updater_fetch.set()
        return original_command(root, argv, *args)

    def check():
        checking.set()
        update.check()

    monkeypatch.setattr(runner, "_git", task_git)
    monkeypatch.setattr(self_update, "command", updater_command)
    with ThreadPoolExecutor(2) as threads:
        running = threads.submit(runner._run, task)
        assert task_fetch.wait(5)
        updating = threads.submit(check)
        try:
            assert checking.wait(5)
            assert not updater_fetch.wait(0.2)
        finally:
            release_task.set()
        updating.result(10)
        running.result(10)
    assert updater_fetch.is_set() and update.reason == "current"
    assert task.status == "done", task.message


@pytest.mark.parametrize(("error", "failures", "attempts", "status"), [
    ("cannot lock ref 'refs/remotes/origin/main'", 1, 2, "done"),
    ("Unable to create 'demo/.git/index.lock': File exists.", 3, 3, "failed"),
    ("remote origin unavailable", 1, 1, "failed"),
])
def test_task_fetch_retries_only_git_lock_errors(tmp_path, monkeypatch, error, failures, attempts, status):
    work, _ = make_repo(tmp_path)
    gates = tmp_path / "gates"
    store, runner = _runner(tmp_path, {"x/demo": work}, gates)
    _open(gates)
    task = _task("retry")
    real_run = subprocess.run
    fetches = 0

    def run(argv, **kwargs):
        nonlocal fetches
        if argv[:4] == ["git", "-C", str(work), "fetch"]:
            fetches += 1
            if fetches <= failures:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr=error)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    runner._run(task)
    assert fetches == attempts and task.status == status, task.message
    assert store.log_path(task.id).read_text(encoding="utf-8").count("retrying git fetch") == attempts - 1


def test_other_clones_can_proceed_and_same_clone_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(git_lock, "LOCK_TIMEOUT", 0.01)
    with ThreadPoolExecutor(1) as threads:
        def competing_writes():
            assert git_lock.run_locked(tmp_path / "other", lambda: True)
            with pytest.raises(RuntimeError, match="Timed out.*Git lock"):
                git_lock.run_locked(tmp_path, lambda: pytest.fail("entered twice"))

        git_lock.run_locked(tmp_path, lambda: threads.submit(competing_writes).result(2))


def test_updater_retries_fetch_without_exposing_stderr(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    _, runner = _runner(tmp_path, {}, tmp_path / "gates")
    update = self_update.SelfUpdate(work, runner, True, Mock())
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="private URL\ncannot lock ref 'main'")
    run = Mock(return_value=failed)
    monkeypatch.setattr(subprocess, "run", run)
    update.check()
    assert run.call_count == 3 and update.reason == "failed"
    assert update.error == "git fetch (exit 1): Git lock contention"
