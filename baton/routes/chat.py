"""Local model chat relay and ACP Agents chat routes (/chat, /api/agents, /api/chat)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ..agent_chat import CHAT_AGENTS, EFFORTS, MAX_PROMPT_CHARS, MODES, ChatError, is_abliterated
from ..chat_history import VALID_AGENTS
from ..local_harness import ToolError, run_harness_stream
from ..models import CHAT_MAX_IMAGES, ModelError, chat_payload, validate_image_data_url
from ..sandbox import is_local_opencode_model
from ..task_models import opencode_groups, readable_model_name
from .deps import CTX, AppContext

router = APIRouter()

_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}

_AGENT_LABELS = {
    "gemini": "Gemini",
    "claude-code": "Claude Code",
    "codex": "Codex",
}


def _agent_error(ctx: AppContext, exc: ChatError, status: int = 409) -> HTTPException:
    text = ctx.t.get(exc.key, exc.key)
    return HTTPException(status, f"{text} {exc.detail}".strip())


def _get_chat(ctx: AppContext, chat_id: str):
    chat = ctx.agent_chats.get(chat_id)
    if chat is None:
        raise HTTPException(404, ctx.t["ag_closed"])
    return chat


def _attach_thread(chat, thread_id: str) -> None:
    chat._thread_id = thread_id
    chat._persisted_seq = 0
    chat._persist_lock = threading.Lock()


def _chat_payload(chat) -> dict:
    info = dict(chat.info())
    tid = getattr(chat, "_thread_id", "")
    if tid:
        info["thread_id"] = tid
    return info


def _sync_agent_thread(ctx: AppContext, chat, *, force_partial: bool = False) -> None:
    """Persist completed assistant turns from `chat.events` into SQLite once."""
    tid = getattr(chat, "_thread_id", "")
    lock = getattr(chat, "_persist_lock", None)
    if not tid or lock is None:
        return
    with lock:
        start = int(getattr(chat, "_persisted_seq", 0))
        events = list(getattr(chat, "events", []))
        if start >= len(events):
            return
        # Update resumable session_id if the CLI has reported one (Claude Code / Codex).
        sid = str(getattr(chat, "session_id", "") or "")
        if sid and chat.agent in ("claude-code", "codex"):
            ctx.chat_history.update_session(tid, sid)

        buf_text: list[str] = []
        buf_thought: list[str] = []
        # Latest state of each tool call in the turn (events repeat per update).
        buf_tools: dict[str, dict] = {}
        last_persisted = start

        for idx in range(start, len(events)):
            ev = events[idx]
            kind = ev.get("type")
            if kind == "user":
                # User turns are already saved on POST /prompt; advance marker.
                last_persisted = idx + 1
            elif kind == "text":
                buf_text.append(str(ev.get("text") or ""))
            elif kind == "thought":
                buf_thought.append(str(ev.get("text") or ""))
            elif kind == "tool" and ev.get("id"):
                buf_tools[str(ev["id"])] = {**buf_tools.get(str(ev["id"]), {}), **ev}
            elif kind in ("done", "error", "closed"):
                content = "".join(buf_text).strip()
                thought = "".join(buf_thought).strip()
                if content or thought or buf_tools:
                    ctx.chat_history.append_message(
                        tid, "assistant", content, reasoning=thought,
                        meta=str(ev.get("reason") or "") if kind == "done" else "",
                        tools=list(buf_tools.values()),
                    )
                buf_text.clear()
                buf_thought.clear()
                buf_tools.clear()
                last_persisted = idx + 1

        if force_partial and (buf_text or buf_thought or buf_tools):
            content = "".join(buf_text).strip()
            thought = "".join(buf_thought).strip()
            if content or thought or buf_tools:
                ctx.chat_history.append_message(
                    tid, "assistant", content, reasoning=thought, tools=list(buf_tools.values()),
                )
            last_persisted = len(events)

        chat._persisted_seq = last_persisted


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
    agent: Literal["local", "opencode", "gemini", "claude-code", "codex"] | None = None,
    thread: str = "",
    folder: str = "",
    ctx: AppContext = CTX,
):
    active_thread = await asyncio.to_thread(ctx.chat_history.get_thread, thread) if thread else None
    if agent is None:
        if active_thread and active_thread["agent"] in VALID_AGENTS:
            agent = active_thread["agent"]
        else:
            agent = "local"
    if not model and active_thread and active_thread["agent"] == "local" and active_thread["model"]:
        model = active_thread["model"]

    threads = await asyncio.to_thread(ctx.chat_history.list_threads, limit=60)
    view = await asyncio.to_thread(ctx.models.overview, ctx.settings.lang)
    ids = [m["id"] for m in view["models"]]
    running = [m["id"] for m in view["models"] if m["status"] in ("running", "external")]
    selected = model if model in ids else (running[0] if running else (ids[0] if ids else ""))
    oc: dict = {"available": False, "groups": [], "folders": [], "sandbox_ready": False, "sessions": [], "model_labels": {}}
    if agent == "opencode" and ctx.find_opencode_fn():
        oc_list, folders, sandbox_ready = await asyncio.gather(
            asyncio.to_thread(ctx.opencode_models_fn),
            asyncio.to_thread(ctx.chat_folders),
            asyncio.to_thread(ctx.agent_chats.sandbox_ready),
        )
        active_families = {m["family_key"] for m in view["models"] if m["uses_gpu"]}
        oc_groups = opencode_groups(oc_list, ctx.t, active_families=active_families)
        oc = {"available": True, "groups": oc_groups,
              "model_labels": {m: readable_model_name(m) for g in oc_groups for m in g["models"]},
              "folders": folders, "sandbox_ready": sandbox_ready, "sessions": []}
    elif agent in ("gemini", "claude-code", "codex"):
        finder = {"gemini": ctx.find_agy_fn, "claude-code": ctx.find_claude_fn, "codex": ctx.find_codex_fn}[agent]
        if finder():
            cat, folders, sessions = await asyncio.gather(
                asyncio.to_thread(ctx.catalog_fn, agent),
                asyncio.to_thread(ctx.chat_folders),
                asyncio.to_thread(ctx.agent_chats.sessions, agent),
            )
            oc = {
                "available": True,
                "folders": folders,
                "groups": [{"provider": agent, "label": _AGENT_LABELS[agent], "included": False, "payg": False,
                            "models": list(cat)}] if cat else [],
                "model_labels": {m: readable_model_name(m, v.get("label", "")) for m, v in cat.items()},
                "efforts": {m: v["efforts"] for m, v in cat.items()},
                "sandbox_ready": False,
                "sessions": sessions,
            }
    local_folders = await asyncio.to_thread(ctx.chat_folders) if agent == "local" else []
    chat_js = _chat_strings(ctx.t, ctx.settings.lang, agent)
    return ctx.page(
        request, "chat.html", "chat", view=view, selected=selected, error=error[:300],
        agent=agent, oc=oc, efforts=EFFORTS, threads=threads, active_thread=active_thread,
        selected_folder=folder, local_folders=local_folders, chat_js=chat_js,
    )


def _chat_strings(t: dict[str, str], lang: str, agent: str) -> dict:
    """Single i18n payload shared by the unified /chat component (local & agents)."""
    base: dict = {
        "stop": t["chat_stop"], "send": t["chat_send"], "reasoning": t["chat_reasoning"],
        "thinking": t["chat_thinking"], "writing": t["chat_writing"], "stopped": t["chat_stopped"],
        "error": t["chat_error"], "copy": t["chat_copy"], "copied": t["chat_copied"],
        "regenerate": t["chat_regenerate"], "edit": t["chat_edit"],
        "history_empty": t["chat_history_empty"], "untitled": t["chat_untitled"], "new_chat": t["chat_new"],
        "rename": t["chat_rename"], "del": t["chat_delete"], "del_confirm": t["chat_delete_confirm"],
        "perm_title": t["ag_perm_title"], "allow": t["ag_allow"], "deny": t["ag_deny"],
        "expires": t["ag_perm_expires"], "allowed": t["ag_allowed"], "denied": t["ag_denied"],
        "timeout": t["ag_timeout"], "cancelled": t["ag_cancelled"], "expired": t["ag_expired"],
        "input": t["ag_input"], "output": t["ag_output"], "command": t["ag_command"],
        "st_pending": t["ag_st_pending"], "st_in_progress": t["ag_st_in_progress"],
        "st_completed": t["ag_st_completed"], "st_failed": t["ag_st_failed"],
        "grp_read": t["ag_grp_read"], "grp_search": t["ag_grp_search"], "grp_steps": t["ag_grp_steps"],
        "grp_read_one": t["ag_grp_read_one"], "grp_search_one": t["ag_grp_search_one"],
        "kinds": {k: t["ag_kind_" + k] for k in (
            "read", "edit", "delete", "move", "search", "execute", "think", "fetch", "utility", "other")},
        "lbl_agent": t["ag_agent"], "lbl_model": t["chat_model"], "lbl_effort": t["chat_effort"],
        "lbl_folder": t["ag_folder"], "locked": t["chat_locked"], "filter_models": t["chat_filter_models"],
        "thinking_s": t["chat_thinking_s"], "thought_for": t["chat_thought_for"],
        "perm_keys": t["ag_perm_keys"], "diff_unchanged": t["ag_diff_unchanged"],
        "kbd_hint": t["chat_kbd_hint"], "lang": lang,
    }
    if agent in ("opencode", "gemini", "claude-code", "codex"):
        suffix = {"opencode": "", "gemini": "_gemini", "claude-code": "_claude", "codex": "_codex"}[agent]
        base.update({
            "starting": t["ag_starting" + suffix], "fixed": t["ag_fixed"], "plan": t["ag_plan"],
            "done_reason": t["ag_done_reason"], "closed": t["ag_closed"],
            "ag_effort_missing": t["ag_effort_missing"], "ag_resumed": t["ag_resumed"],
            "full_off": t["ag_full_off"], "full_help": t["ag_full_help"],
            "full_local_only": t["ag_full_local_only"], "full_abliterated": t["ag_full_abliterated"],
            "full_badge": t["ag_full_badge"], "confirm_label": t["ag_confirm"], "full_label": t["ag_full"],
            "confirm_help": t["ag_confirm_help" + suffix],
            "lbl_mode": t["ag_mode"], "lbl_resume": t["ag_resume"],
        })
    else:
        base.update({
            "ctx_label": t["chat_ctx_label"],
            "presets": {
                "coder": t["chat_sys_coder"],
                "tutor": t["chat_sys_tutor"],
                "reviewer": t["chat_sys_reviewer"],
            },
            "off": t["chat_off"], "loading": t["chat_loading"], "busy": t["chat_busy"], "start": t["m_start"],
            "lh_off": t["lh_off"], "lh_on": t["lh_on"],
            "lh_help_tools": t["lh_help_tools"], "lh_help_safety": t["lh_help_safety"],
            "lh_warn": t["lh_warn"], "lh_warn_folder": t["lh_warn_folder"],
            "lh_warn_abliterated": t["lh_warn_abliterated"],
            "lh_running_tools": t["lh_running_tools"], "lh_waiting_perm": t["lh_waiting_perm"],
            "lh_rounds": t["lh_rounds"],
            "lbl_mode": t["lh_mode"], "lbl_preset": t["chat_preset"],
        })
    return base


# ------------------------------------------------------- Persistent threads API

@router.get("/api/chat-threads")
async def chat_threads_list(q: str = "", limit: int = Query(60, ge=1, le=200), ctx: AppContext = CTX):
    threads = await asyncio.to_thread(ctx.chat_history.list_threads, limit=limit, query=q)
    return JSONResponse({"threads": threads})


@router.post("/api/chat-threads")
async def chat_threads_create(request: Request, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    body = await _json_body(request)
    agent = str(body.get("agent") or "local")
    if agent not in VALID_AGENTS:
        raise HTTPException(400, "Unknown agent")
    thread = await asyncio.to_thread(
        ctx.chat_history.create_thread,
        agent=agent,
        model=str(body.get("model") or ""),
        effort=str(body.get("effort") or ""),
        folder_key=str(body.get("folder_key") or ""),
        folder_label=str(body.get("folder_label") or ""),
        mode=str(body.get("mode") or "confirm"),
        session_id=str(body.get("session_id") or ""),
        title=str(body.get("title") or ""),
    )
    return JSONResponse(thread, status_code=201)


@router.get("/api/chat-threads/{thread_id}")
async def chat_threads_get(thread_id: str, ctx: AppContext = CTX):
    thread = await asyncio.to_thread(ctx.chat_history.get_thread, thread_id)
    if thread is None:
        raise HTTPException(404, "Unknown thread")
    return JSONResponse(thread)


@router.post("/api/chat-threads/{thread_id}/messages")
async def chat_threads_messages(request: Request, thread_id: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    body = await _json_body(request)
    if isinstance(body.get("replace"), list):
        updated = await asyncio.to_thread(
            ctx.chat_history.replace_messages,
            thread_id,
            body["replace"],
            model=str(body["model"]) if "model" in body else None,
            effort=str(body["effort"]) if "effort" in body else None,
        )
        if updated is None:
            raise HTTPException(404, "Unknown thread")
        return JSONResponse(updated)
    role = str(body.get("role") or "")
    content = str(body.get("content") or "")
    reasoning = str(body.get("reasoning") or "")
    meta = str(body.get("meta") or "")
    tools = body.get("tools") if isinstance(body.get("tools"), list) else None
    if role not in ("user", "assistant", "system") or (
        not content.strip() and not reasoning.strip() and not tools
    ):
        raise HTTPException(400, "Valid role and content are required.")
    msg = await asyncio.to_thread(
        ctx.chat_history.append_message, thread_id, role, content,
        reasoning=reasoning, meta=meta, tools=tools,
    )
    if msg is None:
        raise HTTPException(404, "Unknown thread")
    return JSONResponse(msg, status_code=201)


@router.patch("/api/chat-threads/{thread_id}")
async def chat_threads_rename(request: Request, thread_id: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    body = await _json_body(request)
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "Title is required.")
    ok = await asyncio.to_thread(ctx.chat_history.rename_thread, thread_id, title)
    if not ok:
        raise HTTPException(404, "Unknown thread")
    return JSONResponse({"ok": True, "title": title[:120]})


@router.delete("/api/chat-threads/{thread_id}")
async def chat_threads_delete(request: Request, thread_id: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    ok = await asyncio.to_thread(ctx.chat_history.delete_thread, thread_id)
    if not ok:
        raise HTTPException(404, "Unknown thread")
    return JSONResponse({"ok": True})


@router.get("/api/agents/sessions")
async def agents_sessions(
    agent: Literal["claude-code", "codex"],
    ctx: AppContext = CTX,
):
    sessions = await asyncio.to_thread(ctx.agent_chats.sessions, agent)
    return JSONResponse({"sessions": sessions})


@router.post("/api/agents")
async def agents_create(request: Request, ctx: AppContext = CTX):
    """Start a conversation. Agent, folder, model, effort and permission
    mode are checked and fixed here, on the server, for its whole life."""
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    body = await _json_body(request)
    agent, model = body.get("agent", "opencode"), str(body.get("model") or "")
    effort, mode = str(body.get("effort") or "default"), str(body.get("mode") or "confirm")
    resume = str(body.get("resume") or "").strip()
    thread_id = str(body.get("thread_id") or "").strip()
    if agent not in CHAT_AGENTS:
        raise HTTPException(400, "Unknown agent")
    if mode not in MODES or effort not in EFFORTS:
        raise HTTPException(400, "Unknown permission mode or reasoning effort")
    if agent in ("gemini", "claude-code", "codex"):
        err_key = {"gemini": "ag_err_model_gemini", "claude-code": "ag_err_model_claude", "codex": "ag_err_model_codex"}[agent]
        if model not in await asyncio.to_thread(ctx.catalog_fn, agent):
            raise HTTPException(400, ctx.t[err_key])
    elif model not in await asyncio.to_thread(ctx.opencode_models_fn):
        raise HTTPException(400, ctx.t["ag_err_model"])
    if resume:
        if agent not in ("claude-code", "codex"):
            raise HTTPException(400, ctx.t["ag_err_resume"])
        saved = await asyncio.to_thread(ctx.agent_chats.session, agent, resume)
        if saved is None:
            raise HTTPException(400, ctx.t["ag_err_resume"])
    local_models: list[dict] | None = None
    if mode == "full":
        # Abliterated models never get All permissions, even inside Docker.
        view = await asyncio.to_thread(ctx.models.overview, ctx.settings.lang)
        local_models = view["models"]
        if is_abliterated(model, local_models):
            raise HTTPException(403, ctx.t["ag_full_abliterated"])
        if agent != "opencode" or not await asyncio.to_thread(ctx.agent_chats.sandbox_ready):
            raise HTTPException(403, ctx.t["ag_full_off"])
        if not is_local_opencode_model(model):
            raise HTTPException(403, ctx.t["ag_full_local_only"])
    folders = {f["key"]: f for f in await asyncio.to_thread(ctx.chat_folders)}
    folder = folders.get(str(body.get("folder") or ""))
    if folder is None:
        raise HTTPException(400, ctx.t["ag_err_folder"])
    path = folder["path"]
    if folder["key"] == "scratch":
        path.mkdir(parents=True, exist_ok=True)
    # The canonical folder (junctions and links resolved) is what the agent gets.
    path = Path(os.path.realpath(path))
    try:
        chat = await asyncio.to_thread(
            ctx.agent_chats.create, path, folder["label"], model, effort, mode,
            agent, local_models=local_models, folder_key=folder["key"], resume_id=resume,
        )
    except ChatError as exc:
        status = 403 if exc.key in (
            "ag_full_off", "ag_full_abliterated", "ag_full_local_only", "ag_full_hardlink"
        ) else (502 if exc.key == "ag_err_start" else 409)
        raise _agent_error(ctx, exc, status) from exc

    existing = await asyncio.to_thread(ctx.chat_history.get_thread, thread_id) if thread_id else None
    if existing and existing["agent"] == agent:
        tid = existing["id"]
    else:
        created = await asyncio.to_thread(
            ctx.chat_history.create_thread,
            agent=agent,
            model=model,
            effort=effort,
            folder_key=folder["key"],
            folder_label=folder["label"],
            mode=mode,
            session_id=resume,
        )
        tid = created["id"]
    _attach_thread(chat, tid)
    return JSONResponse(_chat_payload(chat))


@router.get("/api/agents/{chat_id}")
async def agents_info(chat_id: str, ctx: AppContext = CTX):
    return JSONResponse(_chat_payload(_get_chat(ctx, chat_id)))


def _save_chat_images(data_dir: Path, chat_id: str, images: list) -> list[Path]:
    if len(images) > CHAT_MAX_IMAGES:
        raise ValueError(f"at most {CHAT_MAX_IMAGES} images per message")
    validated = [validate_image_data_url(url) for url in images]
    upload_dir = Path(data_dir) / "chat-uploads" / chat_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(upload_dir.iterdir()))
    saved: list[Path] = []
    for idx, (mime, raw, _canon) in enumerate(validated, start=existing + 1):
        ext = _MIME_EXT.get(mime, ".png")
        target = upload_dir / f"img_{idx:02d}{ext}"
        target.write_bytes(raw)
        saved.append(target)
    return saved


@router.post("/api/agents/{chat_id}/prompt")
async def agents_prompt(request: Request, chat_id: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    chat = _get_chat(ctx, chat_id)
    body = await _json_body(request)
    text = body.get("text")
    raw_images = body.get("images") or []
    if not isinstance(raw_images, list):
        raise HTTPException(400, "images must be a list of base64 data URLs")
    if not isinstance(text, str) or (not text.strip() and not raw_images) or len(text) > MAX_PROMPT_CHARS:
        raise HTTPException(400, f"Write a message of up to {MAX_PROMPT_CHARS} characters.")
    clean = text.strip()
    attached_paths: list[Path] = []
    if raw_images:
        try:
            attached_paths = await asyncio.to_thread(_save_chat_images, ctx.settings.data_dir, chat.id, raw_images)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    prompt_text = clean
    if attached_paths:
        refs = "\n".join(f"[Attached image: {p}]" for p in attached_paths)
        prompt_text = f"{clean}\n\n{refs}".strip()
    try:
        chat.prompt(prompt_text)
    except ChatError as exc:
        raise _agent_error(ctx, exc) from exc
    tid = getattr(chat, "_thread_id", "")
    if tid:
        saved_note = clean or f"[{len(attached_paths)} image(s)]"
        await asyncio.to_thread(ctx.chat_history.append_message, tid, "user", saved_note)
    return JSONResponse(_chat_payload(chat), status_code=202)


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
                if event.get("type") in ("done", "error", "closed"):
                    await asyncio.to_thread(_sync_agent_thread, ctx, chat)
            position += len(events)
            if not more and position >= len(chat.events):
                await asyncio.to_thread(_sync_agent_thread, ctx, chat)
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
    chat = _get_chat(ctx, chat_id)
    await asyncio.to_thread(chat.cancel)
    await asyncio.to_thread(_sync_agent_thread, ctx, chat, force_partial=True)
    return JSONResponse({"ok": True})


@router.post("/api/agents/{chat_id}/close")
async def agents_close(request: Request, chat_id: str, ctx: AppContext = CTX):
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    chat = _get_chat(ctx, chat_id)
    await asyncio.to_thread(_sync_agent_thread, ctx, chat, force_partial=True)
    await asyncio.to_thread(ctx.agent_chats.close, chat_id)
    shutil.rmtree(ctx.settings.data_dir / "chat-uploads" / chat.id, ignore_errors=True)
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


@router.post("/api/chat/{model_id}/harness")
async def chat_harness_api(request: Request, model_id: str, ctx: AppContext = CTX):
    """Run an opt-in tool-calling harness turn against a local llama-server model.

    Streams reasoning, answer text, tool calls, and interactive Allow/Deny
    permission requests over SSE. Workspace tools are enabled only when `folder`
    matches an allowlisted key from `ctx.chat_folders()`.
    """
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    body = await _json_body(request)
    try:
        payload = chat_payload(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        port, model_name, abliterated = await asyncio.to_thread(ctx.models.chat_model_info, model_id)
    except ModelError as exc:
        raise HTTPException(404 if exc.key == "m_err_unknown" else 409, exc.text(ctx.t)) from exc

    folder_key = str(body.get("folder") or "").strip()
    workspace: Path | None = None
    folder_label = ""
    if folder_key:
        folders = {f["key"]: f for f in await asyncio.to_thread(ctx.chat_folders)}
        folder = folders.get(folder_key)
        if folder is None:
            raise HTTPException(400, ctx.t["ag_err_folder"])
        path = folder["path"]
        if folder["key"] == "scratch":
            path.mkdir(parents=True, exist_ok=True)
        workspace = Path(os.path.realpath(path))
        folder_label = folder["label"]

    try:
        run = ctx.local_harness.create(
            model_id=model_id,
            model_name=model_name,
            abliterated=abliterated,
            workspace=workspace,
            folder_label=folder_label,
        )
    except ToolError as exc:
        raise HTTPException(429, str(exc)) from exc

    stream = run_harness_stream(
        port=port,
        payload=payload,
        run=run,
        manager=ctx.local_harness,
        is_disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/chat-harness/{run_id}/permission")
async def chat_harness_permission(request: Request, run_id: str, ctx: AppContext = CTX):
    """Allow or deny one pending tool permission for a local harness run (one-shot)."""
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    run = ctx.local_harness.get(run_id)
    if run is None:
        raise HTTPException(404, ctx.t["ag_expired"])
    body = await _json_body(request)
    allow = body.get("allow")
    if not isinstance(allow, bool):
        raise HTTPException(400, "allow must be true or false")
    if not await asyncio.to_thread(run.answer, str(body.get("id") or ""), allow):
        raise HTTPException(404, ctx.t["ag_expired"])
    return JSONResponse({"ok": True})


@router.post("/api/chat-harness/{run_id}/cancel")
async def chat_harness_cancel(request: Request, run_id: str, ctx: AppContext = CTX):
    """Cancel an active local harness run (unblocks permissions and stops any command)."""
    ctx.check_post(request, request.headers.get("x-baton-csrf", ""))
    run = ctx.local_harness.get(run_id)
    if run is not None:
        await asyncio.to_thread(run.cancel)
    return JSONResponse({"ok": True})

