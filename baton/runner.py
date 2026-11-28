"""Run a task with an AI agent on this computer, safely, in its own git worktree.

Flow for one task:
  1. find the local clone of the repository (BATON_LOCAL_ROOT / BATON_LOCAL_PATHS);
  2. `git fetch` and create a *worktree* (a second working folder of the same
     repository) on a new branch `baton/<id>`, so the agent never touches the
     folder you work in;
  3. run the agent there. Claude Code, Codex and Gemini get full permissions
     (the owner's decision, 2026-11-25: they work unattended in the worktree);
     OpenCode and local AI keep Baton's limits;
  4. Baton commits what changed, pushes the branch and opens a pull request;
  5. with auto-merge on (the default), Baton waits for the repository's tests
     on GitHub, reviews OpenCode/local-AI work with Claude Code, and merges;
  6. the worktree is removed once the branch is safe on GitHub.

Modularized responsibilities:
  - `baton/task_store.py`: Task dataclass, queue ordering, TaskStore, exceptions
  - `baton/worktree.py`:   LocalRepos clone discovery, Git helpers, worktree cleanup
  - `baton/agents.py`:     agent configs, prompts, model discovery, CLI command builder
  - `baton/review_merge.py`: CI waiting, AI review execution, PR auto-merge
  - `baton/runner.py`:     Runner orchestrator (queue, dispatch, cancel, execution loop)
"""

from __future__ import annotations

import json
import os
import shutil as shutil  # re-exported for tests patching runner.shutil.which
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from .agents import (
    AGENTS as AGENTS,
    ALWAYS_DENIED as ALWAYS_DENIED,
    NEXT_TASK_REQUEST as NEXT_TASK_REQUEST,
    NO_GIT_CHANGES as NO_GIT_CHANGES,
    OPENCODE_CONFIG as OPENCODE_CONFIG,
    PROMPT as PROMPT,
    READ_RULES as READ_RULES,
    RECOMMEND_REQUEST as RECOMMEND_REQUEST,
    REVIEW_PROMPT as REVIEW_PROMPT,
    REVIEWED_AGENTS as REVIEWED_AGENTS,
    SUMMARY_REQUEST as SUMMARY_REQUEST,
    TASK_RULES as TASK_RULES,
    _rule as _rule,
    agy_events as agy_events,
    available_agents,
    build_agent_cmd,
    build_prompt,
    cached_opencode_models as cached_opencode_models,
    extract_summary as extract_summary,
    find_opencode as find_opencode,
    format_model_choices,
    next_task_request as next_task_request,
    opencode_models as opencode_models,
)
from .artifacts import collect_worktree_artifacts
from .git_lock import GitLockTimeout, run_locked
from .ping import _NO_WINDOW, agent_env, find_agy, find_claude, find_codex
from .proc import attach_job, close_job, kill_tree
from .ranking import Ranking, review_verdict as review_verdict
from .review_merge import finish_pr, pick_reviewer, resolve_reviewer_model, run_review, wait_for_tests
from .task_map import execution_items, execution_order
from .task_models import catalog
from .task_store import (
    PRIORITIES as PRIORITIES,
    Cancelled as Cancelled,
    LeftOpen as LeftOpen,
    Task as Task,
    TaskStore as TaskStore,
    UpdateInProgress as UpdateInProgress,
    _slug as _slug,
    new_id as new_id,
    pr_number as pr_number,
    queue_key as queue_key,
)
from .usage import record_gemini
from .worktree import (
    LocalRepos as _BaseLocalRepos,
    _remote_repo as _remote_repo,
    branch_published,
    discard_worktree,
    git_run,
    parse_numstat,
)


class LocalRepos(_BaseLocalRepos):
    """LocalRepos wired to runner._remote_repo so legacy test patches keep working."""

    def _parse_remote(self, url: str) -> str:
        return _remote_repo(url)


