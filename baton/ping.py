"""Refresh quota readings on a schedule by asking the official CLIs.

Claude Code: `claude -p ... --output-format stream-json --verbose` prints a
`rate_limit_event` whose `unifiedWindows` holds the 5-hour and 7-day
utilization of your plan. Baton sends a one-word prompt with the cheapest
model and saves those two numbers. The CLI uses its own login; Baton never
sees or touches the credentials.

Codex: `codex exec` writes the plan's rate limits to its session logs, which
Baton already reads; a ping just makes them fresh.

Side effects to know about:
  * each ping spends a little quota (one tiny request);
  * if no 5-hour window is open, the ping opens one (the clock starts then).
Standard library only.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .statusline import save


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    # With ANTHROPIC_API_KEY set, Claude Code bills the API instead of the plan,
    # and the numbers would not be your subscription's. Never pass it on.
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def find_claude() -> str | None:
    explicit = os.environ.get("BATON_CLAUDE_BIN")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    # The VS Code extension ships its own copy of the CLI.
    pattern = str(Path.home() / ".vscode" / "extensions" / "anthropic.claude-code-*"
                  / "resources" / "native-binary" / ("claude.exe" if os.name == "nt" else "claude"))
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def find_codex() -> str | None:
    explicit = os.environ.get("BATON_CODEX_BIN")
    if explicit:
        return explicit
    for name in ("codex.cmd", "codex") if os.name == "nt" else ("codex",):
        found = shutil.which(name)
        if found:
            return found
    return None


def parse_claude_stream(text: str) -> dict | None:
    """Return {'five_hour': {...}, 'seven_day': {...}} in status line format."""
    for line in text.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        windows = (ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}
        out = {}
        for key in ("five_hour", "seven_day"):
            w = windows.get(key)
            if isinstance(w, dict) and w.get("utilization") is not None:
                util = float(w["utilization"])
                out[key] = {
                    "used_percentage": util * 100 if util <= 1 else util,
                    "resets_at": w.get("resetsAt"),
                }
        if out:
            return out
    return None


def ping_claude(data_dir: Path, binary: str | None = None, model: str = "haiku",
                timeout: int = 120) -> tuple[bool, str]:
    binary = binary or find_claude()
    if not binary:
        return False, "Claude Code CLI not found (install it or set BATON_CLAUDE_BIN)"
    cmd = [binary, "-p", "--model", model, "--max-turns", "1",
           "--output-format", "stream-json", "--verbose"]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(cmd, input="ok", capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout,
                               cwd=tmp, env=_clean_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run Claude Code: {exc.__class__.__name__}"
    windows = parse_claude_stream(r.stdout)
    if not windows:
        return False, "Claude Code answered without rate-limit data (not logged in to a plan?)"
    save({"rate_limits": windows}, data_dir, source="ping")
    return True, "ok"


def ping_codex(binary: str | None = None, timeout: int = 180) -> tuple[bool, str]:
    binary = binary or find_codex()
    if not binary:
        return False, "Codex CLI not found (set BATON_CODEX_BIN)"
    cmd = [binary, "exec", "--skip-git-repo-check", "-s", "read-only", "--color", "never", "-"]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(cmd, input="Reply with the single word ok.", capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, cwd=tmp)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run Codex: {exc.__class__.__name__}"
    return (r.returncode == 0), ("ok" if r.returncode == 0 else f"codex exited with {r.returncode}")


def in_hours(spec: str, hour: int | None = None) -> bool:
    """'8-24' -> True from 08:00 to 23:59 local time. Empty spec = always."""
    if not spec.strip():
        return True
    hour = time.localtime().tm_hour if hour is None else hour
    start, end = (int(x) for x in spec.split("-", 1))
    return start <= hour < end if start <= end else (hour >= start or hour < end)


class Scheduler:
    """Runs the pings every N minutes inside `baton serve`."""

    def __init__(self, data_dir: Path, claude_minutes: int, codex_minutes: int, hours: str,
                 model: str = "haiku"):
        self.data_dir = data_dir
        self.claude_minutes = claude_minutes
        self.codex_minutes = codex_minutes
        self.hours = hours
        self.model = model
        self.last: dict[str, float] = {}
        self.results: dict[str, tuple[float, bool, str]] = {}

    @property
    def enabled(self) -> bool:
        return self.claude_minutes > 0 or self.codex_minutes > 0

    def due(self, name: str, minutes: int, now: float) -> bool:
        return minutes > 0 and now - self.last.get(name, 0) >= minutes * 60

    def tick(self) -> None:
        now = time.time()
        if not in_hours(self.hours):
            return
        if self.due("claude", self.claude_minutes, now):
            self.last["claude"] = now
            ok, msg = ping_claude(self.data_dir, model=self.model)
            self.results["claude"] = (now, ok, msg)
        if self.due("codex", self.codex_minutes, now):
            self.last["codex"] = now
            ok, msg = ping_codex()
            self.results["codex"] = (now, ok, msg)
