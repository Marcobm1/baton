# Local task queue with priority

Decided 2026-11-24 (owner's request). Before this, Baton refused a new local
task when its repository already had one running.

## Rules

1. A new local task is saved as `queued`. `Runner.dispatch()` starts queued
   tasks while there are free slots, in queue order.
2. **One task at a time per repository**, and at most `BATON_MAX_PARALLEL`
   (default 3) local tasks on the whole computer. Cloud tasks don't count.
3. **Order:** priority (`high`, `normal`, `low`; `normal` by default), then
   creation time, oldest first (`runner.queue_key`). A started task is never
   interrupted: priority only matters while a task is waiting.
4. A slot is freed only when the task's thread ends, which is after the
   tests on GitHub, the review and the auto-merge. `dispatch()` runs at that
   point, when a task is submitted, and when the server starts.
5. **Fetch and worktree when the task starts**, not when it is created: the
   task's thread runs `git fetch origin <base>` and `git worktree add … origin/<base>`
   as its first steps, so it begins from what the previous task merged.
6. A queued task's priority can be changed, and it can be cancelled, from its
   page (POST with the same CSRF token + `Origin` check as the task form).
   Cancelling a queued task creates nothing: no worktree, no branch.
7. **Restart:** tasks still `running` in `tasks.json` are marked `failed`
   (their processes died with the old Baton); `queued` tasks keep their place
   and start when the server is up (FastAPI's lifespan start-up).
8. The list and the task page show, for a queued task: its priority, its place
   in **its repository's queue** ("queued, 2nd") and what it waits for: the
   queued task just ahead of it, otherwise the task running in that repository,
   otherwise "a free slot" (when only the global limit holds it back).

## Why these choices

- **The queue is `tasks.json` itself**, not a separate list in memory. The
  status (`queued`), `priority` and `created_at` are enough to rebuild the
  order at any time, so a restart keeps the queue for free and there is no
  second copy that could disagree with the first.
- **One lock for the queue and for cancelling.** Starting a task (status
  `queued` → `running` plus creating its thread), changing its priority and
  cancelling a queued task all happen under `Runner._lock`. So a cancel or a
  priority change can never land on a task that has just started: it either
  sees the task still queued or already running. This keeps the "one writer
  per task" rule from docs/cancelling-tasks.md: while queued, the queue writes
  the task; once running, only its own thread does.
- **Running repositories are counted from memory** (`Runner._active`, the
  threads of this run), not from `tasks.json`. After a crash the file may say
  `running` for a task nobody runs; counting it would block its repository
  forever. `recover()` fixes those records at start-up.
- **Position per repository**, not global: with one task per repository, the
  global order says little about when *this* task will start, while its
  place in its own repository's queue does.
- **Reads of `tasks.json` take the store's lock.** The queue reads the file
  more often, which exposed a Windows race: `os.replace()` of the new file
  fails with "Access denied" while another thread has the old one open for
  reading. Reads now wait for writes, and the replace retries for up to a
  second if another program (antivirus, indexer) holds the file.
- **Start-up dispatch in the lifespan**, not in `create_app()`, so building
  the app (as the tests do) doesn't launch agents; the real server does.

## What we give up

- No resuming: a task that was running when Baton stopped is failed, not
  restarted (its half-done worktree may be inconsistent).
- No reordering by hand beyond the three priorities, and no "start now"
  button that skips the limit.
- `BATON_MAX_PARALLEL` is read at start-up; changing it needs a restart.
