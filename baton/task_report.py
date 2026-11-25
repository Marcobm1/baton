"""Build a task detail view and readable logs from Baton's saved record and CLI transcript."""

from __future__ import annotations

import json
import re

from .agents import cached_opencode_models
from .task_models import closest_opencode_go_model
from .task_store import Task


def _format_tool_step(step: dict) -> str:
    """Render one agy tool step_update payload as a single readable line."""
    tool = step.get("tool_call") or step.get("tool") or {}
    if isinstance(tool, str):
        name, payload = tool, step
    elif isinstance(tool, dict):
        name = str(tool.get("name") or tool.get("tool_name") or step.get("tool_name")
                   or step.get("name") or tool.get("title") or step.get("title") or "tool")
        payload = tool
    else:
        return ""
    detail = ""
    for key in ("command", "cmd", "path", "file", "file_path", "title", "query", "pattern"):
        val = payload.get(key) or step.get(key)
        if isinstance(val, str) and val.strip():
            detail = val.strip()
            break
    if not detail:
        args = payload.get("arguments") or payload.get("args") or payload.get("input") or step.get("arguments")
        if isinstance(args, dict):
            for key in ("command", "cmd", "path", "file", "file_path", "query", "pattern"):
                val = args.get(key)
                if isinstance(val, str) and val.strip():
                    detail = val.strip()
                    break
            if not detail and args:
                detail = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
        elif isinstance(args, str) and args.strip():
            detail = args.strip()
    status = str(payload.get("status") or step.get("status") or "").strip()
    one_line = " ".join(detail.split())[:140]
    suffix = f" ({status})" if status and status.lower() not in ("", "running", "in_progress") else ""
    return f"[tool] {name}: {one_line}{suffix}" if one_line else f"[tool] {name}{suffix}"


def _format_gemini_result(res: dict) -> str:
    status = str(res.get("status") or "DONE")
    usage = res.get("usage") if isinstance(res.get("usage"), dict) else {}
    parts = []
    for label, keys in (
        ("in", ("input_tokens", "inputTokens")),
        ("out", ("output_tokens", "outputTokens")),
        ("think", ("thinking_tokens", "thoughtTokens")),
        ("cache", ("cache_read_tokens", "cachedReadTokens")),
    ):
        for key in keys:
            val = usage.get(key)
            if isinstance(val, (int, float)) and int(val) > 0:
                parts.append(f"{label} {int(val):,}")
                break
    tokens_str = f" · tokens: {' · '.join(parts)}" if parts else ""
    return f"[result] {status}{tokens_str}"


def format_gemini_log(log: str) -> str:
    """Turn agy `--output-format stream-json` lines into a readable transcript.

    Joins streamed text deltas into paragraphs, collapses tool steps to one
    line each, appends a concise final `[result]` line with status and tokens,
    and preserves Baton's own `==` / `!!` log lines unchanged.
    """
    out: list[str] = []
    text_buf: list[str] = []
    saw_text = False

    def flush_text() -> None:
        if not text_buf:
            return
        chunk = "".join(text_buf).strip("\n")
        text_buf.clear()
        if chunk:
            out.append(chunk)

    for raw in log.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("{") or '"event"' not in stripped:
            flush_text()
            out.append(raw)
            continue
        try:
            ev = json.loads(stripped)
        except ValueError:
            flush_text()
            out.append(raw)
            continue
        if not isinstance(ev, dict) or "event" not in ev:
            flush_text()
            out.append(raw)
            continue

        kind = ev.get("event")
        if kind == "init":
            flush_text()
            init = ev.get("init") if isinstance(ev.get("init"), dict) else {}
            model = str(init.get("model") or "").strip()
            if model:
                out.append(f"[init] {model}")
        elif kind == "step_update":
            step = ev.get("step_update") if isinstance(ev.get("step_update"), dict) else {}
            delta = step.get("text_delta")
            if not isinstance(delta, str):
                delta = step.get("delta") if isinstance(step.get("delta"), str) else None
            if delta:
                text_buf.append(delta)
                saw_text = True
            elif any(k in step for k in ("tool_call", "tool", "tool_name")):
                flush_text()
                line = _format_tool_step(step)
                if line:
                    out.append(line)
            elif isinstance(step.get("text"), str) and step["text"].strip():
                text_buf.append(step["text"])
                saw_text = True
        elif kind == "result":
            res = ev.get("result") if isinstance(ev.get("result"), dict) else {}
            if not saw_text and isinstance(res.get("response"), str) and res["response"].strip():
                text_buf.append(res["response"])
                saw_text = True
            flush_text()
            out.append(_format_gemini_result(res))
        elif kind == "error":
            flush_text()
            err = ev.get("error")
            msg = err.get("message") if isinstance(err, dict) else str(err or "")
            if msg:
                out.append(f"[error] {msg}")

    flush_text()
    return "\n".join(out)


