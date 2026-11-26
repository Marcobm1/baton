"""Read the project dashboard from Markdown source and existing task records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import re
from textwrap import dedent

from typing import TYPE_CHECKING

from markdown_it import MarkdownIt

if TYPE_CHECKING:
    from .task_store import Task


# A line in a Next item's description that names the agent to use:
#   Recommended: codex, gpt-6.2-astra, high
# (agent, then optional model and reasoning effort; commas because model
# names can contain "/" or ".").
_recommended = re.compile(r"^\s*(?:recommended|recomendado)\s*:\s*(.+?)\s*$", re.I | re.M)
_agent_names = {"claude": "claude-code", "claude code": "claude-code", "claude-code": "claude-code",
                "codex": "codex", "gemini": "gemini", "antigravity": "gemini", "agy": "gemini",
                "opencode": "opencode", "local": "local", "local ai": "local", "ia local": "local"}


def recommendation(detail: str) -> tuple[dict, str]:
    """Split a task description into its recommendation (if any) and the rest."""
    match = _recommended.search(detail or "")
    if not match:
        return {}, detail
    parts = [part.strip() for part in match[1].split(",")]
    agent = _agent_names.get(parts[0].casefold())
    end = match.end() + (detail[match.end():match.end() + 1] == "\n")
    rest = (detail[:match.start()] + detail[end:]).strip()
    if not agent:
        return {}, rest
    return {"agent": agent, "model": parts[1] if len(parts) > 1 else "",
            "effort": parts[2].casefold() if len(parts) > 2 else ""}, rest


DEFAULT_OWNER_TAG = "(Owner)"


def normalize_owner_tag(tag: str | None) -> str:
    """Normalize BATON_OWNER_TAG into a checklist prefix (default '(Owner)')."""
    clean = (tag or "").strip()
    if not clean:
        return DEFAULT_OWNER_TAG
    if clean.startswith(("(", "[")) or clean.endswith(":"):
        return clean
    return f"({clean})"


# A line naming the repository where the task is done, when it is not the
# project's own:  Repo: owner/hub
_repo_line = re.compile(r"^\s*(?:repo|repositorio)\s*:\s*([\w.-]+/[\w.-]+)\s*$", re.I | re.M)


def target_repo(detail: str) -> tuple[str, str]:
    """Split a task description into its target repository (if any) and the rest."""
    match = _repo_line.search(detail or "")
    if not match:
        return "", detail
    end = match.end() + (detail[match.end():match.end() + 1] == "\n")
    return match[1], (detail[:match.start()] + detail[end:]).strip()


@dataclass
class ChecklistItem:
    title: str
    checked: bool
    detail: str
    recommended: dict = field(default_factory=dict)
    line: int = 0
    repo: str = ""          # where the task is done, if not in the project's repository
    owner_tag: str = DEFAULT_OWNER_TAG

    @property
    def owner_action(self) -> bool:
        return bool(self.owner_tag) and self.title.startswith(self.owner_tag)

    @property
    def display_title(self) -> str:
        if self.owner_action:
            return self.title[len(self.owner_tag):].strip()
        return self.title

    def __post_init__(self):
        if not self.recommended:
            self.recommended, self.detail = recommendation(self.detail)
        if not self.repo:
            self.repo, self.detail = target_repo(self.detail)


@dataclass
class Sections:
    checklist: list[ChecklistItem] = field(default_factory=list)
    done_recently: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def upcoming(self):
        return [item for item in self.checklist if not item.checked and not item.owner_action]

    @property
    def owner_actions(self):
        return [item for item in self.checklist if not item.checked and item.owner_action]


_markdown = MarkdownIt("commonmark")
# STATUS.md files written in Spanish use these section names.
_HEADINGS = {"siguiente": "next", "próximo": "next", "próximas tareas": "next",
             "hecho recientemente": "done recently", "bloqueos": "blockers",
             "decisiones": "decisions", "registro": "log"}
_marker = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s*")
_checkbox = re.compile(r"^\[([ xX])\]\s+(.+)")


def sections(body: str, *, owner_tag: str = DEFAULT_OWNER_TAG) -> Sections:
    tag = normalize_owner_tag(owner_tag)
    result = Sections()
    lines = body.expandtabs(4).splitlines()
    tokens = _markdown.parse("\n".join(lines))
    section = ""
    for i, token in enumerate(tokens):
        if token.type == "heading_open" and token.level == 0:
            section = tokens[i + 1].content.strip().rstrip(":").strip().casefold()
            section = _HEADINGS.get(section, section)
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
                    box[2].strip(), box[1].lower() == "x", dedent("\n".join(detail)).strip(),
                    line=start, owner_tag=tag))
        elif section in ("done recently", "blockers", "decisions", "log"):
            entry = " ".join([first, *(line.strip() for line in lines[start + 1:end])]).strip()
            if entry and not (section == "blockers" and entry.casefold().rstrip(".") == "none"):
                getattr(result, section.replace(" ", "_")).append(entry)
    return result


@dataclass
class Finished:
    title: str
    when: str = ""
    task: Task | None = None


def finished_items(content: Sections, tasks: list[Task]) -> list[Finished]:
    items = []
    titles = [item.title for item in content.checklist if item.checked]
    for title in titles + content.done_recently:
        when = ""
        match = re.match(r"(\d{4}-\d{2}-\d{2})\b", title)
        if match:
            try:
                when = date.fromisoformat(match[1]).isoformat()
            except ValueError:
                pass
        items.append(Finished(title, when))
    for task in tasks:
        if task.finished:
            when = datetime.fromtimestamp(task.finished_at).isoformat(" ") if task.finished_at else ""
            items.append(Finished(task.title, when, task))
    return sorted(items, key=lambda item: item.when, reverse=True)


def summary_headline(text: str, limit: int = 160) -> str:
    """Return a clean one-line preview from an agent summary (preferring `Summary:`)."""
    if not text:
        return ""
    fallback = ""
    for raw in text.splitlines():
        line = re.sub(r"^[#>*\-\s]+", "", raw).strip()
        if not line:
            continue
        if line.lower().startswith("summary:"):
            head = line.split(":", 1)[1].strip()
            if head:
                return head[:limit - 1].rstrip() + "…" if len(head) > limit else head
        if not fallback:
            fallback = line
    return fallback[:limit - 1].rstrip() + "…" if len(fallback) > limit else fallback
