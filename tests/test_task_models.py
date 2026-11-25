import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from baton import web
from baton.config import Settings
from baton.i18n import strings
from baton.runner import Runner, TaskStore
from baton.task_models import closest_opencode_go_model, opencode_groups


def test_opencode_models_are_grouped_and_payg_warns_in_form(tmp_path, monkeypatch):
    raw = [
        "local-inkling/inkling-2-mini",
        "opencode/gemini-4-neon",
        "opencode-go/gemini-4-argon",
        "opencode-go/glm-5.3",
        "opencode/big-pickle",
        "local-qwen/qwen3.8-27b",
    ]
    groups_en = opencode_groups(raw, strings("en"))
    assert [(g["provider"], g["label"], g["models"]) for g in groups_en] == [
        ("opencode-go", "Included in OpenCode Go", ["opencode-go/gemini-4-argon", "opencode-go/glm-5.3"]),
        ("opencode", "Pay as you go (credits)", ["opencode/gemini-4-neon", "opencode/big-pickle"]),
        ("local-inkling", "local-inkling", ["local-inkling/inkling-2-mini"]),
        ("local-qwen", "local-qwen", ["local-qwen/qwen3.8-27b"]),
    ]
    groups_es = opencode_groups(raw, strings("es"))
    assert [g["label"] for g in groups_es] == [
        "Incluidos en OpenCode Go",
        "Pago por uso (créditos)",
        "local-inkling",
        "local-qwen",
    ]
    assert closest_opencode_go_model("opencode/gemini-4-neon", raw) == "opencode-go/gemini-4-argon"
    assert closest_opencode_go_model("opencode/glm-5.3", raw) == "opencode-go/glm-5.3"

    async def projects(self, force=False):
        return [SimpleNamespace(repo="x/demo", branch="main")], []

    monkeypatch.setattr(web.Hub, "load", projects)
    monkeypatch.setattr(web.LocalRepos, "get", lambda self, name: tmp_path)
    monkeypatch.setattr(Runner, "agents_available", lambda self: {k: "" for k in web.AGENTS})
    monkeypatch.setattr(web, "opencode_models", lambda: raw)
    monkeypatch.setattr(web, "catalog", lambda a: {})
    client = TestClient(web.create_app(Settings(data_dir=tmp_path, lang="en")))

    page_go = client.get("/tasks/new?agent=opencode&model=opencode-go/gemini-4-argon").text
    assert '<optgroup label="Included in OpenCode Go">' in page_go
    assert '<optgroup label="Pay as you go (credits)">' in page_go
    assert '<optgroup label="local-inkling">' in page_go
    assert re.search(r'id="oc-payg-warning"\s+hidden>', page_go)

    page_payg = client.get("/tasks/new?agent=opencode&model=opencode/gemini-4-neon").text
    assert '<option value="opencode/gemini-4-neon" selected>' in page_payg
    assert re.search(r'id="oc-payg-warning"\s*>', page_payg)
    assert "billed to OpenCode Models credits" in page_payg


@pytest.mark.parametrize("agent", ["codex", "claude-code"])
def test_model_and_effort_reach_local_command(tmp_path, monkeypatch, agent):
    async def projects(self, force=False):
        return [SimpleNamespace(repo="x/demo", branch="main")], []

    monkeypatch.setattr(web.Hub, "load", projects)
    monkeypatch.setattr(web.LocalRepos, "get", lambda self, name: tmp_path)
    monkeypatch.setattr(Runner, "agents_available", lambda self: {k: "" for k in web.AGENTS})
    monkeypatch.setattr(web, "opencode_models", lambda: [])
    monkeypatch.setattr(web, "catalog", lambda a: {"demo": {"label": "Demo", "efforts": ["high"]}})
    monkeypatch.setattr(Runner, "submit", lambda self, task: self.store.save(task))
    client = TestClient(web.create_app(Settings(data_dir=tmp_path)))
    page = client.get("/tasks/new").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
    form = {"csrf": csrf, "repo": "x/demo", "where": f"local:{agent}", "title": "Test", "model": "demo"}
    bad = client.post("/tasks", data={**form, "effort": "invalid"}, follow_redirects=False)
    assert bad.headers["location"].startswith("/tasks/new?")
    assert not TaskStore(tmp_path).all()
    response = client.post("/tasks", data={**form, "effort": "high"}, follow_redirects=False)
    task = TaskStore(tmp_path).get(response.headers["location"].split("/")[-1])
    assert (task.model, task.effort) == ("demo", "high")
    runner = Runner(TaskStore(tmp_path), web.LocalRepos("", ""), timeout=60, local_model="",
                    local_provider="ollama", git_name="Baton", git_email="b@example.org", open_pr=None)
    cmd = runner._agent_cmd(task, tmp_path)
    if agent == "codex":
        assert cmd[cmd.index("-m") + 1] == "demo"
        assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="high"'
    else:
        assert cmd[cmd.index("--model") + 1] == "demo"
        assert cmd[cmd.index("--effort") + 1] == "high"
