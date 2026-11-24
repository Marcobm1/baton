---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex and OpenCode Go quota windows.
next: Watch the first OpenCode task go through review and auto-merge
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
- [x] 2026-11-24 Prepare the next editable task from the project page
- [ ] Watch the first OpenCode task go through review and auto-merge
  Follow a real OpenCode task through review and CI. Check that Baton records
  the verdict on the task page and merges the reviewed commit after tests pass.
- [x] 2026-11-24 Organize project details and launch drafts from the Next checklist
- [x] 2026-11-24 Check the project detail on a phone
- [x] Automatic idle-only self-update with validation, rollback and status on Quotas
- [x] 2026-11-24 Confirm the first live self-update and scheduled restart on Windows
- [x] OpenCode delegation ranking with separate agent and human reviews
- [x] 2026-11-24 Visual redesign: sidebar / phone tab bar, Auto-Light-Dark theme, WCAG AA colour tokens
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
- [x] 2026-11-24 Faster quota updates: 5-10 min automatic checks that skip when a newer reading exists, OpenCode Go refreshed in the background
- [x] `baton delegate`: subtasks for OpenCode models, read-only by default
- [x] State-specific task details and Claude model access, quota and other error guidance
- [ ] Review a historical task and a running task in the web UI
- [x] Cancel a running task from its page
- [ ] Try cancelling a real agent task on Windows (check Task Manager afterwards)
- [x] Show the agent's completion summary on local and cloud task pages (PRs #3 + #4 combined)
- [ ] Check a live task summary from the web
- [x] Every local task (Claude Code, Codex, OpenCode, local AI) ends in a pull request; no opt-out checkbox
- [x] Auto-merge: Claude/Codex PRs merge when tests pass; OpenCode/local AI work and repos without tests get a review (with fixes) by Claude Code or Codex first
- [x] First real auto-merges without review: Codex (#10, #12) and Claude Code (#11)
- [x] Local task queue with priority (high/normal/low), one task per repository, `BATON_MAX_PARALLEL` global limit, change priority or cancel while queued
- [ ] Queue two real tasks on one repository and check the second starts from the merged result
- [x] Test: "en" and "es" UI strings have the same keys and no empty text
- [x] 2026-11-24 Project dependencies (`depends_on`) and subprojects (`parent`): blocked cards turn red until the dependency is done
- [ ] Check a blocked card on the live page (hub: Cuantización depends on IA local)
- [ ] Monitoring section for this PC and, later, other computers and servers
  One card per machine: CPU, RAM, disk, GPU load, VRAM, temperatures and power draw, uptime, online/offline.
  Alerts for high temperatures, RAM or disk almost full, and unusual use (unknown processes using the GPU or the network, logins at odd hours).
  Start with this Windows PC; design the data source so a small agent on each extra machine can report to Baton later.
  Prerequisite for a "Models" tab: start/stop local models (llama-server router mode), VRAM budget, tokens/s and tokens served (see the hub project "Cuantización").

## Blockers
- None.

## Decisions
- 2026-11-24: "blocked" is computed from `depends_on` each time projects load instead of being a status value, so it clears itself when the dependency is marked done; unknown names keep the project blocked so typos are visible (docs/dependencies.md).
- 2026-11-24: a scheduled quota check is skipped when a stored reading is fresher than the interval (the CLI just reported usage through its status line or logs), so 5-10 minute intervals cost little extra quota; OpenCode Go is asked at most every 5 minutes while the server runs, not only when the page is open (README "Quota panel").
- 2026-11-24: one design system with colour tokens for light and dark; theme Auto/Light/Dark stored per browser and applied before the first paint; phone tab bar, desktop sidebar (docs/design.md).
- 2026-11-24: project detail combines repository activity with each project's own Next checklist. Undated completions keep their source order after dated history (docs/project-detail.md).
- 2026-11-24: the updater reserves the idle queue until validation and restart finish; Windows relaunches the scheduled task after the server PID exits (docs/self-update.md).
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
- 2026-11-24: local tasks wait in a priority queue kept in tasks.json (one per
  repository, at most BATON_MAX_PARALLEL=3 at once); a task fetches and makes
  its worktree only when it starts; restarts keep the queue (docs/task-queue.md).

## Log
- 2026-11-24 (codex): Project pages open an editable next-task draft and include Done recently in history. Local and cloud prompts request the next title and description. Project detail tests passed. The Windows suite still reports two cancellation cleanup failures.
- 2026-11-24 (claude): `depends_on` and `parent` in STATUS.md; red blocked cards and detail note, blocked note in INDEX.md, en/es strings, README section and docs/dependencies.md; monitoring section added to Next; 90 tests passed.
- 2026-11-24 (opencode): quota update review: scheduled checks now skip when a fresher reading exists (5-10 minute intervals barely spend extra quota), the OpenCode Go card refreshes every 5 minutes in the background, README/.env.example recommend 5-10 minutes; 87 tests passed plus an end-to-end smoke run of the loop.
- 2026-11-24 (claude-code): visual redesign (control-panel style), sidebar on desktop and tab bar on phones, Auto/Light/Dark theme without flash, colour tokens with WCAG AA tests; checked with screenshots in both themes at 1440 px and 390 px.
- 2026-11-24 (opencode): confirmed the first live self-update on Windows: c34eb76→16a9f3a validated 05:20Z and running 05:21Z; two more updates (→551150c, →82b9604) pulled, pytest-validated and restarted themselves through the Baton scheduled task; server on 82b9604, up to date, clean checkout.
- 2026-11-24 (codex): the owner confirmed the project detail and prefilled task draft worked on mobile. The Windows self-update check is next.
- 2026-11-24 (codex): added structured project details and editable checklist drafts. All six detail tests passed, including cached GitHub failure and token-free views. Full suite: 77 passed, two known Windows cancellation cleanup failures. Phone and live PR check pending.
- 2026-11-24 (claude-code): installed #10-#12 on Windows by hand (73 tests passed there); this commit is the first one the self-updater should pull and restart on by itself.
- 2026-11-24 (codex): added native self-update, a CSRF-protected Quotas control and update history. All 11 updater tests passed; two Windows cancellation failures also reproduced on the unchanged base. Live scheduler handoff pending.
- 2026-11-24 (claude-code): task queue with priority instead of refusing a second task per repository; global limit BATON_MAX_PARALLEL; change priority / cancel while queued; queue survives restarts; en/es texts; docs/task-queue.md; tasks.json reads now take the store lock and the replace retries (Windows "Access denied" race, seen once in 3 runs); 8 new tests, 62 passing 5 runs in a row.
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
