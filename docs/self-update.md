# Updating the running Baton checkout

Decision, 2026-11-24. The server owns one update worker. The timer, the
Quotas form and the task completion callback wake the same worker. The
checkout is identified from the loaded Baton package so starting the service
from another directory cannot update an unrelated repository.

The updater can fetch while an agent works. Its Git commands and task Git
writes share a thread lock keyed by the resolved clone path, including writes
from linked task worktrees. This prevents a newly dispatched task and the
updater from writing `origin/main` at the same time. Each command waits up
to 120 seconds for its clone. If Git reports a ref lock conflict or an
existing lock file, Baton retries after one second, up to three attempts in
total. The task log records each retry.

Before changing files the updater reserves
the local queue under the same lock used to submit and dispatch tasks. It
checks both persisted tasks and active workers, including cleanup after a
merge. A local task submitted during validation receives HTTP 503 and can be
retried. Cloud tasks execute independently on GitHub.

Only a clean main branch whose HEAD still matches the server's startup commit
can advance. A fast-forward preserves its history. After the pull, Baton
compares the two actual commits to decide whether to reinstall with the
service interpreter. The development extra includes pytest. Test failures
record the command, exit code and available test names. A rollback uses
`git reset --keep` and reinstalls the previous requirements if necessary.
If the checkout has moved or rollback fails, the error on Quotas explains
which step needs attention. Keep the installation checkout dedicated to
the service while it is updating.

The updater records validation in `updates.jsonl` before requesting a graceful
Uvicorn shutdown. On Windows a hidden PowerShell process outside the server
job waits for that specific PID and for the scheduler to report the Baton
task ready, then calls `schtasks /Run /TN Baton`. A separate restart log in
the data directory captures helper errors if the server is already down.
On other native platforms the process replaces itself with the same serve
command. At startup the new process records its commit as running, so a
successful test run and a confirmed restart can be distinguished.
