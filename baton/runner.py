"""Run a task with an AI agent on this computer, safely, in its own git worktree.

Flow for one task:
  1. find the local clone of the repository (BATON_LOCAL_ROOT / BATON_LOCAL_PATHS);
  2. `git fetch` and create a *worktree* (a second working folder of the same
     repository) on a new branch `baton/<id>`, so the agent never touches the
     folder you work in;
  3. run the agent there with limited permissions:
       claude-code  `claude -p --permission-mode auto` (a classifier reviews actions)
       codex        `codex exec -s workspace-write` (sandbox: writes only there)
       local        `codex exec --oss` with a local model (Ollama or LM Studio)
  4. Baton commits what changed, pushes the branch and opens a
     *draft* pull request for you to review;
  5. the worktree is removed once the branch is safe on GitHub.

A running task can be cancelled from its page: Baton kills the agent and all
its subprocesses, commits and pushes nothing, and removes the worktree and the
local branch unless the branch is already on GitHub.

Everything is written to <data dir>/tasks/<id>.log and tasks.json.
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .ping import _NO_WINDOW, _clean_env, find_claude, find_codex

AGENTS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "local": "IA local",
}

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
                               errors="replace", timeout=60, stdin=subprocess.DEVNULL, **_NO_WINDOW)
            models = [ln.strip() for ln in r.stdout.splitlines()
                      if re.fullmatch(r"[\w.\-]+/[\w.:\-/]+", ln.strip())]
        except (OSError, subprocess.SubprocessError):
            models = []
    _models_cache = (time.time(), models)
    return models

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


def extract_summary(output: str) -> str:
    # Some CLIs colour their output. Ignore incomplete reports and echoed instructions.
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    report = None
    last = ""
    for line in output.splitlines():
        if line.strip() == "BATON_SUMMARY_BEGIN":
            report = []
        elif line.strip() == "BATON_SUMMARY_END":
            if report:
                last = "\n".join(report).strip() or last
            report = None
        elif report is not None:
            report.append(line)
    return last


PROMPT = """You are working on the task below for the repository {repo}.
Baton created this folder for you: a git worktree of {repo} on the new branch
`{branch}`, based on `{base}`. You are not in the owner's own working folder.

Rules:
- First read the repository's AGENTS.md or CLAUDE.md and STATUS.md, and follow them.
- Do the task. Keep the change focused on it.
- Update STATUS.md as the repository's rules say (updated_by: {agent}).
- Do NOT commit, push, switch branches or rewrite history: when you finish,
  Baton commits everything in this folder and opens a draft pull request.
- If the task is unclear, needs secrets, or would need anything destructive or
  outside this folder, stop and explain why instead of doing it.
- Prefix progress updates with "Progress:".
- {summary_request}

Task: {title}

