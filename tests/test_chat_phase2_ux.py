"""Chat-2 tests: Chat-to-Task bridge, Open-in-Chat link, and delayed Telegram approval notifications."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from baton import github, web
from baton.agent_chat import AgentChats
from baton.chat_history import ChatHistory, thread_task_draft
from baton.config import Settings
from baton.notify import Notifier
from baton.runner import Task, TaskStore
from baton.status import parse

FAKE_ACP = Path(__file__).with_name("fake_acp_agent.py")


def test_thread_task_draft_and_tasks_new_from_thread_and_open_in_chat(tmp_path: Path, monkeypatch):
    proj = github.Project(
        "u/baton", "u/baton", "STATUS.md", "main", False, "https://github.com/u/baton", None,
        parse("---\nname: Baton\nstatus: active\npriority: high\nupdated: 2026-11-26\n---\n"),
    )

    async def fake_load(self, force=False):
        return [proj], []

    monkeypatch.setattr(github.Hub, "load", fake_load)

    h = ChatHistory(tmp_path)
    th = h.create_thread(
        agent="gemini", model="gemini-4-neon", effort="high",
        folder_key="u/baton", folder_label="baton", title="Refactor alerts",
    )
    h.append_message(th["id"], "user", "Let's simplify the alert hysteresis threshold.")
    h.append_message(th["id"], "assistant", "Plan: move threshold checks into a single helper and add unit tests.")

    loaded = h.get_thread(th["id"])
    assert loaded is not None
    draft = thread_task_draft(loaded, lang="es")
    assert draft["title"] == "Refactor alerts"
    assert draft["repo"] == "u/baton"
    assert draft["agent"] == "gemini"
    assert draft["model"] == "gemini-4-neon"
    assert draft["effort"] == "high"
    assert "Contexto desde /chat:" in draft["details"]
    assert "hysteresis threshold" in draft["details"]

    # Local thread omits agent/model so /tasks/new picks an executable coding agent.
    local_th = h.create_thread(agent="local", model="qwen3", title="Draft spec")
    h.append_message(local_th["id"], "user", "Outline API endpoints")
    local_draft = thread_task_draft(h.get_thread(local_th["id"]), lang="en")
    assert local_draft["agent"] == "" and local_draft["model"] == "" and local_draft["title"] == "Draft spec"

    # End-to-end /tasks/new?from_thread=<id> pre-fills the new task form cleanly.
    store = TaskStore(tmp_path)
    store.save(Task("t-chat", "u/baton", "Fix bug", "", "codex", "local", 1.0, status="done", finished_at=2.0))
    app = web.create_app(Settings(data_dir=tmp_path, lang="es", self_update=False))
    client = TestClient(app)

    new_html = client.get(f"/tasks/new?from_thread={th['id']}").text
    assert "Refactor alerts" in new_html
    assert "hysteresis threshold" in new_html
    assert client.get("/tasks/new?from_thread=nope").status_code == 404

    # Task detail includes reverse "Abrir en Chat" bridge with agent & folder pre-selected.
    task_html = client.get("/tasks/t-chat").text
    assert "Abrir en Chat" in task_html
    assert "/chat?agent=codex&amp;folder=u/baton" in task_html or "/chat?agent=codex&amp;folder=u%2Fbaton" in task_html


def test_delayed_permission_alert_fires_only_when_unanswered_and_formats_telegram(tmp_path: Path):
    # 1. Notifier formats and delivers permission alert via Telegram.
    sent: list[tuple[str, bytes]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append((str(req.url), req.content))
        return httpx.Response(200, json={"ok": True})

    notifier = Notifier(
        telegram_bot_token="123:TOK",
        telegram_chat_id="999",
        lang="es",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    title, body = notifier.format_permission({
        "agent_label": "Claude Code",
        "folder": "baton",
        "command": "pytest -q",
        "title": "Bash",
    })
    assert "Permiso pendiente" in title and "Claude Code" in title
    assert "Carpeta: baton" in body and "pytest -q" in body
    ok, _ = notifier.send_sync(title, body, severity="warn")
    assert ok is True and len(sent) == 1

    # 2. AgentChats delayed timer fires when permission stays unanswered past threshold.
    fired: list[dict] = []
    fired_ev = threading.Event()

    def on_wait(payload: dict) -> None:
        fired.append(payload)
        fired_ev.set()

    chats = AgentChats(
        command=lambda: [sys.executable, str(FAKE_ACP)],
        data_dir=tmp_path,
        on_permission_wait=on_wait,
        permission_notify_after=0.15,
        reap_interval=0,
    )
    folder = tmp_path / "repo"
    folder.mkdir()
    chat = chats.create(folder, "repo", "opencode-go/glm-5.3", "default", "confirm", "opencode")
    try:
        chat.prompt("write a file")
        assert fired_ev.wait(timeout=5.0), "Expected delayed permission alert to fire"
        assert len(fired) == 1
        assert fired[0]["agent"] == "opencode"
        assert fired[0]["folder"] == "repo"
    finally:
        chats.close(chat.id)

    # 3. If user answers quickly (before delay), timer is cancelled and NEVER fires.
    fired2: list[dict] = []
    chats2 = AgentChats(
        command=lambda: [sys.executable, str(FAKE_ACP)],
        data_dir=tmp_path,
        on_permission_wait=fired2.append,
        permission_notify_after=0.6,
        reap_interval=0,
    )
    chat2 = chats2.create(folder, "repo", "opencode-go/glm-5.3", "default", "confirm", "opencode")
    try:
        chat2.prompt("write a file")
        deadline = time.time() + 5.0
        while not chat2.pending and time.time() < deadline:
            time.sleep(0.02)
        assert chat2.pending
        pid = next(iter(chat2.pending))
        assert chat2.answer(pid, False)
        time.sleep(0.75)
        assert fired2 == []
    finally:
        chats2.close(chat2.id)
