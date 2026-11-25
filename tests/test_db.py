"""Tests for Phase 3B unified SQLite WAL storage and legacy migration."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing

from baton import db
from baton.ranking import Ranking
from baton.task_store import Task, TaskStore


def test_unified_db_uses_wal_busy_timeout_and_initializes_once(tmp_path):
    before = db._schema_init_count
    store = TaskStore(tmp_path)
    task = Task("t1", "x/demo", "Title", "Details", "codex", "local", time.time(), status="queued")
    store.save(task)

    assert (tmp_path / "baton.sqlite3").is_file()
    assert not (tmp_path / "tasks.json").exists()
    assert not (tmp_path / "ranking.sqlite3").exists()

    with db.connect(tmp_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        fks = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert str(mode).lower() == "wal"
    assert timeout == db.BUSY_TIMEOUT_MS
    assert fks == 1

    # Multiple subsequent calls on the same database file must not re-run schema init.
    init_after_first = db._schema_init_count
    assert init_after_first == before + 1
    assert store.get("t1").title == "Title"
    assert len(store.all()) == 1
    store.ranking.start("opencode-go/glm-5.3", "check", "prompt")
    store.ranking.overview()
    assert db._schema_init_count == init_after_first


def test_automatic_idempotent_migration_from_legacy_files(tmp_path):
    # 1. Create a legacy ranking.sqlite3 database with a job, a review, and a task_job.
    legacy_ranking = tmp_path / "ranking.sqlite3"
    with closing(sqlite3.connect(legacy_ranking)) as old, old:
        old.execute("""CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, model TEXT NOT NULL, started REAL NOT NULL,
            kind TEXT NOT NULL, prompt TEXT NOT NULL, outcome TEXT, result TEXT)""")
        old.execute("""CREATE TABLE reviews (
            job INTEGER NOT NULL, role TEXT NOT NULL, reviewer TEXT NOT NULL,
            fulfillment INTEGER NOT NULL, correctness INTEGER NOT NULL,
            rework INTEGER NOT NULL, comment TEXT NOT NULL, PRIMARY KEY(job, role))""")
        old.execute("""CREATE TABLE task_jobs (
            task_id TEXT PRIMARY KEY, job INTEGER UNIQUE NOT NULL,
            title TEXT NOT NULL, merged INTEGER NOT NULL, report TEXT NOT NULL)""")
        old.execute("INSERT INTO jobs VALUES (101, 'opencode-go/glm-5.3', 100.0, 'summary', 'P', 'done', 'R')")
        old.execute("INSERT INTO reviews VALUES (101, 'human', 'human', 9, 8, 10, 'Solid work')")
        old.execute("INSERT INTO jobs VALUES (102, 'claude-code/default', 200.0, 'Baton task', 'T', 'done', 'OK')")
        old.execute("INSERT INTO task_jobs VALUES ('old-1', 102, 'Legacy task', 1, 'Verdict: APPROVE')")

    # 2. Create a legacy tasks.json with a minimal record (missing optional fields) + one bad row.
    legacy_tasks = tmp_path / "tasks.json"
    legacy_payload = [
        {"id": "old-1", "repo": "x/demo", "title": "Legacy task", "details": "",
         "agent": "claude-code", "where": "local", "created_at": 200.0, "status": "done", "merged": True},
        {"id": "broken-row-missing-required-fields"},
    ]
    legacy_tasks.write_text(json.dumps(legacy_payload), encoding="utf-8")

    # 3. Opening TaskStore/Ranking migrates both files into baton.sqlite3 automatically.
    store = TaskStore(tmp_path)
    tasks = store.all()
    assert len(tasks) == 1
    assert tasks[0].id == "old-1" and tasks[0].merged is True and tasks[0].summary == ""

    models, jobs = store.ranking.overview("all")
    assert {j["id"] for j in jobs} == {101, 102}
    details, reviews = store.ranking.detail(101)
    assert details["model"] == "opencode-go/glm-5.3"
    assert reviews["human"]["comment"] == "Solid work"

    # 4. Legacy files remain untouched as backup, and updates in SQLite are NOT overwritten on reopen.
    assert legacy_ranking.is_file()
    assert legacy_tasks.is_file()
    updated = store.get("old-1")
    updated.summary = "Updated in SQLite"
    store.save(updated)

    reopened = TaskStore(tmp_path)
    assert reopened.get("old-1").summary == "Updated in SQLite"
    assert len(reopened.ranking.overview("all")[1]) == 2


def test_concurrent_readers_and_writers_in_wal_mode(tmp_path):
    store = TaskStore(tmp_path)
    errors: list[Exception] = []

    def writer(worker: int) -> None:
        try:
            for i in range(15):
                tid = f"w{worker}-{i}"
                task = Task(tid, "x/demo", f"Task {tid}", "", "codex", "local", time.time(), status="done")
                store.save(task)
        except Exception as exc:
            errors.append(exc)

    def reader() -> None:
        try:
            for _ in range(30):
                store.all()
                store.ranking.overview()
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(0,)),
        threading.Thread(target=writer, args=(1,)),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30.0)

    assert not errors
    assert len(store.all()) == 30
    assert len(store.ranking.overview("task")[1]) == 30


def test_worktree_env_collects_sandbox_wal_delegations(tmp_path):
    main_dir = tmp_path / "main-data"
    wt = tmp_path / "worktree"
    wt.mkdir()
    ranking = Ranking(main_dir)

    with ranking.worktree_env({}, wt, "claude-code") as env:
        sandbox = Ranking(env["BATON_DELEGATION_DIR"])
        job_id = sandbox.start("opencode-go/glm-5.3", "check", "Inspect diff")
        sandbox.finish(job_id, "ok", "All good")
        sandbox.review(job_id, "claude-code", 9, 9, 10, "Clean")

    models, jobs = ranking.overview("delegation")
    assert len(jobs) == 1 and jobs[0]["id"] == job_id
    details, reviews = ranking.detail(job_id)
    assert details["outcome"] == "ok"
    assert reviews["agent"]["comment"] == "Clean"


def test_save_commits_task_even_if_ranking_savepoint_fails(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)

    def broken_record(task, conn=None):
        raise sqlite3.OperationalError("simulated ranking error")

    monkeypatch.setattr(store.ranking, "record_task", broken_record)
    task = Task("t-safe", "x/demo", "Resilient", "", "codex", "local", time.time(), status="done")
    store.save(task)

    saved = store.get("t-safe")
    assert saved is not None and saved.status == "done"
    assert store.ranking.overview("task")[1] == []

