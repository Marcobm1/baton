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
- **Baton's page** finds every `STATUS.md` in your GitHub account and shows
  them together, flags the ones that look out of date, and adds a
  **quota panel** with your Claude Code and Codex 5-hour and weekly windows,
  so you can see what is left to spend before it resets.

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

Open <http://127.0.0.1:8787>.

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
window**. The panel shows, for each window, how much is used, when it resets,
and your pace compared with an even spread (the tick on the bar is how much of
the window has passed). "Quota to spare" means you will probably reach the
reset with quota left over.

| Source | How | Notes |
|---|---|---|
| **Codex** (CLI, IDE, app) | Read automatically from Codex's own session logs in `~/.codex/sessions` (the `rate_limits` it records). | Updated whenever you use Codex. |
| **Claude Code** (terminal) | Set Baton as your status line command (below). Claude Code passes `rate_limits.five_hour` and `rate_limits.seven_day` to it, and Baton saves them to `~/.baton/claude-usage.json`. | Claude Code runs status line commands only in the terminal, not in the VS Code extension. Usage on claude.ai counts against the same limits. |
| **Anything else** | Type the % used and the reset time in *Add a reading by hand* (for example from claude.ai → Settings → Usage). | Saved to `~/.baton/manual-usage.json`. |

The newest reading of each window wins. Baton never reads or uses your Claude
or ChatGPT credentials.

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
| `BATON_DATA_DIR` | `~/.baton` | Quota readings (status line, manual). |
| `CODEX_HOME` | `~/.codex` | Where Codex keeps its logs. |

## CLI

```
baton serve [--host 127.0.0.1] [--port 8787]   the page
baton index [-o INDEX.md] [--lang es]          Markdown index of all projects
baton check STATUS.md ...                      validate STATUS.md files
baton new "Name" [--path STATUS.md]            create a STATUS.md
baton quotas                                   quota windows as JSON
baton statusline                               Claude Code status line command
```

JSON endpoints: `GET /api/projects`, `GET /api/quotas`.

## Security notes

- The page has no login. Docker publishes it on `127.0.0.1` only; keep it that
  way, because it can show private repositories.
- Use a read-only, fine-grained token limited to the repos you need.
- `STATUS.md` bodies are rendered as Markdown with raw HTML disabled.

## License

MIT, see [LICENSE](LICENSE).
