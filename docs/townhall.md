# Town Hall in Huo (`/townhall`)

Huo mounts the standalone open-source [`ai-townhall`](https://github.com/Marcobm1/townhall) deliberation library at `/townhall` and `/townhall/{id}` so three core AI CLIs (**Gemini**, **Codex**, and **Claude Code**) can elect a 4th **OpenCode** guest by consensus, debate a topic or repository state, and produce a structured conclusion.

## How it works

1. **Dynamic 2–4 participants + live quota visibility + optional consensus guest:** Both the **Start a Town Hall** form and the **Schedule a recurring review** form open with **2 empty participant slots** (minimum 2, up to 4 via **+ Add model**) and no pre-selected models. For each slot you pick a **family** (`Claude Code`, `Codex`, `Gemini`, `OpenCode`) — labeled with its current remaining quota windows from `baton/quotas.py` (e.g. `5h 82% · 7d 65%`) so you can skip a provider that is out of quota — and then pick a **model** from that family. The **Moderator** selector updates dynamically to the chosen participants. The **Consensus guest** (`OpenCode Go`, `All OpenCode`, or `None`) stays independently optional (even alongside 4 chosen models).
2. **Two modes:**
   - **Council (debate → conclusion):** optional consensus-guest vote → parallel opening positions → configurable critique rounds → structured **Conclusion**, **Summary**, **Agreements**, **Disagreements**, and **Action Plan** by the chosen moderator.
   - **Open floor (free chat):** round-robin dialogue bounded by max turns and wall-clock time budget.
3. **Live progress & durable storage:** Each session runs in a background worker thread backed by `<data_dir>/townhall.sqlite3` (SQLite WAL). The detail page subscribes to `GET /api/townhall/{id}/events` (SSE with `id:` sequence numbers, `?after=` / `Last-Event-ID` resume, and 10 s `: keepalive` heartbeats) while running: `townhall.js` auto-reconnects with exponential backoff, displays which participants are currently thinking with an elapsed timer, logs `guest_fallback` switches, and reloads cleanly once finished (even if the debate completed during a brief disconnect). `/townhall` disables `<meta http-equiv="refresh">` so an active debate never wipes an in-progress form.
4. **Resilience & guest exclusions:**
   - Already-selected core participant models and any comma-separated IDs in `BATON_TOWNHALL_GUEST_EXCLUDE` (e.g. region-restricted OpenCode Go models such as `opencode-go/deepseek-v4-pro`) are automatically removed from the consensus guest voting pool.
   - Inside `ai-townhall`, if the #1 consensus guest fails in Round 1 (for example due to a provider region restriction), the engine automatically falls back to the next Borda runner-up, budgets debate/synthesis prompts safely below Windows' 32 KB `CreateProcessW` argument limit, isolates empty-workspace runs in a clean temporary directory, falls back to a healthy moderator if needed, and terminates the full subprocess tree promptly on Cancel.
5. **Huo bridges:**
   - **Convert to task →** (`/tasks/new?from_townhall={id}&repo=...`) turns a Town Hall conclusion and prioritized action plan into a prefilled task draft.
   - **Open in Chat** (`/chat?folder=...`) jumps straight to `/chat` in the same allowlisted workspace.
   - **Download .md** (`/townhall/{id}.md`) exports a portable Markdown report.
   - **Telegram / Webhook notices:** `Notifier.notify_townhall()` sends a summary when a council finishes or fails (including scheduled runs).
6. **Recurring schedules:** Weekly or interval-based debates (with custom 2–4 participant lineup, moderator, optional consensus guest, and `HH:MM-HH:MM` active window) are stored in `townhall.sqlite3` and ticked inside Huo's existing background loop (`_quota_tick`).

## Security invariants

- **Read-only deliberation by default:** `townhall`'s CLI adapters invoke each provider with read-only / no-write flags and run `sanitize_env()` so Huo secrets (such as `BATON_ACCESS_KEY`, Telegram tokens, or `GITHUB_TOKEN`) are not leaked into agent subprocesses.
- **Disposable git worktree:** When a session targets a git repository (non-demo), `townhall` creates a detached temporary git worktree and deletes it afterwards, so a debate never dirties a working checkout.
- **Allowlisted workspaces only:** `/townhall` never accepts arbitrary filesystem paths from the browser; `_resolve_folder()` validates every folder key strictly against `ctx.chat_folders()`.
- **Auth & CSRF:** All `/townhall*` and `/api/townhall*` routes inherit Huo's `BATON_ACCESS_KEY` middleware, and every state-changing `POST` checks origin and CSRF token via `ctx.check_post()`.
- **Pinned dependency:** `pyproject.toml` pins `ai-townhall` to an exact commit SHA of `Marcobm1/townhall`.
