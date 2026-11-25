"""Local model monitoring and start/stop routes (/models)."""

from __future__ import annotations

import asyncio
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..models import ModelError
from .deps import CTX, AppContext

router = APIRouter()


@router.get("/models", response_class=HTMLResponse)
async def models_page(request: Request, error: str = "", ctx: AppContext = CTX):
    view = await asyncio.to_thread(ctx.models.overview, ctx.settings.lang)
    return ctx.page(request, "models.html", "models", view=view, error=error[:300])


@router.post("/models/{model_id}/{action}")
async def model_action(
    request: Request,
    model_id: str,
    action: Literal["start", "stop"],
    csrf: str = Form(""),
    back: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    # Started from the chat page: go back there with that model selected.
    target = f"/chat?model={quote(model_id)}" if back == "/chat" else "/models"
    sep = "&" if "?" in target else "?"
    try:
        # Stopping waits for the process to exit: keep the other pages answering.
        await asyncio.to_thread(ctx.models.start if action == "start" else ctx.models.stop, model_id)
    except ModelError as exc:
        return RedirectResponse(f"{target}{sep}error={quote(exc.text(ctx.t))}", status_code=303)
    return RedirectResponse(target, status_code=303)
