---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude, Codex and OpenCode Go quota windows.
next: Agents chat for OpenCode, Claude Code and Codex in Baton, with tools
updated: 2026-11-24
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
- [ ] Agents chat for OpenCode, Claude Code and Codex in Baton, with tools
  Priority for Marco (asked 2026-11-24). Turn the Chat page into an "Agents" chat: one page with three selectors, **agent** (Local model, OpenCode, Claude Code, Codex), **model** and **reasoning effort**, whose options change with the agent (reuse `task_models.catalog` for Claude Code/Codex and the OpenCode model list; the local models from `models.json`).
  - **Phase 1, OpenCode** (also covers the local llama-server models, already set up as OpenCode providers `local-inkling` / `local-qwen`): Baton starts `opencode serve` (or uses ACP, `opencode acp`; pick one and record why in docs/) and keeps one OpenCode session per conversation. Stream the answer and show every tool call (tool, arguments, result) in the chat.
  - **Phase 2, Claude Code and Codex**: the same chat through their CLIs: `codex exec --json` + `codex exec resume <id>`; Claude Code non-interactive with streaming JSON output and session resume. Check whether ACP adapters for both exist on this PC; if they do, consider ACP for all three.
  - **Working folder per conversation**: choose one of Baton's local repos or `C:\IA\pruebas-opencode`; never the whole disk.
  - **Two permission modes, chosen per conversation:**
    1. **Confirm** (default): anything that writes, edits or runs a command shows **Allow / Deny** in the chat and waits (OpenCode permission requests; Codex `--sandbox workspace-write` with approvals; Claude Code's default permission mode). Read-only tools run without asking.
    2. **Full permissions**: everything is pre-approved (OpenCode auto-approve, Codex `--dangerously-bypass-approvals-and-sandbox` or `danger-full-access`, Claude Code `bypassPermissions`). Show a clear red warning when it's chosen and a red badge on the conversation while it's on. Never allowed with abliterated local models. Log every tool call the same way.
  - Keep Baton's rules: never read or store provider credentials (the CLIs handle login), never pass chat text through a shell, CSRF on every request, the agent's terminal only inside the chosen folder.
  - Quotas: show which plan the conversation spends (Claude, ChatGPT/Codex, OpenCode Go, local = free).
  - Done when, from the phone through Tailscale, Marco can hold a multi-turn conversation with each of the four agents, see tool calls, approve one in Confirm mode, and run one in Full mode; tests cover both modes with fake agents; docs/agents-chat.md explains the design and the risks.
  Recommended: claude-code, opus, high
- [x] 2026-11-24 Check a model survives a Baton restart
  With a model started from Baton's Models page, restart Baton (scheduled task
  or a self-update) and check the model is still running, still shown as
  started by Baton (not "outside Baton"), and Stop still works. Record the
  result in the Log.
  Recommended: claude-code, opus, medium
