"""Agents chat: one OpenCode or Gemini (agy) process per conversation, over ACP.

ACP (Agent Client Protocol) is JSON-RPC 2.0 with one JSON message per line on
the agent's stdin and stdout. Baton is the *client*: it sends `initialize`,
`session/new` and `session/prompt`; OpenCode sends back `session/update`
notifications (text, reasoning, tool calls) and `session/request_permission`
requests that wait until Baton answers. No port is opened, and the user's text
only ever travels as a JSON string inside a protocol message, never through a
shell.

Phase 1 (docs/agents-chat.md): the security boundary is Marco's approval.
OpenCode runs with Baton's own `baton-chat` agent: reads inside the folder run
freely; every write, edit, command or access outside the folder becomes a
permission request shown in the chat. "Full permissions" is refused on the
server until the isolated container exists, and always for abliterated models.

Gemini (`agy acp`, added 2026-11-25) runs in Antigravity's own "default" mode:
every terminal command asks first, but Baton cannot add per-path rules as it
does for OpenCode, and agy edits files inside the folder without asking
(checked with agy 1.5.17). docs/agents-chat.md explains the difference.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .ping import _NO_WINDOW, agent_env, find_agy
from .proc import attach_job, close_job, kill_tree
from .runner import ALWAYS_DENIED, NO_GIT_CHANGES, _rule, find_opencode
from .usage import record_gemini

CHAT_AGENT = "baton-chat"
MODES = ("confirm", "full")
EFFORTS = ("default", "none", "low", "medium", "high", "xhigh", "max")
PERMISSION_TIMEOUT = 600          # seconds without an answer -> Deny
MAX_PROMPT_CHARS = 20000
MAX_CHATS = 4                     # agent processes open at once
IDLE_SECONDS = 1800               # an idle conversation's process is closed after this
MAX_EVENTS = 20000
START_TIMEOUT = 90


@dataclass(frozen=True)
class Profile:
    """How one agent's ACP server is set up for a conversation."""
    label: str          # name shown in messages
    mode: str           # the session mode Baton sets (fail closed if missing)
    effort_option: str  # the configOptions id of the reasoning effort


PROFILES = {
    "opencode": Profile("OpenCode", "baton-chat", "effort"),
    # agy: "default" asks before commands; `thought_level` is its effort.
    "gemini": Profile("Gemini", "default", "thought_level"),
}

# OpenCode 2.x rules: a list of {action, resource, effect}, the last match wins.
# Deny everything first, then allow only reading inside the folder; anything
# that changes something or leaves the folder ("external_directory") asks.
# `.env` files ask too, so a secret never reaches a cloud model unseen.
CONFIRM_RULES = (
    [_rule("*", "deny")]
    + [_rule(a, "allow") for a in ("read", "glob", "grep", "list")]
    + [_rule("read", "ask", p) for p in ("*.env", "*.env.*")]
    + [_rule(a, "ask") for a in ("edit", "shell", "external_directory")]
    # Same as tasks: no commits, pushes or branch changes, even if approved.
    + [_rule("shell", "deny", c) for c in NO_GIT_CHANGES]
    + ALWAYS_DENIED
)


def chat_config() -> dict:
    return {
        "$schema": "https://opencode.ai/config.json",
        "agents": {
            CHAT_AGENT: {
                "mode": "primary",
                "description": "Baton's Agents chat: reads freely, asks before any change",
                "permissions": CONFIRM_RULES,
            }
        },
    }


def chat_env(agent: str = "opencode") -> dict[str, str]:
    """The environment the agent gets: Baton's own settings and GitHub tokens
    stay out (an approved `echo %GITHUB_TOKEN%` must find nothing)."""
    env = agent_env()
    if agent == "opencode":
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(chat_config())
    return env


def opencode_command() -> list[str] | None:
    exe = find_opencode()
    return [exe, "acp"] if exe else None


def agy_command() -> list[str] | None:
    """`agy acp` without --dangerously-skip-permissions: commands ask first."""
    exe = find_agy()
    return [exe, "acp"] if exe else None


_ABLITERATED = re.compile(r"abliterat|uncensor", re.I)


