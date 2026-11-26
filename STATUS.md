---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex, Gemini and OpenCode Go quota windows.
next: "UI-1: Near-black dark theme (Claude Code desktop look)"
updated: 2026-11-26
updated_by: claude-code
open_source: true
tags: [tooling, ai-agents]
links:
  Hub repo: https://github.com/Marcobm1/proyectos
---

## Done recently
- 2026-11-26: UI-0: fixed invisible buttons in `/chat` (suggestion chips, attach `＋`, Sign out) and the "Convert to task" link showing on an empty chat; guarded by `tests/test_design.py`.
- 2026-11-26: Local model tool-calling harness in `/chat`: opt-in **Internet + tools** mode (off by default) with OpenAI-compatible multi-round tool loop (`baton/local_harness.py`), `web_search` (DuckDuckGo HTML or optional `BATON_SEARCH_URL` SearXNG JSON), SSRF-guarded `fetch_url`, `get_datetime`, AST `calculate`, optional allowlisted-folder file/command tools, and mandatory one-shot **Allow / Deny** confirmation on writes, commands, and `.env` reads (`docs/local-harness.md`, `tests/test_local_harness.py`).
- 2026-11-26: Chat-3 overhaul: magic-byte validated raster image attachments & `Ctrl+V` paste for both local vision models and agent chats (auto-cleaned on close), selectable system prompt presets + custom system prompt, and subtle context-window usage meter in local chat (`baton/models.py`, `baton/routes/chat.py`, `tests/test_chat_vision_presets.py`).
- 2026-11-26: Chat-2 overhaul: consecutive read/search tool grouping in `/chat` (`.ag-tool-group` keeping diffs, commands, and approvals prominent), bidirectional Chat ↔ Tasks bridge (**Convert to task →** via `/tasks/new?from_thread=...` + **Open in Chat** on `/tasks/{id}`), and delayed Telegram/Webhook alerts when an agent approval waits >30 s (`baton/agent_chat.py`, `baton/notify.py`, `tests/test_chat_phase2_ux.py`).
- 2026-11-26: Chat-1 overhaul: SQLite persistent chat history (`chat_threads` + `chat_messages`), collapsible cross-device sidebar with live search/rename/delete, upgraded safe Markdown (GFM tables, code-block copy bar, blockquotes, checklists), sticky auto-scroll with `↓` jump button, copy/regenerate/edit actions, and prompt suggestion chips (`baton/chat_history.py`, `tests/test_chat_history.py`).
- 2026-11-26: UX-1/2/3 overhaul: global UI decluttering, Projects & Tasks remodel with automatic `git diff --cached --numstat`, structured agent reports, secure local `.baton-artifacts/` image gallery, and Telegram `sendPhoto` delivery.

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
- [x] 2026-11-26 Chat-2: Agent tool grouping + Chat-to-Task bridge + Telegram approval alerts (`baton/agent_chat.py`, `baton/notify.py`, `tests/test_chat_phase2_ux.py`)
- [x] 2026-11-26 Chat-3: Chat image attachments (vision) + local system prompt presets + context usage meter (`baton/models.py`, `baton/routes/chat.py`, `tests/test_chat_vision_presets.py`)
- [x] 2026-11-26 Local model tool-calling harness in `/chat`: opt-in Internet + utilities + allowlisted-folder file/command tools with mandatory Allow/Deny on writes, commands, and `.env` reads (`baton/local_harness.py`, `docs/local-harness.md`, `tests/test_local_harness.py`)
- [x] 2026-11-26 UI-0: Fix invisible buttons and the always-visible "Convert to task" link (global `button` rule no longer forces its colour with `!important`; `[hidden]` always wins; `tests/test_design.py`)
- [ ] UI-1: Near-black dark theme (Claude Code desktop look)
  Change only the dark token blocks in `static/style.css` (both copies) to neutral near-black
  surfaces instead of blue-grey, for the whole app; update the Theme notes in `docs/design.md`.
  Verify: `pytest -q tests/test_design.py` (dark blocks match, WCAG AA) and a dark screenshot of `/`, `/tasks`, `/chat`.
  Recommended: claude-code, opus, medium
- [ ] UI-2: Full-height `/chat` layout like Claude Code desktop
  On `/chat` only: no `main` max-width/padding, history as a flush full-height left column,
  conversation centred (~760 px), composer anchored to the bottom, slim header (title, folder, branch,
  "⋯" menu with Convert to task / Rename / Delete). Keep the mobile drawer and tab bar.
  Recommended: claude-code, opus, medium
- [ ] UI-3: Composer as the control centre
  Agent, model (readable names), effort, folder and permission mode become chips in the composer's
  bottom row with popovers; locked (not hidden) once the chat starts. Enter / Shift+Enter / Esc / Shift+Tab.
  Recommended: claude-code, opus, medium
- [ ] UI-4: Claude Code-style messages
  Assistant text without a card, compact one-line tool rows with status dots, inline diffs with line
  numbers, inline approvals answerable with keys 1/2, collapsed "Thinking… Ns", status line with context.
  Recommended: claude-code, opus, medium