- [x] 2026-11-24 Chat page for local models (`/chat`), relayed and streamed by Baton
- [x] 2026-11-24 Fix: the Start button on model cards didn't react (covered by the card's click layer)
- [x] 2026-11-24 Start and stop a model from the live Models page (start, stop, VRAM refusal checked on Marco's PC)
- [x] 2026-11-24 Models page: this computer, VRAM budget and local models with Start/Stop
  `/models`: CPU, RAM, disk, GPU load, VRAM, temperature, power; one card per
  model in `~/.baton/models.json` with live speed and tokens served (docs/models.md).
- [x] 2026-11-24 Separate owner actions and continue finished tasks
- [x] 2026-11-24 Show the reviewer's model and effort on the task page
  Saved on the task when the review starts and shown next to "Review by"
  (for example "opus · medium"); with nothing configured the heading stays
  plain. Suite: 111 passed on Windows.
- [x] 2026-11-24 Saving a personal rating returns to the Ranking list with a confirmation toast
  The form keeps the origin filter and chart view; the message fades out and
  never repeats after a page reload. Suite: 111 passed on Windows.
- [x] 2026-11-24 Add historical delivery scores, three ranking charts and per-job rating actions
- [x] 2026-11-24 Prepare the next editable task from the project page
- [x] 2026-11-24 Recommended agent/model/effort for drafted tasks, last choice remembered, Spanish STATUS headings, project cards and page redesign
- [x] 2026-11-24 Check on the live page that "Launch next task" preselects the recommended model
  Open a project whose next item has a `Recommended:` line, press Launch next task
  and check the agent, model and effort; then open New task from Tasks and check
  it starts with the last task's choice.
  Recommended: codex, gpt-6.2-astra, low
- [x] 2026-11-24 Include OpenCode and local-AI tasks and their reviews in Ranking
- [x] 2026-11-24 Confirm the first OpenCode review and auto-merge
- [x] 2026-11-24 Verify live OpenCode reviewer scores and charts
  Find a finished OpenCode delivery in tasks.json or GitHub and compare the
  recorded review and merge outcome with Ranking. Check chart calculations
  against existing ratings and document any missing sample.
  Recommended: codex, gpt-6.2-sol, low
- [x] (Marco) Rate a real OpenCode delivery in Ranking
- [x] 2026-11-24 Organize project details and launch drafts from the Next checklist
- [x] 2026-11-24 Check the project detail on a phone
- [x] Automatic idle-only self-update with validation, rollback and status on Quotas
- [x] 2026-11-24 Confirm the first live self-update and scheduled restart on Windows
- [x] OpenCode delegation ranking with separate agent and human reviews
- [x] 2026-11-24 Visual redesign: sidebar / phone tab bar, Auto-Light-Dark theme, WCAG AA colour tokens
- [x] (Marco) Rate a real delegation from the CLI and web
- [x] 2026-11-24 Future phase for native ChatGPT and Claude model rankings
- [x] STATUS.md format and parser
- [x] Web page with projects and quota panel
- [x] Docker + README instructions
- [x] Tested on Marco's Windows PC: native (gh login) and Docker (public repos without token)
- [x] (Marco) Use Baton for a few days and give feedback
- [x] (Marco) Configure the `BATON_TOKEN` secret for the daily hub index workflow
- [x] 2026-11-24 Prepare the daily INDEX.md workflow in the hub repository
- [x] Automatic Claude/Codex quota checks via the official CLIs (`baton ping`, scheduler)
- [x] 2026-11-24 Screenshots in the README
- [x] "New task" form: GitHub issue for @claude (Claude Code GitHub Action)
- [x] Separate pages: Projects, Tasks, Quotas
- [x] Local tasks on this computer (Claude Code, Codex, local AI) in git worktrees, ending in a PR
- [x] Remote access through Tailscale Serve
- [x] 2026-11-24 Check how Codex cloud tasks can be triggered
  Recorded the [documented triggers and smoke test](docs/codex-cloud-tasks.md).
- [x] (Marco) Validate a Codex cloud implementation trigger
  Use a connected test repository and the intended Baton GitHub identity for
  the [smoke test](docs/codex-cloud-tasks.md#smoke-test). Record the task and
  delivery links, or the observed failure, before adding a form option.
- [x] OpenCode as a local agent, with any model it lists
- [x] Select model and supported reasoning effort for local Claude Code and Codex tasks
- [x] Tokens spent per day/week, per app and model (Quotas page)
- [x] OpenCode Go quota (5 h, week, month) with an optional key
- [x] 2026-11-24 Faster quota updates: 5-10 min automatic checks that skip when a newer reading exists, OpenCode Go refreshed in the background
- [x] `baton delegate`: subtasks for OpenCode models, read-only by default
- [x] State-specific task details and Claude model access, quota and other error guidance
- [x] 2026-11-24 Review a historical task and a running task in the web UI
  Open the completed task for [PR #29](https://github.com/Marcobm1/baton/pull/29)
  and a running task in Baton's Tasks page. Check the report and PR link on the
  completed task and the active step and cancel control on the running task.
  If no task is running, use the [task report fixture](tests/test_task_report.py)
  for the active step and the [web cancellation test](tests/test_runner.py)
  for the control.
  Recommended: codex, gpt-6.2-sol, low
- [x] Cancel a running task from its page
- [ ] Diagnose Windows cancellation cleanup failures
  Run the two cancellation tests in [test_runner.py](tests/test_runner.py) on
  Windows with their temporary repositories. Check whether the agent and its
  child exit and whether Git removes the worktree and branch. Record the
  failing command and exit code, or document a clean reproduction if both pass.
  Recommended: codex, gpt-6.2-sol, medium
- [x] (Marco) Try cancelling a real agent task on Windows (check Task Manager afterwards)
  Use a disposable task. Cancel it from its Tasks detail page, then confirm
  the page says Cancelled and the agent and its children exit in Task Manager.
  See [cancellation behavior](docs/cancelling-tasks.md).
- [x] Show the agent's completion summary on local and cloud task pages (PRs #3 + #4 combined)
- [x] 2026-11-24 Check a live task summary from the web
  The [PR #29](https://github.com/Marcobm1/baton/pull/29) task shows a saved
  report with Summary and Checks.
- [x] Every local task (Claude Code, Codex, OpenCode, local AI) ends in a pull request; no opt-out checkbox
- [x] Auto-merge: Claude/Codex PRs merge when tests pass; OpenCode/local AI work and repos without tests get a review (with fixes) by Claude Code or Codex first
- [x] First real auto-merges without review: Codex (#10, #12) and Claude Code (#11)
- [x] Local task queue with priority (high/normal/low), one task per repository, `BATON_MAX_PARALLEL` global limit, change priority or cancel while queued
- [ ] Queue two real tasks on one repository and check the second starts from the merged result
- [x] Test: "en" and "es" UI strings have the same keys and no empty text
- [x] 2026-11-24 Project dependencies (`depends_on`) and subprojects (`parent`): blocked cards turn red until the dependency is done
- [ ] Check a blocked card on the live page (hub: Cuantización depends on IA local)
- [ ] Monitoring for other computers and servers, and alerts
  The Models page already has this PC's card (CPU, RAM, disk, GPU, VRAM,
  temperature, power, uptime, amber/red levels). Still to do: online/offline
  for extra machines through a small agent that reports to Baton, and alerts
  (notifications) for high temperatures, RAM or disk almost full and unusual
  use (unknown processes using the GPU or the network, logins at odd hours).
  Recommended: claude-code, opus, high

## Blockers
- Adding a Codex cloud form option depends on the live test above.
- Verifying a new personal rating in Ranking depends on the owner action above.

## Decisions
- 2026-11-24: the chat goes through Baton (`/api/chat/<id>`, streamed) instead of opening the model's port: works from the phone, the model stays on 127.0.0.1, and only text plus reasoning effort is forwarded (docs/models.md).
- 2026-11-24: local models run as one detached `llama-server` per model from `~/.baton/models.json`, not router mode: each keeps its tuned options, a 16 GB card fits one large model anyway, and models survive Baton restarts (docs/models.md).
- 2026-11-24: a Codex cloud form option needs a live implementation task and a
  retrievable result from the intended trigger ([test plan](docs/codex-cloud-tasks.md)).
- 2026-11-24: a Next item can run in another repository with a `Repo:` line; the origin project keeps the checklist item, ticked from the finished task's page.
- 2026-11-24: an agent task can be ticked from the web when it was already done or is no longer needed; Baton commits STATUS.md like an agent would (date prefix, `next` moved, YAML-safe), refusing stale pages.
- 2026-11-24: owner checkboxes use a version-checked GitHub Contents commit. Follow-ups read the finished task by ID (docs/project-detail.md).
- 2026-11-24: the reviewer's model and effort come from settings, checked against the CLI's own list; an unknown value falls back to the CLI default so a typo can't block every merge (docs/auto-merge.md).
- 2026-11-24: every local script and stylesheet URL ends in `?v=` + a hash of all static CSS/JS, so a self-update never leaves a browser with an old file.
- 2026-11-24: the New task form starts with the draft's `Recommended:` line, else the last local task's agent/model/effort (from tasks.json, so the same on PC and phone); unavailable recommendations fall back and are named (docs/task-defaults.md).
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
- 2026-11-24 (claude-code): Chat page (nav tab) relaying /v1/chat/completions with streaming, reasoning shown apart, safe Markdown, per-model history in the browser, start a model from the chat; fixed the dead Start button; checked live with Inkling 2 Mini (~57 tok/s through Baton) at desktop and 375 px.
- 2026-11-24 (claude-code): Models page (`/models`, nav tab): this PC's CPU/RAM/disk/GPU readings, VRAM budget, Start/Stop for the llama-server models in `~/.baton/models.json`, live speed and tokens served from `--metrics`; detects servers started outside Baton; new dependency psutil 7.2.2; 130 tests passed on Windows (the two cancellation tests passed too); checked at 800 px dark and 375 px light.
- 2026-11-24 (codex): Checked the saved report and merged [PR #29](https://github.com/Marcobm1/baton/pull/29) link in Tasks. This running review showed the Codex step and the cancel button with confirmation. Windows suite: 117 passed, two known cancellation cleanup failures.
- 2026-11-24 (claude): Ranking "Reviewed scores only" toggle and rating from the list; Tasks filter by repository; project page split into Next / Done / Notes tabs with a compact header, Markdown descriptions with GitHub links; 119 tests passed; screenshots checked at 1440 and 390 px.
- 2026-11-24 (claude): Ranking redesign (summary tiles, one leaderboard with agent/you/average per model and readable names, sample counts and a few-scores warning, filter by model, 8 jobs per page with All/To rate tabs, origin chips); Tasks page keeps running and queued tasks on top and pages the history (10 per page) with status filters; 117 tests passed; checked with screenshots at 1440 and 390 px in both themes.
- 2026-11-24 (claude): after Baton merges a task it drops its 5-minute project cache, so the ticked STATUS.md item shows at once (before, a just-merged task could still look pending for up to five minutes).
- 2026-11-24 (codex): Compared official Codex triggers with Baton's Claude issue flow and documented a smoke test. Live submission is pending. Windows suite: 114 passed, two known cancellation cleanup failures.
- 2026-11-24 (codex): Added Projects and Quotas screenshots with sample data to the README. Checked both rendered image paths and inspected both PNGs. Windows suite: 114 passed, two known cancellation cleanup failures.
- 2026-11-24 (claude): `Repo: <owner/repo>` in a Next item sends its launch to that repository (badge on the project page, warning if Baton doesn't list it) and the finished task offers to tick it in the origin project; agents are told to add it. Installed the daily index workflow in Marcobm1/proyectos (skips quietly until BATON_TOKEN exists; manual run passed); 116 tests passed.
- 2026-11-24 (claude): "Mark as done" for agent tasks in STATUS.md (project page, and on a finished task whose item is still unchecked, e.g. after "no changes"); ticks with today's date and moves `next`; 115 tests passed.
- 2026-11-24 (claude): verified live: OpenCode tasks #30 and #31 were reviewed by Claude Code (opus · medium) with structured scores 9/9/10 and 10/10/10, merged after CI, and Ranking shows them (9.3 and 10.0); GLM 5.3 human average 7.4 matches the five saved human scores.
- 2026-11-24 (codex): added owner actions and editable task follow-ups. All 16 project-detail and task-default tests passed with simulated GitHub writes. Full suite: 112 passed; both Windows cancellation cleanup failures reproduced on the unchanged base.
- 2026-11-24 (opencode): the task page shows the reviewer's model and effort
  (for example "opus · medium") next to "Review by Claude Code/Codex"; both are
  saved on the task when the review starts, and nothing configured keeps the
  heading plain; 111 tests passed on Windows.
- 2026-11-24 (opencode): saving a personal rating on Ranking now returns to the
  list (origin filter and chart view kept) with a one-time "valoración guardada"
  toast that fades out and disappears from the URL; 111 tests passed on Windows.
- 2026-11-24 (codex): On live Ranking, five historical `opencode-go/glm-5.3`
  estimates averaged 6.6, with no human or combined scores for that model.
  Rate matched the model and merge result for #16 and #18. GitHub confirmed
  [CI](https://github.com/Marcobm1/baton/actions/runs/45349145376) and merge
  for #18. Local suite: 108 passed, two known Windows cancellation cleanup
  failures. A fresh structured review and Marco's rating are still pending.
- 2026-11-24 (codex): [PR #16](https://github.com/Marcobm1/baton/pull/16) logged `APPROVE` and merged `17c7bf2` after [CI passed](https://github.com/Marcobm1/baton/actions/runs/45337107945). Rate showed `opencode-go/glm-5.3`, merged, and an estimated 9/10. An isolated form save survived restart and charts matched 6.6 (agent), 8.0 (human) and 8.5 (combined). Local suite: 108 passed, two known Windows cancellation cleanup failures. Live reviewer criteria and Marco's score remain pending.
- 2026-11-24 (codex): Ranking imports finished tasks across agents, labels delivery estimates and charts agent, human and paired scores. Suite: 108 passed, two known Windows cancellation cleanup failures. Live history check pending.
- 2026-11-24 (claude): reviewer model and effort configurable (BATON_REVIEW_CLAUDE_MODEL/EFFORT, BATON_REVIEW_CODEX_MODEL/EFFORT); Marco's PC reviews with Opus 5.5 at medium effort; values the CLI doesn't list fall back to its default; 107 tests passed.
- 2026-11-24 (codex): Ranking includes finished OpenCode/local tasks, structured reviewer scores, historical reports and origin filters. Suite: 104 passed; both Windows cancellation cleanup failures also reproduced on the unchanged base. Live review check remains next.
- 2026-11-24 (codex): In live Edge, Launch next task matched the Baton recommendation and Tasks > New task matched the latest local task. Both selected Codex, gpt-6.2-astra, low.
- 2026-11-24 (claude): the self-update rolled back #22 because the new test read templates with Windows' default encoding (cp1252); it now reads UTF-8. Suite checked on Windows before merging.
- 2026-11-24 (claude): phones kept the old task-form.js after the update (only style.css and theme.js had a version in their URL), so the recommended model was not preselected there; every script and stylesheet URL now carries a version that changes with any of them; test added; 97 tests passed.
- 2026-11-24 (claude): drafted tasks carry a recommended agent/model/effort that the form preselects; the form remembers the last local choice; prompts list this computer's models; Spanish section headings parsed; cards open the project and launch the next task, project page features the next task; 96 tests passed, checked with screenshots (1440 px and 390 px, light and dark).
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
