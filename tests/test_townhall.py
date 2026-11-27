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


def test_build_participants_validation_and_duplicate_family_disambiguation():
    from baton.townhall_bridge import build_participants, format_provider_quotas, resolve_moderator_id

    # 2–4 participants across mixed or repeated families work cleanly.
    p2 = build_participants([("gemini", "gemini-4-neon"), ("codex", "gpt-5.6-astra")])
    assert [(p.id, p.name, p.provider, p.model) for p in p2] == [
        ("gemini", "Gemini", "gemini", "gemini-4-neon"),
        ("codex", "Codex", "codex", "gpt-5.6-astra"),
    ]
    assert resolve_moderator_id("1", p2) == "codex"
    assert resolve_moderator_id("gemini", p2) == "gemini"
    assert resolve_moderator_id("claude", p2) == "gemini"  # falls back to first active participant

    p4 = build_participants([
        ("gemini", "gemini-4-neon"),
        ("opencode", "opencode-go/deepseek-v4-pro"),
        ("opencode", "opencode-go/qwen4-max"),
        ("codex", "gpt-5.6-astra"),
    ])
    assert len(p4) == 4
    assert [p.id for p in p4] == ["gemini", "opencode_1", "opencode_2", "codex"]
    assert "deepseek-v4-pro" in p4[1].name and "qwen4-max" in p4[2].name

    # <2, >4, unknown provider, or empty model raise ValueError.
    with pytest.raises(ValueError):
        build_participants([("gemini", "gemini-4-neon")])
    with pytest.raises(ValueError):
        build_participants([("gemini", "g")] * 5)
    with pytest.raises(ValueError):
        build_participants([("gemini", "g"), ("unknown", "m")])
    with pytest.raises(ValueError):
        build_participants([("gemini", "g"), ("codex", "")])

    q = format_provider_quotas([
        {"provider": "claude", "kind": "5h", "left": 82.4, "age_s": 10, "source": "statusline"},
        {"provider": "claude", "kind": "week", "left": 65.0, "age_s": 10, "source": "statusline"},
        {"provider": "codex", "kind": "5h", "left": 3.0, "age_s": 10, "source": "codex-logs"},
    ])
    assert q["claude-code"]["summary"] == "5h 82% · 7d 65%"
    assert q["claude-code"]["exhausted"] is False
    assert q["codex"]["summary"] == "5h 3%"
    assert q["codex"]["exhausted"] is True


