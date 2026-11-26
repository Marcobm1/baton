---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex, Gemini and OpenCode Go quota windows.
next: "Chat-2: Agent tool grouping + Chat-to-Task bridge + Telegram approval alerts"
updated: 2026-11-26
updated_by: gemini
open_source: true
tags: [tooling, ai-agents]
links:
  Hub repo: https://github.com/Marcobm1/proyectos
---

## Done recently
- 2026-11-26: Chat-1 overhaul: SQLite persistent chat history (`chat_threads` + `chat_messages`), collapsible cross-device sidebar with live search/rename/delete, upgraded safe Markdown (GFM tables, code-block copy bar, blockquotes, checklists), sticky auto-scroll with `↓` jump button, copy/regenerate/edit actions, and prompt suggestion chips (`baton/chat_history.py`, `tests/test_chat_history.py`).
- 2026-11-26: UX-3 structured agent reports + secure local `.baton-artifacts/` image gallery + Telegram `sendPhoto` delivery (`baton/artifacts.py`, `baton/static/lightbox.js`, SQLite `tasks.artifacts` migration, authenticated `/api/tasks/{id}/artifacts/{name}` with magic-byte verification & sandboxed CSP, lightbox gallery on `/tasks/{id}` and `/p/{id}?tab=done`, `tests/test_ux_artifacts.py`).
- 2026-11-26: UX-2 Projects & Tasks remodel (status filters + "Needs attention" tray + live repo activity on `/`, upgraded Deliveries cards with Markdown reports on `/p/{id}`, automatic `git diff --cached --numstat` capture persisted in SQLite `tasks.diff_stat` and rendered across `/`, `/p/{id}`, `/tasks`, `/tasks/{id}`).
- 2026-11-26: UX-1 global UI decluttering (removed verbose help prose across all pages, relocated Baton self-update to `/machines`, replaced duplicated host cards on `/models` with a compact VRAM strip, deduplicated `/tasks/{id}` summary + unified top action bar, compact `/chat` permission pills + auto-collapsing active header, mobile tab-bar alert badge, fixed `ChecklistItem.display_title` owner tag slicing).
- 2026-11-26: Hardening Phase 4 open-source cleanup (`BATON_OWNER_TAG`, `BATON_CHAT_SCRATCH`, `BATON_SCHEDULED_TASK` with injection guard; personal references scrubbed from code, docs, and tests).

## Next
- [x] 2026-11-25 Hardening Phases 1–3 (security, auth, Job Objects, modularization, SQLite WAL, live task SSE, readable Gemini logs)
- [x] 2026-11-25 Isolated Docker container for the All permissions mode (`baton/sandbox.py`, `docker/opencode-sandbox.Dockerfile`, hardlink guard)
- [x] 2026-11-25 (Marco) Verify Phases 1–3 on Windows, enable BATON_ACCESS_KEY, sign in on desktop/phone, and build Docker sandbox image
- [x] 2026-11-25 Agents chat for OpenCode, Claude Code and Codex in Baton, with tools (`baton/chat_claude.py`, `baton/chat_codex.py`, `chat_sessions` resume)
- [x] 2026-11-25 Monitoring for other computers and servers, and alerts (Phases M1–M3: `/machines`, `baton monitor-agent`, `AlertEngine`, Telegram + Webhook notifications for alerts & finished/failed tasks, [docs/machines.md](docs/machines.md))
- [x] 2026-11-26 Hardening Phase 4: open-source release cleanup (`BATON_OWNER_TAG`, `BATON_CHAT_SCRATCH`, `BATON_SCHEDULED_TASK`, generic defaults and scrubbed docs/tests, [docs/hardening-roadmap.md](docs/hardening-roadmap.md))
- [x] 2026-11-26 UX-1: Global UI decluttering and layout fixes (`tests/test_ux_cleanup.py`)
- [x] 2026-11-26 UX-2: Remodel Projects and Tasks views with automatic git diff stats (`baton/worktree.py`, `baton/db.py`, `baton/routes/projects.py`, `tests/test_ux_projects.py`)
- [x] 2026-11-26 UX-3: Structured agent reports + local image artifacts gallery + Telegram photo (`baton/artifacts.py`, `baton/static/lightbox.js`, `tests/test_ux_artifacts.py`)
- [x] 2026-11-26 (Marco) Configure Telegram bot, `BATON_OWNER_TAG`, and `BATON_CHAT_SCRATCH` in `.env` and test notifications on `/machines`
- [x] 2026-11-26 Chat-1: Persistent SQLite chat history sidebar, upgraded Markdown (tables + code copy), sticky auto-scroll, and regenerate/edit actions (`baton/chat_history.py`, `tests/test_chat_history.py`)
- [ ] Chat-2: Agent tool grouping + Chat-to-Task bridge + Telegram approval alerts
  Collapse consecutive read/search tool calls in `/chat` into a compact summary while keeping diffs, commands, and approvals prominent; add a "Convert to task →" button pre-filling `/tasks/new` from any chat thread; notify Telegram when an agent approval request waits >30 s.
  Recommended: gemini, gemini-4-pro, medium
