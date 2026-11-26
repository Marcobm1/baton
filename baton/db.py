"""Unified SQLite WAL storage for Baton tasks and model ranking.

Consolidates legacy `tasks.json` and `ranking.sqlite3` into a single
`<data_dir>/baton.sqlite3` database opened in Write-Ahead Logging (WAL) mode
with `PRAGMA busy_timeout = 30000`.

Schema initialization runs once per database file (tracked by resolved path
plus file identity), and legacy files are migrated automatically on first
sight without deleting or modifying the originals.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

DB_NAME = "baton.sqlite3"
BUSY_TIMEOUT_MS = 30_000

_SCHEMA_SQL = (
    """CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        title TEXT NOT NULL,
        details TEXT NOT NULL,
        agent TEXT NOT NULL,
        where_kind TEXT NOT NULL,
        created_at REAL NOT NULL,
        status TEXT NOT NULL,
        branch TEXT NOT NULL,
        base TEXT NOT NULL,
        worktree TEXT NOT NULL,
        pr_url TEXT NOT NULL,
        issue_url TEXT NOT NULL,
        message TEXT NOT NULL,
        finished_at REAL,
        open_pr INTEGER NOT NULL,
        model TEXT NOT NULL,
        steps TEXT NOT NULL,
        effort TEXT NOT NULL,
        summary TEXT NOT NULL,
        issue_number INTEGER,
        reviewer TEXT NOT NULL,
        review TEXT NOT NULL,
        reviewer_model TEXT NOT NULL,
        reviewer_effort TEXT NOT NULL,
        merged INTEGER NOT NULL,
        priority TEXT NOT NULL,
        started_at REAL,
        review_rating TEXT,
        diff_stat TEXT,
        artifacts TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    """CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY,
        model TEXT NOT NULL,
        started REAL NOT NULL,
        kind TEXT NOT NULL,
        prompt TEXT NOT NULL,
        outcome TEXT,
        result TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS reviews (
        job INTEGER NOT NULL REFERENCES jobs(id),
        role TEXT NOT NULL,
        reviewer TEXT NOT NULL,
        fulfillment INTEGER NOT NULL CHECK(fulfillment BETWEEN 1 AND 10),
        correctness INTEGER NOT NULL CHECK(correctness BETWEEN 1 AND 10),
        rework INTEGER NOT NULL CHECK(rework BETWEEN 1 AND 10),
        comment TEXT NOT NULL CHECK(length(comment) <= 500),
        PRIMARY KEY(job, role)
    )""",
    """CREATE TABLE IF NOT EXISTS task_jobs (
        task_id TEXT PRIMARY KEY,
        job INTEGER UNIQUE NOT NULL REFERENCES jobs(id),
        title TEXT NOT NULL,
        merged INTEGER NOT NULL,
        report TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS chat_sessions (
        session_id TEXT NOT NULL,
        agent TEXT NOT NULL,
        folder_key TEXT NOT NULL,
        folder_label TEXT NOT NULL,
        model TEXT NOT NULL,
        effort TEXT NOT NULL,
        title TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY(agent, session_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chat_sessions_agent_updated ON chat_sessions(agent, updated_at DESC)",
    """CREATE TABLE IF NOT EXISTS machines (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        os_info TEXT NOT NULL,
        last_seen REAL NOT NULL,
        metrics_json TEXT NOT NULL,
        thresholds_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS machine_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        machine_id TEXT NOT NULL,
        ts REAL NOT NULL,
        cpu_pct REAL,
        ram_pct REAL,
        disk_pct REAL,
        gpu_pct REAL,
        gpu_temp_c REAL,
        net_rx_bps REAL,
        net_tx_bps REAL,
        logins_count INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_machine_samples_machine_ts ON machine_samples(machine_id, ts DESC)",
    """CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        machine_id TEXT NOT NULL,
        machine_name TEXT NOT NULL,
        kind TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT NOT NULL,
        opened_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        resolved_at REAL,
        acked_at REAL,
        notified_at REAL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(resolved_at, opened_at DESC)",
    """CREATE TABLE IF NOT EXISTS known_logins (
        machine_id TEXT NOT NULL,
        login_key TEXT NOT NULL,
        first_seen REAL NOT NULL,
        PRIMARY KEY(machine_id, login_key)
    )""",
)

_TASK_COLUMNS = (
    "id", "repo", "title", "details", "agent", "where_kind", "created_at",
    "status", "branch", "base", "worktree", "pr_url", "issue_url", "message",
    "finished_at", "open_pr", "model", "steps", "effort", "summary",
    "issue_number", "reviewer", "review", "reviewer_model", "reviewer_effort",
    "merged", "priority", "started_at", "review_rating", "diff_stat", "artifacts",
)

_TASK_DEFAULTS = {
    "status": "queued",
    "branch": "",
    "base": "",
    "worktree": "",
    "pr_url": "",
    "issue_url": "",
    "message": "",
    "finished_at": None,
    "open_pr": True,
    "model": "",
    "steps": [],
    "effort": "",
    "summary": "",
    "issue_number": None,
    "reviewer": "",
    "review": "",
    "reviewer_model": "",
    "reviewer_effort": "",
    "merged": False,
    "priority": "normal",
    "started_at": None,
    "review_rating": None,
    "diff_stat": None,
    "artifacts": [],
}

_TASK_REQUIRED = ("id", "repo", "title", "details", "agent", "where", "created_at")

_init_lock = threading.Lock()
# Maps resolved db path -> (st_dev, st_ino) so schema init runs once per DB file,
# while re-initializing automatically if a file is removed and recreated (e.g. tests).
_initialized: dict[Path, tuple[int, int]] = {}
_schema_init_count = 0


def db_path(data_dir: Path) -> Path:
    return Path(data_dir) / DB_NAME


def _file_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _row_from_legacy_dict(raw: dict) -> tuple | None:
    if not isinstance(raw, dict):
        return None
    if any(k not in raw for k in _TASK_REQUIRED):
        return None
    allowed = set(_TASK_REQUIRED) | set(_TASK_DEFAULTS)
    if any(k not in allowed for k in raw):
        return None
    merged = {**_TASK_DEFAULTS, **raw}
    steps = merged["steps"] if isinstance(merged["steps"], list) else []
    rating = merged["review_rating"]
    rating_json = json.dumps(rating) if isinstance(rating, dict) else None
    diff_stat = merged["diff_stat"]
    diff_json = json.dumps(diff_stat) if isinstance(diff_stat, dict) else None
    arts = merged["artifacts"]
    arts_json = json.dumps(arts) if isinstance(arts, list) and arts else None
    return (
        str(merged["id"]),
        str(merged["repo"]),
        str(merged["title"]),
        str(merged["details"]),
        str(merged["agent"]),
        str(merged["where"]),
        float(merged["created_at"]),
        str(merged["status"]),
        str(merged["branch"]),
        str(merged["base"]),
        str(merged["worktree"]),
        str(merged["pr_url"]),
        str(merged["issue_url"]),
        str(merged["message"]),
        float(merged["finished_at"]) if merged["finished_at"] is not None else None,
        1 if merged["open_pr"] else 0,
        str(merged["model"]),
        json.dumps(steps),
        str(merged["effort"]),
        str(merged["summary"]),
        int(merged["issue_number"]) if merged["issue_number"] is not None else None,
        str(merged["reviewer"]),
        str(merged["review"]),
        str(merged["reviewer_model"]),
        str(merged["reviewer_effort"]),
        1 if merged["merged"] else 0,
        str(merged["priority"]),
        float(merged["started_at"]) if merged["started_at"] is not None else None,
        rating_json,
        diff_json,
        arts_json,
    )


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply idempotent additive column migrations on existing SQLite databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "diff_stat" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN diff_stat TEXT")
    if "artifacts" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN artifacts TEXT")


def migrate_legacy_tasks(data_dir: Path, conn: sqlite3.Connection) -> int:
    """Import rows from legacy `tasks.json` once, keeping the original file untouched."""
    legacy = Path(data_dir) / "tasks.json"
    if not legacy.is_file() or _meta_get(conn, "migrated_tasks") == "1":
        return 0
    try:
        rows = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(rows, list):
        return 0
    placeholders = ", ".join("?" for _ in _TASK_COLUMNS)
    cols = ", ".join(_TASK_COLUMNS)
    imported = 0
    for item in rows:
        try:
            values = _row_from_legacy_dict(item)
        except (TypeError, ValueError):
            continue
        if values is None:
            continue
        cur = conn.execute(f"INSERT OR IGNORE INTO tasks({cols}) VALUES ({placeholders})", values)
        imported += cur.rowcount
    _meta_set(conn, "migrated_tasks", "1")
    return imported


def migrate_legacy_ranking(data_dir: Path, conn: sqlite3.Connection) -> int:
    """Import rows from legacy `ranking.sqlite3` once, keeping the original file untouched."""
    legacy = Path(data_dir) / "ranking.sqlite3"
    if not legacy.is_file() or _meta_get(conn, "migrated_ranking") == "1":
        return 0
    uri = legacy.resolve().as_uri() + "?mode=ro"
    imported = 0
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as src:
            src.execute("PRAGMA trusted_schema = OFF")
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "jobs" in tables:
                for row in src.execute(
                    "SELECT id, model, started, kind, prompt, outcome, result FROM jobs"
                ):
                    imported += conn.execute(
                        "INSERT OR IGNORE INTO jobs(id, model, started, kind, prompt, outcome, result) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        row,
                    ).rowcount
            if "reviews" in tables:
                for row in src.execute(
                    "SELECT job, role, reviewer, fulfillment, correctness, rework, comment FROM reviews"
                ):
                    conn.execute(
                        "INSERT OR IGNORE INTO reviews(job, role, reviewer, fulfillment, correctness, rework, comment) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        row,
                    )
            if "task_jobs" in tables:
                for row in src.execute(
                    "SELECT task_id, job, title, merged, report FROM task_jobs"
                ):
                    conn.execute(
                        "INSERT OR IGNORE INTO task_jobs(task_id, job, title, merged, report) "
                        "VALUES (?, ?, ?, ?, ?)",
                        row,
                    )
    except sqlite3.Error as exc:
        logging.getLogger(__name__).warning("Could not migrate legacy ranking.sqlite3: %s", exc)
        return 0
    _meta_set(conn, "migrated_ranking", "1")
    return imported


def _ensure_initialized(data_dir: Path, path: Path) -> None:
    global _schema_init_count
    key = _file_key(path)
    if key is not None and _initialized.get(path) == key:
        return
    with _init_lock:
        key = _file_key(path)
        if key is not None and _initialized.get(path) == key:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0)) as conn:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                for stmt in _SCHEMA_SQL:
                    conn.execute(stmt)
                _migrate_schema(conn)
                migrate_legacy_ranking(data_dir, conn)
                migrate_legacy_tasks(data_dir, conn)
        _schema_init_count += 1
        new_key = _file_key(path)
        if new_key is not None:
            _initialized[path] = new_key


@contextmanager
def connect(data_dir: Path):
    """Open a WAL-mode connection to `<data_dir>/baton.sqlite3` inside a transaction."""
    data_dir = Path(data_dir)
    path = db_path(data_dir).resolve()
    _ensure_initialized(data_dir, path)
    with closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn


def save_chat_session(
    data_dir: Path,
    *,
    session_id: str,
    agent: str,
    folder_key: str,
    folder_label: str,
    model: str,
    effort: str,
    title: str,
    updated_at: float | None = None,
    keep: int = 50,
) -> None:
    """Persist minimal chat session metadata for resume (never stores credentials or transcripts)."""
    import time as _time

    if not session_id or not agent:
        return
    ts = _time.time() if updated_at is None else float(updated_at)
    clean_title = " ".join((title or "").split())[:100]
    with connect(data_dir) as conn:
        conn.execute(
            "INSERT INTO chat_sessions "
            "(session_id, agent, folder_key, folder_label, model, effort, title, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent, session_id) DO UPDATE SET "
            "folder_key=excluded.folder_key, folder_label=excluded.folder_label, "
            "model=excluded.model, effort=excluded.effort, "
            "title=CASE WHEN excluded.title != '' THEN excluded.title ELSE chat_sessions.title END, "
            "updated_at=excluded.updated_at",
            (session_id, agent, folder_key, folder_label, model, effort, clean_title, ts),
        )
        conn.execute(
            "DELETE FROM chat_sessions WHERE agent = ? AND session_id NOT IN "
            "(SELECT session_id FROM chat_sessions WHERE agent = ? ORDER BY updated_at DESC LIMIT ?)",
            (agent, agent, keep),
        )


def list_chat_sessions(data_dir: Path, agent: str, *, limit: int = 15) -> list[dict]:
    """List recent saved chat sessions for `agent`, newest first."""
    with connect(data_dir) as conn:
        rows = conn.execute(
            "SELECT session_id, agent, folder_key, folder_label, model, effort, title, updated_at "
            "FROM chat_sessions WHERE agent = ? ORDER BY updated_at DESC LIMIT ?",
            (agent, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_chat_session(data_dir: Path, agent: str, session_id: str) -> dict | None:
    """Return one saved chat session by `(agent, session_id)`, or None."""
    with connect(data_dir) as conn:
        row = conn.execute(
            "SELECT session_id, agent, folder_key, folder_label, model, effort, title, updated_at "
            "FROM chat_sessions WHERE agent = ? AND session_id = ?",
            (agent, session_id),
        ).fetchone()
    return dict(row) if row else None


def delete_chat_session(data_dir: Path, agent: str, session_id: str) -> bool:
    """Delete one saved chat session record."""
    with connect(data_dir) as conn:
        cur = conn.execute(
            "DELETE FROM chat_sessions WHERE agent = ? AND session_id = ?",
            (agent, session_id),
        )
    return cur.rowcount > 0
