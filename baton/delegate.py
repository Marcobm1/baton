"""`baton delegate`: hand a small subtask to an OpenCode model and print its answer.

Meant for the main agent (Claude Code, Codex) or for you: "summarise these
files with a cheap model", "draft tests for this function", "second opinion
on this plan". The main agent stays responsible and checks the result.

Two modes, both through Baton's own OpenCode agents (rules in the config, so
they hold even with --auto, which approves only what isn't denied):
  * read-only (default): agent "baton-helper" may read and search, never edit
    files or run commands. Commands are denied entirely because a model that
    can't edit will write files with the terminal instead (tested).
  * --write: agent "baton" (the one Baton's tasks use) may edit and run
    commands inside the folder, never commit, push or change branch.
"""

from __future__ import annotations

import json
import subprocess
import sys

from .ping import _NO_WINDOW, _clean_env
from .runner import OPENCODE_CONFIG, find_opencode, opencode_models

READ_ONLY_AGENT = {
    "mode": "primary",
    "description": "Read-only helper for a subtask delegated with baton delegate",
    "permission": {
        "read": "allow", "glob": "allow", "grep": "allow", "list": "allow",
        "edit": "deny", "bash": "deny", "external_directory": "deny",
    },
}


def config() -> dict:
    cfg = json.loads(json.dumps(OPENCODE_CONFIG))
    cfg["agent"]["baton-helper"] = READ_ONLY_AGENT
    return cfg


def command(exe: str, model: str, prompt: str, write: bool) -> list[str]:
    return [exe, "run", "--standalone", "--auto", "--agent", "baton" if write else "baton-helper",
            "-m", model, "--archived", "--title", "Delegated with baton", prompt]


def run(model: str, prompt: str, cwd: str, write: bool = False, timeout: int = 900,
        check_model: bool = True) -> int:
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
    env = _clean_env()
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config())
    try:
        # stdin closed: OpenCode would otherwise wait for more input.
        p = subprocess.run(command(exe, model, prompt, write), cwd=cwd, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, **_NO_WINDOW)
    except subprocess.TimeoutExpired:
        print(f"Stopped after {timeout} s without an answer.", file=sys.stderr)
        return 124
    sys.stdout.write(p.stdout)
    if p.returncode:
        sys.stderr.write(p.stderr[-2000:])
    return p.returncode
