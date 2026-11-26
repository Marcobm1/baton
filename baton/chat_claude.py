"""Claude Code backend for Agents chat (bidirectional stream-json over stdio).

Uses Claude Code's verified interactive permission protocol (checked live on
claude.exe 2.1.331):
  claude -p --input-format stream-json --output-format stream-json --verbose
    --permission-mode manual --permission-prompt-tool stdio
    --strict-mcp-config --setting-sources local
    --disallowedTools AskUserQuestion [--model ...] [--effort ...] [--resume ...]

Security invariants:
  - Baton passes `agent_env()` (no BATON_*, GITHUB_TOKEN, ANTHROPIC_API_KEY).
  - Fail closed if Claude reports a bypass permission mode on initialize.
  - Every `can_use_tool` permission request is allow-once (`behavior: "allow"`
    with `updatedInput`, never `updatedPermissions`), single-use, and expires
    to `deny` after `permission_timeout`.
  - Never stores, logs, or forwards `account` or `credential_expires_at`.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent_chat import (
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
from .ping import _NO_WINDOW, find_claude
from .proc import attach_job, close_job, kill_tree

_BYPASS_MODES = frozenset({"bypassPermissions", "dontAsk"})


def claude_chat_command(
    model: str = "",
    effort: str = "default",
    resume_id: str = "",
    *,
    binary: str | None = None,
) -> list[str] | None:
    """Build the argv for an interactive Confirm-mode Claude Code session."""
    exe = binary if binary is not None else find_claude()
    if not exe:
        return None
    cmd = [
        exe, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "manual",
        "--permission-prompt-tool", "stdio",
        "--strict-mcp-config",
        "--setting-sources", "local",
        "--disallowedTools", "AskUserQuestion",
    ]
    if model:
        cmd.extend(["--model", model])
    if effort and effort != "default":
        cmd.extend(["--effort", effort])
    if resume_id:
        cmd.extend(["--resume", resume_id])
    return cmd


def _tool_kind(name: str) -> str:
    low = (name or "").lower()
    if low in ("read", "glob", "grep", "ls", "list"):
        return "read"
    if low in ("edit", "write", "multiedit", "notebookedit"):
        return "edit"
    if low in ("bash", "powershell", "shell"):
        return "execute"
    return "other"


def _tool_locations(inp: dict[str, Any], blocked_path: str = "") -> list[str]:
    locs: list[str] = []
    for candidate in (blocked_path, inp.get("file_path"), inp.get("path"), inp.get("notebook_path")):
        if isinstance(candidate, str) and candidate and candidate not in locs:
            locs.append(candidate)
    return locs[:20]


def _tool_diffs(name: str, inp: dict[str, Any]) -> list[dict]:
    low = (name or "").lower()
    path = str(inp.get("file_path") or inp.get("path") or "")
    if low == "edit" and isinstance(inp.get("new_string"), str):
        return [{
            "path": path,
            "old": _clip(str(inp.get("old_string") or ""), 100000),
            "new": _clip(inp["new_string"], 100000),
        }]
    if low == "write" and isinstance(inp.get("content"), str):
        return [{"path": path, "old": "", "new": _clip(inp["content"], 100000)}]
    return []


class _ClaudeProcess:
    """Line-delimited JSON transport over Claude Code's stdin/stdout."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str],
                 on_message: Callable[[dict], None], on_exit: Callable[[str], None]):
        group = {} if os.name == "nt" else {"start_new_session": True}
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(cwd), env=env, **group, **_NO_WINDOW,
        )
        attach_job(self.proc)
        self._on_message, self._on_exit = on_message, on_exit
        self._lock = threading.Lock()
        self._write = threading.Lock()
        self._waiting: dict[str, dict[str, Any]] = {}
        self.stderr: deque[str] = deque(maxlen=20)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "control_response":
                resp = msg.get("response") if isinstance(msg.get("response"), dict) else {}
                rid = str(resp.get("request_id") or "")
                with self._lock:
                    waiter = self._waiting.get(rid)
                if waiter:
                    waiter["reply"] = resp
                    waiter["event"].set()
                    continue
            try:
                self._on_message(msg)
            except Exception as exc:
                self.stderr.append(f"baton: {exc!r}")
        self.proc.wait()
        close_job(self.proc)
        with self._lock:
            for waiter in self._waiting.values():
                waiter["event"].set()
        self._on_exit(" ".join(self.stderr)[-1500:])

    def _read_stderr(self) -> None:
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.stderr.append(line[:500])

    def send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def control_request(self, subtype: str, extra: dict | None = None,
                        timeout: float = START_TIMEOUT) -> dict:
        rid = f"baton_{secrets.token_hex(6)}"
        waiter: dict[str, Any] = {"event": threading.Event(), "reply": None}
        with self._lock:
            self._waiting[rid] = waiter
        try:
            body = {"subtype": subtype, **(extra or {})}
            self.send({"type": "control_request", "request_id": rid, "request": body})
            if not waiter["event"].wait(timeout):
                raise AcpError(f"Claude Code did not answer {subtype} in {timeout:.0f} s.")
        finally:
            with self._lock:
                self._waiting.pop(rid, None)
        reply = waiter["reply"]
        if reply is None:
            raise AcpError("Claude Code stopped. " + " ".join(self.stderr)[-500:])
        if reply.get("subtype") != "success":
            raise AcpError(str(reply.get("error") or reply.get("message") or "Claude Code control error"))
        return reply.get("response") if isinstance(reply.get("response"), dict) else {}

    def close(self) -> None:
        if self.proc.poll() is None:
            kill_tree(self.proc)
        else:
            close_job(self.proc)


