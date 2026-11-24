# Instructions for AI agents (copy into each tracked repo)

Paste the block below into the repository's `AGENTS.md` (read by Codex, Cursor,
Copilot, Gemini CLI and, when there is no `CLAUDE.md`, by Claude Code).

If the repository also has a `CLAUDE.md`, Claude Code reads that one instead of
`AGENTS.md`. Then either paste the same block into `CLAUDE.md` too, or put the
line `@AGENTS.md` near the top of `CLAUDE.md` so Claude Code imports it.

---

```markdown
## Project status (Baton)

This repository is tracked with [Baton](https://github.com/Marcobm1/baton).
`STATUS.md` is the single source of truth for where the project stands; a
dashboard and other AI agents read it to know what to do next.

- **Before you start:** read `STATUS.md`. Its `next` field and its "Next"
  list say what comes next. If the owner asked for something else, do that.
- **When you finish a work session that changed anything** (code, docs, data,
  decisions), update `STATUS.md` in the same commit or pull request:
  - front matter: `updated` = today (YYYY-MM-DD), `updated_by` = your tool
    (`claude-code`, `codex`, `chatgpt`, `human`, ...), `next` = the next
    concrete step. Change `status` or `priority` only when the owner decides it;
  - tick finished tasks (`- [x]`) and add new ones (`- [ ]`) under "Next";
    use a short task title, then indent optional lines by two spaces for the
    instructions, prerequisites and acceptance checks used in the task form.
    Prefix finished titles with YYYY-MM-DD when the completion date is known;
  - update "Blockers" and "Decisions" if they changed;
  - add one line at the top of "Log": `- YYYY-MM-DD (tool): what changed`.
- Keep it short: `STATUS.md` is a dashboard, not a diary. Details belong in
  the repository's own docs.
- Never put secrets, tokens, passwords or personal data in `STATUS.md`.
```
