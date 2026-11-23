import datetime as dt
import json
import sqlite3
import time
from pathlib import Path

from baton import usage


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def test_collect_from_the_three_tools(tmp_path: Path):
    today = dt.date.today()
    now = dt.datetime.now().replace(microsecond=0)
    yesterday = now - dt.timedelta(days=1)

    # Claude Code: the same answer appears on two lines (counted once).
    proj = tmp_path / "claude" / "projects" / "p"
    proj.mkdir(parents=True)
    line = {"timestamp": _iso(now), "requestId": "r1", "message": {
        "id": "m1", "model": "claude-opus-5-5",
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 200, "output_tokens": 50}}}
    old = {"timestamp": _iso(yesterday), "requestId": "r0", "message": {
        "id": "m0", "model": "claude-haiku-5-5", "usage": {"input_tokens": 5, "output_tokens": 5}}}
    (proj / "s.jsonl").write_text("\n".join(json.dumps(x) for x in (line, line, old)))

    # Codex: cumulative totals; only what grows counts.
    sess = tmp_path / "codex" / "sessions" / "2026"
    sess.mkdir(parents=True)

    def tc(total_in, cached, out):
        return {"timestamp": _iso(now), "type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total_in, "cached_input_tokens": cached,
                                  "output_tokens": out}}}}
    lines = [{"type": "session_meta", "payload": {"model_provider": "openai"}},
             {"type": "turn_context", "payload": {"model": "gpt-6.2-astra"}},
             tc(1000, 800, 10), tc(1000, 800, 10), tc(1500, 1100, 30)]
    (sess / "rollout.jsonl").write_text("\n".join(json.dumps(x) for x in lines))

    # OpenCode: one free model and one local (ollama) model.
    db = tmp_path / "opencode.db"
    con = sqlite3.connect(db)
    con.execute("create table session_message (id text, session_id text, type text, seq int, "
                "time_created int, time_updated int, data text)")
    ms = int(time.time() * 1000)
    for prov, model in (("opencode", "big-pickle"), ("ollama", "qwen3:8b")):
        con.execute("insert into session_message values ('x','s','assistant',1,?,?,?)", (ms, ms, json.dumps({
            "time": {"created": ms}, "model": {"id": model, "providerID": prov},
            "tokens": {"input": 100, "output": 20, "reasoning": 5, "cache": {"read": 300, "write": 0}}})))
    con.commit()
    con.close()

    out = usage.collect(tmp_path / "claude", tmp_path / "codex", db, today=today)
    apps = {a["app"]: a for a in out["apps"]}
    assert set(apps) == {"claude-code", "codex", "opencode", "local"}
    cc = apps["claude-code"]
    assert (cc["today"].fresh, cc["today"].cache, cc["today"].output) == (10, 1200, 50)
    assert cc["week"].total == 1260 + 10
    cx = apps["codex"]["today"]
    assert (cx.fresh, cx.cache, cx.output) == (400, 1100, 30)
    assert apps["opencode"]["models"][0][0] == "opencode/big-pickle"
    assert apps["local"]["today"].total == 425
    assert len(cc["bars"]) == 7 and max(b["pct"] for b in cc["bars"]) == 100


def test_human():
    assert usage.human(950) == "950"
    assert usage.human(1_200) == "1.2k"
    assert usage.human(3_000_000) == "3M"
