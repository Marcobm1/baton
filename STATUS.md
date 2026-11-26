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
- 2026-11-25: Hardening Phases 1–3, isolated Docker sandbox, Agents chat Phase 2 (Claude Code + Codex + session resume), and Monitoring Phases M1–M2 (`/machines`, push agent, alert engine with hysteresis & sidebar badge). Archive in [docs/history.md](docs/history.md).

## Next
- [x] 2026-11-25 Hardening Phases 1–3 (security, auth, Job Objects, modularization, SQLite WAL, live task SSE, readable Gemini logs)
- [x] 2026-11-25 Isolated Docker container for the All permissions mode (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`, hardlink guard)
- [x] 2026-11-25 (Marco) Verify Phases 1–3 on Windows, enable BATON_ACCESS_KEY, sign in on desktop/phone, and build Docker sandbox image
- [x] 2026-11-25 Agents chat for OpenCode, Claude Code and Codex in Baton, with tools (`baton/chat_claude.py`, `baton/chat_codex.py`, `chat_sessions` resume)
- [x] 2026-11-25 Monitoring Phase M1: `/machines` dashboard, local PC + remote push agent (`baton/machines.py`, `baton/monitor_agent.py`, `docs/machines.md`)
- [x] 2026-11-25 Monitoring Phase M2: alert engine with strikes, hysteresis, cooldowns, unusual GPU/network/login rules, sidebar badge & Acknowledge (`baton/alerts.py`, `tests/test_alerts.py`)
- [ ] (Marco) Live smoke check of Claude Code and Codex in `/chat` (Confirm approvals + resume)
  Open `/chat?agent=claude-code` and `/chat?agent=codex` in the scratch folder, verify
  Allow/Deny on a harmless command, restart Baton or start a new chat, and resume the saved session.
- [ ] (Marco) Send a first real Gemini task from Baton
  New task → Gemini, model `gemini-3.8-flash` (low) on a small change in this
  repo; check auto-merge without review, readable log, summary, and Quotas.
- [ ] Monitoring for other computers and servers, and alerts
  Phases M1–M2 (`/machines`, push agent, alert engine + UI badge/Ack) are done.
  Remaining: Phase M3 — pluggable async mobile notifications via `.env`
  (**Telegram Bot** primary + generic **Webhook / ntfy**) covering both machine
  alerts (`AlertEngine.on_opened`) and finished/failed tasks (`runner.on_finished`),
  plus a "Send test notification" button ([docs/machines.md](docs/machines.md)).
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
- 2026-11-25: Mobile notifications & future bot channel: Marco chose **Telegram** (free, official, zero templates/SIM/inbound webhook) for Phase M3 alerts + task completion notices (plus generic Webhook/ntfy), keeping any future conversational OpenCode assistant as a separate read-only project.
- 2026-11-25: Machine monitoring Phases M1–M2 (`baton/machines.py`, `baton/monitor_agent.py`, `baton/alerts.py`): read-only push heartbeats with per-machine SHA-256 Bearer tokens, local PC self-monitoring, 24 h `machine_samples`, and SQLite `alerts` + `known_logins` with 2-strike confirmation, hysteresis, 5-min cooldown, sidebar badge, and CSRF Acknowledge ([docs/machines.md](docs/machines.md)).
- 2026-11-25: Agents chat Phase 2 (`baton/chat_claude.py`, `baton/chat_codex.py`): Claude Code uses bidirectional `stream-json` + `--permission-prompt-tool stdio` (`--permission-mode manual`, `--resume`); Codex uses `codex app-server` (`sandbox: "read-only"`, `approvalPolicy: "untrusted"`, `thread/resume` with `excludeTurns: true`); allow-once only, `account`/`credential_expires_at`/`planType` scrubbed, Full mode refused (403), minimal session metadata persisted in `baton.sqlite3` (`chat_sessions`) ([docs/agents-chat.md](docs/agents-chat.md)).
- 2026-11-25: Phase 1–3 hardening, isolated Docker sandbox (`baton/sandbox.py`, hardlink guard), Windows verification fixes, and auto-merge policy ([docs/security.md](docs/security.md), [docs/auto-merge.md](docs/auto-merge.md), [docs/history.md](docs/history.md)).

## Log
- 2026-11-25 (gemini): Completed Monitoring Phase M2: added `alerts` & `known_logins` tables (`baton/db.py`), `AlertEngine` (`baton/alerts.py`) with 2-strike confirmation, hysteresis, cooldowns, offline/temp/RAM/disk/GPU/network/login rules, sidebar badge, Acknowledge routes & `/api/alerts`, and `tests/test_alerts.py`. Suite: 242 passed.
- 2026-11-25 (gemini): Completed Monitoring Phase M1 (`/machines`, `MachineStore`, `baton monitor-agent`, per-machine SHA-256 Bearer auth). Suite: 239 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed Agents chat Phase 2 (Claude Code + Codex + SQLite session resume). Suite: 235 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Applied Windows PC verification fixes & isolated Docker sandbox & hardening Phases 1–3C. Archive in [docs/history.md](docs/history.md).
