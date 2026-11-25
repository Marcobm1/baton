"""CI check waiting, AI code review, and PR auto-merge pipeline."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .agents import AGENTS, REVIEW_PROMPT, REVIEWED_AGENTS, extract_summary
from .ping import _NO_WINDOW, agent_env
from .proc import attach_job, close_job, kill_tree
from .ranking import Ranking, parse_rating, review_verdict
from .task_models import catalog
from .task_store import Cancelled, LeftOpen, Task, pr_number


def resolve_reviewer_model(
    review_models: dict[str, tuple[str, str]],
    reviewer: str,
    *,
    catalog_fn: Callable[[str], dict] = catalog,
) -> tuple[str, str]:
    """The configured model and effort for a reviewer, if its CLI lists them.

    A model the CLI doesn't list (a typo, or one the account can't use)
    would make the review fail and leave the PR open, so it falls back to
    the CLI default instead; an unknown effort is dropped the same way.
    """
    model, effort = review_models.get(reviewer, ("", ""))
    if not model and not effort:
        return "", ""
    options = catalog_fn(reviewer)
    if not options:                 # the list could not be read: trust the setting
        return model, effort
    if model and model not in options:
        return "", ""
    if not model:
        return "", effort
    return model, effort if effort in options[model]["efforts"] else ""


def pick_reviewer(
    availability: dict[str, str],
    quota_left: Callable[[str], float | None] | None,
) -> str:
    """Claude Code reviews OpenCode and local-AI work (Opus, medium effort
    unless BATON_REVIEW_CLAUDE_* say otherwise). Without it, or without
    quota left in its 5-hour window, the pull request stays open."""
    if availability.get("claude-code", "missing"):
        raise LeftOpen("Claude Code is not available to review the work")
    left = quota_left("claude-code") if quota_left else None
    if left is not None and left <= 0:
        raise LeftOpen("Claude Code has no quota left to review the work")
    return "claude-code"


def wait_for_tests(runner, task: Task, sha: str, step, *, must_exist: bool = False) -> str:
    """'passed', or 'none' if the repository runs no tests on GitHub."""
    step(f"Waiting for the repository's tests on GitHub ({sha[:7]})")
    start = time.monotonic()
    cancel_ev = runner._cancel_events.get(task.id)
    while True:
        runner._check_cancelled(task)
        c = runner.github.checks(task.repo, sha)
        waited = time.monotonic() - start
        if c["failed"]:
            raise LeftOpen("the tests failed on GitHub: " + ", ".join(sorted(set(c["failed"]))[:5]))
        if not c["pending"] and c["passed"]:
            step("Tests passed on GitHub")
            return "passed"
        if not c["pending"] and not c["passed"] and waited >= runner.ci_grace:
            if must_exist:
                raise LeftOpen("the tests did not run on GitHub for the review's fixes")
            step("This repository runs no tests on GitHub")
            return "none"
        if waited >= runner.ci_timeout:
            raise LeftOpen(f"the tests were still running after {runner.ci_timeout // 60} minutes")
        if cancel_ev is not None:
            cancel_ev.wait(runner.ci_poll)
        else:
            time.sleep(runner.ci_poll)


def run_review(
    runner,
    task: Task,
    repo_dir: Path,
    wt: Path,
    reviewer: str,
    number: int,
    ident: list[str],
    step,
) -> str:
    """Run the reviewer in the task's worktree; returns the commit to merge."""
    task.reviewer = reviewer
    model, effort = runner.reviewer_model(reviewer)
    task.reviewer_model, task.reviewer_effort = model, effort  # saved by step() below
    setup = " · ".join(x for x in (model, effort) if x)
    step(f"Review by {AGENTS[reviewer]}{f' ({setup})' if setup else ''}…")
    rtask = replace(task, agent=reviewer, model=model, effort=effort)
    prompt = REVIEW_PROMPT.format(author=AGENTS.get(task.agent, task.agent), repo=task.repo,
                                  branch=task.branch, base=task.base, title=task.title,
                                  details=task.details or "(no more details)")
    if task.agent in REVIEWED_AGENTS:
        prompt += (
            "\nAfter the marked review, rate the original delivery before your fixes. "
            "Put BATON_RATING_BEGIN and BATON_RATING_END on separate lines around one JSON "
            "object. Use integer scores from 1 to 10 for fulfillment (request covered), "
            "correctness (as delivered), and rework (10 means no fixes needed). "
            "Include a brief nonempty comment (at most 500 characters). "
            "Use exactly the keys fulfillment, correctness, rework and comment.\n"
        )
    log_path = runner.store.log_path(task.id)
    start = log_path.stat().st_size if log_path.exists() else 0
    group = {} if os.name == "nt" else {"start_new_session": True}
    with (open(log_path, "a", encoding="utf-8") as log,
          Ranking(runner.store.dir).worktree_env(agent_env(), wt, reviewer) as env):
        with runner._lock:
            if task.id in runner._cancelled:
                raise Cancelled()
            # The reviewer reads work by a less trusted model: limited mode.
            proc = subprocess.Popen(runner._agent_cmd(rtask, wt, restricted=True),
                                    stdin=subprocess.PIPE, stdout=log,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", cwd=str(wt), env=env, **group,
                                    **_NO_WINDOW)
            attach_job(proc)
            runner._procs[task.id] = proc
        try:
            proc.communicate(input=prompt, timeout=runner.timeout)
        except subprocess.TimeoutExpired as exc:
            kill_tree(proc)
            raise LeftOpen(f"the review took longer than {runner.timeout // 60} minutes") from exc
        except OSError:
            proc.wait()
        finally:
            with runner._lock:
                runner._procs.pop(task.id, None)
                cancelled = task.id in runner._cancelled
            if cancelled:
                kill_tree(proc)
            else:
                close_job(proc)
    runner._check_cancelled(task)
    output = runner._read_output(log_path, start)
    task.review = extract_summary(output, "BATON_REVIEW")
    if task.agent in REVIEWED_AGENTS:
        task.review_rating = parse_rating(extract_summary(output, "BATON_RATING", after="BATON_REVIEW_END"))
    verdict = review_verdict(task.review)
    step(f"Review finished (exit code {proc.returncode}, verdict {verdict or 'none'})")
    changed = bool(runner._git(wt, "status", "--porcelain"))
    try:
        runner.github.comment(task.repo, number, f"**Review by {AGENTS[reviewer]}** (from Baton)"
                              f"\n\n{task.review or '(The reviewer gave no report; see the task log.)'}")
    except Exception as exc:  # the review itself still counts
        runner._log(task, f"!! Could not post the review on the PR: {exc}")
    if proc.returncode != 0:
        raise LeftOpen(f"the review stopped with exit code {proc.returncode}")
    if verdict == "BLOCK":
        raise LeftOpen(f"{AGENTS[reviewer]} advised not to merge it (see its review)")
    if not verdict:
        raise LeftOpen(f"{AGENTS[reviewer]} gave no verdict")
    if changed:
        with runner._lock:
            if task.id in runner._cancelled:
                raise Cancelled()
            runner._committing.add(task.id)
        try:
            runner._write_git(task, repo_dir, "add", "-A", cwd=wt)
            message = f"Review fixes by {AGENTS[reviewer]}\n\nBaton review of the task: {task.title}"
            runner._write_git(task, repo_dir, *ident, "commit", "-m", message, cwd=wt)
            runner._write_git(task, repo_dir, "push", "origin", task.branch, cwd=wt)
        finally:
            with runner._lock:
                runner._committing.discard(task.id)
        step("Review fixes committed and pushed")
    return runner._git(wt, "rev-parse", "HEAD")


