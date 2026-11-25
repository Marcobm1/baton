"""Regression tests for Hardening Phase 2 (Blocks 2.2 and 2.3)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from baton import agent_chat, models as models_mod
from baton.config import Settings
from baton.github import Hub
from baton.runner import LocalRepos, Runner, Task, TaskStore
from test_runner import make_repo


def _wait_task(store: TaskStore, task_id: str, timeout: float = 30.0) -> Task:
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = store.get(task_id)
        if t and t.status not in ("queued", "running"):
            return t
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish")


# ----------------------------------------------------------------- Block 2.2


def test_dispatch_casefolds_repository_names(tmp_path):
    work, _ = make_repo(tmp_path)
    gate = tmp_path / "gate"
    started = tmp_path / "started.log"

    class GatedRunner(Runner):
        def _agent_cmd(self, task, wt, restricted=False):
            script = (
                "import pathlib, time\n"
                f"with open({str(started)!r}, 'a', encoding='utf-8') as fh:\n"
                f"    fh.write({task.id!r} + '\\n')\n"
                f"if {task.id!r} == 't1':\n"
                f"    while not pathlib.Path({str(gate)!r}).exists():\n"
                "        time.sleep(0.02)\n"
            )
            return [sys.executable, "-c", script]

    store = TaskStore(tmp_path / "data")
    runner = GatedRunner(
        store,
        LocalRepos("", f"Owner/Demo={work}"),
        timeout=60, local_model="", local_provider="ollama",
        git_name="T", git_email="t@example.com", open_pr=lambda *a: "",
        max_parallel=3,
    )
    runner.submit(Task("t1", "Owner/Demo", "One", "", "codex", "local", time.time(),
                       branch="baton/t1", base="main", open_pr=False))
    runner.submit(Task("t2", "owner/demo", "Two", "", "codex", "local", time.time() + 1,
                       branch="baton/t2", base="main", open_pr=False))

    deadline = time.time() + 10
    while time.time() < deadline and not started.exists():
        time.sleep(0.05)
    assert started.exists()
    # Second task with different casing on the same repo must stay queued.
    time.sleep(0.2)
    assert store.get("t2").status == "queued"
    info = runner.queue_info()
    assert info["t2"]["position"] == 1 and info["t2"]["waiting_for"].id == "t1"

    gate.write_text("go")
    assert _wait_task(store, "t1").status == "no-changes"
    assert _wait_task(store, "t2").status == "no-changes"


def test_cancel_works_during_ci_wait(tmp_path):
    work, _ = make_repo(tmp_path)
    in_ci = threading.Event()

    class QuickRunner(Runner):
        def _agent_cmd(self, task, wt, restricted=False):
            return [sys.executable, "-c", "open('change.txt', 'w').write('ok')"]

    class PendingCI:
        def checks(self, repo, sha):
            in_ci.set()
            return {"pending": ["ci"], "passed": [], "failed": []}

        def merge(self, repo, number, sha):
            raise AssertionError("must not merge a cancelled task")

    store = TaskStore(tmp_path / "data")
    runner = QuickRunner(
        store,
        LocalRepos("", f"x/demo={work}"),
        timeout=60, local_model="", local_provider="ollama",
        git_name="T", git_email="t@example.com",
        open_pr=lambda *a: "https://github.com/x/demo/pull/5",
        github=PendingCI(),
        auto_merge=True,
    )
    runner.ci_poll = 30  # long poll: cancel must wake immediately via _cancel_events

    runner.submit(Task("ci1", "x/demo", "CI wait", "", "codex", "local", time.time(),
                       branch="baton/ci1", base="main"))
    assert in_ci.wait(15), "task never reached CI wait"
    assert runner.can_cancel("ci1")
    started = time.time()
    assert runner.cancel("ci1", wait=10)
    assert time.time() - started < 5  # woke immediately, did not sleep 30s
    done = store.get("ci1")
    assert done.status == "cancelled"
    assert "on GitHub" in done.message


def test_models_save_skips_unchanged_and_retries_permission_error(tmp_path, monkeypatch):
    mgr = models_mod.LocalModels(tmp_path)
    replaces = []
    real_replace = models_mod.os.replace

    def flaky_replace(src, dst):
        replaces.append(1)
        if len(replaces) <= 2:
            raise PermissionError("locked by antivirus")
        return real_replace(src, dst)

    monkeypatch.setattr(models_mod.os, "replace", flaky_replace)
    monkeypatch.setattr(models_mod.time, "sleep", lambda _s: None)

    state = {"m1": {"pid": 123}}
    mgr._save(state)
    assert len(replaces) == 3  # 2 retries + 1 success
    # Identical state does not touch disk again.
    mgr._save({"m1": {"pid": 123}})
    assert len(replaces) == 3


def test_models_chat_port_is_lightweight_without_overview(tmp_path, monkeypatch):
    srv = tmp_path / "llama-server"
    gguf = tmp_path / "m.gguf"
    srv.write_text("")
    gguf.write_text("")
    (tmp_path / "models.json").write_text(json.dumps({
        "server": str(srv),
        "models": [{"id": "m1", "name": "Model 1", "file": str(gguf), "port": 18080, "vram_gb": 4}],
    }), encoding="utf-8")

    mgr = models_mod.LocalModels(tmp_path)
    monkeypatch.setattr(mgr, "overview", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("overview called")))
    monkeypatch.setattr(models_mod, "_listener", lambda port: object() if port == 18080 else None)
    monkeypatch.setattr(models_mod, "_is_llama_server", lambda p: True)
    monkeypatch.setattr(models_mod, "_health", lambda port: "ok" if port == 18080 else None)

    assert mgr.chat_port("m1") == 18080
    with pytest.raises(models_mod.ModelError) as exc:
        mgr.chat_port("unknown")
    assert exc.value.key == "m_err_unknown"


def test_models_start_enforces_vram_budget_inside_lock(tmp_path, monkeypatch):
    srv = tmp_path / "llama-server"
    gguf1 = tmp_path / "m1.gguf"
    gguf2 = tmp_path / "m2.gguf"
    for p in (srv, gguf1, gguf2):
        p.write_text("")
    (tmp_path / "models.json").write_text(json.dumps({
        "server": str(srv),
        "models": [
            {"id": "m1", "name": "Big 1", "file": str(gguf1), "port": 18081, "vram_gb": 10},
            {"id": "m2", "name": "Big 2", "file": str(gguf2), "port": 18082, "vram_gb": 10},
        ],
    }), encoding="utf-8")

    mgr = models_mod.LocalModels(tmp_path)
    launched: list[int] = []
    entered = threading.Event()
    release = threading.Event()

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 5000 + len(launched)
            launched.append(self.pid)
            entered.set()
            release.wait(2.0)

    monkeypatch.setattr(mgr, "gpus", lambda: [{"vram_total_mib": 16 * 1024, "vram_used_mib": 0, "temp": 40}])
    monkeypatch.setattr(models_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(models_mod.psutil, "Process", lambda pid: Mock(create_time=lambda: 100.0))
    monkeypatch.setattr(models_mod.LocalModels, "_proc", staticmethod(
        lambda entry: Mock() if entry.get("pid") in launched else None
    ))
    monkeypatch.setattr(models_mod, "_listener", lambda port: None)
    monkeypatch.setattr(models_mod, "_health", lambda port: "ok")
    monkeypatch.setattr(models_mod, "_metrics", lambda port: {})

    errors: list[models_mod.ModelError] = []

    def run_start(mid: str):
        try:
            mgr.start(mid)
        except models_mod.ModelError as exc:
            errors.append(exc)

    t1 = threading.Thread(target=run_start, args=("m1",))
    t2 = threading.Thread(target=run_start, args=("m2",))
    t1.start()
    assert entered.wait(2.0)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join(5.0)
    t2.join(5.0)

    assert len(launched) == 1
    assert len(errors) == 1 and errors[0].key == "m_err_vram"


def test_agent_chats_periodic_reaper_and_turn_timeout(tmp_path):
    fake_agent = Path(__file__).parent / "fake_acp_agent.py"
    chats = agent_chat.AgentChats(
        command=lambda: [sys.executable, str(fake_agent)],
        idle_seconds=0.3,
        turn_timeout=0.3,
        reap_interval=0.05,
    )
    try:
        chat = chats.create(tmp_path, "demo", "opencode-go/glm-5.3", "default", "confirm", "opencode")
        assert chats.get(chat.id) is not None
        # Wait for the periodic idle reaper to close the idle conversation automatically.
        deadline = time.time() + 5.0
        while time.time() < deadline and chats.get(chat.id) is not None:
            time.sleep(0.05)
        assert chats.get(chat.id) is None
        assert not chat.alive
    finally:
        chats.close_all()

    # Turn timeout: a hung session/prompt cancels and emits an error instead of hanging forever.
    chat2 = agent_chat.AgentChat("c2", tmp_path, "demo", "m", "default", turn_timeout=0.1)
    cancelled = []

    class HangingClient:
        def request(self, method, params, timeout=None):
            assert method == "session/prompt" and timeout == 0.1
            raise agent_chat.AcpError("The agent did not answer session/prompt in 0 s.")

        def notify(self, method, params):
            cancelled.append(method)

        def close(self):
            pass

    chat2.client = HangingClient()
    chat2.session_id = "s1"
    chat2.prompt("hang please")
    deadline = time.time() + 3.0
    while time.time() < deadline and chat2.busy:
        time.sleep(0.02)
    assert not chat2.busy
    assert cancelled == ["session/cancel"]
    assert any(e["type"] == "error" and "did not answer" in e["text"] for e in chat2.events)


# ----------------------------------------------------------------- Block 2.3


def test_open_pulls_does_not_cache_transient_graphql_failures(monkeypatch):
    fail = True

    def handler(request):
        if fail:
            return httpx.Response(503)
        page = {"nodes": [{"title": "PR 1", "number": 1, "commits": {"nodes": []}}],
                "pageInfo": {"hasNextPage": False}}
        return httpx.Response(200, json={"data": {"repository": {"pullRequests": page}}})

    hub = Hub(Settings(github_token="t", cache_seconds=300))
    monkeypatch.setattr(hub, "_client", lambda: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)))

    assert asyncio.run(hub.open_pulls("u/demo")) == []
    fail = False
    pulls = asyncio.run(hub.open_pulls("u/demo"))
    assert len(pulls) == 1 and pulls[0]["title"] == "PR 1"


def test_hub_load_bounds_concurrency_with_semaphore(monkeypatch):
    in_flight = 0
    peak = 0
    repos = [{"full_name": f"u/r{i}", "name": f"r{i}", "default_branch": "main",
              "archived": False, "fork": False, "html_url": f"https://github.com/u/r{i}"}
             for i in range(20)]

    hub = Hub(Settings(github_token="t", owner="u"))

    async def fake_list(self, client):
        return repos

    async def fake_projects_in(self, client, repo):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return []

    monkeypatch.setattr(Hub, "_list_repos", fake_list)
    monkeypatch.setattr(Hub, "_projects_in", fake_projects_in)

    asyncio.run(hub.load(force=True))
    assert 1 <= peak <= 8


def test_hub_delete_branch_and_merged_task_cleanup(tmp_path, monkeypatch):
    # 1. Hub.delete_branch refuses non-baton branches and deletes baton/* branches.
    deleted_urls: list[str] = []

    def fake_delete(url, *, headers, timeout):
        deleted_urls.append(url)
        return httpx.Response(204)

    monkeypatch.setattr(httpx, "delete", fake_delete)
    hub = Hub(Settings(github_token="tok"))
    assert hub.delete_branch("u/demo", "main") is False
    assert not deleted_urls
    assert hub.delete_branch("u/demo", "baton/t-clean") is True
    assert deleted_urls == ["https://api.github.com/repos/u/demo/git/refs/heads/baton/t-clean"]

    # 2. Merged task calls delete_branch and cleans up the local baton/* branch.
    work, _origin = make_repo(tmp_path)
    deleted_remote: list[tuple[str, str]] = []

    class QuickRunner(Runner):
        def _agent_cmd(self, task, wt, restricted=False):
            return [sys.executable, "-c", "open('ok.txt', 'w').write('ok')"]

    class CleaningGitHub:
        def checks(self, repo, sha):
            return {"pending": [], "passed": ["ci"], "failed": []}

        def merge(self, repo, number, sha):
            pass

        def delete_branch(self, repo, branch):
            deleted_remote.append((repo, branch))
            return True

    store = TaskStore(tmp_path / "data")
    runner = QuickRunner(
        store,
        LocalRepos("", f"x/demo={work}"),
        timeout=60, local_model="", local_provider="ollama",
        git_name="T", git_email="t@example.com",
        open_pr=lambda *a: "https://github.com/x/demo/pull/42",
        github=CleaningGitHub(),
        auto_merge=True,
    )
    runner.submit(Task("t-clean", "x/demo", "Clean branch", "", "codex", "local", time.time(),
                       branch="baton/t-clean", base="main"))
    done = _wait_task(store, "t-clean")
    assert done.status == "done" and done.merged, done.message
    assert deleted_remote == [("x/demo", "baton/t-clean")]
    local_branches = subprocess.run(
        ["git", "-C", str(work), "branch", "--list", "baton/t-clean"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert local_branches.strip() == ""
