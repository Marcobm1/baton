import asyncio
import base64
import html
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from baton.config import Settings
from baton.github import Hub, Project
from baton.project_detail import sections
from baton.runner import AGENTS, Runner, Task, TaskStore
from baton.status import parse
from baton.web import create_app


BODY = """---
name: Project
status: active
updated: 2026-11-24
---
## Done recently
- 2023-02-01 First release

Next:
=====
* [ ] Fix "A&B"
  Use <script> & check.
  Second line.
* [X] 2024-01-01 Older milestone
* [ ] No detail

## Other
- [x] Ignore

### Decisions ###
- First decision
  continued here.
## Blockers
- None.
## LOG
- Latest entry
  wrapped.
"""


def test_sections():
    content = sections(BODY.split("---", 2)[-1])
    assert [(x.title, x.checked, x.detail) for x in content.checklist] == [
        ('Fix "A&B"', False, "Use <script> & check.\nSecond line."),
        ("2024-01-01 Older milestone", True, ""), ("No detail", False, "")]
    assert content.decisions == ["First decision continued here."]
    assert content.log == ["Latest entry wrapped."]
    assert not content.blockers
    empty = sections("A few notes\n\n```\n## Next\n- [ ] example\n```")
    assert not any(vars(empty).values())


@pytest.mark.parametrize("lang", ["en", "es"])
def test_dashboard_without_token(tmp_path, monkeypatch, lang):
    def github(request):
        assert "Authorization" not in request.headers
        data = {
            "/repos/u/demo": {"name": "demo", "full_name": "u/demo", "html_url": "https://github.com/u/demo"},
            "/repos/u/demo/contents/": [{"name": "STATUS.md", "type": "file"}],
        }
        if request.url.path.endswith("/contents/STATUS.md"):
            return httpx.Response(200, text=BODY)
        return httpx.Response(200, json=data[request.url.path])

    monkeypatch.setattr(Hub, "_client", lambda self: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(github)))
    monkeypatch.setattr(Runner, "agents_available", lambda self: dict.fromkeys(AGENTS, "offline"))
    client = TestClient(create_app(Settings(repos=["u/demo"], data_dir=tmp_path, lang=lang)))
    store = TaskStore(tmp_path)
    def save(id, status, **kw):
        store.save(Task(id, kw.pop("repo", "u/demo"), id, "A short explanation", "codex",
                        "local", 1, status=status, **kw))
    save("active-job", "running")
    save("queued-job", "queued", priority="high")
    save("completed-job", "done", finished_at=1740000000, merged=True,
         pr_url="https://github.com/u/demo/pull/7", summary="Result <script>")
    save("foreign-job", "done", repo="u/other")
    for n in range(10):
        save(f"older-job-{n}", "no-changes", finished_at=1600000000+n)
    response = client.get("/p/u/demo")
    assert response.status_code == 200
    page = response.text
    assert '<progress value="1" max="3">' in page
    assert "foreign-job" not in page and 'href="/tasks/active-job"' in page
    assert 'href="/tasks/queued-job"' in page and ("en cola, 1" if lang == "es" else "queued, 1") in page
    # Finished work is its own tab, 10 per page, newest first.
    done = client.get("/p/u/demo?tab=done").text
    assert "completed-job" not in page and "completed-job" in done
    assert done.index("completed-job") < done.index("Older milestone") < done.index("older-job-9")
    assert done.index("Older milestone") < done.index("First release") < done.index("older-job-9")
    assert "older-job-3" in done and "older-job-2" not in done
    assert "older-job-2" in client.get("/p/u/demo?tab=done&page=2").text
    # Text from STATUS.md and agents is escaped; the only scripts are Baton's own.
    assert "Result &lt;script&gt;" in done and "Result <script>" not in done
    assert "Use <script> & check" not in page
    assert 'href="https://github.com/u/demo/pull/7"' in done
    notes = client.get("/p/u/demo?tab=notes").text
    assert '<details class="full-status">' in notes and "---\nname: Project" in notes
    label = "Lanzar siguiente tarea →" if lang == "es" else "Launch next task →"
    url = re.search(r'href="(/tasks/new\?[^"]+)">' + label + r'</a>', page)[1]
    form = client.get(html.unescape(url))
    assert form.status_code == 200
    assert '<option value="u/demo" selected>' in form.text
    assert 'value="Fix &#34;A&amp;B&#34;"' in form.text
    assert '>Use &lt;script&gt; &amp; check.\nSecond line.</textarea>' in form.text
    legacy_url = re.search(r'href="(/tasks/new\?[^"]*title=No\+detail[^"]*)"', page)[1]
    legacy_form = client.get(html.unescape(legacy_url))
    assert re.search(r'<textarea[^>]*>[^<]*No detail[^<]+</textarea>', legacy_form.text)
    assert len(store.all()) == 14


