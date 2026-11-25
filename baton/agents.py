"""Agent definitions, OpenCode rules, prompts, CLI command builders, and output parsers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from .ping import _NO_WINDOW, agent_env, find_agy, find_claude, find_codex
from .task_models import catalog
from .task_store import Task

AGENTS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "gemini": "Gemini",
    "opencode": "OpenCode",
    "local": "IA local",
}

# Agents whose work Claude Code reviews (and rates) before it is merged. The
# others (Claude Code, Codex, Gemini) run with full permissions and merge alone.
REVIEWED_AGENTS = ("opencode", "local")

# OpenCode has no OS sandbox, so Baton gives it its own agents with rules
# (OpenCode 2.x format: a list of {action, resource, effect}; last match wins).
# The list starts by denying EVERYTHING and then allows only what is needed,
# because OpenCode can have more tools than you expect: MCP servers (a GitHub
# one can push files), Code Mode ("execute"), subagents, scheduled automations.
# Explicit denies hold even with --auto, which only approves what isn't denied.
# Tested 2026-11-23: denying `edit` alone is not enough (the model wrote the
# file with the terminal), and an "allow everything except…" list let a helper
# keep GitHub and automation tools.
NO_GIT_CHANGES = ["git commit*", "git push*", "git checkout*", "git switch*", "git reset*",
                  "git rebase*", "git worktree*", "git branch*", "git remote*", "git tag*",
                  "gh *", "sudo*"]


def _rule(action: str, effect: str, resource: str = "*") -> dict:
    return {"action": action, "resource": resource, "effect": effect}


# Tested 2026-11-23: OpenCode's own tools (opencode_worktree_remove…) still
# ran under the deny-all, so they are named again explicitly, at the end.
ALWAYS_DENIED = [_rule(a, "deny") for a in ("opencode_*", "skill", "subagent", "execute")]

READ_RULES = [_rule("*", "deny")] + [_rule(a, "allow") for a in ("read", "glob", "grep")]

# Baton's tasks (and `baton delegate --write`): read, edit and run commands in
# the task's folder; never commit, push, change branch or leave the folder.
TASK_RULES = (READ_RULES + [_rule("edit", "allow"), _rule("shell", "allow")]
              + [_rule("shell", "deny", c) for c in NO_GIT_CHANGES] + ALWAYS_DENIED)

OPENCODE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "agents": {
        "baton": {
            "mode": "primary",
            "description": "Task sent from Baton, inside its own git worktree",
            "permissions": TASK_RULES,
        }
    },
}


def find_opencode() -> str | None:
    """The real opencode.exe, never the npm .cmd/.ps1 wrapper.

    Passing a task's text as an argument to a .cmd file would go through
    cmd.exe, which treats & | < > ^ as commands and cuts at line breaks.
    """
    explicit = os.environ.get("BATON_OPENCODE_BIN")
    if explicit:
        return explicit
    found = shutil.which("opencode")
    if found and found.lower().endswith((".cmd", ".ps1", ".bat")):
        exe = Path(found).parent / "node_modules" / "@opencode" / "cli" / "bin" / "opencode.exe"
        return str(exe) if exe.is_file() else None
    return found


_models_cache: tuple[float, list[str]] | None = None


def opencode_models() -> list[str]:
    """Models OpenCode can use right now (`opencode models`), cached 10 minutes."""
    global _models_cache
    if _models_cache and time.time() - _models_cache[0] < 600:
        return _models_cache[1]
    exe = find_opencode()
    models: list[str] = []
    if exe:
        try:
            r = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60, stdin=subprocess.DEVNULL,
                               env=agent_env(), **_NO_WINDOW)
            models = [ln.strip() for ln in r.stdout.splitlines()
                      if re.fullmatch(r"[\w.\-]+/[\w.:\-/]+", ln.strip())]
        except (OSError, subprocess.SubprocessError):
            models = []
    _models_cache = (time.time(), models)
    return models


def cached_opencode_models() -> list[str]:
    """The last `opencode models` list, without running OpenCode (safe in request handlers)."""
    return _models_cache[1] if _models_cache else []


def agy_events(output: str) -> tuple[str, dict]:
    """The model of agy's `init` event and its last `result` event.

    With `--output-format stream-json` agy writes one JSON object per line:
    `init` (model, tools, permission mode), `step_update` (text deltas, tool
    steps) and finally `result` (status, the whole `response` text and the
    token `usage`). The final report sits inside `response` as an escaped
    JSON string, so it is read from there rather than from the raw lines.
    """
    model, result = "", {}
    for line in output.splitlines():
        if not line.startswith("{") or '"event"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("event") == "init" and isinstance(ev.get("init"), dict):
            model = str(ev["init"].get("model") or model)
        elif ev.get("event") == "result" and isinstance(ev.get("result"), dict):
            result = ev["result"]
    return model, result


NEXT_TASK_REQUEST = (
    "When you finish a task, mark its checklist item complete in the project's STATUS.md. "
    "Prefix owner-only actions with (Marco). Find links in tasks.json or on GitHub yourself. "
    "Prepare the first unchecked agent task under ## Next so an agent can do it independently. Reuse or add "
    "a short title and an indented description of the work and how to check it. "
    "A provisional draft is fine. Set the front matter next field to that title. "
    "If that task must be done in another repository, add a line `Repo: <owner/repo>` to its "
    "description. If this task belongs in another repository, say so in your report instead "
    "of changing files outside this one."
)

RECOMMEND_REQUEST = (
    "In that description add one line `Recommended: <agent>, <model>, <effort>` naming the "
    "agent, model and reasoning effort that fit that next task best: cheaper, faster models "
    "for simple and well-defined work; stronger models and more effort for complex, risky or "
    "design work. Leave out the model or the effort to use the tool's default. "
    "Choose from what this computer offers: {choices}."
)


def next_task_request(choices: str = "") -> str:
    """The next-task instructions, with the recommendation line when choices are known."""
    if not choices:
        return NEXT_TASK_REQUEST
    return f"{NEXT_TASK_REQUEST} {RECOMMEND_REQUEST.format(choices=choices)}"


SUMMARY_REQUEST = (
    "Finish with a report for the owner, in the language required by the project's "
    "instructions or otherwise in the language of the task. Explain in plain language "
    "what you accomplished or found, how and why, what you actually verified and the "
    "results, any problems you ran into, and anything the owner needs to know or do "
    "next. Start the report with a line beginning \"Summary:\" (one sentence) and add a "
    "line beginning \"Checks:\" (the checks you ran and their results). Include no "
    "secrets. Put BATON_SUMMARY_BEGIN on its own line before the report and "
    "BATON_SUMMARY_END on its own line after it."
)


def extract_summary(output: str, marker: str = "BATON_SUMMARY", *, after: str = "") -> str:
    # Some CLIs colour their output. Ignore incomplete reports and echoed instructions.
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    if after:
        # A CLI can echo the task prompt. Only the rating after the final report counts.
        sections = re.split(rf"(?m)^[^\S\n]*{re.escape(after)}[^\S\n]*$", output)
        if len(sections) < 2:
            return ""
        output = sections[-1]
    report = None
    last = ""
    for line in output.splitlines():
        if line.strip() == f"{marker}_BEGIN":
            report = []
        elif line.strip() == f"{marker}_END":
            if report:
                last = "\n".join(report).strip() or last
            report = None
        elif report is not None:
            report.append(line)
    return last


REVIEW_PROMPT = """You are reviewing work that another AI agent ({author}) did for the task
below in the repository {repo}, before it is merged. This folder is a git
worktree on the branch `{branch}`; the work is the commits on top of
`origin/{base}` (see `git log origin/{base}..HEAD` and `git diff origin/{base}...HEAD`).

