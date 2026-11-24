import json
import time
from pathlib import Path

from baton import quotas, statusline
from baton.status import parse, template

GOOD = """---
name: Demo
status: active
priority: high
summary: A demo project.
next: Write the tests.
updated: 2026-11-23
updated_by: claude-code
open_source: true
---

## Next
- [x] one
- [ ] two
- [ ] three
"""


def test_parse_good():
    s = parse(GOOD)
    assert s.problems == []
    assert s.name == "Demo" and s.priority == "high" and s.open_source
    assert (s.tasks_done, s.tasks_total, s.progress) == (1, 3, 33)
    assert s.updated.isoformat() == "2026-11-23"


def test_parse_problems():
    s = parse("no front matter here", fallback_name="repo")
    assert s.name == "repo"
    assert any("front matter" in p for p in s.problems)
    s = parse("---\nname: X\nstatus: wip\nupdated: soon\n---\n")
    assert any("status 'wip'" in p for p in s.problems)
    assert any("'updated'" in p for p in s.problems)


def test_template_is_valid():
    assert parse(template("New thing")).problems == []


def _codex_event(ts, p5, pw, reset5, resetw):
    return json.dumps({
        "timestamp": ts, "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex",
            "primary": {"used_percent": p5, "window_minutes": 300, "resets_at": reset5},
            "secondary": {"used_percent": pw, "window_minutes": 10080, "resets_at": resetw},
            "plan_type": "plus"}}})


def test_codex_windows(tmp_path: Path):
    d = tmp_path / "sessions" / "2026" / "11" / "23"
    d.mkdir(parents=True)
    now = time.time()
    lines = [
        _codex_event("2026-11-23T10:00:00Z", 10, 5, now + 3600, now + 86400),
        json.dumps({"timestamp": "2026-11-23T10:00:01Z", "payload": {"rate_limits": {"limit_id": "premium", "primary": None}}}),
        _codex_event("2026-11-23T11:00:00Z", 40, 20, now + 3600, now + 86400),
    ]
    (d / "rollout.jsonl").write_text("\n".join(lines))
    ws = {w.kind: w for w in quotas.codex_windows(tmp_path)}
    assert ws["5h"].used == 40 and ws["week"].used == 20 and ws["5h"].plan == "plus"


def test_expired_window_shows_reset():
    w = quotas.Window("codex", "5h", 100, time.time() - 10, time.time() - 1000, "codex-logs")
    v = w.view()
    assert v["verdict"] == "reset" and v["used"] == 0


def test_statusline_snapshot(tmp_path: Path):
    now = time.time()
    statusline.save({"rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": now + 600},
                                     "seven_day": {"used_percentage": 10, "resets_at": now + 5 * 86400}}}, tmp_path)
    got = {w.kind: w.used for w in quotas.claude_statusline_windows(tmp_path)}
    assert got == {"5h": 30, "week": 10}
    views = {(v["provider"], v["kind"]): v for v in quotas.all_windows(tmp_path, tmp_path / "nocodex")}
    assert views[("claude", "5h")]["source"] == "statusline"


def test_statusline_line():
    assert statusline.line({"model": {"display_name": "Opus"},
                            "rate_limits": {"five_hour": {"used_percentage": 12.4}}}) == "Opus · 5h 12%"


def test_dotenv_with_bom_and_comments(tmp_path, monkeypatch):
    import os

    from baton import config

    f = tmp_path / ".env"
    f.write_bytes("﻿BATON_TEST_A=es\nBATON_TEST_B=x  # note\nBATON_TEST_C=\n".encode("utf-8"))
    for k in ("BATON_TEST_A", "BATON_TEST_B", "BATON_TEST_C"):
        monkeypatch.delenv(k, raising=False)
    config._load_dotenv(f)
    assert os.environ["BATON_TEST_A"] == "es"
    assert os.environ["BATON_TEST_B"] == "x"
    assert os.environ["BATON_TEST_C"] == ""