def test_hub_project_with_only_front_matter_next(tmp_path, monkeypatch):
    path = "projects/report/STATUS.md"
    project = Project("u/hub/report", "u/hub", path, "main", False, "", None,
                      parse("---\nname: Report\nstatus: active\nupdated: 2026-11-24\nnext: Export CSV\n---"))

    async def load(self, force=False):
        return [project], []

    monkeypatch.setattr(Hub, "load", load)
    monkeypatch.setattr(Hub, "open_pulls", lambda self, repo: asyncio.sleep(0, result=[]))
    client = TestClient(create_app(Settings(data_dir=tmp_path)))
    assert 'href="/p/u/hub/report"' in client.get("/").text
    page = client.get("/p/u/hub/report").text
    url = re.search(r'href="(/tasks/new\?[^"]+)">Launch next task →</a>', page)[1]
    form = client.get(html.unescape(url)).text
    assert '<option value="u/hub" selected>' in form and 'value="Export CSV"' in form
    assert path in form and "Export CSV" in form.split("<textarea", 1)[1]


@pytest.mark.parametrize("success", [True, False])
def test_pull_cache_and_errors(monkeypatch, success):
    calls = []
    def github(request):
        after = json.loads(request.content)["variables"]["after"]
        calls.append(after)
        if not success:
            return httpx.Response(403)
        state = "SUCCESS" if after else "PENDING"
        commit = {"commit": {"statusCheckRollup": {"state": state}}}
        page = {"nodes": [{"number": len(calls), "title": "PR", "commits": {"nodes": [commit]}}],
                "pageInfo": {"hasNextPage": not after, "endCursor": "next"}}
        return httpx.Response(200, json={"data": {"repository": {"pullRequests": page}}})
    hub = Hub(Settings(github_token="test"))
    monkeypatch.setattr(hub, "_client", lambda: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(github)))
    async def visit():
        one, two = await asyncio.gather(hub.open_pulls("u/demo"), hub.open_pulls("u/demo"))
        assert one == two
        assert [pr["checks"] for pr in one] == (["pending", "passed"] if success else [])
    asyncio.run(visit())
    assert calls == ([None, "next"] if success else [None])


def test_cached_project_survives_github_outage(tmp_path, monkeypatch):
    offline = False

    def github(request):
        if request.url.path == "/repos/u/demo":
            return httpx.Response(200, json={"name": "demo", "full_name": "u/demo",
                                             "html_url": "https://github.com/u/demo"})
        if offline:
            return httpx.Response(503)
        if request.url.path.endswith("/contents/"):
            return httpx.Response(200, json=[{"name": "STATUS.md", "type": "file"}])
        if request.url.path.endswith("/STATUS.md"):
            return httpx.Response(200, text="---\nname: Minimal\nstatus: active\nupdated: 2026-11-24\n---")
        assert request.url.path == "/graphql"
        pull = {"title": "Fix the build", "number": 1, "commits": {"nodes": []}}
        page = {"nodes": [pull], "pageInfo": {"hasNextPage": False}}
        return httpx.Response(200, json={"data": {"repository": {"pullRequests": page}}})

    monkeypatch.setattr(Hub, "_client", lambda self: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(github)))
    client = TestClient(create_app(Settings(repos=["u/demo"], data_dir=tmp_path,
                                            github_token="test", cache_seconds=0)))
    first = client.get("/p/u/demo")
    assert "Fix the build" in first.text and "No reported tests" in first.text
    offline = True
    second = client.get("/p/u/demo")
    assert second.status_code == 200 and "Minimal" in second.text
    assert "<section>" not in second.text and "<progress" not in second.text


