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


def test_statusline_and_manual(tmp_path: Path):
    now = time.time()
    statusline.save({"rate_limits": {"five_hour": {"used_percentage": 30, "resets_at": now + 600},
                                     "seven_day": {"used_percentage": 10, "resets_at": now + 5 * 86400}}}, tmp_path)
    got = {w.kind: w.used for w in quotas.claude_statusline_windows(tmp_path)}
    assert got == {"5h": 30, "week": 10}
    time.sleep(0.01)
    quotas.add_manual(tmp_path, "Claude", "week", 55, now + 86400)
    views = {(v["provider"], v["kind"]): v for v in quotas.all_windows(tmp_path, tmp_path / "nocodex")}
    assert views[("claude", "week")]["used"] == 55  # newest reading wins
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
