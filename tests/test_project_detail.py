import asyncio
import html
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from baton.config import Settings
from baton.github import Hub
from baton.project_detail import sections
from baton.runner import AGENTS, Runner, Task, TaskStore
from baton.web import create_app


BODY = """---
name: Project
status: active
updated: 2026-11-24
---
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
    assert page.index("completed-job") < page.index("Older milestone") < page.index("older-job-9")
    assert page.index("older-job-2") < page.index("(2)</summary>") < page.index("older-job-1")
    # Text from STATUS.md and agents is escaped; the only scripts are Baton's own.
    assert "Result &lt;script&gt;" in page and "Result <script>" not in page
    assert "Use <script> & check" not in page
    assert 'href="https://github.com/u/demo/pull/7"' in page
    assert '<details class="full-status">' in page and "---\nname: Project" in page
    url = re.search(r'href="(/tasks/new\?[^"]*details=[^"]*)"', page)[1]
    form = client.get(html.unescape(url))
    assert form.status_code == 200
    assert '<option value="u/demo" selected>' in form.text
    assert 'value="Fix &#34;A&amp;B&#34;"' in form.text
    assert '>Use &lt;script&gt; &amp; check.\nSecond line.</textarea>' in form.text
    assert len(store.all()) == 14


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
