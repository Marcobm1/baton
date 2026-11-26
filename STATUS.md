---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex, Gemini and OpenCode Go quota windows.
next: "Monitoring for other computers and servers, and alerts"
updated: 2026-11-25
updated_by: gemini
open_source: true
tags: [tooling, ai-agents]
links:
  Hub repo: https://github.com/Marcobm1/proyectos
---

## Done recently
- 2026-11-25: Hardening Phases 1–3, isolated Docker sandbox for Agents chat Full mode, Windows PC verification + fixes, and Agents chat Phase 2 (Claude Code + Codex with interactive approvals and SQLite session resume). Full archive in [docs/history.md](docs/history.md).

## Next
- [x] 2026-11-25 Hardening Phases 1–3 (security, auth, Job Objects, modularization, SQLite WAL, live task SSE, readable Gemini logs)
- [x] 2026-11-25 Isolated Docker container for the All permissions mode (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`, hardlink guard)
- [x] 2026-11-25 (Marco) Verify Phases 1–3 on Windows, enable BATON_ACCESS_KEY, sign in on desktop/phone, and build Docker sandbox image
- [x] 2026-11-25 Agents chat for OpenCode, Claude Code and Codex in Baton, with tools
  Completed Phase 2 (`baton/chat_claude.py`, `baton/chat_codex.py`, `chat_sessions` in
  `baton.sqlite3`): interactive Confirm approvals for Claude Code (`--permission-prompt-tool stdio`)
  and Codex (`codex app-server`), session resume across restarts from desktop/phone,
  account/credential scrubbing, Full mode refused (403), verified with faithful fake CLIs
  (`tests/test_agents_phase2.py`, [docs/agents-chat.md](docs/agents-chat.md)).
- [ ] (Marco) Live smoke check of Claude Code and Codex in `/chat` (Confirm approvals + resume)
  Open `/chat?agent=claude-code` and `/chat?agent=codex` in the scratch folder, verify
  Allow/Deny on a harmless command, restart Baton or start a new chat, and resume the saved session.
- [ ] (Marco) Send a first real Gemini task from Baton
  New task → Gemini, model `gemini-3.8-flash` (low) on a small change in this
  repo; check auto-merge without review, readable log, summary, and Quotas.
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
- 2026-11-25: Agents chat Phase 2 (`baton/chat_claude.py`, `baton/chat_codex.py`): Claude Code uses bidirectional `stream-json` + `--permission-prompt-tool stdio` (`--permission-mode manual`, `--resume`); Codex uses `codex app-server` (`sandbox: "read-only"`, `approvalPolicy: "untrusted"`, `thread/resume` with `excludeTurns: true`); allow-once only, `account`/`credential_expires_at`/`planType` scrubbed, Full mode refused (403), minimal session metadata persisted in `baton.sqlite3` (`chat_sessions`) ([docs/agents-chat.md](docs/agents-chat.md)).
- 2026-11-25: Windows verification fixes: `refresh_gemini` runs non-blocking in the background on `/quotas`; `_resolve_codex_native()` checks `vendor/<triple>/bin/codex.exe` (codex-cli 0.171.0); Full-mode Docker sandbox refuses folders with hardlinked files (`st_nlink > 1` via `os.lstat`) and pins `@opencode/cli@2.4.1`; `tests/test_sandbox.py` falls back to a Windows junction (`mklink /J`) when `os.symlink` lacks Developer Mode.
- 2026-11-25: Isolated Docker sandbox (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`): All permissions in Agents chat runs OpenCode inside a disposable `--cap-drop=ALL`, `no-new-privileges`, single-`/workspace`-mount container reaching host `llama-server` via `host.docker.internal` (POSIX passes `--user uid:gid` + `HOME=/tmp`); restricted to non-abliterated `local-*` models so no cloud credentials enter the container ([docs/agents-chat.md](docs/agents-chat.md)).
- 2026-11-25: Phase 1–3 hardening & auto-merge policy: `agent_env()`, `BATON_ACCESS_KEY` cookie auth ([docs/security.md](docs/security.md)), Windows Job Objects, modular `runner.py`/`baton/routes/`, unified SQLite WAL (`baton.sqlite3`), live task SSE, readable Gemini logs; Claude/Codex/Gemini auto-merge without review while OpenCode/local AI stay restricted + reviewed by Claude Code ([docs/auto-merge.md](docs/auto-merge.md)). Earlier decisions in [docs/history.md](docs/history.md).

## Log
- 2026-11-25 (gemini): Completed Agents chat Phase 2: added Claude Code (`baton/chat_claude.py`) and Codex (`baton/chat_codex.py`) backends with interactive Confirm approvals, SQLite `chat_sessions` resume across restarts (`baton/db.py`, `/api/agents/sessions`, UI session picker), secret & account scrubbing, Full mode 403 refusal, and faithful fake-CLI tests (`tests/test_agents_phase2.py`). Suite: 235 passed.
- 2026-11-25 (gemini): Applied Windows PC verification fixes: non-blocking Gemini quota refresh on `/quotas`, `vendor/<triple>/bin/codex.exe` native discovery (`codex-cli 0.171.0`), `st_nlink > 1` hardlink refusal via `os.lstat`, pinned `@opencode/cli@2.4.1`, Windows junction fallback. Suite: 230 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Added isolated Docker container for Agents chat All permissions mode (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`, UI red warning & badge, server-side local-only + abliterated refusal, real Docker confinement test). Suite: 227 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phases 1–3C (security, auth, Job Objects, modularization, SQLite WAL, live task SSE, readable Gemini logs, `STATUS.md` archive). Suite: 223 passed (Linux + Windows CI).
- Earlier entries (2026-11-23 – 2026-11-25): see [docs/history.md](docs/history.md).
