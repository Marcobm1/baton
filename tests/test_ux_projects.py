"""UX-2 tests: Projects overview filters, Needs attention tray, deliveries cards, and automatic git diff stats."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from baton import github
from baton.config import Settings
from baton.db import connect as db_connect, db_path
from baton.project_detail import summary_headline
from baton.runner import Task, TaskStore
from baton.status import parse
from baton.web import create_app
from baton.worktree import parse_numstat


def test_parse_numstat_normal_binary_and_empty():
    assert parse_numstat("") is None
    raw = "12\t3\tbaton/web.py\n-\t-\tassets/shot.png\n5\t0\ttests/test_new.py\n"
    stat = parse_numstat(raw)
    assert stat == {
        "files": 3,
        "insertions": 17,
        "deletions": 3,
        "paths": [
            {"path": "baton/web.py", "ins": 12, "del": 3, "binary": False},
            {"path": "assets/shot.png", "ins": 0, "del": 0, "binary": True},
            {"path": "tests/test_new.py", "ins": 5, "del": 0, "binary": False},
        ],
    }


def test_summary_headline_prefers_summary_prefix():
    assert summary_headline("Summary: Added diff stats cleanly.\nChecks: pytest -q") == "Added diff stats cleanly."
    assert summary_headline("## Report\n- Fixed the race condition\n") == "Report"
    assert summary_headline("") == ""


def test_db_migrates_existing_tasks_table_adding_diff_stat(tmp_path):
    """Existing baton.sqlite3 files created before UX-2 gain diff_stat transparently."""
    path = db_path(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE tasks (
            id TEXT PRIMARY KEY, repo TEXT NOT NULL, title TEXT NOT NULL,
            details TEXT NOT NULL, agent TEXT NOT NULL, where_kind TEXT NOT NULL,
            created_at REAL NOT NULL, status TEXT NOT NULL, branch TEXT NOT NULL,
            base TEXT NOT NULL, worktree TEXT NOT NULL, pr_url TEXT NOT NULL,
            issue_url TEXT NOT NULL, message TEXT NOT NULL, finished_at REAL,
            open_pr INTEGER NOT NULL, model TEXT NOT NULL, steps TEXT NOT NULL,
            effort TEXT NOT NULL, summary TEXT NOT NULL, issue_number INTEGER,
            reviewer TEXT NOT NULL, review TEXT NOT NULL, reviewer_model TEXT NOT NULL,
            reviewer_effort TEXT NOT NULL, merged INTEGER NOT NULL, priority TEXT NOT NULL,
            started_at REAL, review_rating TEXT
        )"""
    )
    conn.commit()
    conn.close()

    with db_connect(tmp_path) as upgraded:
        cols = {row[1] for row in upgraded.execute("PRAGMA table_info(tasks)")}
    assert "diff_stat" in cols

    store = TaskStore(tmp_path)
    diff = {"files": 2, "insertions": 40, "deletions": 10, "paths": [{"path": "a.py", "ins": 40, "del": 10, "binary": False}]}
    store.save(Task("t1", "u/demo", "Refactor", "", "codex", "local", 1.0, status="done", finished_at=2.0, diff_stat=diff))
    loaded = store.get("t1")
    assert loaded is not None and loaded.diff_stat == diff


