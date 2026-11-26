"""Fake Claude Code CLI speaking verified stream-json + --permission-prompt-tool stdio."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read_msg() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", action="store_true")
    ap.add_argument("--input-format", default="stream-json")
    ap.add_argument("--output-format", default="stream-json")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--permission-mode", default="manual")
    ap.add_argument("--permission-prompt-tool", default="stdio")
    ap.add_argument("--strict-mcp-config", action="store_true")
    ap.add_argument("--setting-sources", default="local")
    ap.add_argument("--disallowedTools", default="")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--effort", default="default")
    ap.add_argument("--resume", default="")
    ap.add_argument("--unsafe-mode", action="store_true")
    args, _ = ap.parse_known_args()

    cwd = Path.cwd()
    session_id = args.resume or f"claude-sess-{uuid.uuid4().hex[:8]}"
    state_file = cwd / f".fake-claude-{session_id}.txt"
    pmode = "bypassPermissions" if args.unsafe_mode else "default"

    def ask_permission(tool_name: str, tool_input: dict, desc: str, tid: str, blocked: str = "") -> tuple[bool, dict]:
        rid = f"perm-{uuid.uuid4().hex[:8]}"
        send({
            "type": "control_request",
            "request_id": rid,
            "request": {
                "subtype": "can_use_tool",
                "tool_name": tool_name,
                "display_name": tool_name,
                "input": tool_input,
                "description": desc,
                "blocked_path": blocked,
                "tool_use_id": tid,
            },
        })
        while True:
            reply = read_msg()
            if reply is None:
                return False, {}
            if reply.get("type") == "control_request":
                req = reply.get("request") or {}
                if req.get("subtype") == "interrupt":
                    return False, {}
            if reply.get("type") == "control_response":
                resp = reply.get("response") or {}
                if resp.get("request_id") == rid:
                    inner = resp.get("response") or {}
                    return inner.get("behavior") == "allow", inner

    while True:
        msg = read_msg()
        if msg is None:
            break
        mtype = msg.get("type")
        if mtype == "control_request":
            req = msg.get("request") or {}
            rid = msg.get("request_id", "")
            if req.get("subtype") == "initialize":
                send({
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": rid,
                        "response": {
                            "claude_code_version": "2.1.331",
                            "current_permission_mode": pmode,
                            "account": {"email": "SECRET_CLAUDE_EMAIL@example.com"},
                        },
                    },
                })
            elif req.get("subtype") == "interrupt":
                send({
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": rid, "response": {}},
                })
            continue

        if mtype != "user":
            continue

        blocks = ((msg.get("message") or {}).get("content") or [])
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
        send({
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "cwd": str(cwd),
            "model": args.model,
            "permissionMode": pmode,
        })

        if text.startswith("remember "):
            word = text.split(" ", 1)[1].strip()
            state_file.write_text(word, encoding="utf-8")
            send({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Remembered {word}."}]}})
        elif text == "recall":
            val = state_file.read_text(encoding="utf-8").strip() if state_file.exists() else "nothing"
            send({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Recalled: {val}."}]}})
        elif text.startswith("read "):
            name = text.split(" ", 1)[1].strip()
            tid = f"toolu_{uuid.uuid4().hex[:6]}"
            target = cwd / name
            content = target.read_text(encoding="utf-8") if target.exists() else "missing"
            send({"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": f"Reading {name}"},
                {"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": str(target)}},
            ]}})
            send({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content},
            ]}})
            send({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Read: {content.strip()}"}]}})
        elif text.startswith("write "):
            _, name, body = text.split(" ", 2)
            tid = f"toolu_{uuid.uuid4().hex[:6]}"
            target = cwd / name
            inp = {"file_path": str(target), "content": body + "\n"}
            send({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": "Write", "input": inp},
            ]}})
            allowed, inner = ask_permission("Write", inp, f"Write {name}", tid, blocked=str(target))
            if allowed:
                updated = inner.get("updatedInput") or inp
                Path(updated["file_path"]).write_text(updated["content"], encoding="utf-8")
                send({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": "ok"},
                ]}})
                send({"type": "assistant", "message": {"content": [{"type": "text", "text": "Written."}]}})
            else:
                send({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": "denied", "is_error": True},
                ]}})
                send({"type": "assistant", "message": {"content": [{"type": "text", "text": "Not written."}]}})
        elif text.startswith("run "):
            command = text.split(" ", 1)[1].strip()
            tid = f"toolu_{uuid.uuid4().hex[:6]}"
            inp = {"command": command, "description": f"Run {command}"}
            send({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": "PowerShell", "input": inp},
            ]}})
            allowed, _ = ask_permission("PowerShell", inp, f"Run {command}", tid)
            if allowed:
                send({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": f"ran: {command}"},
                ]}})
                send({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Ran {command}."}]}})
            else:
                send({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tid, "content": "denied", "is_error": True},
                ]}})
                send({"type": "assistant", "message": {"content": [{"type": "text", "text": "Command denied."}]}})
        elif text == "env":
            leaked = sorted(k for k in os.environ
                            if k.upper().startswith("BATON_") or k.upper() in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY"))
            reply = "leaked: " + (", ".join(leaked) if leaked else "none")
            send({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
        else:
            send({"type": "assistant", "message": {"content": [{"type": "text", "text": f"Echo: {text}"}]}})

        send({
            "type": "result",
            "subtype": "success",
            "session_id": session_id,
            "result": "done",
            "credential_expires_at": "SECRET_CREDENTIAL_EXPIRY_2099",
        })


if __name__ == "__main__":
    main()
