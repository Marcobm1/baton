"""Chat-3 tests: magic-byte validated image attachments (vision & agents), system prompt presets, and context meter."""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton import github, models, web
from baton.config import Settings

FAKE_ACP = Path(__file__).with_name("fake_acp_agent.py")
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
FAKE_PNG_URL = "data:image/png;base64," + base64.b64encode(b"<svg onload=alert(1)/>").decode("ascii")
SVG_URL = "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode("ascii")


def test_validate_image_data_url_and_multimodal_chat_payload():
    mime, raw, canonical = models.validate_image_data_url(PNG_URL)
    assert mime == "image/png" and raw == PNG_BYTES and canonical == PNG_URL

    # Rejects SSRF remote URLs, SVG, spoofed magic bytes, invalid base64, oversized payloads.
    for bad in (
        "https://169.254.169.254/latest/meta-data",
        SVG_URL,
        FAKE_PNG_URL,
        "data:image/png;base64,!!!not-base64!!!",
        "data:image/png;base64," + base64.b64encode(PNG_BYTES + b"\x00" * models.CHAT_MAX_IMAGE_BYTES).decode("ascii"),
    ):
        with pytest.raises(ValueError):
            models.validate_image_data_url(bad)

    # Valid multimodal user message + system preset passes cleanly.
    payload = models.chat_payload({
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": [
                {"type": "text", "text": "What is in this screenshot?"},
                {"type": "image_url", "image_url": {"url": PNG_URL}},
            ]},
        ],
        "reasoning_effort": "medium",
    })
    assert payload["messages"][0] == {"role": "system", "content": "Be concise."}
    assert payload["messages"][1]["content"][1]["image_url"]["url"] == PNG_URL

    # Enforces max 4 images per request and still rejects raw string lists like ["image"].
    too_many = [{"type": "image_url", "image_url": {"url": PNG_URL}} for _ in range(models.CHAT_MAX_IMAGES + 1)]
    with pytest.raises(ValueError):
        models.chat_payload({"messages": [{"role": "user", "content": too_many}]})
    with pytest.raises(ValueError):
        models.chat_payload({"messages": [{"role": "user", "content": ["image"]}]})


def test_chat_page_renders_presets_context_meter_and_agent_image_upload_cleanup(tmp_path: Path, monkeypatch):
    from test_models import model_item, write_config

    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        web, "catalog",
        lambda agent: {"gemini-3.8-flash": {"label": "Gemini 3.8 Flash", "efforts": ["low", "high"]}} if agent == "gemini" else {},
    )
    item = model_item(tmp_path, "qwen")
    item["args"] = ["-c", "32768"]
    write_config(tmp_path, [item])
    scratch = tmp_path / "scratch"
    app = web.create_app(Settings(data_dir=tmp_path, lang="es", self_update=False, chat_scratch=str(scratch)))
    chats = app.state.agent_chats
    chats.gemini_command = lambda: [sys.executable, str(FAKE_ACP), "--gemini"]

    with TestClient(app) as client:
        local_html = client.get("/chat").text
        assert "data-preset" in local_html
        assert "data-custom-system" in local_html
        assert "data-ctx-meter" in local_html
        assert 'data-ctx="32768"' in local_html
        assert "data-attach-btn" in local_html
        assert "data-attach-input" in local_html

        csrf = app.state.ctx.csrf_token
        headers = {"X-Baton-CSRF": csrf}
        created = client.post(
            "/api/agents",
            json={"agent": "gemini", "model": "gemini-3.8-flash", "effort": "low", "folder": "scratch", "mode": "confirm"},
            headers=headers,
        )
        assert created.status_code == 200
        cid = created.json()["id"]

        # Hostile SVG / spoofed PNG rejected with 400.
        assert client.post(
            f"/api/agents/{cid}/prompt",
            json={"text": "look", "images": [SVG_URL]},
            headers=headers,
        ).status_code == 400
        assert client.post(
            f"/api/agents/{cid}/prompt",
            json={"text": "look", "images": [FAKE_PNG_URL]},
            headers=headers,
        ).status_code == 400

        # Valid PNG saved outside repo under data_dir/chat-uploads/<cid>/ and passed to agent.
        resp = client.post(
            f"/api/agents/{cid}/prompt",
            json={"text": "check screenshot", "images": [PNG_URL]},
            headers=headers,
        )
        assert resp.status_code == 202
        upload_dir = tmp_path / "chat-uploads" / cid
        files = list(upload_dir.glob("img_*.png"))
        assert len(files) == 1 and files[0].read_bytes() == PNG_BYTES

        chat = chats.get(cid)
        deadline = time.time() + 15
        while chat.busy and time.time() < deadline:
            time.sleep(0.05)
        user_events = [e for e in chat.events if e["type"] == "user"]
        assert user_events and "[Attached image:" in user_events[0]["text"]

        # Closing chat removes temporary uploads cleanly.
        assert client.post(f"/api/agents/{cid}/close", headers=headers).status_code == 200
        assert not upload_dir.exists()
