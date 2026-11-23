# Baton

**A shared project hub for you and your AI agents.**

If you work on several projects with more than one AI (Claude Code, Codex,
ChatGPT…), the state of each project tends to live in chat histories. Baton
moves it into the repositories themselves, so any agent can pick up the baton
where the previous one left it:

- every tracked repository keeps a small **`STATUS.md`** (what it is, where it
  stands, the next step);
- an **`AGENTS.md`** block tells every agent to read `STATUS.md` before working
  and to update it at the end of each session;
- **Baton's pages**: **Projects** finds every `STATUS.md` in your GitHub
  account and shows them together (next task, progress, out-of-date warnings);
  **Tasks** sends work to an AI agent with one form; **Quotas** shows your
  Claude and Codex 5-hour and weekly windows, so you can see what is left to
  spend before it resets.

```
 repo A ── STATUS.md ─┐
 repo B ── STATUS.md ─┼──► GitHub API ──► Baton page (projects + quotas)
 hub ── projects/*/   ┘                    └─► INDEX.md for agents (baton index)
        STATUS.md
        ▲
        └── agents update STATUS.md as part of their work (AGENTS.md rule)
```

Baton runs on your own computer. It only talks to the GitHub API with your
read-only token, and reads quota data from local files. Nothing is sent
anywhere else.

## Quick start (Docker)

```bash
git clone https://github.com/Marcobm1/baton.git
cd baton
cp .env.example .env      # then edit .env (see below)
docker compose up -d --build
```

Open <http://127.0.0.1:8787>. If the native service already uses port 8787,
set `BATON_PORT=8788` in `.env` before starting Compose. The Docker quota
page will then be at <http://127.0.0.1:8788/quotas>. After a key change,
run `docker compose up -d` to reload the container environment.

