"""Local model chat relay and ACP Agents chat routes (/chat, /api/agents, /api/chat)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..agent_chat import EFFORTS, MAX_PROMPT_CHARS, MODES, ChatError, is_abliterated
from ..models import ModelError, chat_payload
from ..task_models import opencode_groups
from .deps import CTX, AppContext

router = APIRouter()


def _agent_error(ctx: AppContext, exc: ChatError, status: int = 409) -> HTTPException:
    text = ctx.t.get(exc.key, exc.key)
    return HTTPException(status, f"{text} {exc.detail}".strip())


def _get_chat(ctx: AppContext, chat_id: str):
    chat = ctx.agent_chats.get(chat_id)
    if chat is None:
        raise HTTPException(404, ctx.t["ag_closed"])
    return chat


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, "A JSON object is needed.")
    return body


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    request: Request,
    model: str = "",
    error: str = "",
    agent: Literal["local", "opencode", "gemini"] = "local",
    ctx: AppContext = CTX,
):
    view = await asyncio.to_thread(ctx.models.overview, ctx.settings.lang)
    ids = [m["id"] for m in view["models"]]
    running = [m["id"] for m in view["models"] if m["status"] in ("running", "external")]
    selected = model if model in ids else (running[0] if running else (ids[0] if ids else ""))
    oc: dict = {"available": False, "groups": [], "folders": []}
    if agent == "opencode" and ctx.find_opencode_fn():
        oc_list = await asyncio.to_thread(ctx.opencode_models_fn)
        active_families = {m["family_key"] for m in view["models"] if m["uses_gpu"]}
        oc = {"available": True, "groups": opencode_groups(oc_list, ctx.t, active_families=active_families),
              "folders": await asyncio.to_thread(ctx.chat_folders)}
    elif agent == "gemini" and ctx.find_agy_fn():
        gm = await asyncio.to_thread(ctx.catalog_fn, "gemini")
        # One group; each model carries its own thinking levels (data-efforts).
        oc = {"available": True, "folders": await asyncio.to_thread(ctx.chat_folders),
              "groups": [{"provider": "gemini", "label": "Gemini", "included": False, "payg": False,
                          "models": list(gm)}] if gm else [],
              "efforts": {m: v["efforts"] for m, v in gm.items()}}
    return ctx.page(request, "chat.html", "chat", view=view, selected=selected, error=error[:300],
                    agent=agent, oc=oc, efforts=EFFORTS)


@router.post("/api/agents")
async def agents_create(request: Request, ctx: AppContext = CTX):
    """Start a conversation. Agent, folder, model, effort and permission
    mode are checked and fixed here, on the server, for its whole life."""
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    body = await _json_body(request)
    agent, model = body.get("agent", "opencode"), str(body.get("model") or "")
    effort, mode = str(body.get("effort") or "default"), str(body.get("mode") or "confirm")
    if agent not in ("opencode", "gemini"):
        raise HTTPException(400, "Unknown agent")
    if mode not in MODES or effort not in EFFORTS:
        raise HTTPException(400, "Unknown permission mode or reasoning effort")
    if agent == "gemini":
        if model not in await asyncio.to_thread(ctx.catalog_fn, "gemini"):
            raise HTTPException(400, ctx.t["ag_err_model_gemini"])
    elif model not in await asyncio.to_thread(ctx.opencode_models_fn):
        raise HTTPException(400, ctx.t["ag_err_model"])
    if mode == "full":
        # Refused for everyone in this phase; abliterated models never get it.
        view = await asyncio.to_thread(ctx.models.overview, ctx.settings.lang)
        if is_abliterated(model, view["models"]):
            raise HTTPException(403, ctx.t["ag_full_abliterated"])
        raise HTTPException(403, ctx.t["ag_full_off"])
    folders = {f["key"]: f for f in await asyncio.to_thread(ctx.chat_folders)}
    folder = folders.get(str(body.get("folder") or ""))
    if folder is None:
        raise HTTPException(400, ctx.t["ag_err_folder"])
    path = folder["path"]
    if folder["key"] == "scratch":
        path.mkdir(parents=True, exist_ok=True)
    # The canonical folder (junctions and links resolved) is what OpenCode gets.
    path = Path(os.path.realpath(path))
    try:
        chat = await asyncio.to_thread(ctx.agent_chats.create, path, folder["label"], model, effort, mode,
                                       agent)
    except ChatError as exc:
        raise _agent_error(ctx, exc, 502 if exc.key == "ag_err_start" else 409) from exc
    return JSONResponse(chat.info())


@router.get("/api/agents/{chat_id}")
async def agents_info(chat_id: str, ctx: AppContext = CTX):
    return JSONResponse(_get_chat(ctx, chat_id).info())


@router.post("/api/agents/{chat_id}/prompt")
async def agents_prompt(request: Request, chat_id: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    chat = _get_chat(ctx, chat_id)
    text = (await _json_body(request)).get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_PROMPT_CHARS:
        raise HTTPException(400, f"Write a message of up to {MAX_PROMPT_CHARS} characters.")
    try:
        chat.prompt(text.strip())
    except ChatError as exc:
        raise _agent_error(ctx, exc) from exc
    return JSONResponse(chat.info(), status_code=202)


@router.get("/api/agents/{chat_id}/events")
async def agents_events(request: Request, chat_id: str, after: int = Query(0, ge=0),
                        ctx: AppContext = CTX):
    """Server-sent events of one conversation from event `after` on. The
    stream ends once no turn is running; reconnecting (Last-Event-ID)
    continues where it stopped, so a phone that sleeps loses nothing."""
    chat = _get_chat(ctx, chat_id)
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


@router.post("/api/agents/{chat_id}/permission")
async def agents_permission(request: Request, chat_id: str, ctx: AppContext = CTX):
    """Allow or deny one pending request. The id is looked up only among
    this conversation's pending requests and works once."""
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    chat = _get_chat(ctx, chat_id)
    body = await _json_body(request)
    allow = body.get("allow")
    if not isinstance(allow, bool):
        raise HTTPException(400, "allow must be true or false")
    if not await asyncio.to_thread(chat.answer, str(body.get("id") or ""), allow):
        raise HTTPException(404, ctx.t["ag_expired"])
    return JSONResponse({"ok": True})


@router.post("/api/agents/{chat_id}/cancel")
async def agents_cancel(request: Request, chat_id: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    await asyncio.to_thread(_get_chat(ctx, chat_id).cancel)
    return JSONResponse({"ok": True})


@router.post("/api/agents/{chat_id}/close")
async def agents_close(request: Request, chat_id: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    _get_chat(ctx, chat_id)
    await asyncio.to_thread(ctx.agent_chats.close, chat_id)
    return JSONResponse({"ok": True})


@router.post("/api/chat/{model_id}")
async def chat_api(request: Request, model_id: str, ctx: AppContext = CTX):
    """Relay a chat to a local model and stream its answer back (server-sent
    events), so the chat works from any device that can open Baton."""
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    try:
        payload = chat_payload(await request.json())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        port = await asyncio.to_thread(ctx.models.chat_port, model_id)
    except ModelError as exc:
        raise HTTPException(404 if exc.key == "m_err_unknown" else 409, exc.text(ctx.t)) from exc
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
