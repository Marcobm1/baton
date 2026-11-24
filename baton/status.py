"""Parse and validate STATUS.md files.

A STATUS.md is Markdown with a YAML front matter block:

    ---
    name: My project
    status: active        # active | planned | paused | done
    priority: high        # high | medium | low
    summary: One line about what this is.
    next: The very next concrete step.
    updated: 2026-11-23
    updated_by: claude-code
    ---

    ## Done recently
    - ...

    ## Next
    - [ ] a task
    - [x] a finished task

Everything after the front matter is free Markdown. Check boxes in the body
are counted to show a progress bar.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

STATUSES = ("active", "planned", "paused", "done")
PRIORITIES = ("high", "medium", "low")
REQUIRED = ("name", "status", "updated")

_FRONT_MATTER = re.compile(r"\A﻿?---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", re.S)
_BOX = re.compile(r"^\s*[-*+]\s+\[( |x|X)\]\s", re.M)
_OPEN_BOX = re.compile(r"^\s*[-*+]\s+\[ \]\s+(.+)$", re.M)


@dataclass
class Status:
    name: str
    status: str = "active"
    priority: str = "medium"
    summary: str = ""
    next: str = ""
    updated: dt.date | None = None
    updated_by: str = ""
    open_source: bool = False
    tags: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    body: str = ""
    source: str = ""
    tasks_done: int = 0
    tasks_total: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def next_task(self) -> str:
        """The first unticked task in the body, or the 'next' field."""
        m = _OPEN_BOX.search(self.body)
        text = m.group(1) if m else self.next
        text = re.sub(r"[`*_]", "", text).strip()
        return text

    @property
    def tasks_left(self) -> int:
        return self.tasks_total - self.tasks_done

    @property
    def progress(self) -> int | None:
        if not self.tasks_total:
            return None
        return round(100 * self.tasks_done / self.tasks_total)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "priority": self.priority,
            "summary": self.summary,
            "next": self.next,
            "updated": self.updated.isoformat() if self.updated else None,
            "updated_by": self.updated_by,
            "open_source": self.open_source,
            "tags": self.tags,
            "links": self.links,
            "tasks_done": self.tasks_done,
            "tasks_total": self.tasks_total,
            "progress": self.progress,
            "next_task": self.next_task,
            "tasks_left": self.tasks_left,
            "problems": self.problems,
        }


def _as_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def parse(text: str, fallback_name: str = "") -> Status:
    """Parse a STATUS.md. Never raises: problems are collected in .problems."""
    problems: list[str] = []
    meta: dict[str, Any] = {}
    body = text
    match = _FRONT_MATTER.match(text)
    if not match:
        problems.append("missing YAML front matter (--- ... ---) at the top")
    else:
        body = text[match.end():]
        try:
            loaded = yaml.safe_load(match.group(1)) or {}
            if isinstance(loaded, dict):
                meta = loaded
            else:
                problems.append("front matter is not a key: value mapping")
        except yaml.YAMLError as exc:
            problems.append(f"front matter is not valid YAML: {exc.__class__.__name__}")

    for key in REQUIRED:
        if meta.get(key) in (None, ""):
            problems.append(f"missing required field '{key}'")

    status = str(meta.get("status", "active")).strip().lower()
    if status not in STATUSES:
        problems.append(f"status '{status}' is not one of {', '.join(STATUSES)}")
        status = "active"
    priority = str(meta.get("priority", "medium")).strip().lower()
    if priority not in PRIORITIES:
        problems.append(f"priority '{priority}' is not one of {', '.join(PRIORITIES)}")
        priority = "medium"

    updated = _as_date(meta.get("updated"))
    if meta.get("updated") not in (None, "") and updated is None:
        problems.append("'updated' must be a date like 2026-11-23")

    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    links = meta.get("links") or {}
    if not isinstance(links, dict):
        problems.append("'links' must be a mapping of label: url")
        links = {}

    boxes = _BOX.findall(body)
    return Status(
        name=str(meta.get("name") or fallback_name or "Unnamed project"),
        status=status,
        priority=priority,
        summary=str(meta.get("summary") or "").strip(),
        next=str(meta.get("next") or "").strip(),
        updated=updated,
        updated_by=str(meta.get("updated_by") or "").strip(),
        open_source=bool(meta.get("open_source", False)),
        tags=[str(t) for t in tags],
        links={str(k): str(v) for k, v in links.items()},
        body=body.strip(),
        source=text,
        tasks_done=sum(1 for b in boxes if b.lower() == "x"),
        tasks_total=len(boxes),
        problems=problems,
    )


TEMPLATE = """---
name: {name}
status: active        # active | planned | paused | done
priority: medium      # high | medium | low
summary: One line about what this project is.
next: The very next concrete step.
updated: {today}
updated_by: {who}
open_source: false
---

## Done recently
- {today}: created this STATUS.md

## Next
- [ ] First task
  Describe the work, prerequisites and how to check the result here.

## Blockers
- None

## Decisions
-

## Log
- {today} ({who}): STATUS.md created.
"""


def template(name: str, who: str = "human") -> str:
    return TEMPLATE.format(name=name, today=dt.date.today().isoformat(), who=who)
