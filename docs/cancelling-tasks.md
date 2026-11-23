# Cancelling a running local task

Decided 2026-11-23.

## What happens

1. The task page shows **Cancel task** while the task runs on this computer.
   The button posts a form with the per-run CSRF token, checked exactly like
   the "New task" form (token + `Origin` check). Without it: `403`.
2. `Runner.cancel()` marks the task as cancelled and kills the agent's process
   tree. It then waits (up to 30 s) for the task's thread to finish, so the
   page you are redirected to already shows the result.
3. The task's own thread notices, skips commit/push, and cleans up:
   - `git ls-remote --exit-code --heads origin refs/heads/<branch>`:
     exit 2 means the branch is not on GitHub, so the worktree is removed
     (`git worktree remove --force`, retried for a few seconds because Windows
     can keep files of killed processes open) and the local branch deleted
     (`git branch -D`);
   - if the branch *is* on GitHub, or the check fails (no network), both are
     kept and the message says so. Losing work by mistake is worse than
     leaving a folder behind.
4. Status `cancelled`, `finished_at` = the time of the cancel.

## Why these choices

- **`Popen` instead of `subprocess.run`.** `run` waits until the process ends
  and gives no handle to it; to stop it from another thread (the web request)
  Baton needs the `Popen` object.
- **Killing the whole tree.** `proc.kill()` only stops the direct child.
  Claude Code, Codex and OpenCode start their own processes (node, shells,
  tools). Even `.venv\Scripts\python.exe` is a launcher that starts the real
  interpreter as a child. An orphaned child would keep working, and on Windows
  it also locks the worktree folder (a process's current directory can't be
  deleted). The test checks this with a fake agent whose child writes a
  heartbeat file: after the cancel the file must stop growing.
  - Windows: `taskkill /T /F /PID <pid>` (`/T` = the tree, `/F` = force).
    Called by its full path in `System32` so a `taskkill.exe` elsewhere on
    `PATH` can't be picked up instead. Limitation: `/T` follows parent→child
    links, so a grandchild whose parent already exited is not found. A *Job
    Object* would catch those too, but needs `ctypes` and Win32 calls; not
    worth it until it shows up in practice.
  - Linux/macOS: the agent starts with `start_new_session=True` (its own
    process group) and `os.killpg(pgid, SIGKILL)` stops the whole group.
- **One writer per task.** Only the task's thread saves the task; `cancel()`
  just sets a flag and kills the process. A lock makes "is it cancelled?" and
  "start the agent" / "start committing" a single step, so a cancel can't slip
  in between. Once committing has started, `cancel()` returns `False`
  (`409` on the page): a half-made commit or push would be worse than letting
  it finish.
- **After a restart** Baton can't reach the processes of a previous run; those
  tasks are already marked `failed` at start-up, so there is no button for them.