- [ ] UI-5: One chat component for local models and agents
  Merge the two branches of `templates/chat.html` into one composer and message renderer.
  Recommended: claude-code, opus, high
- [ ] Fix Windows CI: `tests/test_models.py::test_thermal_guard_without_page_visits[False]` (KeyError 'one' on `main` since the harness commit)
  Recommended: claude-code, opus, medium
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
- 2026-11-26 (Marco): UI reference for `/chat` and the app is the Claude Code desktop app; the dark theme becomes near-black like Claude Code across the whole app (replaces "blue-grey, never pure black"). Plan: UI-0 … UI-5 under Next.
- 2026-11-26: Native local-model tool-calling harness (`baton/local_harness.py`, [docs/local-harness.md](docs/local-harness.md)): opt-in per conversation (Offline by default), built-in DuckDuckGo HTML search + optional `BATON_SEARCH_URL` SearXNG JSON, SSRF-guarded `fetch_url`, `get_datetime`, AST `calculate`, and optional allowlisted-folder tools (`list_files`, `read_file`, `search_files`, `write_file`, `run_command`). Writes, commands, and `.env` reads ALWAYS require one-shot CSRF-protected Allow/Deny confirmation (never unattended, including for abliterated models).
- 2026-11-26: Three-phase Chat overhaul completed (Chat-1 SQLite history sidebar + upgraded Markdown + sticky scroll, Chat-2 tool grouping + Chat-to-Task bridge + Telegram approval pings, Chat-3 validated image attachments + system presets + context usage meter).
- 2026-11-26: Three-phase UX overhaul completed (UX-1 global decluttering, UX-2 Projects & Tasks remodel + automatic git diff stats, UX-3 structured agent reports + local `.baton-artifacts/` image gallery + optional Telegram `sendPhoto`, [docs/project-detail.md](docs/project-detail.md)).
- 2026-11-26: Hardening Phase 4 open-source cleanup: `(Owner)` is now the generic default for `BATON_OWNER_TAG` (plain names auto-wrapped), `BATON_CHAT_SCRATCH` defaults to empty on all OSes, and `BATON_SCHEDULED_TASK` defaults to `Baton` with strict allowlist validation against PowerShell injection ([docs/hardening-roadmap.md](docs/hardening-roadmap.md)).
- 2026-11-25: Machine monitoring & alerts Phases M1–M3 (`baton/machines.py`, `baton/monitor_agent.py`, `baton/alerts.py`, `baton/notify.py`): read-only push heartbeats with per-machine SHA-256 Bearer tokens, local PC self-monitoring, 24 h `machine_samples`, `AlertEngine` with 2-strike confirmation/hysteresis/cooldowns + sidebar badge, and async **Telegram** (primary) + generic **Webhook/ntfy** notifications for both machine alerts and finished/failed tasks ([docs/machines.md](docs/machines.md)).
- 2026-11-25: Agents chat Phase 2 (`baton/chat_claude.py`, `baton/chat_codex.py`): Claude Code uses bidirectional `stream-json` + `--permission-prompt-tool stdio` (`--permission-mode manual`, `--resume`); Codex uses `codex app-server` (`sandbox: "read-only"`, `approvalPolicy: "untrusted"`, `thread/resume` with `excludeTurns: true`); allow-once only, `account`/`credential_expires_at`/`planType` scrubbed, Full mode refused (403), minimal session metadata persisted in `baton.sqlite3` (`chat_sessions`) ([docs/agents-chat.md](docs/agents-chat.md)).
- 2026-11-25: Phase 1–3 hardening, isolated Docker sandbox (`baton/sandbox.py`, hardlink guard), Windows verification fixes, and auto-merge policy ([docs/security.md](docs/security.md), [docs/auto-merge.md](docs/auto-merge.md), [docs/history.md](docs/history.md)).

## Log
- 2026-11-26 (claude-code): UI-0 CSS fix for invisible buttons and `[hidden]`; added UI-1…UI-5 plan (Claude Code look) and the Windows CI fix to Next.
- 2026-11-26 (gemini): Added opt-in local model tool-calling harness in `/chat` (`baton/local_harness.py`, `/api/chat/{model_id}/harness`, `/api/chat-harness/{run_id}/{permission,cancel}`, `BATON_SEARCH_URL`, `docs/local-harness.md`, `tests/test_local_harness.py`).
- 2026-11-26 (gemini): Completed Chat-3 overhaul (magic-byte validated image attachments & `Ctrl+V` paste for local vision and agent chats with auto-cleanup on close, system prompt presets + custom system prompt, context usage meter in local chat, `tests/test_chat_vision_presets.py`).
- 2026-11-26 (gemini): Completed Chat-2 overhaul (grouped read-only tool calls in `/chat`, **Convert to task →** & **Open in Chat** bridge via `from_thread`/`folder`, and delayed Telegram/Webhook alerts for unanswered chat approvals >30 s, `tests/test_chat_phase2_ux.py`).
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
