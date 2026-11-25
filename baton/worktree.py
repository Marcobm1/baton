"""Local clone discovery, Git helpers, and worktree lifecycle management."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .git_lock import GitLockError, is_lock_error
from .ping import _NO_WINDOW


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

    def _parse_remote(self, url: str) -> str:
        return _remote_repo(url)

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
                repo = self._parse_remote(url)
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

    def all(self) -> dict[str, Path]:
        """Every local clone Baton knows: owner/repo (lower case) -> folder."""
        if not self._cache or time.time() - self._cache[0] > 300:
            self._cache = (time.time(), self._scan())
        return {**self._cache[1], **self.overrides}


def git_run(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, **_NO_WINDOW)
    if r.returncode:
        message = f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}"
        if is_lock_error(r.stderr):
            raise GitLockError(message)
        if check:
            raise RuntimeError(message)
    return r.stdout.strip()


def branch_published(repo_dir: Path, branch: str) -> bool | None:
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


def remove_worktree_fallback(wt: Path, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Remove a worktree directory directly when `git worktree remove` hits locks or read-only files."""
    def _clear_readonly(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            func(path)
        except OSError:
            pass

    for _ in range(6):
        if sys.version_info >= (3, 12):
            shutil.rmtree(wt, onexc=_clear_readonly)
        else:
            shutil.rmtree(wt, onerror=_clear_readonly)
        if not wt.exists():
            return True
        sleep(0.5)
    return not wt.exists()


def discard_worktree(
    task,
    repo_dir: Path | None,
    wt: Path | None,
    *,
    write_git: Callable[..., str],
    check_published: Callable[[Path, str], bool | None] = branch_published,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """After a cancel: drop the worktree and the local branch, unless the
    branch is on GitHub. Returns what happened, for the task's message."""
    if not repo_dir or not wt:
        return "Nothing had been created yet."
    published = check_published(repo_dir, task.branch)
    if published is not False:
        why = "is on GitHub" if published else "could not be checked on GitHub"
        return f"The branch {task.branch} {why}, so its worktree and branch were kept."
    # Windows may keep files of the killed processes open for a moment.
    for _ in range(10):
        write_git(task, repo_dir, "worktree", "remove", "--force", str(wt), check=False)
        if not wt.exists():
            break
        sleep(0.5)
    if wt.exists():
        remove_worktree_fallback(wt, sleep=sleep)
    if wt.exists():
        return f"The worktree could not be removed; delete it by hand: {wt}"
    write_git(task, repo_dir, "worktree", "prune", check=False)
    write_git(task, repo_dir, "branch", "-D", task.branch, check=False)
    task.worktree = ""
    return "Nothing was committed or pushed; the worktree and the local branch were removed."
