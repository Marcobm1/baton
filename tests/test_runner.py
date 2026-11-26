import json
import subprocess
import sys
import time
from pathlib import Path

from baton import runner as runner_mod
from baton.runner import LocalRepos, Runner, Task, TaskStore, _remote_repo


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "root" / "demo"
    work.mkdir(parents=True)
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    git(work, "config", "commit.gpgsign", "false")
    (work / "README.md").write_text("hi\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "-u", "origin", "main")
    return work, origin


def test_remote_repo_parsing():
    assert _remote_repo("https://github.com/Owner/Demo.git") == "owner/demo"
    assert _remote_repo("git@github.com:Owner/baton.git\n") == "owner/baton"
    assert _remote_repo("https://example.com/x/y") == ""


def test_task_summary_page_and_legacy_history(tmp_path):
    from fastapi.testclient import TestClient
    from baton.config import Settings
    from baton.web import create_app

    # A legacy tasks.json record saved before summaries existed migrates cleanly.
    (tmp_path / "tasks.json").write_text(json.dumps([dict(
        id="test", repo="x/demo", title="Test", details="", agent="codex",
        where="local", created_at=0, status="no-changes", finished_at=1.0,
    )]), encoding="utf-8")
    store = TaskStore(tmp_path)
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es")))
    assert "No se guardó un resumen" in client.get("/tasks/test").text
    task = store.get("test")
    assert task is not None
    task.summary = "Checked **both paths**. <script>alert(1)</script>"
    store.save(task)
    html = client.get("/tasks/test").text
    assert "Resumen de la tarea" in html and "<strong>both paths</strong>" in html
    assert "<script>alert(1)</script>" not in html


def test_summary_request_asks_for_problems_and_what_to_know():
    req = runner_mod.SUMMARY_REQUEST
    assert "problems" in req and "needs to know" in req and "Checks:" in req


def test_local_repos_prefers_named_folder(tmp_path, monkeypatch):
    root = tmp_path / "root"
    for name in ("Mamporro-respaldo-u2", "Mamporro-git", "Other"):
        (root / name / ".git").mkdir(parents=True)
    urls = {"Mamporro-respaldo-u2": "https://github.com/M/Mamporro.git",
            "Mamporro-git": "https://github.com/M/Mamporro.git", "Other": ""}

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=urls[Path(cmd[2]).name], stderr="")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    repos = LocalRepos(str(root), "x/y=C:/somewhere")
    assert repos.get("M/Mamporro").name == "Mamporro-git"
    assert str(repos.get("x/y")) .endswith("somewhere")
    assert repos.get("nobody/none") is None


def _fake_agent_runner(tmp_path, work, script, opened):
    store = TaskStore(tmp_path / "data")
    r = Runner(store, LocalRepos(str(work.parent), ""), timeout=60, local_model="", local_provider="ollama",
               git_name="Baton", git_email="baton@localhost",
               open_pr=lambda *a: opened.append(a) or "https://github.com/x/demo/pull/1")
    r._agent_cmd = lambda task, wt: [sys.executable, "-c", script]
    return store, r


def _wait(store, task_id):
    for _ in range(200):
        t = store.get(task_id)
        if t and t.status not in ("queued", "running"):
            return t
        time.sleep(0.05)
    raise AssertionError("task did not finish")


def test_runner_end_to_end_in_a_worktree(tmp_path, monkeypatch):
    work, origin = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    opened = []
    script = ("import sys; sys.stdin.read(); open('DONE.md','w').write('agent was here\\n'); "
              "print('BATON_SUMMARY_BEGIN\\nAdded DONE.md to record completion.\\nBATON_SUMMARY_END')")
    store, r = _fake_agent_runner(tmp_path, work, script, opened)
    task = Task(id="t1", repo="x/demo", title="Add DONE", details="please", agent="codex", where="local",
                created_at=time.time(), branch="baton/t1", base="main", open_pr=True)
    assert runner_mod.NEXT_TASK_REQUEST in r._prompt(task)
    r.submit(task)
    done = _wait(store, "t1")
    assert done.status == "done", done.message
    assert done.pr_url.endswith("/pull/1") and opened[0][1] == "baton/t1"
    # The owner's folder is untouched; the branch with the commit is on origin.
    assert not (work / "DONE.md").exists()
    files = subprocess.run(["git", "-C", str(origin), "show", "--name-only", "--format=%s", "baton/t1"],
                           capture_output=True, text=True, check=True).stdout
    assert "Add DONE" in files and "DONE.md" in files
    assert done.summary == "Added DONE.md to record completion."
    assert done.worktree == ""


def test_summary_ignores_instructions_and_incomplete_blocks():
    assert runner_mod.extract_summary(runner_mod.SUMMARY_REQUEST) == ""
    assert runner_mod.extract_summary("BATON_SUMMARY_BEGIN\nunfinished") == ""
    assert runner_mod.extract_summary("trace\n\x1b[32mBATON_SUMMARY_BEGIN\x1b[0m\n"
                                      "one\n\ntwo\nBATON_SUMMARY_END\ntrace") == "one\n\ntwo"


