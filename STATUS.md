---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude/Codex quota windows.
next: Marco cancels a real Claude Code task from its page and checks no node/claude process is left.
updated: 2026-11-23
updated_by: claude-code
open_source: true
tags: [tooling, ai-agents]
links:
  Hub repo: https://github.com/Marcobm1/proyectos
---

## Done recently
- 2026-11-23: first version: STATUS.md format, GitHub discovery, web page,
  quota panel (Codex logs, Claude Code status line, manual), CLI, Docker, tests.

## Next
- [x] STATUS.md format and parser
- [x] Web page with projects and quota panel
- [x] Docker + README instructions
- [x] Tested on Marco's Windows PC: native (gh login) and Docker (public repos without token)
- [ ] Marco uses it for a few days and gives feedback
- [ ] Daily INDEX.md workflow in the hub repo (needs `BATON_TOKEN` secret)
- [x] Automatic Claude/Codex quota checks via the official CLIs (`baton ping`, scheduler)
- [ ] Screenshots in the README
- [ ] Remote access without exposing it to the internet (Tailscale Serve)
- [x] "New task" form: GitHub issue for @claude (Claude Code GitHub Action)
- [x] Separate pages: Projects, Tasks, Quotas
- [x] Local tasks on this computer (Claude Code, Codex, local AI) in git worktrees, ending in a PR
- [x] Remote access through Tailscale Serve
- [ ] Codex cloud tasks (only documented from PR comments; test first)
- [x] OpenCode as a local agent, with any model it lists
- [x] Tokens spent per day/week, per app and model (Quotas page)
- [x] Cancel a running task from its page
- [ ] Try cancelling a real agent task on Windows (check Task Manager afterwards)

## Blockers
- None.

## Decisions
- 2026-11-23: the page reads STATUS.md from GitHub (not local folders), so it
  works the same for every agent and every computer.
- 2026-11-23: quota data only from local files written by the tools or typed
  by hand; Baton never touches provider credentials.
- 2026-11-23: cancelling kills the agent's whole process tree, commits/pushes
  nothing, and deletes the worktree + local branch only if the branch is not
  on GitHub (see docs/cancelling-tasks.md).

## Log
- 2026-11-23 (claude-code): Cancel task button (kills the process tree, no commit/push, removes worktree and branch); status cancelled/cancelada; tests.
- 2026-11-23 (claude): OpenCode agent (own permission rules, exe without shell); tokens spent per app/model.
- 2026-11-23 (claude): pages split (Projects/Tasks/Quotas); local tasks in worktrees; manual quota form removed.
- 2026-11-23 (claude): one quota card per provider; next task on project cards; New task form with CSRF protection; Claude task workflow template.
- 2026-11-23 (claude): runs at Windows logon (scheduled task, pythonw); log file when there is no console.
- 2026-11-23 (claude): automatic quota checks (claude -p stream-json unifiedWindows; codex exec).
- 2026-11-23 (claude): tested on Windows (native + Docker); fixed .env files with a BOM; Codex CLI oriented itself correctly from the hub's AGENTS.md + INDEX.md.
- 2026-11-23 (claude): v0.1.0 created.
