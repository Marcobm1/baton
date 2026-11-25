---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex, Gemini and OpenCode Go quota windows.
next: "Agents chat for OpenCode, Claude Code and Codex in Baton, with tools"
updated: 2026-11-25
updated_by: gemini
open_source: true
tags: [tooling, ai-agents]
links:
  Hub repo: https://github.com/Marcobm1/proyectos
---

## Done recently
- 2026-11-25: Hardening Phases 1–3 complete + isolated Docker container for Agents chat "All permissions" mode (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`). Full archive in [docs/history.md](docs/history.md).

## Next
- [x] 2026-11-25 Hardening Phase 1: `agent_env()`, `BATON_ACCESS_KEY` auth, Windows Job Objects, `SelfUpdate` retry skip, native `find_codex()`, Windows + lint CI
- [x] 2026-11-25 Hardening Phase 2: data accuracy, concurrency, cancellation during CI/review and GitHub hygiene
- [x] 2026-11-25 Hardening Phase 3A: modularize `runner.py` and `web.py` into focused modules and `baton/routes/`
- [x] 2026-11-25 Hardening Phase 3B: unified SQLite WAL storage (`baton.sqlite3`) with automatic legacy migration
- [x] 2026-11-25 Hardening Phase 3C: live task SSE, readable Gemini logs, background `usage.collect()` and `STATUS.md` archive
- [x] 2026-11-25 Readable log for Gemini tasks
- [x] 2026-11-25 Diagnose Windows cancellation cleanup failures (resolved via Phase 1.3 Job Objects; verified on `windows-latest` CI)
- [x] 2026-11-25 Isolated Docker container for the All permissions mode
  Added `docker/opencode-sandbox.Dockerfile` and `baton/sandbox.py` (`--cap-drop=ALL`,
  `no-new-privileges`, `--pids-limit=256`, `--memory=4g`, single `/workspace` bind
  mount, `host.docker.internal` for local `llama-server`, zero host secrets/Docker
  socket); unlocked All permissions for non-abliterated `local-*` models with red
  warning banner and red badge; verified with fake ACP + real Docker sibling/symlink
  isolation test (`tests/test_sandbox.py`, [docs/agents-chat.md](docs/agents-chat.md)).
- [x] 2026-11-25 (Marco) Verify Phases 1–3 on Windows, enable BATON_ACCESS_KEY and build Docker sandbox image
  Verified on Marco's Windows PC (Python 3.13.15, Docker Desktop 29.5.2): `test_runner.py`
  5/5 clean, `BATON_ACCESS_KEY` set & `Baton` task restarted (`/healthz` 200, `/` 303,
  `/api/tasks` 401), `baton-opencode-sandbox:latest` built (`opencode v2.4.1`, `CapEff=0`,
  `NoNewPrivs=1`, junction confinement verified). Follow-up fixes landed for non-blocking
  Gemini quota refresh, `codex-cli 0.171.0` `vendor/<triple>/bin/codex.exe`, `st_nlink > 1`
  hardlink guard, pinned `@opencode/cli@2.4.1`, and Windows junction test fallback.
- [ ] (Marco) Sign in with BATON_ACCESS_KEY on desktop browser and phone
  Open Baton on desktop and phone and log in once with the `BATON_ACCESS_KEY` configured
  in `.env` (30-day session cookie).
- [ ] (Marco) Send a first real Gemini task from Baton
  New task → Gemini, model `gemini-3.8-flash` (low) on a small change in this
  repo; check auto-merge without review, readable log, summary, and Quotas.
- [ ] Agents chat for OpenCode, Claude Code and Codex in Baton, with tools
  Phase 1 (OpenCode/Gemini over ACP, Confirm mode) and isolated Docker container
  (OpenCode local models, Full mode) are done; live Windows protocols verified
  (Claude `--permission-prompt-tool stdio`, Codex `app-server`). Remaining:
  Phase 2 implementation + fake-CLI tests + session resume ([docs/agents-chat.md](docs/agents-chat.md)).
  Recommended: claude-code, opus, high
- [ ] Monitoring for other computers and servers, and alerts
  Add online/offline reporting for extra machines via a small agent and alerts
  for high temperatures, low RAM/disk, and unusual GPU/network/login activity.
  Recommended: claude-code, opus, high
- [ ] (Marco) Live smoke checks: queue two tasks on one repo and check a blocked card (hub: Cuantización → IA local)
- [ ] Hardening Phase 4: open-source release cleanup (deferred)
  Make `(Marco)`, `C:\IA\pruebas-opencode`, and scheduled task `Baton`
  configurable via env vars and scrub personal references before public release
  ([docs/hardening-roadmap.md](docs/hardening-roadmap.md)).
  Recommended: codex, gpt-6.2-sol, medium

## Blockers
- Confirm mode on the host runs approved commands as Marco's Windows user (Gemini Confirm covers commands only; Codex approved commands write even under `sandbox: "read-only"`).

## Decisions
- 2026-11-25: Windows verification fixes: `refresh_gemini` runs non-blocking in the background on `/quotas`; `_resolve_codex_native()` checks `vendor/<triple>/bin/codex.exe` (codex-cli 0.171.0); Full-mode Docker sandbox refuses folders with hardlinked files (`st_nlink > 1`) and pins `@opencode/cli@2.4.1`; `tests/test_sandbox.py` falls back to a Windows junction (`mklink /J`) when `os.symlink` lacks Developer Mode.
- 2026-11-25: Isolated Docker sandbox (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`): All permissions in Agents chat runs OpenCode inside a disposable `--cap-drop=ALL`, `no-new-privileges`, single-`/workspace`-mount container reaching host `llama-server` via `host.docker.internal` (POSIX passes `--user uid:gid` + `HOME=/tmp`); restricted to non-abliterated `local-*` models so no cloud credentials enter the container ([docs/agents-chat.md](docs/agents-chat.md)).
- 2026-11-25: Phase 3C live UX: `/tasks/{id}` streams live status, steps, progress, and formatted logs via `GET /api/tasks/{task_id}/events` (`task-live.js`); `task_report.py` formats Gemini `stream-json` logs (joined text deltas, one-line tool steps, `[result]` + tokens); `usage.collect()` runs in the background loop so `/quotas` never blocks on disk log scans; older history archived in [docs/history.md](docs/history.md).
- 2026-11-25: Phase 3B storage: tasks and rankings share `<data_dir>/baton.sqlite3` in WAL mode (`busy_timeout=30000`, one-time schema init, atomic `SAVEPOINT` task+ranking commits, lossless automatic migration from legacy `tasks.json` and `ranking.sqlite3`).
- 2026-11-25: Phase 3A modularization: `runner.py` delegates to `task_store.py`, `worktree.py`, `agents.py`, `review_merge.py`; `web.py` mounts eight FastAPI `APIRouter` modules under `baton/routes/` with isolated per-app `AppContext` (`app.state.ctx`).
- 2026-11-25: Phase 1–2 security & reliability: `agent_env()` strips secrets from all agent subprocesses; optional `BATON_ACCESS_KEY` locks the UI/API behind a signed `HttpOnly` cookie ([docs/security.md](docs/security.md)); Windows Job Objects (`baton/proc.py`), `SelfUpdate` failed-SHA skip, native `find_codex()`, `quotas.py` stdlib-only, cancellation during CI/review, and merged `baton/*` branch cleanup.
- 2026-11-25: auto-merge policy: Claude Code, Codex, and Gemini run with full permissions and auto-merge without review; OpenCode and local AI keep restricted rules and are reviewed by Claude Code (Opus, medium, `auto` mode); abliterated models never get full permissions ([docs/auto-merge.md](docs/auto-merge.md)). Earlier decisions in [docs/history.md](docs/history.md).

