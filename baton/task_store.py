"""Task data model, queue ordering, and SQLite-backed TaskStore."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .db import connect as db_connect, db_path, migrate_legacy_tasks
from .ranking import Ranking


class LeftOpen(Exception):
    """The pull request stays open for the owner; the message says why."""


class Cancelled(Exception):
    """Raised inside a task's thread when the owner cancelled it."""


class UpdateInProgress(Exception):
    """Task admission is closed until the self-update finishes."""


def pr_number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[1])


@dataclass
class Task:
    id: str
    repo: str
    title: str
    details: str
    agent: str
    where: str                      # "local" | "cloud"
    created_at: float
    status: str = "queued"          # queued | running | done | no-changes | failed | cancelled
    branch: str = ""
    base: str = ""
    worktree: str = ""
    pr_url: str = ""
    issue_url: str = ""
    message: str = ""
    finished_at: float | None = None
    open_pr: bool = True            # always True for new tasks; False only in old records
    model: str = ""
    steps: list[str] = field(default_factory=list)
    effort: str = ""
    summary: str = ""
    issue_number: int | None = None
    reviewer: str = ""              # agent that reviewed the work before merging
    review: str = ""                # its report
    reviewer_model: str = ""        # model the reviewer ran ("" = the CLI's default)
    reviewer_effort: str = ""       # its reasoning effort, if any
    merged: bool = False
    priority: str = "normal"        # high | normal | low: order in the queue
    started_at: float | None = None  # when it left the queue
    review_rating: dict | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("done", "no-changes", "failed", "cancelled") or (
            self.status == "sent" and bool(self.summary))


# Queue order: higher priority first, then the oldest. The id breaks the
# (very unlikely) tie of two tasks created at the same instant.
PRIORITIES = ("high", "normal", "low")


def queue_key(task: Task) -> tuple:
    rank = PRIORITIES.index(task.priority) if task.priority in PRIORITIES else 1
    return (rank, task.created_at, task.id)


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:30] or "task"


def new_id(title: str) -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + _slug(title)


def _task_from_row(row: sqlite3.Row) -> Task:
    try:
        steps = json.loads(row["steps"]) if row["steps"] else []
    except ValueError:
        steps = []
    try:
        rating = json.loads(row["review_rating"]) if row["review_rating"] else None
    except ValueError:
        rating = None
    return Task(
        id=row["id"],
        repo=row["repo"],
        title=row["title"],
        details=row["details"],
        agent=row["agent"],
        where=row["where_kind"],
        created_at=row["created_at"],
        status=row["status"],
        branch=row["branch"],
        base=row["base"],
        worktree=row["worktree"],
        pr_url=row["pr_url"],
        issue_url=row["issue_url"],
        message=row["message"],
        finished_at=row["finished_at"],
        open_pr=bool(row["open_pr"]),
        model=row["model"],
        steps=steps if isinstance(steps, list) else [],
        effort=row["effort"],
        summary=row["summary"],
        issue_number=row["issue_number"],
        reviewer=row["reviewer"],
        review=row["review"],
        reviewer_model=row["reviewer_model"],
        reviewer_effort=row["reviewer_effort"],
        merged=bool(row["merged"]),
        priority=row["priority"],
        started_at=row["started_at"],
        review_rating=rating if isinstance(rating, dict) else None,
    )


_UPSERT_SQL = """INSERT INTO tasks(
    id, repo, title, details, agent, where_kind, created_at, status, branch, base,
    worktree, pr_url, issue_url, message, finished_at, open_pr, model, steps, effort,
    summary, issue_number, reviewer, review, reviewer_model, reviewer_effort, merged,
    priority, started_at, review_rating
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    repo = excluded.repo,
    title = excluded.title,
    details = excluded.details,
    agent = excluded.agent,
    where_kind = excluded.where_kind,
    created_at = excluded.created_at,
    status = excluded.status,
    branch = excluded.branch,
    base = excluded.base,
    worktree = excluded.worktree,
    pr_url = excluded.pr_url,
    issue_url = excluded.issue_url,
    message = excluded.message,
    finished_at = excluded.finished_at,
    open_pr = excluded.open_pr,
    model = excluded.model,
    steps = excluded.steps,
    effort = excluded.effort,
    summary = excluded.summary,
    issue_number = excluded.issue_number,
    reviewer = excluded.reviewer,
    review = excluded.review,
    reviewer_model = excluded.reviewer_model,
    reviewer_effort = excluded.reviewer_effort,
    merged = excluded.merged,
    priority = excluded.priority,
    started_at = excluded.started_at,
    review_rating = excluded.review_rating"""


class TaskStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.db_path = db_path(self.dir)
        self.file = self.dir / "tasks.json"  # legacy path (migrated automatically if present)
        self.ranking = Ranking(self.dir)
        self.logs = self.dir / "tasks"
        self.lock = threading.Lock()
        self._legacy_migrated = False

    def _maybe_migrate(self, conn: sqlite3.Connection) -> None:
        if not self._legacy_migrated and self.file.is_file():
            migrate_legacy_tasks(self.dir, conn)
            self._legacy_migrated = True

    def all(self) -> list[Task]:
        with db_connect(self.dir) as conn:
            self._maybe_migrate(conn)
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
        return [_task_from_row(r) for r in rows]

    def get(self, task_id: str) -> Task | None:
        with db_connect(self.dir) as conn:
            self._maybe_migrate(conn)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_from_row(row) if row else None

    def save(self, task: Task) -> None:
        params = (
            task.id, task.repo, task.title, task.details, task.agent, task.where,
            task.created_at, task.status, task.branch, task.base, task.worktree,
            task.pr_url, task.issue_url, task.message, task.finished_at,
            1 if task.open_pr else 0, task.model, json.dumps(task.steps), task.effort,
            task.summary, task.issue_number, task.reviewer, task.review,
            task.reviewer_model, task.reviewer_effort, 1 if task.merged else 0,
            task.priority, task.started_at,
            json.dumps(task.review_rating) if task.review_rating is not None else None,
        )
        with self.lock:
            with db_connect(self.dir) as conn:
                self._maybe_migrate(conn)
                conn.execute(_UPSERT_SQL, params)
                try:
                    conn.execute("SAVEPOINT record_task")
                    self.ranking.record_task(task, conn=conn)
                    conn.execute("RELEASE SAVEPOINT record_task")
                except sqlite3.Error as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT record_task")
                    conn.execute("RELEASE SAVEPOINT record_task")
                    logging.getLogger(__name__).warning("Could not index task %s: %s", task.id, exc)

    def record_task(self, task: Task):
        try:
            return self.ranking.record_task(task)
        except sqlite3.Error as exc:
            logging.getLogger(__name__).warning("Could not index task %s: %s", task.id, exc)
            return None

    def log_path(self, task_id: str) -> Path:
        self.logs.mkdir(parents=True, exist_ok=True)
        return self.logs / f"{task_id}.log"

    def read_log(self, task_id: str) -> str:
        try:
            return self.log_path(task_id).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def log_tail(self, task_id: str, lines: int = 200) -> str:
        return "\n".join(self.read_log(task_id).splitlines()[-lines:])
