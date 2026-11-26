"""Codex backend for Agents chat (`codex app-server`, JSON-RPC 2.0 over stdio).

Uses Codex's verified app-server protocol (checked live on codex-cli 0.171.0):
  - Handshake: `initialize` -> notification `initialized`
  - Session: `thread/start` or `thread/resume` (`excludeTurns: true`) with
    `sandbox: "read-only"` and `approvalPolicy: "untrusted"`
  - Turn: `turn/start` (optional `effort`) and `turn/interrupt` on cancel
  - Approvals: `item/commandExecution/requestApproval`,
    `item/fileChange/requestApproval`, `item/permissions/requestApproval`
    (plus legacy `execCommandApproval`, `applyPatchApproval`) -> answered with
    `{"decision": "accept"}` (allow once, never `acceptForSession`) or
    `{"decision": "decline" | "cancel"}`.

Security invariants:
  - Always uses native `codex.exe` via `find_codex()` and `agent_env()`.
  - Never stores, logs, or forwards `account/*` or `planType` notifications.
  - Note on Windows: an approved command executes with write access as the
    Windows user even when `sandbox: "read-only"` is set.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .agent_chat import (
    AcpClient,
    AcpError,
    ChatError,
    MAX_EVENTS,
    PERMISSION_TIMEOUT,
    START_TIMEOUT,
    TURN_TIMEOUT,
    Permission,
    _as_text,
    _clip,
)
from .ping import find_codex

_APPROVAL_METHODS = frozenset({
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
})


def codex_chat_command(*, binary: str | None = None) -> list[str] | None:
    """Build the argv for an interactive Confirm-mode Codex `app-server`."""
    exe = binary if binary is not None else find_codex()
    return [exe, "app-server"] if exe else None


def _command_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(str(x) for x in value)
    return ""


def _codex_tool_kind(item_type: str) -> str:
    if item_type == "commandExecution":
        return "execute"
    if item_type == "fileChange":
        return "edit"
    if item_type in ("fileRead", "webSearch"):
        return "read"
    return "other"


def _codex_tool_status(raw_status: str, exit_code: Any = None) -> str:
    low = (raw_status or "").lower()
    if low in ("declined", "failed", "error", "cancelled", "canceled"):
        return "failed"
    if low in ("completed", "done", "succeeded"):
        if isinstance(exit_code, int) and exit_code != 0:
            return "failed"
        return "completed"
    return "in_progress"


def _codex_locations(obj: dict[str, Any]) -> list[str]:
    locs: list[str] = []
    for key in ("path", "file", "file_path"):
        val = obj.get(key)
        if isinstance(val, str) and val and val not in locs:
            locs.append(val)
    for key in ("changes", "files"):
        items = obj.get(key)
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, str) and entry not in locs:
                    locs.append(entry)
                elif isinstance(entry, dict):
                    p = entry.get("path") or entry.get("file")
                    if isinstance(p, str) and p and p not in locs:
                        locs.append(p)
        elif isinstance(items, dict):
            for p in items:
                if isinstance(p, str) and p and p not in locs:
                    locs.append(p)
    return locs[:20]


@dataclass
class CodexChat:
    """One interactive Codex conversation over `codex app-server` JSON-RPC 2.0."""

    id: str
    folder: Path
    folder_label: str
    model: str
    effort: str
    mode: str = "confirm"
    agent: str = "codex"
    folder_key: str = ""
    resume_id: str = ""
    on_session: Callable[[str, str], None] | None = None
    created: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    busy: bool = False
    alive: bool = True
    session_id: str = ""
    resumed: bool = False
    isolated: bool = False
    permission_timeout: float = PERMISSION_TIMEOUT
    turn_timeout: float = TURN_TIMEOUT

    def __post_init__(self) -> None:
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.pending: dict[str, Permission] = {}
        self.tools: dict[str, dict] = {}
        self.client: AcpClient | None = None
        self._turn_done = threading.Event()
        self._turn_id = ""
        self._saw_delta = False
        self._cancelled = False
        self._title = ""

    # ------------------------------------------------------------ events
    def emit(self, event_type: str, /, **data) -> None:
        with self.cond:
            if len(self.events) < MAX_EVENTS:
                self.events.append({"seq": len(self.events), "type": event_type, **data})
            self.cond.notify_all()

    def wait(self, after: int, timeout: float) -> tuple[list[dict], bool]:
        with self.cond:
            if len(self.events) <= after and self.busy and self.alive:
                self.cond.wait(timeout)
            return self.events[after:], self.busy and self.alive

    def info(self) -> dict:
        return {
            "id": self.id, "agent": self.agent, "model": self.model, "effort": self.effort,
            "mode": self.mode, "folder": self.folder_label, "busy": self.busy,
            "alive": self.alive, "isolated": self.isolated, "session_id": self.session_id,
            "resumed": self.resumed, "events": len(self.events),
        }

    def _record_session(self, sid: str) -> None:
        if not sid:
            return
        self.session_id = sid
        if self.on_session:
            try:
                self.on_session(sid, self._title)
            except Exception:
                pass

    # ------------------------------------------------------------- start
    def start(self, command: list[str], env: dict[str, str]) -> None:
        self.client = AcpClient(command, self.folder, env, self._on_message, self._on_exit)
        req = self.client.request
        req("initialize", {"clientInfo": {"name": "baton", "version": __version__}}, START_TIMEOUT)
        self.client.notify("initialized", {})
        if self.resume_id:
            res = req("thread/resume", {
                "threadId": self.resume_id,
                "cwd": str(self.folder),
                "sandbox": "read-only",
                "approvalPolicy": "untrusted",
                "excludeTurns": True,
            }, START_TIMEOUT)
            self.resumed = True
        else:
            params: dict[str, Any] = {
                "cwd": str(self.folder),
                "sandbox": "read-only",
                "approvalPolicy": "untrusted",
            }
            if self.model:
                params["model"] = self.model
            res = req("thread/start", params, START_TIMEOUT)
        thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
        sid = str(thread.get("id") or res.get("threadId") or self.resume_id or "")
        if not sid:
            raise AcpError("Codex did not open a thread.")
        self._record_session(sid)
        if self.resumed:
            self.emit("notice", key="ag_resumed", session=sid[:12])

    # -------------------------------------------------------------- turns
    def prompt(self, text: str) -> None:
        with self.cond:
            if not self.alive:
                raise ChatError("ag_err_closed")
            if self.busy:
                raise ChatError("ag_err_busy")
            self.busy = True
            self.last_active = time.time()
            if not self._title:
                self._title = " ".join(text.split())[:100]
        self.emit("user", text=text)
        threading.Thread(target=self._turn, args=(text,), daemon=True).start()

    def _turn(self, text: str) -> None:
        self._turn_done.clear()
        self._saw_delta = False
        self._cancelled = False
        self._turn_id = ""
        try:
            params: dict[str, Any] = {
                "threadId": self.session_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": "untrusted",
                "cwd": str(self.folder),
            }
            if self.effort and self.effort != "default":
                params["effort"] = self.effort
            res = self.client.request("turn/start", params, START_TIMEOUT)
            turn = res.get("turn") if isinstance(res.get("turn"), dict) else {}
            self._turn_id = str(turn.get("id") or res.get("turnId") or "")
            if not self._turn_done.wait(self.turn_timeout):
                if self.alive:
                    self.cancel()
                    self.emit("error", text=f"Codex did not finish the turn in {self.turn_timeout:.0f} s.")
        except AcpError as exc:
            if self.alive:
                self.emit("error", text=str(exc)[:1000])
        finally:
            self._resolve_all("cancelled")
            self._record_session(self.session_id)
            with self.cond:
                self.busy = False
                self.last_active = time.time()
                self.cond.notify_all()

    def cancel(self) -> None:
        if self.client and self.busy:
            self._cancelled = True
            self._resolve_all("cancelled")
            if self._turn_id:
                try:
                    self.client.notify("turn/interrupt", {
                        "threadId": self.session_id,
                        "turnId": self._turn_id,
                    })
                except Exception:
                    pass

    def close(self) -> None:
        with self.cond:
            was = self.alive
            self.alive = False
            self.cond.notify_all()
        self._turn_done.set()
        self._resolve_all("cancelled")
        if self.client:
            self.client.close()
        if was:
            self.emit("closed")

    def _on_exit(self, stderr: str) -> None:
        with self.cond:
            was = self.alive
            self.alive = False
            self.cond.notify_all()
        self._turn_done.set()
        self._resolve_all("cancelled")
        if was:
            self.emit("error", text=("Codex stopped. " + stderr).strip()[:1500])
            self.emit("closed")

    # ----------------------------------------------------- Codex -> Baton
    def _on_message(self, msg: dict) -> None:
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        rid = msg.get("id")

        # Never log, store, or forward account / plan notifications.
        if method.startswith("account/"):
            return

        if rid is not None:
            if method in _APPROVAL_METHODS:
                self._approval(rid, method, params)
            else:
                self.client.respond(rid, error={"code": -32601, "message": f"{method} is not offered by Baton"})
            return

        if method == "item/agentMessage/delta":
            delta = str(params.get("delta") or "")
            if delta:
                self._saw_delta = True
                self.emit("text", text=delta)
        elif method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            delta = str(params.get("delta") or "")
            if delta:
                self.emit("thought", text=delta)
        elif method in ("item/started", "item/completed"):
            self._handle_item(method, params)
        elif method == "item/commandExecution/outputDelta":
            tid = str(params.get("itemId") or "")
            delta = str(params.get("delta") or "")
            if tid and delta:
                with self.cond:
                    t = self.tools.setdefault(tid, {
                        "id": tid, "name": "command", "title": "command", "kind": "execute",
                        "status": "in_progress", "input": "", "output": "", "locations": [], "diffs": [],
                    })
                    t["output"] = _clip((t["output"] + delta), 20000)
                    snap = dict(t)
                self.emit("tool", **snap)
        elif method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            if not self._saw_delta:
                for item in turn.get("items") or []:
                    if isinstance(item, dict) and item.get("type") == "agentMessage":
                        txt = str(item.get("text") or "")
                        if txt:
                            self._saw_delta = True
                            self.emit("text", text=txt)
            status = str(turn.get("status") or "end_turn")
            if status == "failed":
                err = turn.get("error")
                emsg = err.get("message") if isinstance(err, dict) else str(err or "Codex turn failed")
                self.emit("error", text=str(emsg)[:1000])
            self.emit("done", reason="cancelled" if self._cancelled else status)
            self._turn_done.set()

    def _handle_item(self, method: str, params: dict) -> None:
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        itype = str(item.get("type") or "")
        if itype == "agentMessage":
            if method == "item/completed" and not self._saw_delta:
                txt = str(item.get("text") or "")
                if txt:
                    self._saw_delta = True
                    self.emit("text", text=txt)
            return
        if itype not in ("commandExecution", "fileChange", "mcpToolCall", "webSearch", "toolCall"):
            return
        tid = str(item.get("id") or secrets.token_hex(4))
        cmd = _command_text(item.get("command"))
        status = _codex_tool_status(
            str(item.get("status") or ("completed" if method == "item/completed" else "in_progress")),
            item.get("exitCode"),
        )
        locs = _codex_locations(item)
        title = (cmd or (locs[0] if locs else "") or itype)[:300]
        out = str(item.get("aggregatedOutput") or item.get("output") or "")
        with self.cond:
            t = self.tools.setdefault(tid, {
                "id": tid, "name": itype[:300], "title": title, "kind": _codex_tool_kind(itype),
                "status": status, "input": _clip(cmd, 5000) if cmd else "",
                "output": "", "locations": locs, "diffs": [],
            })
            t["status"] = status
            if title:
                t["title"] = title
            if locs:
                t["locations"] = locs
            if out:
                t["output"] = _clip(out)
            snap = dict(t)
        self.emit("tool", **snap)

    def _approval(self, rid: Any, method: str, params: dict) -> None:
        if not self.alive:
            self.client.respond(rid, {"decision": "cancel"})
            return
        command = _command_text(params.get("command"))
        reason = str(params.get("reason") or "")
        locs = _codex_locations(params)
        tid = str(params.get("itemId") or "")
        is_file = "fileChange" in method or "Patch" in method
        kind = "edit" if is_file else "execute"
        title = (command or reason or (locs[0] if locs else "") or method)[:300]

        perm = Permission(
            id=secrets.token_urlsafe(16), rpc_id=rid, allow="accept", deny="decline",
            expires=time.time() + self.permission_timeout,
        )
        perm.timer = threading.Timer(self.permission_timeout, self.answer, args=(perm.id, False, "timeout"))
        perm.timer.daemon = True
        with self.cond:
            self.pending[perm.id] = perm
        perm.timer.start()

        safe_input = {k: v for k, v in params.items() if k not in ("threadId", "turnId")}
        self.emit(
            "permission",
            id=perm.id,
            tool=tid,
            title=title,
            kind=kind,
            command=command,
            input=_clip(_as_text(safe_input), 5000) if safe_input else "",
            locations=locs,
            diffs=[],
            can_allow=True,
            expires=perm.expires,
        )

    def answer(self, permission_id: str, allow: bool, why: str = "") -> bool:
        with self.cond:
            perm = self.pending.pop(permission_id, None)
        if perm is None:
            return False
        if perm.timer:
            perm.timer.cancel()
        if why == "cancelled":
            decision = "cancel"
        elif allow:
            # Allow once only: NEVER acceptForSession or execpolicy amendments.
            decision, why = "accept", "allowed"
        else:
            decision, why = "decline", (why or "denied")
        if self.client:
            self.client.respond(perm.rpc_id, {"decision": decision})
        self.last_active = time.time()
        self.emit("permission_done", id=permission_id, outcome=why)
        return True

    def _resolve_all(self, why: str) -> None:
        with self.cond:
            ids = list(self.pending)
        for pid in ids:
            self.answer(pid, False, why)
