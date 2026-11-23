"""The local task queue: one task per repository, a global limit, priorities.

Git is real (bare "origin" repositories on disk). The fake agents wait for a
"gate" file before finishing, so a test decides when each task may end.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

from baton import runner as runner_mod
from baton.runner import LocalRepos, Runner, Task, TaskStore

from test_runner import git, make_repo

PASSED = {"pending": [], "passed": ["test"], "failed": []}


def _gated_cmd(gates: Path):
    """Agent command: wait for gates/<task id> (or gates/all), then write
    <task id>.md listing the .md files the worktree started with."""
    gates.mkdir(parents=True, exist_ok=True)

    def cmd(task, wt):
        script = ("import os, sys, time\n"
                  "sys.stdin.read()\n"
                  f"while not (os.path.exists({str(gates / task.id)!r}) or "
                  f"os.path.exists({str(gates / 'all')!r})):\n"
                  "    time.sleep(0.02)\n"
                  "seen = ' '.join(sorted(f for f in os.listdir('.') if f.endswith('.md')))\n"
                  f"open({task.id + '.md'!r}, 'w').write('seen: ' + seen)\n")
        return [sys.executable, "-c", script]
    return cmd


def _open(gates: Path, name: str = "all") -> None:
    (gates / name).write_text("go")


def _runner(tmp_path, paths: dict[str, Path], gates: Path, github=None, max_parallel=3):
    store = TaskStore(tmp_path / "data")
    opened = []

    def open_pr(*a):
        opened.append(a)
        return f"https://github.com/x/demo/pull/{len(opened)}"

    r = Runner(store, LocalRepos("", ";".join(f"{k}={v}" for k, v in paths.items())),
               timeout=60, local_model="", local_provider="ollama", git_name="Baton",
               git_email="baton@localhost", open_pr=open_pr, github=github,
               auto_merge=github is not None, max_parallel=max_parallel)
    r.ci_poll, r.ci_grace = 0.01, 0.05
    r._agent_cmd = _gated_cmd(gates)
    return store, r


def _task(tid, repo="x/demo", priority="normal", created=None):
    return Task(id=tid, repo=repo, title=f"Task {tid}", details="", agent="codex", where="local",
                created_at=time.time() if created is None else created, branch=f"baton/{tid}",
                base="main", priority=priority)


def _wait_status(store, tid, *statuses, tries=400):
    for _ in range(tries):
        t = store.get(tid)
        if t and t.status in statuses:
            return t
        time.sleep(0.05)
    raise AssertionError(f"{tid} never reached {statuses}: {store.get(tid)}")


def _wait_done(store, tid):
    return _wait_status(store, tid, "done", "no-changes", "failed", "cancelled")


def _show(origin, ref):
    return subprocess.run(["git", "-C", str(origin), "show", ref], capture_output=True,
                          text=True, check=True).stdout


class MergingGitHub:
    """Tests always pass and "merging" really moves origin's main."""

    def __init__(self, origin):
        self.origin, self.merged = origin, []

    def checks(self, repo, sha):
        return PASSED

    def comment(self, repo, number, body):
        pass

    def merge(self, repo, number, sha):
        git(self.origin, "update-ref", "refs/heads/main", sha)
        self.merged.append(sha)


def test_same_repo_tasks_run_one_after_another_from_the_updated_origin(tmp_path):
    work, origin = make_repo(tmp_path)
    gates = tmp_path / "gates"
    gh = MergingGitHub(origin)
    store, r = _runner(tmp_path, {"x/demo": work}, gates, github=gh)
    r.submit(_task("t-a"))
    _wait_status(store, "t-a", "running")
    r.submit(_task("t-b"))
    assert store.get("t-b").status == "queued"          # no error: it waits
    info = r.queue_info()["t-b"]
    assert info["position"] == 1 and info["waiting_for"].id == "t-a"
    time.sleep(0.3)
    assert store.get("t-b").status == "queued" and store.get("t-b").started_at is None
    _open(gates)
    a, b = _wait_done(store, "t-a"), _wait_done(store, "t-b")
    assert a.status == b.status == "done" and a.merged and b.merged, (a.message, b.message)
    # B started only after A had finished completely (merge included)…
    assert b.started_at >= a.finished_at
    # …so its worktree came from the origin/main that already had A's work.
    assert _show(origin, "baton/t-b:t-b.md") == "seen: README.md t-a.md"
    assert _show(origin, "baton/t-a:t-a.md") == "seen: README.md"
    assert len(gh.merged) == 2


