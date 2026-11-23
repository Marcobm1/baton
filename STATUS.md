---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex and OpenCode Go quota windows.
next: Review a real OpenCode delegation from the Ranking page.
updated: 2026-11-24
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
- [x] OpenCode delegation ranking with separate agent and human reviews
- [ ] Rate a real delegation from the CLI and web
- [ ] Future phase for native ChatGPT and Claude model rankings
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
- [x] State-specific task details and Claude model access, quota and other error guidance
- [ ] Review a historical task and a running task in the web UI
- [x] Cancel a running task from its page
- [ ] Try cancelling a real agent task on Windows (check Task Manager afterwards)
- [x] Show the agent's completion summary on local and cloud task pages (PRs #3 + #4 combined)
- [ ] Check a live task summary from the web
- [x] Every local task (Claude Code, Codex, OpenCode, local AI) ends in a pull request; no opt-out checkbox
- [x] Auto-merge: Claude/Codex PRs merge when tests pass; OpenCode/local AI work and repos without tests get a review (with fixes) by Claude Code or Codex first
- [ ] Watch the first real auto-merged tasks (one Codex, one OpenCode)
- [x] Test: "en" and "es" UI strings have the same keys and no empty text

## Blockers
- None.

## Decisions
- 2026-11-23: the page reads STATUS.md from GitHub (not local folders), so it
  works the same for every agent and every computer.
- 2026-11-23: every local task ends in a pull request (owner's request), so
  finished work is always on GitHub and visible from the phone.
- 2026-11-23: local tasks merge themselves; OpenCode/local AI work and repos
  without tests are reviewed by Claude Code or Codex first (docs/auto-merge.md).
- 2026-11-23: Claude and Codex quota readings come from local sources. OpenCode Go
  is optional and uses its configured key to query the official usage endpoint.
- 2026-11-23: Claude units: the status line sends used_percentage as 0-100 and the
  automatic check sends utilization as a 0-1 fraction; claude-usage.json stores percent.
- 2026-11-23: cancelling kills the agent's whole process tree, commits/pushes
  nothing, and deletes the worktree + local branch only if the branch is not
  on GitHub (see docs/cancelling-tasks.md).

## Log
- 2026-11-24 (codex): added OpenCode Ranking and confirmed both reviews survived a process restart. 52 tests passed; two Windows cancellation tests also failed on the unchanged base.
- 2026-11-23 (claude-code): auto-merge of local tasks: wait for GitHub tests, review and fix by Claude Code or Codex when needed (OpenCode, local AI, repos without tests), merge the checked commit; BATON_AUTO_MERGE; docs/auto-merge.md; 7 tests.
- 2026-11-23 (opencode): test that every language has exactly the "en" keys and no
  empty UI text; "es"."ago" is empty on purpose (Spanish puts "hace" before the
  time), so the test allows that one case explicitly.
- 2026-11-23 (claude-code): Claude quota: a used-up window ("rejected" in the automatic check) now reads 100 % used instead of keeping an old reading; quota bars turn red when exhausted and orange when fast. PR #5 closed (it read status line values of 1 % or less as fractions).
- 2026-11-23 (claude-code): removed the "open a pull request" checkbox: every local task now pushes its branch and opens a PR; test for the three agents.
- 2026-11-23 (claude-code): task summary panel (combined PRs #3 and #4): the agent ends with a marked report (what, how, why, checks, problems, what you need to know); cloud tasks read it from the issue.
- 2026-11-23 (claude-code): Cancel task button (kills the process tree, no commit/push, removes worktree and branch); status cancelled/cancelada; tests.
- 2026-11-23 (codex): added collapsible task details using existing records and CLI logs. Tested legacy reports and simulated Claude access, quota and unrelated failures.
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