class Runner:
    def __init__(self, store: TaskStore, repos: _BaseLocalRepos, *, timeout: int, local_model: str,
                 local_provider: str, git_name: str, git_email: str,
                 open_pr: Callable[[str, str, str, str, str], str], github=None,
                 auto_merge: bool = False,
                 quota_left: Callable[[str], float | None] | None = None,
                 max_parallel: int = 3,
                 review_models: dict[str, tuple[str, str]] | None = None,
                 owner_tag: str = "(Owner)"):
        self.store = store
        # Reviewer -> (model, effort); "" keeps the CLI's own default.
        self.review_models = review_models or {}
        self.owner_tag = owner_tag
        self.max_parallel = max(1, max_parallel)   # local tasks at once on this computer
        self.repos = repos
        self.timeout = timeout
        self.local_model = local_model
        self.local_provider = local_provider
        self.git_name = git_name
        self.git_email = git_email
        self.open_pr = open_pr        # (repo, branch, base, title, body) -> url
        # Auto-merge: `github` has checks(repo, sha), comment(repo, n, body)
        # and merge(repo, n, sha); quota_left(agent) is the % left in its 5 h window.
        self.github = github
        self.auto_merge = auto_merge and github is not None
        self.quota_left = quota_left
        self.ci_poll = 15             # seconds between looks at the tests
        self.ci_grace = 120           # no test at all after this long = no tests
        self.ci_timeout = 30 * 60     # give up waiting after this long
        # Shared between the web thread (cancel) and each task's thread. The
        # lock makes "is it cancelled?" plus "start the agent" (or "start
        # committing") a single step, so a cancel can never slip in between.
        self._lock = threading.Lock()
        self._updating = False
        self._overheated = False
        self._thermal_stops: set[str] = set()
        self.on_finished: Callable[[Task], None] | None = None
        # The same lock also guards the queue: while a task is "queued" the
        # queue is its only writer (start, priority change, cancel), always
        # under this lock; once "running", its own thread is.
        self._threads: dict[str, threading.Thread] = {}
        self._active: dict[str, Task] = {}          # running tasks by id
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._committing: set[str] = set()

    # ---------------------------------------------------------- availability
    def agents_available(self) -> dict[str, str]:
        return available_agents(
            local_model=self.local_model,
            claude_bin=find_claude,
            codex_bin=find_codex,
            agy_bin=find_agy,
            opencode_bin=find_opencode,
        )

    # ----------------------------------------------------------------- queue
    def queued(self) -> list[Task]:
        """Local tasks waiting to start, in the order they will start."""
        return sorted((t for t in self.store.all() if t.where == "local" and t.status == "queued"),
                      key=queue_key)

    def reserve_update(self) -> bool:
        with self._lock:
            if self._updating or self._active or any(
                t.where == "local" and t.status in ("running", "queued") for t in self.store.all()
            ):
                return False
            self._updating = True
            return True

    def release_update(self) -> None:
        with self._lock:
            self._updating = False

    def submit(self, task: Task) -> None:
        """Put a local task in the queue; it starts as soon as its repository
        and one of the `max_parallel` slots are free (maybe right now)."""
        self.submit_batch([task])

    def submit_batch(self, tasks: list[Task], expected_runs: dict[str, str] | None = None) -> None:
        with self._lock:
            if self._updating:
                raise UpdateInProgress
            history = self.store.all() if any(t.status_task_id for t in tasks) else []
            active = {(t.status_project_id, t.status_task_id) for t in history if t.status in ("running", "queued")}
            if expected_runs and tasks:
                latest = self.store.latest_map_runs(history, tasks[0].status_project_id)
                if any(latest.get(key, "") != value for key, value in expected_runs.items()):
                    raise ValueError("A checklist item has been queued since the preview. Preview again.")
            for task in tasks:
                if task.status_task_id and (task.status_project_id, task.status_task_id) in active:
                    raise ValueError(f"{task.status_task_id} is already queued or running.")
                if task.agent == "local" and not task.model:
                    task.model = self.local_model
                task.status = "queued"
            self.store.save_many(tasks)
        self.dispatch()

    def refresh_map_merge(self, task: Task) -> None:
        """Discover a manual merge after Baton left a bound task's PR open."""
        if not task.status_task_id or task.status != "done" or task.merged or not task.pr_url:
            return
        try:
            merged = self.github.pr_merged(task.repo, pr_number(task.pr_url))
        except Exception:
            return  # Keep waiting while GitHub is unavailable.
        if merged:
            task.merged = True
            self.store.save(task)

    def dispatch(self) -> None:
        """Start queued tasks while there are free slots. Called when a task
        is submitted, when one finishes and when Baton starts."""
        # Network reads must not hold the lock used to cancel a running agent.
        snapshot = self.store.all()
        candidates = sorted((t for t in snapshot if t.where == "local" and t.status == "queued"), key=queue_key)
        required_runs = {run for t in candidates for run in t.predecessor_runs}
        for parent in snapshot:
            if parent.id in required_runs:
                self.refresh_map_merge(parent)
        stopped = []
        maps = {}
        for pid in {t.status_project_id for t in candidates if t.status_project_id}:
            try:
                maps[pid] = execution_items(self.github.map_source(pid), self.owner_tag)
            except ValueError as exc:
                maps[pid] = f"Waiting for a valid checklist: {exc}"
            except Exception:
                maps[pid] = "Waiting for the current project checklist."
        with self._lock:
            busy = {t.repo.casefold() for t in self._active.values()}
            records = {t.id: t for t in self.store.all()}
            for candidate in candidates:
                task = records.get(candidate.id)
                if not task or task.status != "queued":
                    continue
                if task.status_task_id:
                    parents = [records.get(run) for run in task.predecessor_runs]
                    failed = next((p for p in parents if p and p.status in ("failed", "cancelled")), None)
                    if failed:
                        task.status, task.finished_at = "cancelled", time.time()
                        task.message = f"Chain stopped because {failed.status_task_id} was {failed.status}."
                        self.store.save(task)
                        stopped.append(task)
                        continue
                    items = maps[task.status_project_id]
                    if isinstance(items, dict) and task.status_task_id in items and items[task.status_task_id].done:
                        task.status, task.finished_at = "no-changes", time.time()
                        task.message = "Checklist item already completed while queued."
                        self.store.save(task)
                        continue
                    reason = items if isinstance(items, str) else self._map_wait(task, items)
                    if not reason and any(p is None or not p.finished for p in parents):
                        reason = "Waiting for predecessor runs to finish."
                    if not reason and any(p.pr_url and not p.merged for p in parents):
                        reason = "Waiting for predecessor pull requests to merge."
                    if reason:
                        if reason != task.message:
                            task.message = reason
                            self.store.save(task)
                        continue
                    task.message = ""
                if self._updating or self._overheated or len(self._active) >= self.max_parallel:
                    continue
                repo_key = task.repo.casefold()
                if repo_key in busy:       # one task at a time per repository
                    continue
                if task.agent == "local" and not task.model:
                    task.model = self.local_model
                task.status, task.started_at = "running", time.time()
                self.store.save(task)
                thread = threading.Thread(target=self._run, args=(task,), daemon=True)
                self._threads[task.id] = thread
                self._active[task.id] = task
                self._cancel_events[task.id] = threading.Event()
                busy.add(repo_key)
                thread.start()
        if self.on_finished:
            for task in stopped:
                self.on_finished(task)

    def _map_wait(self, task, items):
        try:
            order = execution_order(items, task.status_task_id)
        except ValueError as exc:
            return f"Waiting for a valid checklist: {exc}"
        item = order[-1]
        if item.owner:
            return "Waiting at an owner checkpoint."
        if (item.repo or "/".join(task.status_project_id.split("/")[:2])).casefold() != task.repo.casefold():
            return "Checklist destination changed. Cancel and queue again."
        waiting = [t.id for t in order[:-1] if not t.done]
        if waiting:
            return "Waiting for checklist items: " + ", ".join(waiting)
        return ""

    def set_priority(self, task_id: str, priority: str) -> bool:
        """Change the priority of a queued task. False if it isn't queued."""
        if priority not in PRIORITIES:
            raise ValueError(f"Unknown priority: {priority}")
        with self._lock:
            task = self.store.get(task_id)
            if not task or task.where != "local" or task.status != "queued":
                return False
            task.priority = priority
            self.store.save(task)
            return True

    def recover(self) -> None:
        """After a restart: tasks that were running can't be resumed (their
        processes belonged to the previous run), so they are marked failed.
        Queued tasks keep their place; dispatch() starts them."""
        with self._lock:
            for old in self.store.all():
                if old.status == "running":
                    old.status, old.message = "failed", "Baton stopped while this task was running."
                    old.finished_at = old.finished_at or time.time()
                    self.store.save(old)
                else:
                    self.store.record_task(old)

    def queue_info(self) -> dict[str, dict]:
        """For each queued task: its place in its repository's queue (1 = next)
        and what it waits for: the queued task just ahead of it, else the task
        running in that repository, else (None) a free slot on this computer."""
        tasks = self.store.all()
        running = {t.repo.casefold(): t for t in tasks if t.where == "local" and t.status == "running"}
        slots = sum(1 for t in tasks if t.where == "local" and t.status == "running")
        info: dict[str, dict] = {}
        ahead: dict[str, Task] = {}
        count: dict[str, int] = {}
        for task in self.queued():
            key = task.repo.casefold()
            count[key] = count.get(key, 0) + 1
            info[task.id] = {"position": count[key],
                             "waiting_for": ahead.get(key) or running.get(key),
                             "running": slots, "max": self.max_parallel}
            ahead[key] = task
        return info

    # ------------------------------------------------------------ cancelling
    def set_overheated(self, hot: bool) -> None:
        with self._lock:
            was_hot = self._overheated
            self._overheated = hot
            if hot:
                self._thermal_stops.update(self._active)
                self._cancelled.update(self._active)
                for ev in self._cancel_events.values():
                    ev.set()
                processes = list(self._procs.values())
        if hot:
            for proc in processes:
                if proc.poll() is None:
                    kill_tree(proc)
        elif was_hot:
            self.dispatch()

    def can_cancel(self, task_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(task_id)
            if not thread:
                task = self.store.get(task_id)
                return bool(task and task.where == "local" and task.status == "queued")
            return bool(thread.is_alive() and task_id not in self._committing
                        and task_id not in self._cancelled)

    def _cancel_queued(self, task_id: str) -> bool:
        """Take a queued task out of the queue. Nothing exists yet for it (no
        worktree, no branch), so there is nothing to clean up."""
        with self._lock:
            task = self.store.get(task_id)
            if not task or task.where != "local" or task.status != "queued":
                return False
            task.status, task.finished_at = "cancelled", time.time()
            task.message = "Cancelled while queued: it never started, so no worktree or branch was created."
            self.store.save(task)
        self._log(task, f"!! {task.message}")
        return True

    def cancel(self, task_id: str, wait: float = 30) -> bool:
        """Cancel a queued or running local task. False if it isn't queued or
        running here (or is already committing its result, which only takes
        a moment)."""
        if self._cancel_queued(task_id):
            self.dispatch()
            return True
        with self._lock:
            thread = self._threads.get(task_id)
            if not thread or not thread.is_alive() or task_id in self._committing:
                return False
            self._cancelled.add(task_id)
            ev = self._cancel_events.get(task_id)
            if ev is not None:
                ev.set()
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
        return branch_published(repo_dir, branch)

    def _discard(self, task: Task, repo_dir: Path | None, wt: Path | None) -> str:
        return discard_worktree(
            task,
            repo_dir,
            wt,
            write_git=self._write_git,
            check_published=self._branch_published,
            sleep=time.sleep,
        )

    # --------------------------------------------------------------- logging
    def _log(self, task: Task, text: str) -> None:
        with open(self.store.log_path(task.id), "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")

    @staticmethod
    def _read_output(log_path: Path, start: int, limit: int = 65536) -> str:
        """The tail (last `limit` bytes) of the output written by this agent run."""
        try:
            with open(log_path, "rb") as log:
                end = log.seek(0, 2)
                log.seek(max(start, end - limit))
                return log.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    # ------------------------------------------------------------ auto-merge
    def _finish_pr(self, task: Task, repo_dir: Path, wt: Path, ident: list[str], step) -> None:
        finish_pr(self, task, repo_dir, wt, ident, step)

    def _wait_for_tests(self, task: Task, sha: str, step, must_exist: bool = False) -> str:
        return wait_for_tests(self, task, sha, step, must_exist=must_exist)

    def _pick_reviewer(self, task: Task) -> str:
        return pick_reviewer(self.agents_available(), self.quota_left)

    def reviewer_model(self, reviewer: str) -> tuple[str, str]:
        return resolve_reviewer_model(self.review_models, reviewer, catalog_fn=catalog)

    def _review(self, task: Task, repo_dir: Path, wt: Path, reviewer: str, number: int, ident: list[str],
                step) -> str:
        return run_review(self, task, repo_dir, wt, reviewer, number, ident, step)

    # ------------------------------------------------------------------- git
    def _write_git(self, task: Task, repo_dir: Path, *args: str, cwd: Path | None = None,
                   check: bool = True) -> str:
        return run_locked(repo_dir, lambda: self._git(cwd or repo_dir, *args, check=check),
                          lambda n: self._log(task, f"Git lock contention: retrying git {args[0]} (attempt {n}/3)"))

    def _git(self, cwd: Path, *args: str, check: bool = True) -> str:
        return git_run(cwd, *args, check=check)

    # ---------------------------------------------------------------- agents
    def _agent_cmd(self, task: Task, wt: Path, restricted: bool = False) -> list[str]:
        return build_agent_cmd(
            task,
            wt,
            prompt_fn=self._prompt,
            local_provider=self.local_provider,
            restricted=restricted,
            claude_bin=find_claude,
            codex_bin=find_codex,
            agy_bin=find_agy,
            opencode_bin=find_opencode,
        )

    def model_choices(self) -> str:
        return format_model_choices(
            self.agents_available(),
            catalog_fn=catalog,
            opencode_models_fn=opencode_models,
        )

    def _prompt(self, task: Task) -> str:
        try:
            choices = self.model_choices()
        except Exception:  # never let model discovery stop a task
            choices = ""
        return build_prompt(task, choices=choices, owner_tag=self.owner_tag)

    # --------------------------------------------------------- execution loop
    def _run(self, task: Task) -> None:
        def step(msg: str) -> None:
            task.steps.append(f"{time.strftime('%H:%M:%S')} {msg}")
            self._log(task, f"== {msg}")
            self.store.save(task)

        task.status = "running"         # dispatch() already saved it so
        task.started_at = task.started_at or time.time()
        repo_dir: Path | None = None
        wt: Path | None = None          # set once the worktree exists
        try:
            repo_dir = self.repos.get(task.repo)
            if not repo_dir:
                raise RuntimeError("No local clone of this repository (check BATON_LOCAL_ROOT).")
            step(f"Local clone: {repo_dir}")
            self._write_git(task, repo_dir, "fetch", "origin", task.base)
            self._check_cancelled(task)
            path = self.store.dir / "worktrees" / f"{task.repo.split('/')[1]}-{task.id}"
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_git(task, repo_dir, "worktree", "add", "-b", task.branch, str(path), f"origin/{task.base}")
            wt = path
            task.worktree = str(wt)
            step(f"Worktree on branch {task.branch} (from origin/{task.base})")

            if task.agent == "local" and not task.model and self.local_model:
                task.model = self.local_model
            label = AGENTS.get(task.agent, task.agent) + (f" ({task.model})" if task.model else "")
            step(f"Running {label}…")
            env = agent_env()
            if task.agent == "opencode":
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(OPENCODE_CONFIG)
            # Claude and Codex read the prompt from stdin; OpenCode and Gemini get
            # it as an argument, and their stdin is closed so they never wait.
            stdin_text = "" if task.agent in ("opencode", "gemini") else self._prompt(task)
            # Popen (not run) so that cancel() can reach the process while it works.
            group = {} if os.name == "nt" else {"start_new_session": True}
            log_path = self.store.log_path(task.id)
            start = log_path.stat().st_size if log_path.exists() else 0
            # Built before taking the lock: the prompt lists the models, which may
            # run `opencode models` / `agy models` when their cache has expired.
            cmd = self._agent_cmd(task, wt)
            with (open(log_path, "a", encoding="utf-8") as log,
                  Ranking(self.store.dir).worktree_env(env, wt, task.agent) as env):
                with self._lock:
                    if task.id in self._cancelled:
                        raise Cancelled()
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                            stdout=log, stderr=subprocess.STDOUT, text=True,
                                            encoding="utf-8", errors="replace", cwd=str(wt),
                                            env=env, **group, **_NO_WINDOW)
                    attach_job(proc)
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
                        cancelled = task.id in self._cancelled
                    if cancelled:
                        # Serialized with cancel(): guarantees full process-tree
                        # teardown finishes before _discard() removes the worktree.
                        kill_tree(proc)
                    else:
                        close_job(proc)
            # Only the end of this agent run can contain its final report.
            # agy's JSON lines: the whole run, so the first (`init`) and the
            # last (`result`, with the full answer) lines are both complete.
            output = self._read_output(log_path, start, 16 * 2**20 if task.agent == "gemini" else 65536)
            if task.agent == "gemini":
                model, result = agy_events(output)
                if model and not task.model:
                    task.model = model
                task.summary = extract_summary(str(result.get("response") or "")) or extract_summary(output)
                if record_gemini(self.store.dir, task.model or model, result.get("usage") or {}, "task"):
                    self._log(task, "== Gemini tokens recorded for the Quotas page")
            else:
                task.summary = extract_summary(output)
            self._check_cancelled(task)
            task.artifacts = collect_worktree_artifacts(wt, self.store.dir, task.id, task.summary)
            if task.artifacts:
                self._log(task, f"== Collected {len(task.artifacts)} image artifact(s)")
            step(f"Agent finished (exit code {proc.returncode})")
            if proc.returncode != 0:
                raise RuntimeError(f"The agent stopped with exit code {proc.returncode}; see the log.")

            if not self._git(wt, "status", "--porcelain"):
                task.status = "no-changes"
                task.message = "The agent finished without changing anything."
                self._write_git(task, repo_dir, "worktree", "remove", "--force", str(wt), check=False)
                self._write_git(task, repo_dir, "branch", "-D", task.branch, check=False)
                task.worktree = ""
                return
            with self._lock:
                if task.id in self._cancelled:
                    raise Cancelled()
                self._committing.add(task.id)  # only while committing, pushing, and opening the PR
            try:
                self._write_git(task, repo_dir, "add", "-A", cwd=wt)
                task.diff_stat = parse_numstat(
                    self._git(wt, "diff", "--cached", "--numstat", check=False)
                )
                ident = []
                if not self._git(wt, "config", "user.email", check=False):
                    ident = ["-c", f"user.name={self.git_name}", "-c", f"user.email={self.git_email}"]
                body = f"Task from Baton, done by {AGENTS.get(task.agent, task.agent)}.\n\n{task.details}".strip()
                self._write_git(task, repo_dir, *ident, "commit", "-m", f"{task.title}\n\n{body}", cwd=wt)
                step("Changes committed")
                self._check_cancelled(task)

                if task.open_pr:
                    self._write_git(task, repo_dir, "push", "-u", "origin", task.branch, cwd=wt)
                    step("Branch pushed to GitHub")
                    self._check_cancelled(task)
                    if not self.auto_merge:
                        after = "Review it before merging."
                    elif task.agent in REVIEWED_AGENTS:
                        after = ("Baton merges it when the repository's tests pass, after a review "
                                 "by Claude Code.")
                    else:
                        after = "Baton merges it when the repository's tests pass, without a review."
                    pr_body = (f"Task sent from Baton to **{AGENTS.get(task.agent, task.agent)}** "
                               f"on my computer.\n\n**Task:** {task.title}\n\n{task.details}\n\n{after}")
                    task.pr_url = self.open_pr(task.repo, task.branch, task.base, task.title, pr_body)
                    step(f"Pull request: {task.pr_url}")
                    task.message = "Done: review the pull request."
            finally:
                with self._lock:
                    self._committing.discard(task.id)

            if task.open_pr:
                if self.auto_merge:
                    try:
                        self._finish_pr(task, repo_dir, wt, ident, step)
                    except Cancelled:
                        raise
                    except GitLockTimeout:
                        raise
                    except LeftOpen as exc:
                        task.message = f"Pull request left open for you: {exc}"
                        step(f"Not merged: {exc}")
                    except Exception as exc:  # the PR is safe on GitHub either way
                        task.message = f"Pull request left open for you: {str(exc)[:300]}"
                        step(f"Not merged: {str(exc)[:300]}")
                if not task.merged:
                    self._check_cancelled(task)
                self._write_git(task, repo_dir, "worktree", "remove", "--force", str(wt), check=False)
                task.worktree = ""
                if task.merged and task.branch.startswith("baton/"):
                    self._write_git(task, repo_dir, "branch", "-D", task.branch, check=False)
                    self._write_git(task, repo_dir, "remote", "prune", "origin", check=False)
            else:
                task.message = f"Done: the changes are on the local branch {task.branch} in {wt}."
            task.status = "done"
        except Cancelled:
            if task.id in self._thermal_stops:
                task.status = "cancelled"
                task.message = "Stopped because GPU temperature exceeded 85 °C. Existing work was preserved."
                self._log(task, task.message)
                return
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
                self._cancel_events.pop(task.id, None)
                self._thermal_stops.discard(task.id)
                self._committing.discard(task.id)
                self._threads.pop(task.id, None)
                self._active.pop(task.id, None)
            # Only now (tests, review and merge all done) is the repository
            # free: the next queued task fetches the merged result.
            try:
                self.dispatch()
                if self.on_finished:
                    self.on_finished(task)
            except Exception as exc:  # a broken queue must not hide this task's result
                self._log(task, f"!! Could not start the next queued task: {exc}")
