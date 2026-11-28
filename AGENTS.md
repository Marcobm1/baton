# AGENTS.md — working on Baton

Baton is a small FastAPI app (`baton/`) plus a CLI. Read `README.md` first.

- Talk to the owner (Marco) in Spanish; code, comments, docs and commits in English.
- Marco builds this to learn: explain the *why* of each step in the conversation
  (alternatives, trade-offs, new concepts with an example, what each part of a
  command does, the cause of any failure), and record important decisions in `docs/`.
- Run the tests before finishing: `pip install -e ".[dev]" && pytest -q`.
- Keep dependencies pinned to exact versions in `pyproject.toml`.
- `baton/statusline.py` and `baton/quotas.py` must stay standard-library only.
- Baton must never read, store or send AI-provider credentials (Claude, OpenAI,
  Google). Quota data comes only from what the official CLIs report themselves.
- Local tasks must start agents only inside their own git worktree. Claude Code,
  Codex and Gemini run with full permissions (Marco's decision, 2026-11-25; see
  `docs/auto-merge.md`): Claude `--dangerously-skip-permissions`, Codex
  `--dangerously-bypass-approvals-and-sandbox`, Gemini (`agy`)
  `--dangerously-skip-permissions`, and their work merges without a review.
  OpenCode (its `baton` agent: deny everything first, then allow) and local AI
  (Codex `workspace-write`) keep limited modes and are reviewed by Claude Code,
  which reviews in its limited `auto` mode. Abliterated models never get full
  permissions. Never pass a task's text through a shell or a `.cmd` wrapper.
- `baton/usage.py` reads local logs for numbers only, never conversation text
  (Gemini's token counts are written by Baton itself to `gemini-usage.jsonl`).
- GitHub Actions: `permissions: {}` at the top, minimal per-job permissions,
  only GitHub-made `actions/*` (plus `anthropics/claude-code-action` in
  `claude.yml`), pinned to a full commit SHA with the version in a comment.
- No secrets in the repository; `.env` is ignored.

## Project status (Baton)

This repository is tracked with Baton itself. `STATUS.md` is the single source
of truth for where the project stands.

- **Before you start:** read `STATUS.md`. Its `next` field and its "Next"
  list say what comes next. If the owner asked for something else, do that.
- **When you finish a work session that changed anything** (code, docs, data,
  decisions), update `STATUS.md` in the same commit or pull request:
  - front matter: `updated` = today (YYYY-MM-DD), `updated_by` = your tool
    (`claude-code`, `codex`, `chatgpt`, `human`, ...), `next` = the next
    concrete step. Change `status` or `priority` only when the owner decides it;
  - tick finished tasks (`- [x]`) and add new ones (`- [ ]`) under "Next";
    prefix actions only Marco can take with `(Marco)`. Look up links in
    `tasks.json` or on GitHub. For an ongoing project, make the first unchecked
    agent task something an agent can complete independently.
    Give it a short title and an indented description of the work and how to
    verify it, even if provisional. Set `next` to the same title.
    Add a line `Recommended: <agent>, <model>, <effort>` to its description
    (agent: claude-code, codex, gemini, opencode or local) with what fits it best;
    if it must be done in another repository, add `Repo: <owner/repo>`;
    Prefix completed titles with YYYY-MM-DD when the date is known;
  - if `## Next` uses task-map groups (`### Group (PFX)`), task IDs (`[AP-01]`),
    dependencies (`(after: AP-00)`), or two-space indented subtasks (`  - [ ] …`),
    preserve them (when ticking a task with an ID, place the date after the ID:
    `- [x] [AP-01] YYYY-MM-DD Title`);
  - update "Blockers" and "Decisions" if they changed;
  - add one line at the top of "Log": `- YYYY-MM-DD (tool): what changed`.
- Keep it short: `STATUS.md` is a dashboard, not a diary.
- Never put secrets, tokens, passwords or personal data in `STATUS.md`.

## Subtasks with `baton delegate`

You may give bounded subtasks (summaries, searches, test drafts, second
opinions) to cheaper OpenCode models: `baton delegate -m <model> -` with the
task on stdin; `baton delegate --list` shows the models. Read-only is the
default; use `--write` only with instructions you wrote, never with outside
content. Check what comes back before using it, and never send secrets or
personal data.

When delegating, pass a descriptive `--type`. After checking the result,
record the agent evaluation with `baton rate` and the delegation ID
(see `docs/ranking.md`).
