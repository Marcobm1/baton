"""Tests for Phase 3C: readable Gemini logs, live task SSE, and background usage.collect()."""

from __future__ import annotations

import json
import threading
import time

from fastapi.testclient import TestClient

from baton import usage, web
from baton.config import Settings
from baton.task_report import format_gemini_log, format_task_log, task_report
from baton.task_store import Task, TaskStore


def _saved_agy_log() -> str:
    events = [
        {"event": "init", "conversation_id": "c1", "init": {"model": "gemini-4-neon", "permission_mode": "auto"}},
        {"event": "step_update", "step_update": {"text_delta": "Progress: Reading tests.\n"}},
        {"event": "step_update", "step_update": {"tool_call": {"name": "read_file", "arguments": {"path": "baton/web.py"}}}},
        {"event": "step_update", "step_update": {"tool_call": {"name": "run_command", "command": "pytest -q", "status": "completed"}}},
        {"event": "step_update", "step_update": {"text_delta": "Summary: Added readable Gemini logs.\n"}},
        {"event": "step_update", "step_update": {"text_delta": "Checks: 12 passed.\n"}},
        {"event": "result", "result": {
            "status": "SUCCESS",
            "response": "Summary: Added readable Gemini logs.\nChecks: 12 passed.\n",
            "usage": {"input_tokens": 1200, "output_tokens": 340, "thinking_tokens": 80, "cache_read_tokens": 4500},
        }},
    ]
    return "\n".join([
        "== Local clone: /tmp/repo",
        "== Running Gemini (gemini-4-neon)…",
        *(json.dumps(e) for e in events),
        "== Agent finished (exit code 0)",
    ])


def test_format_gemini_log_and_task_page_rendering(tmp_path):
    raw = _saved_agy_log()
    formatted = format_gemini_log(raw)

    assert '{"event":' not in formatted
    assert "== Local clone: /tmp/repo" in formatted
    assert "[init] gemini-4-neon" in formatted
    assert "Progress: Reading tests." in formatted
    assert "[tool] read_file: baton/web.py" in formatted
    assert "[tool] run_command: pytest -q (completed)" in formatted
    assert "Summary: Added readable Gemini logs." in formatted
    assert "Checks: 12 passed." in formatted
    assert "[result] SUCCESS · tokens: in 1,200 · out 340 · think 80 · cache 4,500" in formatted
    # Non-gemini logs pass through unchanged.
    assert format_task_log("codex", raw) == raw

    task = Task("g-log", "x/demo", "Gemini task", "", "gemini", "local", time.time(),
                status="done", model="gemini-4-neon", finished_at=time.time())
    report = task_report(task, raw)
    assert report["progress"] == "Reading tests."
    assert report["summary"] == "Added readable Gemini logs."
    assert report["checks"] == "12 passed."

    # Verify /tasks/{id} renders the readable log instead of raw JSON lines.
    store = TaskStore(tmp_path)
    store.save(task)
    store.log_path(task.id).write_text(raw, encoding="utf-8")
    client = TestClient(web.create_app(Settings(data_dir=tmp_path, lang="en")))
    html = client.get(f"/tasks/{task.id}").text
    assert '{"event":' not in html
    assert "[tool] read_file: baton/web.py" in html
    assert "[result] SUCCESS" in html


