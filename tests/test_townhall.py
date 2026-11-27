"""Integration tests for Phase 4 Town Hall mounted inside Huo (/townhall)."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from townhall import DemoAdapter, SessionConfig, TownHallEngine

from baton import github, web
from baton.config import Settings
from baton.notify import Notifier
from baton.townhall_bridge import (
    DEFAULT_GUEST_POOL,
    TownHallService,
    build_core_participants,
    build_schedule_spec,
    pick_strong_model,
)


def _make_app(tmp_path, monkeypatch, *, lang: str = "es", access_key: str = ""):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(
        web,
        "catalog",
        lambda agent: {
            "gemini": {
                "gemini-3.8-flash": {"label": "Gemini 3.8 Flash", "efforts": ["low"]},
                "gemini-4-neon": {"label": "Gemini 4 Neon", "efforts": ["high"]},
            },
            "codex": {
                "gpt-5.6-astra": {"label": "GPT 5.6 Astra", "efforts": ["high"]},
            },
            "claude-code": {
                "claude-sonnet": {"label": "Claude Sonnet", "efforts": ["medium"]},
                "claude-opus": {"label": "Claude Opus", "efforts": ["high"]},
            },
        }.get(agent, {}),
    )
    monkeypatch.setattr(
        web,
        "opencode_models",
        lambda: ["opencode-go/deepseek-v4-pro", "opencode-go/qwen4-max", "free/demo-model"],
    )

    repo_dir = tmp_path / "demo-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    app = web.create_app(
        Settings(
            data_dir=tmp_path / "data",
            lang=lang,
            self_update=False,
            access_key=access_key,
            chat_scratch=str(scratch_dir),
        )
    )
    app.state.ctx.runner.repos.all = lambda: {"u/demo": repo_dir}
    app.state.ctx.runner.agents_available = lambda: {
        "claude-code": "",
        "codex": "",
        "gemini": "",
        "opencode": "",
    }
    return app


def _wait_session_done(client: TestClient, sid: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/api/townhall/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in ("completed", "failed", "cancelled"):
            return data
        time.sleep(0.05)
    raise AssertionError(f"Town Hall session {sid} did not finish within {timeout}s")


def test_pick_strong_model_prefers_neon_astra_opus():
    assert pick_strong_model(
        {"gemini-3.8-flash": {}, "gemini-4-neon": {}}, ("neon", "pro")
    ) == "gemini-4-neon"
    assert pick_strong_model(
        {"gpt-5.6-mini": {}, "gpt-5.6-astra": {}}, ("astra", "codex")
    ) == "gpt-5.6-astra"
    assert pick_strong_model(
        {"claude-sonnet": {}, "claude-opus": {}}, ("opus", "sonnet")
    ) == "claude-opus"
    assert pick_strong_model({"only-model": {}}, ("neon",)) == "only-model"
    assert pick_strong_model({}, ("neon",)) == ""


@pytest.mark.parametrize("lang", ["en", "es"])
def test_townhall_page_renders_en_and_es(tmp_path, monkeypatch, lang):
    app = _make_app(tmp_path, monkeypatch, lang=lang)
    client = TestClient(app)
    resp = client.get("/townhall")
    assert resp.status_code == 200
    html = resp.text
    assert 'href="/townhall"' in html
    assert "u/demo" in html
    assert "gemini-4-neon" in html
    assert "gpt-5.6-astra" in html
    assert "claude-opus" in html
    assert "/static/townhall.css" in html


def test_townhall_auth_csrf_and_folder_allowlist(tmp_path, monkeypatch):
    # Auth protection when BATON_ACCESS_KEY is configured.
    locked = _make_app(tmp_path / "locked", monkeypatch, access_key="top-secret-key")
    locked_client = TestClient(locked, follow_redirects=False)
    assert locked_client.get("/townhall").status_code == 303
    assert locked_client.get("/townhall").headers["location"].startswith("/login")
    assert locked_client.get("/api/townhall").status_code == 401

    # CSRF + folder allowlist + empty topic checks.
    app = _make_app(tmp_path / "open", monkeypatch)
    client = TestClient(app, follow_redirects=False)
    csrf = app.state.ctx.csrf_token

    bad_csrf = client.post("/townhall", data={"csrf": "wrong", "topic": "Audit"})
    assert bad_csrf.status_code == 403

    empty_topic = client.post("/townhall", data={"csrf": csrf, "topic": "   "})
    assert empty_topic.status_code == 400

    traversal = client.post(
        "/townhall",
        data={"csrf": csrf, "topic": "Audit", "folder": "../../etc/passwd", "demo_mode": "1"},
    )
    assert traversal.status_code == 400


def test_townhall_demo_council_end_to_end_sse_markdown_and_task_bridge(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, lang="es")
    client = TestClient(app, follow_redirects=False)
    csrf = app.state.ctx.csrf_token

    resp = client.post(
        "/townhall",
        data={
            "csrf": csrf,
            "topic": "Revisar estado de la pagina y que falta",
            "folder": "u/demo",
            "mode": "council",
            "rounds": "1",
            "max_seconds": "60",
            "moderator": "claude",
            "guest_scope": "go",
            "demo_mode": "1",
        },
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/townhall/")
    sid = loc.rsplit("/", 1)[-1]

    data = _wait_session_done(client, sid)
    assert data["status"] == "completed"
    assert data["guest_selection"]["chosen_model"] in DEFAULT_GUEST_POOL
    assert data["conclusion"]["conclusion"]
    assert len(data["turns"]) >= 4

    # Detail HTML renders Conclusion, Summary, Guest Election, Full Transcript, and bridges.
    detail = client.get(f"/townhall/{sid}")
    assert detail.status_code == 200
    html = detail.text
    assert "Revisar estado de la pagina y que falta" in html
    assert data["guest_selection"]["chosen_model"] in html
    assert f"/tasks/new?from_townhall={sid}&amp;repo=u/demo" in html or f"/tasks/new?from_townhall={sid}&repo=u/demo" in html
    assert "/chat?folder=u/demo" in html
    assert f"/townhall/{sid}.md" in html

    # Markdown export endpoint.
    md = client.get(f"/townhall/{sid}.md")
    assert md.status_code == 200
    assert "text/markdown" in md.headers["content-type"]
    assert "# Town Hall:" in md.text
    assert "## Conclusion" in md.text

    # SSE endpoint emits terminal event cleanly even after completion.
    sse = client.get(f"/api/townhall/{sid}/events")
    assert sse.status_code == 200
    assert "text/event-stream" in sse.headers["content-type"]
    assert "session_completed" in sse.text

    # Convert-to-task bridge prefills /tasks/new with conclusion + guest + action plan.
    task_new = client.get(f"/tasks/new?from_townhall={sid}&repo=u/demo")
    assert task_new.status_code == 200
    assert "Town Hall: Revisar estado de la pagina y que falta" in task_new.text
    assert data["guest_selection"]["chosen_model"] in task_new.text


def test_townhall_schedules_crud_toggle_run_and_tick(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, lang="es")
    client = TestClient(app, follow_redirects=False)
    csrf = app.state.ctx.csrf_token

    # Create weekly schedule.
    r = client.post(
        "/townhall/schedules",
        data={
            "csrf": csrf,
            "name": "Revision semanal",
            "topic": "Revisar prioridades semanales",
            "folder": "u/demo",
            "mode": "council",
            "interval_hours": "168",
            "allowed_hours": "00:00-23:59",
            "max_seconds": "120",
            "moderator": "claude",
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/townhall"

    api = client.get("/api/townhall").json()
    assert len(api["schedules"]) == 1
    sched = api["schedules"][0]
    sched_id = sched["id"]
    assert sched["name"] == "Revision semanal"
    assert sched["enabled"] is True

    # Toggle off and back on.
    assert client.post(f"/townhall/schedules/{sched_id}/toggle", data={"csrf": csrf}).status_code == 303
    assert client.get("/api/townhall").json()["schedules"][0]["enabled"] is False
    assert client.post(f"/townhall/schedules/{sched_id}/toggle", data={"csrf": csrf}).status_code == 303
    assert client.get("/api/townhall").json()["schedules"][0]["enabled"] is True

    # Trigger manually in demo mode.
    run_resp = client.post(
        f"/townhall/schedules/{sched_id}/run",
        data={"csrf": csrf, "demo_mode": "1"},
    )
    assert run_resp.status_code == 303
    sid = run_resp.headers["location"].rsplit("/", 1)[-1]
    data = _wait_session_done(client, sid)
    assert data["status"] == "completed"
    assert client.get("/api/townhall").json()["schedules"][0]["last_session_id"] == sid

    # Delete schedule.
    assert client.post(f"/townhall/schedules/{sched_id}/delete", data={"csrf": csrf}).status_code == 303
    assert client.get("/api/townhall").json()["schedules"] == []

    # Direct scheduler tick fires on_finished callback with schedule_name.
    finished: list[tuple[str, str]] = []
    svc = TownHallService(
        tmp_path / "svc",
        adapter=DemoAdapter(),
        on_finished=lambda rec, name: finished.append((rec.status, name)),
    )
    spec = build_schedule_spec(
        name="Nightly check",
        topic="Check open risks",
        participants=build_core_participants(),
        guest_pool=list(DEFAULT_GUEST_POOL),
        interval_hours=1.0,
        allowed_hours="00:00-23:59",
    )
    svc.store.save_schedule(spec)
    ran = svc.tick_schedules()
    assert len(ran) == 1
    assert ran[0].status == "completed"
    assert finished == [("completed", "Nightly check")]


def test_townhall_telegram_notification_formatting_and_delivery():
    cfg = SessionConfig(
        topic="Review site architecture",
        participants=build_core_participants(
            gemini_model="gemini-4-neon",
            codex_model="gpt-5.6-astra",
            claude_model="claude-opus",
        ),
        guest_pool=list(DEFAULT_GUEST_POOL),
        mode="council",
        debate_rounds=1,
        language="es",
    )
    rec = TownHallEngine(DemoAdapter()).run(cfg, session_id="th123456")

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"ok": True})

    notifier_es = Notifier(
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="999",
        lang="es",
        app_name="Huo",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    title_es, body_es = notifier_es.format_townhall(rec, schedule_name="Semanal")
    assert title_es == "🏛️ Huo · Town Hall listo (Semanal)"
    assert "Review site architecture" in body_es
    assert "4º modelo" in body_es
    assert "/townhall/th123456" in body_es

    ok, msgs = notifier_es.send_sync(title_es, body_es)
    assert ok and msgs == ["Telegram: ok"]
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "999"
    assert "Huo · Town Hall listo (Semanal)" in sent[0]["text"]

    notifier_en = Notifier(lang="en", app_name="MyHuo")
    title_en, body_en = notifier_en.format_townhall(rec)
    assert title_en == "🏛️ MyHuo · Town Hall finished"
    assert "4th guest" in body_en

    rec.status = "failed"
    rec.conclusion = None
    rec.error = "CLI timed out"
    fail_title, fail_body = notifier_es.format_townhall(rec)
    assert fail_title == "❌ Huo · Town Hall fallido"
    assert "CLI timed out" in fail_body
