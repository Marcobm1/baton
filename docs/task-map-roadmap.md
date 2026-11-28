# Task map roadmap (TM-1 – TM-4)

Follow-up phases to optimize and evolve the interactive per-project task map
(`?tab=map`, shipped in PR #92; see [project-detail.md](project-detail.md)).
Each phase is self-contained and preserves `STATUS.md` as the single source of
truth with zero external build steps.

---

## TM-1: Layout clarity & scaling for larger graphs — shipped (2026-11-27)

**Problem:** Once a project grows past ~15–20 tasks, raw file-order placement
introduces crossing arrows, redundant transitive links (`A → C` when `A → B → C`
already exists), and lanes cluttered with long lists of finished tasks.

**Scope:**
- **Barycenter crossing reduction (auto-order):** compute card order inside
  each depth column by averaging connected neighbours' vertical positions
  (2–3 sweeps, $O(V + E)$), with a toggle to switch back to **Manual**
  drag-and-drop file order.
- **Transitive edge hiding:** detect edges implied by a longer path (e.g.,
  `C` depends on `A` and `B`, and `B` depends on `A`) and keep them in
  `STATUS.md`, drawing them only as subtle dashed lines when `A` or `C` is
  hovered or selected.
- **Completed-task folding:** when a lane has 3+ finished tasks, fold them into
  a compact expandable stub (e.g., `8 done`), omitting incoming arrows from
  folded completed dependencies.
- **Viewport controls:** fit-to-screen zoom (`0`), horizontal vs vertical
  orientation (`L`), and column-spacing adjustment (`[` / `]`).

**Trade-offs:** Exact crossing minimization is NP-hard; a 2–3 sweep barycenter
heuristic runs in sub-millisecond time for 200 tasks and keeps layout
predictable without pulling in a JS graph library.

---

## TM-2: Performance, commit coalescing, 3-way conflict merge & live SSE — shipped (2026-11-28)

**Problem:** Immediate one-commit-per-edit writes are simple, but editing a
title and adding three subtasks produces 4 rapid GitHub commits (~1–2 s each).
Meanwhile, an agent completing a task or another browser tab updating
`STATUS.md` is only visible after clicking Reload (`↻`).

**Scope:**
- **Short trailing debounce / batching (1.5 s window):** coalesce
  rapid back-to-back edits (e.g., adding several subtasks or editing title +
  description) into a single clean GitHub commit, flushing immediately on
  `beforeunload` / tab switch and keeping an explicit **Save now** affordance
  while pending.
- **Semantic 3-way merge on `409 Conflict`:** in `baton/task_map.py`, compare
  `(base, local, remote)` task maps by task/group ID. When an agent ticked
  `AP-01` while you edited `UI-03`, merge both cleanly and retry once instead
  of discarding your edit; only fall back to reload when both sides modified
  the same task.
- **Live SSE stream (`/api/projects/{pid}/map/stream`):** push running/queued
  task transitions from `TaskStore` and `STATUS.md` SHA updates so cards light
  up and settle in real time without polling or full page reloads.

**Trade-offs:** Debouncing introduces a brief (<2 s) window where a crash could
lose an un-flushed edit unless flushed via `navigator.sendBeacon` / `keepalive`;
semantic 3-way merge requires keeping the base snapshot alongside each queued
save.

---

## TM-3: Canvas ergonomics, Kanban board toggle & mobile workflow — shipped (2026-11-28)

**Problem:** Power users benefit from faster keyboard/context-menu flows, deep
links to specific task IDs, and a state-oriented view—especially on phones
where a 2D dependency graph is cramped.

**Scope:**
- **Right-click context menus:**
  - Card: Launch task, Mark done / Reopen, Add successor / predecessor, Copy ID,
    Delete.
  - Arrow: Jump to source/target card, Remove dependency.
  - Empty lane space: New task here, Rename / Delete group.
- **Keyboard navigation & URL deep-links:** arrow keys to move selection
  between cards, `Enter` to focus the side panel, `#AP-02` URL hash to open and
  focus a task directly, and a `?` keyboard-shortcuts modal.
- **Board (Kanban) view toggle:** switch between **Graph** (swimlanes by group
  × depth) and **Board** (columns by computed state: Blocked, Ready, Your turn,
  Running, Done). On phones (`< 640 px`), Board becomes the default single-column
  state view instead of redirecting away to the flat list.
- **Editable `STATUS.md` drawer:** allow editing the `## Next` Markdown directly
  in the side drawer and clicking **Apply** (validated server-side via
  `POST /api/projects/{pid}/map`).

---

## TM-4: Dependency-aware runner & safe chain execution

Shipped 2026-11-28. Right-click a target and choose **Queue up to here**.
The preview shows each unchecked ancestor once, prerequisites first, with its
destination and agent. Already scheduled items keep their existing run. The first unchecked owner action ends the preview.
Confirming saves the entire prefix in one SQLite transaction. A changed
checklist requires a new preview. Finish the owner action and preview again
to launch the remaining steps.

Map launches use the local runner. Each run stores its project and checklist ID,
including when a `Repo:` line sends the work elsewhere. Before dispatch Baton
reads the source project's default-branch checklist. Unchecked dependencies,
an unreadable map or an unfinished predecessor hold the run in the queue.
Baton retries after a run finishes and during its minute maintenance loop.
A manually merged predecessor PR can release the chain once its checkbox is
checked. After a merge, an unchecked item offers a completion
action on its map card and task page.

A failed or cancelled run cancels its queued descendants. Their task pages and
configured task notifications name the upstream step. Independent branches can
continue. Fix the cause and preview again to start a new chain.