@pytest.mark.parametrize("agent_task", [True, False])
def test_owner_actions(tmp_path, monkeypatch, agent_task):
    source = "---\nname: Hub\nstatus: active\nupdated: 2026-11-24\nnext: (Owner) Rate\n---\n"
    source += "## Siguiente\n- [ ] (Owner) Rate\n  First\n* [ ] (Owner) Rate\n  Second\n"
    if agent_task:
        source += "- [ ] Export CSV\n"
    source = source.replace("\n", "\r\n")
    project = Project("u/hub/sub", "u/hub", "projects/sub/STATUS.md", "release", False, "", None,
                      parse(source))
    writes = []

    async def load(self, force=False):
        return [project], []

    def github(request):
        assert request.method == "PUT" and request.url.path == "/repos/u/hub/contents/projects/sub/STATUS.md"
        writes.append(json.loads(request.content))
        return httpx.Response(200 if len(writes) == 1 else 409)

    monkeypatch.setattr(Hub, "load", load)
    monkeypatch.setattr(Hub, "_client", lambda self: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(github)))
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es")))
    for page in (client.get("/").text, client.get("/p/u/hub/sub").text):
        assert "Te toca" in page
        links = re.findall(r'href="(/tasks/new[^"]*)"', page)
        assert not any("Owner" in url for url in links)
        assert any("Export+CSV" in url for url in links) == agent_task
        assert ("Lanzar siguiente tarea" in page) == agent_task
    assert project.status.next_task == ("Export CSV" if agent_task else "")
    forms = re.findall(r'<form class="project-row owner-row".*?</form>', page, re.S)
    assert len(forms) == 2
    data = dict(re.findall(r'name="(\w+)" value="([^"]+)"', forms[1]))
    endpoint = "/p/u/hub/sub/done"
    assert client.post(endpoint, data={**data, "csrf": "wrong"}).status_code == 403
    assert client.post(endpoint, data=data, headers={"origin": "https://other.org"}).status_code == 403
    assert client.post(endpoint, data={**data, "line": 5}).status_code == 409
    assert client.post(endpoint, data={**data, "sha": "stale"}).status_code == 409
    assert not writes
    assert client.post(endpoint, data=data, follow_redirects=False).status_code == 303
    assert writes[0]["branch"] == "release" and writes[0]["sha"] == project.status_sha
    import datetime as dt
    today = dt.date.today().isoformat()
    written = base64.b64decode(writes[0]["content"]).decode()
    assert f"* [x] {today} (Owner) Rate\r\n" in written
    assert f"updated: {today}\r\n" in written
    assert "updated_by: human\r\n" in written
    assert not parse(written).problems
    assert client.post(endpoint, data=data).status_code == 409


