"""Tests for isolated Docker sandbox and All permissions mode in Agents chat."""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton import github, sandbox, web
from baton.config import Settings
from baton.i18n import strings
from baton.web import create_app

FAKE_ACP = Path(__file__).with_name("fake_acp_agent.py")
MODELS = ["opencode-go/glm-5.3", "local-inkling/inkling-2-mini", "local-test/inkling-abliterated"]
EN = strings("en")


def _full_effect(action: str, resource: str = "*") -> str:
    effect = "deny"
    for rule in sandbox.FULL_RULES:
        if fnmatch.fnmatchcase(action, rule["action"]) and fnmatch.fnmatchcase(resource, rule["resource"]):
            effect = rule["effect"]
    return effect


def test_sandbox_docker_command_hardening_flags(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg = json.dumps(sandbox.sandbox_opencode_config([], mode="full"))
    cmd = sandbox.sandbox_docker_command(
        ws, "baton-opencode-sandbox:latest", cfg,
        container_name="baton-sandbox-abc", docker_bin="docker",
    )

    assert cmd[:5] == ["docker", "run", "--rm", "-i", "--init"]
    assert "--cap-drop=ALL" in cmd
    assert "--security-opt=no-new-privileges:true" in cmd
    assert "--pids-limit=256" in cmd
    assert "--memory=4g" in cmd
    assert "--add-host=host.docker.internal:host-gateway" in cmd
    assert f"type=bind,src={os.path.realpath(ws)},dst=/workspace" in cmd
    assert cmd[cmd.index("-w") + 1] == "/workspace"
    # Never mount docker socket, host profile, or Baton data directory.
    joined = " ".join(cmd)
    assert "docker.sock" not in joined
    assert "GITHUB_TOKEN" not in joined
    assert "BATON_ACCESS_KEY" not in joined
    assert cmd[-3:] == ["baton-opencode-sandbox:latest", "opencode", "acp"]


def test_sandbox_opencode_config_maps_local_models_and_full_rules():
    local_models = [
        {"id": "inkling-2-mini", "name": "Inkling 2 Mini", "family_key": "inkling", "port": 18080},
        {"id": "qwen-32b", "name": "Qwen 32B", "family_key": "qwen", "port": 18081},
    ]
    cfg = sandbox.sandbox_opencode_config(local_models, mode="full")
    assert cfg["provider"]["local-inkling"]["options"]["baseURL"] == "http://host.docker.internal:18080/v1"
    assert cfg["provider"]["local-qwen"]["options"]["baseURL"] == "http://host.docker.internal:18081/v1"
    assert sandbox.CHAT_AGENT_FULL in cfg["agents"]

    # FULL_RULES allow read/edit/shell inside /workspace, but still deny dangerous commands/external paths.
    assert _full_effect("read", "/workspace/a.py") == "allow"
    assert _full_effect("edit", "/workspace/a.py") == "allow"
    assert _full_effect("shell", "pytest -q") == "allow"
    assert _full_effect("external_directory", "/etc/passwd") == "deny"
    for denied in [("shell", "git push origin main"), ("shell", "gh pr merge 1"), ("shell", "sudo rm -rf /"),
                   ("subagent", "*"), ("execute", "*"), ("opencode_worktree_remove", "*")]:
        assert _full_effect(*denied) == "deny", denied


def test_full_mode_switch_with_fake_acp_and_server_guards(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "opencode_models", lambda: MODELS)
    monkeypatch.setattr(web, "find_opencode", lambda: "opencode")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("BATON_ACCESS_KEY", "top_secret")

    scratch = tmp_path / "scratch"
    app = create_app(Settings(data_dir=tmp_path / "data", lang="en", self_update=False,
                              chat_scratch=str(scratch)))
    chats = app.state.agent_chats
    chats.command = lambda: [sys.executable, str(FAKE_ACP)]
    seen_sandbox: list[tuple[Path, str, str]] = []

    def fake_sandbox_cmd(folder: Path, cname: str, cfg_json: str) -> list[str]:
        seen_sandbox.append((folder, cname, cfg_json))
        return [sys.executable, str(FAKE_ACP)]

    chats.sandbox_command = fake_sandbox_cmd

    with TestClient(app) as client:
        page = client.get("/chat?agent=opencode").text
        assert 'data-sandbox-ready="1"' in page
        assert 'value="full" data-mode' in page
        assert "data-full-warn" in page
        assert "data-full-badge" in page
        csrf = page.split('data-csrf="')[1].split('"')[0]
        headers = {"X-Baton-CSRF": csrf}

        # 1. Abliterated models are ALWAYS refused, even with Docker sandbox ready.
        r_abl = client.post("/api/agents", json={
            "agent": "opencode", "model": "local-test/inkling-abliterated",
            "effort": "low", "folder": "scratch", "mode": "full",
        }, headers=headers)
        assert r_abl.status_code == 403 and r_abl.json()["detail"] == EN["ag_full_abliterated"]

        # 2. Cloud/Go models are refused for Full mode (no cloud credentials inside container).
        r_go = client.post("/api/agents", json={
            "agent": "opencode", "model": "opencode-go/glm-5.3",
            "effort": "low", "folder": "scratch", "mode": "full",
        }, headers=headers)
        assert r_go.status_code == 403 and r_go.json()["detail"] == EN["ag_full_local_only"]

        # 3. Non-abliterated local model in Full mode starts inside the sandbox.
        r_ok = client.post("/api/agents", json={
            "agent": "opencode", "model": "local-inkling/inkling-2-mini",
            "effort": "low", "folder": "scratch", "mode": "full",
        }, headers=headers)
        assert r_ok.status_code == 200, r_ok.text
        info = r_ok.json()
        assert info["mode"] == "full" and info["isolated"] is True
        assert len(seen_sandbox) == 1
        assert seen_sandbox[0][0] == Path(os.path.realpath(scratch))

        cid = info["id"]
        # Write and run succeed without permission requests, while logging every tool call.
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "write auto.txt created in full"},
                           headers=headers).status_code == 202
        chat = chats.get(cid)
        deadline = time.time() + 10
        while time.time() < deadline and chat.busy:
            time.sleep(0.02)
        assert (scratch / "auto.txt").read_text() == "created in full\n"

        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "run echo isolated"},
                           headers=headers).status_code == 202
        deadline = time.time() + 10
        while time.time() < deadline and chat.busy:
            time.sleep(0.02)

        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "env"},
                           headers=headers).status_code == 202
        deadline = time.time() + 10
        while time.time() < deadline and chat.busy:
            time.sleep(0.02)

        ev_resp = client.get(f"/api/agents/{cid}/events?after=0")
        events = [json.loads(line[6:]) for line in ev_resp.text.splitlines()
                  if line.startswith("data: ") and line != "data: {}"]
        assert not [e for e in events if e["type"] == "permission"]
        tool_kinds = [e["kind"] for e in events if e["type"] == "tool"]
        assert "edit" in tool_kinds and "execute" in tool_kinds
        full_text = "".join(e["text"] for e in events if e["type"] == "text")
        assert "Written." in full_text
        assert "Command once." in full_text
        assert "leaked: none" in full_text


