"""JSON API and health check routes (/api/projects, /api/quotas, /api/models, /healthz)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import quotas
from .deps import CTX, AppContext

router = APIRouter()


@router.get("/api/projects")
async def api_projects(refresh: int = 0, ctx: AppContext = CTX):
    projects, errors = await ctx.hub.load(force=bool(refresh))
    return JSONResponse({"projects": [p.to_dict() for p in projects], "errors": errors})


@router.get("/api/quotas")
async def api_quotas(ctx: AppContext = CTX):
    await asyncio.to_thread(ctx.refresh_go)
    return JSONResponse({"windows": quotas.all_windows(ctx.settings.data_dir, ctx.settings.codex_home)})


@router.get("/api/models")
async def api_models(ctx: AppContext = CTX):
    return JSONResponse(await asyncio.to_thread(ctx.models.overview, ctx.settings.lang))


@router.get("/manifest.webmanifest")
async def webmanifest(ctx: AppContext = CTX):
    name = ctx.settings.title
    payload = {
        "name": name,
        "short_name": name,
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0c0c0d",
        "theme_color": "#0c0c0d",
        "icons": [
            {"src": "/static/huo-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/huo-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/huo-icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        ],
    }
    return JSONResponse(payload, media_type="application/manifest+json")


@router.get("/healthz")
async def healthz():
    return {"ok": True}
