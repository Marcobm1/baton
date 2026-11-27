"""Shared per-application context and request helpers for FastAPI route modules."""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from .. import quotas, usage
from ..agent_chat import AgentChats
from ..alerts import AlertEngine
from ..auth import LoginRateLimiter
from ..chat_history import ChatHistory
from ..config import Settings
from ..local_harness import LocalHarnessManager
from ..machines import MachineStore
from ..models import LocalModels
from ..notify import Notifier
from ..ping import Scheduler, fetch_gemini_quota
from ..ranking import Ranking
from ..task_report import task_report
from ..task_store import Task, TaskStore
from ..townhall_bridge import TownHallService


@dataclass
class AppContext:
    """All per-app services, caches, security tokens, and injectable lookups.

    Stored on ``app.state.ctx`` so every ``create_app()`` instance stays
    completely isolated (no module-level singletons).
    """

    settings: Settings
    hub: Any
    scheduler: Scheduler
    store: TaskStore
    models: LocalModels
    machines: MachineStore
    alerts: AlertEngine
    notifier: Notifier
    agent_chats: AgentChats
    local_harness: LocalHarnessManager
    chat_history: ChatHistory
    townhall: TownHallService
    ranking: Ranking
    runner: Any
    updater: Any
    update_requested: asyncio.Event
    templates: Jinja2Templates
    t: dict[str, str]
    csrf_token: str
    login_csrf: str
    access_key: str
    auth_enabled: bool
    login_limiter: LoginRateLimiter
    catalog_fn: Callable[[str], dict]
    opencode_models_fn: Callable[[], list[str]]
    find_opencode_fn: Callable[[], str | None]
    find_agy_fn: Callable[[], str | None]
    find_claude_fn: Callable[[], str | None] = lambda: None
    find_codex_fn: Callable[[], str | None] = lambda: None
    usage_cache: dict = field(default_factory=dict)
    usage_ttl: float = 300.0
    _usage_lock: threading.Lock = field(default_factory=threading.Lock)
    _usage_refreshing: bool = False
    task_sse_poll: float = 1.0
    task_sse_keepalive: float = 15.0
    go_state: dict = field(default_factory=lambda: {"at": 0.0, "msg": ""})
    gemini_state: dict = field(default_factory=lambda: {"at": 0.0, "msg": ""})
    _gemini_lock: threading.Lock = field(default_factory=threading.Lock)
    _gemini_refreshing: bool = False

    def page(self, request: Request, name: str, nav: str, **ctx):
        return self.templates.TemplateResponse(request, name, {"nav": nav, **ctx})

    @staticmethod
    def check_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin:
            hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
            if urlparse(origin).netloc not in hosts:
                raise HTTPException(403, "Cross-site request refused.")

    @staticmethod
    def is_https(request: Request) -> bool:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or proto == "https"

    def check_post(self, request: Request, token: str) -> None:
        if not secrets.compare_digest(token or "", self.csrf_token):
            raise HTTPException(403, "Invalid form token; reload the page.")
        self.check_origin(request)

    def refresh_usage(self, *, force: bool = False) -> dict:
        """Compute token usage from disk logs and update the in-memory cache."""
        with self._usage_lock:
            if not force and self.usage_cache and time.time() - self.usage_cache["at"] <= self.usage_ttl:
                return self.usage_cache["data"]
            if self._usage_refreshing and self.usage_cache:
                return self.usage_cache["data"]
            self._usage_refreshing = True
        try:
            data = usage.collect(
                self.settings.claude_home,
                self.settings.codex_home,
                self.settings.opencode_db,
                data_dir=self.settings.data_dir,
            )
            with self._usage_lock:
                self.usage_cache = {"at": time.time(), "data": data}
            return data
        finally:
            with self._usage_lock:
                self._usage_refreshing = False

    def schedule_usage_refresh(self) -> None:
        """Kick off a background refresh if one is not already running."""
        with self._usage_lock:
            if self._usage_refreshing:
                return
            self._usage_refreshing = True

        def _worker() -> None:
            try:
                data = usage.collect(
                    self.settings.claude_home,
                    self.settings.codex_home,
                    self.settings.opencode_db,
                    data_dir=self.settings.data_dir,
                )
                with self._usage_lock:
                    self.usage_cache = {"at": time.time(), "data": data}
            except Exception:
                pass
            finally:
                with self._usage_lock:
                    self._usage_refreshing = False

        threading.Thread(target=_worker, daemon=True).start()

    def token_usage(self) -> dict:
        """Return cached token usage immediately without blocking HTTP requests."""
        with self._usage_lock:
            cached = self.usage_cache
        if cached:
            if time.time() - cached["at"] > self.usage_ttl:
                self.schedule_usage_refresh()
            return cached["data"]
        self.schedule_usage_refresh()
        return {"days": 7, "apps": []}

    def refresh_go(self) -> str:
        # OpenCode Go's usage endpoint costs nothing: ask at most every 5 minutes.
        if self.settings.opencode_go_key and time.time() - self.go_state["at"] > 300:
            ok, msg = quotas.fetch_opencode_go(self.settings.opencode_go_key, self.settings.data_dir)
            self.go_state.update(at=time.time(), msg="" if ok else msg)
        return self.go_state["msg"]

    def refresh_gemini(self) -> str:
        # `agy -p /usage` asks no model (0 tokens): at most every 5 minutes.
        with self._gemini_lock:
            if time.time() - self.gemini_state["at"] <= 300 or self._gemini_refreshing:
                return self.gemini_state["msg"]
            if not self.find_agy_fn():
                return self.gemini_state["msg"]
            self._gemini_refreshing = True
        try:
            ok, msg = fetch_gemini_quota(self.settings.data_dir)
            with self._gemini_lock:
                self.gemini_state.update(at=time.time(), msg="" if ok else msg)
                return self.gemini_state["msg"]
        finally:
            with self._gemini_lock:
                self._gemini_refreshing = False

    def schedule_gemini_refresh(self) -> None:
        """Kick off a background Gemini quota refresh if stale and not already running."""
        with self._gemini_lock:
            if time.time() - self.gemini_state["at"] <= 300 or self._gemini_refreshing:
                return
            if not self.find_agy_fn():
                return
            self._gemini_refreshing = True

        def _worker() -> None:
            try:
                ok, msg = fetch_gemini_quota(self.settings.data_dir)
                with self._gemini_lock:
                    self.gemini_state.update(at=time.time(), msg="" if ok else msg)
            except Exception:
                pass
            finally:
                with self._gemini_lock:
                    self._gemini_refreshing = False

        threading.Thread(target=_worker, daemon=True).start()

    def gemini_error(self) -> str:
        """Return cached Gemini quota error immediately without blocking HTTP requests."""
        self.schedule_gemini_refresh()
        with self._gemini_lock:
            return self.gemini_state["msg"]

    def followup_context(self, task: Task) -> str:
        summary = task.summary or task_report(task, self.store.read_log(task.id))["summary"]
        parts = [
            f"{self.t['original_task']}\n{task.title}\n{task.details}".rstrip(),
            f"{self.t['agent_summary']}\n{summary or self.t['no_summary']}",
        ]
        if task.pr_url:
            parts.append(f"PR: {task.pr_url}")
        parts.append(f"{self.t['your_reply']}\n")
        return "\n\n".join(parts)

    def chat_folders(self) -> list[dict]:
        """Where an agent conversation may work: Baton's local clones and the
        scratch folder. The browser only ever sends one of these keys."""
        out = [
            {"key": repo, "label": repo, "path": path, "branch": _git_branch(path)}
            for repo, path in sorted(self.runner.repos.all().items())
            if path.is_dir()
        ]
        if self.settings.chat_scratch:
            scratch = Path(self.settings.chat_scratch)
            out.append({
                "key": "scratch",
                "label": f"{self.settings.chat_scratch} ({self.t['ag_scratch']})",
                "path": scratch,
                "branch": _git_branch(scratch) if scratch.is_dir() else "",
            })
        return out


def _git_branch(path: Path) -> str:
    """Read the current git branch (or short detached HEAD SHA) directly from .git/HEAD."""
    dot_git = path / ".git"
    try:
        if dot_git.is_file():
            text = dot_git.read_text(encoding="utf-8", errors="ignore").strip()
            if not text.lower().startswith("gitdir:"):
                return ""
            raw = text.split(":", 1)[1].strip()
            git_dir = Path(raw)
            if not git_dir.is_absolute():
                git_dir = (path / git_dir).resolve()
        elif dot_git.is_dir():
            git_dir = dot_git
        else:
            return ""
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""
    if head.startswith("ref: refs/heads/"):
        return head[len("ref: refs/heads/"):].strip()
    if head.startswith("ref:"):
        return head.rsplit("/", 1)[-1].strip()
    if len(head) >= 7 and all(c in "0123456789abcdefABCDEF" for c in head[:7]):
        return head[:7]
    return ""


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


CTX = Depends(get_ctx)
