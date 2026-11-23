"""The web pages: Projects, Quotas and Tasks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from . import __version__, quotas, usage
from .config import Settings
from .github import GitHubError, Hub
from .i18n import strings
from .ping import Scheduler
from .task_models import catalog
from .runner import AGENTS, LocalRepos, Runner, Task, TaskStore, new_id, opencode_models

HERE = Path(__file__).parent
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    hub = Hub(settings)
    scheduler = Scheduler(settings.data_dir, settings.ping_claude_minutes,
                          settings.ping_codex_minutes, settings.ping_hours, settings.ping_model)
    store = TaskStore(settings.data_dir)
    runner = Runner(store, LocalRepos(settings.local_root, settings.local_paths),
                    timeout=settings.task_timeout_minutes * 60, local_model=settings.local_model,
                    local_provider=settings.local_provider, git_name=settings.git_name,
                    git_email=settings.git_email, open_pr=hub.open_draft_pr)

    # Tasks that were running when Baton stopped can't be resumed: mark them.
    for old in store.all():
        if old.status in ("queued", "running"):
            old.status, old.message = "failed", "Baton stopped while this task was running."
            store.save(old)

    async def _loop():
        # Check once a minute whether a quota check is due; checks run in a
        # thread so the pages keep answering while a CLI is working.
        while True:
            try:
                await asyncio.to_thread(scheduler.tick)
            except Exception:  # never let the loop die
                pass
            await asyncio.sleep(60)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        loop_task = asyncio.create_task(_loop()) if scheduler.enabled else None
        yield
        if loop_task:
            loop_task.cancel()

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
        if origin:
            hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
            if urlparse(origin).netloc not in hosts:
                raise HTTPException(403, "Cross-site request refused.")

    t = strings(settings.lang)
    templates.env.globals.update(
        t=t, title=settings.title, version=__version__, lang=settings.lang, csrf=csrf_token,
        agents=AGENTS,
        # Changes whenever style.css changes, so browsers never keep an old copy.
        css_v=hashlib.sha256((HERE / "static" / "style.css").read_bytes()).hexdigest()[:10])
    templates.env.filters["md"] = _render_md
    templates.env.filters["dur"] = _duration
    templates.env.filters["tok"] = usage.human
    usage_cache: dict = {}

    def token_usage() -> dict:
        # Reading the logs takes a moment: reuse the result for 5 minutes.
        if not usage_cache or time.time() - usage_cache["at"] > 300:
            usage_cache.update(at=time.time(), data=usage.collect(
                settings.claude_home, settings.codex_home, settings.opencode_db))
        return usage_cache["data"]

    go_state: dict = {"at": 0.0, "msg": ""}

    def refresh_go() -> str:
        # OpenCode Go's usage endpoint costs nothing: ask at most every 5 minutes.
        if settings.opencode_go_key and time.time() - go_state["at"] > 300:
            ok, msg = quotas.fetch_opencode_go(settings.opencode_go_key, settings.data_dir)
            go_state.update(at=time.time(), msg="" if ok else msg)
        return go_state["msg"]

    def page(request: Request, name: str, nav: str, **ctx):
        return templates.TemplateResponse(request, name, {"nav": nav, **ctx})

    # ------------------------------------------------------------- projects
    @app.get("/", response_class=HTMLResponse)
    async def projects_page(request: Request, refresh: int = 0):
        projects, errors = await hub.load(force=bool(refresh))
        groups = {k: [p for p in projects if p.status.status == k]
                  for k in ("active", "planned", "paused", "done")}
        running = {x.repo for x in store.all() if x.status in ("queued", "running")}
        return page(request, "projects.html", "projects", groups=groups, count=len(projects),
                    errors=errors, no_token=not settings.github_token, running=running)

    @app.get("/p/{pid:path}", response_class=HTMLResponse)
    async def project(request: Request, pid: str):
        projects, _ = await hub.load()
        for p in projects:
            if p.id == pid:
                return page(request, "project.html", "projects", p=p)
        raise HTTPException(404, "Unknown project")

    # --------------------------------------------------------------- quotas
    @app.get("/quotas", response_class=HTMLResponse)
    async def quotas_page(request: Request):
        go_error = await asyncio.to_thread(refresh_go)
        return page(request, "quotas.html", "quotas", go_error=go_error,
                    providers=quotas.by_provider(quotas.all_windows(settings.data_dir, settings.codex_home)),
                    sched=scheduler, tokens=await asyncio.to_thread(token_usage),
                    sched_results={k: {"age": time.time() - v[0], "ok": v[1], "msg": v[2]}
                                   for k, v in scheduler.results.items()})

    # ---------------------------------------------------------------- tasks
    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request):
        tasks = store.all()
        return page(request, "tasks.html", "tasks", tasks=tasks,
                    any_running=any(x.status in ("queued", "running") for x in tasks))

    @app.get("/tasks/new", response_class=HTMLResponse)
    async def task_new(request: Request, repo: str = "", error: str = ""):
        projects, _ = await hub.load()
        repos = sorted({p.repo for p in projects})
        local = {r: bool(runner.repos.get(r)) for r in repos}
        available = runner.agents_available()
        agents = [a for a in ("claude-code", "codex") if not available[a]]
        models, *choices = await asyncio.gather(
            asyncio.to_thread(opencode_models) if not available["opencode"] else asyncio.sleep(0, result=[]),
            *(asyncio.to_thread(catalog, a) for a in agents))
        return page(request, "task_new.html", "tasks", repos=repos, selected=repo,
                    local=local, available=available, oc_models=models, catalogs=dict(zip(agents, choices)),
                    cloud=settings.cloud_tasks, error=error[:300],
                    has_token=bool(settings.github_token))

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str):
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "Unknown task")
        return page(request, "task.html", "tasks", task=task, log=store.log_tail(task.id))

    @app.post("/tasks")
    async def task_create(request: Request, repo: str = Form(...), where: str = Form(...),
                          title: str = Form(...), details: str = Form(""),
                          open_pr: str = Form(""), oc_model: str = Form(""), csrf: str = Form(""),
                          model: str = Form(""), effort: str = Form("")):
        check_post(request, csrf)
        projects, _ = await hub.load()
        by_repo = {p.repo: p for p in projects}
        if repo not in by_repo:
            raise HTTPException(400, "Unknown repository")
        title, details = title.strip()[:200], details.strip()[:5000]
        if not title:
            raise HTTPException(400, "A title is needed")

        def back(msg: str):
            return RedirectResponse(f"/tasks/new?repo={quote(repo)}&error={quote(msg)}",
                                    status_code=303)

        if where == "cloud":
            if not settings.cloud_tasks:
                raise HTTPException(400, "Cloud tasks are turned off")
            body = (f"@claude {details or title}\n\n---\n"
                    "Task created from Baton. Follow this repository's AGENTS.md / CLAUDE.md, "
                    "open a pull request with the change, and update STATUS.md in it. "
                    "Leave a final reply on this issue in the owner's language explaining what you did, "
                    "how and why, what you verified, any problems and what remains.")
            try:
                issue = await hub.create_issue(repo, title, body)
            except GitHubError as exc:
                return back(str(exc))
            task = Task(id=new_id(title), repo=repo, title=title, details=details,
                        agent="claude-code", where="cloud", created_at=time.time(),
                        status="sent", issue_url=issue["url"],
                        message="Sent to GitHub Actions.")
            store.save(task)
            return RedirectResponse(f"/tasks/{task.id}", status_code=303)

        agent = where.removeprefix("local:")
        if not where.startswith("local:") or agent not in AGENTS:
            raise HTTPException(400, "Unknown destination")
        reason = runner.agents_available().get(agent, "unknown agent")
        if reason:
            return back(reason)
        if not runner.repos.get(repo):
            return back("There is no local clone of this repository on this computer.")
        if runner.busy(repo):
            return back("A task is already running on this repository; wait for it to finish.")
        if agent in ("codex", "claude-code") and (model or effort):
            options = await asyncio.to_thread(catalog, agent)
            if model not in options or (effort and effort not in options[model]["efforts"]):
                return back(strings(settings.lang)["invalid_model"])
        elif agent == "opencode":
            # Only a model OpenCode itself lists is accepted.
            if oc_model not in await asyncio.to_thread(opencode_models):
                return back("Choose one of the models OpenCode lists.")
            model, effort = oc_model, ""
        else:
            model, effort = "", ""
        task_id = new_id(title)
        task = Task(id=task_id, repo=repo, title=title, details=details, agent=agent,
                    where="local", created_at=time.time(), branch=f"baton/{task_id}",
                    base=by_repo[repo].branch, open_pr=bool(open_pr), model=model, effort=effort)
        runner.start(task)
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    # ------------------------------------------------------------------ api
    @app.get("/api/projects")
    async def api_projects(refresh: int = 0):
        projects, errors = await hub.load(force=bool(refresh))
        return JSONResponse({"projects": [p.to_dict() for p in projects], "errors": errors})

    @app.get("/api/quotas")
    async def api_quotas():
        await asyncio.to_thread(refresh_go)
        return JSONResponse({"windows": quotas.all_windows(settings.data_dir, settings.codex_home)})

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
