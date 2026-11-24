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


def test_continue_finished_task(tmp_path, monkeypatch):
    project = Project("u/demo", "u/demo", "STATUS.md", "main", False, "", None, parse(""))
    async def load(self, force=False):
        return [project], []

    monkeypatch.setattr(Hub, "load", load)
    monkeypatch.setattr(web.Runner, "agents_available", lambda self: AVAILABLE)
    monkeypatch.setattr(web, "catalog", lambda agent: CATALOGS.get(agent, {}))
    monkeypatch.setattr(web, "opencode_models", lambda: OC)
    client = TestClient(web.create_app(Settings(data_dir=tmp_path, lang="es", github_token="test")))
    store = runner_mod.TaskStore(tmp_path)
    original = Task("first", "u/demo", "Export", "A" * 5000, "codex", "local", 1,
                    status="done", model="gpt-a", effort="high", summary="Check <output>",
                    pr_url="https://github.com/u/demo/pull/1")
    store.save(original)
    store.save(Task("later", "u/demo", "Other", "", "claude-code", "local", 2))
    url = "/tasks/new?followup=first"
    assert f'href="{url}">Continuar' in client.get("/tasks/first").text
    assert "Continuar" not in client.get("/tasks/later").text
    assert client.get("/tasks/new?followup=later").status_code == 404
    form = client.get(url).text
    assert 'value="Seguimiento: Export"' in form and 'value="u/demo" selected' in form
    assert 'value="local:codex" checked' in form
    assert '"model": "gpt-a"' in form and '"effort": "high"' in form
    for fragment in (original.details, "Check &lt;output&gt;", original.pr_url, "Mi respuesta"):
        assert fragment in form.split("<textarea")[1].split("</textarea>")[0]
    monkeypatch.setattr(web.LocalRepos, "get", lambda self, repo: tmp_path)
    monkeypatch.setattr(web.Runner, "submit", lambda self, task: store.save(task))
    csrf = re.search(r'name="csrf" value="([^"]+)"', form)[1]
    reply = html.unescape(form.split("</textarea>")[0].rsplit(">", 1)[1]) + "Please verify."
    response = client.post("/tasks", data={"csrf": csrf, "followup": "first", "repo": original.repo,
                           "where": "local:codex", "model": "gpt-a", "effort": "high", "title": "Follow up",
                           "details": reply}, follow_redirects=False)
    assert response.status_code == 303
    saved = store.get(response.headers["location"].split("/")[-1])
    assert saved.details == reply and len(saved.details) > 5000
    assert (saved.agent, saved.model, saved.effort) == ("codex", "gpt-a", "high")
    original.where, original.agent, original.status = "cloud", "claude-code", "sent"
    store.save(original)
    assert 'value="cloud" checked' in client.get(url).text


def test_tasks_and_ranking_are_paged(tmp_path, monkeypatch):
    from baton.ranking import Ranking
    from baton.ranking_view import paginate
    assert paginate(list(range(25)), 9, 10) == {"items": list(range(20, 25)), "page": 3, "pages": 3,
                                                "total": 25, "first": 21, "last": 25}
    assert paginate([], 1, 10)["pages"] == 1

    async def load(self, force=False):
        return [], []

    monkeypatch.setattr(Hub, "load", load)
    store = runner_mod.TaskStore(tmp_path)
    for i in range(23):
        store.save(Task(id=f"t{i:02}", repo="u/demo", title=f"Task {i:02}", details="", agent="codex",
                        where="local", created_at=1000.0 + i, status="failed" if i % 5 == 0 else "done",
                        finished_at=2000.0 + i, model="gpt-a"))
    client = TestClient(web.create_app(Settings(data_dir=tmp_path, lang="en")))
    # Saved after start-up: Baton marks tasks left running by a restart as failed.
    store.save(Task(id="run", repo="u/demo", title="Running one", details="", agent="codex",
                    where="local", created_at=5000.0, status="running"))

    page = client.get("/tasks").text
    assert "Running one" in page and page.count('class="task-row') == 1 + 10
    assert "Page 1 of 3" in page and 'href="?page=2#history"' in page
    last = client.get("/tasks?page=3").text
    assert last.count('class="task-row') == 1 + 3
    failed = client.get("/tasks?status=failed").text
    assert failed.count('class="task-row st-failed') == 5 and "Page 1 of" not in failed

    ranking = Ranking(tmp_path)
    assert len(ranking.overview()[1]) == 23
    board = client.get("/ranking").text
    jobs = board.split('id="jobs"', 1)[1]
    assert jobs.count('class="task-row ranking-entry"') == 8 and "Page 1 of 3" in jobs
    assert "GPT-a" in board or "gpt-a" in board
    pending = client.get("/ranking?tab=pending&page=3").text.split('id="jobs"', 1)[1]
    assert pending.count('class="task-row ranking-entry"') == 7          # 23 jobs: 8 + 8 + 7
    only = client.get("/ranking?model=gpt-a").text
    assert "All models" in only


def test_ranking_options_quick_rate_and_repo_filter(tmp_path, monkeypatch):
    from baton.ranking import Ranking

    async def load(self, force=False):
        return [], []

    monkeypatch.setattr(Hub, "load", load)
    store = runner_mod.TaskStore(tmp_path)
    for i, repo in enumerate(["u/a", "u/a", "u/b"]):
        store.save(Task(id=f"t{i}", repo=repo, title=f"Task {i}", details="", agent="codex", where="local",
                        created_at=1.0 + i, status="done", finished_at=2.0 + i, merged=True, model="gpt-a"))
    client = TestClient(web.create_app(Settings(data_dir=tmp_path, lang="en")))
    board = client.get("/ranking").text
    assert "Reviewed scores only" in board and "8.0" in board          # merged = estimated 8
    real = client.get("/ranking?real=1").text
    assert "Only scores from a real review" in real and 'aria-checked="true"' in real
    assert re.search(r'class="metric main">.*?<span class="metric-value">—</span>', real, re.S)

    # Rate from the list and come back to the same list.
    page = client.get("/ranking?tab=pending&real=1").text
    form = re.search(r'<details class="quick-rate">.*?</details>', page, re.S)[0]
    data = dict(re.findall(r'name="(\w+)" value="([^"]*)"', form))
    action = re.search(r'action="([^"]+)"', form)[1]
    back = html.unescape(data["back"])
    assert back.startswith("/ranking?") and "real=1" in back and "tab=pending" in back
    saved = client.post(action, data={**data, "back": back, "fulfillment": 9, "correctness": 8, "rework": 7},
                        follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == back + "&saved=1#jobs"
    assert Ranking(tmp_path).overview()[0][0]["human"] == 8.0
    assert client.post(action, data={**data, "back": "https://evil.test/", "fulfillment": 9, "correctness": 8,
                                     "rework": 7}, follow_redirects=False).headers["location"].startswith("/ranking?origin=")

    tasks = client.get("/tasks?repo=u/b").text
    assert "Task 2" in tasks and "Task 0" not in tasks and "All repositories" in tasks


def test_status_markdown_links_point_to_github():
    from types import SimpleNamespace
    from baton.web import _render_repo_md
    p = SimpleNamespace(repo="u/hub", branch="main", path="projects/x/STATUS.md")
    out = _render_repo_md("See [the doc](docs/a.md), [web](https://e.org) and [x](javascript:alert(1)).", p)
    assert 'href="https://github.com/u/hub/blob/main/projects/x/docs/a.md"' in out
    assert 'href="https://e.org"' in out and 'href="javascript' not in out and "blob/main/projects/x/javascript" not in out
