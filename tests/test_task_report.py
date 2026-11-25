import json
from html import unescape

import pytest
from fastapi.testclient import TestClient

from baton.config import Settings
from baton.runner import Task, TaskStore
from baton.web import create_app


@pytest.fixture
def detail(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es")))
    store = TaskStore(tmp_path)

    def render(status, output="", **fields):
        agent = fields.pop("agent", "claude-code")
        task = Task("old", "x/demo", "Fix the parser", "", agent, "local", 0,
                    status=status, **fields)
        store.save(task)
        store.log_path(task.id).write_text(output, encoding="utf-8")
        response = client.get("/tasks/old")
        assert response.status_code == 200
        return response.text
    return render


@pytest.mark.parametrize("reason,advice", [
    ("Your account does not have access to Fable.", "otro modelo"),
    ("Mythos requires extra usage on this plan.", "otro modelo"),
    ("You've hit your weekly limit. Resets Friday.", "cuando se restablezca"),
    ("Connection to server failed for model Fable.", "Revisa la respuesta"),
    ("Permission denied reading a file.", "Revisa la respuesta"),
])
def test_simulated_claude_errors(detail, reason, advice):
    log = f"== Running Claude Code…\n{reason}\n== Agent finished (exit code 1)\n!! exit code 1"
    html = detail("failed", log, model="Fable")
    overview = html.split("<details")[0]
    assert advice in overview and "no comunicó comprobaciones" in overview
    assert reason in unescape(overview)


def test_old_success_and_remote_branch(detail):
    log = "== Running Claude Code…\nFixed escaping.\n12 passed in 2s\n== Agent finished (exit code 0)"
    html = detail("done", log, branch="baton/old", steps=["12:00:00 Branch pushed to GitHub"])
    overview, details = html.split("<details")
    assert "Fixed escaping." in overview and "12 passed" in overview
    assert 'href="https://github.com/x/demo/tree/baton/old"' in overview
    assert 'class="log"' in details


def test_queue_and_running(detail):
    html = detail("queued", base="main")
    assert "Pendiente de iniciar" in html and "copia local" in html and "main" in html
    html = detail("running", steps=["12:00:00 Running Claude Code…"])
    assert "12:00:00 Running" in html and "no ha comunicado su progreso" in html
    html = detail("running", "== Running Claude Code…\nProgress: Reading tests.\n")
    assert "Reading tests." in html.split("<details")[0]


def test_old_record_loads_without_optional_fields(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es")))
    (tmp_path / "tasks.json").write_text(json.dumps([dict(
        id="old", repo="x/demo", title="Review", details="", agent="codex",
        where="local", created_at=0, status="no-changes")]))
    html = client.get("/tasks/old").text
    assert "sin cambios" in html and "no comunicó comprobaciones" in html

def test_git_failure_after_claude_success(detail):
    log = ("== Running Claude Code…\nSummary: Documented the usage limit.\n"
           "Checks: Reviewed the example.\n== Agent finished (exit code 0)\n"
           "!! git push failed")
    overview = detail("failed", log, message="git push failed").split("<details")[0]
    assert "git push failed" in overview and "Documented the usage limit." in overview
    assert "Reviewed the example." in overview and "cuando se restablezca" not in overview


def test_opencode_go_insufficient_balance_error_and_suggestion(tmp_path, monkeypatch):
    from baton import task_report as tr

    monkeypatch.setattr(tr, "cached_opencode_models", lambda: [
        "opencode-go/gemini-4-argon",
        "opencode-go/glm-5.3",
        "opencode/gemini-4-neon",
    ])
    error_line = "Insufficient balance ... OpenCode Go covers only requests to the OpenCode Go provider"
    log = (f"== Running OpenCode (opencode/gemini-4-neon)…\n{error_line}\n"
           "== Agent finished (exit code 1)\n!! The agent stopped with exit code 1; see the log.")

    for lang, expected_msg, expected_sug in (
        ("en", "This model is not included in OpenCode Go; pick an opencode-go/ model or add credits",
         "Closest included model: opencode-go/gemini-4-argon"),
        ("es", "Este modelo no está incluido en OpenCode Go; elige un modelo opencode-go/ o añade créditos",
         "Modelo incluido más cercano: opencode-go/gemini-4-argon"),
    ):
        client = TestClient(create_app(Settings(data_dir=tmp_path, lang=lang)))
        store = TaskStore(tmp_path)
        task = Task("oc-fail", "x/demo", "Tell models apart", "", "opencode", "local", 0,
                    status="failed", model="opencode/gemini-4-neon",
                    message="The agent stopped with exit code 1; see the log.")
        store.save(task)
        store.log_path(task.id).write_text(log, encoding="utf-8")
        overview = unescape(client.get("/tasks/oc-fail").text.split("<details")[0])
        assert expected_msg in overview
        assert expected_sug in overview
        assert error_line in overview
        assert "stopped with exit code 1" not in overview
