import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from baton import web
from baton.config import Settings
from baton.runner import Runner, TaskStore


@pytest.mark.parametrize("agent", ["codex", "claude-code"])
def test_model_and_effort_reach_local_command(tmp_path, monkeypatch, agent):
    async def projects(self, force=False):
        return [SimpleNamespace(repo="x/demo", branch="main")], []

    monkeypatch.setattr(web.Hub, "load", projects)
    monkeypatch.setattr(web.LocalRepos, "get", lambda self, name: tmp_path)
    monkeypatch.setattr(Runner, "agents_available", lambda self: {k: "" for k in web.AGENTS})
    monkeypatch.setattr(web, "opencode_models", lambda: [])
    monkeypatch.setattr(web, "catalog", lambda a: {"demo": {"label": "Demo", "efforts": ["high"]}})
    monkeypatch.setattr(Runner, "start", lambda self, task: self.store.save(task))
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