def test_parse_claude_stream_and_hours(tmp_path):
    from baton import ping

    line = ('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1795467600,'
            '"rateLimitType":"five_hour","unifiedWindows":{"five_hour":{"utilization":0.31,'
            '"resetsAt":1795467600,"status":"allowed"},"seven_day":{"utilization":0.08,'
            '"resetsAt":1795993200,"status":"allowed"}}}}')
    out = ping.parse_claude_stream('{"type":"system"}\n' + line + '\n{"type":"result"}')
    assert round(out["five_hour"]["used_percentage"]) == 31
    assert out["seven_day"]["resets_at"] == 1795993200
    assert ping.parse_claude_stream('{"type":"result"}') is None
    assert ping.in_hours("8-24", 9) and not ping.in_hours("8-24", 3)
    assert ping.in_hours("22-6", 23) and ping.in_hours("22-6", 2) and not ping.in_hours("22-6", 12)
    assert ping.in_hours("", 3)
    from baton import statusline, quotas
    statusline.save({"rate_limits": out}, tmp_path, source="ping")
    assert {w.source for w in quotas.claude_statusline_windows(tmp_path)} == {"ping"}


def test_parse_claude_stream_exhausted_window(tmp_path):
    from baton import ping
    # With the plan used up, Claude Code says "rejected" for that window and
    # may send no utilization: the window must read 100 % used, not stay stale.
    rejected = ('{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
                '"resetsAt":1795467600,"rateLimitType":"five_hour","isUsingOverage":false}}')
    out = ping.parse_claude_stream(rejected)
    assert out == {"five_hour": {"used_percentage": 100.0, "resets_at": 1795467600}}
    # A later rejection overrides an earlier reading of the same window.
    earlier = ('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":'
               '{"five_hour":{"utilization":0.01,"resetsAt":1795467600},'
               '"seven_day":{"utilization":0.4,"resetsAt":1795993200}}}}')
    out = ping.parse_claude_stream(earlier + "\n" + rejected)
    assert out["five_hour"]["used_percentage"] == 100.0
    assert round(out["seven_day"]["used_percentage"]) == 40
    from baton import statusline, quotas
    statusline.save({"rate_limits": out}, tmp_path, source="ping")
    used = {w.kind: w.used for w in quotas.claude_statusline_windows(tmp_path)}
    assert used["5h"] == 100.0 and round(used["week"]) == 40


def _claude_reading(path: Path, age_s: float) -> float:
    """Write claude-usage.json as the status line would, seen_at age_s ago."""
    now = time.time()
    (path / "claude-usage.json").write_text(json.dumps({
        "seen_at": now - age_s, "source": "statusline",
        "rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": now + 3600},
                        "seven_day": {"used_percentage": 10, "resets_at": now + 86400}}}))
    return now - age_s


def test_scheduler_skips_pings_when_readings_are_fresh(tmp_path, monkeypatch):
    from baton import ping

    calls = []
    monkeypatch.setattr(ping, "ping_claude", lambda *a, **k: calls.append("claude") or (True, "ok"))
    monkeypatch.setattr(ping, "ping_codex", lambda *a, **k: calls.append("codex") or (True, "ok"))
    seen = _claude_reading(tmp_path, 60)   # the status line reported a minute ago
    home = tmp_path / "codex"             # and Codex's own logs two minutes ago
    d = home / "sessions" / "2026" / "11" / "24"
    d.mkdir(parents=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 120))
    (d / "rollout.jsonl").write_text(_codex_event(ts, 40, 20, time.time() + 3600,
                                                 time.time() + 86400))

    s = ping.Scheduler(tmp_path, 10, 10, "", codex_home=home)
    s.tick()
    assert calls == []                    # fresher readings exist: no quota spent
    assert s.results["claude"][1] and "skipped" in s.results["claude"][2]
    assert s.results["codex"][1] and "skipped" in s.results["codex"][2]
    assert abs(s.last["claude"] - seen) < 2   # next check due from the reading's time
    s.tick()
    assert calls == []                    # and nothing runs again before N minutes


