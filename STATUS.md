---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex, Gemini and OpenCode Go quota windows.
next: "Isolated Docker container for the All permissions mode"
updated: 2026-11-25
updated_by: gemini
open_source: true
tags: [tooling, ai-agents]
links:
  Hub repo: https://github.com/Marcobm1/proyectos
---

## Done recently
- 2026-11-25: Hardening Phases 1–3 complete (security & auth, Windows Job Objects, CI on Windows + lint, data accuracy & concurrency, modular `runner.py` & `routes/`, unified `baton.sqlite3` WAL storage, live task SSE, readable Gemini logs, non-blocking background `usage.collect()`). Full archive in [docs/history.md](docs/history.md).

## Next
- [x] 2026-11-25 Hardening Phase 1: `agent_env()`, `BATON_ACCESS_KEY` auth, Windows Job Objects, `SelfUpdate` retry skip, native `find_codex()`, Windows + lint CI
- [x] 2026-11-25 Hardening Phase 2: data accuracy, concurrency, cancellation during CI/review and GitHub hygiene
- [x] 2026-11-25 Hardening Phase 3A: modularize `runner.py` and `web.py` into focused modules and `baton/routes/`
- [x] 2026-11-25 Hardening Phase 3B: unified SQLite WAL storage (`baton.sqlite3`) with automatic legacy migration
- [x] 2026-11-25 Hardening Phase 3C: live task SSE, readable Gemini logs, background `usage.collect()` and `STATUS.md` archive
- [x] 2026-11-25 Readable log for Gemini tasks
- [x] 2026-11-25 Diagnose Windows cancellation cleanup failures (resolved via Phase 1.3 Job Objects; verified on `windows-latest` CI)
- [ ] (Marco) Verify Phase 1 on Windows and enable BATON_ACCESS_KEY
  Pull `main` on Windows, run `pytest tests/test_runner.py -q` 5 times in a row,
  and add a generated `BATON_ACCESS_KEY` to `.env` (sign in once on PC and phone).
- [ ] (Marco) Send a first real Gemini task from Baton
  New task → Gemini, model `gemini-3.8-flash` (low) on a small change in this
  repo; check auto-merge without review, readable log, summary, and Quotas.
- [ ] Isolated Docker container for the All permissions mode
  Run Agents chat's OpenCode inside a Docker container mounting only the chosen
  folder (no Baton data, no Docker socket, no host profile). Start with local
  models (`local-inkling`, `local-qwen`) reaching the host's llama-server, then
  enable "All permissions" for that path only (red warning + badge, still refused
  for abliterated models). Verify with two sibling folders + junction as in
  [docs/agents-chat.md](docs/agents-chat.md) and a fake ACP agent test.
  Recommended: claude-code, opus, high
- [ ] Agents chat for OpenCode, Claude Code and Codex in Baton, with tools
  Phase 1 (OpenCode/Gemini over ACP, Confirm mode) is done. Remaining: Phase 2
  (Claude Code + Codex chat with session resume) and Full permissions mode once
  the isolated Docker container above exists ([docs/agents-chat.md](docs/agents-chat.md)).
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
- Agents chat "All permissions" mode is blocked on the isolated Docker container task above; Confirm mode runs approved commands as Marco's Windows user (Gemini Confirm covers commands only).

## Decisions
- 2026-11-25: Phase 3C live UX: `/tasks/{id}` streams live status, steps, progress, and formatted logs via `GET /api/tasks/{task_id}/events` (`task-live.js`); `task_report.py` formats Gemini `stream-json` logs (joined text deltas, one-line tool steps, `[result]` + tokens); `usage.collect()` runs in the background loop so `/quotas` never blocks on disk log scans; older history archived in [docs/history.md](docs/history.md).
- 2026-11-25: Phase 3B storage: tasks and rankings share `<data_dir>/baton.sqlite3` in WAL mode (`busy_timeout=30000`, one-time schema init, atomic `SAVEPOINT` task+ranking commits, lossless automatic migration from legacy `tasks.json` and `ranking.sqlite3`).
- 2026-11-25: Phase 3A modularization: `runner.py` delegates to `task_store.py`, `worktree.py`, `agents.py`, `review_merge.py`; `web.py` mounts eight FastAPI `APIRouter` modules under `baton/routes/` with isolated per-app `AppContext` (`app.state.ctx`).
- 2026-11-25: Phase 1–2 security & reliability: `agent_env()` strips secrets from all agent subprocesses; optional `BATON_ACCESS_KEY` locks the UI/API behind a signed `HttpOnly` cookie ([docs/security.md](docs/security.md)); Windows Job Objects (`baton/proc.py`), `SelfUpdate` failed-SHA skip, native `find_codex()`, `quotas.py` stdlib-only, cancellation during CI/review, and merged `baton/*` branch cleanup.
- 2026-11-25: auto-merge policy: Claude Code, Codex, and Gemini run with full permissions and auto-merge without review; OpenCode and local AI keep restricted rules and are reviewed by Claude Code (Opus, medium, `auto` mode); abliterated models never get full permissions ([docs/auto-merge.md](docs/auto-merge.md)). Earlier decisions in [docs/history.md](docs/history.md).

## Log
- 2026-11-25 (gemini): Completed hardening roadmap Phase 3C (live UX & hygiene): live task SSE (`GET /api/tasks/{task_id}/events` + `baton/static/task-live.js`), readable Gemini `stream-json` log formatting (`format_gemini_log` / `format_task_log` in `task_report.py`), non-blocking background `usage.collect()` in `deps.py`/`web.py`, and archived older `STATUS.md` history to `docs/history.md` (<100 lines). Linux VM suite: 223 passed, ruff and JS checks clean.
- 2026-11-25 (gemini): Completed hardening roadmap Phase 3B (unified SQLite WAL storage): `baton/db.py` (`baton.sqlite3` WAL, `busy_timeout=30000`, one-time schema init, automatic legacy migration), atomic `SAVEPOINT` commits in `TaskStore` + `Ranking`, `tests/test_db.py`. Suite: 219 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phase 3A (modularization): split `runner.py` (1,253 → 590) and `web.py` (935 → 276) into focused modules and `baton/routes/`, fixed expiring timestamp in `tests/test_gemini.py`, added `tests/test_modularization.py`. Suite: 214 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phase 2 (data accuracy, concurrency & repo hygiene). Suite: 211 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phase 1 (1.1–1.6: security, auth, Windows Job Objects, SelfUpdate skip, native `find_codex()`, Windows + lint CI). Suite: 198 passed (Linux + Windows CI).
- Earlier entries (2026-11-23 – 2026-11-25): see [docs/history.md](docs/history.md).
