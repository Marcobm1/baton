# Project detail

The project page reads the project's own STATUS.md and overlays activity for
its GitHub repository. Projects in different folders of a hub share the
repository queue and PRs. Their checklists and progress stay independent.
There is no additional task or project database.

## Writing Next items

Use one top-level checkbox per task. When finishing work on an ongoing project,
mark the completed item and put the next task first among unchecked agent items.
Set the front matter `next` to its title and indent its description underneath
the checkbox, including how to verify completion. A provisional draft is enough.

```markdown
## Next
- [ ] Export the filtered report
  Include the visible columns in their current order.
  Use the sample report to check the header and row count.
- [x] 2026-11-23 Add report filters
```

The project progress bar counts only these checkboxes. **Create next task**
opens an editable draft for the first unchecked agent item. Older files can supply
just the front matter `next`. A title without instructions gets a provisional
description. Hub drafts include the project's STATUS.md path so the agent
knows which folder to update. Submitting the form starts the usual task flow.
Keep drafts within the task form limits (200 characters for the title and
5,000 for the details).

## Owner actions

An unchecked title starting with `(Owner)` (configurable via `BATON_OWNER_TAG`)
belongs in **Your turn**. It counts
toward progress. Only an agent task can become a launch draft. **Done** posts
the selected Markdown line and the Git blob ID of the displayed STATUS.md
with the form's CSRF token. Baton verifies that line is still an open owner
action and sends the file to GitHub with the expected blob ID. A stale form
cannot overwrite a newer version, even for repeated titles or a hub subproject.

## Reading the history

Done combines checked items, entries under `## Done recently` and terminal
Baton tasks, including failures and cancellations labelled with their outcome. A cloud task with a recorded
agent summary also appears here. Each source keeps its own entry because a
task title is not a reliable identifier. Prefix a checked title with an ISO
completion date to place it chronologically. Entries with no recorded date
follow dated entries in source order. Ten entries are initially visible.

**Continue** on a finished task opens New task by record ID. The editable
draft includes the original instructions, report, PR when recorded and room
for a reply. Baton reuses the original execution settings when available.

Write the newest Log entries first, as in the agent guide. The page shows the
first five and folds the remainder. The complete original STATUS.md, including
its front matter and any custom sections, stays at the bottom.

## GitHub reads

Opening a detail page requests the repository's open PRs and the combined check
state of each head commit in a paginated GraphQL query. Concurrent visitors
share the result in the Hub for the configured `BATON_CACHE_SECONDS`. This
optional query needs GitHub read access to pull requests and their checks.
Without a token or when the request fails, the page omits the PR list.

## Layout (2026-11-24)

The project page was getting long, so it is split: a compact header (name,
summary, links, progress and a blockers warning), "Your turn" and "Right now"
when they have something, then three tabs: **Next** (the next task with its
description and the next five, the rest folded), **Done** (10 per page) and
**Notes** (blockers, decisions, log and the full STATUS.md). Descriptions are
rendered as Markdown; relative links point to the file on GitHub.

## Structured reports and image attachments (2026-11-26)

When finishing a local task, agents are prompted to structure their final report
under `Summary:`, `Changes:`, `Checks:`, and `Next:` (legacy free-form Markdown
reports still render cleanly as a fallback).

For UI, visual, or game-dev tasks, agents can optionally drop up to 8 raster
screenshots (`PNG`, `JPEG`, `WebP`, or `GIF`, max 5 MiB each) into
`.baton-artifacts/` inside the worktree and list optional captions under
`Artifacts: filename.png | short caption`. Before `git status` or `git add`
runs, Baton validates each file by extension and magic bytes (refusing SVG,
HTML, symlinks, and hardlinks), copies valid images into
`<data_dir>/artifacts/<task_id>/`, and deletes `.baton-artifacts/` from the
worktree so screenshots never bloat Git history. Saved images are served only
through the authenticated `/api/tasks/{id}/artifacts/{filename}` endpoint with
`X-Content-Type-Options: nosniff` and a strict sandboxed CSP, rendered in a
lightbox gallery on `/tasks/{id}` and `/p/{id}?tab=done`, and the first image
is attached to Telegram task-completion notifications when Telegram is enabled.

