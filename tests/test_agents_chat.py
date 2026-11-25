"""Agents chat, phase 1: OpenCode over ACP in Confirm mode, against a fake ACP agent."""
import fnmatch
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton import agent_chat, github, web
from baton.config import Settings
from baton.i18n import strings
from baton.web import create_app

FAKE_ACP = Path(__file__).with_name("fake_acp_agent.py")
MODELS = ["opencode-go/glm-5.3", "opencode/gemini-4-neon", "local-inkling/inkling-2-mini",
          "local-test/inkling-abliterated"]
ES = strings("es")


@pytest.fixture
def env(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []
    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "opencode_models", lambda: MODELS)
    monkeypatch.setattr(web, "find_opencode", lambda: "opencode")
    # Must never reach the agent's environment.
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("BATON_TEST_SECRET", "x")
    scratch = tmp_path / "scratch"
    app = create_app(Settings(data_dir=tmp_path / "data", lang="es", self_update=False,
                              chat_scratch=str(scratch)))
    chats = app.state.agent_chats
    chats.command = lambda: [sys.executable, str(FAKE_ACP)]
    with TestClient(app) as client:
        page = client.get("/chat?agent=opencode").text
        csrf = page.split('data-csrf="')[1].split('"')[0]
        yield {"client": client, "chats": chats, "scratch": scratch, "page": page,
               "headers": {"X-Baton-CSRF": csrf}}
    # Leaving the TestClient ran the app's shutdown: no OpenCode left running.
    assert not chats._chats