def format_task_log(agent: str, log: str) -> str:
    """Format a task log for human display (readable stream-json for Gemini)."""
    if agent == "gemini" and log:
        return format_gemini_log(log)
    return log


def _claude_failure(reason: str) -> str:
    if re.search(r"rate.limit|usage.limit|hit your (?:session |weekly )?limit|"
                 r"quota.{0,30}(?:exceed|exhaust)|out of (?:usage|credits)|"
                 r"insufficient_quota|too many requests", reason, re.I):
        return "quota"
    model = r"(?:model|fable|mythos)"
    restriction = (r"(?:additional access|extra usage|not (?:available|enabled|allowed) "
                   r"(?:to|on|for)|access (?:is )?(?:denied|required)|"
                   r"requires? .{0,40}(?:plan|subscription|tier|access))")
    if re.search(model + r".{0,160}" + restriction + "|" +
                 r"(?:access to|not authorized to (?:use|access)|not entitled to|"
                 r"not have permission to use|extra usage.{0,30}required for).{0,80}" + model,
                 reason, re.I):
        return "access"
    return "other"


_OPENCODE_GO_RE = re.compile(
    r"OpenCode Go covers only requests to the OpenCode Go provider"
    r"|insufficient balance.*opencode go"
    r"|opencode go.*insufficient balance",
    re.I,
)


def _opencode_failure(lines: list[str], is_opencode: bool) -> tuple[str, str]:
    for line in reversed(lines):
        if _OPENCODE_GO_RE.search(line) or (is_opencode and re.search(r"insufficient balance", line, re.I)):
            return "opencode_go", line[-1000:]
    joined = " ".join(lines[-6:])[-1000:]
    if _OPENCODE_GO_RE.search(joined) or (is_opencode and re.search(r"insufficient balance", joined, re.I)):
        return "opencode_go", joined
    return "", ""


def _agent_lines(output: str, agent: str) -> list[str]:
    if agent == "gemini":
        output = format_gemini_log(output)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if agent not in ("codex", "local") or "user" not in lines:
        return lines
    # Codex echoes the prompt and terminal/tool output. Only use assistant
    # turns as reports. The full transcript is still available in the log.
    assistant = False
    reports = []
    for line in lines[lines.index("user") + 1:]:
        if line == "codex":
            assistant = True
        elif line in ("exec", "user", "thinking", "tokens used", "file update") or line.startswith("tool "):
            assistant = False
        elif assistant:
            reports.append(line)
    return reports


def task_report(task: Task, log: str) -> dict:
    start = re.search(r"^== Running [^\n]*\n", log, re.M)
    output = log[start.end():] if start else ""
    finish = re.search(r"== Agent finished \(exit code (-?\d+)\)", output, re.M)
    output = output[:finish.start()] if finish else output
    output = re.sub(r"^!! .*", "", output, flags=re.M)
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    lines = _agent_lines(output, task.agent)

    failure = reason = suggestion = ""
    if task.status == "failed" and (not finish or int(finish[1])):
        is_oc = task.agent == "opencode" or task.model.startswith("opencode/")
        failure, reason = _opencode_failure(lines, is_oc)
        if failure == "opencode_go":
            model = task.model
            if not model and start:
                m = re.search(r"\(([^()\s]+/[^()\s]+)\)", start[0])
                model = m[1] if m else ""
            if model:
                suggestion = closest_opencode_go_model(model, cached_opencode_models())
        elif task.agent == "claude-code" and finish and int(finish[1]):
            reason = " ".join(lines[-4:])[-1000:]
            if reason:
                failure = _claude_failure(reason)

    fields = {}
    for line in lines:
        match = re.fullmatch(r"(progress|summary|checks|tests|progreso|resumen|comprobaciones):\s*(.+)", line, re.I)
        if match:
            fields[match[1].lower()] = match[2]
    progress = fields.get("progress") or fields.get("progreso")
    summary = fields.get("summary") or fields.get("resumen")
    checks = fields.get("checks") or fields.get("tests") or fields.get("comprobaciones")
    if task.agent in ("codex", "local") and "\ncodex\n" in output and lines:
        progress = progress or lines[-1]
        final_turn = output.rsplit("\ncodex\n", 1)[-1].strip().splitlines()
        if final_turn:
            summary = summary or final_turn[0]
    elif task.agent == "claude-code" and lines and task.status in ("done", "no-changes"):
        # Older Claude -p runs printed the final answer directly.
        summary = summary or lines[0]
    if not checks:
        checks = next((line for line in reversed(lines)
                       if re.search(r"\b\d+ (?:passed|failed|tests? passed|pruebas superadas)\b", line)), "")
    return dict(summary=summary, checks=checks, progress=progress, reason=reason, failure=failure,
                suggestion=suggestion, pushed=any(s.endswith("Branch pushed to GitHub") for s in task.steps))
