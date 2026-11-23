"""Build a task detail view from Baton's saved record and CLI transcript."""

import re

from .runner import Task


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


def _agent_lines(output: str, agent: str) -> list[str]:
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

    failure = reason = ""
    if task.agent == "claude-code" and task.status == "failed" and finish and int(finish[1]):
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
                pushed=any(s.endswith("Branch pushed to GitHub") for s in task.steps))
