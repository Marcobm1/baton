"""Command line: python -m baton <command>  (or just `baton <command>`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path


def _settings():
    from .config import Settings

    return Settings.from_env()


def windows_restart(data_dir: Path):
    """Hand off to an independent process before the server starts shutting down."""
    subprocess.run(["schtasks", "/Query", "/TN", "Baton"], check=True,
                   capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    # The scheduled action can include the venv launcher, which exits just
    # after pythonw. Wait for Task Scheduler to release the instance as well.
    script = f"""
        $ErrorActionPreference = 'Stop'
        Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue
        $scheduler = New-Object -ComObject Schedule.Service
        $scheduler.Connect()
        $task = $scheduler.GetFolder('\\').GetTask('Baton')
        for ($i = 0; $i -lt 120; $i++) {{
            if ($task.State -eq 3) {{
                schtasks /Run /TN Baton
                if ($LASTEXITCODE -eq 0) {{ exit 0 }}
            }}
            Start-Sleep -Seconds 1
        }}
        throw 'Baton scheduled task did not become ready or restart.'
    """
    with (data_dir / "update-restart.log").open("ab") as log:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB)


def cmd_serve(args) -> int:
    import uvicorn

    from .web import create_app

    s = _settings()
    if sys.stdout is None or sys.stderr is None:
        # Started without a console (pythonw.exe, a Windows scheduled task):
        # there is nowhere to print, so the server log goes to a file.
        s.data_dir.mkdir(parents=True, exist_ok=True)
        log = open(s.data_dir / "baton.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    restarting = False

    def restart():
        nonlocal restarting
        if os.name == "nt":
            try:
                windows_restart(s.data_dir)
            except subprocess.SubprocessError as exc:
                raise RuntimeError("Could not prepare scheduled task Baton") from exc
        restarting = True
        server.should_exit = True

    server = uvicorn.Server(uvicorn.Config(create_app(s, restart=restart),
                                           host=args.host, port=args.port, log_level="info"))
    server.run()
    if restarting and os.name != "nt":
        os.execv(sys.executable, [sys.executable, "-m", "baton", *sys.argv[1:]])
    return 0


def cmd_index(args) -> int:
    from .github import Hub, index_markdown

    s = _settings()
    projects, errors = asyncio.run(Hub(s).load(force=True))
    text = index_markdown(projects, lang=args.lang or s.lang)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for e in errors:
        print(f"warning: {e}", file=sys.stderr)
    return 1 if errors and not projects else 0


def cmd_check(args) -> int:
    from .status import parse

    bad = 0
    for path in args.files:
        text = Path(path).read_text(encoding="utf-8")
        st = parse(text)
        if st.problems:
            bad += 1
            for p in st.problems:
                print(f"{path}: {p}")
        else:
            print(f"{path}: ok ({st.name}, {st.status}, {st.priority}, updated {st.updated})")
    return 1 if bad else 0


def cmd_new(args) -> int:
    from .status import template

    target = Path(args.path)
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template(args.name, args.who), encoding="utf-8")
    print(f"created {target}")
    return 0


def cmd_quotas(args) -> int:
    from .quotas import all_windows

    s = _settings()
    print(json.dumps(all_windows(s.data_dir, s.codex_home), indent=2))
    return 0


def cmd_ping(args) -> int:
    from .ping import ping_claude, ping_codex

    s = _settings()
    rc = 0
    if args.what in ("claude", "all"):
        ok, msg = ping_claude(s.data_dir, model=s.ping_model)
        print(f"claude: {msg}")
        rc |= 0 if ok else 1
    if args.what in ("codex", "all"):
        ok, msg = ping_codex()
        print(f"codex: {msg}")
        rc |= 0 if ok else 1
    return rc


def cmd_delegate(args) -> int:
    from . import delegate
    from .agents import opencode_models

    if args.list:
        print("\n".join(opencode_models()) or "No models (is OpenCode installed?)")
        return 0
    if not args.model:
        print("Choose a model with -m (see: baton delegate --list)", file=sys.stderr)
        return 2
    if args.web and args.write:
        # A web page could carry hidden instructions ("prompt injection"); they
        # must never reach a model that has a terminal.
        print("--web and --write can't be used together.", file=sys.stderr)
        return 2
    prompt = sys.stdin.read() if args.prompt in (None, "-") else args.prompt
    if not prompt.strip():
        print("Give the subtask as an argument, or with - read it from stdin.", file=sys.stderr)
        return 2
    if not args.kind.strip():
        print("Give the assignment a type.", file=sys.stderr)
        return 2
    return delegate.run(args.model, prompt, cwd=args.dir, write=args.write, timeout=args.timeout,
                        web=args.web, kind=args.kind.strip())


def cmd_rate(args) -> int:
    from .ranking import cli_ranking

    try:
        cli_ranking().review(args.job, args.agent, args.fulfillment,
                             args.correctness, args.rework, args.comment)
    except KeyError:
        print(f"Unknown delegation #{args.job}.", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"Saved agent review for delegation #{args.job}.")
    return 0


def cmd_statusline(args) -> int:
    from .statusline import main

    return main()


def cmd_monitor_agent(args) -> int:
    from .monitor_agent import run_agent

    url = args.url or os.environ.get("BATON_URL", "")
    token = args.token or os.environ.get("BATON_AGENT_TOKEN", "")
    return run_agent(url, token, interval=args.interval, once=args.once)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="baton", description="A shared project hub for you and your AI agents.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the web page")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("index", help="print (or write) a Markdown index of all projects")
    p.add_argument("-o", "--output")
    p.add_argument("--lang", choices=["en", "es"])
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("check", help="validate STATUS.md files")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("new", help="create a STATUS.md from the template")
    p.add_argument("name")
    p.add_argument("--path", default="STATUS.md")
    p.add_argument("--who", default="human")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("quotas", help="print the current quota windows as JSON")
    p.set_defaults(fn=cmd_quotas)

    p = sub.add_parser("ping", help="refresh quota readings now by asking the CLIs (spends a little quota)")
    p.add_argument("what", nargs="?", default="claude", choices=["claude", "codex", "all"])
    p.set_defaults(fn=cmd_ping)

    p = sub.add_parser("delegate", help="give a subtask to an OpenCode model and print its answer")
    p.add_argument("prompt", nargs="?", help="the subtask; '-' or nothing reads it from stdin")
    p.add_argument("-m", "--model", help="provider/model, e.g. opencode-go/kimi-k3")
    p.add_argument("-C", "--dir", default=".", help="folder it works in (default: here)")
    p.add_argument("--write", action="store_true",
                   help="let it edit files and run commands in that folder (never commit/push)")
    p.add_argument("--web", action="store_true", help="also let it search and read web pages")
    p.add_argument("--timeout", type=int, default=900, help="seconds (default 900)")
    p.add_argument("--list", action="store_true", help="list the models OpenCode offers")
    p.add_argument("--type", dest="kind", default="general", help="assignment type, e.g. test-draft")
    p.set_defaults(fn=cmd_delegate)

    p = sub.add_parser("rate", help="record an agent review after checking a delegation")
    p.add_argument("job", type=int, help="delegation ID printed by baton delegate")
    p.add_argument("--agent", required=True, choices=["claude-code", "codex"])
    p.add_argument("--fulfillment", type=int, required=True, help="assignment fulfillment (1-10)")
    p.add_argument("--correctness", type=int, required=True, help="result correctness (1-10)")
    p.add_argument("--rework", type=int, required=True, help="10 means usable as delivered")
    p.add_argument("--comment", default="", help="optional explanation, up to 500 characters")
    p.set_defaults(fn=cmd_rate)

    p = sub.add_parser("statusline", help="Claude Code status line command (records quota)")
    p.set_defaults(fn=cmd_statusline)

    p = sub.add_parser("monitor-agent", help="send read-only host metrics heartbeats to Baton")
    p.add_argument("--url", default="", help="Baton base URL (or BATON_URL)")
    p.add_argument("--token", default="", help="per-machine token (or BATON_AGENT_TOKEN)")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between heartbeats (default 30)")
    p.add_argument("--once", action="store_true", help="send one heartbeat and exit")
    p.set_defaults(fn=cmd_monitor_agent)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
