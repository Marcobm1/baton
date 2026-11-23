"""Command line: python -m baton <command>  (or just `baton <command>`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _settings():
    from .config import Settings

    return Settings.from_env()


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
    uvicorn.run(create_app(s), host=args.host, port=args.port, log_level="info")
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
    from .runner import opencode_models

    if args.list:
        print("\n".join(opencode_models()) or "No models (is OpenCode installed?)")
        return 0
    if not args.model:
        print("Choose a model with -m (see: baton delegate --list)", file=sys.stderr)
        return 2
    prompt = sys.stdin.read() if args.prompt in (None, "-") else args.prompt
    if not prompt.strip():
        print("Give the subtask as an argument, or with - read it from stdin.", file=sys.stderr)
        return 2
    return delegate.run(args.model, prompt, cwd=args.dir, write=args.write, timeout=args.timeout)


def cmd_statusline(args) -> int:
    from .statusline import main

    return main()


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
    p.add_argument("--timeout", type=int, default=900, help="seconds (default 900)")
    p.add_argument("--list", action="store_true", help="list the models OpenCode offers")
    p.set_defaults(fn=cmd_delegate)

    p = sub.add_parser("statusline", help="Claude Code status line command (records quota)")
    p.set_defaults(fn=cmd_statusline)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