def test_task_sse_streams_live_updates_and_closes_on_finish(tmp_path):
    app = web.create_app(Settings(data_dir=tmp_path, lang="en"))
    app.state.ctx.task_sse_poll = 0.03
    store = app.state.ctx.store

    task = Task("live-1", "x/demo", "Live task", "", "gemini", "local", time.time(),
                status="running", steps=["12:00:00 Running Gemini…"])
    store.save(task)
    store.log_path(task.id).write_text(
        "== Running Gemini…\n" + json.dumps({"event": "step_update", "step_update": {"text_delta": "Progress: Step 1.\n"}}) + "\n",
        encoding="utf-8",
    )

    client = TestClient(app)
    # Running task page loads task-live.js instead of the old reload interval.
    page = client.get("/tasks/live-1").text
    assert "/static/task-live.js" in page
    assert 'data-task-id="live-1"' in page
    assert "setInterval" not in page

    # Unknown task returns 404.
    assert client.get("/api/tasks/missing/events").status_code == 404

    def finish_soon():
        time.sleep(0.08)
        with open(store.log_path(task.id), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "step_update", "step_update": {"text_delta": "Progress: Step 2.\n"}}) + "\n")
        task.steps.append("12:00:02 Agent finished (exit code 0)")
        store.save(task)
        time.sleep(0.08)
        task.status = "done"
        task.finished_at = time.time()
        store.save(task)

    worker = threading.Thread(target=finish_soon)
    worker.start()
    events: list[tuple[str, dict]] = []
    with client.stream("GET", "/api/tasks/live-1/events") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        ev_type = ""
        for line in resp.iter_lines():
            if line.startswith("event: "):
                ev_type = line[7:].strip()
            elif line.startswith("data: "):
                events.append((ev_type, json.loads(line[6:])))
    worker.join(5.0)

    updates = [data for kind, data in events if kind == "update"]
    dones = [data for kind, data in events if kind == "done"]
    assert len(updates) >= 2
    assert any("Step 1." in u["log"] for u in updates)
    assert any("Step 2." in u["log"] and '{"event":' not in u["log"] for u in updates)
    assert updates[-1]["status"] == "done"
    assert dones == [{"status": "done"}]


def test_task_sse_respects_access_key_auth(tmp_path):
    app = web.create_app(Settings(data_dir=tmp_path, access_key="secret-key-123"))
    app.state.ctx.store.save(Task("t-auth", "x/demo", "Locked", "", "codex", "local", time.time(), status="done"))
    client = TestClient(app)
    assert client.get("/api/tasks/t-auth/events").status_code == 401


def test_quotas_does_not_block_on_slow_usage_collect(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_collect(*args, **kwargs):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(5.0)
        return {
            "days": 7,
            "apps": [{
                "app": "gemini",
                "today": usage.Tokens(fresh=100, cache=0, output=50),
                "week": usage.Tokens(fresh=100, cache=0, output=50),
                "bars": [],
                "models": [("gemini-4-neon", {"today": usage.Tokens(fresh=100, output=50),
                                             "week": usage.Tokens(fresh=100, output=50)})],
            }],
        }

    monkeypatch.setattr(usage, "collect", slow_collect)
    app = web.create_app(Settings(data_dir=tmp_path, lang="en"))
    app.state.ctx.find_agy_fn = lambda: None
    client = TestClient(app)

    # First /quotas returns immediately even while usage.collect() is blocked in background.
    t0 = time.monotonic()
    r1 = client.get("/quotas")
    elapsed = time.monotonic() - t0
    assert r1.status_code == 200
    assert elapsed < 1.0
    assert started.wait(2.0)

    # Release the background scan and wait for cache population.
    release.set()
    deadline = time.time() + 5.0
    while time.time() < deadline and not app.state.ctx.usage_cache:
        time.sleep(0.02)
    assert app.state.ctx.usage_cache

    # Subsequent /quotas uses the cached snapshot without calling usage.collect() again.
    r2 = client.get("/quotas")
    assert r2.status_code == 200
    assert "gemini-4-neon" in r2.text
    assert calls == 1


def test_quotas_does_not_block_on_slow_gemini_refresh(tmp_path, monkeypatch):
    from baton.routes import deps as deps_mod

    started = threading.Event()
    release = threading.Event()

    def slow_gemini(data_dir):
        started.set()
        release.wait(5.0)
        return True, "ok"

    monkeypatch.setattr(deps_mod, "fetch_gemini_quota", slow_gemini)
    monkeypatch.setattr(usage, "collect", lambda *a, **kw: {"days": 7, "apps": []})
    app = web.create_app(Settings(data_dir=tmp_path, lang="en"))
    app.state.ctx.find_agy_fn = lambda: "agy"
    client = TestClient(app)

    t0 = time.monotonic()
    resp = client.get("/quotas")
    elapsed = time.monotonic() - t0
    assert resp.status_code == 200
    assert elapsed < 1.0
    assert started.wait(2.0)

    release.set()
    deadline = time.time() + 5.0
    while time.time() < deadline and app.state.ctx.gemini_state["at"] == 0.0:
        time.sleep(0.02)
    assert app.state.ctx.gemini_state["at"] > 0.0
    assert app.state.ctx.gemini_state["msg"] == ""