In `.env`, set at least `GITHUB_TOKEN` (a
[fine-grained token](https://github.com/settings/personal-access-tokens/new)
with **Contents: Read-only** and **Metadata: Read-only** on the repos you want
to see). For the quota panel, point `BATON_HOST_CODEX_DIR` at your `~/.codex`
folder and `BATON_HOST_DATA_DIR` at `~/.baton` (see [Quotas](#quota-panel)).

## Quick start (without Docker)

Python 3.11+:

```bash
git clone https://github.com/Marcobm1/baton.git
cd baton
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e .
baton serve            # http://127.0.0.1:8787
```

Without Docker, if `GITHUB_TOKEN` is empty Baton uses your GitHub CLI login
(`gh auth token`) when `gh` is installed. Settings can go in a `.env` file in
the folder where you run it.

## Tracking a project

1. Add a `STATUS.md` at the root of the repository (or create one with
   `baton new "My project"`):

   ```markdown
   ---
   name: My project
   status: active        # active | planned | paused | done
   priority: high        # high | medium | low
   summary: One line about what this is.
   next: The very next concrete step.
   updated: 2026-11-23
   updated_by: claude-code
   open_source: false
   ---

   ## Done recently
   - ...

   ## Next
   - [ ] a task          (check boxes feed the progress bar)

   ## Blockers
   ## Decisions
   ## Log
   - 2026-11-23 (claude-code): what changed
   ```

2. Add the agent block from [`docs/AGENT_INSTRUCTIONS.md`](docs/AGENT_INSTRUCTIONS.md)
   to the repository's `AGENTS.md`. Claude Code reads `AGENTS.md` only when the
   repository has no `CLAUDE.md`; if it has one, add `@AGENTS.md` to it (or
   paste the block there too).

3. Check the file with `baton check STATUS.md`.

That's all: the next time the page refreshes (every 5 minutes, or with the
*Refresh* button) the project appears.

### Projects without a repository: the hub repo

Ideas and projects that don't have a repository yet can live in a "hub"
repository as `projects/<name>/STATUS.md`; Baton shows each folder as a
project. The hub is also a good home for an **`INDEX.md`** that lists every
project, so an agent that can only open one repository (for example a Codex
cloud task) still sees the whole picture:

```bash
baton index -o INDEX.md          # or --lang es
```

[`docs/hub-index-workflow.yml`](docs/hub-index-workflow.yml) is a GitHub
Actions workflow that regenerates `INDEX.md` every day.

### "Out of date" warnings

A project shows ⚠ when its repository received a push on a later day than the
`updated` date in its `STATUS.md` (`BATON_STALE_DAYS`, default 1). It usually
means an agent worked on it and forgot the update.

## Quota panel

Subscription plans count usage in a rolling **5-hour window** and a **weekly
window** (OpenCode Go adds a **monthly** allowance). The panel shows, for each window, how much is used, when it resets,
and your pace compared with an even spread (the tick on the bar is how much of
the window has passed). "Quota to spare" means you will probably reach the
reset with quota left over.

| Source | How | Notes |
|---|---|---|
| **Codex** (CLI, IDE, app) | Read automatically from Codex's own session logs in `~/.codex/sessions` (the `rate_limits` it records). | Updated whenever you use Codex. |
| **Claude Code** (terminal) | Set Baton as your status line command (below). Claude Code passes `rate_limits.five_hour` and `rate_limits.seven_day` to it, and Baton saves them to `~/.baton/claude-usage.json`. | Claude Code runs status line commands only in the terminal, not in the VS Code extension. Usage on claude.ai counts against the same limits. |
| **Automatic checks** | Set `BATON_PING_CLAUDE_MINUTES` (and/or `BATON_PING_CODEX_MINUTES`). Every N minutes Baton runs the official CLI with a one-word prompt (`claude -p --model haiku --output-format stream-json --verbose`, `codex exec -`) and records the plan usage it reports. Also `baton ping [claude\|codex\|all]`. | Needs Baton running natively on the computer where the CLIs are logged in (not in Docker). Each check spends a little quota and opens a 5-hour window if none is open; `BATON_PING_HOURS=8-24` keeps it to your waking hours. Baton removes `ANTHROPIC_API_KEY` from the CLI's environment so the check always uses your plan. |
| **OpenCode Go** | Its documented usage endpoint, `GET https://opencode.ai/inference/go/v1/usage`, only if you set `BATON_OPENCODE_GO_KEY`. | Free to ask; Baton asks at most every 5 minutes, when the page is open. |

The newest reading of each window wins. Baton never reads or uses your Claude
or ChatGPT credentials: the CLIs use their own login. (Reading a subscription
token from a credentials file and calling the provider directly is what some
tools do; Anthropic's terms do not allow subscription OAuth tokens in other
tools, so Baton doesn't.) An API key would not help either: API usage is billed
separately and does not show your subscription's windows.

**The one optional exception is OpenCode Go**, whose usage is only available
from its own endpoint with your Go key. If you want that card, type the key
yourself in `.env` as `BATON_OPENCODE_GO_KEY`. Baton sends it only to
`opencode.ai` in the `Authorization` header; it never shows it, logs it or
writes it anywhere (it keeps only the percentages and reset dates).

Claude Code status line (`~/.claude/settings.json`):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python -m baton statusline"
  }
}
```

(Baton must be installed in that Python, e.g. `pip install -e .`. The script is
standard-library only, so you can also copy `baton/statusline.py` anywhere and
run `python /path/to/statusline.py`.) It prints a short line such as
`Opus · 5h 34% · 7d 12% · ctx 22%`.

## Sending tasks to agents

Every project card has a **New task** button. You write the task and choose
where it runs:

**On this computer** (while it's on; Baton itself runs here):

| Agent | How it runs | Limits |
|---|---|---|
| Claude Code | `claude -p --permission-mode auto` | Auto mode: a classifier reviews each action instead of asking you. |
| Codex | `codex exec -s workspace-write` | Sandbox: it can only write inside the task's folder. |
| OpenCode | `opencode run --auto --agent baton -m <model>` | Any model OpenCode lists (free ones, OpenCode Go, your ChatGPT plan, Ollama…). Baton's own `baton` agent starts by denying everything, then allows reading, editing and the terminal, and denies commits, pushes, branch changes, `gh`, MCP tools, Code Mode and subagents. **No OS sandbox:** its terminal runs as you and can write outside the folder (tested), so only send it tasks you wrote. |
| Local AI | `codex exec --oss --local-provider ollama -m <model>` | Needs `BATON_LOCAL_MODEL` and Ollama or LM Studio. |

For a local Claude Code or Codex task, choose a model and then its reasoning
effort. Both lists come from the installed CLI and are cached for ten minutes.
Leaving the selection at its default uses the CLI configuration. The task
history records explicit choices. Docker only offers local agents installed
inside the container.

Baton finds the repository's clone under `BATON_LOCAL_ROOT` (by its `origin`
remote; or `BATON_LOCAL_PATHS`), fetches, and creates a **git worktree** (a
second working folder of the same repository) on a new branch `baton/<id>`, so
the agent never touches the folder you work in. The agent is told to follow the
repository's `AGENTS.md`/`CLAUDE.md` and update `STATUS.md`, but not to commit
or push: Baton commits what changed, pushes the branch and opens a pull request
(a draft where your plan allows it), then removes the worktree. One task at a
time per repository; the log and the steps are on the task's page. Tasks use
your plans' quotas like any other use of those CLIs.

While a local task is running, its page has a **Cancel task** button. Baton
stops the agent *and every process it started* (on Windows with
`taskkill /T /F`, elsewhere by killing its process group), commits and pushes
nothing, removes the worktree and deletes the local branch, and marks the task
**cancelled** with the time. If the branch is already on GitHub (or Baton
can't check), the worktree and branch are kept and the page says so. Once Baton
has started committing the result (a matter of seconds) the task can no longer
be cancelled. The button uses the same cross-site protection as the task form.
See [`docs/cancelling-tasks.md`](docs/cancelling-tasks.md) for the details.

On Windows Baton calls `opencode.exe` directly, never npm's `opencode.cmd`
wrapper, so a task's text never goes through `cmd.exe`.

**In the cloud (GitHub Actions)**, an option tucked under the local choices:
Baton opens an issue that starts with `@claude`, and the
[Claude Code GitHub Action](https://github.com/anthropics/claude-code-action)
works on it on GitHub's machines (your computer can be off) and opens a pull
request. It uses your Claude plan (a token from `claude setup-token`) and
GitHub Actions minutes. To enable it on a repository, follow the comments at
the top of [`docs/claude-task-workflow.yml`](docs/claude-task-workflow.yml);
Baton's token then needs **Issues: Read and write**. Turn the option off with
`BATON_CLOUD_TASKS=0`.

The task form is protected against cross-site requests (a per-run secret in
the form and an Origin check), so another website open in your browser can't
start tasks through Baton.

Codex in the cloud: OpenAI documents `@codex` mentions in pull request
comments, not in issues, so Baton doesn't create Codex cloud tasks yet.

## Subtasks for cheaper models: `baton delegate`

Your main agent (Claude Code, Codex) or you can hand a bounded subtask to any
OpenCode model and get the answer back on the terminal:

```
baton delegate --list                                  models OpenCode offers
baton delegate -m opencode-go/<model> "Summarise how auth works in this repo"
"Draft tests for parse()" | baton delegate -m <model> -        (task from stdin)
baton delegate -m <model> --web "Find the paper ..."            may read the web
baton delegate -m <model> --write "Fix the typos in docs/"      may edit
```

- **Read-only by default** (agent `baton-helper`): it may only read and search
  the folder. Edits, the terminal, MCP tools, Code Mode and subagents are
  denied (tested: it can't create a file by any route).
- `--write` uses the tasks' agent: edits and the terminal, no commits or pushes.
  The terminal isn't sandboxed (see above): use it only with instructions you
  or your agents wrote. `--web` and `--write` can't be combined, so text from a
  web page never reaches a model with a terminal.
- Passing the task on stdin (`-`) avoids shell quoting problems, especially
  in Windows PowerShell 5.
- The main agent stays responsible: it should check what comes back, never
  send secrets or personal data, and spend cheap models on cheap work.

## Seeing Baton from your phone without putting it on the internet

Install [Tailscale](https://tailscale.com) on the computer and the phone with
the same account and run `tailscale serve --bg 8787` on the computer. The page
is then available at `https://<computer>.<tailnet>.ts.net`, only to your own
devices permitted by your tailnet rules. On the PC, run `tailscale serve status`
to find the exact HTTPS address. On the phone, connect Tailscale and open
that address in a browser. The PC must be awake with Baton running. The same
address works over Wi-Fi or mobile data. When native Baton and Docker run
side by side, Serve on port 8787 opens the native dashboard.

## Tokens spent

The Quotas page also counts the tokens each tool spent today and in the last
7 days, per model, from their own local logs: Claude Code
(`~/.claude/projects/**/*.jsonl`), Codex (`~/.codex/sessions/**/*.jsonl`) and
OpenCode (`~/.local/share/opencode/opencode.db`, opened read-only). Models
served by Ollama or LM Studio are grouped as *Local AI*. Only numbers, model
names and dates are read. Chats on claude.ai or chatgpt.com leave no log on
your computer, so they are not counted.

## Configuration

All settings are environment variables (or a `.env` file); see
[`.env.example`](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_TOKEN` | — | Read-only token. Without it only public repos are visible. |
| `BATON_OWNER` | token's user | Whose repositories to scan. |
| `BATON_REPOS` | — | Only these repos (`owner/name,...`). |
| `BATON_EXCLUDE` | — | Skip these repos. |
| `BATON_LANG` | `en` | `en` or `es`. |
| `BATON_CACHE_SECONDS` | `300` | Reuse GitHub answers for this long. |
| `BATON_STALE_DAYS` | `1` | Threshold for the ⚠ warning. |
| `BATON_DATA_DIR` | `~/.baton` | Quota readings, tasks, logs and task worktrees. |
| `BATON_LOCAL_ROOT` | — | Folder that contains your clones (for local tasks). |
| `BATON_LOCAL_PATHS` | — | `owner/repo=path;...` when a clone isn't found automatically. |
| `BATON_LOCAL_MODEL` | — | Model for the Local AI agent (e.g. an Ollama model name). |
| `BATON_TASK_TIMEOUT_MINUTES` | `60` | Stop a local task after this long. |
| `BATON_GIT_NAME` / `BATON_GIT_EMAIL` | Baton | Commit identity when the clone has none. |
| `BATON_CLOUD_TASKS` | `1` | `0` hides the GitHub Actions option. |
| `CODEX_HOME` | `~/.codex` | Where Codex keeps its logs. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Where Claude Code keeps its logs (tokens spent). |
| `BATON_OPENCODE_BIN` | found on PATH | Path to `opencode.exe` if it isn't found. |
| `BATON_OPENCODE_DB` | `~/.local/share/opencode/opencode.db` | OpenCode's database (tokens spent; read-only). |
| `BATON_OPENCODE_GO_KEY` | — | Optional: your OpenCode Go key, only to show its quota. Type it yourself. |

## CLI

```
baton serve [--host 127.0.0.1] [--port 8787]   the page
baton index [-o INDEX.md] [--lang es]          Markdown index of all projects
baton check STATUS.md ...                      validate STATUS.md files
baton new "Name" [--path STATUS.md]            create a STATUS.md
baton quotas                                   quota windows as JSON
baton ping [claude|codex|all]                  refresh quota readings now (spends a little quota)
baton statusline                               Claude Code status line command
baton delegate -m MODEL [--write|--web] TASK   give a subtask to an OpenCode model
```

JSON endpoints: `GET /api/projects`, `GET /api/quotas`.

## Security notes

- The page has no login. Docker publishes it on `127.0.0.1` only; keep it that
  way, because it can show private repositories.
- Use a read-only, fine-grained token limited to the repos you need.
- `STATUS.md` bodies are rendered as Markdown with raw HTML disabled.
- OpenCode has no OS sandbox. Baton's rules stop commits, pushes and its extra
  tools, not a terminal command that writes elsewhere. Read-only is the default
  for `baton delegate`.

## License

MIT, see [LICENSE](LICENSE).