## Log
- 2026-11-25 (gemini): Applied Windows PC verification fixes: non-blocking Gemini quota refresh on `/quotas`, `vendor/<triple>/bin/codex.exe` native discovery (`codex-cli 0.171.0`), `st_nlink > 1` hardlink refusal in Full-mode Docker sandbox, pinned `@opencode/cli@2.4.1`, Windows junction fallback in `tests/test_sandbox.py`, and documented verified Phase 2 Claude/Codex protocols. Suite: 230 passed.
- 2026-11-25 (gemini): Added isolated Docker container for Agents chat All permissions mode (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`, UI red warning & badge, server-side local-only + abliterated refusal, `tests/test_sandbox.py` with fake ACP mode switch + real Docker sibling/symlink confinement test). Suite: 227 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phase 3C (live UX & hygiene): live task SSE (`GET /api/tasks/{task_id}/events` + `baton/static/task-live.js`), readable Gemini `stream-json` log formatting (`format_gemini_log` / `format_task_log` in `task_report.py`), non-blocking background `usage.collect()` in `deps.py`/`web.py`, and archived older `STATUS.md` history to `docs/history.md` (<100 lines). Suite: 223 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phase 3B (unified SQLite WAL storage): `baton/db.py` (`baton.sqlite3` WAL, `busy_timeout=30000`, one-time schema init, automatic legacy migration), atomic `SAVEPOINT` commits in `TaskStore` + `Ranking`, `tests/test_db.py`. Suite: 219 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phase 3A (modularization): split `runner.py` (1,253 → 590) and `web.py` (935 → 276) into focused modules and `baton/routes/`, fixed expiring timestamp in `tests/test_gemini.py`, added `tests/test_modularization.py`. Suite: 214 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phase 2 (data accuracy, concurrency & repo hygiene). Suite: 211 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phase 1 (1.1–1.6: security, auth, Windows Job Objects, SelfUpdate skip, native `find_codex()`, Windows + lint CI). Suite: 198 passed (Linux + Windows CI).
- Earlier entries (2026-11-23 – 2026-11-25): see [docs/history.md](docs/history.md).
