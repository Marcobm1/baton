"""Task list, new-task form, task detail, priority, cancel, and creation routes (/tasks)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from .. import ranking_view, task_defaults
from ..agents import AGENTS, SUMMARY_REQUEST, extract_summary, next_task_request
from ..github import GitHubError
from ..i18n import strings
from ..task_models import opencode_groups
from ..task_report import format_task_log, task_report
from ..task_store import PRIORITIES, Task, UpdateInProgress, new_id
from .deps import CTX, AppContext

router = APIRouter()


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(
    request: Request,
    status: Literal["all", "done", "no-changes", "failed", "cancelled", "sent"] = "all",
    repo: str = "",
    page_number: int = Query(1, alias="page"),
    ctx: AppContext = CTX,
):
    tasks = ctx.store.all()
    active = [x for x in tasks if x.status in ("queued", "running")]
    history = [x for x in tasks if x.status not in ("queued", "running")]
    repos: dict[str, int] = {}
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
    return ctx.page(
        request, "tasks.html", "tasks", tasks=tasks, active=active, status=status,
        repo=repo, repos=dict(sorted(repos.items(), key=lambda kv: -kv[1])),
        counts={k: v for k, v in counts.items() if v}, total=sum(counts.values()),
        history=ranking_view.paginate(history, page_number, ranking_view.PER_PAGE_TASKS),
        queue=ctx.runner.queue_info(), any_running=bool(active),
    )


@router.get("/tasks/new", response_class=HTMLResponse)
async def task_new(
    request: Request,
    repo: str = "",
    error: str = "",
    title: str = "",
    details: str = "",
    agent: str = "",
    model: str = "",
    effort: str = "",
    followup: str = "",
    ctx: AppContext = CTX,
):
    original = None
    details_limit = 5000
    if followup:
        original = ctx.store.get(followup)
        if not original or not original.finished:
            raise HTTPException(404, "Unknown finished task")
        repo, agent, model, effort = original.repo, original.agent, original.model, original.effort
        title = f"{ctx.t['followup']}: {original.title}"[:200]
        details = ctx.followup_context(original)
        details_limit += len(details)
    projects, _ = await ctx.hub.load()
    repos = sorted({p.repo for p in projects})
    local = {r: bool(ctx.runner.repos.get(r)) for r in repos}
    available = ctx.runner.agents_available()
    agents = [a for a in ("claude-code", "codex", "gemini") if not available.get(a, "missing")]
    oc_list, *choices = await asyncio.gather(
        asyncio.to_thread(ctx.opencode_models_fn) if not available["opencode"] else asyncio.sleep(0, result=[]),
        *(asyncio.to_thread(ctx.catalog_fn, a) for a in agents),
    )
    catalogs = dict(zip(agents, choices, strict=True))
    initial = task_defaults.initial_choice(
        {"agent": agent, "model": model, "effort": effort}, ctx.store.all(),
        available, catalogs, oc_list,
    )
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
    oc_groups = opencode_groups(oc_list, ctx.t)
    oc_default = (initial["model"] if initial["agent"] == "opencode" and initial["model"]
                  else (oc_groups[0]["models"][0] if oc_groups and oc_groups[0]["models"] else ""))
    return ctx.page(
        request, "task_new.html", "tasks", repos=repos, selected=repo,
        local=local, available=available, oc_models=oc_list, oc_groups=oc_groups,
        oc_selected=oc_default, oc_payg_initial=oc_default.startswith("opencode/"),
        catalogs=catalogs,
        cloud=ctx.settings.cloud_tasks, error=error[:300], auto_merge=ctx.settings.auto_merge,
        has_token=bool(ctx.settings.github_token), priorities=PRIORITIES,
        max_parallel=ctx.settings.max_parallel, draft_title=title, draft_details=details,
        initial=initial, followup=followup, details_limit=details_limit,
        repo_missing=repo_missing,
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str, ctx: AppContext = CTX):
    task = ctx.store.get(task_id)
    if not task:
        raise HTTPException(404, "Unknown task")
    report_error = False
    if task.where == "cloud" and task.issue_number and not task.summary:
        try:
            comments = await ctx.hub.agent_comments(task.repo, task.issue_number)
            for comment in comments:
                task.summary = extract_summary(comment)
                if task.summary:
                    ctx.store.save(task)
                    break
        except (GitHubError, httpx.RequestError):
            report_error = True
    log = ctx.store.read_log(task.id)
    # A finished task whose item is still unchecked in STATUS.md (typical
    # after "no changes": it was already done) can be ticked from here.
    pending = None
    if task.finished and task.status != "cancelled":
        projects, _ = await ctx.hub.load()
        here = task.repo.casefold()
        for project in projects:
            # Its own repository, or another project's item that runs here (Repo: line).
            item = next((i for i in project.status.sections.upcoming
                         if i.title.casefold() == task.title.strip().casefold()
                         and here in (project.repo.casefold(), i.repo.casefold())), None)
            if item:
                pending = {"project": project, "item": item}
                break
    rendered_log = format_task_log(task.agent, log)
    return ctx.page(
        request, "task.html", "tasks", task=task, report=task_report(task, log), pending=pending,
        log="\n".join(rendered_log.splitlines()[-200:]), can_cancel=ctx.runner.can_cancel(task.id),
        report_error=report_error, ranking_job=ctx.store.record_task(task),
        queue=ctx.runner.queue_info().get(task.id), priorities=PRIORITIES,
    )


def _task_snapshot(ctx: AppContext, task: Task) -> dict:
    raw_log = ctx.store.read_log(task.id)
    rendered = format_task_log(task.agent, raw_log)
    report = task_report(task, raw_log)
    return {
        "status": task.status,
        "status_label": ctx.t.get(f"st_{task.status}", task.status),
        "finished": task.finished,
        "last_step": task.steps[-1] if task.steps else "",
        "progress": report.get("progress") or "",
        "steps": list(task.steps),
        "log": "\n".join(rendered.splitlines()[-200:]),
        "can_cancel": ctx.runner.can_cancel(task.id),
    }


@router.get("/api/tasks/{task_id}/events")
async def task_events(request: Request, task_id: str, ctx: AppContext = CTX):
    """Server-sent events streaming live status, steps, progress, and log tail for one task."""
    initial = ctx.store.get(task_id)
    if not initial:
        raise HTTPException(404, "Unknown task")

    async def stream():
        last_payload: str | None = None
        last_sent = time.monotonic()
        poll = max(0.02, ctx.task_sse_poll)
        keepalive = max(poll, ctx.task_sse_keepalive)
        while True:
            task = await asyncio.to_thread(ctx.store.get, task_id)
            if task is None:
                yield 'event: done\ndata: {"status": "deleted"}\n\n'
                return
            snap = await asyncio.to_thread(_task_snapshot, ctx, task)
            encoded = json.dumps(snap, ensure_ascii=False)
            if encoded != last_payload:
                yield f"event: update\ndata: {encoded}\n\n"
                last_payload = encoded
                last_sent = time.monotonic()
            if task.status not in ("queued", "running"):
                yield f"event: done\ndata: {json.dumps({'status': task.status})}\n\n"
                return
            if await request.is_disconnected():
                return
            if time.monotonic() - last_sent >= keepalive:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(poll)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/tasks/{task_id}/priority")
async def task_priority(request: Request, task_id: str, csrf: str = Form(""),
                        priority: str = Form(""), ctx: AppContext = CTX):
    ctx.check_post(request, csrf)
    if not ctx.store.get(task_id):
        raise HTTPException(404, "Unknown task")
    if priority not in PRIORITIES:
        raise HTTPException(400, "Unknown priority")
    if not ctx.runner.set_priority(task_id, priority):
        raise HTTPException(409, "Only a queued task can change its priority.")
    return RedirectResponse(f"/tasks/{quote(task_id)}", status_code=303)


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(request: Request, task_id: str, csrf: str = Form(""),
                      ctx: AppContext = CTX):
    ctx.check_post(request, csrf)
    if not ctx.store.get(task_id):
        raise HTTPException(404, "Unknown task")
    # Killing the agent and removing the worktree block for a few seconds:
    # do it in a thread so the other pages keep answering.
    if not await asyncio.to_thread(ctx.runner.cancel, task_id):
        raise HTTPException(409, "This task is not queued or running on this computer, or is already finishing.")
    return RedirectResponse(f"/tasks/{quote(task_id)}", status_code=303)


@router.post("/tasks")
async def task_create(
    request: Request,
    repo: str = Form(...),
    where: str = Form(...),
    title: str = Form(...),
    details: str = Form(""),
    oc_model: str = Form(""),
    csrf: str = Form(""),
    model: str = Form(""),
    effort: str = Form(""),
    priority: str = Form("normal"),
    followup: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    if priority not in PRIORITIES:
        raise HTTPException(400, "Unknown priority")
    projects, _ = await ctx.hub.load()
    by_repo = {p.repo: p for p in projects}
    if repo not in by_repo:
        raise HTTPException(400, "Unknown repository")
    original = None
    details_limit = 5000
    if followup:
        details = details.replace("\r\n", "\n")
        original = ctx.store.get(followup)
        if not original or not original.finished:
            raise HTTPException(400, "Unknown finished task")
        details_limit += len(ctx.followup_context(original))
    title, details = title.strip()[:200], details.strip()[:details_limit]
    if not title:
        raise HTTPException(400, "A title is needed")

    def back(msg: str):
        return RedirectResponse(f"/tasks/new?repo={quote(repo)}&error={quote(msg)}",
                                status_code=303)

    if where == "cloud":
        if not ctx.settings.cloud_tasks:
            raise HTTPException(400, "Cloud tasks are turned off")
        body = (f"@claude {details or title}\n\n---\n"
                "Task created from Baton. Follow this repository's AGENTS.md / CLAUDE.md, "
                "open a pull request with the change, and update STATUS.md in it. "
                f"{next_task_request(await asyncio.to_thread(ctx.runner.model_choices))} "
                f"Post your report in a comment on this issue. {SUMMARY_REQUEST}")
        try:
            issue = await ctx.hub.create_issue(repo, title, body)
        except GitHubError as exc:
            return back(str(exc))
        task = Task(id=new_id(title), repo=repo, title=title, details=details,
                    agent="claude-code", where="cloud", created_at=time.time(),
                    status="sent", issue_url=issue["url"], issue_number=issue["number"],
                    message="Sent to GitHub Actions.")
        ctx.store.save(task)
        return RedirectResponse(f"/tasks/{task.id}", status_code=303)

    agent = where.removeprefix("local:")
    if not where.startswith("local:") or agent not in AGENTS:
        raise HTTPException(400, "Unknown destination")
    reason = ctx.runner.agents_available().get(agent, "unknown agent")
    if reason:
        return back(reason)
    if not ctx.runner.repos.get(repo):
        return back("There is no local clone of this repository on this computer.")
    if agent in ("codex", "claude-code", "gemini") and (model or effort):
        options = await asyncio.to_thread(ctx.catalog_fn, agent)
        if model not in options or (effort and effort not in options[model]["efforts"]):
            return back(strings(ctx.settings.lang)["invalid_model"])
    elif agent == "opencode":
        # Only a model OpenCode itself lists is accepted.
        if oc_model not in await asyncio.to_thread(ctx.opencode_models_fn):
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
        ctx.runner.submit(task)
    except UpdateInProgress as exc:
        raise HTTPException(503, ctx.t["update_testing"]) from exc
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)