def test_priority_order_and_running_task_is_never_interrupted(tmp_path):
    work, _ = make_repo(tmp_path)
    gates = tmp_path / "gates"
    store, r = _runner(tmp_path, {"x/demo": work}, gates)
    r.submit(_task("t-run"))
    _wait_status(store, "t-run", "running")
    now = time.time()
    r.submit(_task("t-low", priority="low", created=now))
    r.submit(_task("t-high1", priority="high", created=now + 1))
    r.submit(_task("t-high2", priority="high", created=now + 2))
    r.submit(_task("t-normal", created=now + 3))
    order = ["t-high1", "t-high2", "t-normal", "t-low"]
    assert [t.id for t in r.queued()] == order
    info = r.queue_info()
    assert [info[t]["position"] for t in order] == [1, 2, 3, 4]
    assert info["t-high1"]["waiting_for"].id == "t-run"
    assert info["t-high2"]["waiting_for"].id == "t-high1"
    assert store.get("t-run").status == "running"       # high priority doesn't interrupt it
    _open(gates)
    done = [_wait_done(store, t) for t in ["t-run", *order]]
    assert all(t.status == "done" for t in done), [t.message for t in done]
    started = [t.id for t in sorted(done, key=lambda t: t.started_at)]
    assert started == ["t-run", *order]


def test_changing_the_priority_of_a_queued_task(tmp_path):
    work, _ = make_repo(tmp_path)
    gates = tmp_path / "gates"
    store, r = _runner(tmp_path, {"x/demo": work}, gates)
    r.submit(_task("t-run"))
    _wait_status(store, "t-run", "running")
    now = time.time()
    r.submit(_task("t-1", created=now))
    r.submit(_task("t-2", priority="low", created=now + 1))
    assert [t.id for t in r.queued()] == ["t-1", "t-2"]
    assert r.set_priority("t-2", "high")
    assert [t.id for t in r.queued()] == ["t-2", "t-1"]
    assert not r.set_priority("t-run", "low")           # already started
    assert store.get("t-run").priority == "normal"
    _open(gates)
    first, second = _wait_done(store, "t-2"), _wait_done(store, "t-1")
    assert first.started_at < second.started_at


def test_global_limit_of_parallel_tasks(tmp_path):
    repos = {f"x/{n}": make_repo(tmp_path / n)[0] for n in ("a", "b", "c")}
    gates = tmp_path / "gates"
    store, r = _runner(tmp_path, repos, gates, max_parallel=2)
    now = time.time()
    for i, n in enumerate(("a", "b", "c")):
        r.submit(_task(f"t-{n}", repo=f"x/{n}", created=now + i))
    _wait_status(store, "t-a", "running")
    _wait_status(store, "t-b", "running")
    time.sleep(0.3)
    assert store.get("t-c").status == "queued"
    info = r.queue_info()["t-c"]
    assert info["waiting_for"] is None and (info["running"], info["max"]) == (2, 2)
    _open(gates, "t-a")                     # one slot frees up: c takes it
    a = _wait_done(store, "t-a")
    c = _wait_status(store, "t-c", "running")
    assert c.started_at >= a.finished_at
    assert store.get("t-b").status == "running"
    _open(gates)
    tasks = [_wait_done(store, f"t-{n}") for n in ("a", "b", "c")]
    assert all(t.status == "done" for t in tasks), [t.message for t in tasks]
    for t in tasks:                         # never more than 2 at the same moment
        assert sum(1 for o in tasks if o.started_at <= t.started_at < o.finished_at) <= 2