def test_runner_no_changes_and_failure(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    script = "print('BATON_SUMMARY_BEGIN\\nThe setting was already enabled.\\nBATON_SUMMARY_END')"
    store, r = _fake_agent_runner(tmp_path, work, script, [])
    r.submit(Task(id="t2", repo="x/demo", title="Nothing", details="", agent="codex", where="local",
                 created_at=time.time(), branch="baton/t2", base="main"))
    done = _wait(store, "t2")
    assert done.status == "no-changes"
    assert done.summary == "The setting was already enabled."
    r._agent_cmd = lambda task, wt: [sys.executable, "-c", "import sys; sys.exit(3)"]
    r.submit(Task(id="t3", repo="x/demo", title="Boom", details="", agent="codex", where="local",
                 created_at=time.time(), branch="baton/t3", base="main"))
    failed = _wait(store, "t3")
    assert failed.status == "failed" and "exit code 3" in failed.message


def test_opencode_is_launched_without_a_shell(tmp_path, monkeypatch):
    # npm's wrapper is a .cmd; Baton must call the real exe next to it.
    npm = tmp_path / "npm"
    exe = npm / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    (npm / "opencode.cmd").write_text("")
    monkeypatch.delenv("BATON_OPENCODE_BIN", raising=False)
    monkeypatch.setattr(runner_mod.shutil, "which", lambda name: str(npm / "opencode.cmd"))
    assert runner_mod.find_opencode() == str(exe)

    store = TaskStore(tmp_path / "data")
    r = Runner(store, LocalRepos("", ""), timeout=60, local_model="", local_provider="ollama",
               git_name="B", git_email="b@x", open_pr=lambda *a: "")
    task = Task(id="t9", repo="x/demo", title="A & B | C", details="line1\nline2", agent="opencode",
                where="local", created_at=0, branch="baton/t9", base="main", model="opencode/big-pickle")
    cmd = r._agent_cmd(task, tmp_path)
    assert cmd[0] == str(exe) and "--agent" in cmd and cmd[cmd.index("-m") + 1] == "opencode/big-pickle"
    assert cmd[-1].startswith("You are working on the task") and "A & B | C" in cmd[-1]
    rules = runner_mod.OPENCODE_CONFIG["agents"]["baton"]["permissions"]
    assert rules[0] == {"action": "*", "resource": "*", "effect": "deny"}  # everything else denied
    assert {"action": "shell", "resource": "git push*", "effect": "deny"} in rules


def _sleepy_agent(beat: Path) -> str:
    """A fake agent that leaves half-done work, starts a child process that
    writes a heartbeat to `beat` (outside the worktree), and then sleeps."""
    child = f"import time\nwhile True:\n    open({str(beat)!r}, 'a').write('.')\n    time.sleep(0.05)\n"
    return ("import subprocess, sys, time\n"
            "open('HALF.md', 'w').write('half done')\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "time.sleep(120)\n")


def _wait_for_heartbeat(beat: Path) -> None:
    for _ in range(300):
        if beat.exists() and beat.stat().st_size > 2:
            return
        time.sleep(0.05)
    raise AssertionError("the fake agent did not start")


def _assert_cancelled_cleanly(store, task_id, work, origin, beat, started):
    t = store.get(task_id)
    assert t.status == "cancelled", t.message
    assert t.finished_at and t.finished_at >= started
    assert "Nothing was committed or pushed" in t.message and t.worktree == ""
    # The agent's child process is dead too: its heartbeat has stopped.
    time.sleep(0.3)
    size = beat.stat().st_size
    time.sleep(0.5)
    assert beat.stat().st_size == size
    # No worktree, no local branch, nothing on GitHub (origin).
    assert not (store.dir / "worktrees" / f"demo-{task_id}").exists()
    local = subprocess.run(["git", "-C", str(work), "branch", "--list", f"baton/{task_id}"],
                           capture_output=True, text=True, check=True).stdout
    remote = subprocess.run(["git", "-C", str(origin), "branch", "--list", f"baton/{task_id}"],
                            capture_output=True, text=True, check=True).stdout
    assert local.strip() == "" and remote.strip() == ""
    assert not (work / "HALF.md").exists()


def test_cancel_stops_the_agent_and_its_children(tmp_path, monkeypatch):
    work, origin = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    beat = tmp_path / "beat.txt"
    opened = []
    store, r = _fake_agent_runner(tmp_path, work, _sleepy_agent(beat), opened)
    r.submit(Task(id="t4", repo="x/demo", title="Sleep", details="", agent="codex", where="local",
                 created_at=time.time(), branch="baton/t4", base="main"))
    _wait_for_heartbeat(beat)
    assert r.can_cancel("t4") and store.get("t4").status == "running"
    started = time.time()
    assert r.cancel("t4")
    _assert_cancelled_cleanly(store, "t4", work, origin, beat, started)
    assert opened == []                      # no pull request
    assert not r.can_cancel("t4") and not r.cancel("t4")
    assert "Cancelled" in store.log_tail("t4")


def test_cancel_from_the_task_page(tmp_path, monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from baton import github, web
    from baton.config import Settings
    from baton.github import Project
    from baton.status import parse

    work, origin = make_repo(tmp_path)
    beat = tmp_path / "beat.txt"
    st = parse("---\nname: Demo\nstatus: active\nsummary: x\nnext: y\nupdated: 2026-11-23\n---\n")
    proj = Project("x/demo", "x/demo", "STATUS.md", "main", False, "https://github.com/x/demo", None, st)

    async def fake_load(self, force=False):
        return [proj], []

    class SleepyRunner(Runner):
        def agents_available(self):
            return {a: "" for a in runner_mod.AGENTS}

        def _agent_cmd(self, task, wt):
            return [sys.executable, "-c", _sleepy_agent(beat)]

    monkeypatch.setattr(github.Hub, "load", fake_load)
    monkeypatch.setattr(web, "Runner", SleepyRunner)
    c = TestClient(web.create_app(Settings(github_token="t", owner="x", data_dir=tmp_path / "data",
                                           codex_home=tmp_path, local_paths=f"x/demo={work}")))
    token = re.search(r'name="csrf" value="([^"]+)"', c.get("/tasks/new?repo=x/demo").text).group(1)
    r = c.post("/tasks", data={"repo": "x/demo", "where": "local:codex", "title": "Sleep", "csrf": token},
               follow_redirects=False)
    assert r.status_code == 303
    url = r.headers["location"]
    task_id = url.rsplit("/", 1)[1]
    _wait_for_heartbeat(beat)
    assert f'action="/tasks/{task_id}/cancel"' in c.get(url).text

    # Without the form token (or with a wrong one, or from another site) it is refused.
    assert c.post(f"{url}/cancel", data={}).status_code == 403
    assert c.post(f"{url}/cancel", data={"csrf": "bad"}).status_code == 403
    assert c.post(f"{url}/cancel", data={"csrf": token},
                  headers={"origin": "https://evil.example"}).status_code == 403
    assert c.get(url).text.count("/cancel") == 1   # still running: the button is still there

    started = time.time()
    r = c.post(f"{url}/cancel", data={"csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == url
    store = TaskStore(tmp_path / "data")
    _assert_cancelled_cleanly(store, task_id, work, origin, beat, started)
    page = c.get(url).text
    assert "cancelled" in page and "/cancel" not in page
    assert c.post(f"{url}/cancel", data={"csrf": token}).status_code == 409
    assert c.post("/tasks/nope/cancel", data={"csrf": token}).status_code == 404


def test_every_local_task_from_the_web_ends_in_a_pull_request(tmp_path, monkeypatch):
    """Claude Code, Codex and OpenCode tasks all open a PR; there is no opt-out."""
    import re
    from fastapi.testclient import TestClient
    from baton import github, web
    from baton.config import Settings
    from baton.github import Project
    from baton.status import parse

    work, origin = make_repo(tmp_path)
    st = parse("---\nname: Demo\nstatus: active\nsummary: x\nnext: y\nupdated: 2026-11-23\n---\n")
    proj = Project("x/demo", "x/demo", "STATUS.md", "main", False, "https://github.com/x/demo", None, st)
    opened = []

    async def fake_load(self, force=False):
        return [proj], []

    def fake_pr(self, repo, branch, base, title, body, draft=True):
        opened.append((repo, branch, base, title))
        return f"https://github.com/{repo}/pull/{len(opened)}"

    class QuickRunner(Runner):
        def agents_available(self):
            return {a: "" for a in runner_mod.AGENTS}

        def _agent_cmd(self, task, wt):
            script = f"open('{task.agent}.md', 'w').write('done')"
            return [sys.executable, "-c", script]

    monkeypatch.setattr(github.Hub, "load", fake_load)
    monkeypatch.setattr(github.Hub, "open_pr", fake_pr)
    monkeypatch.setattr(web, "Runner", QuickRunner)
    monkeypatch.setattr(web, "opencode_models", lambda: ["opencode/big-pickle"])
    c = TestClient(web.create_app(Settings(github_token="t", owner="x", data_dir=tmp_path / "data",
                                           codex_home=tmp_path, local_paths=f"x/demo={work}",
                                           auto_merge=False)))
    form_page = c.get("/tasks/new?repo=x/demo").text
    assert 'name="open_pr"' not in form_page            # nothing to untick
    token = re.search(r'name="csrf" value="([^"]+)"', form_page).group(1)
    store = TaskStore(tmp_path / "data")
    for agent in ("claude-code", "codex", "opencode"):
        r = c.post("/tasks", data={"repo": "x/demo", "where": f"local:{agent}", "title": f"Task {agent}",
                                   "oc_model": "opencode/big-pickle", "csrf": token},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        done = _wait(store, r.headers["location"].rsplit("/", 1)[1])
        assert done.status == "done", done.message
        assert done.open_pr and done.pr_url.startswith("https://github.com/x/demo/pull/")
    assert [o[3] for o in opened] == ["Task claude-code", "Task codex", "Task opencode"]
