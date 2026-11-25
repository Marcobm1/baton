"""Tokens spent per day, per app and per model, from the tools' own local logs.

Sources (all read-only, all on this computer):
  * Claude Code: ~/.claude/projects/**/*.jsonl  (each answer's `message.usage`)
  * Codex:       ~/.codex/sessions/**/*.jsonl   (`token_count` events)
  * OpenCode:    ~/.local/share/opencode/opencode.db (`session_message.data.tokens`)
  * Gemini:      <data dir>/gemini-usage.jsonl, written by Baton itself from the
                 token counts of agy's own `result` event (tasks) and of the
                 ACP `session/prompt` answer (Agents chat); see record_gemini().
Only numbers, model names and dates are read; never the text of a conversation.

What is NOT counted: chats on claude.ai or chatgpt.com, which leave no log on
this computer.

Kinds of tokens:
  * fresh input: what the model read for the first time;
  * cache: input re-read from the provider's prompt cache (much cheaper, and it
    counts far less towards plan limits) or written to it;
  * output: what the model wrote (including its reasoning).
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

LOCAL_PROVIDERS = {"ollama", "lmstudio", "oss", "llama.cpp", "llamacpp"}
APPS = ("claude-code", "codex", "gemini", "opencode", "local")
GEMINI_LOG = "gemini-usage.jsonl"
_gemini_lock = threading.Lock()


@dataclass
class Tokens:
    fresh: int = 0
    cache: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        return self.fresh + self.cache + self.output

    def add(self, other: "Tokens") -> None:
        self.fresh += other.fresh
        self.cache += other.cache
        self.output += other.output


@dataclass
class Usage:
    # (app, model) -> day -> Tokens
    data: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(Tokens)))

    def add(self, app: str, model: str, day: dt.date, t: Tokens) -> None:
        self.data[(app, model or "?")][day].add(t)


def _day(ts: float) -> dt.date:
    return dt.datetime.fromtimestamp(ts).date()


def _iso(value: str) -> float | None:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _recent_files(pattern: str, since: float) -> list[str]:
    out = []
    for f in glob.glob(pattern, recursive=True):
        try:
            if os.path.getmtime(f) >= since:
                out.append(f)
        except OSError:
            continue
    return out


def read_claude_code(claude_home: Path, since: float, usage: Usage) -> None:
    # Claude Code writes multiple streaming lines per (message.id, requestId)
    # where early lines hold partial output_tokens and later lines hold the final
    # totals. Keep the max token counts per key; never deduplicate (None, None).
    by_key: dict[tuple[str | None, str | None], tuple[str, dt.date, Tokens]] = {}
    for f in _recent_files(str(claude_home / "projects" / "**" / "*.jsonl"), since):
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = ev.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                ts = _iso(ev.get("timestamp", ""))
                model = str(msg.get("model") or "")
                if ts is None or ts < since or model.startswith("<"):
                    continue
                u = msg["usage"]
                tok = Tokens(
                    fresh=int(u.get("input_tokens") or 0),
                    cache=int(u.get("cache_read_input_tokens") or 0)
                    + int(u.get("cache_creation_input_tokens") or 0),
                    output=int(u.get("output_tokens") or 0),
                )
                msg_id, req_id = msg.get("id"), ev.get("requestId")
                if msg_id is None and req_id is None:
                    usage.add("claude-code", model, _day(ts), tok)
                    continue
                key = (msg_id, req_id)
                prev = by_key.get(key)
                if prev is None:
                    by_key[key] = (model, _day(ts), tok)
                else:
                    p_model, p_day, p_tok = prev
                    by_key[key] = (
                        model or p_model,
                        _day(ts),
                        Tokens(
                            fresh=max(p_tok.fresh, tok.fresh),
                            cache=max(p_tok.cache, tok.cache),
                            output=max(p_tok.output, tok.output),
                        ),
                    )
    for model, day, tok in by_key.values():
        usage.add("claude-code", model, day, tok)


def read_codex(codex_home: Path, since: float, usage: Usage) -> None:
    for f in _recent_files(str(codex_home / "sessions" / "**" / "*.jsonl"), since):
        model, provider = "", ""
        prev = {"input": 0, "cached": 0, "output": 0}
        try:
            fh = open(f, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if not any(k in line for k in ('"token_count"', '"turn_context"', '"session_meta"')):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = ev.get("payload") or {}
                if ev.get("type") == "session_meta":
                    provider = str(p.get("model_provider") or provider)
                    continue
                if ev.get("type") == "turn_context":
                    model = str(p.get("model") or model)
                    continue
                total = ((p.get("info") or {}).get("total_token_usage")) if p.get("type") == "token_count" else None
                if not isinstance(total, dict):
                    continue
                now = {"input": int(total.get("input_tokens") or 0),
                       "cached": int(total.get("cached_input_tokens") or 0),
                       "output": int(total.get("output_tokens") or 0)}
                # Totals are cumulative per session: count only what grew.
                d = {k: now[k] - prev[k] for k in now}
                if any(v < 0 for v in d.values()):
                    d = now
                prev = now
                ts = _iso(ev.get("timestamp", ""))
                if ts is None or ts < since or not any(d.values()):
                    continue
                app = "local" if provider.lower() in LOCAL_PROVIDERS else "codex"
                usage.add(app, model, _day(ts), Tokens(
                    fresh=max(0, d["input"] - d["cached"]), cache=d["cached"], output=d["output"]))


def read_opencode(db: Path, since: float, usage: Usage) -> None:
    if not db.is_file():
        return
    try:
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return
    q = """
        select json_extract(data, '$.time.created'),
               json_extract(data, '$.model.providerID'),
               json_extract(data, '$.model.id'),
               json_extract(data, '$.tokens.input'),
               json_extract(data, '$.tokens.output'),
               json_extract(data, '$.tokens.reasoning'),
               json_extract(data, '$.tokens.cache.read'),
               json_extract(data, '$.tokens.cache.write')
        from session_message
        where json_extract(data, '$.tokens') is not null
          and json_extract(data, '$.time.created') >= ?
    """
    try:
        rows = con.execute(q, (int(since * 1000),)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for created, provider, model, inp, out, reas, cr, cw in rows:
        app = "local" if str(provider or "").lower() in LOCAL_PROVIDERS else "opencode"
        name = f"{provider}/{model}" if provider else str(model or "")
        usage.add(app, name, _day(created / 1000), Tokens(
            fresh=int(inp or 0), cache=int(cr or 0) + int(cw or 0),
            output=int(out or 0) + int(reas or 0)))


def record_gemini(data_dir: Path, model: str, tokens: dict, source: str = "task",
                  ts: float | None = None) -> bool:
    """Append one agy run's token counts (numbers and the model name only).

    `tokens` is agy's `usage` object, from `agy -p --output-format stream-json`
    (input_tokens, output_tokens, thinking_tokens, cache_read_tokens) or from
    ACP (inputTokens, outputTokens, thoughtTokens, cachedReadTokens). In both,
    the input count leaves out the cached input (checked 2026-11-25: input +
    output + cache read = total).
    """
    if not isinstance(tokens, dict):
        return False

    def num(*keys: str) -> int:
        for key in keys:
            if tokens.get(key) is not None:
                try:
                    return max(0, int(tokens[key]))
                except (TypeError, ValueError):
                    return 0
        return 0

    row = {"ts": ts or time.time(), "model": str(model or "?")[:100], "source": source,
           "input": num("input_tokens", "inputTokens"), "output": num("output_tokens", "outputTokens"),
           "thinking": num("thinking_tokens", "thoughtTokens"),
           "cache_read": num("cache_read_tokens", "cachedReadTokens")}
    if not any(row[k] for k in ("input", "output", "thinking", "cache_read")):
        return False
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with _gemini_lock, open(data_dir / GEMINI_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        return False
    return True


def read_gemini(data_dir: Path | None, since: float, usage: Usage) -> None:
    if data_dir is None:
        return
    try:
        fh = open(data_dir / GEMINI_LOG, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
                ts = float(row["ts"])
                t = Tokens(fresh=int(row.get("input") or 0), cache=int(row.get("cache_read") or 0),
                           output=int(row.get("output") or 0) + int(row.get("thinking") or 0))
            except (ValueError, KeyError, TypeError):
                continue
            if ts >= since:
                usage.add("gemini", str(row.get("model") or ""), _day(ts), t)


def collect(claude_home: Path, codex_home: Path, opencode_db: Path, days: int = 7,
            today: dt.date | None = None, data_dir: Path | None = None) -> dict:
    """Summary for the page: per app, today / last `days` days, a daily series and models."""
    today = today or dt.date.today()
    first = today - dt.timedelta(days=days - 1)
    since = dt.datetime.combine(first, dt.time()).timestamp()
    usage = Usage()
    read_claude_code(claude_home, since, usage)
    read_codex(codex_home, since, usage)
    read_opencode(opencode_db, since, usage)
    read_gemini(data_dir, since, usage)

    span = [first + dt.timedelta(days=i) for i in range(days)]
    apps: dict[str, dict] = {}
    for (app, model), by_day in usage.data.items():
        a = apps.setdefault(app, {"app": app, "today": Tokens(), "week": Tokens(),
                                  "series": {d: 0 for d in span}, "models": {}})
        m = a["models"].setdefault(model, {"today": Tokens(), "week": Tokens()})
        for day, t in by_day.items():
            if day < first or day > today:
                continue
            a["week"].add(t)
            m["week"].add(t)
            a["series"][day] += t.total
            if day == today:
                a["today"].add(t)
                m["today"].add(t)
    out = []
    for app in APPS:
        if app in apps:
            a = apps[app]
            peak = max(a["series"].values()) or 1
            a["bars"] = [{"day": d, "total": v, "pct": round(100 * v / peak)} for d, v in a["series"].items()]
            a["models"] = sorted(a["models"].items(), key=lambda kv: kv[1]["week"].total, reverse=True)
            out.append(a)
    return {"apps": out, "days": days, "first": first, "today": today}


def human(n: int) -> str:
    for unit, size in (("B", 1_000_000_000), ("M", 1_000_000), ("k", 1_000)):
        if n >= size:
            return f"{n / size:.1f}{unit}".replace(".0", "")
    return str(n)
