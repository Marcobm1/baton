"""Coordinate Git writes by Baton's task and update threads."""

import re
import threading
import time
from pathlib import Path


class GitLockError(RuntimeError):
    """Git reported contention on a ref or lock file."""


class GitLockTimeout(RuntimeError):
    """Another Baton operation held the clone lock for too long."""


def is_lock_error(stderr: str) -> bool:
    return bool(re.search(r"cannot lock ref\b|unable to create '[^\r\n]*\.lock': file exists",
                          stderr, re.IGNORECASE))


# A queued task and the updater can fetch into the same origin/main. Both
# run in this process, so a thread lock per resolved clone path is sufficient.
_locks = {}
_registry_lock = threading.Lock()
LOCK_TIMEOUT = 120


def run_locked(repo: Path, operation, on_retry=None):
    path = repo.resolve()
    with _registry_lock:
        lock = _locks.setdefault(path, threading.Lock())
    if not lock.acquire(timeout=LOCK_TIMEOUT):
        raise GitLockTimeout(f"Timed out after {LOCK_TIMEOUT}s waiting for the Git lock on {path}")
    try:
        for attempt in range(1, 4):
            try:
                return operation()
            except GitLockError:
                if attempt == 3:
                    raise
                if on_retry:
                    on_retry(attempt + 1)
                time.sleep(1)
    finally:
        lock.release()