@dataclass
class ClaudeChat:
    """One interactive Claude Code conversation over stream-json stdio."""

    id: str
    folder: Path
    folder_label: str
    model: str
    effort: str
    mode: str = "confirm"
    agent: str = "claude-code"
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
        self._perm_inputs: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, dict] = {}
        self.client: _ClaudeProcess | None = None
        self._turn_done = threading.Event()
        self._saw_text = False
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
        self.client = _ClaudeProcess(command, self.folder, env, self._on_message, self._on_exit)
        resp = self.client.control_request("initialize", timeout=START_TIMEOUT)
        # Never read/store `account` from resp. Fail closed if bypass mode is active.
        pmode = str(resp.get("current_permission_mode") or "")
        if pmode in _BYPASS_MODES:
            raise AcpError(f"Claude Code kept unsafe permission mode '{pmode}'.")
        if self.resume_id:
            self.resumed = True
            self._record_session(self.resume_id)
            self.emit("notice", key="ag_resumed", session=self.resume_id[:12])

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
        self._saw_text = False
        self._cancelled = False
        try:
            self.client.send({
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
                "parent_tool_use_id": None,
                "session_id": self.session_id or "",
            })
            if not self._turn_done.wait(self.turn_timeout):
                if self.alive:
                    self.cancel()
                    self.emit("error", text=f"Claude Code did not finish the turn in {self.turn_timeout:.0f} s.")
        finally:
            self._resolve_all("cancelled")
            with self.cond:
                self.busy = False
                self.last_active = time.time()
                self.cond.notify_all()

    def cancel(self) -> None:
        if self.client and self.busy:
            self._cancelled = True
            self._resolve_all("cancelled")
            self.client.send({
                "type": "control_request",
                "request_id": f"baton_int_{secrets.token_hex(4)}",
                "request": {"subtype": "interrupt"},
            })

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
            self.emit("error", text=("Claude Code stopped. " + stderr).strip()[:1500])
            self.emit("closed")

    # ---------------------------------------------------- Claude -> Baton
    def _on_message(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "system":
            sub = msg.get("subtype")
            if sub == "init":
                pmode = str(msg.get("permissionMode") or "")
                if pmode in _BYPASS_MODES:
                    self.emit("error", text=f"Claude Code entered unsafe permission mode '{pmode}'.")
                    self.close()
                    return
                self._record_session(str(msg.get("session_id") or ""))
        elif mtype == "assistant":
            message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "thinking":
                    thought = str(block.get("thinking") or block.get("text") or "")
                    if thought:
                        self.emit("thought", text=thought)
                elif btype == "text":
                    txt = str(block.get("text") or "")
                    if txt:
                        self._saw_text = True
                        self.emit("text", text=txt)
                elif btype == "tool_use":
                    self._tool_use(block)
        elif mtype == "user":
            message = msg.get("message") if isinstance(msg.get("message"), dict) else {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._tool_result(block)
        elif mtype == "control_request":
            self._control_request(msg)
        elif mtype == "result":
            # Never store or emit `credential_expires_at` or `account`.
            self._record_session(str(msg.get("session_id") or self.session_id))
            is_err = bool(msg.get("is_error")) or msg.get("subtype") not in (None, "success")
            res_text = msg.get("result")
            if is_err:
                detail = str(res_text or msg.get("subtype") or "error")[:1000]
                if self.alive:
                    self.emit("error", text=detail)
            elif not self._saw_text and isinstance(res_text, str) and res_text:
                self._saw_text = True
                self.emit("text", text=res_text)
            self.emit("done", reason="cancelled" if self._cancelled else "end_turn")
            self._turn_done.set()

    def _tool_use(self, block: dict) -> None:
        tid = str(block.get("id") or secrets.token_hex(4))
        name = str(block.get("name") or "tool")
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        title = str(inp.get("description") or inp.get("command") or inp.get("file_path") or name)[:300]
        with self.cond:
            t = self.tools.setdefault(tid, {
                "id": tid, "name": name[:300], "title": title, "kind": _tool_kind(name),
                "status": "in_progress", "input": _clip(_as_text(inp), 5000) if inp else "",
                "output": "", "locations": _tool_locations(inp), "diffs": _tool_diffs(name, inp),
            })
            t["status"] = "in_progress"
            snap = dict(t)
        self.emit("tool", **snap)

    def _tool_result(self, block: dict) -> None:
        tid = str(block.get("tool_use_id") or "")
        if not tid:
            return
        content = block.get("content")
        if isinstance(content, list):
            parts = [str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"]
            out = "\n".join(parts)
        else:
            out = _as_text(content)
        status = "failed" if block.get("is_error") else "completed"
        with self.cond:
            t = self.tools.setdefault(tid, {
                "id": tid, "name": "tool", "title": "tool", "kind": "other",
                "status": status, "input": "", "output": "", "locations": [], "diffs": [],
            })
            t["status"] = status
            if out:
                t["output"] = _clip(out)
            snap = dict(t)
        self.emit("tool", **snap)

    def _control_request(self, msg: dict) -> None:
        rid = str(msg.get("request_id") or "")
        req = msg.get("request") if isinstance(msg.get("request"), dict) else {}
        if req.get("subtype") != "can_use_tool" or not rid:
            if rid and self.client:
                self.client.send({
                    "type": "control_response",
                    "response": {"subtype": "error", "request_id": rid, "error": "Unsupported control request"},
                })
            return
        if not self.alive:
            self._send_perm_response(rid, False, {}, "Conversation closed.")
            return
        tool_name = str(req.get("tool_name") or "tool")
        display = str(req.get("display_name") or tool_name)
        tool_input = req.get("input") if isinstance(req.get("input"), dict) else {}
        blocked_path = str(req.get("blocked_path") or "")
        tid = str(req.get("tool_use_id") or "")
        desc = str(req.get("description") or "")
        command = str(tool_input.get("command") or "") if isinstance(tool_input.get("command"), str) else ""
        title = (desc or command or blocked_path or display)[:300]
        locs = _tool_locations(tool_input, blocked_path)
        diffs = _tool_diffs(tool_name, tool_input)

        perm = Permission(
            id=secrets.token_urlsafe(16), rpc_id=rid, allow="allow", deny="deny",
            expires=time.time() + self.permission_timeout,
        )
        perm.timer = threading.Timer(self.permission_timeout, self.answer, args=(perm.id, False, "timeout"))
        perm.timer.daemon = True
        with self.cond:
            self.pending[perm.id] = perm
            self._perm_inputs[perm.id] = dict(tool_input)
        perm.timer.start()

        self.emit(
            "permission",
            id=perm.id,
            tool=tid,
            title=f"{display}: {title}" if title != display else display,
            kind=_tool_kind(tool_name),
            command=command,
            input=_clip(_as_text(tool_input), 5000) if tool_input else "",
            locations=locs,
            diffs=diffs,
            can_allow=True,
            expires=perm.expires,
        )

    def _send_perm_response(self, rid: str, allow: bool, orig_input: dict[str, Any], reason: str) -> None:
        if not self.client:
            return
        if allow:
            # Allow once only: echo updatedInput, NEVER grant updatedPermissions.
            payload: dict[str, Any] = {"behavior": "allow", "updatedInput": orig_input}
        else:
            payload = {"behavior": "deny", "message": reason or "Denied by user."}
        self.client.send({
            "type": "control_response",
            "response": {"subtype": "success", "request_id": rid, "response": payload},
        })

    def answer(self, permission_id: str, allow: bool, why: str = "") -> bool:
        with self.cond:
            perm = self.pending.pop(permission_id, None)
            orig_input = self._perm_inputs.pop(permission_id, {})
        if perm is None:
            return False
        if perm.timer:
            perm.timer.cancel()
        if why == "cancelled":
            self._send_perm_response(str(perm.rpc_id), False, orig_input, "Cancelled by user.")
        elif allow:
            why = "allowed"
            self._send_perm_response(str(perm.rpc_id), True, orig_input, "")
        else:
            why = why or "denied"
            msg = "Timed out waiting for approval." if why == "timeout" else "Denied by user."
            self._send_perm_response(str(perm.rpc_id), False, orig_input, msg)
        self.last_active = time.time()
        self.emit("permission_done", id=permission_id, outcome=why)
        return True

    def _resolve_all(self, why: str) -> None:
        with self.cond:
            ids = list(self.pending)
        for pid in ids:
            self.answer(pid, False, why)
