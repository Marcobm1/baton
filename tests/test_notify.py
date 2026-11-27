"""Tests for Phase M3: Telegram + Webhook/ntfy notifications for alerts and finished/failed tasks."""

from __future__ import annotations

import json
import threading
import time

import httpx
from fastapi.testclient import TestClient

from baton import db, web
from baton.alerts import AlertEngine
from baton.config import Settings
from baton.notify import Notifier
from baton.ping import agent_env
from baton.task_store import Task


def _mock_client(calls: list[dict], *, fail_status: int | None = None, raise_exc: Exception | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        calls.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "json": body,
        })
        if raise_exc is not None:
            raise raise_exc
        if fail_status is not None:
            return httpx.Response(fail_status, text="upstream error")
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    return lambda: httpx.Client(transport=transport)


def test_telegram_and_webhook_delivery_and_token_masking(tmp_path):
    calls: list[dict] = []
    notifier = Notifier(
        telegram_bot_token="123456:SUPER_SECRET_BOT_TOKEN",
        telegram_chat_id="987654321",
        webhook_url="https://ntfy.example/baton-topic-secret",
        data_dir=tmp_path,
        lang="es",
        client_factory=_mock_client(calls),
    )
    assert notifier.configured is True
    assert notifier.channels == ["Telegram", "Webhook"]

    ok, msg = notifier.send_test()
    assert ok is True
    assert "Telegram: ok" in msg and "Webhook: ok" in msg
    assert len(calls) == 2

    tg, wh = calls[0], calls[1]
    assert tg["url"] == "https://api.telegram.org/bot123456:SUPER_SECRET_BOT_TOKEN/sendMessage"
    assert tg["json"]["chat_id"] == "987654321"
    assert "Aviso de prueba" in tg["json"]["text"]
    assert tg["json"]["disable_web_page_preview"] is True

    assert wh["url"] == "https://ntfy.example/baton-topic-secret"
    assert wh["json"]["source"] == "baton"
    assert "title" in wh["headers"] or "Title" in wh["headers"]

    # Errors must NEVER leak the Telegram bot token or webhook URL.
    err_calls: list[dict] = []
    err_notifier = Notifier(
        telegram_bot_token="123456:SUPER_SECRET_BOT_TOKEN",
        telegram_chat_id="987654321",
        webhook_url="https://ntfy.example/baton-topic-secret",
        client_factory=_mock_client(
            err_calls,
            raise_exc=httpx.ConnectError(
                "Connection failed to https://api.telegram.org/bot123456:SUPER_SECRET_BOT_TOKEN/sendMessage"
            ),
        ),
    )
    ok_err, msg_err = err_notifier.send_test()
    assert ok_err is False
    assert "SUPER_SECRET_BOT_TOKEN" not in msg_err
    assert "baton-topic-secret" not in msg_err


