# Baton Hardening & Refactoring Roadmap

Owner decisions (2026-11-25):
1. **Stabilize before adding new features:** fix security gaps, Windows cancellation flakiness, self-update retry loops, and data/concurrency bugs before building the Docker container or multi-machine monitoring.
2. **Personal tool first, clean open-source later:** keep personal defaults (`(Marco)`, `C:\IA\pruebas-opencode`, scheduled task `Baton`) for now. De-personalization for a clean public open-source release is deferred to Phase 4.
3. **Keep current auto-merge policy:** Claude Code, Codex, and Gemini keep full permissions and auto-merge without review; OpenCode and local AI keep restricted permissions and require Claude Code review before merging.

---

## Phase 1 — Security, Windows Cancellation, Self-Update & CI (PRIORITY)

### 1.1. Strip sensitive environment variables for all agent subprocesses
- **Problem:** `baton/agent_chat.py` (`chat_env`) strips `BATON_*`, `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT`, and `OPENCODE_PERMISSION`, but `baton/runner.py` (task execution & review) and `baton/delegate.py` only call `_clean_env()` (which only removes `ANTHROPIC_API_KEY`). Untrusted OpenCode/local tasks and `baton delegate --write` can read `GITHUB_TOKEN` and `BATON_OPENCODE_GO_KEY`.
- **Change:** Create a shared `agent_env()` helper used by `runner.py`, `delegate.py`, and `agent_chat.py` that strips `ANTHROPIC_API_KEY`, `BATON_*`, `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT`, and `OPENCODE_PERMISSION`.
- **Verify:** Unit tests confirming none of those env vars reach OpenCode, local AI, delegate, or reviewer subprocesses (while Baton's own `git`/`httpx` calls still work).

### 1.2. Prevent local privilege escalation against `http://127.0.0.1:8787`
- **Problem:** Baton has no authentication and embeds the static CSRF token in every GET HTML page. A restricted local agent (OpenCode, local AI, or future Docker container reaching host loopback) can scrape the CSRF token from `GET /tasks/new` and `POST /tasks` to launch a full-permission Claude Code/Codex/Gemini task with auto-merge.
- **Change:** Add lightweight authentication for state-changing and sensitive endpoints (e.g., `BATON_ACCESS_KEY` with an `HttpOnly`, `SameSite=Strict` signed session cookie, plus optional Tailscale identity header support if behind Tailscale Serve; document setup clearly in `.env.example` and `README.md`, keeping `/healthz` open). Ensure the `BATON_ACCESS_KEY` env var is stripped by `agent_env()`.
- **Verify:** Tests confirming unauthenticated requests (and requests with only a scraped CSRF token) cannot create tasks, start/stop models, open agent chats, or trigger self-update when auth is enabled, while authenticated browser sessions work seamlessly on desktop and phone.

### 1.3. Fix Windows cancellation cleanup reliably (Job Objects)
- **Problem:** Both cancellation tests in `tests/test_runner.py` fail intermittently on Windows because `taskkill /T /F /PID` misses grandchildren spawned via Python venv launcher shims and races with `_run()` calling `_discard()` (`git worktree remove --force`) before child handles/CWDs are released.
- **Change:**
  - On Windows, assign spawned agent processes (in `runner.py` and `agent_chat.py`) to a **Windows Job Object** (`ctypes` + `kernel32.dll`, `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) so terminating/closing the job atomically kills the entire process tree at the kernel level.
  - Ensure `kill_tree()` finishes before `_run()` executes `_discard()`, and add fallback cleanup if `git worktree remove` hits transient Windows locks.
- **Verify:** Run `pytest tests/test_runner.py -q` repeatedly (e.g., 5x in a row) on Windows with 0 failures and verify no orphan processes, worktrees, or local branches remain.

### 1.4. Stop `SelfUpdate` infinite retry loops
- **Problem:** If `origin/main` fails `pytest -q`, `SelfUpdate` rolls back and retries the exact same `target` SHA every 5 minutes forever, blocking task submission with HTTP 503 during every test run.
- **Change:** Track the failed `target` SHA (persisted via `updates.jsonl` / in-memory) and skip automatic retries for that same SHA until `origin/main` advances to a new commit or the user manually triggers **Update now** (`POST /self-update`).
- **Verify:** Regression tests in `tests/test_self_update.py` verifying automatic skip on repeated failed SHA and successful retry when `origin/main` changes or manual update is requested.

### 1.5. Avoid `.cmd` wrapper in `find_codex()` when a native binary exists
- **Problem:** `baton/ping.py:find_codex()` prefers `codex.cmd` on Windows, passing CLI flags (`model`, `effort`, paths) through `cmd.exe` and complicating process-tree teardown.
- **Change:** Resolve the native `codex.exe` (or real binary next to npm's wrapper, like `find_opencode()`) when available, falling back cleanly only if no native binary exists. Document findings in `docs/`.
- **Verify:** Unit tests covering Windows binary resolution without `.cmd` when native `.exe` exists.

### 1.6. Upgrade CI to cover Windows + linting
- **Problem:** `.github/workflows/ci.yml` only runs on `ubuntu-latest`, missing Windows-specific regressions, and has no linter.
- **Change:** Add a `windows-latest` test job (keeping `permissions: {}` and SHA-pinned actions) and add `ruff` + JS syntax check (`node --check baton/static/*.js`).
- **Verify:** CI passes on both `ubuntu-latest` and `windows-latest`.

---

## Phase 2 — Data Accuracy, Concurrency & Repo Hygiene
- Fix Claude Code streaming token undercounting in `baton/usage.py` (keep the final/max usage per `(msg.id, requestId)`, ignore `(None, None)` collisions, and use `db.as_uri() + "?mode=ro"` for OpenCode SQLite).
- Record actual model names in `task.model` / `ranking.sqlite3` so default-model runs don't collapse into `""`; use 52-bit random IDs (`randbits(52)`) for JS safety.
- Update `updated:` and `updated_by:` in `github.py:complete_item()` (so web ticks don't trigger stale warnings) and prefix `YYYY-MM-DD` in `complete_owner_action()`.
- Normalize repo names with `.casefold()` in `Runner.dispatch()`.
- Fix `models.py`: check VRAM budget inside `self.lock`, save `models-state.json` only when changed (with Windows `PermissionError` retry), and make `chat_port()` lightweight without calling full `overview()`.
- Allow cancelling tasks during CI wait and AI review (keep `_committing` strictly around `add/commit/push/open_pr`).
- Remove `httpx` import from `baton/quotas.py` (use `urllib.request`) to honor `AGENTS.md`.
- Add periodic idle reaper + turn timeout for `AgentChats` (`agent_chat.py`).
- Generalize `GEMINI_MODEL` regex in `task_models.py` to `r"gemini-[\w.-]+"`.
- Auto-delete merged remote `baton/*` branches after merge + prune stale remotes; avoid caching transient GraphQL failures for 5 minutes in `open_pulls()`; bound `Hub.load()` concurrency with an `asyncio.Semaphore`.

---

## Phase 3 — Modularization, SQLite Storage & Live UX
- Split `baton/web.py` into FastAPI `APIRouter` modules (`routes/`).
- Split `baton/runner.py` into focused modules (`task_store.py`, `worktree.py`, `agents.py`, `review_merge.py`, `runner.py`).
- Consolidate `tasks.json` + `ranking.sqlite3` into a single SQLite database in WAL mode (`busy_timeout=30000`, schema initialized once).
- Stream task progress and logs via SSE on `/tasks/{id}` (plus readable rendering for Gemini `stream-json` logs).
- Compute `usage.collect()` in the background loop instead of blocking `/quotas` requests.
- Archive old `STATUS.md` history to keep `STATUS.md` concise (<100 lines).

---

## Phase 4 — Open-Source Release Cleanup (Deferred)
- Make `(Marco)`, `C:\IA\pruebas-opencode`, and Windows scheduled task `"Baton"` configurable (`BATON_OWNER_TAG`, `BATON_CHAT_SCRATCH`, `BATON_SCHEDULED_TASK`) with generic defaults.
- Scrub personal references from `README.md`, `docs/`, and tests before public release.