def test_full_mode_refuses_hardlinked_files(tmp_path, monkeypatch):
    """Pre-existing hardlinks (st_nlink > 1) inside a folder must block Full mode."""
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "opencode_models", lambda: MODELS)
    monkeypatch.setattr(web, "find_opencode", lambda: "opencode")

    sibling = tmp_path / "sibling"
    sibling.mkdir()
    secret = sibling / "secret.txt"
    secret.write_text("SIBLING_SECRET\n", encoding="utf-8")

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    hardlink = scratch / "leaked_hardlink.txt"
    os.link(str(secret), str(hardlink))
    assert sandbox.find_hardlinks(scratch) == ["leaked_hardlink.txt"]

    app = create_app(Settings(data_dir=tmp_path / "data", lang="en", self_update=False,
                              chat_scratch=str(scratch)))
    chats = app.state.agent_chats
    chats.sandbox_command = lambda folder, cname, cfg_json: [sys.executable, str(FAKE_ACP)]

    with TestClient(app) as client:
        page = client.get("/chat?agent=opencode").text
        csrf = page.split('data-csrf="')[1].split('"')[0]
        r = client.post("/api/agents", json={
            "agent": "opencode", "model": "local-inkling/inkling-2-mini",
            "effort": "low", "folder": "scratch", "mode": "full",
        }, headers={"X-Baton-CSRF": csrf})
        assert r.status_code == 403
        assert EN["ag_full_hardlink"] in r.json()["detail"]
        assert "leaked_hardlink.txt" in r.json()["detail"]


