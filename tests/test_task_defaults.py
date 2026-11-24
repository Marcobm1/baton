import asyncio
import html
import re
from types import SimpleNamespace

from fastapi.testclient import TestClient

from baton import runner as runner_mod, task_defaults, web
from baton.config import Settings
from baton.github import Hub, Project
from baton.project_detail import recommendation, sections
from baton.runner import Task
from baton.status import parse

AVAILABLE = {"claude-code": "", "codex": "", "opencode": "", "local": "no model"}
CATALOGS = {"codex": {"gpt-a": {"label": "GPT A", "efforts": ["low", "high"]}},
            "claude-code": {"opus": {"label": "Opus", "efforts": ["high"]}}}
OC = ["opencode-go/glm-5.3"]


def task(agent, model="", effort="", at=1.0, where="local"):
    return SimpleNamespace(agent=agent, model=model, effort=effort, created_at=at, where=where)


def test_recommendation_line_is_parsed_and_removed():
    rec, rest = recommendation("Build it.\nRecommended: Codex, gpt-a, High\nCheck it.")
    assert rec == {"agent": "codex", "model": "gpt-a", "effort": "high"} and rest == "Build it.\nCheck it."
    assert recommendation("Recomendado: opencode, opencode-go/glm-5.3")[0]["model"] == "opencode-go/glm-5.3"
    assert recommendation("Recommended: claude")[0] == {"agent": "claude-code", "model": "", "effort": ""}
    assert recommendation("Recommended: someone else")[0] == {}
    item = sections("## Next\n- [ ] Do it\n  Steps.\n  Recommended: codex, gpt-a\n").checklist[0]
    assert item.recommended["model"] == "gpt-a" and item.detail == "Steps."


def test_initial_choice_order():
    history = [task("claude-code", "opus", "high", 1), task("codex", "gpt-a", "low", 5),
               task("claude-code", at=9, where="cloud")]
    pick = lambda asked: task_defaults.initial_choice(asked, history, AVAILABLE, CATALOGS, OC)
    # The draft's recommendation wins.
    r = pick({"agent": "opencode", "model": OC[0], "effort": ""})
    assert (r["agent"], r["model"], r["source"], r["missing"]) == ("opencode", OC[0], "recommended", "")
    # No recommendation: the last local task.
    r = pick({})
    assert (r["agent"], r["model"], r["effort"], r["source"]) == ("codex", "gpt-a", "low", "last")
    # A model this computer doesn't list keeps the agent and says so.
    r = pick({"agent": "codex", "model": "gpt-z", "effort": "high"})
    assert (r["agent"], r["model"], r["source"]) == ("codex", "", "recommended") and "gpt-z" in r["missing"]
    # An agent that isn't installed falls back to the last choice.
    r = pick({"agent": "local"})
    assert (r["agent"], r["source"], r["missing"]) == ("codex", "last", "local")
    assert task_defaults.initial_choice({}, [], AVAILABLE, CATALOGS, OC)["source"] == ""


def test_prompt_asks_for_a_recommendation():
    text = runner_mod.next_task_request("codex: gpt-a (low/high)")
    assert runner_mod.NEXT_TASK_REQUEST in text and "Recommended: <agent>, <model>, <effort>" in text
    assert "codex: gpt-a (low/high)" in text
    assert runner_mod.next_task_request("") == runner_mod.NEXT_TASK_REQUEST


def test_draft_link_preselects_the_recommended_model(tmp_path, monkeypatch):
    body = "## Next\n- [ ] Add export\n  Write the CSV export.\n  Recommended: codex, gpt-a, high\n"
    project = Project("u/demo", "u/demo", "STATUS.md", "main", False, "", None,
                      parse("---\nname: Demo\nstatus: active\nupdated: 2026-11-24\n---\n" + body))

    async def load(self, force=False):
        return [project], []

    monkeypatch.setattr(Hub, "load", load)
    monkeypatch.setattr(Hub, "open_pulls", lambda self, repo: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(web.Runner, "agents_available", lambda self: AVAILABLE)
    monkeypatch.setattr(web, "catalog", lambda agent: CATALOGS.get(agent, {}))
    monkeypatch.setattr(web, "opencode_models", lambda: OC)
    client = TestClient(web.create_app(Settings(data_dir=tmp_path)))

    cards = client.get("/").text
    assert "Launch next task" in cards and "agent=codex" in cards
    page = client.get("/p/u/demo").text
    featured = page.split('class="next-up"', 1)[1].split("</section>", 1)[0]
    assert "★ Codex · gpt-a · high" in featured and "Recommended: codex" not in featured
    url = re.search(r'href="(/tasks/new\?[^"]+)">Launch next task →</a>', page)[1]
    form = client.get(html.unescape(url)).text
    assert re.search(r'value="local:codex" checked', form)
    assert '"source": "recommended"' in form and '"model": "gpt-a"' in form and '"effort": "high"' in form
    assert "Write the CSV export.</textarea>" in form

    # Without a recommendation the form starts from the last local task.
    store = runner_mod.TaskStore(tmp_path)
    store.save(Task(id="t1", repo="u/demo", title="x", details="", agent="claude-code", where="local",
                    created_at=1.0, model="opus", effort="high"))
    form = client.get("/tasks/new?repo=u/demo").text
    assert re.search(r'value="local:claude-code" checked', form) and '"source": "last"' in form
    assert "Your last choice" in form


def test_spanish_headings():
    s = sections("## Siguiente\n- [ ] Uno\n  Detalle.\n## Bloqueos\n- Falta X\n## Registro\n- 2026-11-24 (claude): creado\n")
    assert [i.title for i in s.checklist] == ["Uno"] and s.blockers == ["Falta X"] and len(s.log) == 1


def test_every_local_script_and_stylesheet_is_versioned():
    from pathlib import Path
    templates = Path(web.__file__).parent / "templates"
    for page in templates.glob("*.html"):
        text = page.read_text(encoding="utf-8")  # Windows would decode cp1252 by default
        for ref in re.findall(r'(?:src|href)="(/static/[^"]+\.(?:js|css)[^"]*)"', text):
            assert "?v={{ css_v }}" in ref, f"{page.name}: {ref} would stay cached after an update"
