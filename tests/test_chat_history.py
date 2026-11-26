"""Chat-1 tests: SQLite persistent threads, search/rename/delete API, CSRF/auth guards, and agent turn persistence."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from baton import github, web
from baton.chat_history import ChatHistory
from baton.config import Settings

FAKE_ACP = Path(__file__).with_name("fake_acp_agent.py")


def test_chat_history_crud_search_replace_and_cascade(tmp_path: Path):
    h = ChatHistory(tmp_path)
    t1 = h.create_thread(agent="local", model="qwen3", effort="low")
    assert t1["title"] == ""

    # First user message auto-titles the thread.
    m1 = h.append_message(t1["id"], "user", "Explain SQLite WAL mode\nwith examples")
    assert m1 is not None and m1["thread_title"] == "Explain SQLite WAL mode"
    h.append_message(t1["id"], "assistant", "WAL writes changes to a sidecar log.", reasoning="Check WAL docs")

    t2 = h.create_thread(agent="gemini", model="gemini-4-neon", folder_key="baton", folder_label="baton", title="UI audit")
    h.append_message(t2["id"], "user", "Check mobile layout")

    # Listing & search work case-insensitively across title, model, folder, and agent.
    all_threads = h.list_threads()
    assert len(all_threads) == 2
    assert [t["id"] for t in h.list_threads(query="sqlite")] == [t1["id"]]
    assert [t["id"] for t in h.list_threads(query="gemini")] == [t2["id"]]

    # Replace messages supports edit/regenerate cleanly.
    replaced = h.replace_messages(
        t1["id"],
        [{"role": "user", "content": "Short question"}, {"role": "assistant", "content": "Short answer", "meta": "12 tok/s"}],
        model="qwen3-coder",
    )
    assert replaced is not None
    assert replaced["model"] == "qwen3-coder"
    assert len(replaced["messages"]) == 2
    assert replaced["messages"][1]["meta"] == "12 tok/s"

    # Rename & session link.
    assert h.rename_thread(t1["id"], "SQLite deep dive") is True
    h.update_session(t2["id"], "sess-xyz")
    loaded2 = h.get_thread(t2["id"])
    assert loaded2 is not None and loaded2["session_id"] == "sess-xyz"

    # Delete removes thread and its messages.
    assert h.delete_thread(t1["id"]) is True
    assert h.get_thread(t1["id"]) is None
    assert len(h.list_threads()) == 1


def test_chat_threads_rest_api_csrf_auth_and_page_bootstrap(tmp_path: Path, monkeypatch):
    from test_models import model_item, write_config

    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    write_config(tmp_path, [model_item(tmp_path, "qwen")])
    app = web.create_app(Settings(data_dir=tmp_path, lang="es", self_update=False))
    client = TestClient(app)
    csrf = app.state.ctx.csrf_token
    headers = {"X-Baton-CSRF": csrf}

    # Missing CSRF is rejected on write endpoints.
    assert client.post("/api/chat-threads", json={"agent": "local"}).status_code == 403

    created = client.post("/api/chat-threads", json={"agent": "local", "model": "qwen"}, headers=headers)
    assert created.status_code == 201
    tid = created.json()["id"]

    msg = client.post(
        f"/api/chat-threads/{tid}/messages",
        json={"role": "user", "content": "Hola Baton"},
        headers=headers,
    )
    assert msg.status_code == 201 and msg.json()["thread_title"] == "Hola Baton"

    listed = client.get("/api/chat-threads?q=hola")
    assert listed.status_code == 200
    assert [t["id"] for t in listed.json()["threads"]] == [tid]

    assert client.patch(f"/api/chat-threads/{tid}", json={"title": "Charla guardada"}, headers=headers).status_code == 200
    fetched = client.get(f"/api/chat-threads/{tid}").json()
    assert fetched["title"] == "Charla guardada" and len(fetched["messages"]) == 1

    # Opening /chat?thread=<tid> renders sidebar, suggestion chips, scroller, and bootstrap JSON.
    html = client.get(f"/chat?thread={tid}").text
    assert 'data-chat-shell' in html
    assert 'data-thread-search' in html
    assert 'data-scroll-bottom' in html
    assert 'data-suggest=' in html
    assert "Charla guardada" in html
    assert "Hola Baton" in html

    assert client.delete(f"/api/chat-threads/{tid}", headers=headers).status_code == 200
    assert client.get(f"/api/chat-threads/{tid}").status_code == 404

    # With BATON_ACCESS_KEY enabled, unauthenticated requests to /api/chat-threads return 401.
    locked = TestClient(web.create_app(Settings(data_dir=tmp_path, lang="es", self_update=False, access_key="secret")))
    assert locked.get("/api/chat-threads").status_code == 401


def test_agent_chat_persists_user_and_assistant_turns_to_sqlite(tmp_path: Path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        web, "catalog",
        lambda agent: {"gemini-3.8-flash": {"label": "Gemini 3.8 Flash", "efforts": ["low", "high"]}} if agent == "gemini" else {},
    )
    scratch = tmp_path / "scratch"
    app = web.create_app(Settings(data_dir=tmp_path / "data", lang="es", self_update=False, chat_scratch=str(scratch)))
    chats = app.state.agent_chats
    chats.gemini_command = lambda: [sys.executable, str(FAKE_ACP), "--gemini"]

    with TestClient(app) as client:
        csrf = app.state.ctx.csrf_token
        headers = {"X-Baton-CSRF": csrf}
        r = client.post(
            "/api/agents",
            json={"agent": "gemini", "model": "gemini-3.8-flash", "effort": "low", "folder": "scratch", "mode": "confirm"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        info = r.json()
        tid = info["thread_id"]
        assert tid

        assert client.post(f"/api/agents/{info['id']}/prompt", json={"text": "ping"}, headers=headers).status_code == 202
        chat = chats.get(info["id"])
        deadline = time.time() + 15
        while chat.busy and time.time() < deadline:
            time.sleep(0.05)

        # Drain SSE stream so server-side turn persistence flushes cleanly.
        sse = client.get(f"/api/agents/{info['id']}/events?after=0")
        assert sse.status_code == 200 and "event: idle" in sse.text

        # Thread now holds both the user prompt and the assistant reply in SQLite without duplication.
        thread = client.get(f"/api/chat-threads/{tid}").json()
        assert thread["agent"] == "gemini"
        assert thread["title"] == "ping"
        roles = [m["role"] for m in thread["messages"]]
        assert roles == ["user", "assistant"]
        assert thread["messages"][0]["content"] == "ping"
        assert "gemini-3.8-flash" in thread["messages"][1]["content"]

        # Opening /chat?thread=<tid> without explicit ?agent= auto-selects Gemini and injects transcript!
        page = client.get(f"/chat?thread={tid}").text
        assert 'data-agent="gemini"' in page
        boot_json = page.split('<script id="chat-bootstrap" type="application/json">')[1].split("</script>")[0]
        boot = json.loads(boot_json)
        assert boot["active_thread"]["id"] == tid
        assert len(boot["active_thread"]["messages"]) == 2
