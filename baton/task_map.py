"""Parse, validate, and serialize per-project task maps inside STATUS.md.

The task map lives directly inside the ``## Next`` section of ``STATUS.md`` so
that there is still a single source of truth shared by humans and AI agents:

    ## Next

    ### API (AP)
    - [x] [AP-01] 2026-11-20 Define endpoints
      - [x] Draft OpenAPI spec
    - [ ] [AP-02] Implement handlers (after: AP-01)
      Handlers for /tasks and /groups.
      Recommended: claude-code, opus, medium
      - [ ] GET /tasks

Legacy ``STATUS.md`` files without ``### Group (PFX)`` headings or ``[ID]``
prefixes are parsed into a read-only preview (provisional ``GN-01``… IDs) until
the owner explicitly enables the map with one clean commit.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from itertools import combinations
from textwrap import dedent
from typing import Any
from urllib.parse import quote

from .project_detail import (
    DEFAULT_OWNER_TAG,
    normalize_owner_tag,
    recommendation,
    target_repo,
)

MAX_GROUPS = 30
MAX_TASKS = 200
MAX_SUBTASKS = 50
MAX_GROUP_NAME = 80
MAX_TITLE = 200
MAX_DESC = 5000
MAX_SUBTASK_TEXT = 200

_FRONT_MATTER = re.compile(r"\A\ufeff?---\s*\r?\n.*?\r?\n---\s*(?:\r?\n|\Z)", re.S)
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
_SETEXT_LINE = re.compile(r"^\s*(=+|-+)\s*$")
_GROUP_HEADING = re.compile(r"^###\s+(.+?)(?:\s+\(([A-Z][A-Z0-9]{0,3})\))?\s*$")
_TOP_CHECKBOX = re.compile(r"^[-*+]\s+\[( |x|X)\]\s+(.*)$")
_SUB_CHECKBOX = re.compile(r"^(?:\t| {2,})[-*+]\s+\[( |x|X)\]\s+(.*)$")
_GROUP_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{0,3}$")
_TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{0,3}-\d{1,4}$")
_ID_PREFIX = re.compile(r"^\[([A-Z][A-Z0-9]{0,3}-\d{1,4})\]\s*")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+")
_AFTER_SUFFIX = re.compile(r"\s*\(after:\s*([^)]*)\)\s*$", re.I)

_SECTION_ALIASES = {
    "next": "next",
    "siguiente": "next",
    "próximo": "next",
    "próximas tareas": "next",
    "done recently": "done recently",
    "hecho recientemente": "done recently",
    "blockers": "blockers",
    "bloqueos": "blockers",
    "decisions": "decisions",
    "decisiones": "decisions",
    "log": "log",
    "registro": "log",
}


@dataclass
class Group:
    id: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name}


@dataclass
class Subtask:
    text: str
    done: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "done": self.done}


@dataclass
class MapTask:
    id: str
    group: str
    title: str
    done: bool = False
    date: str | None = None
    owner: bool = False
    deps: list[str] = field(default_factory=list)
    desc: str = ""
    subs: list[Subtask] = field(default_factory=list)
    recommended: dict[str, str] = field(default_factory=dict)
    repo: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "title": self.title,
            "done": self.done,
            "date": self.date,
            "owner": self.owner,
            "deps": list(self.deps),
            "desc": self.desc,
            "subs": [s.to_dict() for s in self.subs],
            "recommended": dict(self.recommended),
            "repo": self.repo,
        }


@dataclass
class TaskMap:
    groups: list[Group] = field(default_factory=list)
    tasks: list[MapTask] = field(default_factory=list)
    enabled: bool = False
    heading: str = "## Next"
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "groups": [g.to_dict() for g in self.groups],
            "tasks": [t.to_dict() for t in self.tasks],
            "problems": list(self.problems),
        }


def _norm_heading(raw: str) -> str:
    clean = raw.strip().rstrip(":").strip().casefold()
    return _SECTION_ALIASES.get(clean, clean)


def _derive_group_id(name: str, existing: set[str]) -> str:
    letters = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    base = letters[:2] if letters and letters[0].isalpha() else "GR"
    if not _GROUP_ID_RE.match(base):
        base = "GR"
    if base not in existing:
        return base
    lead = base[0]
    for i in range(1, 100):
        cand = f"{lead}{i}"
        if cand not in existing and _GROUP_ID_RE.match(cand):
            return cand
    raise ValueError(f"Could not derive unique group prefix for {name!r}")


def _next_task_id(group_id: str, used: set[str]) -> str:
    prefix = f"{group_id}-"
    n = 0
    for tid in used:
        if tid.startswith(prefix):
            tail = tid[len(prefix):]
            if tail.isdigit():
                n = max(n, int(tail))
    while True:
        n += 1
        cand = f"{group_id}-{n:02d}"
        if cand not in used:
            return cand


def _find_next_span(source: str) -> tuple[int, int, int, str] | None:
    """Locate the ``## Next`` section in ``source`` (fence- and frontmatter-aware).

    Returns ``(heading_start_idx, body_start_idx, section_end_idx, heading_text)``
    as 0-based line indices into ``source.splitlines(keepends=True)``, or ``None``
    when the file has no ``Next`` section.
    """
    lines = source.splitlines(keepends=True)
    fm = _FRONT_MATTER.match(source)
    start_line = source[:fm.end()].count("\n") if fm else 0

    fence_char = ""
    fence_len = 0
    in_next = False
    heading_idx = -1
    body_start = -1
    heading_text = "## Next"

    i = start_line
    while i < len(lines):
        raw = lines[i].rstrip("\r\n")
        m_fence = _FENCE.match(raw)
        if m_fence:
            marker = m_fence.group(1)
            if not fence_char:
                fence_char, fence_len = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_len and raw.strip() == marker:
                fence_char, fence_len = "", 0
            i += 1
            continue
        if fence_char:
            i += 1
            continue

        # ATX heading
        m_atx = _ATX_HEADING.match(raw)
        if m_atx:
            level = len(m_atx.group(1))
            norm = _norm_heading(m_atx.group(2))
            if not in_next:
                if norm == "next" and level <= 3:
                    in_next = True
                    heading_idx = i
                    body_start = i + 1
                    heading_text = raw
            else:
                # H1/H2 always ends ## Next; H3 ends ## Next only if it is a known top-level section.
                if level <= 2 or norm in _SECTION_ALIASES.values():
                    return (heading_idx, body_start, i, heading_text)
            i += 1
            continue

        # Setext heading (e.g. "Next:\n=====")
        if i + 1 < len(lines) and raw.strip() and not raw.lstrip().startswith(("-", "*", "+", ">")):
            nxt_raw = lines[i + 1].rstrip("\r\n")
            m_set = _SETEXT_LINE.match(nxt_raw)
            if m_set:
                norm = _norm_heading(raw)
                if not in_next:
                    if norm == "next":
                        in_next = True
                        heading_idx = i
                        body_start = i + 2
                        heading_text = raw + "\n" + nxt_raw
                        i += 2
                        continue
                else:
                    return (heading_idx, body_start, i, heading_text)
        i += 1

    if in_next:
        return (heading_idx, body_start, len(lines), heading_text)
    return None


def next_markdown(source: str) -> str:
    """Show the original section body, including any lines that need correcting."""
    span = _find_next_span(source)
    if span is None:
        return "## Next\n"
    _, start, end, _ = span
    return "\n".join(["## Next", *source.splitlines()[start:end]]) + "\n"


def _strip_task_metadata(rest: str, owner_tag: str) -> tuple[str | None, str | None, bool, list[str], str, bool]:
    """Parse ``[ID]``, ISO date, owner tag, and ``(after: ...)`` from a task line.

    Returns ``(task_id, date_str, is_owner, deps, clean_title, had_map_syntax)``.
    """
    had_map = False
    task_id: str | None = None
    date_str: str | None = None
    is_owner = False
    deps: list[str] = []

    m = _ID_PREFIX.match(rest)
    if m:
        task_id = m.group(1)
        rest = rest[m.end():]
        had_map = True

    m = _DATE_PREFIX.match(rest)
    if m:
        try:
            dt.date.fromisoformat(m.group(1))
            date_str = m.group(1)
            rest = rest[m.end():]
        except ValueError:
            pass

    if task_id is None:
        m = _ID_PREFIX.match(rest)
        if m:
            task_id = m.group(1)
            rest = rest[m.end():]
            had_map = True

    if owner_tag and rest.startswith(owner_tag):
        after_tag = rest[len(owner_tag):]
        if not after_tag or after_tag[0].isspace():
            is_owner = True
            rest = after_tag.lstrip()

    m = _AFTER_SUFFIX.search(rest)
    if m:
        deps = [d.strip() for d in m.group(1).split(",") if d.strip()]
        rest = rest[:m.start()]
        had_map = True

    return task_id, date_str, is_owner, deps, rest.strip(), had_map


def _find_cycle(tasks: list[MapTask]) -> list[str]:
    """Return a cycle path (list of task IDs) if the dependency graph has a cycle, else []."""
    by_id = {t.id: t for t in tasks}
    colour: dict[str, int] = {}  # 0/absent=unvisited, 1=visiting, 2=done
    stack: list[str] = []

    def dfs(tid: str) -> list[str]:
        colour[tid] = 1
        stack.append(tid)
        for dep in by_id[tid].deps:
            if dep not in by_id:
                continue
            state = colour.get(dep, 0)
            if state == 1:
                idx = stack.index(dep)
                return stack[idx:] + [dep]
            if state == 0:
                found = dfs(dep)
                if found:
                    return found
        stack.pop()
        colour[tid] = 2
        return []

    for task in tasks:
        if colour.get(task.id, 0) == 0:
            found = dfs(task.id)
            if found:
                return found
    return []


def transitive_edges(tasks: list[MapTask]) -> list[tuple[str, str]]:
    """Return ``(dep_id, task_id)`` edges already implied by a longer dependency path.

    For example, if ``C`` depends on ``A`` and ``B``, and ``B`` depends on ``A``,
    then ``("A", "C")`` is transitive: it stays in ``STATUS.md`` but can be hidden
    by default in the graph view to reduce clutter.
    """
    by_id = {t.id: t for t in tasks}
    ancestors: dict[str, set[str]] = {}
    visiting: set[str] = set()

    def get_ancestors(tid: str) -> set[str]:
        if tid in ancestors:
            return ancestors[tid]
        if tid in visiting:
            return set()
        visiting.add(tid)
        acc: set[str] = set()
        task = by_id.get(tid)
        if task:
            for dep in task.deps:
                if dep in by_id:
                    acc.add(dep)
                    acc.update(get_ancestors(dep))
        visiting.discard(tid)
        ancestors[tid] = acc
        return acc

    for t in tasks:
        get_ancestors(t.id)

    redundant: list[tuple[str, str]] = []
    for t in tasks:
        valid_deps = [d for d in t.deps if d in by_id]
        for dep in valid_deps:
            if any(dep in ancestors.get(other, ()) for other in valid_deps if other != dep):
                redundant.append((dep, t.id))
    return redundant


def parse_status_map(source: str, *, owner_tag: str = DEFAULT_OWNER_TAG, strict: bool = False,
                     existing_titles: dict[str, str] | None = None) -> TaskMap:
    """Parse the ``## Next`` section of a ``STATUS.md`` into a ``TaskMap``."""
    tag = normalize_owner_tag(owner_tag)
    span = _find_next_span(source)
    if strict and (not span or span[:3] != (0, 1, len(source.splitlines())) or span[3].strip() != "## Next"):
        raise ValueError("Submit only the ## Next section, starting with its heading.")
    if span is None:
        return TaskMap(groups=[Group("GN", "General")], tasks=[], enabled=False)

    _, body_start, section_end, heading = span
    raw_lines = [line.rstrip("\r\n") for line in source.splitlines(keepends=True)[body_start:section_end]]

    groups: list[Group] = []
    group_ids: set[str] = set()
    tasks: list[MapTask] = []
    used_task_ids: set[str] = set()
    problems: list[str] = []

    current_group: Group | None = None
    current_task: MapTask | None = None
    current_desc_lines: list[str] = []
    has_explicit_map = False

    def flush_desc() -> None:
        nonlocal current_desc_lines
        if current_task is None:
            current_desc_lines = []
            return
        while current_desc_lines and not current_desc_lines[-1].strip():
            current_desc_lines.pop()
        while current_desc_lines and not current_desc_lines[0].strip():
            current_desc_lines.pop(0)
        desc = dedent("\n".join(current_desc_lines)).strip() if current_desc_lines else ""
        current_task.desc = desc
        rec, rest = recommendation(desc)
        repo, _ = target_repo(rest)
        current_task.recommended = rec
        current_task.repo = repo
        current_desc_lines = []

    def ensure_general_group() -> Group:
        nonlocal current_group
        if current_group is not None:
            return current_group
        for g in groups:
            if g.id == "GN":
                current_group = g
                return g
        gid = "GN" if "GN" not in group_ids else _derive_group_id("General", group_ids)
        g = Group(gid, "General")
        groups.append(g)
        group_ids.add(gid)
        current_group = g
        return g

    for lineno, raw in enumerate(raw_lines, start=body_start + 1):
        m_group = _GROUP_HEADING.match(raw)
        if m_group:
            flush_desc()
            current_task = None
            has_explicit_map = True
            name = m_group.group(1).strip()
            gid = m_group.group(2)
            if strict and not gid:
                raise ValueError(f"Line {lineno}: expected ### Group name (PFX).")
            if gid and gid not in group_ids:
                pass
            elif gid and gid in group_ids:
                problems.append(f"Duplicate group prefix {gid}")
                gid = _derive_group_id(name, group_ids)
            else:
                gid = _derive_group_id(name, group_ids)
            current_group = Group(gid, name)
            groups.append(current_group)
            group_ids.add(gid)
            continue

        m_top = _TOP_CHECKBOX.match(raw)
        if m_top:
            flush_desc()
            g = ensure_general_group()
            done = m_top.group(1).lower() == "x"
            tid, date_str, is_owner, deps, title, had_map = _strip_task_metadata(m_top.group(2), tag)
            if strict and (not tid or not title):
                raise ValueError(f"Line {lineno}: expected a task ID and title.")
            if had_map:
                has_explicit_map = True
            if tid:
                if tid in used_task_ids:
                    problems.append(f"Duplicate task ID {tid}")
                    tid = _next_task_id(g.id, used_task_ids)
            else:
                tid = _next_task_id(g.id, used_task_ids)
            used_task_ids.add(tid)
            current_task = MapTask(
                id=tid,
                group=g.id,
                title=title or "Untitled",
                done=done,
                date=date_str,
                owner=is_owner,
                deps=deps,
            )
            tasks.append(current_task)
            continue

        if current_task is not None:
            m_sub = _SUB_CHECKBOX.match(raw)
            if m_sub:
                text = m_sub.group(2).strip()
                if strict and not text:
                    raise ValueError(f"Line {lineno}: empty subtask.")
                if text:
                    current_task.subs.append(Subtask(text=text, done=m_sub.group(1).lower() == "x"))
                continue
            if not raw.strip():
                if current_desc_lines:
                    current_desc_lines.append("")
                continue
            if raw.startswith((" ", "\t")):
                current_desc_lines.append(raw)
                continue
            # Unindented non-checkbox line stops the current task continuation.
            flush_desc()
            current_task = None
        if strict and raw.strip():
            raise ValueError(f"Line {lineno}: expected a group, task, or indented task detail.")

    flush_desc()

    if strict:
        if problems:
            raise ValueError("; ".join(problems))
        groups, tasks = validate_map_payload([g.to_dict() for g in groups], [t.to_dict() for t in tasks],
                                             existing_titles=existing_titles)
        return TaskMap(groups=groups, tasks=tasks, enabled=True)

    if not groups:
        groups.append(Group("GN", "General"))

    # Sanitize dangling/self dependencies on read so a hand-edited file still renders cleanly.
    valid_ids = {t.id for t in tasks}
    for task in tasks:
        clean_deps: list[str] = []
        seen_deps: set[str] = set()
        for dep in task.deps:
            if dep == task.id:
                problems.append(f"{task.id} cannot depend on itself")
                continue
            if dep not in valid_ids:
                problems.append(f"{task.id} depends on unknown task {dep}")
                continue
            if dep not in seen_deps:
                seen_deps.add(dep)
                clean_deps.append(dep)
        task.deps = clean_deps

    # Break any cycles in hand-edited STATUS.md so rendering never loops, while reporting it.
    while True:
        cycle = _find_cycle(tasks)
        if not cycle:
            break
        problems.append(f"Dependency cycle ignored: {' -> '.join(cycle)}")
        src, tgt = cycle[0], cycle[1]
        for t in tasks:
            if t.id == src and tgt in t.deps:
                t.deps.remove(tgt)
                break

    return TaskMap(
        groups=groups,
        tasks=tasks,
        enabled=has_explicit_map,
        heading=heading,
        problems=problems,
    )


