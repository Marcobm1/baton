"""Subscription quota windows (5-hour and weekly) for AI coding tools.

Sources, all local and passive (Baton never reads or sends your credentials):
  * Codex: the rate-limit snapshots Codex itself writes in its session logs
    (~/.codex/sessions/**/*.jsonl, "token_count" events).
  * Claude Code: the official status line JSON (rate_limits.five_hour /
    seven_day), saved by `baton statusline`, or the same numbers from a
    scheduled `claude -p` ping (see ping.py); both go to claude-usage.json.
The newest reading of each window wins.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIVE_HOURS = 300
WEEK = 7 * 24 * 60


@dataclass
class Window:
    provider: str          # "claude", "codex", ...
    kind: str              # "5h" | "week"
    used: float            # percent 0-100 at the time it was seen
    resets_at: float | None  # unix seconds
    seen_at: float         # unix seconds of the reading
    source: str            # "codex-logs" | "statusline" | "manual"
    plan: str = ""

    @property
    def minutes(self) -> int:
        return FIVE_HOURS if self.kind == "5h" else WEEK

    def view(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        expired = self.resets_at is not None and self.resets_at <= now
        used = 0.0 if expired else max(0.0, min(100.0, self.used))
        left_s = None if self.resets_at is None or expired else self.resets_at - now
        elapsed_pct = None
        pace = None
        if left_s is not None:
            total = self.minutes * 60
            elapsed_pct = max(0.0, min(100.0, 100 * (total - left_s) / total))
            pace = used - elapsed_pct  # >0 burning faster than an even spread
        if expired:
            verdict = "reset"
        elif pace is None:
            verdict = "unknown"
        elif used >= 95:
            verdict = "exhausted"
        elif pace > 15:
            verdict = "fast"
        elif pace < -25:
            verdict = "spare"
        else:
            verdict = "ok"
        return {
            "provider": self.provider,
            "kind": self.kind,
            "used": round(used, 1),
            "left": round(100 - used, 1),
            "resets_at": self.resets_at,
            "resets_in_s": left_s,
            "elapsed_pct": None if elapsed_pct is None else round(elapsed_pct, 1),
            "pace": None if pace is None else round(pace, 1),
            "verdict": verdict,
            "seen_at": self.seen_at,
            "age_s": now - self.seen_at,
            "source": self.source,
            "plan": self.plan,
            "expired_reading": expired,
        }


# ---------------------------------------------------------------- Codex logs

def _iso_to_ts(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def codex_windows(codex_home: Path, max_files: int = 40) -> list[Window]:
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return []
    files = sorted(sessions.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    best: dict[str, Window] = {}
    for f in files[:max_files]:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            rl = (ev.get("payload") or {}).get("rate_limits") or {}
            if rl.get("limit_id") not in (None, "codex"):
                continue
            try:
                seen = _iso_to_ts(ev.get("timestamp", ""))
            except ValueError:
                seen = f.stat().st_mtime
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    continue
                mins = int(w.get("window_minutes") or 0)
                kind = "5h" if mins and mins <= FIVE_HOURS else "week"
                if kind not in best or best[kind].seen_at < seen:
                    best[kind] = Window(
                        "codex", kind, float(w["used_percent"]),
                        float(w["resets_at"]) if w.get("resets_at") else None,
                        seen, "codex-logs", str(rl.get("plan_type") or ""),
                    )
            if len(best) == 2:
                break
        if len(best) == 2:
            break
    return list(best.values())


# ------------------------------------------------- Claude Code status line

def claude_statusline_windows(data_dir: Path) -> list[Window]:
    f = data_dir / "claude-usage.json"
    try:
        snap = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    rl = snap.get("rate_limits") or {}
    for key, kind in (("five_hour", "5h"), ("seven_day", "week")):
        w = rl.get(key)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            out.append(Window(
                "claude", kind, float(w["used_percentage"]),
                float(w["resets_at"]) if w.get("resets_at") else None,
                float(snap.get("seen_at") or f.stat().st_mtime), str(snap.get("source") or "statusline"),
            ))
    return out


# ---------------------------------------------------------------- combined

def all_windows(data_dir: Path, codex_home: Path) -> list[dict[str, Any]]:
    readings = codex_windows(codex_home) + claude_statusline_windows(data_dir)
    best: dict[tuple[str, str], Window] = {}
    for w in readings:
        k = (w.provider, w.kind)
        if k not in best or best[k].seen_at < w.seen_at:
            best[k] = w
    now = time.time()
    order = {"claude": 0, "codex": 1}
    views = [w.view(now) for w in best.values()]
    views.sort(key=lambda v: (order.get(v["provider"], 9), v["provider"], v["kind"] != "5h"))
    return views


def by_provider(views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group window views into one entry per provider (for one card each)."""
    out: dict[str, dict[str, Any]] = {}
    for v in views:
        p = out.setdefault(v["provider"], {"provider": v["provider"], "plan": "", "windows": {},
                                           "age_s": v["age_s"], "source": v["source"]})
        p["windows"][v["kind"]] = v
        p["plan"] = p["plan"] or v.get("plan", "")
        if v["age_s"] < p["age_s"]:
            p["age_s"], p["source"] = v["age_s"], v["source"]
    return list(out.values())
