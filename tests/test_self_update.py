"""Exercise the updater against two clones and a bare origin, without a live restart."""

import json
import os
import re
import subprocess
import sys
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from baton import __main__ as cli
from baton.config import Settings
from baton.runner import UpdateInProgress
from baton.self_update import SelfUpdate
from baton.web import create_app
from test_queue import _runner, _task
from test_runner import git, make_repo


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    work, origin = make_repo(tmp_path)
    publisher = tmp_path / "publisher"
    git(tmp_path, "clone", "-q", str(origin), str(publisher))
    git(publisher, "config", "user.email", "t@example.com")
    git(publisher, "config", "user.name", "T")
    (publisher / "README.md").write_text("new")
    git(publisher, "commit", "-qam", "update")
    git(publisher, "push")
    _, runner = _runner(tmp_path, {}, tmp_path / "gates")
    updater = SelfUpdate(work, runner, True, Mock())
    monkeypatch.setattr(sys, "base_prefix", "test-base")
    return updater, publisher


@pytest.mark.parametrize("dependency_change", [False, True])
def test_update_validates_before_restart(checkout, dependency_change):
    update, publisher = checkout
    old = update.running
    if dependency_change:
        (publisher / "pyproject.toml").write_text("")
        git(publisher, "add", "pyproject.toml")
        git(publisher, "commit", "-qm", "dependencies")
        git(publisher, "push")
    steps = []
    update.install = lambda: steps.append("pip")

    def validate():
        assert (update.root / "README.md").read_text() == "new"
        with pytest.raises(UpdateInProgress):
            update.runner.submit(_task("late"))
        steps.append("tests")

    update.validate = validate
    update.restart = lambda: steps.append("restart")
    update.check()
    assert steps == (["pip"] if dependency_change else []) + ["tests", "restart"]
    assert update.restarting and update.running == old
    record = json.loads(update.history.read_text())
    assert record["date"] and record["old"] == old
    assert record["new"] == update.git("rev-parse", "origin/main") != old
    assert record["result"] == "validated"
    booted = SelfUpdate(update.root, update.runner, True, Mock())
    assert booted.running == record["new"]
    assert json.loads(update.history.read_text().splitlines()[-1])["result"] == "running"


@pytest.mark.parametrize("reason", ["running", "queued", "dirty", "branch", "diverged"])
def test_waits_without_changing_checkout(checkout, reason):
    update, _ = checkout
    if reason in ("running", "queued"):
        task = _task("other", repo="other/repo")
        task.status = reason
        update.runner.store.save(task)
    elif reason == "dirty":
        (update.root / "notes").write_text("mine")
    elif reason == "branch":
        git(update.root, "switch", "-qc", "feature")
    else:
        git(update.root, "commit", "-q", "--allow-empty", "-m", "local")
        update.running = update.git("rev-parse", "HEAD")
    update.check()
    assert update.checked and update.pending and not update.restarting
    assert update.reason == {"running": "busy", "queued": "busy", "diverged": "failed"}.get(reason, reason)
    assert update.git("rev-parse", "HEAD") == update.running
    update.restart.assert_not_called()


def test_failed_tests_restore_revision_and_dependencies(checkout, monkeypatch):
    update, publisher = checkout
    (publisher / "pyproject.toml").write_text("")
    (publisher / "test_candidate.py").write_text("def test_new(): assert False\n")
    git(publisher, "add", "-A")
    git(publisher, "commit", "-qm", "broken")
    git(publisher, "push")
    installs = []
    update.install = lambda: installs.append(update.git("rev-parse", "HEAD"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    update.check()
    assert installs == [update.pending, update.running]
    assert update.git("rev-parse", "HEAD") == update.running
    assert "pytest -q" in update.error and "rolled back" in update.error
    assert json.loads(update.history.read_text())["result"] == update.error
    assert update.runner.reserve_update()
    update.restart.assert_not_called()


def test_disabled_and_non_git(tmp_path, checkout):
    update, _ = checkout
    for root, enabled in ((tmp_path, True), (update.root, False)):
        disabled = SelfUpdate(root, update.runner, enabled, Mock())
        disabled.check()
        assert not disabled.enabled and not disabled.checked


def test_windows_restart_waits_for_own_pid(tmp_path, monkeypatch):
    query, spawn = Mock(), Mock()
    monkeypatch.setattr(subprocess, "run", query)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    for flag in ("CREATE_NO_WINDOW", "CREATE_BREAKAWAY_FROM_JOB"):
        monkeypatch.setattr(subprocess, flag, 1, raising=False)
    cli.windows_restart(tmp_path)
    assert query.call_args.args[0] == ["schtasks", "/Query", "/TN", "Baton"]
    script = spawn.call_args.args[0][-1]
    assert f"Wait-Process -Id {os.getpid()}" in script
    assert "schtasks /Run /TN Baton" in script
    assert "CommandLine" not in script and "baton serve" not in script


def test_update_button_csrf(tmp_path, monkeypatch, checkout):
    update, _ = checkout
    # Keep the web app on a disposable checkout even when invoked in production.
    from baton import web
    monkeypatch.setattr(web, "SelfUpdate", lambda *args: update)
    checked = threading.Event()
    update.check = Mock(side_effect=checked.set)
    update.repo = "owner/baton"
    monkeypatch.setattr(web, "Runner", lambda *args, **kwargs: update.runner)
    with TestClient(create_app(Settings(data_dir=tmp_path, codex_home=tmp_path, lang="es"))) as client:
        html = client.get("/quotas").text
        assert "Actualizar ahora" in html and update.running[:12] in html
        assert checked.wait(5)
        checked.clear()
        task = _task("merged", repo="Owner/Baton")
        task.merged = True
        update.runner.on_finished(task)
        assert checked.wait(5)
        checked.clear()
        token = re.search(r'name="csrf" value="([^"]+)"', html)[1]
        assert client.post("/self-update").status_code == 403
        assert client.post("/self-update", data={"csrf": token},
                           headers={"origin": "https://example.org"}).status_code == 403
        assert client.post("/self-update", data={"csrf": token}, follow_redirects=False).status_code == 303
        assert checked.wait(5)
    assert update.check.call_count >= 2


def test_a_later_successful_update_clears_an_old_error(tmp_path):
    work, _ = make_repo(tmp_path)
    _, runner = _runner(tmp_path, {}, tmp_path / "gates")
    history = runner.store.dir / "updates.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    rows = [dict(date="d", old="a", new="b", result="pytest -q (exit 1): x (rolled back)"),
            dict(date="d", old="a", new="c", result="validated"),
            dict(date="d", old="a", new="c", result="running")]
    history.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert SelfUpdate(work, runner, True, Mock()).error == ""
    with history.open("a", encoding="utf-8") as log:
        log.write(json.dumps(dict(date="d", old="c", new="e", result="boom")) + "\n")
    assert SelfUpdate(work, runner, True, Mock()).error == "boom"
