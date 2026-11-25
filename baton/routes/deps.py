"""Shared per-application context and request helpers for FastAPI route modules."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates

from .. import quotas, usage
from ..agent_chat import AgentChats
from ..auth import LoginRateLimiter
from ..config import Settings
from ..models import LocalModels
from ..ping import Scheduler, fetch_gemini_quota
from ..ranking import Ranking
from ..task_report import task_report
from ..task_store import Task, TaskStore


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
    agent_chats: AgentChats
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
    usage_cache: dict = field(default_factory=dict)
    go_state: dict = field(default_factory=lambda: {"at": 0.0, "msg": ""})
    gemini_state: dict = field(default_factory=lambda: {"at": 0.0, "msg": ""})

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

    def token_usage(self) -> dict:
        # Reading the logs takes a moment: reuse the result for 5 minutes.
        if not self.usage_cache or time.time() - self.usage_cache["at"] > 300:
            self.usage_cache.update(
                at=time.time(),
                data=usage.collect(
                    self.settings.claude_home,
                    self.settings.codex_home,
                    self.settings.opencode_db,
                    data_dir=self.settings.data_dir,
                ),
            )
        return self.usage_cache["data"]

    def refresh_go(self) -> str:
        # OpenCode Go's usage endpoint costs nothing: ask at most every 5 minutes.
        if self.settings.opencode_go_key and time.time() - self.go_state["at"] > 300:
            ok, msg = quotas.fetch_opencode_go(self.settings.opencode_go_key, self.settings.data_dir)
            self.go_state.update(at=time.time(), msg="" if ok else msg)
        return self.go_state["msg"]

    def refresh_gemini(self) -> str:
        # `agy -p /usage` asks no model (0 tokens): at most every 5 minutes.
        if time.time() - self.gemini_state["at"] > 300 and self.find_agy_fn():
            ok, msg = fetch_gemini_quota(self.settings.data_dir)
            self.gemini_state.update(at=time.time(), msg="" if ok else msg)
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
            {"key": repo, "label": repo, "path": path}
            for repo, path in sorted(self.runner.repos.all().items())
            if path.is_dir()
        ]
        if self.settings.chat_scratch:
            out.append({
                "key": "scratch",
                "label": f"{self.settings.chat_scratch} ({self.t['ag_scratch']})",
                "path": Path(self.settings.chat_scratch),
            })
        return out


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


CTX = Depends(get_ctx)
