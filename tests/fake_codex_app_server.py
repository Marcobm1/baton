"""Fake Codex `app-server` speaking verified JSON-RPC 2.0 over stdio."""

from __future__ import annotations

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
    cwd = Path.cwd()
    thread_id = ""
    next_srv_id = 100

    def ask_approval(method: str, params: dict) -> str:
        nonlocal next_srv_id
        next_srv_id += 1
        rid = next_srv_id
        send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            reply = read_msg()
            if reply is None:
                return "cancel"
            if reply.get("method") == "turn/interrupt":
                return "cancel"
            if reply.get("id") == rid and "result" in reply:
                return str((reply.get("result") or {}).get("decision") or "decline")

    while True:
        msg = read_msg()
        if msg is None:
            break
        method = msg.get("method")
        params = msg.get("params") or {}
        rid = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"serverInfo": {"name": "fake-codex", "version": "0.171.0"}}})
            # Emit account notification so tests prove Baton never forwards/stores it.
            send({"jsonrpc": "2.0", "method": "account/updated",
                  "params": {"planType": "SECRET_PLAN_PRO", "email": "SECRET_CODEX@example.com"}})
        elif method == "initialized":
            continue
        elif method == "thread/start":
            assert params.get("sandbox") == "read-only"
            assert params.get("approvalPolicy") == "untrusted"
            thread_id = f"codex-thread-{uuid.uuid4().hex[:8]}"
            send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": thread_id}}})
        elif method == "thread/resume":
            assert params.get("excludeTurns") is True
            assert params.get("sandbox") == "read-only"
            assert params.get("approvalPolicy") == "untrusted"
            thread_id = str(params.get("threadId") or "")
            send({"jsonrpc": "2.0", "id": rid, "result": {"thread": {"id": thread_id}}})
        elif method == "turn/start":
            turn_id = f"turn-{uuid.uuid4().hex[:6]}"
            send({"jsonrpc": "2.0", "id": rid, "result": {"turn": {"id": turn_id}}})
            items = params.get("input") or []
            text = "".join(i.get("text", "") for i in items if isinstance(i, dict) and i.get("type") == "text").strip()
            state_file = cwd / f".fake-codex-{thread_id}.txt"

            if text.startswith("remember "):
                word = text.split(" ", 1)[1].strip()
                state_file.write_text(word, encoding="utf-8")
                send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                      "params": {"delta": f"Remembered {word}."}})
                send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
            elif text == "recall":
                val = state_file.read_text(encoding="utf-8").strip() if state_file.exists() else "nothing"
                send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                      "params": {"delta": f"Recalled: {val}."}})
                send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
            elif text.startswith("run "):
                cmd = text.split(" ", 1)[1].strip()
                item_id = f"cmd-{uuid.uuid4().hex[:6]}"
                send({"jsonrpc": "2.0", "method": "item/started",
                      "params": {"item": {"id": item_id, "type": "commandExecution", "command": cmd, "status": "inProgress"}}})
                decision = ask_approval("item/commandExecution/requestApproval", {
                    "threadId": thread_id, "turnId": turn_id, "itemId": item_id,
                    "kind": "command", "command": cmd, "cwd": str(cwd), "reason": "Run shell command",
                })
                if decision == "accept":
                    (cwd / "accepted.txt").write_text(cmd + "\n", encoding="utf-8")
                    send({"jsonrpc": "2.0", "method": "item/commandExecution/outputDelta",
                          "params": {"itemId": item_id, "delta": f"out: {cmd}\n"}})
                    send({"jsonrpc": "2.0", "method": "item/completed",
                          "params": {"item": {"id": item_id, "type": "commandExecution", "command": cmd,
                                              "status": "completed", "exitCode": 0}}})
                    send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                          "params": {"delta": f"Executed: {cmd}."}})
                    send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
                else:
                    send({"jsonrpc": "2.0", "method": "item/completed",
                          "params": {"item": {"id": item_id, "type": "commandExecution", "command": cmd,
                                              "status": "declined"}}})
                    send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                          "params": {"delta": f"Declined ({decision})."}})
                    send({"jsonrpc": "2.0", "method": "turn/completed",
                          "params": {"turn": {"status": "cancelled" if decision == "cancel" else "completed"}}})
            elif text.startswith("edit "):
                _, name, body = text.split(" ", 2)
                item_id = f"fc-{uuid.uuid4().hex[:6]}"
                target = cwd / name
                send({"jsonrpc": "2.0", "method": "item/started",
                      "params": {"item": {"id": item_id, "type": "fileChange", "changes": [{"path": str(target)}],
                                          "status": "inProgress"}}})
                decision = ask_approval("item/fileChange/requestApproval", {
                    "threadId": thread_id, "turnId": turn_id, "itemId": item_id,
                    "changes": [{"path": str(target)}], "reason": f"Edit {name}",
                })
                if decision == "accept":
                    target.write_text(body + "\n", encoding="utf-8")
                    send({"jsonrpc": "2.0", "method": "item/completed",
                          "params": {"item": {"id": item_id, "type": "fileChange", "changes": [{"path": str(target)}],
                                              "status": "completed"}}})
                    send({"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"delta": "Edited."}})
                else:
                    send({"jsonrpc": "2.0", "method": "item/completed",
                          "params": {"item": {"id": item_id, "type": "fileChange", "changes": [{"path": str(target)}],
                                              "status": "declined"}}})
                    send({"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"delta": "Edit declined."}})
                send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
            elif text == "env":
                leaked = sorted(k for k in os.environ
                                if k.upper().startswith("BATON_") or k.upper() in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY"))
                reply = "leaked: " + (", ".join(leaked) if leaked else "none")
                send({"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"delta": reply}})
                send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})
            else:
                send({"jsonrpc": "2.0", "method": "item/reasoning/summaryTextDelta",
                      "params": {"delta": "Thinking…"}})
                send({"jsonrpc": "2.0", "method": "item/agentMessage/delta",
                      "params": {"delta": f"Codex echo: {text}"}})
                send({"jsonrpc": "2.0", "method": "turn/completed", "params": {"turn": {"status": "completed"}}})


if __name__ == "__main__":
    main()
