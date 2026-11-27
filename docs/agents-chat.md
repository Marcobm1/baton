# Agents chat design

The Chat page has an **Agent** selector. *Local model* is the original chat: Baton
relays and streams a local `llama-server` model's answer (no tools). *OpenCode*
is the Agents chat: OpenCode works in one folder with its tools, and Baton shows
every tool call and asks the owner before anything changes.

In the OpenCode chat, the catalog buttons filter the model picker by provider.
Go appears first on a fresh browser. Pay-as-you-go models have a separate
button and a credit warning. Other providers, including local ones, share a
third button and keep their provider labels. The chat restores the catalog
along with the saved model and locks both when a conversation starts.

## Phase 1: OpenCode over ACP, Confirm mode (2026-11-25)

**Security boundary in this phase: owner approval.** There is no OS isolation
yet. Reads inside the chosen folder run freely; everything else waits for
**Allow / Deny** in the chat. "All permissions" is visible but disabled until the
isolated container exists.

### How it works

- **Transport: ACP over stdio.** Baton starts `opencode acp` (the real
  `opencode.exe`, never the npm `.cmd` wrapper) once per conversation and speaks
  JSON-RPC, one JSON message per line, on its stdin/stdout. No port is opened.
  The user's text only travels as a JSON string in `session/prompt`; it never
  passes through a shell. Code: `baton/agent_chat.py`.
  - Why ACP and not `opencode serve`: `serve` opens an HTTP port that any local
    program could call, and one server would hold every conversation. With ACP
    the process belongs to exactly one conversation, and closing it ends
    everything that conversation started.
  - Baton advertises no `fs` or `terminal` capability, so OpenCode uses its own
    tools, which go through its permission rules. Any other request from the
    agent gets "method not found".
