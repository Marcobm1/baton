"""Shape Ranking data for the page: readable model names, a leaderboard,
summary numbers and pages of jobs. No database access here."""

from __future__ import annotations

import math

from . import task_models

PER_PAGE_JOBS = 8
PER_PAGE_TASKS = 10
MIN_SAMPLES = 3          # fewer scores than this: the average is shown as tentative

_PROVIDERS = {"opencode-go": "OpenCode Go", "opencode": "OpenCode"}
_ACRONYMS = {"glm", "gpt", "llm", "ai"}


def paginate(items: list, page: int, per_page: int) -> dict:
    """One page of items plus what the pager needs. Out-of-range pages clamp."""
    pages = max(1, math.ceil(len(items) / per_page))
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    return {"items": items[start:start + per_page], "page": page, "pages": pages,
            "total": len(items), "first": start + 1 if items else 0,
            "last": min(start + per_page, len(items))}


def model_label(model: str) -> dict:
    """A readable name and the tool that ran it, without calling any CLI:
    only labels the task form has already read are reused."""
    if not model:
        return {"name": "", "tool": ""}
    for agent, tool in (("claude-code", "Claude Code"), ("codex", "Codex")):
        cached = task_models._cache.get(agent, (0, {}))[1]
        if model in cached:
            return {"name": cached[model]["label"], "tool": tool}
    if "/" in model:
        provider, name = model.split("/", 1)
        pretty = name.replace("-", " ")
        pretty = " ".join(w.upper() if w in _ACRONYMS else w.capitalize() for w in pretty.split())
        return {"name": pretty, "tool": _PROVIDERS.get(provider, provider)}
    if model.startswith("gpt-"):
        parts = model.split("-")            # gpt-6.2-astra -> GPT-6.2 Astra
        return {"name": " ".join(["GPT-" + parts[1], *(w.capitalize() for w in parts[2:])]).strip(),
                "tool": "Codex"}
    if model in ("opus", "sonnet", "haiku", "fable") or model.startswith("claude"):
        return {"name": model.capitalize(), "tool": "Claude Code"}
    return {"name": model, "tool": ""}


def leaderboard(models: list[dict], jobs: list[dict], view: str) -> list[dict]:
    """Models ranked by the chosen score, with counts that show how much each
    average rests on: real reviews, estimates and your own scores."""
    rows = []
    for model in models:
        entries = [j for j in jobs if j["model"] == model["model"]]
        real = sum(1 for j in entries if j["agent"] is not None and not j.get("estimate"))
        estimated = sum(1 for j in entries if j["agent"] is not None and j.get("estimate"))
        human = sum(1 for j in entries if j["human"] is not None)
        pairs = sum(1 for j in entries if j["agent"] is not None and j["human"] is not None)
        samples = {"agent": real + estimated, "human": human, "combined": pairs}[view]
        rows.append({**model, **model_label(model["model"]), "jobs": len(entries),
                     "agent_real": real, "agent_estimated": estimated, "human_count": human,
                     "samples": samples, "tentative": 0 < samples < MIN_SAMPLES})
    rows.sort(key=lambda r: (r[view] is None, -(r[view] or 0), -r["samples"], r["model"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank if row[view] is not None else None
    return rows


def summary(rows: list[dict], jobs: list[dict], view: str) -> dict:
    rated = sum(1 for j in jobs if j["human"] is not None)
    best = next((r for r in rows if r[view] is not None and not r["tentative"]), None) \
        or next((r for r in rows if r[view] is not None), None)
    pending = [j for j in jobs if j["human"] is None and j["outcome"] in ("done", "no-changes", "failed")]
    return {"jobs": len(jobs), "rated": rated, "pending": len(pending), "best": best,
            "next_pending": pending[0]["id"] if pending else None}


def job_list(jobs: list[dict], tab: str, model: str | None) -> list[dict]:
    if model is not None:
        jobs = [j for j in jobs if j["model"] == model]
    if tab == "pending":
        jobs = [j for j in jobs if j["human"] is None and j["outcome"] in ("done", "no-changes", "failed")]
    return [{**j, **{"label": model_label(j["model"])}} for j in jobs]


def without_estimates(models: list[dict], jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """The same overview counting only reviewed agent scores (no estimates)."""
    jobs = [{**j, "agent": None, "estimate": None} if j.get("estimate") else j for j in jobs]
    out = []
    for model in models:
        entries = [j for j in jobs if j["model"] == model["model"]]
        row = dict(model)
        for role in ("agent", "human"):
            scores = [j[role] for j in entries if j[role] is not None]
            row[role] = sum(scores) / len(scores) if scores else None
        pairs = [(j["agent"] + j["human"]) / 2 for j in entries
                 if j["agent"] is not None and j["human"] is not None]
        row["combined"] = sum(pairs) / len(pairs) if pairs else None
        out.append(row)
    return out, jobs
