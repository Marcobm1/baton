"""The web pages: Projects, Quotas and Tasks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from . import __version__, quotas, usage
from .config import Settings
from .github import GitHubError, Hub
from .i18n import ordinal, strings
from .ping import Scheduler
from .ranking import CRITERIA, Ranking
from .project_detail import finished_items, sections
from .task_models import catalog
from . import task_defaults
from .task_report import task_report
from .self_update import SelfUpdate
from .runner import UpdateInProgress
from .runner import (AGENTS, PRIORITIES, NEXT_TASK_REQUEST, SUMMARY_REQUEST, next_task_request, LocalRepos, Runner, Task, TaskStore,
                     extract_summary, new_id, opencode_models, queue_key)

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


def create_app(settings: Settings | None = None, *, restart=None) -> FastAPI:
    settings = settings or Settings.from_env()
    hub = Hub(settings)
    scheduler = Scheduler(settings.data_dir, settings.ping_claude_minutes,
                          settings.ping_codex_minutes, settings.ping_hours, settings.ping_model,
                          codex_home=settings.codex_home)
    store = TaskStore(settings.data_dir)
    ranking = Ranking(settings.data_dir)

    def quota_left(agent: str) -> float | None:
        """% left in the agent's 5 h window (Claude Code or Codex), if known."""
        provider = {"claude-code": "claude", "codex": "codex"}.get(agent)
        for v in quotas.all_windows(settings.data_dir, settings.codex_home):
            if v["provider"] == provider and v["kind"] == "5h":
                return v["left"]
        return None
    runner = Runner(store, LocalRepos(settings.local_root, settings.local_paths),
                    timeout=settings.task_timeout_minutes * 60, local_model=settings.local_model,
                    local_provider=settings.local_provider, git_name=settings.git_name,
                    git_email=settings.git_email,
                    # With auto-merge the PR can't be a draft: GitHub won't merge drafts.
                    open_pr=lambda *a: hub.open_pr(*a, draft=not settings.auto_merge),
                    github=hub, auto_merge=settings.auto_merge, quota_left=quota_left,
                    max_parallel=settings.max_parallel)

    # Tasks that were running when Baton stopped can't be resumed: mark them
    # failed. Queued tasks keep their place and start when the server is up.
    runner.recover()
    updater = SelfUpdate(HERE.resolve().parent, runner, settings.self_update, restart)
    update_requested = asyncio.Event()

    async def update_loop():
        while not updater.restarting:
            update_requested.clear()
            await asyncio.to_thread(updater.check)
            if not updater.restarting:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(update_requested.wait(), 300)

    def _quota_tick():
        scheduler.tick()
        # OpenCode Go's usage endpoint is free to ask: keep that card fresh
        # even when nobody has the page open (throttled to every 5 minutes).
        refresh_go()

    async def _loop():
        # Check once a minute whether a quota check is due; checks run in a
        # thread so the pages keep answering while a CLI is working.
        while True:
            try:
                await asyncio.to_thread(_quota_tick)
            except Exception:  # never let the loop die
                pass
            await asyncio.sleep(60)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await asyncio.to_thread(runner.dispatch)   # the queue left by the last run
        # The loop keeps every quota source at most ~N minutes old (pings when
        # enabled, OpenCode Go when a key is set), not only while pages are open.
        loop_task = (asyncio.create_task(_loop())
                     if scheduler.enabled or settings.opencode_go_key else None)
        update_task = None
        if updater.enabled:
            loop = asyncio.get_running_loop()

            def finished(task):
                if updater.reason == "busy" or (task.merged and task.repo.lower() == updater.repo):
                    loop.call_soon_threadsafe(update_requested.set)

            runner.on_finished = finished
            update_task = asyncio.create_task(update_loop())
        yield
        runner.on_finished = None
        if update_task:
            update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await update_task
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
    templates.env.filters["ordinal"] = lambda n: ordinal(n, settings.lang)
    templates.env.filters["tok"] = usage.human
    templates.env.filters["when"] = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"
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
        drafts = {p.id: next((item for item in sections(p.status.body).checklist if not item.checked), None)
                  for p in projects}
        return page(request, "projects.html", "projects", groups=groups, count=len(projects), drafts=drafts,
                    errors=errors, no_token=not settings.github_token, running=running)

    @app.get("/p/{pid:path}", response_class=HTMLResponse)
    async def project(request: Request, pid: str):
        projects, _ = await hub.load()
        for p in projects:
            if p.id == pid:
                content = sections(p.status.body)
                tasks = [task for task in store.all() if task.repo.casefold() == p.repo.casefold()]
                active = sorted((task for task in tasks if task.status in ("running", "queued")),
                                key=lambda task: (task.status == "queued", queue_key(task)))
                upcoming = [item for item in content.checklist if not item.checked]
                return page(request, "project.html", "projects", p=p, content=content,
                            upcoming=upcoming, checked=len(content.checklist) - len(upcoming),
                            finished=finished_items(content, tasks), active=active,
                            queue=runner.queue_info(), pulls=await hub.open_pulls(p.repo))
        raise HTTPException(404, "Unknown project")

    # --------------------------------------------------------------- quotas
    @app.get("/quotas", response_class=HTMLResponse)
    async def quotas_page(request: Request):
        go_error = await asyncio.to_thread(refresh_go)
        return page(request, "quotas.html", "quotas", go_error=go_error, updater=updater,
                    providers=quotas.by_provider(quotas.all_windows(settings.data_dir, settings.codex_home)),
                    sched=scheduler, tokens=await asyncio.to_thread(token_usage),
                    sched_results={k: {"age": time.time() - v[0], "ok": v[1], "msg": v[2]}
                                   for k, v in scheduler.results.items()})

    @app.post("/self-update")
    async def update_now(request: Request, csrf: str = Form("")):
        check_post(request, csrf)
        if updater.enabled:
            update_requested.set()
        return RedirectResponse("/quotas#self-update", status_code=303)

    @app.get("/ranking", response_class=HTMLResponse)
    def ranking_page(request: Request):
        models, jobs = ranking.overview()
        return page(request, "ranking.html", "ranking", models=models, jobs=jobs)

    @app.get("/ranking/{job}", response_class=HTMLResponse)
    def delegation_page(request: Request, job: int):
        details, reviews = ranking.detail(job)
        if details is None:
            raise HTTPException(404, "Unknown delegation")
        return page(request, "delegation.html", "ranking", job=details, reviews=reviews,
                    criteria=CRITERIA)

    @app.post("/ranking/{job}")
    def human_review(request: Request, job: int, csrf: str = Form(""),
                     fulfillment: int = Form(..., ge=1, le=10),
                     correctness: int = Form(..., ge=1, le=10), rework: int = Form(..., ge=1, le=10),
                     comment: str = Form("", max_length=500)):
        check_post(request, csrf)
        try:
            ranking.review(job, "human", fulfillment, correctness, rework, comment)
        except KeyError:
            raise HTTPException(404, "Unknown delegation")
        return RedirectResponse(f"/ranking/{job}", status_code=303)

    # ---------------------------------------------------------------- tasks
    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request):
        tasks = store.all()
        return page(request, "tasks.html", "tasks", tasks=tasks, queue=runner.queue_info(),
                    any_running=any(x.status in ("queued", "running") for x in tasks))

    @app.get("/tasks/new", response_class=HTMLResponse)
    async def task_new(request: Request, repo: str = "", error: str = "",
                       title: str = "", details: str = "",
                       agent: str = "", model: str = "", effort: str = ""):
        projects, _ = await hub.load()
        repos = sorted({p.repo for p in projects})
        local = {r: bool(runner.repos.get(r)) for r in repos}
        available = runner.agents_available()
        agents = [a for a in ("claude-code", "codex") if not available[a]]
        models, *choices = await asyncio.gather(
            asyncio.to_thread(opencode_models) if not available["opencode"] else asyncio.sleep(0, result=[]),
            *(asyncio.to_thread(catalog, a) for a in agents))
        catalogs = dict(zip(agents, choices))
        initial = task_defaults.initial_choice(
            {"agent": agent, "model": model, "effort": effort}, store.all(),
            available, catalogs, models)
        return page(request, "task_new.html", "tasks", repos=repos, selected=repo,
                    local=local, available=available, oc_models=models, catalogs=catalogs,
                    cloud=settings.cloud_tasks, error=error[:300], auto_merge=settings.auto_merge,
                    has_token=bool(settings.github_token), priorities=PRIORITIES,
                    max_parallel=settings.max_parallel, draft_title=title, draft_details=details,
                    initial=initial)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str):
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "Unknown task")
        report_error = False
        if task.where == "cloud" and task.issue_number and not task.summary:
            try:
                comments = await hub.agent_comments(task.repo, task.issue_number)
                for comment in comments:
                    task.summary = extract_summary(comment)
                    if task.summary:
                        store.save(task)
                        break
            except (GitHubError, httpx.RequestError):
                report_error = True
        log = store.read_log(task.id)
        return page(request, "task.html", "tasks", task=task, report=task_report(task, log),
                    log="\n".join(log.splitlines()[-200:]), can_cancel=runner.can_cancel(task.id),
                    report_error=report_error, queue=runner.queue_info().get(task.id),
                    priorities=PRIORITIES)

    @app.post("/tasks/{task_id}/priority")
    async def task_priority(request: Request, task_id: str, csrf: str = Form(""),
                            priority: str = Form("")):
        check_post(request, csrf)
        if not store.get(task_id):
            raise HTTPException(404, "Unknown task")
        if priority not in PRIORITIES:
            raise HTTPException(400, "Unknown priority")
        if not runner.set_priority(task_id, priority):
            raise HTTPException(409, "Only a queued task can change its priority.")
        return RedirectResponse(f"/tasks/{quote(task_id)}", status_code=303)

    @app.post("/tasks/{task_id}/cancel")
    async def task_cancel(request: Request, task_id: str, csrf: str = Form("")):
        check_post(request, csrf)
        if not store.get(task_id):
            raise HTTPException(404, "Unknown task")
        # Killing the agent and removing the worktree block for a few seconds:
        # do it in a thread so the other pages keep answering.
        if not await asyncio.to_thread(runner.cancel, task_id):
            raise HTTPException(409, "This task is not queued or running on this computer, or is already finishing.")
        return RedirectResponse(f"/tasks/{quote(task_id)}", status_code=303)

    @app.post("/tasks")
    async def task_create(request: Request, repo: str = Form(...), where: str = Form(...),
                          title: str = Form(...), details: str = Form(""),
                          oc_model: str = Form(""), csrf: str = Form(""),
                          model: str = Form(""), effort: str = Form(""),
                          priority: str = Form("normal")):
        check_post(request, csrf)
        if priority not in PRIORITIES:
            raise HTTPException(400, "Unknown priority")
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
                    f"{next_task_request(await asyncio.to_thread(runner.model_choices))} "
                    f"Post your report in a comment on this issue. {SUMMARY_REQUEST}")
            try:
                issue = await hub.create_issue(repo, title, body)
            except GitHubError as exc:
                return back(str(exc))
            task = Task(id=new_id(title), repo=repo, title=title, details=details,
                        agent="claude-code", where="cloud", created_at=time.time(),
                        status="sent", issue_url=issue["url"], issue_number=issue["number"],
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
                    base=by_repo[repo].branch, model=model, effort=effort,  # always ends in a PR
                    priority=priority)
        # Queued; it starts now if its repository and a slot are free.
        try:
            runner.submit(task)
        except UpdateInProgress:
            raise HTTPException(503, t["update_testing"])
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