def test_projects_filters_attention_tray_and_delivery_cards(tmp_path, monkeypatch):
    active_p = github.Project(
        "u/app", "u/app", "STATUS.md", "main", False, "https://github.com/u/app", None,
        parse(
            "---\nname: App\nstatus: active\npriority: high\nupdated: 2026-11-26\n---\n"
            "## Next\n- [ ] (Owner) Check release\n- [ ] Add dark mode\n"
        ),
    )
    paused_p = github.Project(
        "u/old", "u/old", "STATUS.md", "main", False, "https://github.com/u/old", None,
        parse("---\nname: Old\nstatus: paused\npriority: low\nupdated: 2026-11-26\n---\n"),
    )

    async def fake_load(self, force=False):
        return [active_p, paused_p], []

    async def no_pulls(self, repo):
        return []

    monkeypatch.setattr(github.Hub, "load", fake_load)
    monkeypatch.setattr(github.Hub, "open_pulls", no_pulls)

    store = TaskStore(tmp_path)
    diff = {
        "files": 2,
        "insertions": 85,
        "deletions": 12,
        "paths": [
            {"path": "baton/web.py", "ins": 70, "del": 10, "binary": False},
            {"path": "tests/test_web.py", "ins": 15, "del": 2, "binary": False},
        ],
    }
    store.save(Task(
        "t-ok", "u/app", "Polish dashboard", "", "gemini", "local", 10.0,
        status="done", finished_at=20.0, merged=False,
        pr_url="https://github.com/u/app/pull/12",
        summary="Summary: Cleaned up project cards.\nChecks: pytest -q (all green)",
        diff_stat=diff,
    ))
    store.save(Task(
        "t-fail", "u/old", "Broken migration", "", "codex", "local", 15.0,
        status="failed", finished_at=25.0,
    ))

    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es", self_update=False)))

    # Overview shows Needs attention tray (owner action + open PR + failed task), live activity, and diff stat.
    home = client.get("/").text
    assert "Requiere tu atención" in home
    assert "Check release" in home
    assert "Broken migration" in home
    assert "+85" in home and "-12" in home
    assert "App" in home and "Old" in home

    # Status filter isolates active projects cleanly.
    only_active = client.get("/?status=active").text
    assert 'href="/p/u/app"' in only_active
    assert 'href="/p/u/old"' not in only_active

    # Project Deliveries tab renders structured delivery card with diff stats and changed files.
    done_html = client.get("/p/u/app?tab=done").text
    assert "delivery-card" in done_html
    assert "Cleaned up project cards." in done_html
    assert "Archivos modificados (2)" in done_html
    assert "baton/web.py" in done_html

    # Task detail and list also display diff stats and changed files.
    task_html = client.get("/tasks/t-ok").text
    assert "+85" in task_html and "-12" in task_html and "baton/web.py" in task_html
    tasks_html = client.get("/tasks").text
    assert "+85" in tasks_html and "-12" in tasks_html


def test_runner_captures_git_diff_stat_on_commit(tmp_path, monkeypatch):
    import subprocess
    import sys

    from baton import runner as runner_mod
    from baton.runner import LocalRepos, Runner

    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    origin.mkdir()
    work.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "Test")
    (work / "hello.txt").write_text("line1\nline2\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "-u", "origin", "main")

    # Fake CLI that edits hello.txt and creates a new file inside the worktree.
    script = tmp_path / "fake_agent.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('hello.txt').write_text('line1\\nupdated\\nline3\\n', encoding='utf-8')\n"
        "Path('added.py').write_text('x = 1\\ny = 2\\n', encoding='utf-8')\n"
        "print('BATON_SUMMARY_BEGIN\\nSummary: Updated hello and added module.\\nChecks: ok\\nBATON_SUMMARY_END')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        runner_mod, "build_agent_cmd",
        lambda *a, **kw: [sys.executable, str(script)],
    )

    store = TaskStore(tmp_path / "data")
    repos = LocalRepos("", f"u/demo={work}")
    r = Runner(
        store, repos, timeout=60, local_model="", local_provider="ollama",
        git_name="Baton", git_email="baton@localhost", open_pr=lambda *_: "",
    )
    task = Task(
        "t-diff", "u/demo", "Edit files", "", "codex", "local", 1.0,
        status="queued", branch="baton/t-diff", base="main", open_pr=False,
    )
    store.save(task)
    r._run(task)
    store.save(task)

    saved = store.get("t-diff")
    assert saved is not None and saved.status == "done"
    assert saved.diff_stat is not None
    assert saved.diff_stat["files"] == 2
    assert saved.diff_stat["insertions"] == 4
    assert saved.diff_stat["deletions"] == 1
    names = {p["path"] for p in saved.diff_stat["paths"]}
    assert names == {"hello.txt", "added.py"}