@pytest.mark.skipif(
    not sandbox.docker_available("alpine:3.20"),
    reason="Real Docker daemon with alpine:3.20 image not available on this runner",
)
def test_real_docker_container_confines_workspace_and_blocks_sibling_and_symlink(tmp_path):
    """Acceptance check from docs/agents-chat.md: two temporary sibling folders + symlink/junction."""
    workspace = tmp_path / "workspace"
    sibling = tmp_path / "sibling"
    workspace.mkdir()
    sibling.mkdir()
    secret = sibling / "secret.txt"
    secret.write_text("TOP_SECRET_SIBLING_DATA\n", encoding="utf-8")

    escape_target = "/workspace/escape_link"
    escape_link = workspace / "escape_link"
    try:
        os.symlink(str(secret), str(escape_link))
    except OSError:
        if os.name == "nt":
            # Without Windows Developer Mode, os.symlink raises WinError 1314;
            # use a directory junction (`mklink /J`) which needs no elevation.
            j = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(escape_link), str(sibling)],
                capture_output=True, text=True,
            )
            if j.returncode != 0:
                pytest.skip(f"Could not create symlink or junction: {j.stderr.strip()}")
            escape_target = "/workspace/escape_link/secret.txt"
        else:
            pytest.skip("Could not create symlink in workspace")

    script = (
        "set -eu\n"
        "echo from-parent > /workspace/by-parent.txt\n"
        "sh -c 'echo from-child > /workspace/by-child.txt'\n"
        "if cat ../sibling/secret.txt 2>/dev/null; then echo LEAK_REL; exit 2; fi\n"
        f"if cat {str(secret)!r} 2>/dev/null; then echo LEAK_ABS; exit 3; fi\n"
        f"if cat {escape_target} 2>/dev/null; then echo LEAK_LINK; exit 4; fi\n"
        f"if sh -c 'cat {escape_target} 2>/dev/null'; then echo LEAK_CHILD_LINK; exit 5; fi\n"
        "if [ -e /var/run/docker.sock ]; then echo LEAK_SOCK; exit 6; fi\n"
        "if [ -n \"${GITHUB_TOKEN:-}\" ] || [ -n \"${BATON_ACCESS_KEY:-}\" ]; then echo LEAK_ENV; exit 7; fi\n"
        "echo CONFINED_OK\n"
    )
    cmd = sandbox.sandbox_docker_command(
        workspace, "alpine:3.20", "{}",
        container_name=sandbox.safe_container_name("test-iso"),
        cmd=["sh", "-c", script],
    )
    env = {**os.environ, "GITHUB_TOKEN": "ghp_should_not_enter", "BATON_ACCESS_KEY": "key_should_not_enter"}
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    assert res.returncode == 0, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert "CONFINED_OK" in res.stdout
    assert "TOP_SECRET_SIBLING_DATA" not in res.stdout
    assert (workspace / "by-parent.txt").read_text(encoding="utf-8").strip() == "from-parent"
    assert (workspace / "by-child.txt").read_text(encoding="utf-8").strip() == "from-child"
    assert secret.read_text(encoding="utf-8").strip() == "TOP_SECRET_SIBLING_DATA"


FAKE_CLAUDE = Path(__file__).with_name("fake_claude_chat.py")
FAKE_CODEX = Path(__file__).with_name("fake_codex_app_server.py")


