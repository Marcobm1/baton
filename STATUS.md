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
- 2026-11-25: Hardening Phases 1–3, isolated Docker sandbox, Agents chat Phase 2 (Claude Code + Codex + session resume), and Monitoring Phase M1 (local + remote `/machines`, per-machine SHA-256 Bearer tokens, `baton monitor-agent`). Archive in [docs/history.md](docs/history.md).

## Next
- [x] 2026-11-25 Hardening Phases 1–3 (security, auth, Job Objects, modularization, SQLite WAL, live task SSE, readable Gemini logs)
- [x] 2026-11-25 Isolated Docker container for the All permissions mode (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`, hardlink guard)
- [x] 2026-11-25 (Marco) Verify Phases 1–3 on Windows, enable BATON_ACCESS_KEY, sign in on desktop/phone, and build Docker sandbox image
- [x] 2026-11-25 Agents chat for OpenCode, Claude Code and Codex in Baton, with tools (`baton/chat_claude.py`, `baton/chat_codex.py`, `chat_sessions` resume)
- [x] 2026-11-25 Monitoring Phase M1: `/machines` dashboard, local PC + remote push agent (`baton/machines.py`, `baton/monitor_agent.py`, `baton/routes/machines.py`, `docs/machines.md`)
- [ ] (Marco) Live smoke check of Claude Code and Codex in `/chat` (Confirm approvals + resume)
  Open `/chat?agent=claude-code` and `/chat?agent=codex` in the scratch folder, verify
  Allow/Deny on a harmless command, restart Baton or start a new chat, and resume the saved session.
- [ ] (Marco) Send a first real Gemini task from Baton
  New task → Gemini, model `gemini-3.8-flash` (low) on a small change in this
  repo; check auto-merge without review, readable log, summary, and Quotas.
- [ ] Monitoring for other computers and servers, and alerts
  Phase M1 (`/machines`, local PC self-monitoring, per-machine SHA-256 Bearer tokens,
  `baton monitor-agent`, 24 h `machine_samples`) is done. Remaining: Phase M2 (alert
  engine with hysteresis/cooldowns for offline, temp, RAM/disk, GPU/network/login
  anomalies + UI badge & Ack) and Phase M3 (pluggable mobile alerts: WhatsApp via
  CallMeBot, Telegram, generic Webhook/ntfy) ([docs/machines.md](docs/machines.md)).
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
- 2026-11-25: Machine monitoring Phase M1 (`baton/machines.py`, `baton/monitor_agent.py`, `baton/routes/machines.py`): read-only push heartbeats to `POST /api/machines/heartbeat` authenticated by per-machine SHA-256 Bearer tokens (never `BATON_ACCESS_KEY`); local PC (`local`) monitored automatically with 24 h rolling `machine_samples` in `baton.sqlite3` and dedicated `/machines` sidebar tab ([docs/machines.md](docs/machines.md)).
- 2026-11-25: Agents chat Phase 2 (`baton/chat_claude.py`, `baton/chat_codex.py`): Claude Code uses bidirectional `stream-json` + `--permission-prompt-tool stdio` (`--permission-mode manual`, `--resume`); Codex uses `codex app-server` (`sandbox: "read-only"`, `approvalPolicy: "untrusted"`, `thread/resume` with `excludeTurns: true`); allow-once only, `account`/`credential_expires_at`/`planType` scrubbed, Full mode refused (403), minimal session metadata persisted in `baton.sqlite3` (`chat_sessions`) ([docs/agents-chat.md](docs/agents-chat.md)).
- 2026-11-25: Windows verification fixes: `refresh_gemini` runs non-blocking in the background on `/quotas`; `_resolve_codex_native()` checks `vendor/<triple>/bin/codex.exe` (codex-cli 0.171.0); Full-mode Docker sandbox refuses folders with hardlinked files (`st_nlink > 1` via `os.lstat`) and pins `@opencode/cli@2.4.1`; `tests/test_sandbox.py` falls back to a Windows junction (`mklink /J`) when `os.symlink` lacks Developer Mode.
- 2026-11-25: Isolated Docker sandbox (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`): All permissions in Agents chat runs OpenCode inside a disposable `--cap-drop=ALL`, `no-new-privileges`, single-`/workspace`-mount container reaching host `llama-server` via `host.docker.internal` (POSIX passes `--user uid:gid` + `HOME=/tmp`); restricted to non-abliterated `local-*` models so no cloud credentials enter the container ([docs/agents-chat.md](docs/agents-chat.md)).
- 2026-11-25: Phase 1–3 hardening & auto-merge policy: `agent_env()`, `BATON_ACCESS_KEY` cookie auth ([docs/security.md](docs/security.md)), Windows Job Objects, modular `runner.py`/`baton/routes/`, unified SQLite WAL (`baton.sqlite3`), live task SSE, readable Gemini logs; Claude/Codex/Gemini auto-merge without review while OpenCode/local AI stay restricted + reviewed by Claude Code ([docs/auto-merge.md](docs/auto-merge.md)). Earlier decisions in [docs/history.md](docs/history.md).

## Log
- 2026-11-25 (gemini): Completed Monitoring Phase M1: added `machines` & `machine_samples` tables (`baton/db.py`), local + remote `MachineStore` (`baton/machines.py`), read-only `baton monitor-agent` (`baton/monitor_agent.py`), `/machines` tab & live 5 s refresh (`baton/routes/machines.py`, `machines.html`/`js`/`css`), per-machine SHA-256 Bearer heartbeat isolation under `BATON_ACCESS_KEY`, and `tests/test_machines.py`. Suite: 239 passed.
- 2026-11-25 (gemini): Completed Agents chat Phase 2: Claude Code + Codex backends with interactive Confirm approvals, SQLite `chat_sessions` resume across restarts, and faithful fake-CLI tests. Suite: 235 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Applied Windows PC verification fixes (`refresh_gemini` non-blocking, `vendor/<triple>/bin/codex.exe`, `st_nlink > 1` hardlink refusal, pinned `@opencode/cli@2.4.1`, junction fallback). Suite: 230 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Added isolated Docker container for Agents chat All permissions mode (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`). Suite: 227 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed hardening roadmap Phases 1–3C. Suite: 223 passed (Linux + Windows CI).
- Earlier entries (2026-11-23 – 2026-11-25): see [docs/history.md](docs/history.md).