def test_mark_agent_task_done(tmp_path, monkeypatch):
    import datetime as dt
    from baton.runner import Task, TaskStore
    source = ("---\nname: Hub\nstatus: active\nupdated: 2026-11-24\nnext: Future phase\n---\n"
              "## Next\n- [ ] Future phase\n  Nothing left.\n- [ ] (Owner) Rate it\n"
              "- [ ] Ranking: export CSV\n- [x] Old\n").replace("\n", "\r\n")
    project = Project("u/hub", "u/hub", "STATUS.md", "main", False, "", None, parse(source))
    writes = []

    async def load(self, force=False):
        return [project], []

    def github(request):
        writes.append(json.loads(request.content))
        return httpx.Response(200)

    monkeypatch.setattr(Hub, "load", load)
    monkeypatch.setattr(Hub, "_client", lambda self: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(github)))
    TaskStore(tmp_path).save(Task(id="t1", repo="u/hub", title="Future phase", details="", agent="codex",
                                  where="local", created_at=1.0, status="no-changes", finished_at=2.0))
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es")))

    # The finished task offers to tick its STATUS.md item.
    page = client.get("/tasks/t1").text
    assert "sigue sin marcar en el STATUS.md de Hub" in page and "Marcar como hecha" in page
    form = re.search(r'<form method="post" action="(/p/u/hub/check)".*?</form>', page, re.S)
    data = dict(re.findall(r'name="(\w+)" value="([^"]+)"', form[0]))
    assert data["back"] == "/tasks/t1"
    assert client.post(form[1], data={**data, "csrf": "wrong"}).status_code == 403
    owner_line = next(i.line for i in project.status.sections.owner_actions)
    assert client.post(form[1], data={**data, "line": owner_line}).status_code == 409   # not an agent task
    assert client.post(form[1], data={**data, "sha": "stale"}).status_code == 409
    assert not writes
    response = client.post(form[1], data=data, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/tasks/t1"
    written = base64.b64decode(writes[0]["content"]).decode()
    today = dt.date.today().isoformat()
    assert f"- [x] {today} Future phase\r\n  Nothing left.\r\n" in written
    assert 'next: "Ranking: export CSV"\r\n' in written           # quoted: the title has ": "
    assert f"updated: {today}\r\n" in written
    assert "updated_by: human\r\n" in written
    assert written.count("[ ]") == 2 and "Marcar" not in writes[0]["message"]
    parsed_written = parse(written)
    assert parsed_written.next == "Ranking: export CSV" and not parsed_written.problems
    assert parsed_written.updated.isoformat() == today and parsed_written.updated_by == "human"

    # The project page offers it too, for the featured task and the rest.
    page = client.get("/p/u/hub").text
    assert page.count('action="/p/u/hub/check"') == 2


def test_task_done_in_another_repository(tmp_path, monkeypatch):
    from baton.project_detail import target_repo
    assert target_repo("Build it.\nRepo: owner/hub\nCheck.") == ("owner/hub", "Build it.\nCheck.")
    assert target_repo("no repo here") == ("", "no repo here")

    baton_source = ("---\nname: Baton\nstatus: active\nupdated: 2026-11-24\n---\n## Next\n"
                    "- [ ] Daily index workflow\n  Install it.\n  Repo: u/hub\n  Recommended: codex\n")
    baton = Project("u/baton", "u/baton", "STATUS.md", "main", False, "", None, parse(baton_source))
    hub = Project("u/hub", "u/hub", "STATUS.md", "main", False, "", None,
                  parse("---\nname: Hub\nstatus: active\nupdated: 2026-11-24\n---\n"))
    item = baton.status.sections.upcoming[0]
    assert item.repo == "u/hub" and item.detail == "Install it." and item.recommended["agent"] == "codex"

    async def load(self, force=False):
        return [baton, hub], []

    monkeypatch.setattr(Hub, "load", load)
    TaskStore(tmp_path).save(Task(id="t1", repo="u/hub", title="Daily index workflow", details="",
                                  agent="codex", where="local", created_at=1.0, status="done", finished_at=2.0))
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es")))

    page = client.get("/p/u/baton").text
    assert "↗ u/hub" in page
    url = html.unescape(re.search(r'href="(/tasks/new\?[^"]+)">Lanzar siguiente tarea', page)[1])
    assert "repo=u%2Fhub" in url and "De+la+lista+Siguiente+de+Baton" in url
    form = client.get(url).text
    assert '<option value="u/hub" selected>' in form and "no tiene en su lista" not in form
    assert "no tiene en su lista" in client.get("/tasks/new?repo=u/missing").text

    # Finished in the hub, the task page offers to tick it in Baton's STATUS.md.
    page = client.get("/tasks/t1").text
    assert "sigue sin marcar en el STATUS.md de Baton" in page and 'action="/p/u/baton/check"' in page
