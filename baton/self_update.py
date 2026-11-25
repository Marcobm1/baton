"""Update the checkout used by the server, with exclusive local task admission."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .git_lock import GitLockError, is_lock_error, run_locked
from .runner import _remote_repo


def command(root, argv, label, timeout=120):
    # Git URLs, pip configuration and test output can contain private data.
    # Only record the failing operation and exit status.
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, timeout=timeout,
                                encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                     "BATON_SELF_UPDATE": "0"})
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{label} timed out") from None
    if result.returncode:
        if argv[0] == "git" and is_lock_error(result.stderr):
            raise GitLockError(f"{label} (exit {result.returncode}): Git lock contention")
        detail = ""
        if label == "pytest -q":
            # Only test identifiers, never parameter values or captured output.
            failed = re.findall(r"^FAILED ([\w./-]+\.py::[\w:]+)", result.stdout, re.M)
            if failed:
                detail = ": " + ", ".join(failed[:3])
        raise RuntimeError(f"{label} (exit {result.returncode}){detail}")
    return result.stdout.strip()


class SelfUpdate:
    def __init__(self, root, runner, enabled, restart):
        self.root, self.runner, self.restart = root, runner, restart
        self.python = Path(sys.executable)
        if self.python.name.lower() == "pythonw.exe":
            self.python = self.python.with_name("python.exe")
        self.history = runner.store.dir / "updates.jsonl"
        self.checked = None
        self.pending = ""
        self.error = ""
        self.failed_target = ""
        self._force_retry = False
        self.running = ""
        self.repo = ""
        self.reason = "off"
        if (root / ".git").exists():
            try:
                self.running = self.git("rev-parse", "HEAD")
                if enabled and restart:
                    self.repo = _remote_repo(self.git("remote", "get-url", "origin"))
                    self.reason = "ready"
            except (OSError, RuntimeError) as exc:
                self.error = str(exc)
        self.enabled = bool(enabled and restart and self.running)
        self.restarting = False
        if self.enabled and self.history.is_file():
            try:
                self.restore_history()
            except OSError as exc:
                self.error = str(exc)

    def request_manual(self):
        """Force the next check() to retry even if origin/main is on a previously failed SHA."""
        self._force_retry = True

    def restore_history(self):
        last = {}
        with self.history.open(encoding="utf-8") as log:
            for line in log:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("result"), str):
                    last = entry
                    # Only the newest failure counts: a later successful update
                    # clears it, so Quotas doesn't show an old error as current.
                    if last["result"] in ("validated", "running"):
                        self.error = ""
                        self.failed_target = ""
                    else:
                        self.error = last["result"]
                        self.failed_target = str(last.get("new") or "")
        if last.get("result") == "validated" and last.get("new") == self.running:
            self.record(last.get("old"), self.running, "running")

    def git(self, *args):
        return run_locked(self.root, lambda: command(self.root, ["git", *args], "git " + args[0]))

    def record(self, old, new, result):
        self.history.parent.mkdir(parents=True, exist_ok=True)
        with self.history.open("a", encoding="utf-8") as log:
            log.write(json.dumps(dict(date=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                      old=old, new=new, result=result)) + "\n")

    def install(self):
        command(self.root, [self.python, "-m", "pip", "install", "-e", ".[dev]"],
                "pip install", timeout=600)

    def validate(self):
        command(self.root, [self.python, "-m", "pytest", "-q"], "pytest -q", timeout=900)

    def check(self, manual: bool = False):
        """Called by one worker shared by the timer, task completion and POST."""
        if not self.enabled or self.restarting:
            return
        force = manual or self._force_retry
        self._force_retry = False
        self.checked = time.time()
        try:
            self.git("fetch", "origin", "+refs/heads/main:refs/remotes/origin/main")
            old = self.git("rev-parse", "HEAD")
            target = self.git("rev-parse", "origin/main")
            self.pending = target if self.git("rev-list", "-1", "HEAD..origin/main") else ""
            if self.git("branch", "--show-current") != "main":
                self.reason = "branch"
            elif self.git("status", "--porcelain", "--untracked-files=all"):
                self.reason = "dirty"
            elif old != self.running:
                self.reason = "moved"
            elif not self.pending:
                self.reason = "current"
            elif not force and self.failed_target and target == self.failed_target:
                self.reason = "skipped"
            elif sys.prefix == sys.base_prefix:
                self.reason = "venv"
            elif not self.runner.reserve_update():
                self.reason = "busy"
            else:
                try:
                    self.apply(old, target)
                finally:
                    if not self.restarting:
                        self.runner.release_update()
        except (OSError, RuntimeError) as exc:
            self.error = str(exc)
            self.reason = "failed"

    def apply(self, old, target):
        new = None
        dependencies = False
        try:
            self.reason = "testing"
            if self.git("rev-list", "-1", f"{target}..{old}"):
                raise RuntimeError("Local main and origin/main have diverged; fast-forward is impossible")
            # Check again after reserving the queue, immediately before the pull.
            if (self.git("branch", "--show-current") != "main" or
                    self.git("rev-parse", "HEAD") != old or self.git("status", "--porcelain")):
                raise RuntimeError("Checkout changed during the check")
            self.git("pull", "--ff-only", "--no-rebase", "--no-autostash", "origin", "main")
            new = self.git("rev-parse", "HEAD")
            self.pending = new
            # The remote may have advanced again during pull.
            dependencies = bool(self.git("diff", "--name-only", old, new, "--", "pyproject.toml"))
            if dependencies:
                self.install()
            self.validate()
            if (self.git("branch", "--show-current") != "main" or
                    self.git("rev-parse", "HEAD") != new or self.git("status", "--porcelain")):
                raise RuntimeError("Checkout changed during validation")
            self.failed_target = ""
            self.error = ""
            self.record(old, new, "validated")
            self.restart()
            self.restarting = True
            self.reason = "restart"
        except (OSError, RuntimeError) as exc:
            result = str(exc)
            if new and new != old:
                try:
                    if self.git("branch", "--show-current") != "main" or self.git("rev-parse", "HEAD") != new:
                        raise RuntimeError("Checkout moved before rollback")
                    if self.git("diff", "--cached", "--name-only"):
                        raise RuntimeError("Index changed before rollback")
                    self.git("reset", "--keep", old)
                    if dependencies:
                        self.install()
                    result += " (rolled back)"
                except (OSError, RuntimeError) as rollback:
                    result += f"; rollback failed: {rollback}"
            self.failed_target = new or target
            self.record(old, self.failed_target, result)
            raise RuntimeError(result) from exc