{details}
"""


@dataclass
class Task:
    id: str
    repo: str
    title: str
    details: str
    agent: str
    where: str                      # "local" | "cloud"
    created_at: float
    status: str = "queued"          # queued | running | done | no-changes | failed | cancelled
    branch: str = ""
    base: str = ""
    worktree: str = ""
    pr_url: str = ""
    issue_url: str = ""
    message: str = ""
    finished_at: float | None = None
    open_pr: bool = True            # always True for new tasks; False only in old records
    model: str = ""
    steps: list[str] = field(default_factory=list)
    effort: str = ""
    summary: str = ""
    issue_number: int | None = None


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:30] or "task"


def _remote_repo(url: str) -> str:
    m = re.search(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$", url.strip())
    return m.group(1).lower() if m else ""


class LocalRepos:
    """Maps owner/name -> the folder of its clone on this computer."""

    def __init__(self, root: str, overrides: str):
        self.root = Path(root) if root else None
        self.overrides: dict[str, Path] = {}
        for part in overrides.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                self.overrides[k.strip().lower()] = Path(v.strip())
        self._cache: tuple[float, dict[str, Path]] | None = None

    def _scan(self) -> dict[str, Path]:
        found: dict[str, list[Path]] = {}
        if self.root and self.root.is_dir():
            for d in self.root.iterdir():
                if not (d / ".git").exists():
                    continue
                try:
                    url = subprocess.run(["git", "-C", str(d), "remote", "get-url", "origin"],
                                         capture_output=True, text=True, timeout=10,
                                         **_NO_WINDOW).stdout
                except (OSError, subprocess.SubprocessError):
                    continue
                repo = _remote_repo(url)
                if repo:
                    found.setdefault(repo, []).append(d)
        result = {}
        for repo, dirs in found.items():
            name = repo.split("/")[1]
            exact = [d for d in dirs if d.name.lower() == name]
            # Prefer the folder named like the repo; otherwise the shortest name
            # (e.g. "Mamporro-git" over "Mamporro-respaldo-u2").
            result[repo] = (exact or sorted(dirs, key=lambda d: len(d.name)))[0]
        return result

    def get(self, repo: str) -> Path | None:
        repo = repo.lower()
        if repo in self.overrides:
            return self.overrides[repo]
        if not self._cache or time.time() - self._cache[0] > 300:
            self._cache = (time.time(), self._scan())
        return self._cache[1].get(repo)


class TaskStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self.file = data_dir / "tasks.json"
        self.logs = data_dir / "tasks"
        self.lock = threading.Lock()

    def all(self) -> list[Task]:
        try:
            rows = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for r in rows:
            try:
                out.append(Task(**r))
            except TypeError:
                continue
        return sorted(out, key=lambda t: t.created_at, reverse=True)

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self.all() if t.id == task_id), None)

    def save(self, task: Task) -> None:
        with self.lock:
            tasks = {t.id: t for t in self.all()}
            tasks[task.id] = task
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(t) for t in tasks.values()], indent=1), encoding="utf-8")
            tmp.replace(self.file)

    def log_path(self, task_id: str) -> Path:
        self.logs.mkdir(parents=True, exist_ok=True)
        return self.logs / f"{task_id}.log"

    def read_log(self, task_id: str) -> str:
        try:
            return self.log_path(task_id).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def log_tail(self, task_id: str, lines: int = 200) -> str:
        return "\n".join(self.read_log(task_id).splitlines()[-lines:])


class Cancelled(Exception):
    """Raised inside a task's thread when the owner cancelled it."""


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and every process it started.

    `proc.kill()` only stops the direct child; Claude Code, Codex and OpenCode
    start their own subprocesses (node, shells, tools) that would keep running.
    On Windows `taskkill /T` walks the tree of child processes; elsewhere the
    agent runs in its own process group (start_new_session) and the whole
    group gets the signal.
    """
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=30, stdin=subprocess.DEVNULL, **_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()  # last resort: at least the direct child


def new_id(title: str) -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + _slug(title)


class Runner:
    def __init__(self, store: TaskStore, repos: LocalRepos, *, timeout: int, local_model: str,
                 local_provider: str, git_name: str, git_email: str,
                 open_pr: Callable[[str, str, str, str, str], str]):
        self.store = store
        self.repos = repos
        self.timeout = timeout
        self.local_model = local_model
        self.local_provider = local_provider
        self.git_name = git_name
        self.git_email = git_email
        self.open_pr = open_pr        # (repo, branch, base, title, body) -> url
        # Shared between the web thread (cancel) and each task's thread. The
        # lock makes "is it cancelled?" plus "start the agent" (or "start
        # committing") a single step, so a cancel can never slip in between.
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._committing: set[str] = set()

    # ---------------------------------------------------------- availability
    def agents_available(self) -> dict[str, str]:
        """agent -> "" if usable, else the reason it isn't."""
        out = {}
        out["claude-code"] = "" if find_claude() else "Claude Code CLI not found"
        codex = find_codex()
        out["codex"] = "" if codex else "Codex CLI not found"
        out["opencode"] = "" if find_opencode() else "OpenCode CLI not found"
        out["local"] = ("" if (codex and self.local_model)
                        else "set BATON_LOCAL_MODEL (and install Ollama or LM Studio)")
        return out

    def busy(self, repo: str) -> bool:
        return any(t.repo == repo and t.where == "local" and t.status in ("queued", "running")
                   for t in self.store.all())

    def start(self, task: Task) -> None:
        if task.agent == "local":
            task.model = self.local_model
        self.store.save(task)
        thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        with self._lock:
            self._threads[task.id] = thread
        thread.start()

    # ------------------------------------------------------------ cancelling
    def can_cancel(self, task_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(task_id)
            return bool(thread and thread.is_alive() and task_id not in self._committing
                        and task_id not in self._cancelled)

    def cancel(self, task_id: str, wait: float = 30) -> bool:
        """Stop a running local task. False if it isn't running here (or is
        already committing its result, which only takes a moment)."""
        with self._lock:
            thread = self._threads.get(task_id)
            if not thread or not thread.is_alive() or task_id in self._committing:
                return False
            self._cancelled.add(task_id)
            proc = self._procs.get(task_id)
        if proc:
            kill_tree(proc)
        # The task's own thread cleans up and saves the "cancelled" status
        # (one writer per task); wait a little so the page shows the result.
        thread.join(wait)
        return True

    def _check_cancelled(self, task: Task) -> None:
        with self._lock:
            if task.id in self._cancelled:
                raise Cancelled()

    def _branch_published(self, repo_dir: Path, branch: str) -> bool | None:
        """True if the branch exists on origin, False if not, None if unknown."""
        try:
            r = subprocess.run(["git", "-C", str(repo_dir), "ls-remote", "--exit-code", "--heads",
                                "origin", f"refs/heads/{branch}"], capture_output=True, text=True,
                               timeout=60, stdin=subprocess.DEVNULL, **_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0:
            return True
        return False if r.returncode == 2 else None  # 2 = "no matching ref"

    def _discard(self, task: Task, repo_dir: Path | None, wt: Path | None) -> str:
        """After a cancel: drop the worktree and the local branch, unless the
        branch is on GitHub. Returns what happened, for the task's message."""
        if not repo_dir or not wt:
            return "Nothing had been created yet."
        published = self._branch_published(repo_dir, task.branch)
        if published is not False:
            why = "is on GitHub" if published else "could not be checked on GitHub"
            return f"The branch {task.branch} {why}, so its worktree and branch were kept."
        # Windows may keep files of the killed processes open for a moment.
        for _ in range(10):
            self._git(repo_dir, "worktree", "remove", "--force", str(wt), check=False)
            if not wt.exists():
                break
            time.sleep(0.5)
        if wt.exists():
            return f"The worktree could not be removed; delete it by hand: {wt}"
        self._git(repo_dir, "worktree", "prune", check=False)
        self._git(repo_dir, "branch", "-D", task.branch, check=False)
        task.worktree = ""
        return "Nothing was committed or pushed; the worktree and the local branch were removed."

    # --------------------------------------------------------------- running
    def _log(self, task: Task, text: str) -> None:
        with open(self.store.log_path(task.id), "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")

    @staticmethod
    def _read_summary(log_path: Path, start: int) -> str:
        """The agent's final report, from the part of the log this run wrote."""
        try:
            with open(log_path, "rb") as log:
                end = log.seek(0, 2)
                log.seek(max(start, end - 65536))
                return extract_summary(log.read().decode("utf-8", errors="replace"))
        except OSError:
            return ""

    def _git(self, cwd: Path, *args: str, check: bool = True) -> str:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, **_NO_WINDOW)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
        return r.stdout.strip()

    def _agent_cmd(self, task: Task, wt: Path) -> list[str]:
        if task.agent == "opencode":
            # The prompt goes as the message argument (opencode.exe directly, no shell).
            return [find_opencode() or "opencode", "run", "--standalone", "--auto", "--agent", "baton",
                    "-m", task.model, "--title", f"Baton {task.id}", self._prompt(task)]
        if task.agent == "claude-code":
            cmd = [find_claude() or "claude", "-p", "--permission-mode", "auto"]
            if task.model:
                cmd.extend(["--model", task.model])
            if task.effort:
                cmd.extend(["--effort", task.effort])
            return cmd
        codex = find_codex() or "codex"
        cmd = [codex, "exec", "-s", "workspace-write", "--color", "never", "-C", str(wt)]
        if task.agent == "local":
            cmd[2:2] = ["--oss", "--local-provider", self.local_provider, "-m", self.local_model]
        elif task.agent == "codex":
            if task.model:
                cmd.extend(["-m", task.model])
            if task.effort:
                cmd.extend(["-c", f"model_reasoning_effort={json.dumps(task.effort)}"])
        return cmd + ["-"]

    def _prompt(self, task: Task) -> str:
        return PROMPT.format(repo=task.repo, branch=task.branch, base=task.base,
                             agent=task.agent, title=task.title, summary_request=SUMMARY_REQUEST,
                             details=task.details or "(no more details)")

    def _run(self, task: Task) -> None:
        def step(msg: str) -> None:
            task.steps.append(f"{time.strftime('%H:%M:%S')} {msg}")
            self._log(task, f"== {msg}")
            self.store.save(task)

        task.status = "running"
        repo_dir: Path | None = None
        wt: Path | None = None          # set once the worktree exists
        try:
            repo_dir = self.repos.get(task.repo)
            if not repo_dir:
                raise RuntimeError("No local clone of this repository (check BATON_LOCAL_ROOT).")
            step(f"Local clone: {repo_dir}")
            self._git(repo_dir, "fetch", "origin", task.base)
            self._check_cancelled(task)
            path = self.store.dir / "worktrees" / f"{task.repo.split('/')[1]}-{task.id}"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._git(repo_dir, "worktree", "add", "-b", task.branch, str(path), f"origin/{task.base}")
            wt = path
            task.worktree = str(wt)
            step(f"Worktree on branch {task.branch} (from origin/{task.base})")

            label = AGENTS.get(task.agent, task.agent) + (f" ({task.model})" if task.model else "")
            step(f"Running {label}…")
            env = _clean_env()
            if task.agent == "opencode":
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(OPENCODE_CONFIG)
            # Claude and Codex read the prompt from stdin; OpenCode gets it as an
            # argument, and its stdin is closed so it never waits for input.
            stdin_text = "" if task.agent == "opencode" else self._prompt(task)
            # Popen (not run) so that cancel() can reach the process while it works.
            group = {} if os.name == "nt" else {"start_new_session": True}
            log_path = self.store.log_path(task.id)
            start = log_path.stat().st_size if log_path.exists() else 0
            with open(log_path, "a", encoding="utf-8") as log:
                with self._lock:
                    if task.id in self._cancelled:
                        raise Cancelled()
                    proc = subprocess.Popen(self._agent_cmd(task, wt), stdin=subprocess.PIPE,
                                            stdout=log, stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace", cwd=str(wt),
                                            env=env, **group, **_NO_WINDOW)
                    self._procs[task.id] = proc
                try:
                    proc.communicate(input=stdin_text, timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    kill_tree(proc)
                    raise
                except OSError:
                    # The pipe broke: the agent was killed while reading its prompt.
                    proc.wait()
                finally:
                    with self._lock:
                        self._procs.pop(task.id, None)
            # Only the end of this agent run can contain its final report.
            task.summary = self._read_summary(log_path, start)
            self._check_cancelled(task)
            step(f"Agent finished (exit code {proc.returncode})")
            if proc.returncode != 0:
                raise RuntimeError(f"The agent stopped with exit code {proc.returncode}; see the log.")

            if not self._git(wt, "status", "--porcelain"):
                task.status = "no-changes"
                task.message = "The agent finished without changing anything."
                self._git(repo_dir, "worktree", "remove", "--force", str(wt), check=False)
                self._git(repo_dir, "branch", "-D", task.branch, check=False)
                task.worktree = ""
                return
            with self._lock:
                if task.id in self._cancelled:
                    raise Cancelled()
                self._committing.add(task.id)  # from here on, cancel() says no
            self._git(wt, "add", "-A")
            ident = []
            if not self._git(wt, "config", "user.email", check=False):
                ident = ["-c", f"user.name={self.git_name}", "-c", f"user.email={self.git_email}"]
            body = f"Task from Baton, done by {AGENTS.get(task.agent, task.agent)}.\n\n{task.details}".strip()
            self._git(wt, *ident, "commit", "-m", f"{task.title}\n\n{body}")
            step("Changes committed")

            if task.open_pr:
                self._git(wt, "push", "-u", "origin", task.branch)
                step("Branch pushed to GitHub")
                pr_body = (f"Task sent from Baton to **{AGENTS.get(task.agent, task.agent)}** "
                           f"on my computer.\n\n**Task:** {task.title}\n\n{task.details}\n\n"
                           "Review it before merging.")
                task.pr_url = self.open_pr(task.repo, task.branch, task.base, task.title, pr_body)
                step(f"Draft pull request: {task.pr_url}")
                self._git(repo_dir, "worktree", "remove", "--force", str(wt), check=False)
                task.worktree = ""
                task.message = "Done: review the draft pull request."
            else:
                task.message = f"Done: the changes are on the local branch {task.branch} in {wt}."
            task.status = "done"
        except Cancelled:
            step("Cancelled: the agent and its subprocesses were stopped")
            task.status = "cancelled"
            try:
                task.message = "Cancelled. " + self._discard(task, repo_dir, wt)
            except Exception as exc:  # the cancel itself already happened
                task.message = f"Cancelled, but the clean-up failed: {str(exc)[:300]}"
            self._log(task, f"!! {task.message}")
        except subprocess.TimeoutExpired:
            task.status = "failed"
            task.message = f"The agent took longer than {self.timeout // 60} minutes and was stopped."
        except Exception as exc:  # report every failure on the page
            task.status = "failed"
            task.message = str(exc)[:500]
            self._log(task, f"!! {task.message}")
        finally:
            task.finished_at = time.time()
            self.store.save(task)
            with self._lock:
                self._cancelled.discard(task.id)
                self._committing.discard(task.id)
                self._threads.pop(task.id, None)
