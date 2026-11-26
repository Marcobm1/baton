"""Machines monitoring routes (/machines, /api/machines, /api/machines/heartbeat)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..machines import MAX_HEARTBEAT_BYTES
from .deps import CTX, AppContext

router = APIRouter()


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


@router.get("/machines", response_class=HTMLResponse)
async def machines_page(request: Request, ctx: AppContext = CTX):
    cards = await asyncio.to_thread(ctx.machines.list_machines)
    return ctx.page(request, "machines.html", "machines", machines=cards, created=None, error="")


@router.post("/machines/add", response_class=HTMLResponse)
async def machines_add(
    request: Request,
    name: str = Form(""),
    csrf: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    try:
        machine, raw_token = await asyncio.to_thread(ctx.machines.create_machine, name)
        created = {"id": machine["id"], "name": machine["name"], "token": raw_token}
        error = ""
    except ValueError as exc:
        created = None
        error = str(exc)
    cards = await asyncio.to_thread(ctx.machines.list_machines)
    return ctx.page(request, "machines.html", "machines", machines=cards, created=created, error=error)


@router.post("/machines/{machine_id}/delete")
async def machines_delete(
    request: Request,
    machine_id: str,
    csrf: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    await asyncio.to_thread(ctx.machines.delete_machine, machine_id)
    return RedirectResponse("/machines", status_code=303)


@router.get("/api/machines")
async def machines_api(ctx: AppContext = CTX):
    cards = await asyncio.to_thread(ctx.machines.list_machines)
    return JSONResponse({"machines": cards})


@router.post("/api/machines/heartbeat")
async def machines_heartbeat(request: Request, ctx: AppContext = CTX):
    """Receive one read-only metrics heartbeat authenticated by per-machine Bearer token."""
    token = _bearer_token(request)
    if not token:
        raise HTTPException(401, "Missing machine Bearer token.")
    machine = await asyncio.to_thread(ctx.machines.verify_token, token)
    if machine is None:
        raise HTTPException(401, "Invalid machine token.")
    raw = await request.body()
    if len(raw) > MAX_HEARTBEAT_BYTES:
        raise HTTPException(413, "Heartbeat payload too large.")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, "Invalid JSON payload.") from exc
    try:
        await asyncio.to_thread(ctx.machines.record_heartbeat, machine["id"], body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(401, "Machine no longer registered.") from exc
    return JSONResponse({"ok": True, "machine_id": machine["id"]})
