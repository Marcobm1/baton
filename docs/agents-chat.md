# Agents chat design

The Chat page has an **Agent** selector. *Local model* is the original chat: Baton
relays and streams a local `llama-server` model's answer (no tools). *OpenCode*
is the Agents chat: OpenCode works in one folder with its tools, and Baton shows
every tool call and asks Marco before anything changes.

## Phase 1: OpenCode over ACP, Confirm mode (2026-11-25)

**Security boundary in this phase: Marco's approval.** There is no OS isolation
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
  `BATON_LOCAL_PATHS`) or the scratch folder `BATON_CHAT_SCRATCH`
  (`C:\IA\pruebas-opencode` by default on Windows, created on first use).
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
- 2026-11-25 on Marco's PC with OpenCode 2.4.1 and `local-inkling/inkling-2-mini`
  in `C:\IA\pruebas-opencode\baton-fase1`: the read tool ran without asking,
  an allowed write created the file, a denied write did not, reading
  `C:\Windows\win.ini` asked first (denied), a `cat` command asked first. The
  same through the web API with a separate Baton instance: the exact `printf ...`
  command was shown, allowed with CSRF, the file written, the stream ended.

### Limits of this phase

- **No OS isolation.** An *allowed* command runs as Marco's Windows user and can
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

## Next: isolated container for All permissions

Docker Desktop is installed. Run the agent in a container that mounts only the
chosen folder (no Baton data, no Docker socket, no host profile). Start with the
local models (`local-inkling`, `local-qwen`), which need no credentials: the
container reaches the `llama-server` on the host. Then enable All permissions
for that path only, with a red warning and badge, and keep it refused for
abliterated models.

## Working directory and permissions (background)

Starting an agent in a repository sets the base for relative paths. A child process can still open an absolute path with the Windows user's privileges. The existing OpenCode runner has demonstrated writes outside its starting directory. Codex's installed help also specifies external isolation for its bypass mode. Tailscale restricts access to Baton but cannot constrain commands Baton starts.

Run the tool process and its descendants inside an OS boundary with the selected repository or scratch directory as its only host workspace. Keep the hub's environment, data, Docker socket and host profiles out of reach. The existing Compose service mounts Baton data and Codex session logs and is unsuitable as the agent worker. CLI login and session state need a separate design that keeps credentials out of both Baton and the tool terminal.

Verify isolation with two temporary sibling directories, one mounted as the workspace and one holding a marker. An approved command and a child process should each be able to edit the workspace and be denied access to the sibling, including through a junction. Repeat in Full mode. Disable tool execution if the boundary cannot be established.

## Phase 2 notes: Claude Code and Codex

The Windows PATH check found neither a Claude ACP adapter nor a Codex ACP adapter. Codex exec supports stdin, JSON events and session resume. Its help did not advertise a bidirectional approval channel. Verify the approval exchange with a real client before exposing Confirm mode. A log event emitted after a command starts is too late for authorization. Verify Claude's streaming permission exchange separately.

Full mode should preapprove calls only inside the same boundary. Display a red warning and conversation badge, and reject Full on the server for abliterated models, including when selected via their OpenCode provider alias. Fake agents can verify the chat protocol and UI state. Host confinement and phone access require separate acceptance checks.