def test_alert_engine_notifies_and_stamps_notified_at_and_task_hook(tmp_path):
    calls: list[dict] = []
    done = threading.Event()

    def client_factory():
        base = _mock_client(calls)

        class _Hooked(httpx.Client):
            def post(self, *args, **kwargs):
                res = super().post(*args, **kwargs)
                done.set()
                return res

        return _Hooked(transport=base()._transport)

    notifier = Notifier(
        telegram_bot_token="tok-abc",
        telegram_chat_id="42",
        data_dir=tmp_path,
        lang="es",
        client_factory=client_factory,
    )
    engine = AlertEngine(tmp_path, on_opened=notifier.notify_alert)
    opened = engine.evaluate([{
        "id": "srv-1", "name": "VPS", "local": False, "status": "offline", "age_s": 240.0, "metrics": {},
    }])
    assert len(opened) == 1
    assert done.wait(5.0)
    # Wait briefly for SQLite stamp after HTTP returns.
    deadline = time.time() + 3.0
    notified_at = None
    while time.time() < deadline:
        with db.connect(tmp_path) as conn:
            row = conn.execute("SELECT notified_at FROM alerts WHERE id = ?", (opened[0]["id"],)).fetchone()
            notified_at = row["notified_at"]
        if notified_at:
            break
        time.sleep(0.02)
    assert notified_at is not None
    assert "VPS" in calls[0]["json"]["text"]

    # Task completion notification: done (merged) + failed notify, cancelled does NOT.
    done.clear()
    task_done = Task(
        "t1", "owner/baton", "Add Telegram alerts", "", "gemini", "local", time.time(),
        status="done", model="gemini-3.8-flash", merged=True,
        summary="Added Telegram alerts cleanly.", pr_url="https://github.com/owner/baton/pull/66",
    )
    notifier.notify_task(task_done)
    assert done.wait(5.0)
    txt = calls[-1]["json"]["text"]
    assert "Fusionada" in txt and "owner/baton" in txt
    assert "https://github.com/owner/baton/pull/66" in txt

    before = len(calls)
    task_cancelled = Task("t2", "owner/baton", "Cancelled", "", "codex", "local", time.time(), status="cancelled")
    notifier.notify_task(task_cancelled)
    time.sleep(0.1)
    assert len(calls) == before


def test_agent_env_strips_notification_secrets_and_test_route_enforces_csrf_and_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("BATON_TELEGRAM_BOT_TOKEN", "secret-tg-token")
    monkeypatch.setenv("BATON_TELEGRAM_CHAT_ID", "12345")
    monkeypatch.setenv("BATON_ALERT_WEBHOOK_URL", "https://ntfy.example/secret")
    env = agent_env()
    assert "BATON_TELEGRAM_BOT_TOKEN" not in env
    assert "BATON_TELEGRAM_CHAT_ID" not in env
    assert "BATON_ALERT_WEBHOOK_URL" not in env

    calls: list[dict] = []
    app = web.create_app(Settings(
        data_dir=tmp_path, lang="es",
        telegram_bot_token="tok-ui", telegram_chat_id="99",
    ))
    app.state.ctx.notifier._client_factory = _mock_client(calls)

    with TestClient(app) as client:
        page = client.get("/machines").text
        assert "Telegram" in page
        assert "Enviar aviso de prueba" in page
        csrf = app.state.ctx.csrf_token

        assert client.post("/machines/notify/test", data={"csrf": "bad"}).status_code == 403
        r = client.post("/machines/notify/test", data={"csrf": csrf})
        assert r.status_code == 200
        assert "Telegram: ok" in r.text
        assert len(calls) == 1

    # Route requires auth when BATON_ACCESS_KEY is set.
    locked = web.create_app(Settings(data_dir=tmp_path, access_key="key-notify-123"))
    with TestClient(locked) as locked_client:
        assert locked_client.post("/machines/notify/test", data={"csrf": "x"}).status_code == 401


def test_notifier_uses_huo_default_and_custom_app_name():
    default_n = Notifier(lang="es")
    t_alert, _ = default_n.format_alert({"severity": "bad", "machine_name": "VPS", "message": "down"})
    t_task, _ = default_n.format_task(Task("t1", "u/r", "Fix", "", "gemini", "local", 1.0, status="done"))
    t_perm, b_perm = default_n.format_permission({"agent": "claude-code", "command": "ls"})
    assert "Huo · CRÍTICA" in t_alert
    assert "Huo · Terminada" in t_task
    assert "Huo · Permiso pendiente" in t_perm and "en Huo" in b_perm

    custom_n = Notifier(lang="en", app_name="StarHub")
    ct_alert, _ = custom_n.format_alert({"severity": "warn", "machine_name": "PC", "message": "hot"})
    ct_perm, cb_perm = custom_n.format_permission({"agent": "codex", "command": "pwd"})
    assert "StarHub · WARNING" in ct_alert
    assert "StarHub · Approval waiting" in ct_perm and "in StarHub" in cb_perm