def wait(check, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def create(env, **body):
    data = {"agent": "opencode", "model": "local-inkling/inkling-2-mini", "effort": "low",
            "folder": "scratch", "mode": "confirm", **body}
    return env["client"].post("/api/agents", json=data, headers=env["headers"])


def say(env, cid, text):
    r = env["client"].post(f"/api/agents/{cid}/prompt", json={"text": text}, headers=env["headers"])
    assert r.status_code == 202, r.text
    return env["chats"].get(cid)


def finished(chat):
    wait(lambda: not chat.busy)
    return chat


def stream(env, cid, after=0):
    """The events as the browser receives them (the stream ends when idle)."""
    r = env["client"].get(f"/api/agents/{cid}/events?after={after}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.text.rstrip().endswith("event: idle\ndata: {}")
    return [json.loads(line[6:]) for line in r.text.splitlines()
            if line.startswith("data: ") and line != "data: {}"]


def turn_text(events, start):
    return "".join(e["text"] for e in events[start:] if e["type"] == "text")


def pending_permission(chat):
    def shown():
        return next((e for e in reversed(chat.events) if e["type"] == "permission" and e["id"] in chat.pending), None)
    wait(shown)
    return shown()


def test_page_offers_opencode_models_folders_and_a_disabled_full_mode(env):
    page = env["page"]
    assert 'data-agent-switch' in page and 'value="opencode" selected' in page
    assert 'label="Incluidos en OpenCode Go" data-catalog-key="go"' in page
    assert 'label="Pago por uso (créditos)" data-catalog-key="payg"' in page
    assert 'label="local-inkling" data-catalog-key="other" data-catalog-label="Otros proveedores"' in page
    assert 'value="local-inkling/inkling-2-mini"' in page
    assert 'value="opencode/gemini-4-neon" data-payg' in page
    assert f'<option value="scratch">{env["scratch"]} (pruebas)</option>' in page
    assert 'value="full" disabled data-mode' in page
    assert "Necesita el contenedor aislado (siguiente tarea)" in page
    assert 'value="confirm" checked data-mode' in page
    # The local-model chat is still the default agent.
    assert 'value="local" selected' in env["client"].get("/chat").text


def test_multi_turn_conversation_with_a_read_and_an_allowed_and_denied_write(env):
    client, headers, scratch = env["client"], env["headers"], env["scratch"]
    assert not scratch.exists()
    # Every change needs the page's token, like the rest of Baton.
    assert client.post("/api/agents", json={"model": MODELS[0], "folder": "scratch"}).status_code == 403
    r = create(env)
    assert r.status_code == 200, r.text
    info = r.json()
    cid = info["id"]
    assert scratch.is_dir()                       # created on first use
    assert info["model"] == "local-inkling/inkling-2-mini" and info["effort"] == "low"
    assert info["mode"] == "confirm" and info["folder"].startswith(str(scratch))
    assert client.post(f"/api/agents/{cid}/prompt", json={"text": "hola"}).status_code == 403

    # Two turns in the same OpenCode session: the agent remembers the first.
    finished(say(env, cid, "hola"))
    events = stream(env, cid)
    assert [e["type"] for e in events[:2]] == ["user", "thought"]
    assert turn_text(events, 0) == ("turn 1: hola (previous: -, model local-inkling/inkling-2-mini,"
                                    " effort low, mode baton-chat)")
    start = len(events)
    finished(say(env, cid, "¿y ahora?"))
    events = stream(env, cid)
    assert "turn 2: ¿y ahora? (previous: hola" in turn_text(events, start)
    assert stream(env, cid, after=len(events)) == []          # nothing new: ends at once

    # A read inside the folder runs without asking.
    (scratch / "notes.txt").write_text("hello from the file\n")
    start = len(events)
    finished(say(env, cid, "read notes.txt"))
    events = stream(env, cid)
    turn = events[start:]
    assert not [e for e in turn if e["type"] == "permission"]
    read = [e for e in turn if e["type"] == "tool"][-1]
    assert read["kind"] == "read" and read["status"] == "completed"
    assert read["output"] == "hello from the file\n" and "notes.txt" in read["input"]
    assert turn_text(events, start) == "The file says: hello from the file"

    # A write asks first, with the exact diff; allowed once, it happens.
    chat = say(env, cid, "write a.txt first line")
    perm = pending_permission(chat)
    assert perm["kind"] == "edit" and perm["can_allow"] and perm["command"] == ""
    assert perm["diffs"] == [{"path": str(scratch.resolve() / "a.txt"), "old": "", "new": "first line\n"}]
    assert perm["expires"] > time.time() + 590             # denied after 10 minutes
    assert not (scratch / "a.txt").exists()
    r = client.post(f"/api/agents/{cid}/permission", json={"id": perm["id"], "allow": True}, headers=headers)
    assert r.status_code == 200
    finished(chat)
    assert (scratch / "a.txt").read_text() == "first line\n"
    # An answer works once.
    again = client.post(f"/api/agents/{cid}/permission", json={"id": perm["id"], "allow": False}, headers=headers)
    assert again.status_code == 404
    events = stream(env, cid)
    assert {"type": "permission_done", "id": perm["id"], "outcome": "allowed"}.items() <= next(
        e for e in events if e["type"] == "permission_done").items()
    assert [e for e in events if e["type"] == "tool"][-1]["status"] == "completed"

    # Another write, denied: the file is never created.
    start = len(events)
    chat = say(env, cid, "write b.txt second")
    perm = pending_permission(chat)
    assert client.post(f"/api/agents/{cid}/permission", json={"id": perm["id"], "allow": False},
                       headers=headers).status_code == 200
    finished(chat)
    assert not (scratch / "b.txt").exists()
    events = stream(env, cid)
    assert turn_text(events, start) == "Not written (reject)."
    assert [e["outcome"] for e in events[start:] if e["type"] == "permission_done"] == ["denied"]

    # A command shows exactly what would run.
    chat = say(env, cid, "run del /s /q C:\\ & echo hi")
    perm = pending_permission(chat)
    assert perm["command"] == "del /s /q C:\\ & echo hi" and perm["kind"] == "execute"
    client.post(f"/api/agents/{cid}/permission", json={"id": perm["id"], "allow": False}, headers=headers)
    finished(chat)
    assert "Command reject." in turn_text(stream(env, cid), 0)


def test_a_permission_answer_from_another_conversation_is_refused(env):
    client, headers, scratch = env["client"], env["headers"], env["scratch"]
    first, second = create(env).json()["id"], create(env).json()["id"]
    chat = say(env, first, "write x.txt from the first")
    perm = pending_permission(chat)
    for target in (second, "not-a-chat"):
        r = client.post(f"/api/agents/{target}/permission", json={"id": perm["id"], "allow": True},
                        headers=headers)
        assert r.status_code == 404
    # Without the page's token it is refused too, even on the right conversation.
    r = client.post(f"/api/agents/{first}/permission", json={"id": perm["id"], "allow": True})
    assert r.status_code == 403
    assert chat.pending and not (scratch / "x.txt").exists()
    client.post(f"/api/agents/{first}/permission", json={"id": perm["id"], "allow": False}, headers=headers)
    finished(chat)
    assert not (scratch / "x.txt").exists()


def test_no_answer_denies(env):
    env["chats"].permission_timeout = 0.3
    cid = create(env).json()["id"]
    chat = finished(say(env, cid, "write late.txt too late"))
    events = stream(env, cid)
    assert [e["outcome"] for e in events if e["type"] == "permission_done"] == ["timeout"]
    assert turn_text(events, 0) == "Not written (reject)."
    assert not (env["scratch"] / "late.txt").exists()
    assert chat.pending == {}


def test_stop_cancels_a_pending_request(env):
    cid = create(env).json()["id"]
    chat = say(env, cid, "write never.txt nope")
    pending_permission(chat)
    assert env["client"].post(f"/api/agents/{cid}/cancel", headers=env["headers"]).status_code == 200
    finished(chat)
    events = stream(env, cid)
    assert [e["outcome"] for e in events if e["type"] == "permission_done"] == ["cancelled"]
    assert not (env["scratch"] / "never.txt").exists()


def test_full_permissions_is_refused_on_the_server(env, monkeypatch):
    r = create(env, mode="full", model="opencode-go/glm-5.3")
    assert r.status_code == 403 and r.json()["detail"] == ES["ag_full_off"]
    r = create(env, mode="full", model="local-test/inkling-abliterated")
    assert r.status_code == 403 and r.json()["detail"] == ES["ag_full_abliterated"]
    assert not env["chats"]._chats
    # Below the web layer too.
    with pytest.raises(agent_chat.ChatError):
        env["chats"].create(env["scratch"], "x", "opencode-go/glm-5.3", "default", "full")


def test_abliterated_also_behind_a_local_alias():
    official = {"id": "inkling-2-mini", "name": "Inkling 2 Mini (oficial)", "status": "running"}
    abliterated = {"id": "inkling-2-mini-abliterated", "name": "Inkling 2 Mini (abliterated)", "status": "running"}
    assert agent_chat.is_abliterated("local-x/qwen-uncensored", [])
    assert not agent_chat.is_abliterated("local-inkling/inkling-2-mini", [official])
    assert agent_chat.is_abliterated("local-inkling/inkling-2-mini", [official, abliterated])
    assert not agent_chat.is_abliterated("local-inkling/inkling-2-mini", [{**abliterated, "status": "stopped"}])
    assert not agent_chat.is_abliterated("opencode-go/glm-5.3", [abliterated])


def test_the_server_fixes_model_folder_and_effort(env, tmp_path):
    assert create(env, model="nope/nope").status_code == 400
    assert create(env, folder=str(tmp_path)).status_code == 400          # only listed folders
    assert create(env, folder="../..").status_code == 400
    assert create(env, effort="turbo").status_code == 400
    assert create(env, agent="claude-code").status_code == 400
    # An effort the model doesn't offer falls back to its default, and says so.
    r = create(env, effort="xhigh")
    assert r.status_code == 200 and r.json()["effort"] == "default"
    events = stream(env, r.json()["id"])
    assert events[0] == {"seq": 0, "type": "notice", "key": "ag_effort_missing", "effort": "xhigh"}
    cid = r.json()["id"]
    too_long = "x" * (agent_chat.MAX_PROMPT_CHARS + 1)
    assert env["client"].post(f"/api/agents/{cid}/prompt", json={"text": too_long},
                              headers=env["headers"]).status_code == 400


def test_secrets_stay_out_of_the_agent_and_closing_ends_it(env):
    cid = create(env).json()["id"]
    chat = finished(say(env, cid, "env"))
    assert turn_text(stream(env, cid), 0) == "leaked: none"
    proc = chat.client.proc
    assert env["client"].post(f"/api/agents/{cid}/close", headers=env["headers"]).status_code == 200
    assert proc.poll() is not None
    assert env["client"].get(f"/api/agents/{cid}").status_code == 404
    r = env["client"].post(f"/api/agents/{cid}/prompt", json={"text": "hola"}, headers=env["headers"])
    assert r.status_code == 404


def test_refuses_to_start_without_batons_permission_rules(env, monkeypatch):
    # The fake (like OpenCode) only offers the baton-chat mode when Baton's
    # config defines it; without it Baton must not fall back to "build".
    monkeypatch.setattr(agent_chat, "chat_config", lambda: {"agents": {}})
    r = create(env)
    assert r.status_code == 502 and ES["ag_err_start"] in r.json()["detail"]
    assert not env["chats"]._chats


def _effect(action: str, resource: str = "*") -> str:
    """OpenCode's rule evaluation: the last matching rule wins."""
    effect = "deny"
    for rule in agent_chat.CONFIRM_RULES:
        if fnmatch.fnmatchcase(action, rule["action"]) and fnmatch.fnmatchcase(resource, rule["resource"]):
            effect = rule["effect"]
    return effect


def test_confirm_rules_read_freely_and_ask_for_everything_else():
    assert _effect("read", "C:/repo/baton/web.py") == "allow"
    assert _effect("grep") == _effect("glob") == "allow"
    assert _effect("read", "C:/repo/.env") == "ask"
    assert _effect("edit", "C:/repo/a.txt") == "ask"
    assert _effect("shell", "pytest -q") == "ask"
    assert _effect("external_directory", "C:/Users/*") == "ask"
    for denied in [("shell", "git push origin main"), ("shell", "gh pr merge 1"), ("websearch", "*"),
                   ("webfetch", "*"), ("subagent", "*"), ("execute", "*"), ("opencode_worktree_remove", "*")]:
        assert _effect(*denied) == "deny", denied
    config = agent_chat.chat_config()["agents"][agent_chat.CHAT_AGENT]
    assert config["permissions"] == agent_chat.CONFIRM_RULES and config["mode"] == "primary"
