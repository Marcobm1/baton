# Town Hall in Huo (`/townhall`)

Huo mounts the standalone open-source [`ai-townhall`](https://github.com/Marcobm1/townhall) deliberation library at `/townhall` and `/townhall/{id}` so three core AI CLIs (**Gemini**, **Codex**, and **Claude Code**) can elect a 4th **OpenCode** guest by consensus, debate a topic or repository state, and produce a structured conclusion.

## How it works

1. **Core council + consensus 4th guest:** Huo automatically pre-selects the strongest installed model from each CLI catalog (`pick_strong_model` prefers Neon / Astra / Opus when available) and offers the installed **OpenCode Go** pool (or all OpenCode models) for the 4th seat. In **Council** mode, the 3 core models vote in parallel and `townhall` resolves the winner via Majority → Borda count → deterministic tie-breaker.
2. **Two modes:**
   - **Council (debate → conclusion):** 4th-guest vote → parallel opening positions → configurable critique rounds → structured **Conclusion**, **Summary**, **Agreements**, **Disagreements**, and **Action Plan** by the chosen moderator.
   - **Open floor (free chat):** round-robin dialogue bounded by max turns and wall-clock time budget.
3. **Live progress & durable storage:** Each session runs in a background worker thread backed by `<data_dir>/townhall.sqlite3` (SQLite WAL). The detail page subscribes to `GET /api/townhall/{id}/events` (SSE) while running and reloads when finished to display the Conclusion, Summary, Guest Election table, and collapsible Full Conversation.
4. **Huo bridges:**
   - **Convert to task →** (`/tasks/new?from_townhall={id}&repo=...`) turns a Town Hall conclusion and prioritized action plan into a prefilled task draft.
   - **Open in Chat** (`/chat?folder=...`) jumps straight to `/chat` in the same allowlisted workspace.
   - **Download .md** (`/townhall/{id}.md`) exports a portable Markdown report.
   - **Telegram / Webhook notices:** `Notifier.notify_townhall()` sends a summary when a council finishes or fails (including scheduled runs).
5. **Recurring schedules:** Weekly or interval-based debates (with optional `HH:MM-HH:MM` active window) are stored in `townhall.sqlite3` and ticked inside Huo's existing background loop (`_quota_tick`).

## Security invariants

- **Read-only deliberation by default:** `townhall`'s CLI adapters invoke each provider with read-only / no-write flags and run `sanitize_env()` so Huo secrets (such as `BATON_ACCESS_KEY`, Telegram tokens, or `GITHUB_TOKEN`) are not leaked into agent subprocesses.
- **Disposable git worktree:** When a session targets a git repository (non-demo), `townhall` creates a detached temporary git worktree and deletes it afterwards, so a debate never dirties a working checkout.
- **Allowlisted workspaces only:** `/townhall` never accepts arbitrary filesystem paths from the browser; `_resolve_folder()` validates every folder key strictly against `ctx.chat_folders()`.
- **Auth & CSRF:** All `/townhall*` and `/api/townhall*` routes inherit Huo's `BATON_ACCESS_KEY` middleware, and every state-changing `POST` checks origin and CSRF token via `ctx.check_post()`.
- **Pinned dependency:** `pyproject.toml` pins `ai-townhall` to an exact commit SHA of `Marcobm1/townhall`.
