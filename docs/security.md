# Local Security & Subprocess Isolation

This document records the security boundaries added in Phase 1 of the hardening roadmap (`docs/hardening-roadmap.md`).

## 1. Subprocess environment isolation (`agent_env()`)

When Baton starts, it loads `.env` into its own process environment (`os.environ`). By default on both Windows and Linux, every child process inherits its parent's full environment.

To prevent any AI CLI or restricted agent (`opencode`, local AI, `baton delegate`, Agents chat, quota pings, model discovery, and reviewers) from reading Baton's secrets, all agent subprocesses receive a filtered copy returned by `baton.ping.agent_env()`.

Variables stripped case-insensitively:
- Every `BATON_*` variable (`BATON_ACCESS_KEY`, `BATON_OPENCODE_GO_KEY`, `BATON_OWNER`, `BATON_LOCAL_ROOT`, etc.)
- `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT`
- `ANTHROPIC_API_KEY` (so Claude Code always uses the subscription plan, not API billing)
- `OPENCODE_PERMISSION` (so an inherited override cannot weaken Baton's deny-first OpenCode rules)

Baton's own process environment is never mutated, so its GitHub API calls, `git fetch`/`push`, quota checks, and authentication continue to work normally. For Claude Code and Codex tasks/reviews, `Ranking.worktree_env()` injects only `BATON_DELEGATION_DIR` *after* `agent_env()`, scoped to a temporary directory inside the task worktree.

## 2. Preventing local privilege escalation (`BATON_ACCESS_KEY`)

### Threat model
Baton runs full-permission auto-merging agents (Claude Code, Codex, Gemini) alongside restricted agents (OpenCode, local AI, and future containerized chats). Without authentication on `http://127.0.0.1:8787`, any restricted local process that can reach loopback could:
1. `GET /tasks/new` to scrape the CSRF token, and
2. `POST /tasks` to launch a full-permission Claude Code / Codex / Gemini task with auto-merge, start/stop local models, open an agent chat, or trigger a self-update.

### Design
Setting `BATON_ACCESS_KEY` in `.env` enables session-cookie authentication via a centralized HTTP middleware in `baton/web.py` (fail-closed by default):

- **Public routes:** only `GET/HEAD /healthz`, `GET/HEAD /static/*`, and `GET/HEAD/POST /login`.
- **Unauthenticated requests:** HTML `GET`/`HEAD` requests redirect (`303`) to `/login?next=<safe_path>` without rendering the main CSRF token; all `/api/*` requests and all non-GET/HEAD requests return `401 Unauthorized` before reaching any route handler—even if a CSRF token is supplied.
- **Session cookie (`baton_session`):** HMAC-SHA256 signed payload (`v1.<expires>.<nonce>.<sig>`) using a key derived from `BATON_ACCESS_KEY`. Flags:
  - `HttpOnly`: unreadable by JavaScript.
  - `SameSite=Strict`: never attached to cross-site navigations/requests.
  - `Secure`: enabled automatically when served over HTTPS (including behind Tailscale Serve via `X-Forwarded-Proto: https`) and omitted on plain `http://127.0.0.1`.
  - **30-day lifetime** and key-derived signature: sessions survive Baton restarts (`SelfUpdate`) on desktop and phone, while rotating `BATON_ACCESS_KEY` immediately invalidates every session.
- **Separate login CSRF + Origin check + rate limiter:** `/login` uses its own CSRF token (never exposing the authenticated form CSRF token) and throttles repeated failed attempts (`429 Too Many Requests` after 10 failures within 60 seconds).

### Why `Tailscale-User-Login` is NOT trusted on `127.0.0.1`
When Baton binds to `127.0.0.1:8787`, Tailscale Serve proxies phone requests to `127.0.0.1:8787`—the exact same loopback socket reachable by restricted local agents on the host. Any local process can spoof `Tailscale-User-Login` directly against `127.0.0.1:8787`, which would bypass authentication completely. Requiring the `BATON_ACCESS_KEY` cookie once every 30 days on each browser closes that bypass with almost zero friction.

### Setup
Generate a random key and add it to `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```dotenv
BATON_ACCESS_KEY=<paste-generated-key>
```

Restart Baton, then sign in once from your desktop browser and once from your phone.

## 3. Avoiding `.cmd` wrappers on Windows (`find_codex()`)

Executing a `.cmd` or `.bat` npm wrapper on Windows implicitly invokes `cmd.exe`, which:
- interprets shell metacharacters (`&`, `|`, `<`, `>`, `^`, `%VAR%`) in CLI arguments (model names, effort levels, paths), and
- inserts extra `cmd.exe` / `node.exe` parent layers that complicate process-tree teardown.

`baton.ping.find_codex()` therefore prefers native binaries in this order on Windows:
1. Explicit `BATON_CODEX_BIN` if configured.
2. `codex.exe` directly on `PATH` (or any non-wrapper `codex` on `PATH`).
3. When `PATH` resolves to an npm wrapper (`codex.cmd` / `codex.ps1` / `codex.bat`), the native `codex.exe` shipped inside the adjacent `node_modules/@openai/codex*` package for the host architecture (`x64` / `arm64`), matching how `find_opencode()` resolves `opencode.exe`.
4. Only if no native binary exists anywhere does `find_codex()` fall back to the `.cmd` wrapper so custom installations keep working.

