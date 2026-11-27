"""FastAPI application factory, middleware, background loops, and template wiring.

Route handlers live in focused `APIRouter` modules under `baton/routes/`:
  - `auth.py`     (/login, /logout)
  - `projects.py` (/, /p/...)
  - `quotas.py`   (/quotas, /self-update)
  - `ranking.py`  (/ranking, /ranking/{job})
  - `tasks.py`    (/tasks, /tasks/new, /tasks/{id}, priority, cancel, create)
  - `models.py`   (/models, /models/{id}/{action})
  - `chat.py`     (/chat, /api/agents/*, /api/chat/{model_id})
  - `api.py`      (/api/projects, /api/quotas, /api/models, /healthz)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from . import __version__, quotas, usage
from .agent_chat import AgentChats
from .alerts import AlertEngine
from .auth import COOKIE_NAME, LoginRateLimiter, safe_next_path, verify_session_token
from .chat_history import ChatHistory
from .config import Settings
from .github import Hub
from .i18n import ordinal, strings
from .local_harness import LocalHarnessManager
from .machines import MachineStore
from .models import LocalModels
from .notify import Notifier
from .ping import Scheduler, find_agy, find_claude, find_codex
from .routes import AppContext, register_routes
from .runner import (
    AGENTS as AGENTS,
    NEXT_TASK_REQUEST as NEXT_TASK_REQUEST,
    LocalRepos,
    Runner,
    TaskStore,
    find_opencode,
    opencode_models,
)
from .self_update import SelfUpdate
from .task_models import catalog

HERE = Path(__file__).parent
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_relative_link = re.compile(r"\]\((?![a-zA-Z][a-zA-Z0-9+.-]*:|#|/)([^)\s]+)\)")


def _render_repo_md(text: str, p) -> str:
    """Markdown from a STATUS.md: relative links point to the file on GitHub."""
    base = f"https://github.com/{p.repo}/blob/{p.branch}/"
    folder = p.path.rsplit("/", 1)[0] + "/" if "/" in p.path else ""
    return _render_md(_relative_link.sub(lambda m: f"]({base}{folder}{m[1]})", text or ""))


def _render_md(text: str) -> str:
    text = text.replace("- [x] ", "- ☑ ").replace("- [X] ", "- ☑ ").replace("- [ ] ", "- ☐ ")
    return _md.render(text)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(max(0, seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def create_app(settings: Settings | None = None, *, restart=None) -> FastAPI:
    settings = settings or Settings.from_env()
    hub = Hub(settings)
    scheduler = Scheduler(
        settings.data_dir, settings.ping_claude_minutes,
        settings.ping_codex_minutes, settings.ping_hours, settings.ping_model,
        codex_home=settings.codex_home,
    )
    store = TaskStore(settings.data_dir)
    models = LocalModels(settings.data_dir)
    machines = MachineStore(settings.data_dir)
    notifier = Notifier(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        webhook_url=settings.alert_webhook_url,
        notify_tasks=settings.notify_tasks,
        data_dir=settings.data_dir,
        lang=settings.lang,
    )
    alerts = AlertEngine(settings.data_dir, on_opened=notifier.notify_alert)
    agent_chats = AgentChats(
        data_dir=settings.data_dir,
        sandbox_image=settings.sandbox_image,
        on_permission_wait=notifier.notify_permission,
    )
    local_harness = LocalHarnessManager(
        on_permission_wait=notifier.notify_permission,
        search_url=settings.search_url,
        brave_api_key=settings.brave_api_key,
        tavily_api_key=settings.tavily_api_key,
    )
    chat_history = ChatHistory(settings.data_dir)
    ranking = store.ranking

    def quota_left(agent: str) -> float | None:
        """% left in the agent's 5 h window (Claude Code or Codex), if known."""
        provider = {"claude-code": "claude", "codex": "codex"}.get(agent)
        for v in quotas.all_windows(settings.data_dir, settings.codex_home):
            if v["provider"] == provider and v["kind"] == "5h":
                return v["left"]
        return None

    runner = Runner(
        store, LocalRepos(settings.local_root, settings.local_paths),
        timeout=settings.task_timeout_minutes * 60, local_model=settings.local_model,
        local_provider=settings.local_provider, git_name=settings.git_name,
        git_email=settings.git_email,
        # With auto-merge the PR can't be a draft: GitHub won't merge drafts.
        open_pr=lambda *a: hub.open_pr(*a, draft=not settings.auto_merge),
        github=hub, auto_merge=settings.auto_merge, quota_left=quota_left,
        max_parallel=settings.max_parallel,
        review_models={"claude-code": (settings.review_claude_model, settings.review_claude_effort)},
        owner_tag=settings.owner_tag,
    )
    runner.on_finished = notifier.notify_task

    # Tasks that were running when Baton stopped can't be resumed: mark them
    # failed. Queued tasks keep their place and start when the server is up.
    runner.recover()
    updater = SelfUpdate(HERE.resolve().parent, runner, settings.self_update, restart)
    update_requested = asyncio.Event()

    templates = Jinja2Templates(directory=HERE / "templates")
    # Anti-CSRF: a random value that only pages served by Baton know. A form
    # posted from any other website can't include it, so it is rejected.
    csrf_token = secrets.token_urlsafe(32)
    # Separate token for /login so an unauthenticated page never exposes csrf_token.
    login_csrf = secrets.token_urlsafe(32)
    access_key = settings.access_key
    auth_enabled = bool(access_key)
    t = strings(settings.lang)

    templates.env.globals.update(
        t=t, title=settings.title, version=__version__, lang=settings.lang, csrf=csrf_token,
        agents=AGENTS, auth_enabled=auth_enabled, active_alerts_count=alerts.active_count,
        # Changes whenever any stylesheet or script changes, so browsers (and
        # phones, which cache hard) never keep an old copy after an update.
        css_v=hashlib.sha256(b"".join(
            f.read_bytes() for f in sorted((HERE / "static").glob("*")) if f.suffix in (".css", ".js")
        )).hexdigest()[:10],
    )
    templates.env.filters["md"] = _render_md
    templates.env.filters["repo_md"] = _render_repo_md
    templates.env.filters["dur"] = _duration
    templates.env.filters["ordinal"] = lambda n: ordinal(n, settings.lang)
    templates.env.filters["tok"] = usage.human
    # {**a, **b} for query strings: a None value drops that parameter.
    templates.env.filters["combine"] = lambda base, changes: {**base, **changes}
    templates.env.filters["when"] = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"

    ctx = AppContext(
        settings=settings,
        hub=hub,
        scheduler=scheduler,
        store=store,
        models=models,
        machines=machines,
        alerts=alerts,
        notifier=notifier,
        agent_chats=agent_chats,
        local_harness=local_harness,
        chat_history=chat_history,
        ranking=ranking,
        runner=runner,
        updater=updater,
        update_requested=update_requested,
        templates=templates,
        t=t,
        csrf_token=csrf_token,
        login_csrf=login_csrf,
        access_key=access_key,
        auth_enabled=auth_enabled,
        login_limiter=LoginRateLimiter(),
        # Resolved through module globals at call time so tests patching web.* work cleanly.
        catalog_fn=lambda agent: catalog(agent),
        opencode_models_fn=lambda: opencode_models(),
        find_opencode_fn=lambda: find_opencode(),
        find_agy_fn=lambda: find_agy(),
        find_claude_fn=lambda: find_claude(),
        find_codex_fn=lambda: find_codex(),
    )

    async def update_loop():
        while not updater.restarting:
            update_requested.clear()
            await asyncio.to_thread(updater.check)
            if not updater.restarting:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(update_requested.wait(), 300)

    def _quota_tick():
        # Refresh token usage, local machine metrics, and evaluate alerts in the background.
        ctx.refresh_usage()
        cards = machines.list_machines(refresh_local=True)
        try:
            gpu_busy = any(m.get("uses_gpu") for m in models.overview(settings.lang).get("models") or [])
        except Exception:
            gpu_busy = False
        alerts.evaluate(
            cards,
            local_gpu_busy=gpu_busy,
            recent_samples_fn=lambda mid: machines.recent_samples(mid, hours=24.0, limit=120),
        )
        if scheduler.enabled:
            scheduler.tick()
        # OpenCode Go's usage endpoint and agy's /usage are free to ask: keep
        # those cards fresh even when nobody has the page open (every 5 minutes).
        if settings.opencode_go_key:
            ctx.refresh_go()
        if find_agy():
            ctx.refresh_gemini()

    async def _loop(stopping: asyncio.Event):
        # Check once a minute whether usage or quota refreshes are due; work runs
        # in a thread so HTTP handlers stay responsive.
        while not stopping.is_set():
            try:
                await asyncio.to_thread(_quota_tick)
            except Exception:  # never let the loop die
                pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), 60)

    def thermal_check():
        hot = models.check_temperature()
        try:
            runner.set_overheated(hot)
        finally:
            if hot:
                models.stop_overheated_models()

    async def thermal_loop(stopping):
        while not stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), 5)
            if not stopping.is_set():
                try:
                    await asyncio.to_thread(thermal_check)
                except Exception:
                    logging.getLogger(__name__).exception("GPU temperature check failed")

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await asyncio.to_thread(thermal_check)
        await asyncio.to_thread(runner.dispatch)   # the queue left by the last run
        stopping = asyncio.Event()
        monitor = asyncio.create_task(thermal_loop(stopping))
        # Background loop keeps token usage and quota cards fresh without blocking pages.
        loop_task = asyncio.create_task(_loop(stopping))
        update_task = None
        if updater.enabled:
            loop = asyncio.get_running_loop()

            def finished(task):
                notifier.notify_task(task)
                if updater.reason == "busy" or (task.merged and task.repo.lower() == updater.repo):
                    loop.call_soon_threadsafe(update_requested.set)

            runner.on_finished = finished
            update_task = asyncio.create_task(update_loop())
        try:
            yield
        finally:
            stopping.set()
            await monitor
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
        await asyncio.to_thread(agent_chats.close_all)   # no OpenCode left behind
        await asyncio.to_thread(local_harness.close_all)
        runner.on_finished = notifier.notify_task
        if update_task:
            update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await update_task

    app = FastAPI(title="Baton", version=__version__, docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.ctx = ctx
    app.state.agent_chats = agent_chats
    app.state.local_harness = local_harness
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if not auth_enabled:
            return await call_next(request)
        path = request.url.path
        if (path == "/healthz" or path.startswith("/static/")) and request.method in ("GET", "HEAD"):
            return await call_next(request)
        if path == "/login" and request.method in ("GET", "HEAD", "POST"):
            return await call_next(request)
        # Machine heartbeats authenticate strictly with their own per-machine Bearer token.
        if path == "/api/machines/heartbeat" and request.method == "POST":
            return await call_next(request)
        if verify_session_token(request.cookies.get(COOKIE_NAME), access_key):
            return await call_next(request)
        if request.method in ("GET", "HEAD") and not path.startswith("/api/"):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(safe_next_path(target), safe='/?=&')}", status_code=303)
        return JSONResponse({"detail": "Authentication required."}, status_code=401)

    register_routes(app)
    return app