def test_scheduler_pings_when_the_reading_is_stale(tmp_path, monkeypatch):
    from baton import ping

    calls = []
    monkeypatch.setattr(ping, "ping_claude", lambda *a, **k: calls.append("claude") or (True, "ok"))
    monkeypatch.setattr(ping, "ping_codex", lambda *a, **k: calls.append("codex") or (True, "ok"))
    _claude_reading(tmp_path, 20 * 60)    # nothing new for 20 minutes

    s = ping.Scheduler(tmp_path, 10, 10, "")  # no codex_home: no codex logs to read
    s.tick()
    assert calls == ["claude", "codex"]
    assert s.results["claude"][1:] == (True, "ok")
    s.tick()
    assert calls == ["claude", "codex"]   # a check runs at most once per interval


def test_next_task_and_grouping():
    s = parse(GOOD)
    assert s.next_task == "two" and s.tasks_left == 2
    no_boxes = parse("---\nname: X\nstatus: active\nupdated: 2026-11-23\nnext: Do `this`\n---\nbody")
    assert no_boxes.next_task == "Do this"
    views = [
        {"provider": "claude", "kind": "5h", "age_s": 10, "source": "ping", "plan": ""},
        {"provider": "claude", "kind": "week", "age_s": 5, "source": "manual", "plan": ""},
        {"provider": "codex", "kind": "5h", "age_s": 1, "source": "codex-logs", "plan": "plus"},
    ]
    g = {p["provider"]: p for p in quotas.by_provider(views)}
    assert set(g["claude"]["windows"]) == {"5h", "week"} and g["claude"]["source"] == "manual"
    assert g["codex"]["plan"] == "plus"


def test_task_endpoint_csrf_and_issue(tmp_path, monkeypatch):
    import asyncio, re
    from fastapi.testclient import TestClient
    from baton import github, web
    from baton.config import Settings
    from baton.github import Project

    st = parse(GOOD)
    proj = Project("x/demo", "x/demo", "STATUS.md", "main", True, "https://github.com/x/demo", None, st)
    calls = []

    async def fake_load(self, force=False):
        return [proj], []

    async def fake_issue(self, repo, title, body):
        calls.append((repo, title, body))
        return {"number": 7, "url": "https://github.com/x/demo/issues/7"}

    async def fake_comments(self, repo, issue):
        assert (repo, issue) == ("x/demo", 7)
        return ["BATON_SUMMARY_BEGIN\n**Checked** the result.\nBATON_SUMMARY_END"]

    monkeypatch.setattr(github.Hub, "agent_comments", fake_comments)
    orig = (github.Hub.load, github.Hub.create_issue)
    github.Hub.load, github.Hub.create_issue = fake_load, fake_issue
    try:
        c = TestClient(web.create_app(Settings(github_token="t", owner="x", data_dir=tmp_path, codex_home=tmp_path)))
        page = c.get("/tasks/new?repo=x/demo").text
        token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        form = {"repo": "x/demo", "where": "cloud", "title": "T"}
        assert c.post("/tasks", data={**form, "csrf": "bad"}).status_code == 403
        r = c.post("/tasks", data={**form, "csrf": token},
                   headers={"origin": "https://evil.example"}, follow_redirects=False)
        assert r.status_code == 403
        # Behind Tailscale Serve the browser's Origin matches X-Forwarded-Host.
        r = c.post("/tasks", data={**form, "title": "Fix it", "details": "do X", "csrf": token},
                   headers={"origin": "https://pc.tail.ts.net", "x-forwarded-host": "pc.tail.ts.net"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/tasks/")
        detail = c.get(r.headers["location"]).text
        assert "https://github.com/x/demo/issues/7" in detail
        assert "<strong>Checked</strong> the result." in detail
        assert web.TaskStore(tmp_path).all()[0].summary == "**Checked** the result."
        assert calls and calls[0][2].startswith("@claude do X")
        assert c.post("/tasks", data={**form, "repo": "other/repo", "csrf": token}).status_code == 400
        assert c.post("/tasks", data={**form, "where": "local:rm", "csrf": token}).status_code == 400
        assert c.get("/quotas").status_code == 200 and c.get("/tasks").status_code == 200
    finally:
        github.Hub.load, github.Hub.create_issue = orig
