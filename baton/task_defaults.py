"""Which agent, model and reasoning effort the "New task" form starts with.

Order: the recommendation that came with a drafted task (from STATUS.md),
then the choice of the last local task, then the form's own default (the
first available agent with its CLI default model).
"""


def describe(choice: dict) -> str:
    return " · ".join(x for x in (choice.get("agent"), choice.get("model"), choice.get("effort")) if x)


def last_used(tasks) -> dict:
    local = [t for t in tasks if t.where == "local" and t.agent]
    if not local:
        return {}
    last = max(local, key=lambda t: t.created_at)
    return {"agent": last.agent, "model": last.model, "effort": last.effort}


def _usable(choice: dict, available: dict, catalogs: dict, oc_models: list) -> dict:
    """Keep only what this computer can run right now; {} if the agent itself can't."""
    agent = choice.get("agent", "")
    if not agent or available.get(agent, "missing"):
        return {}
    model, effort = choice.get("model", ""), choice.get("effort", "")
    if agent in ("claude-code", "codex", "gemini"):
        options = catalogs.get(agent) or {}
        if model not in options:
            model, effort = "", ""
        elif effort not in options[model]["efforts"]:
            effort = ""
    elif agent == "opencode":
        effort = ""
        if model not in oc_models:
            model = ""
    else:
        model, effort = "", ""
    return {"agent": agent, "model": model, "effort": effort}


def initial_choice(asked: dict, tasks, available: dict, catalogs: dict, oc_models: list) -> dict:
    result = {"agent": "", "model": "", "effort": "", "source": "", "missing": ""}
    if asked.get("agent"):
        usable = _usable(asked, available, catalogs, oc_models)
        if usable:
            result.update(usable, source="recommended")
            if (usable["model"], usable["effort"]) != (asked.get("model", ""), asked.get("effort", "")):
                result["missing"] = describe(asked)
            return result
        result["missing"] = describe(asked)
    usable = _usable(last_used(tasks), available, catalogs, oc_models)
    if usable:
        result.update(usable, source="last")
    return result