def test_townhall_dynamic_picker_page_quotas_validation_and_custom_sessions(tmp_path, monkeypatch):
    from baton import quotas

    monkeypatch.setattr(
        quotas,
        "all_windows",
        lambda data_dir, codex_home: [
            {"provider": "claude", "kind": "5h", "left": 74.0, "age_s": 5, "source": "statusline"},
            {"provider": "codex", "kind": "5h", "left": 100.0, "age_s": 5, "source": "codex-logs"},
        ],
    )
    app = _make_app(tmp_path, monkeypatch, lang="es")
    client = TestClient(app, follow_redirects=False)
    csrf = app.state.ctx.csrf_token

    # Initial page renders 2 empty slots per form (4 total across session + schedule forms),
    # with no model preselected and live quota percentages visible in family options.
    html = client.get("/townhall").text
    assert html.count("data-th-picker") == 2
    assert html.count('class="th-slot"') == 4
    assert "5h 74%" in html
    assert "5h 100%" in html
    assert 'data-th-model' in html
    assert 'data-th-add' in html

    # Validation: 1 participant -> 400, 5 participants -> 400, missing model -> 400.
    r1 = client.post("/townhall", data={
        "csrf": csrf, "topic": "Solo uno", "demo_mode": "1",
        "participant_provider": ["gemini"], "participant_model": ["gemini-4-neon"],
    })
    assert r1.status_code == 400

    r5 = client.post("/townhall", data={
        "csrf": csrf, "topic": "Demasiados", "demo_mode": "1",
        "participant_provider": ["gemini", "codex", "claude-code", "opencode", "opencode"],
        "participant_model": [
            "gemini-4-neon", "gpt-5.6-astra", "claude-opus",
            "opencode-go/deepseek-v4-pro", "opencode-go/qwen4-max",
        ],
    })
    assert r5.status_code == 400

    r_empty = client.post("/townhall", data={
        "csrf": csrf, "topic": "Sin elegir",
        "participant_provider": ["", ""], "participant_model": ["", ""],
    })
    assert r_empty.status_code == 400

    # 2-model debate without Claude (e.g. when Claude has no quota) + no guest.
    r_two = client.post("/townhall", data={
        "csrf": csrf, "topic": "Debate de dos sin Claude", "mode": "council",
        "rounds": "1", "moderator": "1", "guest_scope": "none", "demo_mode": "1",
        "participant_provider": ["gemini", "codex"],
        "participant_model": ["gemini-4-neon", "gpt-5.6-astra"],
    })
    assert r_two.status_code == 303
    sid2 = r_two.headers["location"].rsplit("/", 1)[-1]
    d2 = _wait_session_done(client, sid2)
    assert d2["status"] == "completed"
    assert [p["id"] for p in d2["participants"]] == ["gemini", "codex"]
    assert d2["guest_selection"] is None
    assert d2["conclusion"]["moderator_id"] == "codex"

    # 4-model debate + optional consensus guest still works (4 core + 1 guest).
    r_four = client.post("/townhall", data={
        "csrf": csrf, "topic": "Debate de cuatro mas invitado", "mode": "council",
        "rounds": "0", "moderator": "2", "guest_scope": "go", "demo_mode": "1",
        "participant_provider": ["gemini", "codex", "opencode", "opencode"],
        "participant_model": [
            "gemini-4-neon", "gpt-5.6-astra",
            "opencode-go/deepseek-v4-pro", "opencode-go/qwen4-max",
        ],
    })
    assert r_four.status_code == 303
    sid4 = r_four.headers["location"].rsplit("/", 1)[-1]
    d4 = _wait_session_done(client, sid4)
    assert d4["status"] == "completed"
    assert len(d4["participants"]) == 5  # 4 chosen + 1 optional consensus guest
    assert d4["guest_selection"]["chosen_model"] in DEFAULT_GUEST_POOL
    assert d4["guest_selection"]["chosen_model"] not in {
        "opencode-go/deepseek-v4-pro",
        "opencode-go/qwen4-max",
    }
    assert d4["conclusion"]["moderator_id"] == "opencode_1"

    # Schedule with custom 2 participants and no consensus guest.
    r_sc = client.post("/townhall/schedules", data={
        "csrf": csrf, "name": "Sin Claude", "topic": "Auditoria ligera",
        "interval_hours": "24", "moderator": "0", "guest_scope": "none",
        "participant_provider": ["gemini", "opencode"],
        "participant_model": ["gemini-4-neon", "opencode-go/qwen4-max"],
    })
    assert r_sc.status_code == 303
    sched = client.get("/api/townhall").json()["schedules"][0]
    assert [(p["provider"], p["model"]) for p in sched["participants"]] == [
        ("gemini", "gemini-4-neon"),
        ("opencode", "opencode-go/qwen4-max"),
    ]
    assert sched["guest_pool"] == []
    assert sched["moderator_id"] == "gemini"
    assert "opencode-go/qwen4-max" in client.get("/townhall").text


def test_townhall_no_meta_refresh_even_when_active(tmp_path, monkeypatch):
    app = _make_app(tmp_path, monkeypatch, lang="es")
    monkeypatch.setattr(app.state.ctx.townhall, "active_count", lambda: 2)
    client = TestClient(app)
    html = client.get("/townhall").text
    assert 'http-equiv="refresh"' not in html