def test_sandbox_agent_docker_command_and_login_isolation(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    all_vols = {a: p.volume for a, p in sandbox.AGENT_SANDBOX_PROFILES.items()}

    for agent, profile in sandbox.AGENT_SANDBOX_PROFILES.items():
        cmd = sandbox.sandbox_agent_docker_command(
            agent, ws, "baton-agents-sandbox:latest", ["echo", "ok"],
            container_name=f"baton-sandbox-{agent}", docker_bin="docker",
        )
        assert cmd[:5] == ["docker", "run", "--rm", "-i", "--init"]
        assert "--cap-drop=ALL" in cmd
        assert "--security-opt=no-new-privileges:true" in cmd
        assert "--pids-limit=256" in cmd
        assert "--memory=4g" in cmd
        assert f"type=bind,src={os.path.realpath(ws)},dst=/workspace" in cmd
        assert f"type=volume,src={profile.volume},dst={profile.mount_dst}" in cmd
        assert cmd[cmd.index("-w") + 1] == "/workspace"
        joined = " ".join(cmd)
        assert "docker.sock" not in joined
        assert "GITHUB_TOKEN" not in joined
        assert "BATON_ACCESS_KEY" not in joined
        # One agent's container never mounts another agent's auth volume.
        for other, other_vol in all_vols.items():
            if other != agent:
                assert other_vol not in joined

        login = sandbox.sandbox_login_command(agent, "baton-agents-sandbox:latest", docker_bin="docker")
        assert login[:5] == ["docker", "run", "--rm", "-it", "--init"]
        assert "--cap-drop=ALL" in login
        assert "--security-opt=no-new-privileges:true" in login
        assert f"type=volume,src={profile.volume},dst={profile.mount_dst}" in login
        assert "type=bind" not in " ".join(login)
        assert login[-len(profile.login_cmd):] == list(profile.login_cmd)

    with pytest.raises(ValueError):
        sandbox.sandbox_agent_docker_command("unknown", ws, "img", ["sh"], container_name="c")
    with pytest.raises(ValueError):
        sandbox.sandbox_login_command("unknown", "img")


def test_claude_codex_gemini_full_mode_sandbox_end_to_end(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []

    catalogs = {
        "claude-code": {"claude-sonnet-4-6": {"label": "Claude Sonnet 4.6", "efforts": ["low", "high"]}},
        "codex": {"gpt-6.2-sol": {"label": "GPT-6.2 Sol", "efforts": ["low", "high"]}},
        "gemini": {"gemini-4-neon": {"label": "Gemini 4 Neon", "efforts": ["low", "high"]}},
    }
    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_claude", lambda: "claude")
    monkeypatch.setattr(web, "find_codex", lambda: "codex")
    monkeypatch.setattr(web, "find_agy", lambda: "agy")
    monkeypatch.setattr(web, "catalog", lambda agent: catalogs.get(agent, {}))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_agents")
    monkeypatch.setenv("BATON_ACCESS_KEY", "secret_key_agents")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk_ant_secret_agents")

    scratch = tmp_path / "scratch"
    app = create_app(Settings(
        data_dir=tmp_path / "data", lang="en", self_update=False,
        chat_scratch=str(scratch), sandbox_image="", agents_sandbox_image="",
    ))
    chats = app.state.agent_chats
    seen_calls: list[tuple[str, Path, str, list[str]]] = []

    def fake_agent_sandbox(agent: str, folder: Path, cname: str, inner: list[str]) -> list[str]:
        seen_calls.append((agent, folder, cname, list(inner)))
        if agent == "claude-code":
            return [sys.executable, str(FAKE_CLAUDE), *inner[1:]]
        if agent == "codex":
            return [sys.executable, str(FAKE_CODEX)]
        return [sys.executable, str(FAKE_ACP), "--gemini"]

    chats.agent_sandbox_command = fake_agent_sandbox

    def wait_idle(chat, seconds: float = 10.0) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline and chat.busy:
            time.sleep(0.02)
        assert not chat.busy

    def get_events(client: TestClient, cid: str) -> list[dict]:
        resp = client.get(f"/api/agents/{cid}/events?after=0")
        assert resp.status_code == 200
        return [json.loads(line[6:]) for line in resp.text.splitlines()
                if line.startswith("data: ") and line != "data: {}"]

    with TestClient(app) as client:
        # 1. Claude Code Full mode inside Docker sandbox.
        p_claude = client.get("/chat?agent=claude-code").text
        assert 'data-sandbox-ready="1"' in p_claude
        assert 'value="full" data-mode' in p_claude
        assert EN["ag_full_help_agent"] in p_claude
        csrf = p_claude.split('data-csrf="')[1].split('"')[0]
        headers = {"X-Baton-CSRF": csrf}

        r = client.post("/api/agents", json={
            "agent": "claude-code", "model": "claude-sonnet-4-6",
            "effort": "high", "folder": "scratch", "mode": "full",
        }, headers=headers)
        assert r.status_code == 200, r.text
        info = r.json()
        assert info["mode"] == "full" and info["isolated"] is True
        cid = info["id"]
        chat = chats.get(cid)
        assert seen_calls[-1][0] == "claude-code"
        assert "--dangerously-skip-permissions" in seen_calls[-1][3]
        assert "--permission-mode" not in seen_calls[-1][3]

        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "write claude_full.txt ok"}, headers=headers).status_code == 202
        wait_idle(chat)
        assert (scratch / "claude_full.txt").read_text(encoding="utf-8") == "ok\n"
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "run echo hi"}, headers=headers).status_code == 202
        wait_idle(chat)
        assert client.post(f"/api/agents/{cid}/prompt", json={"text": "env"}, headers=headers).status_code == 202
        wait_idle(chat)
        evs = get_events(client, cid)
        assert not [e for e in evs if e["type"] == "permission"]
        raw = json.dumps(evs)
        assert "leaked: none" in raw and "SECRET_CLAUDE_EMAIL" not in raw
        assert client.post(f"/api/agents/{cid}/close", headers=headers).status_code == 200

        # 2. Codex Full mode inside Docker sandbox.
        p_codex = client.get("/chat?agent=codex").text
        assert 'data-sandbox-ready="1"' in p_codex
        r2 = client.post("/api/agents", json={
            "agent": "codex", "model": "gpt-6.2-sol",
            "effort": "low", "folder": "scratch", "mode": "full",
        }, headers=headers)
        assert r2.status_code == 200, r2.text
        info2 = r2.json()
        assert info2["mode"] == "full" and info2["isolated"] is True
        cid2 = info2["id"]
        chat2 = chats.get(cid2)
        assert seen_calls[-1][0] == "codex" and seen_calls[-1][3] == ["codex", "app-server"]

        assert client.post(f"/api/agents/{cid2}/prompt", json={"text": "edit codex_full.txt done"}, headers=headers).status_code == 202
        wait_idle(chat2)
        assert (scratch / "codex_full.txt").read_text(encoding="utf-8") == "done\n"
        assert client.post(f"/api/agents/{cid2}/prompt", json={"text": "run echo codex-ok"}, headers=headers).status_code == 202
        wait_idle(chat2)
        assert (scratch / "accepted.txt").read_text(encoding="utf-8").strip() == "echo codex-ok"
        assert client.post(f"/api/agents/{cid2}/prompt", json={"text": "env"}, headers=headers).status_code == 202
        wait_idle(chat2)
        evs2 = get_events(client, cid2)
        assert not [e for e in evs2 if e["type"] == "permission"]
        raw2 = json.dumps(evs2)
        assert "leaked: none" in raw2 and "SECRET_PLAN_PRO" not in raw2
        assert client.post(f"/api/agents/{cid2}/close", headers=headers).status_code == 200

        # 3. Gemini (agy) Full mode inside Docker sandbox.
        p_gem = client.get("/chat?agent=gemini").text
        assert 'data-sandbox-ready="1"' in p_gem
        r3 = client.post("/api/agents", json={
            "agent": "gemini", "model": "gemini-4-neon",
            "effort": "high", "folder": "scratch", "mode": "full",
        }, headers=headers)
        assert r3.status_code == 200, r3.text
        info3 = r3.json()
        assert info3["mode"] == "full" and info3["isolated"] is True
        cid3 = info3["id"]
        chat3 = chats.get(cid3)
        assert seen_calls[-1][0] == "gemini"
        assert seen_calls[-1][3] == ["agy", "acp", "--dangerously-skip-permissions"]

        assert client.post(f"/api/agents/{cid3}/prompt", json={"text": "write gem_full.txt ok"}, headers=headers).status_code == 202
        wait_idle(chat3)
        assert (scratch / "gem_full.txt").read_text(encoding="utf-8") == "ok\n"
        assert client.post(f"/api/agents/{cid3}/prompt", json={"text": "run echo gem-ok"}, headers=headers).status_code == 202
        wait_idle(chat3)
        assert client.post(f"/api/agents/{cid3}/prompt", json={"text": "env"}, headers=headers).status_code == 202
        wait_idle(chat3)
        evs3 = get_events(client, cid3)
        assert not [e for e in evs3 if e["type"] == "permission"]
        gem_text = "".join(e["text"] for e in evs3 if e["type"] == "text")
        assert "leaked: none" in gem_text
        assert client.post(f"/api/agents/{cid3}/close", headers=headers).status_code == 200

        # 4. Hardlinks refuse Full mode for Claude Code, Codex, and Gemini too.
        sibling = tmp_path / "sibling_agents"
        sibling.mkdir()
        secret = sibling / "secret.txt"
        secret.write_text("SIBLING\n", encoding="utf-8")
        os.link(str(secret), str(scratch / "hl.txt"))
        for ag, mod in [("claude-code", "claude-sonnet-4-6"), ("codex", "gpt-6.2-sol"), ("gemini", "gemini-4-neon")]:
            rh = client.post("/api/agents", json={
                "agent": ag, "model": mod, "effort": "low", "folder": "scratch", "mode": "full",
            }, headers=headers)
            assert rh.status_code == 403 and EN["ag_full_hardlink"] in rh.json()["detail"]


