"""Gemini through the Antigravity CLI (agy): models, task command, report and
tokens, quota windows and the Agents chat. Real agy output (1.5.17, 2026-11-25)
is used as fixtures; no test runs agy itself."""
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton import agent_chat, github, ping, quotas, runner as runner_mod, usage, web
from baton.config import Settings
from baton.project_detail import recommendation
from baton.runner import LocalRepos, Runner, Task, TaskStore
from baton.task_models import parse_agy_models

from test_runner import _wait, make_repo

FAKE_ACP = Path(__file__).with_name("fake_acp_agent.py")

AGY_MODELS = """Fetching available models...
gemini-4-neon-high\tGemini 4 Neon (High)
gemini-4-neon-xhigh\tGemini 4 Neon (XHigh)
gemini-4-neon-medium\tGemini 4 Neon (Medium)
gemini-4-neon-low\tGemini 4 Neon (Low)
gemini-4-argon-medium\tGemini 4 Argon (Medium)
gemini-4-argon-low\tGemini 4 Argon (Low)
gemini-pro-orchestrator-neon\tOrchestrator (Neon)
gemini-pro-orchestrator\tOrchestrator (Carbon)
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-opus-5-5-high\tClaude Opus 5.5 (High)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""

AGY_USAGE = json.dumps({
    "conversation_id": "", "status": "SUCCESS", "num_turns": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "cache_read_tokens": 0},
    "command": {"name": "usage", "data": {"groups": [
        {"name": "Gemini Models", "buckets": [
            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.75,
             "reset_time": "2026-12-02T15:53:03Z"},
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.4,
             "reset_time": "2026-11-25T20:53:03Z"}]},
        {"name": "Claude and GPT models", "buckets": [
            {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 1,
             "reset_time": "1970-01-01T00:00:00Z"}]}]}}})


def test_agy_models_become_base_models_with_their_thinking_levels():
    models = parse_agy_models(AGY_MODELS)
    assert list(models) == ["gemini-4-neon", "gemini-4-argon", "gemini-pro-orchestrator-neon",
                            "gemini-pro-orchestrator", "gemini-3.8-flash"]
    assert models["gemini-4-neon"] == {"label": "Gemini 4 Neon", "efforts": ["low", "medium", "high", "xhigh"]}
    assert models["gemini-pro-orchestrator"] == {"label": "Orchestrator (Carbon)", "efforts": []}
    assert models["gemini-3.8-flash"]["efforts"] == ["low", "high"]
    # Claude and GPT through agy are left out: they have their own CLIs.
    assert not any(m.startswith(("claude", "gpt")) for m in models)


def _runner(tmp_path, **kw):
    return Runner(TaskStore(tmp_path / "data"), LocalRepos("", ""), timeout=60, local_model="qwen",
                  local_provider="ollama", git_name="B", git_email="b@x", open_pr=lambda *a: "", **kw)


def test_commands_give_full_permissions_only_to_claude_codex_and_gemini(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "find_agy", lambda: r"C:\agy\agy.exe")
    monkeypatch.setattr(runner_mod, "find_claude", lambda: "claude")
    monkeypatch.setattr(runner_mod, "find_codex", lambda: "codex")
    monkeypatch.setattr(runner_mod, "catalog", lambda a: {})
    monkeypatch.setattr(runner_mod, "opencode_models", lambda: [])
    r = _runner(tmp_path)
    r.agents_available = lambda: {}

    def cmd(agent, restricted=False, **kw):
        task = Task(id="t", repo="x/demo", title="A & B | C", details="line1\nline2", agent=agent,
                    where="local", created_at=0, branch="baton/t", base="main", **kw)
        return r._agent_cmd(task, tmp_path, restricted=restricted)

    gemini = cmd("gemini", model="gemini-4-neon", effort="high")
    assert gemini[:2] == [r"C:\agy\agy.exe", "-p"] and "A & B | C" in gemini[2]
    assert gemini[3:] == ["--output-format", "stream-json", "--dangerously-skip-permissions",
                          "--model", "gemini-4-neon", "--effort", "high"]
    assert cmd("claude-code")[:3] == ["claude", "-p", "--dangerously-skip-permissions"]
    codex = cmd("codex")
    assert codex[:4] == ["codex", "--search", "exec", "--dangerously-bypass-approvals-and-sandbox"]
    assert "-s" not in codex
    # Reviews (restricted) and local AI keep the limited modes.
    assert cmd("claude-code", restricted=True)[:4] == ["claude", "-p", "--permission-mode", "auto"]
    assert cmd("codex", restricted=True)[1:4] == ["exec", "-s", "workspace-write"]
    local = cmd("local", model="qwen")
    assert "--oss" in local and local[local.index("-s") + 1] == "workspace-write"
    assert not any("dangerously" in part for part in local)


def test_gemini_task_report_and_tokens_come_from_the_result_event(tmp_path, monkeypatch):
    work, _ = make_repo(tmp_path)
    monkeypatch.setattr(runner_mod, "_remote_repo", lambda url: "x/demo" if url.strip() else "")
    report = "BATON_SUMMARY_BEGIN\nSummary: added DONE.md.\nChecks: none.\nBATON_SUMMARY_END\n"
    events = [
        {"event": "init", "conversation_id": "c1", "init": {"model": "gemini-4-neon", "permission_mode": "x"}},
        {"event": "step_update", "step_update": {"text_delta": report}},
        {"event": "result", "result": {"status": "SUCCESS", "response": report, "usage": {
            "input_tokens": 1000, "output_tokens": 200, "thinking_tokens": 50, "cache_read_tokens": 3000}}},
    ]
    script = ("import sys; open('DONE.md','w').write('x'); "
              + "; ".join(f"print({json.dumps(json.dumps(e))})" for e in events))
    opened = []
    store = TaskStore(tmp_path / "data")
    r = Runner(store, LocalRepos(str(work.parent), ""), timeout=60, local_model="", local_provider="ollama",
               git_name="Baton", git_email="baton@localhost",
               open_pr=lambda *a: opened.append(a) or "https://github.com/x/demo/pull/1")
    seen = []
    r._agent_cmd = lambda task, wt: seen.append(task) or [sys.executable, "-c", script]
    r.submit(Task(id="g1", repo="x/demo", title="Add DONE", details="", agent="gemini", where="local",
                  created_at=time.time(), branch="baton/g1", base="main", model=""))
    done = _wait(store, "g1")
    assert done.status == "done", done.message
    assert done.summary == "Summary: added DONE.md.\nChecks: none."
    row, = [json.loads(line) for line in (tmp_path / "data" / usage.GEMINI_LOG).read_text().splitlines()]
    assert (row["model"], row["source"], row["input"], row["output"], row["thinking"], row["cache_read"]) == (
        "gemini-4-neon", "task", 1000, 200, 50, 3000)
    assert "**Gemini**" in opened[0][4]

    # The Quotas page counts them per day, week and model (output includes thinking).
    data = usage.collect(tmp_path / "no-claude", tmp_path / "no-codex", tmp_path / "no.db",
                         data_dir=tmp_path / "data")
    app, = data["apps"]
    assert app["app"] == "gemini" and app["today"].total == 4250
    assert (app["week"].fresh, app["week"].cache, app["week"].output) == (1000, 3000, 250)
    assert app["models"][0][0] == "gemini-4-neon"


def test_agy_usage_becomes_quota_windows(tmp_path):
    windows = ping.parse_agy_usage(AGY_USAGE)
    assert windows == {"5h": {"left": 40.0, "resets_at": "2026-11-25T20:53:03Z"},
                       "week": {"left": 75.0, "resets_at": "2026-12-02T15:53:03Z"}}
    assert ping.parse_agy_usage("not json") is None
    (tmp_path / "gemini-quota.json").write_text(json.dumps({"seen_at": time.time(), "windows": windows}))
    views = {v["kind"]: v for v in quotas.all_windows(tmp_path, tmp_path / "no-codex")
             if v["provider"] == "gemini"}
    assert views["5h"]["left"] == 40.0 and views["week"]["used"] == 25.0
    assert views["5h"]["source"] == "agy-usage"


def test_fetch_gemini_quota_runs_usage_without_a_model(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return ping.subprocess.CompletedProcess(cmd, 0, stdout=AGY_USAGE, stderr="")

    monkeypatch.setattr(ping.subprocess, "run", fake_run)
    assert ping.fetch_gemini_quota(tmp_path, binary="agy") == (True, "ok")
    assert calls[0][:5] == ["agy", "-p", "/usage", "--output-format", "json"]
    saved = json.loads((tmp_path / "gemini-quota.json").read_text())
    assert saved["windows"]["5h"]["left"] == 40.0


def test_find_agy_uses_the_winget_folder(tmp_path, monkeypatch):
    exe = tmp_path / "Microsoft" / "WinGet" / "Packages" / "Google.AntigravityCLI_x" / "agy.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.delenv("BATON_AGY_BIN", raising=False)
    monkeypatch.setattr(ping.shutil, "which", lambda name: None)
    monkeypatch.setattr(ping.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ping.find_agy() == str(exe)
    monkeypatch.setenv("BATON_AGY_BIN", "custom-agy")
    assert ping.find_agy() == "custom-agy"


def test_recommended_line_can_name_gemini():
    rec, rest = recommendation("Do it.\nRecommended: gemini, gemini-4-neon, high")
    assert rec == {"agent": "gemini", "model": "gemini-4-neon", "effort": "high"} and rest == "Do it."


# ----------------------------------------------------------------- chat
CATALOG = {"gemini-4-neon": {"label": "Gemini 4 Neon", "efforts": ["low", "medium", "high"]},
           "gemini-3.8-flash": {"label": "Gemini 3.8 Flash", "efforts": ["low", "high"]}}


@pytest.fixture
def chat_env(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []
    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_agy", lambda: "agy")
    monkeypatch.setattr(web, "catalog", lambda agent: CATALOG if agent == "gemini" else {})
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    scratch = tmp_path / "scratch"
    app = web.create_app(Settings(data_dir=tmp_path / "data", lang="es", self_update=False,
                                  chat_scratch=str(scratch)))
    chats = app.state.agent_chats
    chats.gemini_command = lambda: [sys.executable, str(FAKE_ACP), "--gemini"]
    with TestClient(app) as client:
        page = client.get("/chat?agent=gemini").text
        csrf = page.split('data-csrf="')[1].split('"')[0]
        yield {"client": client, "chats": chats, "page": page, "data": tmp_path / "data",
               "headers": {"X-Baton-CSRF": csrf}}
    assert not chats._chats


def test_chat_page_offers_gemini_models_with_their_levels(chat_env):
    page = chat_env["page"]
    assert 'value="gemini" selected' in page and 'data-agent="gemini"' in page
    assert '<option value="gemini-4-neon"' in page and '<option value="gemini-3.8-flash"' in page
    assert "data-efforts=" in page and "&#34;gemini-3.8-flash&#34;: [&#34;low&#34;, &#34;high&#34;]" in page
    assert "Modo Default de Antigravity" in page and 'value="full" disabled data-mode' in page


def test_gemini_chat_sets_default_mode_model_and_thinking_level_and_counts_tokens(chat_env):
    client, headers = chat_env["client"], chat_env["headers"]
    body = {"agent": "gemini", "model": "gemini-3.8-flash", "effort": "low", "folder": "scratch",
            "mode": "confirm"}
    assert client.post("/api/agents", json={**body, "model": "opencode-go/glm-5.3"},
                       headers=headers).status_code == 400
    r = client.post("/api/agents", json=body, headers=headers)
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["agent"] == "gemini" and info["model"] == "gemini-3.8-flash"
    chat = chat_env["chats"].get(info["id"])
    assert chat.client.proc.args[-1] == "--gemini"
    assert client.post(f"/api/agents/{info['id']}/prompt", json={"text": "hola"},
                       headers=headers).status_code == 202
    deadline = time.time() + 15
    while chat.busy and time.time() < deadline:
        time.sleep(0.05)
    text = "".join(e["text"] for e in chat.events if e["type"] == "text")
    # Fail closed: Baton moved agy from "auto" to its asking "default" mode.
    assert text.endswith("model gemini-3.8-flash, effort low, mode default)")
    row, = [json.loads(x) for x in (chat_env["data"] / usage.GEMINI_LOG).read_text().splitlines()]
    assert (row["model"], row["source"], row["input"], row["output"], row["thinking"], row["cache_read"]) == (
        "gemini-3.8-flash", "chat", 120, 30, 5, 400)
    # Its environment has no GitHub token or Baton settings (and no OpenCode config).
    env = agent_chat.chat_env("gemini")
    assert "GITHUB_TOKEN" not in env and "OPENCODE_CONFIG_CONTENT" not in env
