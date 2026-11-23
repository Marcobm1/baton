---
name: Baton (hub + quotas)
status: active
priority: high
summary: Open-source hub that shows every STATUS.md in my GitHub repos, plus Claude/Codex quota windows.
next: Marco creates a read-only fine-grained token for Docker / the daily INDEX.md workflow, and merges the STATUS.md PRs in Cairn and Mamporro.
updated: 2026-11-23
updated_by: claude
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
- [ ] Better Claude reading when using the VS Code extension (status line doesn't run there)
- [ ] Screenshots in the README

## Blockers
- None.

## Decisions
- 2026-11-23: the page reads STATUS.md from GitHub (not local folders), so it
  works the same for every agent and every computer.
- 2026-11-23: quota data only from local files written by the tools or typed
  by hand; Baton never touches provider credentials.

## Log
- 2026-11-23 (claude): tested on Windows (native + Docker); fixed .env files with a BOM; Codex CLI oriented itself correctly from the hub's AGENTS.md + INDEX.md.
- 2026-11-23 (claude): v0.1.0 created.