def is_abliterated(model: str, local_models: list[dict]) -> bool:
    """True for an abliterated model, also behind an OpenCode provider alias.

    A `local-*` provider is only a URL to a llama-server; Baton doesn't read
    OpenCode's config (it can hold keys), so if any abliterated model from
    models.json is running, every local alias counts as abliterated.
    """
    if _ABLITERATED.search(model):
        return True
    if model.split("/", 1)[0].startswith("local-"):
        return any(m.get("status") in ("running", "external", "loading")
                   and _ABLITERATED.search(f"{m.get('id', '')} {m.get('name', '')}")
                   for m in local_models)
    return False


class ChatError(RuntimeError):
    """A problem to show the user (`key` is an i18n string, `detail` extra text)."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(f"{key}: {detail}" if detail else key)
        self.key, self.detail = key, detail


class AcpError(RuntimeError):
    pass


def _clip(text: str, limit: int = 20000) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("output"), str):
        return value["output"]
    return json.dumps(value, ensure_ascii=False, indent=1)


def _content(items: Any) -> tuple[str, list[dict]]:
    """Text and diffs from an ACP tool-call `content` list."""
    texts, diffs = [], []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "diff":
            diffs.append({"path": str(item.get("path", "")),
                          "old": _clip(item.get("oldText") or "", 100000),
                          "new": _clip(item.get("newText") or "", 100000)})
        elif item.get("type") == "content":
            inner = item.get("content") or {}
            if isinstance(inner, dict) and inner.get("type") == "text":
                texts.append(str(inner.get("text", "")))
    return _clip("\n".join(texts)), diffs[:20]


class AcpClient:
    """JSON-RPC over a child process's stdin/stdout, with threads (works with
    any asyncio loop on Windows, and keeps the web server responsive)."""

    def __init__(self, command: list[str], cwd: Path, env: dict[str, str],
                 on_message: Callable[[dict], None], on_exit: Callable[[str], None]):
        group = {} if os.name == "nt" else {"start_new_session": True}
        # A list of arguments, no shell: nothing in a message is ever parsed
        # by cmd.exe or sh.
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=str(cwd), env=env,
                                     **group, **_NO_WINDOW)
        attach_job(self.proc)
        self._on_message, self._on_exit = on_message, on_exit
        self._lock = threading.Lock()
        self._write = threading.Lock()
        self._next = 0
        self._waiting: dict[int, dict] = {}
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
            if "method" in msg:
                try:
                    self._on_message(msg)
                except Exception as exc:  # a bad message must not stop the reader
                    self.stderr.append(f"baton: {exc!r}")
            elif "id" in msg:
                with self._lock:
                    waiter = self._waiting.get(msg["id"])
                if waiter:
                    waiter["reply"] = msg
                    waiter["event"].set()
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

    def _send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass  # the process is gone; _read reports it

    def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        with self._lock:
            self._next += 1
            rid = self._next
            waiter = {"event": threading.Event(), "reply": None}
            self._waiting[rid] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if not waiter["event"].wait(timeout):
                raise AcpError(f"The agent did not answer {method} in {timeout:.0f} s.")
        finally:
            with self._lock:
                self._waiting.pop(rid, None)
        reply = waiter["reply"]
        if reply is None:
            raise AcpError("The agent stopped. " + " ".join(self.stderr)[-500:])
        if "error" in reply:
            error = reply["error"] if isinstance(reply["error"], dict) else {}
            raise AcpError(str(error.get("message") or reply["error"]))
        return reply.get("result") or {}

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid: Any, result: dict | None = None, error: dict | None = None) -> None:
        msg = {"jsonrpc": "2.0", "id": rid}
        msg.update({"error": error} if error else {"result": result or {}})
        self._send(msg)

    def close(self) -> None:
        if self.proc.poll() is None:
            kill_tree(self.proc)
        else:
            close_job(self.proc)


@dataclass
class Permission:
    id: str
    rpc_id: Any
    allow: str | None       # the agent's "allow once" option, never "always"
    deny: str | None
    expires: float
    timer: threading.Timer | None = None


@dataclass
class AgentChat:
    id: str
    folder: Path
    folder_label: str
    model: str
    effort: str
    mode: str = "confirm"
    agent: str = "opencode"
    on_usage: Callable[[str, dict], None] | None = None
    created: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    busy: bool = False
    alive: bool = True
    session_id: str = ""
    permission_timeout: float = PERMISSION_TIMEOUT

    def __post_init__(self):
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.pending: dict[str, Permission] = {}
        self.tools: dict[str, dict] = {}
        self.client: AcpClient | None = None
        self.profile = PROFILES[self.agent]

    # ------------------------------------------------------------ events
    def emit(self, event_type: str, /, **data) -> None:
        # Positional-only: an event can carry its own "kind" (a tool's kind).
        with self.cond:
            if len(self.events) < MAX_EVENTS:
                self.events.append({"seq": len(self.events), "type": event_type, **data})
            self.cond.notify_all()

    def wait(self, after: int, timeout: float) -> tuple[list[dict], bool]:
        """Events from `after` on (waiting up to `timeout` for new ones) and
        whether more can still come (a turn is running)."""
        with self.cond:
            if len(self.events) <= after and self.busy and self.alive:
                self.cond.wait(timeout)
            return self.events[after:], self.busy and self.alive

    def info(self) -> dict:
        return {"id": self.id, "agent": self.agent, "model": self.model, "effort": self.effort,
                "mode": self.mode, "folder": self.folder_label, "busy": self.busy,
                "alive": self.alive, "events": len(self.events)}

    # ------------------------------------------------------------- start
    def start(self, command: list[str], env: dict[str, str]) -> None:
        self.client = AcpClient(command, self.folder, env, self._on_message, self._on_exit)
        req = self.client.request
        req("initialize", {"protocolVersion": 1,
                           "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                  "terminal": False},
                           "clientInfo": {"name": "baton", "version": __version__}}, START_TIMEOUT)
        new = req("session/new", {"cwd": str(self.folder), "mcpServers": []}, START_TIMEOUT)
        self.session_id = str(new.get("sessionId") or "")
        if not self.session_id:
            raise AcpError(f"{self.profile.label} did not open a session.")
        # Fail closed: without Baton's own agent (OpenCode) or agy's asking
        # "default" mode the model could run commands unasked.
        options = self._set("mode", self.profile.mode, new.get("configOptions"))
        options = self._set("model", self.model, options)
        effort_id = self.profile.effort_option
        effort = next((o for o in options if o.get("id") == effort_id), None)
        values = [str(v.get("value")) for v in (effort or {}).get("options") or []]
        if self.effort != "default":
            if self.effort in values:
                self._set(effort_id, self.effort, options)
            else:
                self.emit("notice", key="ag_effort_missing", effort=self.effort)
                self.effort = "default"

    def _set(self, option: str, value: str, options: Any) -> list[dict]:
        known = {o.get("id"): o for o in options or [] if isinstance(o, dict)}
        if option in ("mode", "model"):
            allowed = [str(v.get("value")) for v in (known.get(option) or {}).get("options") or []]
            if value not in allowed:
                raise AcpError(f"{self.profile.label} does not offer {option} '{value}'.")
        result = self.client.request("session/set_config_option", {
            "sessionId": self.session_id, "configId": option, "value": value}, START_TIMEOUT)
        options = [o for o in result.get("configOptions") or [] if isinstance(o, dict)]
        current = next((o.get("currentValue") for o in options if o.get("id") == option), None)
        if current != value:
            raise AcpError(f"{self.profile.label} kept {option} '{current}' instead of '{value}'.")
        return options

    # -------------------------------------------------------------- turns
    def prompt(self, text: str) -> None:
        with self.cond:
            if not self.alive:
                raise ChatError("ag_err_closed")
            if self.busy:
                raise ChatError("ag_err_busy")
            self.busy = True
            self.last_active = time.time()
        self.emit("user", text=text)
        threading.Thread(target=self._turn, args=(text,), daemon=True).start()

    def _turn(self, text: str) -> None:
        try:
            result = self.client.request("session/prompt", {
                "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]})
            self.emit("done", reason=str(result.get("stopReason") or "end_turn"))
            if self.on_usage and isinstance(result.get("usage"), dict):
                try:
                    self.on_usage(self.model, result["usage"])
                except Exception:  # counting tokens must never break a turn
                    pass
        except AcpError as exc:
            if self.alive:      # closing the conversation is not an error
                self.emit("error", text=str(exc)[:1000])
        finally:
            self._resolve_all("cancelled")
            with self.cond:
                self.busy = False
                self.last_active = time.time()
                self.cond.notify_all()

    def cancel(self) -> None:
        if self.client and self.busy:
            self.client.notify("session/cancel", {"sessionId": self.session_id})
            # ACP: after a cancel, pending permission requests get "cancelled".
            self._resolve_all("cancelled")

    def close(self) -> None:
        with self.cond:
            was = self.alive
            self.alive = False
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
        self._resolve_all("cancelled")
        if was:
            self.emit("error", text=(f"{self.profile.label} stopped. " + stderr).strip()[:1500])
            self.emit("closed")

    # ---------------------------------------------------- agent -> Baton
    def _on_message(self, msg: dict) -> None:
        method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
        if method == "session/request_permission" and rid is not None:
            self._permission(rid, params)
        elif method == "session/update" and rid is None:
            if params.get("sessionId") == self.session_id:
                self._update(params.get("update") or {})
        elif rid is not None:
            # Files and terminals stay with the agent's own (permission-checked) tools.
            self.client.respond(rid, error={"code": -32601, "message": f"{method} is not offered by Baton"})

    def _update(self, u: dict) -> None:
        kind = u.get("sessionUpdate")
        content = u.get("content") if isinstance(u.get("content"), dict) else {}
        if kind in ("agent_message_chunk", "agent_thought_chunk") and content.get("type") == "text":
            self.emit("text" if kind == "agent_message_chunk" else "thought", text=str(content.get("text", "")))
        elif kind in ("tool_call", "tool_call_update"):
            self.emit("tool", **self._tool(u))
        elif kind == "plan":
            entries = [{"content": str(e.get("content", ""))[:300], "status": str(e.get("status", ""))}
                       for e in u.get("entries") or [] if isinstance(e, dict)]
            self.emit("plan", entries=entries[:50])

    def _tool(self, u: dict) -> dict:
        tid = str(u.get("toolCallId") or "")
        with self.cond:
            t = self.tools.setdefault(tid, {"id": tid, "name": "", "title": "", "kind": "other",
                                            "status": "pending", "input": "", "output": "",
                                            "locations": [], "diffs": []})
            for key in ("name", "title", "kind", "status"):
                if isinstance(u.get(key), str) and u[key]:
                    t[key] = u[key][:300]
            if u.get("rawInput") not in (None, {}, ""):
                t["input"] = _clip(_as_text(u["rawInput"]), 5000)
            if isinstance(u.get("locations"), list):
                t["locations"] = [str(x.get("path", "")) for x in u["locations"] if isinstance(x, dict)][:20]
            if "content" in u:
                text, diffs = _content(u["content"])
                t["output"] = text or t["output"]
                t["diffs"] = diffs or t["diffs"]
            if u.get("rawOutput") not in (None, {}, "") and not t["output"]:
                t["output"] = _clip(_as_text(u["rawOutput"]))
            return dict(t)

    def _permission(self, rid: Any, params: dict) -> None:
        if params.get("sessionId") != self.session_id or not self.alive:
            self.client.respond(rid, {"outcome": {"outcome": "cancelled"}})
            return
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]

        def option(*kinds: str) -> str | None:
            for k in kinds:
                for o in options:
                    if o.get("kind") == k and o.get("optionId"):
                        return str(o["optionId"])
            return None

        call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        tool = self._tool({**call, "content": call.get("content") or []}) if call.get("toolCallId") else None
        raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
        _, diffs = _content(call.get("content"))
        perm = Permission(id=secrets.token_urlsafe(16), rpc_id=rid, allow=option("allow_once"),
                          deny=option("reject_once", "reject_always"),
                          expires=time.time() + self.permission_timeout)
        perm.timer = threading.Timer(self.permission_timeout, self.answer, args=(perm.id, False, "timeout"))
        perm.timer.daemon = True
        with self.cond:
            self.pending[perm.id] = perm
        perm.timer.start()
        if tool:
            self.emit("tool", **tool)
        self.emit("permission", id=perm.id, tool=tool["id"] if tool else "",
                  title=str(call.get("title") or (tool or {}).get("title") or "")[:300],
                  kind=str(call.get("kind") or (tool or {}).get("kind") or "other"),
                  command=str(raw.get("command") or "") if isinstance(raw.get("command"), str) else "",
                  input=_clip(_as_text(raw), 5000) if raw else "",
                  locations=(tool or {}).get("locations", []), diffs=diffs,
                  can_allow=perm.allow is not None, expires=perm.expires)

    # ----------------------------------------------------- Baton -> agent
    def answer(self, permission_id: str, allow: bool, why: str = "") -> bool:
        """Answer one pending request of *this* conversation, once."""
        with self.cond:
            perm = self.pending.pop(permission_id, None)
        if perm is None:
            return False
        if perm.timer:
            perm.timer.cancel()
        if why == "cancelled":
            outcome = {"outcome": "cancelled"}
        elif allow and perm.allow:
            outcome, why = {"outcome": "selected", "optionId": perm.allow}, "allowed"
        else:
            why = why or "denied"
            outcome = ({"outcome": "selected", "optionId": perm.deny} if perm.deny
                       else {"outcome": "cancelled"})
        if self.client:
            self.client.respond(perm.rpc_id, {"outcome": outcome})
        self.last_active = time.time()
        self.emit("permission_done", id=permission_id, outcome=why)
        return True

    def _resolve_all(self, why: str) -> None:
        with self.cond:
            ids = list(self.pending)
        for pid in ids:
            self.answer(pid, False, why)


class AgentChats:
    """The open conversations (in memory: a Baton restart ends them)."""

    def __init__(self, command: Callable[[], list[str] | None] = opencode_command,
                 permission_timeout: float = PERMISSION_TIMEOUT,
                 gemini_command: Callable[[], list[str] | None] = agy_command,
                 data_dir: Path | None = None):
        self.command = command                  # OpenCode
        self.gemini_command = gemini_command
        self.data_dir = data_dir                # where Gemini token counts go
        self.permission_timeout = permission_timeout
        self._chats: dict[str, AgentChat] = {}
        self._lock = threading.Lock()

    def get(self, chat_id: str) -> AgentChat | None:
        with self._lock:
            return self._chats.get(chat_id)

    def _reap(self) -> None:
        now = time.time()
        with self._lock:
            chats = list(self._chats.values())
        for chat in chats:
            idle = not chat.busy and not chat.pending
            if not chat.alive or (idle and now - chat.last_active > IDLE_SECONDS):
                self.close(chat.id)
        with self._lock:
            open_now = sorted(self._chats.values(), key=lambda c: c.last_active)
        while len(open_now) >= MAX_CHATS:
            oldest = next((c for c in open_now if not c.busy and not c.pending), None)
            if oldest is None:
                raise ChatError("ag_err_too_many")
            self.close(oldest.id)
            open_now.remove(oldest)

    def _record(self, model: str, tokens: dict) -> None:
        if self.data_dir is not None:
            record_gemini(self.data_dir, model, tokens, "chat")

    def create(self, folder: Path, folder_label: str, model: str, effort: str,
               mode: str = "confirm", agent: str = "opencode") -> AgentChat:
        if mode != "confirm":
            raise ChatError("ag_full_off")
        if agent not in PROFILES:
            raise ChatError("ag_unavailable")
        command = (self.gemini_command if agent == "gemini" else self.command)()
        if not command:
            raise ChatError("ag_unavailable_gemini" if agent == "gemini" else "ag_unavailable")
        self._reap()
        chat = AgentChat(id=secrets.token_urlsafe(12), folder=folder, folder_label=folder_label,
                         model=model, effort=effort, mode=mode, agent=agent,
                         on_usage=self._record if agent == "gemini" else None,
                         permission_timeout=self.permission_timeout)
        try:
            chat.start(command, chat_env(agent))
        except (AcpError, OSError) as exc:
            chat.close()
            raise ChatError("ag_err_start", str(exc)[:500]) from exc
        with self._lock:
            self._chats[chat.id] = chat
        return chat

    def close(self, chat_id: str) -> bool:
        with self._lock:
            chat = self._chats.pop(chat_id, None)
        if chat:
            chat.close()
        return chat is not None

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._chats)
        for cid in ids:
            self.close(cid)
