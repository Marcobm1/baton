"""Chat page: what may be sent to a model, and the streamed relay through Baton."""
import json
import re
import sys

import pytest
from fastapi.testclient import TestClient

from baton import github, models
from baton.config import Settings
from baton.i18n import strings
from baton.web import create_app
from test_models import FAKE, model_item, wait_for, write_config


def test_payload_keeps_only_text_messages_and_effort():
    p = models.chat_payload({"messages": [{"role": "user", "content": "hi", "extra": 1}],
                             "reasoning_effort": "low", "temperature": 2, "max_tokens": 99999})
    assert p == {"messages": [{"role": "user", "content": "hi"}], "stream": True, "reasoning_effort": "low"}
    assert "reasoning_effort" not in models.chat_payload(
        {"messages": [{"role": "user", "content": "x"}], "reasoning_effort": "turbo"})
    for bad in ([], {"messages": []}, {"messages": [{"role": "tool", "content": "x"}]},
                {"messages": [{"role": "user", "content": ["image"]}]},
                {"messages": [{"role": "user", "content": "x" * (models.CHAT_MAX_CHARS + 1)}]}):
        with pytest.raises(ValueError):
            models.chat_payload(bad)


@pytest.fixture
def client(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []
    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(models.Model, "command",
                        lambda self, server: [sys.executable, str(FAKE), "--port", str(self.port)])
    monkeypatch.setattr(models, "_is_llama_server", lambda proc: True)
    return TestClient(create_app(Settings(data_dir=tmp_path, lang="es", self_update=False)))


def csrf_of(page: str) -> str:
    return page.split('data-csrf="')[1].split('"')[0]


def test_page_without_models(client):
    page = client.get("/chat").text
    assert 'href="/chat" aria-current="page"' in page and "models.json" in page
    assert 'http-equiv="refresh"' not in page        # would wipe a conversation


def test_chat_relay(client, tmp_path):
    write_config(tmp_path, [model_item(tmp_path, "one"), model_item(tmp_path, "two")])
    page = client.get("/chat?model=two").text
    assert re.search(r'value="two" data-name="Two" data-status="stopped"\s+selected', page)
    csrf = csrf_of(page)
    body = {"messages": [{"role": "user", "content": "hola"}], "reasoning_effort": "low", "temperature": 9}
    # The form token is required, like every other change Baton makes.
    assert client.post("/api/chat/one", json=body).status_code == 403
    headers = {"X-Baton-CSRF": csrf}
    assert client.post("/api/chat/nope", json=body, headers=headers).status_code == 404
    r = client.post("/api/chat/one", json=body, headers=headers)
    assert r.status_code == 409 and "no está en marcha" in r.json()["detail"]
    assert client.post("/api/chat/one", json={"messages": []}, headers=headers).status_code == 400

    lm = models.LocalModels(tmp_path)
    lm.start("one")
    try:
        wait_for(lm, "one", "running")
        r = client.post("/api/chat/one", json=body, headers=headers)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in r.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        text = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
        # Only messages, stream and the effort reach the model (no temperature).
        assert text == "messages,reasoning_effort,stream|hola"
        assert events[-1]["timings"]["predicted_per_second"] == 42.0
    finally:
        lm.stop("one")


def test_start_from_the_chat_returns_there(client, tmp_path):
    write_config(tmp_path, [model_item(tmp_path, "one")])
    csrf = csrf_of(client.get("/chat").text)
    r = client.post("/models/one/start", data={"csrf": csrf, "back": "/chat"}, follow_redirects=False)
    try:
        assert r.status_code == 303 and r.headers["location"] == "/chat?model=one"
        r = client.post("/models/nope/start", data={"csrf": csrf, "back": "/chat"}, follow_redirects=False)
        assert r.headers["location"].startswith("/chat?model=nope&error=")
        # Anything else as "back" goes to the Models page, never elsewhere.
        r = client.post("/models/nope/start", data={"csrf": csrf, "back": "https://evil.example"},
                        follow_redirects=False)
        assert r.headers["location"].startswith("/models?error=")
    finally:
        models.LocalModels(tmp_path).stop("one")


def test_model_cards_link_to_the_chat_and_start_button_is_clickable(client, tmp_path):
    write_config(tmp_path, [model_item(tmp_path, "one")])
    page = client.get("/models").text
    # The card-wide click area of project cards must not cover the Start button.
    assert 'class="c-name m-name"' in page and 'class="btn"' in page
    css = (models.Path(models.__file__).parent / "static" / "models.css").read_text(encoding="utf-8")
    assert ".m-model .m-name::after { content: none; }" in css
    assert strings("es")["m_open_chat"] == "Chatear"
