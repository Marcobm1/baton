"""Tests for .env duplicate-key handling, env_file diagnostics, and safe manual restart."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from baton.config import Settings
from baton.web import create_app


def test_dotenv_later_duplicate_key_overrides_earlier_empty_placeholder(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BATON_TELEGRAM_BOT_TOKEN=\n"
        "BATON_TELEGRAM_CHAT_ID=\n"
        "BATON_OWNER_TAG=(Owner)\n"
        "\n"
        "BATON_TELEGRAM_BOT_TOKEN=123456:REAL_TOKEN\n"
        "BATON_TELEGRAM_CHAT_ID=987654321\n"
        "BATON_OWNER_TAG=(Alice)\n",
        encoding="utf-8",
    )
    for k in ("BATON_TELEGRAM_BOT_TOKEN", "BATON_TELEGRAM_CHAT_ID", "BATON_OWNER_TAG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BATON_ENV_FILE", str(env_file))
    monkeypatch.setenv("BATON_USE_GH_CLI", "0")

    s = Settings.from_env()
    assert s.telegram_bot_token == "123456:REAL_TOKEN"
    assert s.telegram_chat_id == "987654321"
    assert s.owner_tag == "(Alice)"
    assert s.env_file == str(env_file.resolve())


def test_dotenv_still_never_overrides_real_os_env_var(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("BATON_TELEGRAM_CHAT_ID=from_file\n", encoding="utf-8")
    monkeypatch.setenv("BATON_ENV_FILE", str(env_file))
    monkeypatch.setenv("BATON_TELEGRAM_CHAT_ID", "from_os")
    monkeypatch.setenv("BATON_USE_GH_CLI", "0")

    s = Settings.from_env()
    assert s.telegram_chat_id == "from_os"


def test_machines_shows_env_file_hint_and_manual_restart_endpoint(tmp_path: Path):
    restarted: list[bool] = []
    app = create_app(
        Settings(
            data_dir=tmp_path,
            lang="es",
            self_update=False,
            telegram_bot_token="123:ABC",
            telegram_chat_id="",
            env_file=str(tmp_path / ".env"),
        ),
        restart=lambda: restarted.append(True),
    )
    client = TestClient(app)

    page = client.get("/machines").text
    assert "Falta BATON_TELEGRAM_CHAT_ID" in page
    assert str(tmp_path / ".env") in page
    assert "Reiniciar Baton" in page
    assert "123:ABC" not in page

    csrf = app.state.ctx.csrf_token
    # Missing CSRF is rejected.
    assert client.post("/machines/restart", data={"csrf": "bad"}).status_code == 403
    assert not restarted

    # Valid CSRF triggers restart callback safely.
    resp = client.post("/machines/restart", data={"csrf": csrf})
    assert resp.status_code == 200
    assert restarted == [True]
    assert "Reiniciando Baton" in resp.text

    # Second restart while already restarting is refused cleanly.
    resp2 = client.post("/machines/restart", data={"csrf": csrf})
    assert resp2.status_code == 200
    assert restarted == [True]
    assert "no está disponible" in resp2.text
