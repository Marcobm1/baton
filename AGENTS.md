# AGENTS.md — working on Baton

Baton is a small FastAPI app (`baton/`) plus a CLI. Read `README.md` first.

- Talk to the owner (Marco) in Spanish; code, comments, docs and commits in English.
- Marco builds this to learn: explain the *why* of each step in the conversation
  (alternatives, trade-offs, new concepts with an example, what each part of a
  command does, the cause of any failure), and record important decisions in `docs/`.
- Run the tests before finishing: `pip install -e ".[dev]" && pytest -q`.
- Keep dependencies pinned to exact versions in `pyproject.toml`.
- `baton/statusline.py` and `baton/quotas.py` must stay standard-library only.
- Baton must never read, store or send AI-provider credentials (Claude, OpenAI).
  Quota data comes only from files the tools write themselves or from manual input.
- GitHub Actions: `permissions: {}` at the top, minimal per-job permissions,
  only GitHub-made `actions/*`, pinned to a full commit SHA with the version in a comment.
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
  - update "Blockers" and "Decisions" if they changed;
  - add one line at the top of "Log": `- YYYY-MM-DD (tool): what changed`.
- Keep it short: `STATUS.md` is a dashboard, not a diary.
- Never put secrets, tokens, passwords or personal data in `STATUS.md`.
