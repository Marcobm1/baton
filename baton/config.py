"""Settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value and value[0] in "\"'":
            value = value[1:].split(value[0], 1)[0]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        key = key.strip()
        os.environ.setdefault(key, value)


def _csv(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _gh_cli_token() -> str:
    """Use the GitHub CLI's login if no token is configured (local use only)."""
    if not shutil.which("gh"):
        return ""
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


@dataclass
class Settings:
    github_token: str = ""
    token_source: str = "none"
    owner: str = ""
    repos: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    include_forks: bool = False
    cache_seconds: int = 300
    stale_days: int = 1
    data_dir: Path = Path.home() / ".baton"
    codex_home: Path = Path.home() / ".codex"
    lang: str = "en"
    title: str = "Baton"
    ping_claude_minutes: int = 0
    ping_codex_minutes: int = 0
    ping_hours: str = ""
    ping_model: str = "haiku"
    local_root: str = ""
    local_paths: str = ""
    task_timeout_minutes: int = 60
    local_model: str = ""
    local_provider: str = "ollama"
    git_name: str = "Baton"
    git_email: str = "baton@localhost"
    cloud_tasks: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(Path(os.environ.get("BATON_ENV_FILE", ".env")))
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        source = "GITHUB_TOKEN" if token else "none"
        if not token and os.environ.get("BATON_USE_GH_CLI", "1") != "0":
            token = _gh_cli_token()
            source = "gh auth token" if token else "none"
        codex_home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
        return cls(
            github_token=token,
            token_source=source,
            owner=os.environ.get("BATON_OWNER", "").strip(),
            repos=_csv("BATON_REPOS"),
            exclude=[x.lower() for x in _csv("BATON_EXCLUDE")],
            include_forks=os.environ.get("BATON_INCLUDE_FORKS", "0") == "1",
            cache_seconds=int(os.environ.get("BATON_CACHE_SECONDS", "300")),
            stale_days=int(os.environ.get("BATON_STALE_DAYS", "1")),
            data_dir=Path(os.environ.get("BATON_DATA_DIR") or Path.home() / ".baton"),
            codex_home=Path(codex_home),
            lang=os.environ.get("BATON_LANG", "en").strip().lower()[:2] or "en",
            title=os.environ.get("BATON_TITLE", "Baton"),
            ping_claude_minutes=int(os.environ.get("BATON_PING_CLAUDE_MINUTES", "0") or 0),
            ping_codex_minutes=int(os.environ.get("BATON_PING_CODEX_MINUTES", "0") or 0),
            ping_hours=os.environ.get("BATON_PING_HOURS", ""),
            ping_model=os.environ.get("BATON_PING_MODEL", "haiku"),
            local_root=os.environ.get("BATON_LOCAL_ROOT", ""),
            local_paths=os.environ.get("BATON_LOCAL_PATHS", ""),
            task_timeout_minutes=int(os.environ.get("BATON_TASK_TIMEOUT_MINUTES", "60") or 60),
            local_model=os.environ.get("BATON_LOCAL_MODEL", ""),
            local_provider=os.environ.get("BATON_LOCAL_PROVIDER", "ollama"),
            git_name=os.environ.get("BATON_GIT_NAME", "Baton"),
            git_email=os.environ.get("BATON_GIT_EMAIL", "baton@localhost"),
            cloud_tasks=os.environ.get("BATON_CLOUD_TASKS", "1") != "0",
        )