def test_full_mode_fails_closed_without_isolated_flag_and_stops_containers(tmp_path, monkeypatch):
    from baton import chat_claude, chat_codex
    from baton.agent_chat import AcpError, AgentChat

    with pytest.raises(AcpError):
        chat_claude.ClaudeChat(
            id="c1", folder=tmp_path, folder_label="ws", model="claude-sonnet-4-6",
            effort="low", mode="full", isolated=False,
        ).start(["echo"], {})
    with pytest.raises(AcpError):
        chat_codex.CodexChat(
            id="c2", folder=tmp_path, folder_label="ws", model="gpt-6.2-sol",
            effort="low", mode="full", isolated=False,
        ).start(["echo"], {})
    with pytest.raises(AcpError):
        AgentChat(
            id="c3", folder=tmp_path, folder_label="ws", model="gemini-4-neon",
            effort="low", mode="full", agent="gemini", isolated=False,
        ).start(["echo"], {})

    stopped: list[str] = []
    monkeypatch.setattr(chat_claude, "stop_container", stopped.append)
    monkeypatch.setattr(chat_codex, "stop_container", stopped.append)
    chat_claude.ClaudeChat(
        id="c4", folder=tmp_path, folder_label="ws", model="m", effort="low", container_name="box-claude",
    ).close()
    chat_codex.CodexChat(
        id="c5", folder=tmp_path, folder_label="ws", model="m", effort="low", container_name="box-codex",
    ).close()
    assert stopped == ["box-claude", "box-codex"]


def test_cli_sandbox_login(monkeypatch, capsys):
    from baton import __main__ as cli

    monkeypatch.setattr(cli, "_settings", lambda: Settings())
    monkeypatch.setattr(sandbox, "find_docker", lambda: None)
    assert cli.main(["sandbox-login", "codex"]) == 1
    assert "Docker CLI" in capsys.readouterr().err

    monkeypatch.setattr(sandbox, "find_docker", lambda: "docker")
    monkeypatch.setattr(sandbox, "docker_available", lambda img: False)
    assert cli.main(["sandbox-login", "claude-code"]) == 1
    err = capsys.readouterr().err
    assert "docker build -f docker/agents-sandbox.Dockerfile" in err

    ran: list[list[str]] = []

    class _Done:
        returncode = 0

    monkeypatch.setattr(sandbox, "docker_available", lambda img: True)
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd: (ran.append(cmd), _Done())[1])
    assert cli.main(["sandbox-login", "codex", "--image", "custom:latest"]) == 0
    assert ran and ran[0][-3:] == ["codex", "login", "--device-auth"]
    assert "custom:latest" in ran[0]

