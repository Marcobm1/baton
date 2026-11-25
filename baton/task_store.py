"""Task data model, queue ordering, and persistent TaskStore."""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

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


class TaskStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self.file = data_dir / "tasks.json"
        self.ranking = Ranking(data_dir)
        self.logs = data_dir / "tasks"
        self.lock = threading.Lock()

    def all(self) -> list[Task]:
        # Reads take the lock too: on Windows, replacing tasks.json fails
        # ("Access denied") while another thread has it open for reading.
        with self.lock:
            return self._load()

    def _load(self) -> list[Task]:
        try:
            rows = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for r in rows:
            try:
                out.append(Task(**r))
            except TypeError:
                continue
        return sorted(out, key=lambda t: t.created_at, reverse=True)

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self.all() if t.id == task_id), None)

    def save(self, task: Task) -> None:
        with self.lock:
            tasks = {t.id: t for t in self._load()}
            tasks[task.id] = task
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(t) for t in tasks.values()], indent=1), encoding="utf-8")
            for attempt in range(20):
                try:
                    tmp.replace(self.file)
                    break
                except PermissionError:  # another program (antivirus, indexer) has it open
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
            self.record_task(task)

    def record_task(self, task: Task):
        try:
            return self.ranking.record_task(task)
        except sqlite3.Error as exc:
            # tasks.json is durable. Recovery retries without holding up the queue.
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
