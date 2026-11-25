"""Refresh quota readings on a schedule by asking the official CLIs.

Claude Code: `claude -p ... --output-format stream-json --verbose` prints a
`rate_limit_event` whose `unifiedWindows` holds the 5-hour and 7-day
utilization of your plan. Baton sends a one-word prompt with the cheapest
model and saves those two numbers. The CLI uses its own login; Baton never
sees or touches the credentials.

Codex: `codex exec` writes the plan's rate limits to its session logs, which
Baton already reads; a ping just makes them fresh.

Gemini (Antigravity CLI, `agy`): `agy -p /usage --output-format json` prints
the plan's 5-hour and weekly windows without asking any model (0 tokens), so
Baton can read it as often as it likes; see fetch_gemini_quota().

Side effects to know about:
  * each ping spends a little quota (one tiny request);
  * if no 5-hour window is open, the ping opens one (the clock starts then);
  * a scheduled check is skipped when a fresher reading already exists (you
    just used the CLI), so even 5-10 minute intervals cost little extra.
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

from .quotas import claude_statusline_windows, codex_windows
from .statusline import save


# On Windows, don't flash a console window for each check when Baton runs hidden.
_NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


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


def find_agy() -> str | None:
    """The Antigravity CLI (`agy.exe`), never a .cmd/.ps1 wrapper.

    winget installs it in its own Packages folder and adds it to PATH only for
    new terminals, so a Baton started before the install still finds it there.
    """
    explicit = os.environ.get("BATON_AGY_BIN")
    if explicit:
        return explicit
    found = shutil.which("agy")
    if found and not found.lower().endswith((".cmd", ".ps1", ".bat")):
        return found
    local = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local:
        # os.path, not Path: Path picks its Windows or POSIX class from the
        # system, so a test that pretends to be Windows on Linux can't build one.
        winget = os.path.join(local, "Microsoft", "WinGet")
        matches = sorted(glob.glob(os.path.join(winget, "Packages", "Google.AntigravityCLI_*", "agy.exe")))
        link = os.path.join(winget, "Links", "agy.exe")
        if matches:
            return matches[-1]
        if os.path.isfile(link):
            return link
    return None


def parse_agy_usage(text: str) -> dict | None:
    """The Gemini group of `agy -p /usage --output-format json`.

    Returns {"5h": {"left": 99.9, "resets_at": "..."}, "week": {...}}: the
    percentage *left* (agy reports a remaining fraction) and the reset time.
    Only the "Gemini Models" group is kept: Baton offers only Gemini models
    through agy, and the Claude/GPT group there is a separate allowance.
    """
    try:
        data = json.loads(text)
        groups = data["command"]["data"]["groups"]
    except (ValueError, KeyError, TypeError):
        return None
    out: dict = {}
    for group in groups if isinstance(groups, list) else []:
        buckets = [b for b in group.get("buckets") or [] if isinstance(b, dict)]
        if not any(str(b.get("id", "")).startswith("gemini") for b in buckets):
            continue
        for b in buckets:
            kind = {"5h": "5h", "weekly": "week"}.get(str(b.get("window")))
            if kind and b.get("remaining_fraction") is not None:
                reset = str(b.get("reset_time") or "")
                out[kind] = {"left": max(0.0, min(100.0, float(b["remaining_fraction"]) * 100)),
                             "resets_at": "" if reset.startswith("1970") else reset}
    return out or None


def fetch_gemini_quota(data_dir: Path, binary: str | None = None,
                       timeout: int = 60) -> tuple[bool, str]:
    """Save the Gemini windows that agy reports itself (gemini-quota.json).

    `/usage` is answered by the CLI from its own account data: no prompt,
    no model, no quota spent. Baton keeps only percentages and dates.
    """
    binary = binary or find_agy()
    if not binary:
        return False, "Antigravity CLI (agy) not found (install it or set BATON_AGY_BIN)"
    cmd = [binary, "-p", "/usage", "--output-format", "json", "--print-timeout", f"{timeout}s"]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout + 15, cwd=tmp,
                               stdin=subprocess.DEVNULL, env=_clean_env(), **_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run agy: {exc.__class__.__name__}"
    windows = parse_agy_usage(r.stdout)
    if not windows:
        return False, f"agy gave no Gemini quota (exit code {r.returncode}; logged in with Google?)"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "gemini-quota.json").write_text(
        json.dumps({"seen_at": time.time(), "windows": windows}), encoding="utf-8")
    return True, "ok"


def parse_claude_stream(text: str) -> dict | None:
    """Return {'five_hour': {...}, 'seven_day': {...}} in status line format.

    Claude Code reports `utilization` as a fraction (0.31 = 31 %). When a
    window is used up the event says status "rejected" for that window
    (rateLimitType) and may carry no utilization at all, so that window is
    recorded as 100 % used. Later events win over earlier ones.
    """
    out: dict = {}
    for line in text.splitlines():
        if '"rate_limit_event"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = ev.get("rate_limit_info") or {}
        windows = info.get("unifiedWindows") or {}
        for key in ("five_hour", "seven_day"):
            w = windows.get(key)
            if isinstance(w, dict) and w.get("utilization") is not None:
                util = float(w["utilization"])
                out[key] = {
                    "used_percentage": util * 100 if util <= 1 else util,
                    "resets_at": w.get("resetsAt"),
                }
        kind = info.get("rateLimitType")
        if info.get("status") == "rejected" and kind in ("five_hour", "seven_day"):
            resets = info.get("resetsAt") or (out.get(kind) or {}).get("resets_at")
            out[kind] = {"used_percentage": 100.0, "resets_at": resets}
    return out or None


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
                               cwd=tmp, env=_clean_env(), **_NO_WINDOW)
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
                               timeout=timeout, cwd=tmp, **_NO_WINDOW)
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
    """Runs the pings every N minutes inside `baton serve`.

    A check is skipped when a fresher reading already exists: if you just
    used the CLI, its status line (Claude) or session logs (Codex) already
    reported usage seconds ago, and asking again would only spend quota.
    The next check is then due N minutes after that reading, so either way
    the panel never shows a reading older than about N minutes.
    """

    def __init__(self, data_dir: Path, claude_minutes: int, codex_minutes: int, hours: str,
                 model: str = "haiku", codex_home: Path | None = None):
        self.data_dir = data_dir
        self.claude_minutes = claude_minutes
        self.codex_minutes = codex_minutes
        self.hours = hours
        self.model = model
        self.codex_home = codex_home
        self.last: dict[str, float] = {}
        self.results: dict[str, tuple[float, bool, str]] = {}

    @property
    def enabled(self) -> bool:
        return self.claude_minutes > 0 or self.codex_minutes > 0

    def due(self, name: str, minutes: int, now: float) -> bool:
        return minutes > 0 and now - self.last.get(name, 0) >= minutes * 60

    def newest_seen(self, name: str) -> float:
        """Unix time of the freshest stored reading for that provider (0 = none)."""
        try:
            if name == "claude":
                windows = claude_statusline_windows(self.data_dir)
            elif self.codex_home is not None:
                windows = codex_windows(self.codex_home)
            else:
                return 0.0
        except OSError:
            return 0.0
        return max((w.seen_at for w in windows), default=0.0)

    def check(self, name: str, now: float) -> None:
        """Ping one CLI, unless its stored reading is fresher than the interval."""
        minutes = self.claude_minutes if name == "claude" else self.codex_minutes
        freshest = self.newest_seen(name)
        if now - freshest < minutes * 60:
            # The CLI itself reported usage less than N minutes ago: spend no
            # quota; the next check waits for that reading to grow to N minutes.
            self.last[name] = min(freshest, now)
            age = int(max(0.0, now - freshest)) // 60
            self.results[name] = (now, True, f"skipped: reading only {age} min old")
            return
        self.last[name] = now
        ok, msg = (ping_claude(self.data_dir, model=self.model) if name == "claude"
                   else ping_codex())
        self.results[name] = (now, ok, msg)

    def tick(self) -> None:
        now = time.time()
        if not in_hours(self.hours):
            return
        if self.due("claude", self.claude_minutes, now):
            self.check("claude", now)
        if self.due("codex", self.codex_minutes, now):
            self.check("codex", now)