def test_cancelling_a_queued_task_creates_nothing(tmp_path):
    work, origin = make_repo(tmp_path)
    gates = tmp_path / "gates"
    store, r = _runner(tmp_path, {"x/demo": work}, gates)
    r.submit(_task("t-run"))
    _wait_status(store, "t-run", "running")
    r.submit(_task("t-q"))
    assert r.can_cancel("t-q")
    assert r.cancel("t-q")
    q = store.get("t-q")
    assert q.status == "cancelled" and q.finished_at and q.started_at is None
    assert "no worktree or branch" in q.message
    assert not r.can_cancel("t-q") and not r.cancel("t-q") and not r.set_priority("t-q", "high")
    _open(gates)
    assert _wait_done(store, "t-run").status == "done"
    time.sleep(0.2)
    assert store.get("t-q").status == "cancelled"       # it never started afterwards
    assert not (store.dir / "worktrees" / "demo-t-q").exists()
    for repo in (work, origin):
        assert subprocess.run(["git", "-C", str(repo), "branch", "--list", "baton/t-q"],
                              capture_output=True, text=True, check=True).stdout.strip() == ""


def test_restart_keeps_the_queue_and_fails_running_tasks(tmp_path):
    work, _ = make_repo(tmp_path)
    gates = tmp_path / "gates"
    store, _ = _runner(tmp_path, {"x/demo": work}, gates)
    store.save(_task("t-was-running", created=1.0))
    old = store.get("t-was-running")
    old.status = "running"
    store.save(old)
    for tid, prio, created in (("t-low", "low", 2.0), ("t-high", "high", 3.0)):
        t = _task(tid, priority=prio, created=created)
        t.status = "queued"
        store.save(t)
    store.save(Task("t-cloud", "x/demo", "Cloud", "", "claude-code", "cloud", 4.0, status="sent"))

    # A new Runner = Baton after a restart.
    store, r = _runner(tmp_path, {"x/demo": work}, gates)
    r.recover()
    failed = store.get("t-was-running")
    assert failed.status == "failed" and "Baton stopped" in failed.message
    assert [t.id for t in r.queued()] == ["t-high", "t-low"]
    assert store.get("t-cloud").status == "sent"
    _open(gates)
    r.dispatch()
    high, low = _wait_done(store, "t-high"), _wait_done(store, "t-low")
    assert high.status == low.status == "done"
    assert high.finished_at <= low.started_at


# ----------------------------------------------------------------- web pages
def _web(tmp_path, monkeypatch, work, gates, **settings):
    from fastapi.testclient import TestClient
    from baton import github, web
    from baton.config import Settings
    from baton.github import Project
    from baton.status import parse

    st = parse("---\nname: Demo\nstatus: active\nsummary: x\nnext: y\nupdated: 2026-11-23\n---\n")
    proj = Project("x/demo", "x/demo", "STATUS.md", "main", False, "https://github.com/x/demo", None, st)

    async def fake_load(self, force=False):
        return [proj], []

    def fake_pr(self, repo, branch, base, title, body, draft=True):
        return "https://github.com/x/demo/pull/1"

    agent_cmd = _gated_cmd(gates)

    class GatedRunner(Runner):
        def agents_available(self):
            return {a: "" for a in runner_mod.AGENTS}

        def _agent_cmd(self, task, wt):
            return agent_cmd(task, wt)

    monkeypatch.setattr(github.Hub, "load", fake_load)
    monkeypatch.setattr(github.Hub, "open_pr", fake_pr)
    monkeypatch.setattr(web, "Runner", GatedRunner)
    return TestClient(web.create_app(Settings(github_token="t", owner="x", data_dir=tmp_path / "data",
                                              codex_home=tmp_path, local_paths=f"x/demo={work}",
                                              auto_merge=False, **settings)))


