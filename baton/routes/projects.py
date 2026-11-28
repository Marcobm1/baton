"""Project overview and detail routes (/, /p/...)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from .. import ranking_view, task_map
from ..github import GitHubError
from ..project_detail import finished_items, summary_headline
from ..task_store import queue_key
from .deps import CTX, AppContext

router = APIRouter()


def _active_tasks_for(repo: str, ctx: AppContext):
    tasks = [task for task in ctx.store.all() if task.repo.casefold() == repo.casefold()]
    active = sorted(
        (task for task in tasks if task.status in ("running", "queued")),
        key=lambda task: (task.status == "queued", queue_key(task)),
    )
    return tasks, active


def _map_payload(p, active, ctx: AppContext) -> dict:
    parsed = task_map.parse_status_map(p.status.source, owner_tag=p.status.owner_tag)
    running = task_map.match_running_tasks(parsed.tasks, active)
    return {
        "pid": p.id,
        "name": p.status.name,
        "repo": p.repo,
        "path": p.path,
        "branch": p.branch,
        "sha": p.status_sha,
        "enabled": parsed.enabled,
        "can_write": bool(ctx.settings.github_token),
        "owner_tag": p.status.owner_tag,
        "groups": [g.to_dict() for g in parsed.groups],
        "tasks": [t.to_dict() for t in parsed.tasks],
        "transitive_edges": [list(edge) for edge in task_map.transitive_edges(parsed.tasks)],
        "problems": parsed.problems,
        "running": running,
        "next_markdown": task_map.next_markdown(p.status.source),
        "provisional_details": ctx.t["provisional_details"],
        "from_project_fmt": ctx.t["from_project"],
    }


@router.get("/", response_class=HTMLResponse)
async def projects_page(
    request: Request,
    refresh: int = 0,
    status: Literal["all", "active", "planned", "paused", "done"] = "all",
    ctx: AppContext = CTX,
):
    projects, errors = await ctx.hub.load(force=bool(refresh))
    all_groups = {k: [p for p in projects if p.status.status == k]
                  for k in ("active", "planned", "paused", "done")}
    status_counts = {k: len(v) for k, v in all_groups.items()}
    groups = all_groups if status == "all" else {status: all_groups[status]}

    all_tasks = ctx.store.all()
    running = {x.repo for x in all_tasks if x.status in ("queued", "running")}
    running_tasks: dict[str, object] = {}
    latest_tasks: dict[str, object] = {}
    for t in all_tasks:
        rk = t.repo.casefold()
        if t.status in ("running", "queued"):
            prev = running_tasks.get(rk)
            if prev is None or (prev.status == "queued" and t.status == "running"):
                running_tasks[rk] = t
        elif t.finished and rk not in latest_tasks:
            latest_tasks[rk] = t

    attention: list[dict] = []
    for p in projects:
        actions = p.status.sections.owner_actions
        if actions:
            attention.append({
                "kind": "owner",
                "project": p.status.name,
                "label": actions[0].display_title,
                "count": len(actions),
                "href": f"/p/{p.id}#owner-actions",
            })
    seen_repos: set[str] = set()
    for t in all_tasks:
        rk = t.repo.casefold()
        if rk in seen_repos:
            continue
        if t.status == "failed":
            seen_repos.add(rk)
            attention.append({
                "kind": "failed",
                "project": t.repo.split("/")[-1],
                "label": t.title,
                "count": 1,
                "href": f"/tasks/{t.id}",
            })
        elif t.status == "done" and t.pr_url and not t.merged:
            seen_repos.add(rk)
            attention.append({
                "kind": "open_pr",
                "project": t.repo.split("/")[-1],
                "label": t.title,
                "count": 1,
                "href": f"/tasks/{t.id}",
            })
        elif t.status == "done":
            seen_repos.add(rk)

    return ctx.page(
        request, "projects.html", "projects",
        groups=groups, status_filter=status, status_counts=status_counts,
        count=len(projects), errors=errors, no_token=not ctx.settings.github_token,
        running=running, running_tasks=running_tasks, latest_tasks=latest_tasks,
        attention=attention[:8],
    )


@router.get("/p/{pid:path}", response_class=HTMLResponse)
async def project(request: Request, pid: str, tab: Literal["next", "map", "done", "notes"] = "next",
                  page_number: int = Query(1, alias="page"), ctx: AppContext = CTX):
    projects, _ = await ctx.hub.load()
    for p in projects:
        if p.id == pid:
            content = p.status.sections
            tasks, active = _active_tasks_for(p.repo, ctx)
            finished = finished_items(content, tasks)
            map_data = _map_payload(p, active, ctx) if tab == "map" else None
            return ctx.page(
                request, "project.html", "projects", p=p, content=content, tab=tab,
                upcoming=content.upcoming, checked=sum(item.checked for item in content.checklist),
                finished=finished, done_page=ranking_view.paginate(finished, page_number, 10),
                notes=len(content.blockers) + len(content.decisions) + len(content.log),
                active=active, queue=ctx.runner.queue_info(), pulls=await ctx.hub.open_pulls(p.repo),
                summary_headline=summary_headline, map_data=map_data,
            )
    raise HTTPException(404, "Unknown project")


@router.get("/api/projects/{pid:path}/map")
async def api_project_map(pid: str, refresh: int = 0, ctx: AppContext = CTX):
    projects, _ = await ctx.hub.load(force=bool(refresh))
    proj = next((p for p in projects if p.id == pid), None)
    if proj is None:
        raise HTTPException(404, "Unknown project")
    _, active = _active_tasks_for(proj.repo, ctx)
    return JSONResponse(_map_payload(proj, active, ctx))


@router.get("/api/projects/{pid:path}/map/stream")
async def api_project_map_stream(request: Request, pid: str, ctx: AppContext = CTX):
    projects, _ = await ctx.hub.load()
    proj = next((p for p in projects if p.id == pid), None)
    if proj is None:
        raise HTTPException(404, "Unknown project")

    async def stream():
        last_sha, last_running = None, None
        current = proj
        last_sent = time.monotonic()
        while not await request.is_disconnected():
            sha = current.status_sha
            if sha != last_sha:
                parsed = task_map.parse_status_map(current.status.source, owner_tag=current.status.owner_tag)
            active = await asyncio.to_thread(ctx.store.active_for_repo, current.repo)
            running = task_map.match_running_tasks(parsed.tasks, active)
            if sha != last_sha or running != last_running:
                yield f"event: status\ndata: {json.dumps({'sha': sha, 'running': running})}\n\n"
                last_sha, last_running = sha, running
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= ctx.task_sse_keepalive:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(max(0.02, ctx.task_sse_poll))
            projects, _ = await ctx.hub.load()
            current = next((p for p in projects if p.id == pid), None)
            if current is None:
                return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/projects/{pid:path}/map/enable")
async def api_project_map_enable(request: Request, pid: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    if not ctx.settings.github_token:
        raise HTTPException(400, "GITHUB_TOKEN is required to save map edits.")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON payload.") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid JSON payload.")
    sha = str(body.get("sha") or "").strip()
    if not sha:
        raise HTTPException(400, "Missing STATUS.md sha.")

    projects, _ = await ctx.hub.load()
    proj = next((p for p in projects if p.id == pid), None)
    if proj is None:
        raise HTTPException(404, "Unknown project")

    parsed = task_map.parse_status_map(proj.status.source, owner_tag=proj.status.owner_tag)
    new_source = task_map.apply_map_to_source(
        proj.status.source, parsed.groups, parsed.tasks, owner_tag=proj.status.owner_tag
    )
    try:
        await ctx.hub.save_task_map(proj, new_source, sha, "Enable task map in STATUS.md")
    except ValueError as exc:
        fresh_projects, _ = await ctx.hub.load(force=True)
        fresh = next((p for p in fresh_projects if p.id == pid), None)
        payload = None
        if fresh is not None:
            _, active = _active_tasks_for(fresh.repo, ctx)
            payload = _map_payload(fresh, active, ctx)
        return JSONResponse({"detail": str(exc), "conflict": True, "map": payload}, status_code=409)
    except (GitHubError, httpx.RequestError) as exc:
        raise HTTPException(502, str(exc)) from exc

    _, active = _active_tasks_for(proj.repo, ctx)
    return JSONResponse(_map_payload(proj, active, ctx))


@router.post("/api/projects/{pid:path}/map")
async def api_project_map_save(request: Request, pid: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    if not ctx.settings.github_token:
        raise HTTPException(400, "GITHUB_TOKEN is required to save map edits.")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Invalid JSON payload.") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid JSON payload.")

    sha = str(body.get("sha") or "").strip()
    if not sha:
        raise HTTPException(400, "Missing STATUS.md sha.")
    raw_msg = " ".join(str(body.get("message") or "").split()).strip()
    commit_msg = f"Update task map: {raw_msg[:100]}" if raw_msg else "Update task map in STATUS.md"

    projects, _ = await ctx.hub.load()
    proj = next((p for p in projects if p.id == pid), None)
    if proj is None:
        raise HTTPException(404, "Unknown project")

    stored = task_map.parse_status_map(proj.status.source, owner_tag=proj.status.owner_tag)
    existing_titles = {t.id: t.title for t in stored.tasks}
    try:
        if "next_markdown" in body:
            if not isinstance(body["next_markdown"], str):
                raise ValueError("Expected Markdown text.")
            if not stored.enabled:
                raise ValueError("Enable the task map before editing Markdown.")
            local = task_map.parse_status_map(body["next_markdown"], owner_tag=proj.status.owner_tag,
                                              strict=True, existing_titles=existing_titles)
            groups, tasks = local.groups, local.tasks
        else:
            groups, tasks = task_map.validate_map_payload(body.get("groups"), body.get("tasks"),
                                                         existing_titles=existing_titles)
            local = task_map.TaskMap(groups=groups, tasks=tasks, enabled=True)
        base = body.get("base")
        if base is not None and not isinstance(base, dict):
            raise ValueError("Invalid base map.")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    def conflict(detail):
        payload = None
        if proj is not None:
            _, active = _active_tasks_for(proj.repo, ctx)
            payload = _map_payload(proj, active, ctx)
        return JSONResponse({"detail": detail, "conflict": True, "map": payload}, status_code=409)

    merged_changes = False
    for attempt in range(2):
        # A following batch may have been edited on top of an in-flight save.
        # Its base still describes what the user actually saw.
        if base is not None and sha == proj.status_sha:
            remote = task_map.parse_status_map(proj.status.source, owner_tag=proj.status.owner_tag)
            current = remote.to_dict()
            if attempt or any(base.get(key) != current[key] for key in ("groups", "tasks")):
                try:
                    original = task_map.TaskMap(*task_map.validate_map_payload(
                        base.get("groups"), base.get("tasks"), existing_titles=existing_titles
                    ))
                    merged = task_map.merge_task_maps(original, local, remote)
                except ValueError as exc:
                    return conflict(str(exc))
                merged_changes = True
                groups, tasks = merged.groups, merged.tasks
                if groups == remote.groups and tasks == remote.tasks:
                    break
        new_source = task_map.apply_map_to_source(
            proj.status.source, groups, tasks, owner_tag=proj.status.owner_tag
        )
        try:
            await ctx.hub.save_task_map(proj, new_source, sha, commit_msg)
            break
        except ValueError as exc:
            fresh_projects, _ = await ctx.hub.load(force=True)
            proj = next((p for p in fresh_projects if p.id == pid), None)
            if attempt or base is None or proj is None:
                return conflict(str(exc))
            sha = proj.status_sha
        except (GitHubError, httpx.RequestError) as exc:
            raise HTTPException(502, str(exc)) from exc

    _, active = _active_tasks_for(proj.repo, ctx)
    return JSONResponse(dict(_map_payload(proj, active, ctx), merged=merged_changes))


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
