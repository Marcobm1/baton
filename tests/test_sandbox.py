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


@pytest.mark.skipif(
    not sandbox.docker_available("alpine:3.20"),
    reason="Real Docker daemon with alpine:3.20 image not available on this runner",
)
def test_real_docker_container_confines_workspace_and_blocks_sibling_and_symlink(tmp_path):
    """Acceptance check from docs/agents-chat.md: two temporary sibling folders + symlink."""
    workspace = tmp_path / "workspace"
    sibling = tmp_path / "sibling"
    workspace.mkdir()
    sibling.mkdir()
    secret = sibling / "secret.txt"
    secret.write_text("TOP_SECRET_SIBLING_DATA\n", encoding="utf-8")
    escape_link = workspace / "escape_link"
    os.symlink(str(secret), str(escape_link))

    script = (
        "set -eu\n"
        "echo from-parent > /workspace/by-parent.txt\n"
        "sh -c 'echo from-child > /workspace/by-child.txt'\n"
        "if cat ../sibling/secret.txt 2>/dev/null; then echo LEAK_REL; exit 2; fi\n"
        f"if cat {str(secret)!r} 2>/dev/null; then echo LEAK_ABS; exit 3; fi\n"
        "if cat /workspace/escape_link 2>/dev/null; then echo LEAK_LINK; exit 4; fi\n"
        "if sh -c 'cat /workspace/escape_link 2>/dev/null'; then echo LEAK_CHILD_LINK; exit 5; fi\n"
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