def validate_map_payload(groups_raw: Any, tasks_raw: Any, *,
                         existing_titles: dict[str, str] | None = None) -> tuple[list[Group], list[MapTask]]:
    """Strictly validate groups and tasks submitted by the browser before writing STATUS.md."""
    if not isinstance(groups_raw, list) or not groups_raw:
        raise ValueError("At least one group is required.")
    if len(groups_raw) > MAX_GROUPS:
        raise ValueError(f"Too many groups (max {MAX_GROUPS}).")
    if not isinstance(tasks_raw, list):
        raise ValueError("Tasks must be a list.")
    if len(tasks_raw) > MAX_TASKS:
        raise ValueError(f"Too many tasks (max {MAX_TASKS}).")

    groups: list[Group] = []
    group_ids: set[str] = set()
    for item in groups_raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid group entry.")
        gid = str(item.get("id") or "").strip().upper()
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip()
        if not _GROUP_ID_RE.match(gid):
            raise ValueError(f"Invalid group prefix: {gid!r} (use 1-4 letters/digits starting with a letter).")
        if gid in group_ids:
            raise ValueError(f"Duplicate group prefix: {gid}.")
        if not name or len(name) > MAX_GROUP_NAME:
            raise ValueError(f"Group {gid} name must be 1-{MAX_GROUP_NAME} characters.")
        group_ids.add(gid)
        groups.append(Group(id=gid, name=name))

    tasks: list[MapTask] = []
    task_ids: set[str] = set()
    for item in tasks_raw:
        if not isinstance(item, dict):
            raise ValueError("Invalid task entry.")
        tid = str(item.get("id") or "").strip().upper()
        gid = str(item.get("group") or "").strip().upper()
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        if not _TASK_ID_RE.match(tid):
            raise ValueError(f"Invalid task ID: {tid!r}.")
        if tid in task_ids:
            raise ValueError(f"Duplicate task ID: {tid}.")
        if gid not in group_ids:
            raise ValueError(f"Task {tid} refers to unknown group {gid!r}.")
        # Historical STATUS.md entries can exceed the editor's title limit.
        # Preserve the stored text verbatim when editing something else.
        stored_title = (existing_titles or {}).get(tid)
        if stored_title and item.get("title") == stored_title:
            title = stored_title
        elif not title or len(title) > MAX_TITLE:
            raise ValueError(f"Task {tid} title must be 1-{MAX_TITLE} characters.")
        if _AFTER_SUFFIX.search(title):
            raise ValueError(f"Task {tid} title cannot end with '(after: ...)'.")

        done = bool(item.get("done", False))
        raw_date = item.get("date")
        date_str: str | None = None
        if raw_date:
            cand = str(raw_date).strip()
            try:
                date_str = dt.date.fromisoformat(cand).isoformat()
            except ValueError as exc:
                raise ValueError(f"Invalid date for {tid}: {cand!r}.") from exc

        owner = bool(item.get("owner", False))
        desc = str(item.get("desc") or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
        if len(desc) > MAX_DESC:
            raise ValueError(f"Task {tid} description exceeds {MAX_DESC} characters.")

        raw_subs = item.get("subs") or []
        if not isinstance(raw_subs, list) or len(raw_subs) > MAX_SUBTASKS:
            raise ValueError(f"Task {tid} subtasks invalid or exceed {MAX_SUBTASKS}.")
        subs: list[Subtask] = []
        for s in raw_subs:
            if not isinstance(s, dict):
                raise ValueError(f"Invalid subtask in {tid}.")
            text = re.sub(r"\s+", " ", str(s.get("text") or "")).strip()
            if not text or len(text) > MAX_SUBTASK_TEXT:
                raise ValueError(f"Subtask text in {tid} must be 1-{MAX_SUBTASK_TEXT} characters.")
            subs.append(Subtask(text=text, done=bool(s.get("done", False))))

        raw_deps = item.get("deps") or []
        if not isinstance(raw_deps, list):
            raise ValueError(f"Task {tid} deps must be a list.")
        deps: list[str] = []
        for d in raw_deps:
            did = str(d or "").strip().upper()
            if not did:
                continue
            if did == tid:
                raise ValueError(f"Task {tid} cannot depend on itself.")
            if did not in deps:
                deps.append(did)

        rec, rest = recommendation(desc)
        repo, _ = target_repo(rest)
        task_ids.add(tid)
        tasks.append(MapTask(
            id=tid,
            group=gid,
            title=title,
            done=done,
            date=date_str,
            owner=owner,
            deps=deps,
            desc=desc,
            subs=subs,
            recommended=rec,
            repo=repo,
        ))

    for task in tasks:
        missing = [d for d in task.deps if d not in task_ids]
        if missing:
            raise ValueError(f"Task {task.id} depends on unknown task(s): {', '.join(missing)}.")

    cycle = _find_cycle(tasks)
    if cycle:
        raise ValueError(f"Dependency cycle detected: {' -> '.join(cycle)}.")

    return groups, tasks


def _merge_order(base, local, remote, ids):
    """Keep each side's insertions and moves within a lane (or the group list)."""
    orders = [[item.id for item in side if item.id in ids] for side in (base, local, remote)]
    before, ours, theirs = orders
    if before == ours or ours == theirs:
        return theirs
    if before == theirs:
        return ours

    positions = [{key: i for i, key in enumerate(order)} for order in orders]
    graph = TopologicalSorter()
    keys = list(dict.fromkeys(theirs + ours))
    for key in keys:
        graph.add(key)
    for a, b in combinations(keys, 2):
        old, left, right = [
            pos[a] < pos[b] if a in pos and b in pos else None for pos in positions
        ]
        if left is None and right is None:
            continue  # Independent additions have no relative order.
        if old is None and left is not None and right is not None and left != right:
            raise ValueError(f"Conflicting placement of {a} and {b}.")
        first = right if right is not None and (left is None or left == old) else left
        predecessor, successor = (a, b) if first else (b, a)
        graph.add(successor, predecessor)
    try:
        return list(graph.static_order())
    except CycleError as exc:
        raise ValueError("Conflicting changes to map order.") from exc


def _merge_items(base, local, remote):
    original, ours, theirs = ({item.id: item for item in side} for side in (base, local, remote))
    merged = {}
    for key in ours.keys() | theirs.keys():
        old, left, right = original.get(key), ours.get(key), theirs.get(key)
        if left != old and right != old and left != right:
            raise ValueError(f"Conflicting changes to {key}.")
        item = right if left == old else left
        if item is not None:
            merged[key] = item
    return merged


def merge_task_maps(base: TaskMap, local: TaskMap, remote: TaskMap) -> TaskMap:
    """Merge by stable ID, rejecting overlapping edits and invalid combined graphs."""
    if not remote.enabled or remote.problems:
        raise ValueError("The remote task map needs review.")
    groups = _merge_items(base.groups, local.groups, remote.groups)
    tasks = _merge_items(base.tasks, local.tasks, remote.tasks)
    group_ids = _merge_order(base.groups, local.groups, remote.groups, groups)
    if any(task.group not in groups for task in tasks.values()):
        raise ValueError("A task's group was deleted.")
    task_ids = []
    for gid in group_ids:
        lanes = [[t for t in side.tasks if t.group == gid] for side in (base, local, remote)]
        task_ids.extend(_merge_order(*lanes, {key for key, t in tasks.items() if t.group == gid}))
    checked_groups, checked_tasks = validate_map_payload(
        [groups[key].to_dict() for key in group_ids], [tasks[key].to_dict() for key in task_ids],
        existing_titles={t.id: t.title for t in remote.tasks},
    )
    return TaskMap(groups=checked_groups, tasks=checked_tasks, enabled=True)


def serialize_next_body(
    groups: list[Group],
    tasks: list[MapTask],
    *,
    owner_tag: str = DEFAULT_OWNER_TAG,
    eol: str = "\n",
) -> str:
    """Serialize groups and tasks into the body of ``## Next`` (without the heading itself)."""
    tag = normalize_owner_tag(owner_tag)
    valid_ids = {t.id for t in tasks}
    out: list[str] = []

    for g in groups:
        if out:
            out.append("")
        out.append(f"### {g.name} ({g.id})")
        for t in tasks:
            if t.group != g.id:
                continue
            mark = "x" if t.done else " "
            parts = [f"- [{mark}] [{t.id}]"]
            if t.done and t.date:
                parts.append(t.date)
            if t.owner:
                parts.append(tag)
            parts.append(t.title)
            deps = [d for d in t.deps if d in valid_ids]
            line = " ".join(parts)
            if deps:
                line += f" (after: {', '.join(deps)})"
            out.append(line)

            if t.desc:
                for dline in t.desc.split("\n"):
                    out.append(f"  {dline}" if dline.strip() else "")
            for s in t.subs:
                smark = "x" if s.done else " "
                out.append(f"  - [{smark}] {s.text}")

    return eol.join(out) + eol


def first_ready_next_title(tasks: list[MapTask]) -> str:
    """Return the title of the first ready (unblocked) unchecked agent task, or fallback."""
    by_id = {t.id: t for t in tasks}
    fallback = ""
    for t in tasks:
        if t.done or t.owner:
            continue
        if not fallback:
            fallback = t.title
        blocked = any(d in by_id and not by_id[d].done for d in t.deps)
        if not blocked:
            return t.title
    return fallback


def _stamp_frontmatter(source: str, *, next_title: str, today: str, by: str) -> str:
    """Update ``next``, ``updated``, and ``updated_by`` in YAML front matter."""
    fm = _FRONT_MATTER.match(source)
    if not fm:
        return source
    head = source[:fm.end()]
    rest = source[fm.end():]
    eol = "\r\n" if "\r\n" in head else "\n"

    nxt = next_title
    if nxt and (": " in nxt or nxt[0] in "\"'[{&*!|>%@`#"):
        nxt = json.dumps(nxt, ensure_ascii=False)
    if re.search(r"(?m)^next:", head):
        head = re.sub(r"(?m)^next:[^\r\n]*", lambda _: f"next: {nxt}".rstrip(), head, count=1)

    if re.search(r"(?m)^updated:", head):
        head = re.sub(r"(?m)^updated:[^\r\n]*", lambda _: f"updated: {today}", head, count=1)
    else:
        idx = head.rfind(f"{eol}---")
        if idx != -1:
            head = head[:idx] + f"{eol}updated: {today}" + head[idx:]

    if re.search(r"(?m)^updated_by:", head):
        head = re.sub(r"(?m)^updated_by:[^\r\n]*", lambda _: f"updated_by: {by}", head, count=1)
    else:
        head = re.sub(
            r"(?m)^(updated:[^\r\n]*)",
            lambda m: f"{m.group(1)}{eol}updated_by: {by}",
            head,
            count=1,
        )
    return head + rest


def apply_map_to_source(
    source: str,
    groups: list[Group],
    tasks: list[MapTask],
    *,
    owner_tag: str = DEFAULT_OWNER_TAG,
    today: str | None = None,
    updated_by: str = "human",
) -> str:
    """Replace only the ``## Next`` section in ``source`` and refresh front matter."""
    eol = "\r\n" if "\r\n" in source else "\n"
    next_body = serialize_next_body(groups, tasks, owner_tag=owner_tag, eol=eol)
    span = _find_next_span(source)
    lines = source.splitlines(keepends=True)

    if span is not None:
        _, body_start, section_end, _ = span
        before = "".join(lines[:body_start])
        after = "".join(lines[section_end:])
        if before and not before.endswith(("\n", "\r\n")):
            before += eol
        # Keep one blank line before the following section for readability.
        sep = eol if after and not next_body.endswith(eol + eol) else ""
        updated_source = before + next_body + sep + after
    else:
        prefix = source
        if prefix and not prefix.endswith(("\n", "\r\n")):
            prefix += eol
        sep = eol if prefix else ""
        updated_source = f"{prefix}{sep}## Next{eol}{next_body}"

    day = today or dt.date.today().isoformat()
    return _stamp_frontmatter(
        updated_source,
        next_title=first_ready_next_title(tasks),
        today=day,
        by=updated_by,
    )


def match_running_tasks(map_tasks: list[MapTask], active_tasks: list[Any]) -> dict[str, dict[str, str]]:
    """Map task IDs from the graph to currently running/queued Baton tasks."""
    matched: dict[str, dict[str, str]] = {}
    if not map_tasks or not active_tasks:
        return matched

    by_title = {t.title.casefold().strip(): t.id for t in map_tasks}
    id_re = re.compile(r"\[([A-Z][A-Z0-9]{0,3}-\d{1,4})\]")
    lead_id_re = re.compile(r"^([A-Z][A-Z0-9]{0,3}-\d{1,4})\b[:\s-]")
    known_ids = {t.id for t in map_tasks}

    for bt in active_tasks:
        title = str(getattr(bt, "title", "") or "").strip()
        if not title:
            continue
        target_id: str | None = None
        m = id_re.search(title)
        if m and m.group(1) in known_ids:
            target_id = m.group(1)
        else:
            m2 = lead_id_re.match(title)
            if m2 and m2.group(1) in known_ids:
                target_id = m2.group(1)
            else:
                target_id = by_title.get(title.casefold())

        if target_id and (target_id not in matched or getattr(bt, "status", "") == "running"):
            bt_id = str(getattr(bt, "id", ""))
            matched[target_id] = {
                "id": bt_id,
                "status": str(getattr(bt, "status", "running")),
                "title": title,
                "url": f"/tasks/{quote(bt_id, safe='')}",
            }
    return matched
