"""Tests for Agents chat Phase 2: Claude Code, Codex app-server, interactive approvals, and session resume."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from baton import db, github, web
from baton.chat_claude import claude_chat_command
from baton.chat_codex import codex_chat_command
from baton.config import Settings
from baton.i18n import strings
from baton.web import create_app

FAKE_CLAUDE = Path(__file__).with_name("fake_claude_chat.py")
FAKE_CODEX = Path(__file__).with_name("fake_codex_app_server.py")
EN = strings("en")

CLAUDE_CATALOG = {
    "claude-sonnet-4-6": {"label": "Claude Sonnet 4.6", "efforts": ["low", "medium", "high"]},
    "claude-opus-4-6": {"label": "Claude Opus 4.6", "efforts": ["low", "medium", "high", "max"]},
}
CODEX_CATALOG = {
    "gpt-6.2-sol": {"label": "GPT-6.2 Sol", "efforts": ["low", "medium", "high", "xhigh"]},
}


def _wait(check, seconds: float = 15.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.03)
    raise AssertionError("timed out waiting for condition")


def _events(client: TestClient, cid: str) -> list[dict]:
    resp = client.get(f"/api/agents/{cid}/events?after=0")
    assert resp.status_code == 200
    return [
        json.loads(line[6:]) for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: {}"
    ]


def _make_app(tmp_path: Path, monkeypatch, *, unsafe_claude: bool = False):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_claude", lambda: "claude")
    monkeypatch.setattr(web, "find_codex", lambda: "codex")
    monkeypatch.setattr(
        web, "catalog",
        lambda agent: CLAUDE_CATALOG if agent == "claude-code" else (CODEX_CATALOG if agent == "codex" else {}),
    )
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_phase2")
    monkeypatch.setenv("BATON_ACCESS_KEY", "secret_key_phase2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk_ant_secret_phase2")

    scratch = tmp_path / "scratch"
    app = create_app(Settings(
        data_dir=tmp_path / "data", lang="en", self_update=False,
        chat_scratch=str(scratch), sandbox_image="",
    ))
    chats = app.state.agent_chats
    chats.claude_command = lambda model, effort, resume: (
        [sys.executable, str(FAKE_CLAUDE), "--model", model, "--effort", effort]
        + (["--resume", resume] if resume else [])
        + (["--unsafe-mode"] if unsafe_claude else [])
    )
    chats.codex_command = lambda: [sys.executable, str(FAKE_CODEX)]
    return app, scratch


def test_claude_and_codex_command_builders():
    cmd = claude_chat_command("claude-sonnet-4-6", "high", "sess-123", binary="/usr/bin/claude")
    assert cmd is not None
    assert cmd[:2] == ["/usr/bin/claude", "-p"]
    assert "--input-format" in cmd and "stream-json" in cmd
    assert "--output-format" in cmd and "--verbose" in cmd
    assert "--permission-mode" in cmd and cmd[cmd.index("--permission-mode") + 1] == "manual"
    assert "--permission-prompt-tool" in cmd and cmd[cmd.index("--permission-prompt-tool") + 1] == "stdio"
    assert "--strict-mcp-config" in cmd
    assert "--setting-sources" in cmd and cmd[cmd.index("--setting-sources") + 1] == "local"
    assert "--disallowedTools" in cmd and cmd[cmd.index("--disallowedTools") + 1] == "AskUserQuestion"
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"
    assert "--effort" in cmd and cmd[cmd.index("--effort") + 1] == "high"
    assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "sess-123"
    assert "--dangerously-skip-permissions" not in cmd

    cmd_full = claude_chat_command("claude-sonnet-4-6", "high", binary="/usr/bin/claude", mode="full")
    assert cmd_full is not None
    assert "--dangerously-skip-permissions" in cmd_full
    assert "--permission-mode" not in cmd_full
    assert "--permission-prompt-tool" not in cmd_full

    assert codex_chat_command(binary="/usr/bin/codex") == ["/usr/bin/codex", "app-server"]
    assert codex_chat_command(binary="") is None


def test_claude_chat_confirm_approvals_scrubbing_and_resume_across_restart(tmp_path, monkeypatch):
    app, scratch = _make_app(tmp_path, monkeypatch)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "hello.txt").write_text("hi from scratch\n", encoding="utf-8")

    with TestClient(app) as client:
        page = client.get("/chat?agent=claude-code").text
        assert 'value="claude-code" selected' in page
        assert "claude-sonnet-4-6" in page
        assert EN["ag_confirm_help_claude"] in page
        csrf = page.split('data-csrf="')[1].split('"')[0]
        headers = {"X-Baton-CSRF": csrf}

        # Full permissions is refused with 403 for Claude Code.
        r_full = client.post("/api/agents", json={
            "agent": "claude-code", "model": "claude-sonnet-4-6",
            "effort": "low", "folder": "scratch", "mode": "full",
        }, headers=headers)
        assert r_full.status_code == 403

        # Unknown resume session id is refused with 400.
        r_bad_resume = client.post("/api/agents", json={
            "agent": "claude-code", "model": "claude-sonnet-4-6",
            "effort": "low", "folder": "scratch", "mode": "confirm", "resume": "nonexistent",
        }, headers=headers)
        assert r_bad_resume.status_code == 400

        # Start Claude Code Confirm session.
        r = client.post("/api/agents", json={
            "agent": "claude-code", "model": "claude-sonnet-4-6",
            "effort": "high", "folder": "scratch", "mode": "confirm",
        }, headers=headers)
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        chat = app.state.agent_chats.get(cid)

        # 1. Free read runs without permission prompt.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "read hello.txt"}, headers=headers).status_code == 202
        _wait(lambda: not chat.busy)
        evs = _events(client, cid)
        assert not [e for e in evs if e["type"] == "permission"]
        assert "Read: hi from scratch" in "".join(e["text"] for e in evs if e["type"] == "text")

        # 2. Write asks permission -> Allow once writes file.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "write ok.txt allowed body"}, headers=headers).status_code == 202
        _wait(lambda: bool(chat.pending))
        pid = next(iter(chat.pending))
        assert client.post(f"/api/agents/{cid}/permission", json={"id": pid, "allow": True}, headers=headers).status_code == 200
        _wait(lambda: not chat.busy)
        assert (scratch / "ok.txt").read_text(encoding="utf-8") == "allowed body\n"

        # 3. Write asks permission -> Deny does NOT write file.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "write nope.txt blocked"}, headers=headers).status_code == 202
        _wait(lambda: bool(chat.pending))
        pid2 = next(iter(chat.pending))
        assert client.post(f"/api/agents/{cid}/permission", json={"id": pid2, "allow": False}, headers=headers).status_code == 200
        _wait(lambda: not chat.busy)
        assert not (scratch / "nope.txt").exists()

        # 4. Secrets stay out of env, and account/credential_expires_at never appear in events or DB.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "env"}, headers=headers).status_code == 202
        _wait(lambda: not chat.busy)
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "remember blue-whale"}, headers=headers).status_code == 202
        _wait(lambda: not chat.busy)

        all_evs = _events(client, cid)
        raw_evs = json.dumps(all_evs)
        assert "leaked: none" in raw_evs
        assert "SECRET_CLAUDE_EMAIL" not in raw_evs
        assert "SECRET_CREDENTIAL_EXPIRY" not in raw_evs
        sid = chat.session_id
        assert sid.startswith("claude-sess-")

        assert client.post(f"/api/agents/{cid}/close", headers=headers).status_code == 200

    # Simulate Baton restart: fresh app on the same data_dir sees saved session and resumes context.
    saved = db.list_chat_sessions(tmp_path / "data", "claude-code")
    assert len(saved) == 1 and saved[0]["session_id"] == sid
    assert "SECRET_CLAUDE_EMAIL" not in json.dumps(saved)

    app2, _ = _make_app(tmp_path, monkeypatch)
    with TestClient(app2) as client2:
        page2 = client2.get("/chat?agent=claude-code").text
        assert "data-oc-resume" in page2
        assert sid in page2
        csrf2 = page2.split('data-csrf="')[1].split('"')[0]
        headers2 = {"X-Baton-CSRF": csrf2}

        r_res = client2.post("/api/agents", json={
            "agent": "claude-code", "model": "claude-sonnet-4-6",
            "effort": "high", "folder": "scratch", "mode": "confirm", "resume": sid,
        }, headers=headers2)
        assert r_res.status_code == 200, r_res.text
        assert r_res.json()["resumed"] is True
        cid2 = r_res.json()["id"]
        chat2 = app2.state.agent_chats.get(cid2)

        assert client2.post(f"/api/agents/{cid2}/prompt", json={"text": "recall"}, headers=headers2).status_code == 202
        _wait(lambda: not chat2.busy)
        evs2 = _events(client2, cid2)
        assert any(e["type"] == "notice" and e["key"] == "ag_resumed" for e in evs2)
        assert "Recalled: blue-whale." in "".join(e["text"] for e in evs2 if e["type"] == "text")


def test_claude_chat_fails_closed_on_bypass_permission_mode(tmp_path, monkeypatch):
    app, _ = _make_app(tmp_path, monkeypatch, unsafe_claude=True)
    with TestClient(app) as client:
        page = client.get("/chat?agent=claude-code").text
        csrf = page.split('data-csrf="')[1].split('"')[0]
        r = client.post("/api/agents", json={
            "agent": "claude-code", "model": "claude-sonnet-4-6",
            "effort": "low", "folder": "scratch", "mode": "confirm",
        }, headers={"X-Baton-CSRF": csrf})
        assert r.status_code == 502
        assert "bypassPermissions" in r.json()["detail"]
        assert not app.state.agent_chats._chats


def test_codex_chat_app_server_approvals_scrubbing_and_resume_across_restart(tmp_path, monkeypatch):
    app, scratch = _make_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        page = client.get("/chat?agent=codex").text
        assert 'value="codex" selected' in page
        assert "gpt-6.2-sol" in page
        assert EN["ag_confirm_help_codex"] in page
        csrf = page.split('data-csrf="')[1].split('"')[0]
        headers = {"X-Baton-CSRF": csrf}

        # Full mode refused with 403 for Codex.
        r_full = client.post("/api/agents", json={
            "agent": "codex", "model": "gpt-6.2-sol",
            "effort": "medium", "folder": "scratch", "mode": "full",
        }, headers=headers)
        assert r_full.status_code == 403

        # Start Codex app-server session.
        r = client.post("/api/agents", json={
            "agent": "codex", "model": "gpt-6.2-sol",
            "effort": "high", "folder": "scratch", "mode": "confirm",
        }, headers=headers)
        assert r.status_code == 200, r.text
        cid = r.json()["id"]
        tid = r.json()["session_id"]
        assert tid.startswith("codex-thread-")
        chat = app.state.agent_chats.get(cid)

        # 1. Command approval -> accept executes and writes accepted.txt.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "run echo hola-codex"}, headers=headers).status_code == 202
        _wait(lambda: bool(chat.pending))
        pid = next(iter(chat.pending))
        assert client.post(f"/api/agents/{cid}/permission", json={"id": pid, "allow": True}, headers=headers).status_code == 200
        _wait(lambda: not chat.busy)
        assert (scratch / "accepted.txt").read_text(encoding="utf-8").strip() == "echo hola-codex"

        # 2. File change approval -> decline prevents edit.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "edit blocked.txt secret"}, headers=headers).status_code == 202
        _wait(lambda: bool(chat.pending))
        pid2 = next(iter(chat.pending))
        assert client.post(f"/api/agents/{cid}/permission", json={"id": pid2, "allow": False}, headers=headers).status_code == 200
        _wait(lambda: not chat.busy)
        assert not (scratch / "blocked.txt").exists()

        # 3. Timeout auto-denies.
        chat.permission_timeout = 0.25
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "edit late.txt nope"}, headers=headers).status_code == 202
        _wait(lambda: not chat.busy)
        assert not (scratch / "late.txt").exists()

        # 4. Env secret isolation + remember state for resume + account/* scrubbed.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "env"}, headers=headers).status_code == 202
        _wait(lambda: not chat.busy)
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "remember red-fox"}, headers=headers).status_code == 202
        _wait(lambda: not chat.busy)

        evs = _events(client, cid)
        raw_evs = json.dumps(evs)
        assert "leaked: none" in raw_evs
        assert "SECRET_PLAN_PRO" not in raw_evs
        assert "SECRET_CODEX" not in raw_evs
        assert any(e["type"] == "permission_done" and e["outcome"] == "timeout" for e in evs)

        # GET /api/agents/sessions lists saved thread.
        s_resp = client.get("/api/agents/sessions?agent=codex")
        assert s_resp.status_code == 200
        assert any(s["session_id"] == tid for s in s_resp.json()["sessions"])

        assert client.post(f"/api/agents/{cid}/close", headers=headers).status_code == 200

    # Simulate Baton restart and resume Codex thread via thread/resume (excludeTurns: true).
    app2, _ = _make_app(tmp_path, monkeypatch)
    with TestClient(app2) as client2:
        page2 = client2.get("/chat?agent=codex").text
        assert "data-oc-resume" in page2
        assert tid in page2
        csrf2 = page2.split('data-csrf="')[1].split('"')[0]
        headers2 = {"X-Baton-CSRF": csrf2}

        r_res = client2.post("/api/agents", json={
            "agent": "codex", "model": "gpt-6.2-sol",
            "effort": "high", "folder": "scratch", "mode": "confirm", "resume": tid,
        }, headers=headers2)
        assert r_res.status_code == 200, r_res.text
        assert r_res.json()["resumed"] is True
        cid2 = r_res.json()["id"]
        chat2 = app2.state.agent_chats.get(cid2)

        assert client2.post(f"/api/agents/{cid2}/prompt", json={"text": "recall"}, headers=headers2).status_code == 202
        _wait(lambda: not chat2.busy)
        evs2 = _events(client2, cid2)
        assert any(e["type"] == "notice" and e["key"] == "ag_resumed" for e in evs2)
        assert "Recalled: red-fox." in "".join(e["text"] for e in evs2 if e["type"] == "text")


def test_claude_cancel_timeout_and_sessions_auth(tmp_path, monkeypatch):
    app, scratch = _make_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        page = client.get("/chat?agent=claude-code").text
        csrf = page.split('data-csrf="')[1].split('"')[0]
        headers = {"X-Baton-CSRF": csrf}

        r = client.post("/api/agents", json={
            "agent": "claude-code", "model": "claude-sonnet-4-6",
            "effort": "low", "folder": "scratch", "mode": "confirm",
        }, headers=headers)
        assert r.status_code == 200
        cid = r.json()["id"]
        chat = app.state.agent_chats.get(cid)

        # Cancel resolves pending permission as cancelled and does not write.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "write cancelled.txt nope"}, headers=headers).status_code == 202
        _wait(lambda: bool(chat.pending))
        assert client.post(f"/api/agents/{cid}/cancel", headers=headers).status_code == 200
        _wait(lambda: not chat.busy)
        assert not (scratch / "cancelled.txt").exists()

        # Timeout auto-denies without writing.
        chat.permission_timeout = 0.25
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "write late.txt nope"}, headers=headers).status_code == 202
        _wait(lambda: not chat.busy)
        assert not (scratch / "late.txt").exists()

        evs = _events(client, cid)
        outcomes = [e["outcome"] for e in evs if e["type"] == "permission_done"]
        assert "cancelled" in outcomes and "timeout" in outcomes

    # /api/agents/sessions respects BATON_ACCESS_KEY auth (401 without cookie).
    locked = create_app(Settings(data_dir=tmp_path / "data", access_key="lock-key-123"))
    with TestClient(locked) as locked_client:
        assert locked_client.get("/api/agents/sessions?agent=claude-code").status_code == 401
        assert locked_client.get("/api/agents/sessions?agent=codex").status_code == 401
