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
    assert _remote_repo("https://github.com/Marcobm1/Cairn.git") == "marcobm1/cairn"
    assert _remote_repo("git@github.com:Marcobm1/baton.git\n") == "marcobm1/baton"
    assert _remote_repo("https://example.com/x/y") == ""


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
    script = "import sys; sys.stdin.read(); open('DONE.md','w').write('agent was here\\n'); print('summary: done')"
    store, r = _fake_agent_runner(tmp_path, work, script, opened)
    task = Task(id="t1", repo="x/demo", title="Add DONE", details="please", agent="codex", where="local",
                created_at=time.time(), branch="baton/t1", base="main", open_pr=True)
    r.start(task)
    done = _wait(store, "t1")
    assert done.status == "done", done.message
    assert done.pr_url.endswith("/pull/1") and opened[0][1] == "baton/t1"
    # The owner's folder is untouched; the branch with the commit is on origin.
    assert not (work / "DONE.md").exists()
    files = subprocess.run(["git", "-C", str(origin), "show", "--name-only", "--format=%s", "baton/t1"],
                           capture_output=True, text=True, check=True).stdout
    assert "Add DONE" in files and "DONE.md" in files
    assert "summary: done" in store.log_tail("t1")
    assert done.worktree == ""


def test_runner_no_changes_and_failure(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    store, r = _fake_agent_runner(tmp_path, work, "import sys; sys.stdin.read()", [])
    r.start(Task(id="t2", repo="x/demo", title="Nothing", details="", agent="codex", where="local",
                 created_at=time.time(), branch="baton/t2", base="main"))
    assert _wait(store, "t2").status == "no-changes"
    r._agent_cmd = lambda task, wt: [sys.executable, "-c", "import sys; sys.exit(3)"]
    r.start(Task(id="t3", repo="x/demo", title="Boom", details="", agent="codex", where="local",
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
