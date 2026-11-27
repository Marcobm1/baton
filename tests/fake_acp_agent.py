"""A fake OpenCode speaking ACP over stdio, for the Agents chat tests.

Prompts it understands:
  read <file>            reads a file in its folder with a "read" tool (no permission asked)
  write <file> <text>    asks permission with a diff, writes only if allowed
  run <command>          asks permission for a command (never runs it)
  env                    says whether GITHUB_TOKEN / BATON_* reached it
  anything else          answers "turn N: <text> (previous: <last text>)"
It offers the `baton-chat` mode only when Baton's inline config defines that
agent with "ask" for edits, like the real OpenCode loads it.

With `--gemini` it acts like `agy acp` instead: Gemini models, the effort as
`thought_level`, agy's modes (starting in "auto", so Baton must pick
"default") and token usage in each `session/prompt` answer.
"""
import json
import os
import sys
from pathlib import Path

# ACP is UTF-8, whatever the Windows code page says.
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
CWD = Path.cwd()
config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
agents_cfg = config.get("agents") or {}
agent = agents_cfg.get("baton-chat") or {}
asks_edit = any(r == {"action": "edit", "resource": "*", "effect": "ask"} for r in agent.get("permissions", []))
full_agent = agents_cfg.get("baton-chat-full") or {}
allows_edit = any(r == {"action": "edit", "resource": "*", "effect": "allow"} for r in full_agent.get("permissions", []))
GEMINI = "--gemini" in sys.argv
if GEMINI:
    MODES = ["default", "accept-edits", "auto", "plan"]
    MODELS = ["gemini-4-neon", "gemini-3.8-flash", "gemini-pro-orchestrator"]
    EFFORT, EFFORTS = "thought_level", ["low", "medium", "high"]
    state = {"model": "gemini-4-neon", "mode": "auto", EFFORT: "high"}
else:
    MODES = (["build", "plan"]
             + (["baton-chat"] if asks_edit else [])
             + (["baton-chat-full"] if allows_edit else []))
    MODELS = ["opencode-go/glm-5.3", "local-inkling/inkling-2-mini", "local-test/inkling-abliterated"]
    EFFORT, EFFORTS = "effort", ["none", "low", "high", "default"]
    state = {"model": "opencode-go/glm-5.3", "mode": "build", EFFORT: "default"}
history = []
next_id = [1000]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def options():
    return [{"id": "model", "category": "model", "type": "select", "currentValue": state["model"],
             "options": [{"value": m, "name": m} for m in MODELS]},
            {"id": EFFORT, "category": EFFORT, "type": "select", "currentValue": state[EFFORT],
             "options": [{"value": e} for e in EFFORTS]},
            {"id": "mode", "category": "mode", "type": "select", "currentValue": state["mode"],
             "options": [{"value": m, "name": m} for m in MODES]}]


def update(sid, **u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def say(sid, text):
    for i in range(0, len(text), 8):   # in chunks, like a streamed answer
        update(sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": text[i:i + 8]})


def ask(sid, call):
    """Send a permission request and wait for its answer (other messages are queued)."""
    next_id[0] += 1
    rid = next_id[0]
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": call,
        "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                    {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
                    {"optionId": "reject", "kind": "reject_once", "name": "Reject"}]}})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == rid and "method" not in msg:
            outcome = msg["result"]["outcome"]
            return outcome.get("optionId") if outcome.get("outcome") == "selected" else "cancelled"
    sys.exit(0)


def prompt(sid, text):
    history.append(text)
    words = text.split(" ", 2)
    if words[0] == "read":
        path = CWD / words[1]
        update(sid, sessionUpdate="tool_call", toolCallId="t-read", title=f"Read {words[1]}", kind="read",
               status="pending", rawInput={"filePath": str(path)}, locations=[{"path": str(path)}])
        update(sid, sessionUpdate="tool_call_update", toolCallId="t-read", status="completed",
               content=[{"type": "content", "content": {"type": "text", "text": path.read_text()}}])
        say(sid, f"The file says: {path.read_text().strip()}")
    elif words[0] == "write":
        path, new = CWD / words[1], words[2] + "\n"
        old = path.read_text() if path.exists() else None
        tid = f"t-write-{len(history)}"
        call = {"toolCallId": tid, "title": f"Write {words[1]}", "kind": "edit", "status": "pending",
                "rawInput": {"filePath": str(path), "content": new}, "locations": [{"path": str(path)}],
                "content": [{"type": "diff", "path": str(path), "oldText": old, "newText": new}]}
        update(sid, sessionUpdate="tool_call", **call)
        auto_ok = state["mode"] == "baton-chat-full" or (GEMINI and state["mode"] == "auto")
        choice = "once" if auto_ok else ask(sid, call)
        if choice == "once":
            path.write_text(new)
            update(sid, sessionUpdate="tool_call_update", toolCallId=tid, status="completed",
                   rawOutput={"output": "written"})
            say(sid, "Written.")
        else:
            update(sid, sessionUpdate="tool_call_update", toolCallId=tid, status="failed",
                   rawOutput={"output": f"permission {choice}"})
            say(sid, f"Not written ({choice}).")
    elif words[0] == "run":
        cmd = text[4:]
        call = {"toolCallId": "t-run", "title": cmd, "kind": "execute", "status": "pending",
                "rawInput": {"command": cmd}}
        update(sid, sessionUpdate="tool_call", **call)
        auto_ok = state["mode"] == "baton-chat-full" or (GEMINI and state["mode"] == "auto")
        choice = "once" if auto_ok else ask(sid, call)
        if choice == "once":
            update(sid, sessionUpdate="tool_call_update", toolCallId="t-run", status="completed",
                   rawOutput={"output": f"ran: {cmd}"})
        say(sid, f"Command {choice}.")
    elif text == "env":
        leaked = sorted(k for k in os.environ if k.startswith("BATON_") or k == "GITHUB_TOKEN")
        say(sid, "leaked: " + (",".join(leaked) or "none"))
    else:
        previous = history[-2] if len(history) > 1 else "-"
        update(sid, sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "hmm"})
        say(sid, f"turn {len(history)}: {text} (previous: {previous}, model {state['model']},"
                 f" effort {state[EFFORT]}, mode {state['mode']})")
    if GEMINI:
        return {"stopReason": "end_turn", "usage": {"inputTokens": 120, "outputTokens": 30,
                                                    "cachedReadTokens": 400, "thoughtTokens": 5}}
    return {"stopReason": "end_turn"}


sid = "ses_fake"
for line in sys.stdin:
    msg = json.loads(line)
    method, params, rid = msg.get("method"), msg.get("params") or {}, msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {}}})
    elif method == "session/new":
        assert Path(params["cwd"]) == CWD, (params["cwd"], CWD)
        send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid, "configOptions": options()}})
    elif method == "session/set_config_option":
        key, value = params["configId"], params["value"]
        allowed = {"model": MODELS, "mode": MODES, EFFORT: EFFORTS}[key]
        if value not in allowed:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"{key} not found"}})
            continue
        state[key] = value
        send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": options()}})
    elif method == "session/prompt":
        text = "".join(p.get("text", "") for p in params["prompt"])
        send({"jsonrpc": "2.0", "id": rid, "result": prompt(params["sessionId"], text)})
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown"}})
