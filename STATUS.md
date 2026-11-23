---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex and OpenCode Go quota windows.
next: Verify the task summary with a live agent from the web.
updated: 2026-11-23
updated_by: codex
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
- [x] "New task" form: GitHub issue for @claude (Claude Code GitHub Action)
- [x] Separate pages: Projects, Tasks, Quotas
- [x] Local tasks on this computer (Claude Code, Codex, local AI) in git worktrees, ending in a PR
- [x] Remote access through Tailscale Serve
- [ ] Codex cloud tasks (only documented from PR comments; test first)
- [x] OpenCode as a local agent, with any model it lists
- [x] Select model and supported reasoning effort for local Claude Code and Codex tasks
- [x] Tokens spent per day/week, per app and model (Quotas page)
- [x] OpenCode Go quota (5 h, week, month) with an optional key
- [x] `baton delegate`: subtasks for OpenCode models, read-only by default
- [ ] Cancel a running task from its page
- [x] Show the agent's completion summary on local and cloud task pages
- [ ] Check a live task summary from the web

## Blockers
- None.

## Decisions
- 2026-11-23: the page reads STATUS.md from GitHub (not local folders), so it
  works the same for every agent and every computer.
- 2026-11-23: Claude and Codex quota readings come from local sources. OpenCode Go
  is optional and uses its configured key to query the official usage endpoint.

## Log
- 2026-11-23 (codex): added persistent task summaries covering the outcome, approach, reasoning, checks and pending work. All 26 tests passed with simulated local and cloud reports.
- 2026-11-23 (codex): verified private Tailscale Serve routing to native Baton and documented phone access. Replaced inline placeholder icons in quota and token cards with the bundled Anthropic, OpenAI and OpenCode images. Phone-side confirmation remains with the owner.
- 2026-11-23 (codex): verified 15 Claude Code and 10 Codex models, per-model effort selection, and 25 tests. Refreshed the task form. Checked quota backgrounds in both themes and local Anthropic, OpenAI and OpenCode logos on Windows and Docker.
- 2026-11-23 (codex): verified the live Go quota card and all three API windows on native Windows (8787) and Docker Compose (8788). Docker health check passed; 23 tests passed.
- 2026-11-23 (claude): OpenCode Go quota; baton delegate; OpenCode rules rewritten deny-first after tests; tokens section fixed.
- 2026-11-23 (claude): OpenCode agent (own permission rules, exe without shell); tokens spent per app/model.
- 2026-11-23 (claude): pages split (Projects/Tasks/Quotas); local tasks in worktrees; manual quota form removed.
- 2026-11-23 (claude): one quota card per provider; next task on project cards; New task form with CSRF protection; Claude task workflow template.
- 2026-11-23 (claude): runs at Windows logon (scheduled task, pythonw); log file when there is no console.
- 2026-11-23 (claude): automatic quota checks (claude -p stream-json unifiedWindows; codex exec).
- 2026-11-23 (claude): tested on Windows (native + Docker); fixed .env files with a BOM; Codex CLI oriented itself correctly from the hub's AGENTS.md + INDEX.md.
- 2026-11-23 (claude): v0.1.0 created.
