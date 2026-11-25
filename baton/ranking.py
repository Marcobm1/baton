"""Local history and independent reviews of delegations and Baton tasks."""

import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .config import configured_data_dir

CRITERIA = ("fulfillment", "correctness", "rework")


def review_verdict(report: str) -> str:
    """"APPROVE", "FIXED" or "BLOCK" from the review's first Verdict line, else ""."""
    for line in report.splitlines():
        m = re.match(r"\s*\**\s*verdict\s*:?\s*\**\s*:?\s*([a-z]+)", line, re.I)
        if m:
            word = m.group(1).upper()
            return word if word in ("APPROVE", "FIXED", "BLOCK") else ""
    return ""


def historical_score(job):
    """A delivery estimate when the original reviewer supplied no scores."""
    verdict = review_verdict(job["report"] or "")
    if verdict:
        return {"APPROVE": 9, "FIXED": 7, "BLOCK": 3}[verdict], "score_" + verdict.lower()
    if job["merged"]:
        return 8, "score_merged"
    if job["outcome"] == "done":
        return 6, "score_done"
    if job["outcome"] in ("failed", "timeout"):
        return 2, "score_failed"
    return None, "insufficient"


def parse_rating(block):
    try:
        data = json.loads(block)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != {*CRITERIA, "comment"}:
        return None
    if any(type(data[k]) is not int or not 1 <= data[k] <= 10 for k in CRITERIA):
        return None
    comment = data["comment"]
    if not isinstance(comment, str) or not comment.strip() or len(comment) > 500:
        return None
    try:
        comment.encode("utf-8")  # SQLite requires valid Unicode text.
    except UnicodeEncodeError:
        return None
    return data


def cli_ranking():
    local = os.environ.get("BATON_DELEGATION_DIR")
    return Ranking(Path(local) if local else configured_data_dir())