def test_townhall_guest_exclude_env_and_core_models(tmp_path, monkeypatch):
    monkeypatch.setenv("BATON_TOWNHALL_GUEST_EXCLUDE", "opencode-go/deepseek-v4-pro, opencode-go/glm-5.3")
    monkeypatch.setenv("BATON_USE_GH_CLI", "0")
    s = Settings.from_env()
    assert s.townhall_guest_exclude == ["opencode-go/deepseek-v4-pro", "opencode-go/glm-5.3"]

    app = _make_app(tmp_path, monkeypatch, lang="es")
    app.state.ctx.settings.townhall_guest_exclude = ["opencode-go/deepseek-v4-pro"]
    client = TestClient(app, follow_redirects=False)
    csrf = app.state.ctx.csrf_token

    # Schedule created with qwen4-max as core participant + deepseek-v4-pro excluded by env:
    # neither model should appear in the saved guest_pool (and defaults are NOT re-added).
    r = client.post("/townhall/schedules", data={
        "csrf": csrf,
        "name": "Pool filtrado",
        "topic": "Revisar prioridades",
        "interval_hours": "24",
        "moderator": "0",
        "guest_scope": "go",
        "participant_provider": ["gemini", "opencode"],
        "participant_model": ["gemini-4-neon", "opencode-go/qwen4-max"],
    })
    assert r.status_code == 303
    sched = client.get("/api/townhall").json()["schedules"][0]
    assert sched["guest_pool"] == []


def test_townhall_sse_resume_with_after_and_last_event_id(tmp_path, monkeypatch):
    from townhall import SessionEvent, SessionRecord

    from baton.townhall_bridge import ActiveTownHall

    app = _make_app(tmp_path, monkeypatch, lang="es")
    sid = "resumetest1"
    rec = SessionRecord(
        id=sid,
        topic="Live stream test",
        mode="council",
        status="running",
        workspace="",
        language="es",
        participants=build_core_participants(),
        started_at=time.time(),
    )
    app.state.ctx.townhall.store.save(rec)

    now = time.time()
    active = ActiveTownHall(session_id=sid, topic=rec.topic, mode="council")
    active.push(SessionEvent(type="session_started", session_id=sid, timestamp=now, payload={"mode": "council"}))
    active.push(SessionEvent(type="phase_started", session_id=sid, timestamp=now, payload={"phase": "independent", "round_num": 1}))
    active.push(SessionEvent(
        type="guest_fallback",
        session_id=sid,
        timestamp=now,
        payload={"failed_model": "opencode-go/deepseek-v4-pro", "fallback_model": "opencode-go/qwen4-max"},
    ))
    active.push(SessionEvent(type="session_completed", session_id=sid, timestamp=now, payload={"status": "completed"}))
    app.state.ctx.townhall._active[sid] = active

    client = TestClient(app)

    # Running detail page includes live status bar, status URL, and live i18n payload.
    detail = client.get(f"/townhall/{sid}")
    assert detail.status_code == 200
    assert 'id="th-live-status"' in detail.text
    assert f'data-status-url="/api/townhall/{sid}"' in detail.text
    assert 'id="th-live-i18n"' in detail.text

    # Full stream delivers events 1..4 with sequential IDs.
    full = client.get(f"/api/townhall/{sid}/events")
    assert full.status_code == 200
    assert "id: 1\n" in full.text
    assert "id: 4\n" in full.text
    assert "session_started" in full.text
    assert "guest_fallback" in full.text

    # ?after=2 skips events 1..2 and resumes cleanly at id: 3.
    resumed_q = client.get(f"/api/townhall/{sid}/events?after=2")
    assert resumed_q.status_code == 200
    assert "session_started" not in resumed_q.text
    assert "phase_started" not in resumed_q.text
    assert "id: 3\n" in resumed_q.text
    assert "guest_fallback" in resumed_q.text
    assert "id: 4\n" in resumed_q.text

    # Last-Event-ID: 3 header skips events 1..3 and delivers only id: 4.
    resumed_hdr = client.get(f"/api/townhall/{sid}/events", headers={"Last-Event-ID": "3"})
    assert resumed_hdr.status_code == 200
    assert "guest_fallback" not in resumed_hdr.text
    assert "id: 4\n" in resumed_hdr.text
    assert "session_completed" in resumed_hdr.text


