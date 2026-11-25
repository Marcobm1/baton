"""Authentication routes (/login, /logout)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..auth import (COOKIE_NAME, SESSION_MAX_AGE, check_access_key,
                    make_session_token, safe_next_path, verify_session_token)
from .deps import CTX, AppContext

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", ctx: AppContext = CTX):
    if not ctx.auth_enabled:
        return RedirectResponse("/", status_code=303)
    dest = safe_next_path(next)
    if verify_session_token(request.cookies.get(COOKIE_NAME), ctx.access_key):
        return RedirectResponse(dest, status_code=303)
    return ctx.templates.TemplateResponse(
        request, "login.html",
        {"next_path": dest, "error": "", "login_csrf": ctx.login_csrf},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/login")
async def login_submit(request: Request, key: str = Form(""), csrf: str = Form(""),
                       next: str = Form("/"), ctx: AppContext = CTX):
    if not ctx.auth_enabled:
        return RedirectResponse("/", status_code=303)
    if not secrets.compare_digest(csrf or "", ctx.login_csrf):
        raise HTTPException(403, "Invalid form token; reload the page.")
    ctx.check_origin(request)
    dest = safe_next_path(next)
    client = request.client.host if request.client else "unknown"
    if ctx.login_limiter.is_limited(client):
        return ctx.templates.TemplateResponse(
            request, "login.html",
            {"next_path": dest, "error": ctx.t["auth_too_many"], "login_csrf": ctx.login_csrf},
            status_code=429, headers={"Cache-Control": "no-store"},
        )
    if not check_access_key(key, ctx.access_key):
        ctx.login_limiter.record_failure(client)
        return ctx.templates.TemplateResponse(
            request, "login.html",
            {"next_path": dest, "error": ctx.t["auth_invalid"], "login_csrf": ctx.login_csrf},
            status_code=401, headers={"Cache-Control": "no-store"},
        )
    ctx.login_limiter.reset(client)
    response = RedirectResponse(dest, status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(ctx.access_key),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=ctx.is_https(request),
        path="/",
    )
    return response


@router.post("/logout")
async def logout_submit(request: Request, csrf: str = Form(""), ctx: AppContext = CTX):
    ctx.check_post(request, csrf)
    response = RedirectResponse("/login" if ctx.auth_enabled else "/", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
