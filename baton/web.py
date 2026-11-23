"""The web page: projects from GitHub + quota windows."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import datetime as dt
import time
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from urllib.parse import quote, urlparse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from . import __version__, quotas
from .config import Settings
from .github import GitHubError, Hub
from .i18n import strings
from .ping import Scheduler

HERE = Path(__file__).parent
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


def _render_md(text: str) -> str:
    text = text.replace("- [x] ", "- ☑ ").replace("- [X] ", "- ☑ ").replace("- [ ] ", "- ☐ ")
    return _md.render(text)


def _duration(seconds: float | None, lang: str) -> str:
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    hub = Hub(settings)
    scheduler = Scheduler(settings.data_dir, settings.ping_claude_minutes,
                          settings.ping_codex_minutes, settings.ping_hours, settings.ping_model)

    async def _loop():
        # Check once a minute whether a ping is due; pings run in a thread so
        # the page keeps answering while a CLI is working.
        while True:
            try:
                await asyncio.to_thread(scheduler.tick)
            except Exception:  # never let the loop die
                pass
            await asyncio.sleep(60)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(_loop()) if scheduler.enabled else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Baton", version=__version__, docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    # Anti-CSRF: a random value that only pages served by Baton know. A form
    # posted from any other website can't include it, so it is rejected.
    csrf_token = secrets.token_urlsafe(32)

    def check_post(request: Request, token: str) -> None:
        if not secrets.compare_digest(token or "", csrf_token):
            raise HTTPException(403, "Invalid form token; reload the page.")
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Cross-site request refused.")
    t = strings(settings.lang)
    templates.env.globals.update(t=t, title=settings.title, version=__version__, lang=settings.lang,
                                 csrf=csrf_token)
    templates.env.filters["md"] = _render_md
    templates.env.filters["dur"] = lambda s: _duration(s, settings.lang)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, refresh: int = 0, created: str = "", error: str = ""):
        projects, errors = await hub.load(force=bool(refresh))
        groups = {k: [p for p in projects if p.status.status == k]
                  for k in ("active", "planned", "paused", "done")}
        return templates.TemplateResponse(request, "index.html", {
            "groups": groups,
            "count": len(projects),
            "errors": errors,
            "providers": quotas.by_provider(quotas.all_windows(settings.data_dir, settings.codex_home)),
            "no_token": not settings.github_token,
            "sched": scheduler,
            "sched_results": {k: {"age": time.time() - v[0], "ok": v[1], "msg": v[2]}
                              for k, v in scheduler.results.items()},
            "now": dt.datetime.now().strftime("%H:%M"),
            "repos": sorted({p.repo for p in projects}),
            "created": created if created.startswith("https://github.com/") else "",
            "error": error[:200],
        })

    @app.get("/p/{pid:path}", response_class=HTMLResponse)
    async def project(request: Request, pid: str):
        projects, _ = await hub.load()
        for p in projects:
            if p.id == pid:
                return templates.TemplateResponse(request, "project.html", {
                    "p": p, "repos": [p.repo], "created": "", "error": ""})
        raise HTTPException(404, "Unknown project")

    @app.get("/api/projects")
    async def api_projects(refresh: int = 0):
        projects, errors = await hub.load(force=bool(refresh))
        return JSONResponse({"projects": [p.to_dict() for p in projects], "errors": errors})

    @app.get("/api/quotas")
    async def api_quotas():
        return JSONResponse({"windows": quotas.all_windows(settings.data_dir, settings.codex_home)})

    @app.post("/quotas/manual")
    async def manual(request: Request, provider: str = Form(...), kind: str = Form(...),
                     used: float = Form(...), resets_at: str = Form(""), csrf: str = Form("")):
        check_post(request, csrf)
        try:
            ts = float(resets_at) if resets_at.strip() else None
            quotas.add_manual(settings.data_dir, provider, kind, used, ts)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/#quotas", status_code=303)

    @app.post("/tasks")
    async def new_task(request: Request, repo: str = Form(...), agent: str = Form("claude"),
                       title: str = Form(...), details: str = Form(""), csrf: str = Form("")):
        check_post(request, csrf)
        projects, _ = await hub.load()
        if repo not in {p.repo for p in projects}:
            raise HTTPException(400, "Unknown repository")
        if agent != "claude":
            raise HTTPException(400, "Only Claude tasks are supported for now")
        title = title.strip()[:200]
        body = (f"@claude {details.strip() or title}\n\n---\n"
                "Task created from Baton. Follow this repository's AGENTS.md / CLAUDE.md, "
                "open a pull request with the change, and update STATUS.md in it.")
        try:
            issue = await hub.create_issue(repo, title, body)
        except GitHubError as exc:
            return RedirectResponse(f"/?error={quote(str(exc))}#tasks", status_code=303)
        return RedirectResponse(f"/?created={quote(issue['url'], safe=':/')}#tasks", status_code=303)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
