"""Claude Code status line command that also records your quota for Baton.

Claude Code runs the status line command on every render and passes a JSON
document on stdin; for Pro/Max plans it includes rate_limits.five_hour and
rate_limits.seven_day (used_percentage, resets_at). This script saves those
two windows to <data dir>/claude-usage.json and prints a short status line.

Standard library only, so it can run with any Python 3.9+:

    python -m baton statusline            # if Baton is installed
    python path/to/baton/statusline.py    # standalone copy

Note: Claude Code only runs status line commands in the terminal (CLI),
not inside the VS Code extension.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def data_dir() -> Path:
    return Path(os.environ.get("BATON_DATA_DIR") or Path.home() / ".baton")


def save(payload: dict, target: Path, source: str = "statusline") -> None:
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return
    keep = {k: rl[k] for k in ("five_hour", "seven_day") if isinstance(rl.get(k), dict)}
    if not keep:
        return
    target.mkdir(parents=True, exist_ok=True)
    tmp = target / "claude-usage.json.tmp"
    tmp.write_text(json.dumps({"seen_at": time.time(), "source": source, "rate_limits": keep}), encoding="utf-8")
    tmp.replace(target / "claude-usage.json")


def line(payload: dict) -> str:
    parts = []
    model = (payload.get("model") or {}).get("display_name")
    if model:
        parts.append(str(model))
    rl = payload.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = rl.get(key) or {}
        if w.get("used_percentage") is not None:
            parts.append(f"{label} {round(float(w['used_percentage']))}%")
    ctx = (payload.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {round(float(ctx))}%")
    return " · ".join(parts) or "Claude"


def main() -> int:
    # Claude Code reads the line as UTF-8; Windows consoles default to another code page.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    try:
        save(payload, data_dir())
    except OSError:
        pass  # never break the status line
    print(line(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