class Ranking:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "ranking.sqlite3"

    @contextmanager
    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, model TEXT NOT NULL, started REAL NOT NULL,
                kind TEXT NOT NULL, prompt TEXT NOT NULL, outcome TEXT, result TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS reviews (
                job INTEGER NOT NULL REFERENCES jobs(id), role TEXT NOT NULL,
                reviewer TEXT NOT NULL, fulfillment INTEGER NOT NULL CHECK(fulfillment BETWEEN 1 AND 10),
                correctness INTEGER NOT NULL CHECK(correctness BETWEEN 1 AND 10),
                rework INTEGER NOT NULL CHECK(rework BETWEEN 1 AND 10),
                comment TEXT NOT NULL CHECK(length(comment)<=500), PRIMARY KEY(job, role))""")
            db.execute("""CREATE TABLE IF NOT EXISTS task_jobs (
                task_id TEXT PRIMARY KEY, job INTEGER UNIQUE NOT NULL REFERENCES jobs(id),
                title TEXT NOT NULL, merged INTEGER NOT NULL, report TEXT NOT NULL)""")
            yield db

    def start(self, model, kind, prompt):
        job = secrets.randbits(52) + 1  # JS Number-safe (< 2**53) and collision-free across worktrees.
        with self.database() as db:
            db.execute("INSERT INTO jobs(id, model, started, kind, prompt) VALUES (?, ?, ?, ?, ?)",
                       (job, model, time.time(), kind, prompt))
        return job

    def finish(self, job, outcome, result):
        with self.database() as db:
            db.execute("UPDATE jobs SET outcome=?, result=? WHERE id=?", (outcome, result, job))

    def review(self, job, reviewer, fulfillment, correctness, rework, comment):
        if reviewer not in ("human", "codex", "claude-code"):
            raise ValueError("Unknown reviewer.")
        if any(type(n) is not int or not 1 <= n <= 10 for n in (fulfillment, correctness, rework)):
            raise ValueError("Each score must be an integer from 1 to 10.")
        if len(comment) > 500:
            raise ValueError("Comments can contain up to 500 characters.")
        role = "human" if reviewer == "human" else "agent"
        with self.database() as db:
            if not db.execute("SELECT 1 FROM jobs WHERE id=?", (str(job),)).fetchone():
                raise KeyError(job)
            db.execute("""INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job, role) DO UPDATE SET reviewer=excluded.reviewer,
                fulfillment=excluded.fulfillment, correctness=excluded.correctness,
                rework=excluded.rework, comment=excluded.comment""",
                       (job, role, reviewer, fulfillment, correctness, rework, comment.strip()))

    def record_task(self, task):
        outcome = task.status
        if task.where == "cloud" and outcome == "sent" and task.summary:
            outcome = "done"
        if outcome not in ("done", "no-changes", "failed", "cancelled"):
            return None
        with self.database() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT job FROM task_jobs WHERE task_id=?", (task.id,)).fetchone()
            if existing:
                return existing["job"]
            prompt = "\n\n".join(filter(None, (task.title, task.details)))
            result = "\n\n".join(filter(None, (task.message, task.summary)))
            model = task.model or (f"{task.agent}/default" if task.agent else "default")
            job = db.execute("""INSERT INTO jobs(model, started, kind, prompt, outcome, result)
                VALUES (?, ?, 'Baton task', ?, ?, ?)""",
                (model, task.started_at or task.created_at, prompt, outcome, result)).lastrowid
            db.execute("INSERT INTO task_jobs VALUES (?, ?, ?, ?, ?)",
                       (task.id, job, task.title, task.merged, task.review))
            rating = task.review_rating
            if rating and task.reviewer in ("codex", "claude-code"):
                db.execute("INSERT INTO reviews VALUES (?, 'agent', ?, ?, ?, ?, ?)",
                           (job, task.reviewer, *(rating[k] for k in CRITERIA), rating["comment"]))
            return job

    def overview(self, origin="all"):
        # Existing delegation databases need no migration. Only Baton tasks have a link.
        origins = {"all": "1", "task": "task_id IS NOT NULL", "delegation": "task_id IS NULL"}
        source = """ FROM jobs LEFT JOIN task_jobs ON task_jobs.job=jobs.id
            LEFT JOIN reviews ON jobs.id=reviews.job WHERE """ + origins[origin]

        with self.database() as db:
            models = [dict(row) for row in db.execute("""SELECT model,
                AVG(fulfillment) AS fulfillment, AVG(correctness) AS correctness, AVG(rework) AS rework
                """ + source + " GROUP BY model")]
            jobs = [dict(row) for row in db.execute("""SELECT jobs.id, started, model, kind, outcome,
                task_id, title, merged, report,
                MAX(CASE WHEN role='human' THEN (fulfillment+correctness+rework)/3.0 END) AS human,
                MAX(CASE WHEN role='agent' THEN (fulfillment+correctness+rework)/3.0 END) AS agent
                """ + source + " GROUP BY jobs.id ORDER BY started DESC, jobs.id DESC")]
        by_model = {}
        for job in jobs:
            if job["agent"] is None:
                job["agent"], job["estimate"] = historical_score(job)
            by_model.setdefault(job["model"], []).append(job)
        for model in models:
            entries = by_model[model["model"]]
            model["evaluated"] = sum(j["agent"] is not None or j["human"] is not None for j in entries)
            for role in ("agent", "human"):
                scores = [j[role] for j in entries if j[role] is not None]
                model[role] = sum(scores) / len(scores) if scores else None
            pairs = [(j["agent"] + j["human"]) / 2 for j in entries
                     if j["agent"] is not None and j["human"] is not None]
            model["combined"] = sum(pairs) / len(pairs) if pairs else None
        models.sort(key=lambda m: (-(m["human"] or 0), -(m["agent"] or 0), m["model"]))
        return models, jobs

    def detail(self, job):
        with self.database() as db:
            details = db.execute("""SELECT jobs.*, task_id, title, merged, report FROM jobs
                LEFT JOIN task_jobs ON task_jobs.job=jobs.id WHERE jobs.id=?""", (str(job),)).fetchone()
            reviews = db.execute("SELECT * FROM reviews WHERE job=?", (str(job),))
            return details, {row["role"]: row for row in reviews}

    @contextmanager
    def worktree_env(self, env, worktree, reviewer):
        """Collect sandbox-local delegations before the runner checks for Git changes."""
        if reviewer not in ("codex", "claude-code"):
            yield env
            return
        with tempfile.TemporaryDirectory(prefix=".baton-ranking-", dir=worktree) as folder:
            root = Path(folder).absolute()
            (root / ".gitignore").write_text("*\n", encoding="ascii")
            try:
                yield {**env, "BATON_DELEGATION_DIR": str(root)}
            finally:
                path = root / "ranking.sqlite3"
                if path.is_file() and path.resolve() == path:
                    self._collect(path, reviewer)

    def _collect(self, path, reviewer):
        # The task controls its own files. Only import new jobs and agent reviews,
        # using a read-only source and explicit columns in a single transaction.
        uri = path.as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as source, self.database() as db:
            source.execute("PRAGMA trusted_schema = OFF")
            for row in source.execute("SELECT id, model, started, kind, prompt, outcome, result FROM jobs"):
                db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)", row)
                review = source.execute("""SELECT fulfillment, correctness, rework, comment
                    FROM reviews WHERE job=? AND role='agent'""", (row[0],)).fetchone()
                if review:
                    db.execute("INSERT INTO reviews VALUES (?, 'agent', ?, ?, ?, ?, ?)",
                               (row[0], reviewer, *review))
