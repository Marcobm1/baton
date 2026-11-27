"""Town Hall routes mounted inside Huo (/townhall, /townhall/{id}, /api/townhall/*)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from townhall import Participant, SessionConfig, render_markdown

from .. import quotas
from ..task_models import opencode_groups, readable_model_name
from ..townhall_bridge import (
    DEFAULT_GUEST_POOL,
    MAX_PARTICIPANTS,
    MIN_PARTICIPANTS,
    PROVIDER_LABELS,
    VALID_PROVIDERS,
    build_core_participants,
    build_participants,
    build_schedule_spec,
    format_provider_quotas,
    pick_strong_model,
    resolve_moderator_id,
)
from .deps import CTX, AppContext

router = APIRouter()

_DEMO_MODELS: dict[str, list[dict[str, str]]] = {
    "claude-code": [
        {"id": "claude-opus-4-6", "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
    ],
    "codex": [
        {"id": "gpt-5.6-astra", "label": "GPT 5.6 Astra"},
        {"id": "gpt-5.6-mini", "label": "GPT 5.6 Mini"},
    ],
    "gemini": [
        {"id": "gemini-4-neon", "label": "Gemini 4 Neon"},
        {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash"},
    ],
    "opencode": [
        {"id": m, "label": readable_model_name(m)} for m in DEFAULT_GUEST_POOL
    ],
}


def _resolve_folder(ctx: AppContext, folder_key: str) -> tuple[str, str]:
    """Resolve an allowlisted folder key (`owner/repo` or `scratch`) to `(key, path_str)`."""
    key = (folder_key or "").strip()
    if not key:
        return "", ""
    for item in ctx.chat_folders():
        if item["key"] == key:
            p: Path = item["path"]
            return key, str(p) if p.is_dir() else ""
    raise HTTPException(400, "Unknown workspace folder.")


def _folder_key_for_workspace(ctx: AppContext, workspace: str) -> str:
    if not workspace:
        return ""
    try:
        target = Path(workspace).resolve()
    except OSError:
        return ""
    for item in ctx.chat_folders():
        try:
            if Path(item["path"]).resolve() == target:
                return item["key"]
        except OSError:
            continue
    return ""


async def _load_model_choices(ctx: AppContext) -> dict[str, Any]:
    available = ctx.runner.agents_available()
    cli_agents = [a for a in ("claude-code", "codex", "gemini") if not available.get(a, "missing")]
    oc_list, quota_views, *cats = await asyncio.gather(
        asyncio.to_thread(ctx.opencode_models_fn) if not available.get("opencode") else asyncio.sleep(0, result=[]),
        asyncio.to_thread(quotas.all_windows, ctx.settings.data_dir, ctx.settings.codex_home),
        *(asyncio.to_thread(ctx.catalog_fn, a) for a in cli_agents),
    )
    catalogs = dict(zip(cli_agents, cats, strict=True))
    go_models = [m for m in oc_list if m.startswith("opencode-go/")]
    guest_pool = go_models or list(oc_list) or list(DEFAULT_GUEST_POOL)
    quota_map = format_provider_quotas(quota_views)

    families: list[dict[str, Any]] = []
    for fid in VALID_PROVIDERS:
        is_avail = not available.get(fid, "missing")
        if fid == "opencode":
            models = [{"id": m, "label": readable_model_name(m)} for m in oc_list]
        else:
            cat = catalogs.get(fid, {})
            models = [
                {"id": mid, "label": readable_model_name(mid, meta.get("label", ""))}
                for mid, meta in cat.items()
            ]
        families.append({
            "id": fid,
            "label": PROVIDER_LABELS[fid],
            "available": is_avail,
            "quota": quota_map.get(fid, {"summary": "", "min_left": None, "exhausted": False}),
            "models": models,
            "demo_models": _DEMO_MODELS[fid],
        })

    return {
        "available": available,
        "catalogs": catalogs,
        "families": families,
        "quotas": quota_map,
        "oc_models": oc_list,
        "oc_groups": opencode_groups(oc_list, ctx.t),
        "go_models": go_models,
        "default_guest_pool": guest_pool,
        "defaults": {
            "gemini": pick_strong_model(catalogs.get("gemini", {}), ("neon", "pro", "3.8")),
            "codex": pick_strong_model(catalogs.get("codex", {}), ("astra", "5.6", "codex")),
            "claude": pick_strong_model(catalogs.get("claude-code", {}), ("opus", "sonnet")),
        },
    }


def _parse_participants(
    providers: list[str],
    models: list[str],
    *,
    legacy_models: dict[str, str] | None = None,
    use_demo: bool = False,
    allow_legacy_fallback: bool = False,
) -> list[Participant]:
    """Validate 2–4 dynamic participant rows, with optional fallback for legacy form callers."""
    if providers or models:
        if len(providers) != len(models):
            raise HTTPException(400, "Mismatched participant family and model fields.")
        try:
            return build_participants(list(zip(providers, models, strict=True)))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if allow_legacy_fallback:
        leg = legacy_models or {}
        return build_core_participants(
            gemini_model=(leg.get("gemini") or "").strip() or ("gemini-4-neon" if use_demo else ""),
            codex_model=(leg.get("codex") or "").strip() or ("gpt-5.6-astra" if use_demo else ""),
            claude_model=(leg.get("claude") or "").strip() or ("claude-opus" if use_demo else ""),
        )
    raise HTTPException(400, f"Choose between {MIN_PARTICIPANTS} and {MAX_PARTICIPANTS} participants.")


async def _resolve_guest_pool(ctx: AppContext, guest_scope: str, *, use_demo: bool = False) -> list[str]:
    if guest_scope == "none":
        return []
    if use_demo:
        return list(DEFAULT_GUEST_POOL)
    oc_models = (
        await asyncio.to_thread(ctx.opencode_models_fn)
        if not ctx.runner.agents_available().get("opencode")
        else []
    )
    if guest_scope == "go":
        pool = [m for m in oc_models if m.startswith("opencode-go/")]
    else:
        pool = list(oc_models)
    return pool or list(DEFAULT_GUEST_POOL)


@router.get("/townhall", response_class=HTMLResponse)
async def townhall_page(request: Request, ctx: AppContext = CTX):
    choices = await _load_model_choices(ctx)
    folders = ctx.chat_folders()
    sessions = await asyncio.to_thread(ctx.townhall.store.list_sessions, limit=30)
    schedules = await asyncio.to_thread(ctx.townhall.store.list_schedules)
    picker_i18n = {
        "choose_family": ctx.t["th_choose_family"],
        "choose_model": ctx.t["th_choose_model"],
        "choose_family_first": ctx.t["th_choose_family_first"],
        "no_models": ctx.t["th_no_models"],
        "not_installed": ctx.t["th_not_installed"],
        "quota_low": ctx.t["th_quota_low"],
        "remove_model": ctx.t["th_remove_model"],
        "add_model": ctx.t["th_add_model"],
        "moderator_pick": ctx.t["th_moderator_pick"],
        "slot_label": ctx.t["th_slot_label"],
    }
    return ctx.page(
        request,
        "townhall.html",
        "townhall",
        folders=folders,
        sessions=sessions,
        schedules=schedules,
        available=choices["available"],
        catalogs=choices["catalogs"],
        families=choices["families"],
        picker_i18n=picker_i18n,
        min_participants=MIN_PARTICIPANTS,
        max_participants=MAX_PARTICIPANTS,
        defaults=choices["defaults"],
        go_count=len(choices["go_models"]),
        oc_count=len(choices["oc_models"]),
        active_count=ctx.townhall.active_count(),
    )


@router.post("/townhall")
async def townhall_create(
    request: Request,
    csrf: str = Form(""),
    topic: str = Form(""),
    folder: str = Form(""),
    mode: str = Form("council"),
    rounds: int = Form(1),
    max_turns: int = Form(6),
    max_seconds: float = Form(600.0),
    moderator: str = Form("0"),
    participant_provider: Annotated[list[str] | None, Form()] = None,
    participant_model: Annotated[list[str] | None, Form()] = None,
    gemini_model: str = Form(""),
    codex_model: str = Form(""),
    claude_model: str = Form(""),
    guest_scope: str = Form("go"),
    demo_mode: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    topic_clean = topic.strip()
    if not topic_clean:
        raise HTTPException(400, "Topic is required.")
    _, ws_path = _resolve_folder(ctx, folder)
    use_demo = demo_mode == "1"

    legacy_present = bool(gemini_model.strip() or codex_model.strip() or claude_model.strip())
    participants = _parse_participants(
        participant_provider or [],
        participant_model or [],
        legacy_models={"gemini": gemini_model, "codex": codex_model, "claude": claude_model},
        use_demo=use_demo,
        allow_legacy_fallback=use_demo or legacy_present,
    )
    guest_pool = await _resolve_guest_pool(ctx, guest_scope, use_demo=use_demo)

    cfg = SessionConfig(
        topic=topic_clean,
        participants=participants,
        mode="open_floor" if mode == "open_floor" else "council",
        workspace=ws_path,
        guest_pool=guest_pool,
        debate_rounds=max(0, min(3, int(rounds))),
        max_turns=max(2, min(30, int(max_turns))),
        max_duration_seconds=max(10.0, min(3600.0, float(max_seconds))),
        moderator_id=resolve_moderator_id(moderator, participants),
        language=ctx.settings.lang,
        isolate_workspace=not use_demo,
    )
    sid = ctx.townhall.start_session(cfg, demo=use_demo)
    return RedirectResponse(f"/townhall/{sid}", status_code=303)


@router.get("/townhall/{session_id}.md")
async def townhall_markdown(session_id: str, ctx: AppContext = CTX):
    rec = await asyncio.to_thread(ctx.townhall.store.get, session_id)
    if rec is None:
        raise HTTPException(404, "Town Hall session not found.")
    return PlainTextResponse(render_markdown(rec), media_type="text/markdown; charset=utf-8")


@router.get("/townhall/{session_id}", response_class=HTMLResponse)
async def townhall_detail(request: Request, session_id: str, ctx: AppContext = CTX):
    rec = await asyncio.to_thread(ctx.townhall.store.get, session_id)
    if rec is None:
        raise HTTPException(404, "Town Hall session not found.")
    folder_key = _folder_key_for_workspace(ctx, rec.workspace)
    repo_key = folder_key if "/" in folder_key else ""
    transcript_turns = [
        t for t in rec.turns
        if t.phase != "vote_guest" and not (t.phase == "synthesis" and rec.conclusion is not None)
    ]
    return ctx.page(
        request,
        "townhall_detail.html",
        "townhall",
        session=rec,
        transcript_turns=transcript_turns,
        folder_key=folder_key,
        repo_key=repo_key,
    )


@router.post("/townhall/{session_id}/cancel")
async def townhall_cancel(
    request: Request,
    session_id: str,
    csrf: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    ctx.townhall.cancel_session(session_id)
    return RedirectResponse(f"/townhall/{session_id}", status_code=303)


@router.post("/townhall/schedules")
async def townhall_schedule_create(
    request: Request,
    csrf: str = Form(""),
    name: str = Form(""),
    topic: str = Form(""),
    folder: str = Form(""),
    mode: str = Form("council"),
    interval_hours: float = Form(168.0),
    allowed_hours: str = Form(""),
    max_seconds: float = Form(600.0),
    moderator: str = Form("0"),
    participant_provider: Annotated[list[str] | None, Form()] = None,
    participant_model: Annotated[list[str] | None, Form()] = None,
    guest_scope: str = Form("go"),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    if not name.strip() or not topic.strip():
        raise HTTPException(400, "Schedule name and topic are required.")
    _, ws_path = _resolve_folder(ctx, folder)
    if participant_provider or participant_model:
        participants = _parse_participants(participant_provider or [], participant_model or [])
        guest_pool = await _resolve_guest_pool(ctx, guest_scope)
    else:
        choices = await _load_model_choices(ctx)
        participants = build_core_participants(
            gemini_model=choices["defaults"]["gemini"],
            codex_model=choices["defaults"]["codex"],
            claude_model=choices["defaults"]["claude"],
        )
        guest_pool = [] if guest_scope == "none" else choices["default_guest_pool"]
    spec = build_schedule_spec(
        name=name.strip(),
        topic=topic.strip(),
        participants=participants,
        workspace=ws_path,
        guest_pool=guest_pool,
        mode=mode,
        max_duration_seconds=max_seconds,
        moderator_id=resolve_moderator_id(moderator, participants),
        interval_hours=interval_hours,
        allowed_hours=allowed_hours,
        language=ctx.settings.lang,
    )
    await asyncio.to_thread(ctx.townhall.store.save_schedule, spec)
    return RedirectResponse("/townhall", status_code=303)


@router.post("/townhall/schedules/{schedule_id}/toggle")
async def townhall_schedule_toggle(
    request: Request,
    schedule_id: str,
    csrf: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    spec = await asyncio.to_thread(ctx.townhall.store.get_schedule, schedule_id)
    if spec is not None:
        await asyncio.to_thread(ctx.townhall.store.set_schedule_enabled, spec.id, not spec.enabled)
    return RedirectResponse("/townhall", status_code=303)


@router.post("/townhall/schedules/{schedule_id}/run")
async def townhall_schedule_run(
    request: Request,
    schedule_id: str,
    csrf: str = Form(""),
    demo_mode: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    sid = ctx.townhall.trigger_schedule_now(schedule_id, demo=(demo_mode == "1"))
    return RedirectResponse(f"/townhall/{sid}" if sid else "/townhall", status_code=303)


@router.post("/townhall/schedules/{schedule_id}/delete")
async def townhall_schedule_delete(
    request: Request,
    schedule_id: str,
    csrf: str = Form(""),
    ctx: AppContext = CTX,
):
    ctx.check_post(request, csrf)
    await asyncio.to_thread(ctx.townhall.store.delete_schedule, schedule_id)
    return RedirectResponse("/townhall", status_code=303)


@router.get("/api/townhall")
async def api_townhall_list(ctx: AppContext = CTX):
    sessions = await asyncio.to_thread(ctx.townhall.store.list_sessions, limit=50)
    schedules = [s.to_dict() for s in await asyncio.to_thread(ctx.townhall.store.list_schedules)]
    return JSONResponse({"sessions": sessions, "schedules": schedules})


@router.get("/api/townhall/{session_id}")
async def api_townhall_detail(session_id: str, ctx: AppContext = CTX):
    rec = await asyncio.to_thread(ctx.townhall.store.get, session_id)
    if rec is None:
        raise HTTPException(404, "Town Hall session not found.")
    return JSONResponse(rec.to_dict())


@router.get("/api/townhall/{session_id}/events")
async def api_townhall_events(session_id: str, ctx: AppContext = CTX):
    async def _stream():
        active = ctx.townhall.get_active(session_id)
        if active is None:
            rec = await asyncio.to_thread(ctx.townhall.store.get, session_id)
            ev = {
                "type": f"session_{rec.status}" if rec else "session_failed",
                "session_id": session_id,
                "payload": {},
            }
            yield f"data: {json.dumps(ev)}\n\n"
            return

        idx = 0
        while True:
            def _wait_batch(cur_idx: int):
                with active.cond:
                    if cur_idx >= len(active.events) and not active.done:
                        active.cond.wait(timeout=1.0)
                    return list(active.events[cur_idx:]), len(active.events), active.done

            batch, new_idx, done = await asyncio.to_thread(_wait_batch, idx)
            idx = new_idx
            for ev in batch:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if done and idx >= len(active.events):
                return

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
