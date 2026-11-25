"""Project overview and detail routes (/, /p/...)."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import ranking_view
from ..github import GitHubError
from ..project_detail import finished_items
from ..task_store import queue_key
from .deps import CTX, AppContext

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def projects_page(request: Request, refresh: int = 0, ctx: AppContext = CTX):
    projects, errors = await ctx.hub.load(force=bool(refresh))
    groups = {k: [p for p in projects if p.status.status == k]
              for k in ("active", "planned", "paused", "done")}
    running = {x.repo for x in ctx.store.all() if x.status in ("queued", "running")}
    return ctx.page(request, "projects.html", "projects", groups=groups, count=len(projects),
                    errors=errors, no_token=not ctx.settings.github_token, running=running)


@router.get("/p/{pid:path}", response_class=HTMLResponse)
async def project(request: Request, pid: str, tab: Literal["next", "done", "notes"] = "next",
                  page_number: int = Query(1, alias="page"), ctx: AppContext = CTX):
    projects, _ = await ctx.hub.load()
    for p in projects:
        if p.id == pid:
            content = p.status.sections
            tasks = [task for task in ctx.store.all() if task.repo.casefold() == p.repo.casefold()]
            active = sorted((task for task in tasks if task.status in ("running", "queued")),
                            key=lambda task: (task.status == "queued", queue_key(task)))
            finished = finished_items(content, tasks)
            return ctx.page(
                request, "project.html", "projects", p=p, content=content, tab=tab,
                upcoming=content.upcoming, checked=sum(item.checked for item in content.checklist),
                finished=finished, done_page=ranking_view.paginate(finished, page_number, 10),
                notes=len(content.blockers) + len(content.decisions) + len(content.log),
                active=active, queue=ctx.runner.queue_info(), pulls=await ctx.hub.open_pulls(p.repo),
            )
    raise HTTPException(404, "Unknown project")


@router.post("/p/{pid:path}/done")
async def owner_done(request: Request, pid: str, line: int = Form(...), sha: str = Form(...),
                     csrf: str = Form(""), ctx: AppContext = CTX):
    ctx.check_post(request, csrf)
    projects, _ = await ctx.hub.load()
    proj = next((p for p in projects if p.id == pid), None)
    if proj is None:
        raise HTTPException(404, "Unknown project")
    try:
        await ctx.hub.complete_owner_action(proj, line, sha)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (GitHubError, httpx.RequestError) as exc:
        raise HTTPException(502, str(exc)) from exc
    return RedirectResponse(f"/p/{quote(pid)}", status_code=303)


@router.post("/p/{pid:path}/check")
async def item_done(request: Request, pid: str, line: int = Form(...), sha: str = Form(...),
                    csrf: str = Form(""), back: str = Form(""), ctx: AppContext = CTX):
    """Mark an agent task in STATUS.md as done (it was already done, or a
    task ended without changes because there was nothing left to do)."""
    ctx.check_post(request, csrf)
    projects, _ = await ctx.hub.load()
    proj = next((p for p in projects if p.id == pid), None)
    if proj is None:
        raise HTTPException(404, "Unknown project")
    try:
        await ctx.hub.complete_item(proj, line, sha)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (GitHubError, httpx.RequestError) as exc:
        raise HTTPException(502, str(exc)) from exc
    target = back if back.startswith("/") and not back.startswith("//") else f"/p/{quote(pid)}"
    return RedirectResponse(target, status_code=303)