- [ ] Chat-3: Chat image attachments (vision) + local system prompt presets + context usage meter
  Allow pasting/attaching validated raster images (reusing `baton/artifacts.py` magic-byte checks), add selectable system prompt presets and a subtle context-window usage indicator in local chat.
  Recommended: gemini, gemini-4-pro, medium
- [ ] (Marco) Live smoke check of Claude Code and Codex in `/chat` (Confirm approvals + resume)
  Open `/chat?agent=claude-code` and `/chat?agent=codex` in the scratch folder, verify
  Allow/Deny on a harmless command, restart Baton or start a new chat, and resume the saved session.
- [ ] (Marco) Send a first real Gemini task from Baton
  New task → Gemini, model `gemini-3.8-flash` (low) on a small change in this
  repo; check auto-merge without review, readable log, summary, Quotas, and Telegram notice.
- [ ] (Marco) Live smoke checks: queue two tasks on one repo and check a blocked card (hub: Cuantización → IA local)

## Blockers
- Confirm mode on the host runs approved commands as the host Windows user (Gemini Confirm covers commands only; Codex approved commands write even under `sandbox: "read-only"`).

## Decisions
- 2026-11-26: Three-phase Chat overhaul plan (Chat-1 SQLite history sidebar + upgraded Markdown + sticky scroll → Chat-2 tool grouping + Chat-to-Task bridge + Telegram approval pings → Chat-3 vision attachments + presets + context meter).
- 2026-11-26: Three-phase UX overhaul completed (UX-1 global decluttering, UX-2 Projects & Tasks remodel + automatic git diff stats, UX-3 structured agent reports + local `.baton-artifacts/` image gallery + optional Telegram `sendPhoto`, [docs/project-detail.md](docs/project-detail.md)).
- 2026-11-26: Hardening Phase 4 open-source cleanup: `(Owner)` is now the generic default for `BATON_OWNER_TAG` (plain names auto-wrapped), `BATON_CHAT_SCRATCH` defaults to empty on all OSes, and `BATON_SCHEDULED_TASK` defaults to `Baton` with strict allowlist validation against PowerShell injection ([docs/hardening-roadmap.md](docs/hardening-roadmap.md)).
- 2026-11-25: Machine monitoring & alerts Phases M1–M3 (`baton/machines.py`, `baton/monitor_agent.py`, `baton/alerts.py`, `baton/notify.py`): read-only push heartbeats with per-machine SHA-256 Bearer tokens, local PC self-monitoring, 24 h `machine_samples`, `AlertEngine` with 2-strike confirmation/hysteresis/cooldowns + sidebar badge, and async **Telegram** (primary) + generic **Webhook/ntfy** notifications for both machine alerts and finished/failed tasks ([docs/machines.md](docs/machines.md)).
- 2026-11-25: Agents chat Phase 2 (`baton/chat_claude.py`, `baton/chat_codex.py`): Claude Code uses bidirectional `stream-json` + `--permission-prompt-tool stdio` (`--permission-mode manual`, `--resume`); Codex uses `codex app-server` (`sandbox: "read-only"`, `approvalPolicy: "untrusted"`, `thread/resume` with `excludeTurns: true`); allow-once only, `account`/`credential_expires_at`/`planType` scrubbed, Full mode refused (403), minimal session metadata persisted in `baton.sqlite3` (`chat_sessions`) ([docs/agents-chat.md](docs/agents-chat.md)).
- 2026-11-25: Phase 1–3 hardening, isolated Docker sandbox (`baton/sandbox.py`, hardlink guard), Windows verification fixes, and auto-merge policy ([docs/security.md](docs/security.md), [docs/auto-merge.md](docs/auto-merge.md), [docs/history.md](docs/history.md)).