def test_web_queue_priority_and_cancel(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    gates = tmp_path / "gates"
    c = _web(tmp_path, monkeypatch, work, gates)
    form = c.get("/tasks/new?repo=x/demo").text
    assert 'name="priority"' in form and "at most 3 local tasks" in form
    token = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    store = TaskStore(tmp_path / "data")

    def create(title, priority=None):
        data = {"repo": "x/demo", "where": "local:codex", "title": title, "csrf": token}
        if priority:
            data["priority"] = priority
        r = c.post("/tasks", data=data, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/tasks/2"), r.headers
        return r.headers["location"].rsplit("/", 1)[1]

    first = create("First")
    _wait_status(store, first, "running")
    # Creating a task while another runs in the repo is no longer an error.
    second = create("Second", "low")
    assert store.get(second).status == "queued" and store.get(second).priority == "low"
    page = c.get(f"/tasks/{second}").text
    assert "queued, 1st" in page and f'href="/tasks/{first}"' in page and "Waiting for" in page
    assert f'action="/tasks/{second}/priority"' in page and f'action="/tasks/{second}/cancel"' in page
    assert "queued, 1st" in c.get("/tasks").text

    # Changing the priority: same CSRF protection as creating a task.
    url = f"/tasks/{second}/priority"
    assert c.post(url, data={"priority": "high"}).status_code == 403
    assert c.post(url, data={"priority": "high", "csrf": "bad"}).status_code == 403
    assert c.post(url, data={"priority": "high", "csrf": token},
                  headers={"origin": "https://evil.example"}).status_code == 403
    assert c.post(url, data={"priority": "urgent", "csrf": token}).status_code == 400
    r = c.post(url, data={"priority": "high", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and store.get(second).priority == "high"
    assert c.post(f"/tasks/{first}/priority", data={"priority": "low", "csrf": token}).status_code == 409
    assert c.post("/tasks/nope/priority", data={"priority": "low", "csrf": token}).status_code == 404

    third = create("Third")                 # normal: behind the (now high) second
    assert "queued, 2nd" in c.get(f"/tasks/{third}").text

    # Cancelling the queued one: out of the queue, nothing created.
    r = c.post(f"/tasks/{second}/cancel", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    assert store.get(second).status == "cancelled"
    assert "queued, 1st" in c.get(f"/tasks/{third}").text
    assert "/cancel" not in c.get(f"/tasks/{second}").text
    _open(gates)
    assert _wait_done(store, first).status == "done"
    assert _wait_done(store, third).status == "done"
    assert store.get(second).started_at is None
    assert subprocess.run(["git", "-C", str(work), "branch", "--list", f"baton/{second}"],
                          capture_output=True, text=True, check=True).stdout.strip() == ""
    assert not (store.dir / "worktrees" / f"demo-{second}").exists()


def test_web_restart_keeps_queued_tasks(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    store = TaskStore(tmp_path / "data")
    running = _task("20260101-000000-running", created=1.0)
    running.status = "running"
    store.save(running)
    for tid, prio, created in (("20260101-000001-low", "low", 2.0),
                               ("20260101-000002-high", "high", 3.0)):
        t = _task(tid, priority=prio, created=created)
        t.status = "queued"
        store.save(t)
    c = _web(tmp_path, monkeypatch, work, tmp_path / "gates", lang="es")
    assert store.get("20260101-000000-running").status == "failed"
    assert store.get("20260101-000001-low").status == "queued"
    html = c.get("/tasks").text
    assert "en cola, 1.ª" in html and "en cola, 2.ª" in html
    assert html.index("Task 20260101-000002-high") > 0
    assert "en cola, 2.ª" in c.get("/tasks/20260101-000001-low").text
    # When the server starts (lifespan), the saved queue runs in its order.
    _open(tmp_path / "gates")
    with c:
        high = _wait_done(store, "20260101-000002-high")
        low = _wait_done(store, "20260101-000001-low")
    assert high.status == low.status == "done" and high.finished_at <= low.started_at