- **Fixed on the server.** `POST /api/agents` checks the agent (`opencode`), the
  model (must be in `opencode models`), the effort, the permission mode and the
  folder (a key from Baton's list, never a path from the browser), resolves the
  folder's canonical path, and starts the session. Model, effort and mode are set
  with `session/set_config_option` and Baton checks OpenCode's answer: if the
  `baton-chat` mode is missing or OpenCode keeps another value, the conversation
  is refused (fail closed). None of these can change later.
- **Folder per conversation:** a local clone Baton knows (`BATON_LOCAL_ROOT`,
  `BATON_LOCAL_PATHS`) or the optional scratch folder `BATON_CHAT_SCRATCH`
  (empty by default; created on first use when set).
- **Permissions (`baton-chat` agent, OpenCode 2.x rules, last match wins):**
  deny everything; allow `read`, `glob`, `grep`, `list`; ask for `read` of
  `*.env` / `*.env.*`, `edit` (also writes and patches), `shell` and
  `external_directory` (any path outside the folder); deny the same git
  commands as tasks (commit, push, checkout, reset, branch, `gh`, ...) and
  OpenCode's own tools, Code Mode, skills and subagents. Web search and fetch
  stay denied. Unit test: `test_confirm_rules_read_freely_and_ask_for_everything_else`.
- **Environment:** OpenCode gets Baton's environment minus `BATON_*`,
  `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT` and `OPENCODE_PERMISSION`. The
  provider logins stay in OpenCode's own files; Baton never reads them.
- **Permission requests.** A `session/request_permission` gets a random id
  bound to that conversation and that one JSON-RPC request. The chat shows the
  exact command or diff with Allow / Deny. `POST /api/agents/<chat>/permission`
  needs the CSRF token, looks the id up only in that conversation's pending
  requests and works once (a second answer, or an id from another conversation,
  gets 404). Allow sends OpenCode's *allow once* option, never *always allow*.
  With no answer in 10 minutes Baton denies. Stop (`session/cancel`) answers
  pending requests "cancelled".
- **Events.** OpenCode's updates become numbered events kept in memory per
  conversation (user text, reasoning, answer text, tool calls with arguments,
  result and diffs, permission requests and answers, plan, errors).
  `GET /api/agents/<chat>/events?after=N` streams them as server-sent events and
  ends when no turn is running. The browser reconnects with `Last-Event-ID`, so
  a reload or a phone back from sleep replays the conversation and still shows
  a pending request.
- **Lifetime.** Conversations live in Baton's memory: up to 4 OpenCode processes;
  an idle one closes after 30 minutes; "New chat" closes it; stopping Baton
  closes all of them (the process tree is killed, like a cancelled task). After a
  Baton restart the page says the conversation ended.
- **All permissions.** Refused with 403 on the server in this phase. For
  abliterated models it is refused always, also in later phases: a model whose
  name says abliterated/uncensored, or any `local-*` OpenCode provider while an
  abliterated model from `models.json` is running (Baton doesn't read OpenCode's
  config, which may hold keys, so it can't tell which port an alias points to).

### Verified

- Tests with a fake ACP agent (`tests/fake_acp_agent.py`,
  `tests/test_agents_chat.py`): multi-turn conversation, a read without asking,
  a write allowed and one denied, a command shown exactly, a permission answer
  from another conversation refused, CSRF, timeout → deny, Stop, All permissions
  refused (also abliterated), fixed folder/model/effort, no secrets in the
  agent's environment, refusal to start without Baton's rules.
- 2026-11-25 on Windows with OpenCode 2.4.1 and a local model in a scratch
  folder: the read tool ran without asking,
  an allowed write created the file, a denied write did not, reading
  `C:\Windows\win.ini` asked first (denied), a `cat` command asked first. The
  same through the web API with a separate Baton instance: the exact `printf ...`
  command was shown, allowed with CSRF, the file written, the stream ended.

### Limits of this phase

- **No OS isolation.** An *allowed* command runs as the host Windows user and can
  read or write anything that user can, including outside the folder. Read the
  command before allowing it; prefer the scratch folder for experiments.
- Reads inside the folder go to the model's provider without asking. With a
  cloud model, choose a folder you are happy to share; `.env` files ask first,
  but other secret files in the folder do not.
- OpenCode decides which tool to use: a small model may use `cat` (a command,
  so it asks) where `read` would have run freely.
- Conversations don't survive a Baton restart (OpenCode keeps the session and
  could resume it with `session/load`; not wired yet).
- Only OpenCode; Claude Code and Codex are phase 2.

## Gemini over `agy acp` (2026-11-25)

**Agent → Gemini** uses the same code with a second profile
(`baton/agent_chat.py`, `PROFILES`): Baton starts `agy acp` (the Antigravity CLI,
never with `--dangerously-skip-permissions`) and speaks the same ACP.

- **Models and effort.** The picker lists the Gemini models from `agy models`
  (`gemini-4-*`, `gemini-3.8-flash`, the Orchestrators), once per model; each
  option carries the thinking levels that model offers (`data-efforts`), and
  the effort list shows only those. On the server the model must be in that
  catalog. Baton sets `mode`, `model` and agy's `thought_level` option with
  `session/set_config_option` and checks each answer, like for OpenCode.
- **Permissions: agy's own "Default" mode, not Baton's rules.** agy offers the
  modes `default`, `accept-edits`, `auto` and `plan`; Baton sets `default`
  (fail closed: the conversation is refused if agy keeps another one). Checked
  with agy 1.5.17 on Windows:
  - a terminal command (`echo hola-baton`) sent `session/request_permission`
    with `allow_once` / `reject_once`; denying it stopped the command;
  - a file write inside the folder (`b.txt`) ran **without asking**, and
    reading files did too.
  So for Gemini the Confirm boundary covers commands, not edits inside the
  folder; the page says so under "Confirm". Baton cannot pass per-path rules
  as it does for OpenCode (`agy acp` has no `--settings` option, and Baton
  does not edit agy's own `settings.json`). Pick the scratch folder or a clone
  whose changes you can check with `git diff`.
- **Environment:** the same as OpenCode's minus OpenCode's inline config:
  no `BATON_*`, `GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_PAT`. agy keeps its
  Google login in its own files; Baton never reads them.
- **Tokens.** Each `session/prompt` answer from agy carries `usage`
  (`inputTokens`, `outputTokens`, `thoughtTokens`, `cachedReadTokens`); Baton
  appends those numbers and the model to `gemini-usage.jsonl` for the Quotas
  page. agy's input count leaves out cached input (input + output + cache read
  = total in the checked answers).
- **Tests:** `tests/test_gemini.py` runs the chat against the fake ACP agent in
  its `--gemini` role (starts in `auto`, so the test proves Baton switches to
  `default`).

## Isolated Docker container for All permissions (2026-11-25)

Build the sandbox image once on the host:

```bash
docker build -f docker/opencode-sandbox.Dockerfile -t baton-opencode-sandbox:latest .
```

(Override the image name with `BATON_SANDBOX_IMAGE` if needed.)

When Docker is running and the sandbox image is present locally, **All
permissions** unlocks on `/chat?agent=opencode` for **non-abliterated local
models (`local-*`) only**:

- **Hardened container (`baton/sandbox.py`):** Baton runs
  `docker run --rm -i --init --cap-drop=ALL --security-opt=no-new-privileges:true --pids-limit=256 --memory=4g --add-host=host.docker.internal:host-gateway`
  mounting **only** the canonical chosen folder at `/workspace`. No Baton data,
  no Docker socket, no host profile, and no host secrets (`GITHUB_TOKEN`,
  `BATON_*`, etc.) enter the container.
- **Local models only (zero credentials):** `sandbox_opencode_config()` maps
  Baton's `models.json` servers to `http://host.docker.internal:<port>/v1` via
  inline `OPENCODE_CONFIG_CONTENT` so the container reaches the host's
  `llama-server` without any API keys. Cloud/Go models stay refused (`403`) in
  All permissions mode; **abliterated models remain refused (`403`) always**.
- **Visible warning and badge:** selecting All permissions shows a red alert
  banner before starting; active All permissions sessions carry a red
  `ALL PERMISSIONS · DOCKER` badge and still log every tool call.
- **Hardlink guard (`find_hardlinks()`):** before starting a Full-permissions
  container, Baton scans the folder for regular files with `st_nlink > 1`
  (NTFS or POSIX hardlinks) and refuses with `403` if any exist, because a
  pre-existing hardlink to a sibling file shares its inode and remains readable
  inside a bind mount.
- **Verification (`tests/test_sandbox.py`):** unit tests verify the hardened
  `docker run` command, `FULL_RULES` defense-in-depth, hardlink refusal, and
  fake-ACP mode switch + server-side 403 guards; a real Docker acceptance test
  verifies two sibling folders + symlink (or Windows directory junction when
  `os.symlink` lacks Developer Mode privilege): both parent and child process
  edit `/workspace` and are denied access to the sibling, symlink/junction,
  Docker socket, and host secrets. Verified 2026-11-25 on both Linux Docker
  Engine and Windows Docker Desktop (`opencode v2.4.1`, `CapEff=0`,
  `NoNewPrivs=1`).

## Working directory and permissions (background)

Starting an agent in a repository sets the base for relative paths. A child process can still open an absolute path with the Windows user's privileges. The existing OpenCode runner has demonstrated writes outside its starting directory. Codex's installed help also specifies external isolation for its bypass mode. Tailscale restricts access to Baton but cannot constrain commands Baton starts.

Run the tool process and its descendants inside an OS boundary with the selected repository or scratch directory as its only host workspace. Keep the hub's environment, data, Docker socket and host profiles out of reach. The existing Compose service mounts Baton data and Codex session logs and is unsuitable as the agent worker. CLI login and session state need a separate design that keeps credentials out of both Baton and the tool terminal.

Verify isolation with two temporary sibling directories, one mounted as the workspace and one holding a marker. An approved command and a child process should each be able to edit the workspace and be denied access to the sibling, including through a junction. Repeat in Full mode. Disable tool execution if the boundary cannot be established.

## Phase 2: Claude Code and Codex with interactive approvals & session resume (2026-11-25)

Neither Claude Code nor Codex ships an ACP adapter on PATH. Both are now wired
into `/chat?agent=claude-code` and `/chat?agent=codex` using their verified
interactive protocols:

- **Claude Code (`baton/chat_claude.py`, verified on `claude.exe` 2.1.331):**
  - Launches `claude -p --input-format stream-json --output-format stream-json --verbose --permission-mode manual --permission-prompt-tool stdio --strict-mcp-config --setting-sources local --disallowedTools AskUserQuestion` (plus `--model`, `--effort`, and `--resume <session_id>`).
  - Handshake via `control_request` (`initialize`) → `control_response`; fails
    closed if Claude reports a bypass mode (`bypassPermissions`/`dontAsk`).
  - Interactive approvals arrive as `control_request` (`can_use_tool`) and
    answer via `control_response` (`allow` with `updatedInput` only, never
    `updatedPermissions`, or `deny` with message). Cancel sends `interrupt`.
  - Scrubs `account` and `credential_expires_at` (never stored, logged, or sent).
- **Codex (`baton/chat_codex.py`, verified on `codex-cli` 0.171.0):**
  - Launches native `codex.exe` via `find_codex()` as `codex app-server`
    (JSON-RPC 2.0 over stdio; `codex exec --json` has no interactive approval
    channel).
  - Handshake: `initialize` → `initialized` → `thread/start` or `thread/resume`
    (`excludeTurns: true`) with `sandbox: "read-only"` and
    `approvalPolicy: "untrusted"`; turns use `turn/start` and `turn/interrupt`.
  - Interactive approvals (`item/commandExecution/requestApproval`,
    `item/fileChange/requestApproval`, `item/permissions/requestApproval`, and
    legacy equivalents) answer with `decision: "accept"` (allow once, never
    `acceptForSession`) or `"decline"` / `"cancel"`.
  - Ignores all `account/*` notifications (`planType`, account info).
  - **Windows caveat:** an approved Codex command runs with write access as
    the host Windows user even under `sandbox: "read-only"`.
- **Session resume across restarts (`baton/db.py`, `chat_sessions` table):**
  - Baton saves minimal session metadata (`session_id`, `agent`, `folder_key`,
    `folder_label`, `model`, `effort`, first-prompt `title`, `updated_at` — zero
    credentials or raw transcripts) in `<data_dir>/baton.sqlite3` (capped at 50
    per agent).
  - `/chat?agent=claude-code` and `/chat?agent=codex` offer a **Session**
    selector (`GET /api/agents/sessions`) to resume any saved session from
    desktop or phone; `POST /api/agents` validates that `resume` belongs to a
    saved session for that agent (fail closed).
- **Permissions boundary:** Confirm mode remains the default on the host;
  **All permissions** requires the isolated multi-CLI Docker sandbox below
  (refused with `403` otherwise, and always refused for abliterated models).
- **Tests (`tests/test_agents_phase2.py`, `tests/fake_claude_chat.py`, `tests/fake_codex_app_server.py`):**
  verify CLI flags, free reads, allow/deny/timeout/cancel approvals, fail-closed
  bypass rejection, environment & `account`/`credential_expires_at` scrubbing,
  Full mode 403 refusal without sandbox, auth on `/api/agents/sessions`, and
  session resume across a simulated Baton restart.

## Multi-CLI Docker sandbox for Claude Code, Codex, and Gemini (2026-11-27)

**All permissions** in `/chat` also supports **Claude Code**, **Codex**, and
**Gemini (`agy`)** inside a hardened Linux container built from
`docker/agents-sandbox.Dockerfile`:

```bash
# 1. Build the multi-CLI sandbox image once on the host:
docker build -f docker/agents-sandbox.Dockerfile -t baton-agents-sandbox:latest .

# 2. Sign in once per CLI you want to unlock (one isolated Docker volume per CLI):
baton sandbox-login claude-code
baton sandbox-login codex
baton sandbox-login gemini
```

(Override the image tag with `BATON_AGENTS_SANDBOX_IMAGE` if needed.)

### Isolation and credential design

- **Zero host credential access by Baton:** Baton never reads, copies, stores,
  or forwards host OAuth tokens or host `~/.claude` / `~/.codex` / `~/.gemini`
  profiles. Instead, each CLI signs in directly inside a disposable login
  container (`baton sandbox-login <agent>`, mounting **no** host folder) backed
  by its own dedicated Docker named volume:
  - `claude-code` → `baton-sandbox-claude-auth` → `/home/agent/.claude` (`CLAUDE_CONFIG_DIR=/home/agent/.claude`, `DISABLE_AUTOUPDATER=1`)
  - `codex` → `baton-sandbox-codex-auth` → `/home/agent/.codex` (`CODEX_HOME=/home/agent/.codex`, login via `codex login --device-auth`)
  - `gemini` → `baton-sandbox-gemini-auth` → `/home/agent/.gemini` (`XDG_CONFIG_HOME` and `XDG_DATA_HOME` routed inside the volume)
- **Strict per-agent volume separation:** a Claude Code container never mounts
  Codex's or Gemini's auth volume (and vice versa). Baton checks readiness with
  `docker image inspect` + `docker volume inspect` only — it never inspects the
  files inside any volume.
- **Hardened runtime (`sandbox_agent_docker_command()`):**
  `docker run --rm -i --init --cap-drop=ALL --security-opt=no-new-privileges:true --pids-limit=256 --memory=4g`,
  bind-mounting **only** the canonical selected workspace folder at `/workspace`
  plus that single agent's auth volume. No Windows/host home directory, no
  sibling repos, no `.baton` data, no Docker socket, and no host secrets
  (`GITHUB_TOKEN`, `BATON_*`, `ANTHROPIC_API_KEY`, etc.).
- **In-container protocol adjustments & fail-closed guards:**
  - **Claude Code (`baton/chat_claude.py`):** passes `--dangerously-skip-permissions`
    inside Docker only; `ClaudeChat.start()` and `system/init` fail closed if
    `mode == "full"` without `isolated=True` or if bypass mode appears outside
    Docker.
  - **Codex (`baton/chat_codex.py`):** passes `cwd="/workspace"`,
    `sandbox="danger-full-access"`, and `approvalPolicy="never"` inside Docker
    only (nested Landlock/userns sandboxing is unavailable under `--cap-drop=ALL`;
    Docker itself is the boundary). Fails closed if `mode == "full"` without
    `isolated=True`.
  - **Gemini (`baton/agent_chat.py`):** runs `agy acp --dangerously-skip-permissions`
    with `cwd="/workspace"` and ACP mode `"auto"` inside Docker only; fails
    closed if `mode == "full"` without `isolated=True`.
  - **Hardlinks & abliterated models:** `find_hardlinks(folder)` refuses (`403`)
    any workspace containing hardlinks (`st_nlink > 1`), and abliterated models
    remain permanently refused (`403`).
- **Residual trade-off (explicit):** host isolation is real (a rogue command
  cannot reach Windows, sibling projects, SSH keys, Baton data, or other agents'
  volumes), **but** because the CLI process inside the container needs its own
  OAuth token to talk to Anthropic/OpenAI/Google, a malicious command running as
  the same container user could read that single CLI's auth volume and exfiltrate
  it over the internet. Only point All-permissions containers at repositories
  and prompts you trust, and revoke/delete a volume anytime with
  `docker volume rm baton-sandbox-<agent>-auth`.