## Log
- 2026-11-26 (gemini): Completed Chat-1 overhaul (`baton/chat_history.py`, SQLite `chat_threads`/`chat_messages`, collapsible sidebar with search/rename/delete, GFM tables + code-block copy bar, sticky auto-scroll, copy/regenerate/edit actions, `tests/test_chat_history.py`).
- 2026-11-26 (gemini): Fixed `_load_dotenv` so later duplicate keys in `.env` override earlier empty placeholders, surfaced loaded `.env` path / missing Telegram key hints on `/machines`, and added CSRF-protected manual **Restart Baton** button (`POST /machines/restart`, `SelfUpdate.restart_now`).
- 2026-11-26 (gemini): Completed UX-3 structured agent reports + secure local `.baton-artifacts/` image gallery + Telegram `sendPhoto` (`baton/artifacts.py`, `baton/static/lightbox.js`, SQLite `tasks.artifacts`, `/api/tasks/{id}/artifacts/{filename}`, `tests/test_ux_artifacts.py`).
- 2026-11-26 (gemini): Completed UX-2 Projects & Tasks remodel (status filters, "Needs attention" tray, live per-repo activity on `/`, upgraded Deliveries cards on `/p/{id}`, automatic `git diff --cached --numstat` capture + SQLite migration, `tests/test_ux_projects.py`).
- 2026-11-26 (gemini): Completed UX-1 global UI decluttering across all pages (relocated `#self-update` to `/machines`, compact VRAM strip on `/models`, unified `/tasks/{id}` action bar without duplicate summary, compact `/chat` permission pills + auto-collapsing active header, mobile tab-bar alert badge, fixed `ChecklistItem.display_title`, `tests/test_ux_cleanup.py`). Suite: 254 passed (Linux + Windows CI).
- 2026-11-26 (gemini): Completed Hardening Phase 4 open-source cleanup (`BATON_OWNER_TAG`, `BATON_CHAT_SCRATCH`, `BATON_SCHEDULED_TASK` with injection guard, generic defaults, scrubbed code/docs/tests, `tests/test_phase4.py`). Suite: 250 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed Monitoring Phase M3 (`baton/notify.py`): async Telegram Bot + generic Webhook/ntfy notifications for machine alerts (`notified_at` stamped) and finished/failed tasks (`runner.on_finished`), token masking on errors, CSRF-protected test button on `/machines`, `.env.example`, and `tests/test_notify.py`. Suite: 245 passed.
- 2026-11-25 (gemini): Completed Monitoring Phase M2 (`baton/alerts.py`, `alerts` & `known_logins` tables, sidebar badge, Acknowledge UI). Suite: 242 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed Monitoring Phase M1 (`/machines`, `MachineStore`, `baton monitor-agent`, per-machine SHA-256 Bearer auth). Suite: 239 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Completed Agents chat Phase 2 (Claude Code + Codex + SQLite session resume). Suite: 235 passed (Linux + Windows CI).
- 2026-11-25 (gemini): Applied Windows PC verification fixes, isolated Docker sandbox, and hardening Phases 1–3C. Archive in [docs/history.md](docs/history.md).
