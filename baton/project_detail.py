"""Read the project dashboard from Markdown source and existing task records."""

from dataclasses import dataclass, field
from datetime import date, datetime
import re
from textwrap import dedent

from markdown_it import MarkdownIt

from .runner import Task


@dataclass
class ChecklistItem:
    title: str
    checked: bool
    detail: str


@dataclass
class Sections:
    checklist: list[ChecklistItem] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


_markdown = MarkdownIt("commonmark")
_marker = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s*")
_checkbox = re.compile(r"^\[([ xX])\]\s+(.+)")


def sections(body: str) -> Sections:
    result = Sections()
    lines = body.expandtabs(4).splitlines()
    tokens = _markdown.parse("\n".join(lines))
    section = ""
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            section = tokens[i + 1].content.strip().rstrip(":").strip().casefold()
            continue
        is_item = token.type == "list_item_open" and token.level == 1
        is_paragraph = token.type == "paragraph_open" and token.level == 0
        if not token.map or not (is_item or is_paragraph):
            continue
        start, end = token.map
        first = lines[start].strip()
        if is_item:
            first = _marker.sub("", first, count=1)
        if section == "next" and is_item:
            box = _checkbox.match(first)
            if box:
                # Only indented continuations belong to the task draft.
                detail = []
                for line in lines[start + 1:end]:
                    if line and not line.startswith(" "):
                        break
                    detail.append(line)
                result.checklist.append(ChecklistItem(
                    box[2].strip(), box[1].lower() == "x", dedent("\n".join(detail)).strip()))
        elif section in ("blockers", "decisions", "log"):
            entry = " ".join([first, *(line.strip() for line in lines[start + 1:end])]).strip()
            if entry and not (section == "blockers" and entry.casefold().rstrip(".") == "none"):
                getattr(result, section).append(entry)
    return result


@dataclass
class Finished:
    title: str
    when: str = ""
    task: Task | None = None


def finished_items(checklist: list[ChecklistItem], tasks: list[Task]) -> list[Finished]:
    items = []
    for item in checklist:
        if item.checked:
            when = ""
            match = re.match(r"(\d{4}-\d{2}-\d{2})\b", item.title)
            if match:
                try:
                    when = date.fromisoformat(match[1]).isoformat()
                except ValueError:
                    pass
            items.append(Finished(item.title, when))
    for task in tasks:
        if task.status in ("done", "no-changes", "cancelled", "failed") or (
            task.status == "sent" and task.summary
        ):
            when = datetime.fromtimestamp(task.finished_at).isoformat(" ") if task.finished_at else ""
            items.append(Finished(task.title, when, task))
    return sorted(items, key=lambda item: item.when, reverse=True)