## Interactive task map (`?tab=map`, 2026-11-27)

Every project page includes a **Map** tab alongside **Next**, **Done**, and
**Notes**. Instead of maintaining a second database or JSON file that could
drift out of sync with `STATUS.md`, the graph lives directly inside the
`## Next` section of `STATUS.md`:

```markdown
## Next

### API (AP)
- [x] [AP-01] 2026-11-20 Define the REST endpoints
  - [x] Draft the OpenAPI spec
- [ ] [AP-02] Implement the handlers (after: AP-01)
  Handlers for /tasks and /groups, with validation.
  Recommended: claude-code, opus, medium
  - [x] GET /tasks
  - [ ] POST /tasks

### Release (RL)
- [ ] [RL-01] (Owner) Approve and merge (after: AP-02)
```

Key design choices and trade-offs:

- **Explicit opt-in for existing projects:** a legacy `STATUS.md` without
  `### Group (PFX)` subheadings or `[ID]` prefixes opens in **read-only preview
  mode** with provisional `GN-01`… IDs. Clicking **Enable task map**
  (`POST /api/projects/{pid}/map/enable`) writes a single commit that adds
  `### General (GN)` and `[ID]` prefixes while keeping every existing task,
  description, date, owner tag, and section outside `## Next` intact.
- **Automatic dependency-depth layout & scaling (TM-1):** columns/levels are
  computed from each task's dependency depth (inspired by Kanvas), with 3-sweep
  barycenter crossing reduction (**Auto** vs **Manual** order, `M`),
  non-overlapping lane packing (**Compact**), **Left → Right** / **Top → Bottom**
  orientation (`L`), adjustable level spacing (`[` / `]`), and fit-to-screen
  zoom (`0`). Redundant transitive arrows (`A → C` when `A → B → C` exists) are
  computed via `task_map.transitive_edges()` and hidden by default—appearing
  dashed on hover/selection—while lanes with 3+ completed tasks fold into a
  single expandable `"N done"` pill so active work stays front and centre. All
  view preferences persist in `localStorage` (`baton-tm-prefs`).
- **Cycle-free DAG enforcement:** drawing an arrow in the browser checks for
  cycles immediately, and `baton/task_map.py` enforces strict three-colour DFS
  cycle rejection on every write (`400 Bad Request`). Hand-edited `STATUS.md`
  files with a cycle degrade safely on read and surface a format warning.
- **Immediate commits with optimistic blob-SHA locking:** each map edit posts
  to `POST /api/projects/{pid}/map` with CSRF protection (`X-Baton-CSRF`) and
  the expected Git blob SHA. On success, the cached `Project` is updated in
  place so rapid consecutive edits stay responsive; on a `409 Conflict` (e.g.
  an agent updated `STATUS.md` concurrently), Baton reloads from GitHub and
  returns the latest map so no work is overwritten.
- **Top-level progress only:** `Status.tasks_done` / `Status.tasks_total` count
  only column-0 checkboxes so indented subtasks (`  - [ ] …`) do not inflate
  the project progress bar.
- **Live queue and launch bridge:** active (`running` / `queued`) Baton tasks
  whose title contains `[ID]` (or matches the task title) highlight their card
  live on the map with a direct link to `/tasks/{id}`. Clicking **Launch
  task…** on a ready card opens `/tasks/new` prefilled with `[ID] <title>`,
  description, subtasks, and `Recommended:` agent/model/effort.
- **Small screens:** below `640 px`, the Map tab shows a notice pointing to the
  **Next** list view, with an opt-in button to inspect the canvas anyway.

Planned follow-up phases (TM-2 commit coalescing/3-way merge/SSE, TM-3 context
menus/Kanban view, and TM-4 dependency-aware chain execution) are tracked in
[docs/task-map-roadmap.md](task-map-roadmap.md).

