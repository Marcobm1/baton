"""Persistent SQLite-backed conversation threads & messages for /chat (Chat-1).

Stores local-model and agent chat threads in `baton.sqlite3` (`chat_threads` and
`chat_messages`) so conversations stay in sync between desktop and phone and
survive reloads or restarts.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from . import db

VALID_AGENTS = ("local", "opencode", "gemini", "claude-code", "codex")
VALID_ROLES = ("user", "assistant", "system")
MAX_TITLE_CHARS = 120
MAX_CONTENT_CHARS = 200_000
MAX_REASONING_CHARS = 200_000
MAX_META_CHARS = 300
MAX_MESSAGES_PER_REPLACE = 200
# Tool rows saved with an assistant turn (UI-6). Bounded so one noisy turn
# can't bloat the database: long outputs are clipped, big diffs dropped.
MAX_TOOLS_PER_MESSAGE = 80
MAX_TOOL_FIELD_CHARS = 4_000
MAX_TOOL_DIFF_CHARS = 20_000
MAX_TOOLS_JSON_CHARS = 250_000
_TOOL_TEXT_FIELDS = ("id", "kind", "name", "title", "status")
# Never keep what a tool printed after touching an env file: it may hold keys.
_SECRET_HINT = re.compile(r"\.env\b", re.IGNORECASE)


def _clip_title(text: str) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= MAX_TITLE_CHARS:
        return clean
    return clean[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def clean_tools(tools: Any) -> list[dict[str, Any]]:
    """Keep only the fields the chat renders, clipped to safe sizes."""
    if not isinstance(tools, list):
        return []
    out: list[dict[str, Any]] = []
    for ev in tools[:MAX_TOOLS_PER_MESSAGE]:
        if not isinstance(ev, dict):
            continue
        item: dict[str, Any] = {k: str(ev.get(k) or "")[:300] for k in _TOOL_TEXT_FIELDS if ev.get(k)}
        if not item.get("id"):
            continue
        secret = any(_SECRET_HINT.search(str(ev.get(k) or "")) for k in ("title", "input", "name"))
        if ev.get("input"):
            item["input"] = str(ev["input"])[:MAX_TOOL_FIELD_CHARS]
        if ev.get("output") and not secret:
            item["output"] = str(ev["output"])[:MAX_TOOL_FIELD_CHARS]
        diffs = []
        for d in (ev.get("diffs") or [])[:5]:
            if not isinstance(d, dict) or secret:
                continue
            old, new = str(d.get("old") or ""), str(d.get("new") or "")
            if len(old) + len(new) <= MAX_TOOL_DIFF_CHARS:
                diffs.append({"path": str(d.get("path") or "")[:300], "old": old, "new": new})
        if diffs:
            item["diffs"] = diffs
        out.append(item)
    # Last resort: drop outputs, then diffs, until the JSON fits.
    for field in ("output", "diffs"):
        if len(json.dumps(out)) <= MAX_TOOLS_JSON_CHARS:
            break
        for item in out:
            item.pop(field, None)
    return out


def _tools_json(tools: Any) -> str:
    cleaned = clean_tools(tools)
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else ""


def _tools_from_db(raw: str) -> list[dict[str, Any]]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return []
    return value if isinstance(value, list) else []


class ChatHistory:
    """Thread-safe CRUD for persistent chat threads and messages in baton.sqlite3."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def list_threads(self, *, limit: int = 60, query: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        q = (query or "").strip().lower()
        with db.connect(self.data_dir) as conn:
            if q:
                like = f"%{q}%"
                rows = conn.execute(
                    """SELECT t.*,
                              (SELECT COUNT(*) FROM chat_messages m WHERE m.thread_id = t.id) AS msg_count
                       FROM chat_threads t
                       WHERE lower(t.title) LIKE ? OR lower(t.model) LIKE ?
                             OR lower(t.folder_label) LIKE ? OR lower(t.agent) LIKE ?
                       ORDER BY t.updated_at DESC
                       LIMIT ?""",
                    (like, like, like, like, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT t.*,
                              (SELECT COUNT(*) FROM chat_messages m WHERE m.thread_id = t.id) AS msg_count
                       FROM chat_threads t
                       ORDER BY t.updated_at DESC
                       LIMIT ?""",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        tid = (thread_id or "").strip()
        if not tid:
            return None
        with db.connect(self.data_dir) as conn:
            row = conn.execute("SELECT * FROM chat_threads WHERE id = ?", (tid,)).fetchone()
            if row is None:
                return None
            msgs = conn.execute(
                """SELECT id, role, content, reasoning, meta, created_at, tools
                   FROM chat_messages
                   WHERE thread_id = ?
                   ORDER BY id ASC""",
                (tid,),
            ).fetchall()
        out = dict(row)
        out["messages"] = [{**dict(m), "tools": _tools_from_db(m["tools"])} for m in msgs]
        return out

    def create_thread(
        self,
        *,
        agent: str,
        model: str = "",
        effort: str = "",
        folder_key: str = "",
        folder_label: str = "",
        mode: str = "confirm",
        session_id: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        if agent not in VALID_AGENTS:
            raise ValueError(f"Unknown agent: {agent}")
        now = time.time()
        tid = secrets.token_urlsafe(12)
        clean_title = _clip_title(title)
        with db.connect(self.data_dir) as conn:
            conn.execute(
                """INSERT INTO chat_threads
                   (id, agent, model, effort, folder_key, folder_label, mode,
                    session_id, title, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tid,
                    agent,
                    str(model or "")[:120],
                    str(effort or "")[:32],
                    str(folder_key or "")[:120],
                    str(folder_label or "")[:120],
                    str(mode or "confirm")[:24],
                    str(session_id or "")[:120],
                    clean_title,
                    now,
                    now,
                ),
            )
        return {
            "id": tid,
            "agent": agent,
            "model": str(model or "")[:120],
            "effort": str(effort or "")[:32],
            "folder_key": str(folder_key or "")[:120],
            "folder_label": str(folder_label or "")[:120],
            "mode": str(mode or "confirm")[:24],
            "session_id": str(session_id or "")[:120],
            "title": clean_title,
            "created_at": now,
            "updated_at": now,
            "msg_count": 0,
            "messages": [],
        }

    def append_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        reasoning: str = "",
        meta: str = "",
        tools: Any = None,
    ) -> dict[str, Any] | None:
        if role not in VALID_ROLES:
            raise ValueError(f"Unknown role: {role}")
        now = time.time()
        clean_content = str(content or "")[:MAX_CONTENT_CHARS]
        clean_reasoning = str(reasoning or "")[:MAX_REASONING_CHARS]
        clean_meta = str(meta or "")[:MAX_META_CHARS]
        tools_json = _tools_json(tools) if role == "assistant" else ""
        with db.connect(self.data_dir) as conn:
            row = conn.execute("SELECT title FROM chat_threads WHERE id = ?", (thread_id,)).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """INSERT INTO chat_messages (thread_id, role, content, reasoning, meta, created_at, tools)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (thread_id, role, clean_content, clean_reasoning, clean_meta, now, tools_json),
            )
            new_title = row["title"]
            if not new_title and role == "user" and clean_content.strip():
                new_title = _clip_title(clean_content.splitlines()[0])
            conn.execute(
                "UPDATE chat_threads SET title = ?, updated_at = ? WHERE id = ?",
                (new_title, now, thread_id),
            )
            msg_id = cur.lastrowid
        return {
            "id": msg_id,
            "role": role,
            "content": clean_content,
            "reasoning": clean_reasoning,
            "meta": clean_meta,
            "tools": _tools_from_db(tools_json),
            "created_at": now,
            "thread_title": new_title,
        }

    def replace_messages(
        self,
        thread_id: str,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, Any] | None:
        """Replace a thread's message list atomically (used by local-chat edit/regenerate)."""
        cleaned: list[tuple[str, str, str, str, str]] = []
        for m in messages[-MAX_MESSAGES_PER_REPLACE:]:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "")
            if role not in VALID_ROLES:
                continue
            content = str(m.get("content") or "")[:MAX_CONTENT_CHARS]
            reasoning = str(m.get("reasoning") or "")[:MAX_REASONING_CHARS]
            meta = str(m.get("meta") or "")[:MAX_META_CHARS]
            tools_json = _tools_json(m.get("tools")) if role == "assistant" else ""
            if not content and not reasoning and not tools_json:
                continue
            cleaned.append((role, content, reasoning, meta, tools_json))

        now = time.time()
        with db.connect(self.data_dir) as conn:
            row = conn.execute(
                "SELECT title, model, effort FROM chat_threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
            for role, content, reasoning, meta, tools_json in cleaned:
                conn.execute(
                    """INSERT INTO chat_messages (thread_id, role, content, reasoning, meta, created_at, tools)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (thread_id, role, content, reasoning, meta, now, tools_json),
                )
            title = row["title"]
            if not title:
                first_user = next((c for r, c, _, _, _ in cleaned if r == "user" and c.strip()), "")
                if first_user:
                    title = _clip_title(first_user.splitlines()[0])
            new_model = str(model)[:120] if model is not None else row["model"]
            new_effort = str(effort)[:32] if effort is not None else row["effort"]
            conn.execute(
                "UPDATE chat_threads SET title = ?, model = ?, effort = ?, updated_at = ? WHERE id = ?",
                (title, new_model, new_effort, now, thread_id),
            )
        return self.get_thread(thread_id)

    def rename_thread(self, thread_id: str, title: str) -> bool:
        clean = _clip_title(title)
        if not clean:
            return False
        with db.connect(self.data_dir) as conn:
            cur = conn.execute(
                "UPDATE chat_threads SET title = ?, updated_at = ? WHERE id = ?",
                (clean, time.time(), thread_id),
            )
            return cur.rowcount > 0

    def update_session(self, thread_id: str, session_id: str) -> None:
        if not thread_id or not session_id:
            return
        with db.connect(self.data_dir) as conn:
            conn.execute(
                "UPDATE chat_threads SET session_id = ?, updated_at = ? WHERE id = ?",
                (str(session_id)[:120], time.time(), thread_id),
            )

    def delete_thread(self, thread_id: str) -> bool:
        with db.connect(self.data_dir) as conn:
            conn.execute("DELETE FROM chat_messages WHERE thread_id = ?", (thread_id,))
            cur = conn.execute("DELETE FROM chat_threads WHERE id = ?", (thread_id,))
            return cur.rowcount > 0


def thread_task_draft(thread: dict[str, Any], *, lang: str = "en") -> dict[str, str]:
    """Convert a saved chat thread into pre-filled fields for /tasks/new."""
    es = lang == "es"
    title = str(thread.get("title") or "").strip()[:200]
    msgs = [m for m in (thread.get("messages") or []) if isinstance(m, dict)]
    if not title:
        first_user = next((str(m.get("content") or "").strip() for m in msgs if m.get("role") == "user"), "")
        title = _clip_title(first_user.splitlines()[0]) if first_user else ("Tarea desde chat" if es else "Task from chat")

    header = "Contexto desde /chat:" if es else "Context from /chat:"
    user_lbl = "Usuario" if es else "User"
    asst_lbl = "Asistente" if es else "Assistant"
    lines = [header]
    budget = 4200
    tail = msgs[-8:]
    for m in tail:
        role = user_lbl if m.get("role") == "user" else asst_lbl
        body = str(m.get("content") or "").strip()
        if not body:
            continue
        snippet = body[:1000] + ("…" if len(body) > 1000 else "")
        entry = f"\n**{role}:**\n{snippet}"
        if sum(len(x) for x in lines) + len(entry) > budget:
            break
        lines.append(entry)

    folder_key = str(thread.get("folder_key") or "").strip()
    repo = folder_key if "/" in folder_key else ""
    agent = str(thread.get("agent") or "")
    if agent == "local":
        agent, model, effort = "", "", ""
    else:
        model = str(thread.get("model") or "")
        effort = str(thread.get("effort") or "")
        if effort == "default":
            effort = ""

    return {
        "title": title[:200],
        "details": "\n".join(lines).strip()[:4800],
        "repo": repo,
        "agent": agent,
        "model": model,
        "effort": effort,
    }

