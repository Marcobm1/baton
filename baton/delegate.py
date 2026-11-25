"""`baton delegate`: hand a small subtask to an OpenCode model and print its answer.

Meant for the main agent (Claude Code, Codex) or for you: "summarise these
files with a cheap model", "draft tests for this function", "second opinion
on this plan". The main agent stays responsible and checks the result.

Two modes, both through Baton's own OpenCode agents (rules in the config, so
they hold even with --auto, which approves only what isn't denied):
  * read-only (default): agent "baton-helper" may only read and search files
    in the folder. Everything else is denied: edits, commands (a model that
    can't edit writes files with the terminal instead), MCP tools, Code Mode,
    subagents. --web also lets it search and read web pages.
  * --write: agent "baton" (the one Baton's tasks use) may edit and run
    commands, never commit, push or change branch. OpenCode has no OS sandbox:
    its terminal runs as you and CAN write outside the folder (tested
    2026-11-23). Use it only with instructions you or your agents wrote, never
    with outside content (web pages, honeypot data, other people's repos).
    For the same reason --web can't be combined with --write.
"""

from __future__ import annotations

import json
import subprocess
import sys

from .ping import _NO_WINDOW, agent_env
from .ranking import cli_ranking
from .agents import ALWAYS_DENIED, OPENCODE_CONFIG, READ_RULES, _rule, find_opencode, opencode_models

WEB_RULES = [_rule("webfetch", "allow"), _rule("websearch", "allow")]


def config(web: bool = False) -> dict:
    cfg = json.loads(json.dumps(OPENCODE_CONFIG))
    cfg["agents"]["baton-helper"] = {
        "mode": "primary",
        "description": "Read-only helper for a subtask delegated with baton delegate",
        "permissions": READ_RULES + (WEB_RULES if web else []) + ALWAYS_DENIED,
    }
    if web:
        cfg["agents"]["baton"]["permissions"] = (cfg["agents"]["baton"]["permissions"]
                                                 + WEB_RULES + ALWAYS_DENIED)
    return cfg


def command(exe: str, model: str, prompt: str, write: bool) -> list[str]:
    return [exe, "run", "--standalone", "--auto", "--agent", "baton" if write else "baton-helper",
            "-m", model, "--archived", "--title", "Delegated with baton", prompt]


def run(model: str, prompt: str, cwd: str, write: bool = False, timeout: int = 900,
        check_model: bool = True, web: bool = False, kind: str = "general") -> int:
    exe = find_opencode()
    if not exe:
        print("OpenCode not found (install it, or set BATON_OPENCODE_BIN).", file=sys.stderr)
        return 2
    if check_model:
        models = opencode_models()
        if models and model not in models:
            print(f"Model {model!r} isn't available in OpenCode. Run: baton delegate --list",
                  file=sys.stderr)
            return 2
    env = agent_env()
    ranking = cli_ranking()
    job = ranking.start(model, kind, prompt)
    print(f"Delegation #{job} (baton rate {job} --help)", file=sys.stderr, flush=True)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config(web))
    try:
        # stdin closed: OpenCode would otherwise wait for more input.
        p = subprocess.run(command(exe, model, prompt, write), cwd=cwd, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, **_NO_WINDOW)
    except subprocess.TimeoutExpired:
        message = f"Stopped after {timeout} s without an answer."
        ranking.finish(job, "timeout", message)
        print(message, file=sys.stderr)
        return 124
    except OSError as exc:
        ranking.finish(job, "failed", str(exc))
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        ranking.finish(job, "cancelled", "")
        raise
    result = p.stdout
    if p.returncode:
        result += f"\nExit {p.returncode}\n{p.stderr[-2000:]}"
    ranking.finish(job, "failed" if p.returncode else "done", result)
    # Windows consoles default to an old code page that can't print emojis.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.stdout.write(p.stdout)
    if p.returncode:
        sys.stderr.write(p.stderr[-2000:])
    return p.returncode