def finish_pr(runner, task: Task, repo_dir: Path, wt: Path, ident: list[str], step) -> None:
    """Tests, review when needed, merge. Raises LeftOpen to stop."""
    number = pr_number(task.pr_url)
    sha = runner._git(wt, "rev-parse", "HEAD")
    tests = runner._wait_for_tests(task, sha, step)
    # Claude Code, Codex and Gemini are trusted to merge on their own
    # (also in a repository without tests); OpenCode and local AI are not.
    if task.agent in REVIEWED_AGENTS:
        reviewer = runner._pick_reviewer(task)
        new_sha = runner._review(task, repo_dir, wt, reviewer, number, ident, step)
        if new_sha != sha and tests == "passed":
            runner._wait_for_tests(task, new_sha, step, must_exist=True)
        sha = new_sha
    with runner._lock:
        if task.id in runner._cancelled:
            raise Cancelled()
        runner._committing.add(task.id)
    try:
        runner.github.merge(task.repo, number, sha)
        task.merged = True
    finally:
        with runner._lock:
            runner._committing.discard(task.id)
    delete_branch = getattr(runner.github, "delete_branch", None)
    if callable(delete_branch) and task.branch.startswith("baton/"):
        try:
            delete_branch(task.repo, task.branch)
        except Exception:
            pass  # non-fatal: the PR is already merged
    # The merge usually ticks the task in STATUS.md: show it now, not when
    # the project cache expires minutes later.
    invalidate = getattr(runner.github, "invalidate", None)
    if callable(invalidate):
        invalidate()
    step(f"Merged into {task.base}")
    by = f" after a review by {AGENTS[task.reviewer]}" if task.reviewer else ""
    task.message = f"Done: merged into {task.base}{by}."
