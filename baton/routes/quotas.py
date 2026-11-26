"""Quotas page and manual self-update trigger routes."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import quotas
from .deps import CTX, AppContext

router = APIRouter()


@router.get("/quotas", response_class=HTMLResponse)
async def quotas_page(request: Request, ctx: AppContext = CTX):
    go_error = await asyncio.to_thread(ctx.refresh_go)
    gemini_error = ctx.gemini_error()
    return ctx.page(
        request, "quotas.html", "quotas",
        go_error=go_error,
        gemini_error=gemini_error,
        providers=quotas.by_provider(
            quotas.all_windows(ctx.settings.data_dir, ctx.settings.codex_home)
        ),
        sched=ctx.scheduler,
        tokens=ctx.token_usage(),
        sched_results={
            k: {"age": time.time() - v[0], "ok": v[1], "msg": v[2]}
            for k, v in ctx.scheduler.results.items()
        },
    )


@router.post("/self-update")
async def update_now(request: Request, csrf: str = Form(""), ctx: AppContext = CTX):
    ctx.check_post(request, csrf)
    if ctx.updater.enabled:
        ctx.updater.request_manual()
        ctx.update_requested.set()
    return RedirectResponse("/machines#self-update", status_code=303)
