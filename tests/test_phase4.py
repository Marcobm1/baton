"""Phase 4 tests: open-source configurability (owner tag, scratch folder, scheduled task)."""

from __future__ import annotations

import base64
import json
import subprocess
from unittest.mock import Mock

import httpx
import pytest

from baton import __main__ as cli
from baton.agents import build_prompt, next_task_request
from baton.config import Settings
from baton.github import Hub, Project
from baton.ping import agent_env
from baton.project_detail import DEFAULT_OWNER_TAG, normalize_owner_tag
from baton.runner import LocalRepos, Runner, Task, TaskStore
from baton.status import parse


def test_phase4_env_defaults_and_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("BATON_ENV_FILE", str(tmp_path / "missing.env"))
    for key in ("BATON_OWNER_TAG", "BATON_CHAT_SCRATCH", "BATON_SCHEDULED_TASK", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BATON_USE_GH_CLI", "0")

    s = Settings.from_env()
    assert s.owner_tag == DEFAULT_OWNER_TAG == "(Owner)"
    assert s.chat_scratch == ""
    assert s.scheduled_task == "Baton"

    monkeypatch.setenv("BATON_OWNER_TAG", "Alice")
    monkeypatch.setenv("BATON_CHAT_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.setenv("BATON_SCHEDULED_TASK", "Baton Prod")
    s2 = Settings.from_env()
    assert s2.owner_tag == "(Alice)"
    assert s2.chat_scratch == str(tmp_path / "scratch")
    assert s2.scheduled_task == "Baton Prod"

    assert normalize_owner_tag("") == "(Owner)"
    assert normalize_owner_tag("Alice") == "(Alice)"
    assert normalize_owner_tag("(Alice)") == "(Alice)"
    assert normalize_owner_tag("[Human]") == "[Human]"
    assert normalize_owner_tag("OWNER:") == "OWNER:"


def test_custom_owner_tag_parsing_and_hub_completion(tmp_path, monkeypatch):
    source = (
        "---\nname: Demo\nstatus: active\nupdated: 2026-11-25\nnext: (Alice) Review\n---\n"
        "## Next\n"
        "- [ ] Ship feature\n"
        "- [ ] (Alice) Review release\n"
        "- [ ] (Owner) Regular task when tag is Alice\n"
    )
    st = parse(source, owner_tag="(Alice)")
    assert [i.title for i in st.sections.owner_actions] == ["(Alice) Review release"]
    assert [i.title for i in st.sections.upcoming] == [
        "Ship feature",
        "(Owner) Regular task when tag is Alice",
    ]
    assert st.next_task == "Ship feature"

    only_owner = parse(
        "---\nname: Demo\nstatus: active\nupdated: 2026-11-25\nnext: (Alice) Review\n---\n"
        "## Next\n- [ ] (Alice) Review\n",
        owner_tag="Alice",
    )
    assert only_owner.next_task == ""

    # Hub.complete_item keeps respecting custom owner_tag when advancing `next:`.
    writes = []
    project = Project("u/demo", "u/demo", "STATUS.md", "main", False, "", None, st)
    hub = Hub(Settings(data_dir=tmp_path, owner_tag="(Alice)"))

    def fake_github(request):
        writes.append(json.loads(request.content))
        return httpx.Response(200)

    monkeypatch.setattr(
        Hub,
        "_client",
        lambda self: httpx.AsyncClient(
            base_url="https://api.github.com", transport=httpx.MockTransport(fake_github)
        ),
    )

    import asyncio

    ship_line = st.sections.upcoming[0].line
    asyncio.run(hub.complete_item(project, ship_line, project.status_sha))
    written = base64.b64decode(writes[0]["content"]).decode()
    assert "next: (Owner) Regular task when tag is Alice\n" in written


def test_custom_owner_tag_flows_into_agent_prompts(tmp_path):
    req_default = next_task_request()
    assert "Prefix owner-only actions with (Owner)." in req_default
    assert "(Marco)" not in req_default

    req_custom = next_task_request("codex", owner_tag="Alice")
    assert "Prefix owner-only actions with (Alice)." in req_custom

    task = Task(
        id="t1", repo="u/demo", title="Clean docs", details="",
        agent="codex", where="local", created_at=1.0, branch="baton/t1", base="main",
    )
    prompt = build_prompt(task, owner_tag="(Alice)")
    assert "Prefix owner-only actions with (Alice)." in prompt

    runner = Runner(
        TaskStore(tmp_path), LocalRepos("", ""),
        timeout=60, local_model="", local_provider="ollama",
        git_name="Baton", git_email="baton@localhost",
        open_pr=lambda *_: "", owner_tag="(Alice)",
    )
    assert "Prefix owner-only actions with (Alice)." in runner._prompt(task)


def test_windows_restart_custom_task_and_injection_guard(tmp_path, monkeypatch):
    query, spawn = Mock(), Mock()
    monkeypatch.setattr(subprocess, "run", query)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    for flag in ("CREATE_NO_WINDOW", "CREATE_BREAKAWAY_FROM_JOB"):
        monkeypatch.setattr(subprocess, flag, 1, raising=False)

    cli.windows_restart(tmp_path, "Baton Staging")
    assert query.call_args.args[0] == ["schtasks", "/Query", "/TN", "Baton Staging"]
    script = spawn.call_args.args[0][-1]
    assert "GetTask('Baton Staging')" in script
    assert "schtasks /Run /TN 'Baton Staging'" in script

    for bad in ("", "Baton'; calc.exe; '", "Baton$(whoami)", "Folder\\Baton", "bad\nname"):
        with pytest.raises(ValueError):
            cli.windows_restart(tmp_path, bad)


def test_agent_env_strips_phase4_vars(monkeypatch):
    monkeypatch.setenv("BATON_OWNER_TAG", "(Alice)")
    monkeypatch.setenv("BATON_CHAT_SCRATCH", "/tmp/scratch")
    monkeypatch.setenv("BATON_SCHEDULED_TASK", "Baton")
    env = agent_env()
    assert "BATON_OWNER_TAG" not in env
    assert "BATON_CHAT_SCRATCH" not in env
    assert "BATON_SCHEDULED_TASK" not in env
