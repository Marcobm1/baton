"""Local history and independent reviews of OpenCode delegations."""

import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .config import configured_data_dir

CRITERIA = ("fulfillment", "correctness", "rework")


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
            yield db

    def start(self, model, kind, prompt):
        job = secrets.randbits(62) + 1  # Stable across independent task worktrees.
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

    def overview(self):
        with self.database() as db:
            models = db.execute("""SELECT model, COUNT(DISTINCT reviews.job) AS evaluated,
                AVG(fulfillment) AS fulfillment, AVG(correctness) AS correctness, AVG(rework) AS rework,
                AVG(CASE WHEN role='human' THEN (fulfillment+correctness+rework)/3.0 END) AS human,
                AVG(CASE WHEN role='agent' THEN (fulfillment+correctness+rework)/3.0 END) AS agent
                FROM jobs LEFT JOIN reviews ON jobs.id=reviews.job GROUP BY model
                ORDER BY human DESC, agent DESC, model""").fetchall()
            jobs = db.execute("""SELECT jobs.id, started, model, kind, outcome,
                MAX(role='human') AS human, MAX(role='agent') AS agent
                FROM jobs LEFT JOIN reviews ON jobs.id=reviews.job
                GROUP BY jobs.id ORDER BY started DESC, jobs.id DESC""").fetchall()
        return models, jobs

    def detail(self, job):
        with self.database() as db:
            details = db.execute("SELECT * FROM jobs WHERE id=?", (str(job),)).fetchone()
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
