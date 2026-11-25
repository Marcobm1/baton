"""The web pages: Projects, Quotas and Tasks."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from . import __version__, quotas, usage
from .auth import (COOKIE_NAME, SESSION_MAX_AGE, LoginRateLimiter, check_access_key,
                   make_session_token, safe_next_path, verify_session_token)
from .config import Settings
from .github import GitHubError, Hub
from .i18n import ordinal, strings
from .ping import Scheduler, fetch_gemini_quota, find_agy
from .ranking import CRITERIA, historical_score
from .project_detail import finished_items
from .task_models import catalog, opencode_groups
from . import task_defaults, ranking_view
from .task_report import task_report
from .models import LocalModels, ModelError, chat_payload
from .agent_chat import EFFORTS, MAX_PROMPT_CHARS, MODES, AgentChats, ChatError, is_abliterated
from .self_update import SelfUpdate
from .runner import UpdateInProgress
from .runner import (AGENTS, PRIORITIES, NEXT_TASK_REQUEST as NEXT_TASK_REQUEST, SUMMARY_REQUEST,
                     next_task_request, LocalRepos, Runner, Task, TaskStore,
                     extract_summary, find_opencode, new_id, opencode_models, queue_key)

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
    scheduler = Scheduler(settings.data_dir, settings.ping_claude_minutes,
                          settings.ping_codex_minutes, settings.ping_hours, settings.ping_model,
                          codex_home=settings.codex_home)
    store = TaskStore(settings.data_dir)
    models = LocalModels(settings.data_dir)
    agent_chats = AgentChats(data_dir=settings.data_dir)
    ranking = store.ranking

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
                    max_parallel=settings.max_parallel,
                    review_models={"claude-code": (settings.review_claude_model, settings.review_claude_effort)})

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
        # OpenCode Go's usage endpoint and agy's /usage are free to ask: keep
        # those cards fresh even when nobody has the page open (every 5 minutes).
        refresh_go()
        refresh_gemini()

    async def _loop():
        # Check once a minute whether a quota check is due; checks run in a
        # thread so the pages keep answering while a CLI is working.
        while True:
            try:
                await asyncio.to_thread(_quota_tick)
            except Exception:  # never let the loop die
                pass
            await asyncio.sleep(60)

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
        # The loop keeps every quota source at most ~N minutes old (pings when
        # enabled, OpenCode Go when a key is set), not only while pages are open.
        loop_task = (asyncio.create_task(_loop())
                     if scheduler.enabled or settings.opencode_go_key or find_agy() else None)
        update_task = None
        if updater.enabled:
            loop = asyncio.get_running_loop()

            def finished(task):
                if updater.reason == "busy" or (task.merged and task.repo.lower() == updater.repo):
                    loop.call_soon_threadsafe(update_requested.set)

            runner.on_finished = finished
            update_task = asyncio.create_task(update_loop())
        try:
            yield
        finally:
            stopping.set()
            await monitor
        await asyncio.to_thread(agent_chats.close_all)   # no OpenCode left behind
        runner.on_finished = None
        if update_task:
            update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await update_task
        if loop_task:
            loop_task.cancel()

    app = FastAPI(title="Baton", version=__version__, docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    app.state.agent_chats = agent_chats
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    # Anti-CSRF: a random value that only pages served by Baton know. A form
    # posted from any other website can't include it, so it is rejected.
    csrf_token = secrets.token_urlsafe(32)
    # Separate token for /login so an unauthenticated page never exposes csrf_token.
    login_csrf = secrets.token_urlsafe(32)
    access_key = settings.access_key
    auth_enabled = bool(access_key)
    login_limiter = LoginRateLimiter()

    def _check_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin:
            hosts = {request.headers.get("host"), request.headers.get("x-forwarded-host")}
            if urlparse(origin).netloc not in hosts:
                raise HTTPException(403, "Cross-site request refused.")

    def _is_https(request: Request) -> bool:
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return request.url.scheme == "https" or proto == "https"

    def check_post(request: Request, token: str) -> None:
        if not secrets.compare_digest(token or "", csrf_token):
            raise HTTPException(403, "Invalid form token; reload the page.")
        _check_origin(request)

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        if not auth_enabled:
            return await call_next(request)
        path = request.url.path
        if (path == "/healthz" or path.startswith("/static/")) and request.method in ("GET", "HEAD"):
            return await call_next(request)
        if path == "/login" and request.method in ("GET", "HEAD", "POST"):
            return await call_next(request)
        if verify_session_token(request.cookies.get(COOKIE_NAME), access_key):
            return await call_next(request)
        if request.method in ("GET", "HEAD") and not path.startswith("/api/"):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(safe_next_path(target), safe='/?=&')}", status_code=303)
        return JSONResponse({"detail": "Authentication required."}, status_code=401)

    t = strings(settings.lang)
    templates.env.globals.update(
        t=t, title=settings.title, version=__version__, lang=settings.lang, csrf=csrf_token,
        agents=AGENTS, auth_enabled=auth_enabled,
        # Changes whenever any stylesheet or script changes, so browsers (and
        # phones, which cache hard) never keep an old copy after an update.
        css_v=hashlib.sha256(b"".join(
            f.read_bytes() for f in sorted((HERE / "static").glob("*")) if f.suffix in (".css", ".js")
        )).hexdigest()[:10])
    templates.env.filters["md"] = _render_md
    templates.env.filters["repo_md"] = _render_repo_md
    templates.env.filters["dur"] = _duration
    templates.env.filters["ordinal"] = lambda n: ordinal(n, settings.lang)
    templates.env.filters["tok"] = usage.human
    # {**a, **b} for query strings: a None value drops that parameter.
    templates.env.filters["combine"] = lambda base, changes: {**base, **changes}
    templates.env.filters["when"] = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"
    usage_cache: dict = {}

    def token_usage() -> dict:
        # Reading the logs takes a moment: reuse the result for 5 minutes.
        if not usage_cache or time.time() - usage_cache["at"] > 300:
            usage_cache.update(at=time.time(), data=usage.collect(
                settings.claude_home, settings.codex_home, settings.opencode_db,
                data_dir=settings.data_dir))
        return usage_cache["data"]

    go_state: dict = {"at": 0.0, "msg": ""}

    def refresh_go() -> str:
        # OpenCode Go's usage endpoint costs nothing: ask at most every 5 minutes.
        if settings.opencode_go_key and time.time() - go_state["at"] > 300:
            ok, msg = quotas.fetch_opencode_go(settings.opencode_go_key, settings.data_dir)
            go_state.update(at=time.time(), msg="" if ok else msg)
        return go_state["msg"]

    gemini_state: dict = {"at": 0.0, "msg": ""}

    def refresh_gemini() -> str:
        # `agy -p /usage` asks no model (0 tokens): at most every 5 minutes.
        if time.time() - gemini_state["at"] > 300 and find_agy():
            ok, msg = fetch_gemini_quota(settings.data_dir)
            gemini_state.update(at=time.time(), msg="" if ok else msg)
        return gemini_state["msg"]

    def page(request: Request, name: str, nav: str, **ctx):
        return templates.TemplateResponse(request, name, {"nav": nav, **ctx})

    # ----------------------------------------------------------------- auth
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: str = "/"):
        if not auth_enabled:
            return RedirectResponse("/", status_code=303)
        dest = safe_next_path(next)
        if verify_session_token(request.cookies.get(COOKIE_NAME), access_key):
            return RedirectResponse(dest, status_code=303)
        return templates.TemplateResponse(
            request, "login.html",
            {"next_path": dest, "error": "", "login_csrf": login_csrf},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/login")
    async def login_submit(request: Request, key: str = Form(""), csrf: str = Form(""),
                           next: str = Form("/")):
        if not auth_enabled:
            return RedirectResponse("/", status_code=303)
        if not secrets.compare_digest(csrf or "", login_csrf):
            raise HTTPException(403, "Invalid form token; reload the page.")
        _check_origin(request)
        dest = safe_next_path(next)
        client = request.client.host if request.client else "unknown"
        if login_limiter.is_limited(client):
            return templates.TemplateResponse(
                request, "login.html",
                {"next_path": dest, "error": t["auth_too_many"], "login_csrf": login_csrf},
                status_code=429, headers={"Cache-Control": "no-store"},
            )
        if not check_access_key(key, access_key):
            login_limiter.record_failure(client)
            return templates.TemplateResponse(
                request, "login.html",
                {"next_path": dest, "error": t["auth_invalid"], "login_csrf": login_csrf},
                status_code=401, headers={"Cache-Control": "no-store"},
            )
        login_limiter.reset(client)
        response = RedirectResponse(dest, status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            make_session_token(access_key),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=_is_https(request),
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout_submit(request: Request, csrf: str = Form("")):
        check_post(request, csrf)
        response = RedirectResponse("/login" if auth_enabled else "/", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

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
    async def project(request: Request, pid: str, tab: Literal["next", "done", "notes"] = "next",
                      page_number: int = Query(1, alias="page")):
        projects, _ = await hub.load()
        for p in projects:
            if p.id == pid:
                content = p.status.sections
                tasks = [task for task in store.all() if task.repo.casefold() == p.repo.casefold()]
                active = sorted((task for task in tasks if task.status in ("running", "queued")),
                                key=lambda task: (task.status == "queued", queue_key(task)))
                finished = finished_items(content, tasks)
                return page(request, "project.html", "projects", p=p, content=content, tab=tab,
                            upcoming=content.upcoming, checked=sum(item.checked for item in content.checklist),
                            finished=finished, done_page=ranking_view.paginate(finished, page_number, 10),
                            notes=len(content.blockers) + len(content.decisions) + len(content.log),
                            active=active, queue=runner.queue_info(), pulls=await hub.open_pulls(p.repo))
        raise HTTPException(404, "Unknown project")

    @app.post("/p/{pid:path}/done")
    async def owner_done(request: Request, pid: str, line: int = Form(...), sha: str = Form(...),
                         csrf: str = Form("")):
        check_post(request, csrf)
        projects, _ = await hub.load()
        project = next((p for p in projects if p.id == pid), None)
        if project is None:
            raise HTTPException(404, "Unknown project")
        try:
            await hub.complete_owner_action(project, line, sha)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (GitHubError, httpx.RequestError) as exc:
            raise HTTPException(502, str(exc)) from exc
        return RedirectResponse(f"/p/{quote(pid)}", status_code=303)

    @app.post("/p/{pid:path}/check")
    async def item_done(request: Request, pid: str, line: int = Form(...), sha: str = Form(...),
                        csrf: str = Form(""), back: str = Form("")):
        """Mark an agent task in STATUS.md as done (it was already done, or a
        task ended without changes because there was nothing left to do)."""
        check_post(request, csrf)
        projects, _ = await hub.load()
        project = next((p for p in projects if p.id == pid), None)
        if project is None:
            raise HTTPException(404, "Unknown project")
        try:
            await hub.complete_item(project, line, sha)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (GitHubError, httpx.RequestError) as exc:
            raise HTTPException(502, str(exc)) from exc
        target = back if back.startswith("/") and not back.startswith("//") else f"/p/{quote(pid)}"
        return RedirectResponse(target, status_code=303)

    # --------------------------------------------------------------- quotas
    @app.get("/quotas", response_class=HTMLResponse)
    async def quotas_page(request: Request):
        go_error, gemini_error = await asyncio.gather(asyncio.to_thread(refresh_go),
                                                      asyncio.to_thread(refresh_gemini))
        return page(request, "quotas.html", "quotas", go_error=go_error, gemini_error=gemini_error,
                    updater=updater,
                    providers=quotas.by_provider(quotas.all_windows(settings.data_dir, settings.codex_home)),
                    sched=scheduler, tokens=await asyncio.to_thread(token_usage),
                    sched_results={k: {"age": time.time() - v[0], "ok": v[1], "msg": v[2]}
                                   for k, v in scheduler.results.items()})

    @app.post("/self-update")
    async def update_now(request: Request, csrf: str = Form("")):
        check_post(request, csrf)
        if updater.enabled:
            updater.request_manual()
            update_requested.set()
        return RedirectResponse("/quotas#self-update", status_code=303)

    @app.get("/ranking", response_class=HTMLResponse)
    def ranking_page(request: Request, origin: Literal["all", "delegation", "task"] = "all",
                     view: Literal["agent", "human", "combined"] = "agent", saved: int = 0,
                     tab: Literal["all", "pending"] = "all", model: str | None = None,
                     real: int = 0, page_number: int = Query(1, alias="page")):
        models, jobs = ranking.overview(origin)
        if real:
            models, jobs = ranking_view.without_estimates(models, jobs)
        rows = ranking_view.leaderboard(models, jobs, view)
        if model is not None and model not in {r["model"] for r in rows}:
            model = None
        listed = ranking_view.job_list(jobs, tab, model)
        return page(request, "ranking.html", "ranking", models=models, rows=rows, origin=origin,
                    view=view, saved=bool(saved), tab=tab, model=model, real=bool(real),
                    selected_label=ranking_view.model_label(model or ""),
                    stats=ranking_view.summary(rows, jobs, view),
                    pending_count=len(ranking_view.job_list(jobs, "pending", model)),
                    jobs=ranking_view.paginate(listed, page_number, ranking_view.PER_PAGE_JOBS))

    @app.get("/ranking/{job}", response_class=HTMLResponse)
    def delegation_page(request: Request, job: int,
                        origin: Literal["all", "delegation", "task"] = "all",
                        view: Literal["agent", "human", "combined"] = "agent"):
        details, reviews = ranking.detail(job)
        if details is None:
            raise HTTPException(404, "Unknown delegation")
        return page(request, "delegation.html", "ranking", job=details, reviews=reviews,
                    criteria=CRITERIA, estimate=historical_score(details), origin=origin, view=view)

    @app.post("/ranking/{job}")
    def human_review(request: Request, job: int, csrf: str = Form(""),
                     fulfillment: int = Form(..., ge=1, le=10),
                     correctness: int = Form(..., ge=1, le=10), rework: int = Form(..., ge=1, le=10),
                     comment: str = Form("", max_length=500),
                     origin: Literal["all", "delegation", "task"] = Form("all"),
                     view: Literal["agent", "human", "combined"] = Form("agent"),
                     back: str = Form("")):
        check_post(request, csrf)
        try:
            ranking.review(job, "human", fulfillment, correctness, rework, comment)
        except KeyError as exc:
            raise HTTPException(404, "Unknown delegation") from exc
        # Back to the list (same filter, view and page), with a one-time confirmation.
        if back.startswith("/ranking?") or back == "/ranking":
            sep = "&" if "?" in back else "?"
            return RedirectResponse(f"{back}{sep}saved=1#jobs", status_code=303)
        return RedirectResponse(f"/ranking?origin={origin}&view={view}&saved=1", status_code=303)

    # ---------------------------------------------------------------- tasks
    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request,
                         status: Literal["all", "done", "no-changes", "failed", "cancelled", "sent"] = "all",
                         repo: str = "", page_number: int = Query(1, alias="page")):
        tasks = store.all()
        active = [x for x in tasks if x.status in ("queued", "running")]
        history = [x for x in tasks if x.status not in ("queued", "running")]
        repos = {}
        for x in history:
            repos[x.repo] = repos.get(x.repo, 0) + 1
        if repo not in repos:
            repo = ""
        if repo:
            history = [x for x in history if x.repo == repo]
        counts = {key: sum(x.status == key for x in history)
                  for key in ("done", "no-changes", "failed", "cancelled", "sent")}
        if status != "all":
            history = [x for x in history if x.status == status]
        return page(request, "tasks.html", "tasks", tasks=tasks, active=active, status=status,
                    repo=repo, repos=dict(sorted(repos.items(), key=lambda kv: -kv[1])),
                    counts={k: v for k, v in counts.items() if v}, total=sum(counts.values()),
                    history=ranking_view.paginate(history, page_number, ranking_view.PER_PAGE_TASKS),
                    queue=runner.queue_info(), any_running=bool(active))

    def followup_context(task: Task) -> str:
        summary = task.summary or task_report(task, store.read_log(task.id))["summary"]
        parts = [f"{t['original_task']}\n{task.title}\n{task.details}".rstrip(),
                 f"{t['agent_summary']}\n{summary or t['no_summary']}"]
        if task.pr_url:
            parts.append(f"PR: {task.pr_url}")
        parts.append(f"{t['your_reply']}\n")
        return "\n\n".join(parts)

    @app.get("/tasks/new", response_class=HTMLResponse)
    async def task_new(request: Request, repo: str = "", error: str = "",
                       title: str = "", details: str = "",
                       agent: str = "", model: str = "", effort: str = "", followup: str = ""):
        original = None
        details_limit = 5000
        if followup:
            original = store.get(followup)
            if not original or not original.finished:
                raise HTTPException(404, "Unknown finished task")
            repo, agent, model, effort = original.repo, original.agent, original.model, original.effort
            title = f"{t['followup']}: {original.title}"[:200]
            details = followup_context(original)
            details_limit += len(details)
        projects, _ = await hub.load()
        repos = sorted({p.repo for p in projects})
        local = {r: bool(runner.repos.get(r)) for r in repos}
        available = runner.agents_available()
        agents = [a for a in ("claude-code", "codex", "gemini") if not available.get(a, "missing")]
        models, *choices = await asyncio.gather(
            asyncio.to_thread(opencode_models) if not available["opencode"] else asyncio.sleep(0, result=[]),
            *(asyncio.to_thread(catalog, a) for a in agents))
        catalogs = dict(zip(agents, choices, strict=True))
        initial = task_defaults.initial_choice(
            {"agent": agent, "model": model, "effort": effort}, store.all(),
            available, catalogs, models)
        # A draft can name another repository (Repo: line); match its letter case
        # to the listed one, or warn instead of silently picking another.
        listed = {r.casefold(): r for r in repos}
        repo_missing = ""
        if repo and repo not in repos:
            repo = listed.get(repo.casefold(), repo)
            repo_missing = "" if repo in repos else repo
        if original and original.repo not in repos:
            raise HTTPException(409, "The original repository is unavailable. Refresh the projects list.")
        if original and original.where == "cloud":
            initial = {"agent": "cloud", "model": "", "effort": "", "source": "", "missing": ""}
        elif original and original.agent == initial["agent"] == "local":
            initial.update(model=original.model, source="", missing="")
        elif original and initial["source"] == "recommended":
            initial["source"] = ""
        oc_groups = opencode_groups(models, t)
        oc_default = (initial["model"] if initial["agent"] == "opencode" and initial["model"]
                      else (oc_groups[0]["models"][0] if oc_groups and oc_groups[0]["models"] else ""))
        return page(request, "task_new.html", "tasks", repos=repos, selected=repo,
                    local=local, available=available, oc_models=models, oc_groups=oc_groups,
                    oc_selected=oc_default, oc_payg_initial=oc_default.startswith("opencode/"),
                    catalogs=catalogs,
                    cloud=settings.cloud_tasks, error=error[:300], auto_merge=settings.auto_merge,
                    has_token=bool(settings.github_token), priorities=PRIORITIES,
                    max_parallel=settings.max_parallel, draft_title=title, draft_details=details,
                    initial=initial, followup=followup, details_limit=details_limit,
                    repo_missing=repo_missing)

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
        # A finished task whose item is still unchecked in STATUS.md (typical
        # after "no changes": it was already done) can be ticked from here.
        pending = None
        if task.finished and task.status != "cancelled":
            projects, _ = await hub.load()
            here = task.repo.casefold()
            for project in projects:
                # Its own repository, or another project's item that runs here (Repo: line).
                item = next((i for i in project.status.sections.upcoming
                             if i.title.casefold() == task.title.strip().casefold()
                             and here in (project.repo.casefold(), i.repo.casefold())), None)
                if item:
                    pending = {"project": project, "item": item}
                    break
        return page(request, "task.html", "tasks", task=task, report=task_report(task, log), pending=pending,
                    log="\n".join(log.splitlines()[-200:]), can_cancel=runner.can_cancel(task.id),
                    report_error=report_error, ranking_job=store.record_task(task), queue=runner.queue_info().get(task.id),
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
                          priority: str = Form("normal"), followup: str = Form("")):
        check_post(request, csrf)
        if priority not in PRIORITIES:
            raise HTTPException(400, "Unknown priority")
        projects, _ = await hub.load()
        by_repo = {p.repo: p for p in projects}
        if repo not in by_repo:
            raise HTTPException(400, "Unknown repository")
        original = None
        details_limit = 5000
        if followup:
            details = details.replace("\r\n", "\n")
            original = store.get(followup)
            if not original or not original.finished:
                raise HTTPException(400, "Unknown finished task")
            details_limit += len(followup_context(original))
        title, details = title.strip()[:200], details.strip()[:details_limit]
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
        if agent in ("codex", "claude-code", "gemini") and (model or effort):
            options = await asyncio.to_thread(catalog, agent)
            if model not in options or (effort and effort not in options[model]["efforts"]):
                return back(strings(settings.lang)["invalid_model"])
        elif agent == "opencode":
            # Only a model OpenCode itself lists is accepted.
            if oc_model not in await asyncio.to_thread(opencode_models):
                return back("Choose one of the models OpenCode lists.")
            model, effort = oc_model, ""
        else:
            model = original.model if original and original.agent == agent == "local" else ""
            effort = ""
        task_id = new_id(title)
        task = Task(id=task_id, repo=repo, title=title, details=details, agent=agent,
                    where="local", created_at=time.time(), branch=f"baton/{task_id}",
                    base=by_repo[repo].branch, model=model, effort=effort,  # always ends in a PR
                    priority=priority)
        # Queued; it starts now if its repository and a slot are free.
        try:
            runner.submit(task)
        except UpdateInProgress as exc:
            raise HTTPException(503, t["update_testing"]) from exc
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    # --------------------------------------------------------------- models
    @app.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request, error: str = ""):
        view = await asyncio.to_thread(models.overview, settings.lang)
        return page(request, "models.html", "models", view=view, error=error[:300])

    @app.post("/models/{model_id}/{action}")
    async def model_action(request: Request, model_id: str, action: Literal["start", "stop"],
                           csrf: str = Form(""), back: str = Form("")):
        check_post(request, csrf)
        # Started from the chat page: go back there with that model selected.
        target = f"/chat?model={quote(model_id)}" if back == "/chat" else "/models"
        sep = "&" if "?" in target else "?"
        try:
            # Stopping waits for the process to exit: keep the other pages answering.
            await asyncio.to_thread(models.start if action == "start" else models.stop, model_id)
        except ModelError as exc:
            return RedirectResponse(f"{target}{sep}error={quote(exc.text(t))}", status_code=303)
        return RedirectResponse(target, status_code=303)

    # ----------------------------------------------------------------- chat
    @app.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request, model: str = "", error: str = "",
                        agent: Literal["local", "opencode", "gemini"] = "local"):
        view = await asyncio.to_thread(models.overview, settings.lang)
        ids = [m["id"] for m in view["models"]]
        running = [m["id"] for m in view["models"] if m["status"] in ("running", "external")]
        selected = model if model in ids else (running[0] if running else (ids[0] if ids else ""))
        oc = {"available": False, "groups": [], "folders": []}
        if agent == "opencode" and find_opencode():
            oc_list = await asyncio.to_thread(opencode_models)
            active_families = {m["family_key"] for m in view["models"] if m["uses_gpu"]}
            oc = {"available": True, "groups": opencode_groups(oc_list, t, active_families=active_families),
                  "folders": await asyncio.to_thread(chat_folders)}
        elif agent == "gemini" and find_agy():
            gm = await asyncio.to_thread(catalog, "gemini")
            # One group; each model carries its own thinking levels (data-efforts).
            oc = {"available": True, "folders": await asyncio.to_thread(chat_folders),
                  "groups": [{"provider": "gemini", "label": "Gemini", "included": False, "payg": False,
                              "models": list(gm)}] if gm else [],
                  "efforts": {m: v["efforts"] for m, v in gm.items()}}
        return page(request, "chat.html", "chat", view=view, selected=selected, error=error[:300],
                    agent=agent, oc=oc, efforts=EFFORTS)

    # ----------------------------------------------------------- agents chat
    def chat_folders() -> list[dict]:
        """Where an agent conversation may work: Baton's local clones and the
        scratch folder. The browser only ever sends one of these keys."""
        out = [{"key": repo, "label": repo, "path": path}
               for repo, path in sorted(runner.repos.all().items()) if path.is_dir()]
        if settings.chat_scratch:
            out.append({"key": "scratch", "label": f"{settings.chat_scratch} ({t['ag_scratch']})",
                        "path": Path(settings.chat_scratch)})
        return out

    def agent_error(exc: ChatError, status: int = 409) -> HTTPException:
        text = t.get(exc.key, exc.key)
        return HTTPException(status, f"{text} {exc.detail}".strip())

    def get_chat(chat_id: str):
        chat = agent_chats.get(chat_id)
        if chat is None:
            raise HTTPException(404, t["ag_closed"])
        return chat

    async def json_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "A JSON object is needed.")
        return body

    @app.post("/api/agents")
    async def agents_create(request: Request):
        """Start a conversation. Agent, folder, model, effort and permission
        mode are checked and fixed here, on the server, for its whole life."""
        check_post(request, request.headers.get("x-baton-csrf", ""))
        body = await json_body(request)
        agent, model = body.get("agent", "opencode"), str(body.get("model") or "")
        effort, mode = str(body.get("effort") or "default"), str(body.get("mode") or "confirm")
        if agent not in ("opencode", "gemini"):
            raise HTTPException(400, "Unknown agent")
        if mode not in MODES or effort not in EFFORTS:
            raise HTTPException(400, "Unknown permission mode or reasoning effort")
        if agent == "gemini":
            if model not in await asyncio.to_thread(catalog, "gemini"):
                raise HTTPException(400, t["ag_err_model_gemini"])
        elif model not in await asyncio.to_thread(opencode_models):
            raise HTTPException(400, t["ag_err_model"])
        if mode == "full":
            # Refused for everyone in this phase; abliterated models never get it.
            view = await asyncio.to_thread(models.overview, settings.lang)
            if is_abliterated(model, view["models"]):
                raise HTTPException(403, t["ag_full_abliterated"])
            raise HTTPException(403, t["ag_full_off"])
        folders = {f["key"]: f for f in await asyncio.to_thread(chat_folders)}
        folder = folders.get(str(body.get("folder") or ""))
        if folder is None:
            raise HTTPException(400, t["ag_err_folder"])
        path = folder["path"]
        if folder["key"] == "scratch":
            path.mkdir(parents=True, exist_ok=True)
        # The canonical folder (junctions and links resolved) is what OpenCode gets.
        path = Path(os.path.realpath(path))
        try:
            chat = await asyncio.to_thread(agent_chats.create, path, folder["label"], model, effort, mode,
                                           agent)
        except ChatError as exc:
            raise agent_error(exc, 502 if exc.key == "ag_err_start" else 409) from exc
        return JSONResponse(chat.info())

    @app.get("/api/agents/{chat_id}")
    async def agents_info(chat_id: str):
        return JSONResponse(get_chat(chat_id).info())

    @app.post("/api/agents/{chat_id}/prompt")
    async def agents_prompt(request: Request, chat_id: str):
        check_post(request, request.headers.get("x-baton-csrf", ""))
        chat = get_chat(chat_id)
        text = (await json_body(request)).get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_PROMPT_CHARS:
            raise HTTPException(400, f"Write a message of up to {MAX_PROMPT_CHARS} characters.")
        try:
            chat.prompt(text.strip())
        except ChatError as exc:
            raise agent_error(exc) from exc
        return JSONResponse(chat.info(), status_code=202)

    @app.get("/api/agents/{chat_id}/events")
    async def agents_events(request: Request, chat_id: str, after: int = Query(0, ge=0)):
        """Server-sent events of one conversation from event `after` on. The
        stream ends once no turn is running; reconnecting (Last-Event-ID)
        continues where it stopped, so a phone that sleeps loses nothing."""
        chat = get_chat(chat_id)
        last = request.headers.get("last-event-id", "")
        if last.isdigit():
            after = int(last) + 1

        async def stream():
            position = after
            while True:
                events, more = await asyncio.to_thread(chat.wait, position, 15)
                for event in events:
                    yield f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                position += len(events)
                if not more and position >= len(chat.events):
                    yield "event: idle\ndata: {}\n\n"
                    return
                if not events:
                    yield ": still working\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/agents/{chat_id}/permission")
    async def agents_permission(request: Request, chat_id: str):
        """Allow or deny one pending request. The id is looked up only among
        this conversation's pending requests and works once."""
        check_post(request, request.headers.get("x-baton-csrf", ""))
        chat = get_chat(chat_id)
        body = await json_body(request)
        allow = body.get("allow")
        if not isinstance(allow, bool):
            raise HTTPException(400, "allow must be true or false")
        if not await asyncio.to_thread(chat.answer, str(body.get("id") or ""), allow):
            raise HTTPException(404, t["ag_expired"])
        return JSONResponse({"ok": True})

    @app.post("/api/agents/{chat_id}/cancel")
    async def agents_cancel(request: Request, chat_id: str):
        check_post(request, request.headers.get("x-baton-csrf", ""))
        await asyncio.to_thread(get_chat(chat_id).cancel)
        return JSONResponse({"ok": True})

    @app.post("/api/agents/{chat_id}/close")
    async def agents_close(request: Request, chat_id: str):
        check_post(request, request.headers.get("x-baton-csrf", ""))
        get_chat(chat_id)
        await asyncio.to_thread(agent_chats.close, chat_id)
        return JSONResponse({"ok": True})

    @app.post("/api/chat/{model_id}")
    async def chat_api(request: Request, model_id: str):
        """Relay a chat to a local model and stream its answer back (server-sent
        events), so the chat works from any device that can open Baton."""
        check_post(request, request.headers.get("x-baton-csrf", ""))
        try:
            payload = chat_payload(await request.json())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            port = await asyncio.to_thread(models.chat_port, model_id)
        except ModelError as exc:
            raise HTTPException(404 if exc.key == "m_err_unknown" else 409, exc.text(t)) from exc
        # No read timeout: a long answer can take minutes. Closing the browser's
        # request (Stop) closes this stream, and llama-server stops generating.
        client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
        try:
            upstream = await client.send(client.build_request(
                "POST", f"http://127.0.0.1:{port}/v1/chat/completions", json=payload), stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(502, f"The model did not answer: {exc}") from exc

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(relay(), status_code=upstream.status_code, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ api
    @app.get("/api/projects")
    async def api_projects(refresh: int = 0):
        projects, errors = await hub.load(force=bool(refresh))
        return JSONResponse({"projects": [p.to_dict() for p in projects], "errors": errors})

    @app.get("/api/quotas")
    async def api_quotas():
        await asyncio.to_thread(refresh_go)
        return JSONResponse({"windows": quotas.all_windows(settings.data_dir, settings.codex_home)})

    @app.get("/api/models")
    async def api_models():
        return JSONResponse(await asyncio.to_thread(models.overview, settings.lang))

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    return app