Rules:
- First read the repository's AGENTS.md or CLAUDE.md and STATUS.md, and follow them.
- Check that the change does what the task asked and only that, that it is
  correct, that tests cover it and pass (run the project's tests if you can),
  that nothing unsafe was added (secrets, credentials, destructive commands)
  and that STATUS.md was updated as the rules say.
- If you find problems you can fix safely, fix them in this folder, keeping
  the fixes small, and run the tests again.
- If the work should not be merged and you cannot fix it safely, change
  nothing and explain why.
- Do NOT commit, push, switch branches or rewrite history: Baton commits your
  fixes and merges the pull request only if you allow it.
- Finish with a review for the owner, in the language required by the
  project's instructions or otherwise in the language of the task. Its first
  line must be exactly one of: "Verdict: APPROVE" (ready, nothing changed),
  "Verdict: FIXED" (you fixed problems: list them) or "Verdict: BLOCK" (do
  not merge: say why). Then say what you checked and the results. Put
  BATON_REVIEW_BEGIN on its own line before the review and BATON_REVIEW_END
  on its own line after it.

Task: {title}

{details}
"""

PROMPT = """You are working on the task below for the repository {repo}.
Baton created this folder for you: a git worktree of {repo} on the new branch
`{branch}`, based on `{base}`. You are not in the owner's own working folder.

Rules:
- First read the repository's AGENTS.md or CLAUDE.md and STATUS.md, and follow them.
- Do the task. Keep the change focused on it.
- Update STATUS.md as the repository's rules say (updated_by: {agent}).
- {next_task_request}
- Do NOT commit, push, switch branches or rewrite history: when you finish,
  Baton commits everything in this folder and opens a pull request.
- If the task is unclear, needs secrets, or would need anything destructive or
  outside this folder, stop and explain why instead of doing it.
- Prefix progress updates with "Progress:".
- {summary_request}

Task: {title}

{details}
"""


def available_agents(
    *,
    local_model: str,
    claude_bin: Callable[[], str | None] = find_claude,
    codex_bin: Callable[[], str | None] = find_codex,
    agy_bin: Callable[[], str | None] = find_agy,
    opencode_bin: Callable[[], str | None] = find_opencode,
) -> dict[str, str]:
    """agent -> "" if usable, else the reason it isn't."""
    out = {}
    out["claude-code"] = "" if claude_bin() else "Claude Code CLI not found"
    codex = codex_bin()
    out["codex"] = "" if codex else "Codex CLI not found"
    out["gemini"] = "" if agy_bin() else "Antigravity CLI (agy) not found"
    out["opencode"] = "" if opencode_bin() else "OpenCode CLI not found"
    out["local"] = ("" if (codex and local_model)
                    else "set BATON_LOCAL_MODEL (and install Ollama or LM Studio)")
    return out


def format_model_choices(
    availability: dict[str, str],
    *,
    catalog_fn: Callable[[str], dict] = catalog,
    opencode_models_fn: Callable[[], list[str]] = opencode_models,
) -> str:
    """Agents and models available here, for the next-task recommendation."""
    parts = []
    for agent, reason in availability.items():
        if reason:
            continue
        if agent in ("claude-code", "codex", "gemini"):
            models = [f"{m} ({'/'.join(v['efforts'])})" if v["efforts"] else m
                      for m, v in catalog_fn(agent).items()]
            parts.append(f"{agent}: {'; '.join(models)}" if models else agent)
        elif agent == "opencode":
            models = opencode_models_fn()[:20]
            parts.append(f"opencode: {', '.join(models)}" if models else "opencode")
        else:
            parts.append(agent)
    return " | ".join(parts)


def build_prompt(task: Task, *, choices: str = "") -> str:
    return PROMPT.format(
        repo=task.repo,
        branch=task.branch,
        base=task.base,
        agent=task.agent,
        title=task.title,
        summary_request=SUMMARY_REQUEST,
        next_task_request=next_task_request(choices),
        details=task.details or "(no more details)",
    )


def build_agent_cmd(
    task: Task,
    wt: Path,
    *,
    prompt_fn: Callable[[Task], str],
    local_provider: str,
    restricted: bool = False,
    claude_bin: Callable[[], str | None] = find_claude,
    codex_bin: Callable[[], str | None] = find_codex,
    agy_bin: Callable[[], str | None] = find_agy,
    opencode_bin: Callable[[], str | None] = find_opencode,
) -> list[str]:
    """The agent's command line (a list: no shell ever parses it).

    Tasks by Claude Code, Codex and Gemini run with full permissions;
    `restricted` (reviews) keeps Claude Code in `auto` mode and Codex in
    its sandbox. OpenCode and local AI are always limited.
    """
    if task.agent == "opencode":
        # The prompt goes as the message argument (opencode.exe directly, no shell).
        return [opencode_bin() or "opencode", "run", "--standalone", "--auto", "--agent", "baton",
                "-m", task.model, "--title", f"Baton {task.id}", prompt_fn(task)]
    if task.agent == "gemini":
        # agy.exe directly (never a .cmd), the prompt as the -p argument.
        cmd = [agy_bin() or "agy", "-p", prompt_fn(task), "--output-format", "stream-json",
               "--dangerously-skip-permissions"]
        if task.model:
            cmd.extend(["--model", task.model])
        if task.effort:
            cmd.extend(["--effort", task.effort])
        return cmd
    if task.agent == "claude-code":
        mode = ["--permission-mode", "auto"] if restricted else ["--dangerously-skip-permissions"]
        cmd = [claude_bin() or "claude", "-p", *mode]
        if task.model:
            cmd.extend(["--model", task.model])
        if task.effort:
            cmd.extend(["--effort", task.effort])
        return cmd
    codex = codex_bin() or "codex"
    if task.agent == "codex" and not restricted:
        # No sandbox and no approvals: network and live web search included.
        cmd = [codex, "--search", "exec", "--dangerously-bypass-approvals-and-sandbox",
               "--color", "never", "-C", str(wt)]
    else:
        cmd = [codex, "exec", "-s", "workspace-write", "--color", "never", "-C", str(wt)]
    if task.agent == "local":
        cmd[2:2] = ["--oss", "--local-provider", local_provider, "-m", task.model]
    elif task.agent == "codex":
        if task.model:
            cmd.extend(["-m", task.model])
        if task.effort:
            cmd.extend(["-c", f"model_reasoning_effort={json.dumps(task.effort)}"])
    return cmd + ["-"]
