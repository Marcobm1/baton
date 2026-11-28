"""Tests for the per-project interactive task map (parser, validator, routes, and STATUS.md integration)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from baton import task_map
from baton.config import Settings
from baton.github import Hub, Project
from baton.project_detail import sections
from baton.status import parse as parse_status
from baton.task_store import Task
from baton.web import create_app

LEGACY_STATUS = """---
name: Demo
status: active
priority: high
summary: Sample project.
next: Implement handlers
updated: 2026-11-20
updated_by: human
---

## Done recently
- 2026-11-19: initial setup

## Next
- [x] 2026-11-20 Define endpoints
  - [x] OpenAPI draft
  - [x] Naming review
- [ ] Implement handlers
  Add REST routes with validation.
  Recommended: claude-code, opus, medium
  - [x] GET /tasks
  - [ ] POST /tasks
- [ ] (Marco) Approve release

## Blockers
- Waiting on review

## Decisions
- Keep STATUS.md as single source of truth.

## Log
- 2026-11-20 (human): started.
"""

MAP_STATUS = """---
name: Demo
status: active
priority: high
summary: Sample project.
next: Implement handlers
updated: 2026-11-20
updated_by: human
---

## Done recently
- 2026-11-19: initial setup

## Next

### API (AP)
- [x] [AP-01] 2026-11-20 Define endpoints
  - [x] OpenAPI draft
- [ ] [AP-02] Implement handlers (after: AP-01)
  Add REST routes.
  Recommended: claude-code, opus, medium
  - [x] GET /tasks
  - [ ] POST /tasks

### Release (RL)
- [ ] [RL-01] (Marco) Approve release (after: AP-02)

## Blockers
- None

## Log
- 2026-11-20 (human): enabled map.
"""


def test_parse_legacy_status_preview_and_progress_excludes_subtasks():
    parsed = task_map.parse_status_map(LEGACY_STATUS, owner_tag="Marco")
    assert parsed.enabled is False
    assert [(g.id, g.name) for g in parsed.groups] == [("GN", "General")]
    assert [t.id for t in parsed.tasks] == ["GN-01", "GN-02", "GN-03"]
    assert parsed.tasks[0].done is True and parsed.tasks[0].date == "2026-11-20"
    assert len(parsed.tasks[0].subs) == 2
    assert parsed.tasks[1].recommended == {"agent": "claude-code", "model": "opus", "effort": "medium"}
    assert [(s.text, s.done) for s in parsed.tasks[1].subs] == [("GET /tasks", True), ("POST /tasks", False)]
    assert parsed.tasks[2].owner is True and parsed.tasks[2].title == "Approve release"

    # Top-level progress counts only the 3 top-level tasks (1 done), NOT the 4 indented subtasks.
    st = parse_status(LEGACY_STATUS, owner_tag="Marco")
    assert (st.tasks_done, st.tasks_total, st.progress) == (1, 3, 33)


def test_parse_map_status_and_sections_compatibility():
    parsed = task_map.parse_status_map(MAP_STATUS, owner_tag="Marco")
    assert parsed.enabled is True
    assert [(g.id, g.name) for g in parsed.groups] == [("AP", "API"), ("RL", "Release")]
    assert [(t.id, t.group, t.deps, t.owner, t.done) for t in parsed.tasks] == [
        ("AP-01", "AP", [], False, True),
        ("AP-02", "AP", ["AP-01"], False, False),
        ("RL-01", "RL", ["AP-02"], True, False),
    ]
    assert parsed.problems == []

    # project_detail.sections() must still read all items across ### group headings.
    body = MAP_STATUS.split("---", 2)[-1]
    sec = sections(body, owner_tag="Marco")
    assert [(x.task_id, x.title, x.deps, x.owner_action, x.checked) for x in sec.checklist] == [
        ("AP-01", "2026-11-20 Define endpoints", [], False, True),
        ("AP-02", "Implement handlers", ["AP-01"], False, False),
        ("RL-01", "(Marco) Approve release", ["AP-02"], True, False),
    ]
    assert [x.display_title for x in sec.owner_actions] == ["Approve release"]
    assert [x.title for x in sec.upcoming] == ["Implement handlers"]
    assert sec.log == ["2026-11-20 (human): enabled map."]


def test_round_trip_preserves_other_sections_spanish_heading_and_code_fences():
    src = (
        "---\nname: Prueba\nstatus: active\nnext: Viejo\nupdated: 2026-11-01\n---\n\n"
        "## Hecho recientemente\n- Algo\n\n"
        "```markdown\n## Next\n- [ ] Fake inside code block\n```\n\n"
        "## Siguiente\n"
        "- [ ] Primera tarea\n\n"
        "## Bloqueos\n- Ninguno\n"
    )
    parsed = task_map.parse_status_map(src, owner_tag="Marco")
    assert parsed.enabled is False
    assert [t.title for t in parsed.tasks] == ["Primera tarea"]

    out = task_map.apply_map_to_source(
        src, parsed.groups, parsed.tasks, owner_tag="Marco", today="2026-11-27"
    )
    assert "## Siguiente\n" in out
    assert "```markdown\n## Next\n- [ ] Fake inside code block\n```" in out
    assert "## Bloqueos\n- Ninguno\n" in out
    assert "updated: 2026-11-27" in out
    assert "updated_by: human" in out
    assert "next: Primera tarea" in out

    reparsed = task_map.parse_status_map(out, owner_tag="Marco")
    assert reparsed.enabled is True
    assert [(t.id, t.title) for t in reparsed.tasks] == [("GN-01", "Primera tarea")]


def test_crlf_and_frontmatter_next_picks_first_ready_task():
    crlf_src = MAP_STATUS.replace("\n", "\r\n")
    parsed = task_map.parse_status_map(crlf_src, owner_tag="Marco")
    # Put blocked task first, ready task second: frontmatter `next:` should pick the ready one.
    reordered = [parsed.tasks[0], parsed.tasks[2], parsed.tasks[1]]
    out = task_map.apply_map_to_source(
        crlf_src, parsed.groups, reordered, owner_tag="Marco", today="2026-11-27"
    )
    assert "\r\n" in out
    assert "next: Implement handlers\r\n" in out


def test_validate_map_payload_rejects_cycles_and_invalid_inputs():
    groups = [{"id": "AP", "name": "API"}]
    with pytest.raises(ValueError, match="cycle"):
        task_map.validate_map_payload(groups, [
            {"id": "AP-01", "group": "AP", "title": "A", "deps": ["AP-02"]},
            {"id": "AP-02", "group": "AP", "title": "B", "deps": ["AP-01"]},
        ])
    with pytest.raises(ValueError, match="itself"):
        task_map.validate_map_payload(groups, [
            {"id": "AP-01", "group": "AP", "title": "A", "deps": ["AP-01"]},
        ])
    with pytest.raises(ValueError, match="unknown task"):
        task_map.validate_map_payload(groups, [
            {"id": "AP-01", "group": "AP", "title": "A", "deps": ["AP-99"]},
        ])
    with pytest.raises(ValueError, match="unknown group"):
        task_map.validate_map_payload(groups, [
            {"id": "AP-01", "group": "NOPE", "title": "A", "deps": []},
        ])
    with pytest.raises(ValueError, match="Invalid group prefix"):
        task_map.validate_map_payload([{"id": "1BAD", "name": "Bad"}], [])


def test_hand_edited_cycle_does_not_crash_parse():
    bad = (
        "---\nname: Bad\nstatus: active\nupdated: 2026-11-27\n---\n\n"
        "## Next\n### A (A)\n"
        "- [ ] [A-01] First (after: A-02)\n"
        "- [ ] [A-02] Second (after: A-01)\n"
    )
    parsed = task_map.parse_status_map(bad)
    assert any("cycle" in p.lower() for p in parsed.problems)
    assert task_map._find_cycle(parsed.tasks) == []


def test_match_running_tasks():
    parsed = task_map.parse_status_map(MAP_STATUS, owner_tag="Marco")
    t_queued = Task(
        id="q1", repo="me/demo", title="[AP-02] Implement handlers",
        details="", agent="codex", where="local", created_at=1.0, status="queued",
    )
    t_running = Task(
        id="r1", repo="me/demo", title="[AP-02] Implement handlers",
        details="", agent="codex", where="local", created_at=2.0, status="running",
    )
    t_by_title = Task(
        id="r2", repo="me/demo", title="Approve release",
        details="", agent="codex", where="local", created_at=3.0, status="queued",
    )
    matched = task_map.match_running_tasks(parsed.tasks, [t_queued, t_running, t_by_title])
    assert matched["AP-02"]["id"] == "r1" and matched["AP-02"]["status"] == "running"
    assert matched["RL-01"]["id"] == "r2" and matched["RL-01"]["status"] == "queued"


def test_transitive_edges_detection():
    # A -> B -> C and redundant A -> C; plus diamond A -> D -> E, A -> E (redundant), and unrelated X -> Y.
    tasks = [
        task_map.MapTask(id="AP-01", group="AP", title="A", deps=[]),
        task_map.MapTask(id="AP-02", group="AP", title="B", deps=["AP-01"]),
        task_map.MapTask(id="AP-03", group="AP", title="C", deps=["AP-01", "AP-02"]),
        task_map.MapTask(id="AP-04", group="AP", title="D", deps=["AP-01"]),
        task_map.MapTask(id="AP-05", group="AP", title="E", deps=["AP-01", "AP-03", "AP-04"]),
        task_map.MapTask(id="UI-01", group="UI", title="X", deps=[]),
        task_map.MapTask(id="UI-02", group="UI", title="Y", deps=["UI-01"]),
    ]
    redundant = set(task_map.transitive_edges(tasks))
    assert redundant == {("AP-01", "AP-03"), ("AP-01", "AP-05")}
    # Essential edges are never flagged.
    assert ("AP-01", "AP-02") not in redundant
    assert ("AP-02", "AP-03") not in redundant
    assert ("AP-03", "AP-05") not in redundant
    assert ("AP-04", "AP-05") not in redundant
    assert ("UI-01", "UI-02") not in redundant


def _make_project(source: str, owner_tag: str = "Marco") -> Project:
    return Project(
        id="me/demo",
        repo="me/demo",
        path="STATUS.md",
        branch="main",
        private=False,
        html_url="https://github.com/me/demo",
        pushed_at=None,
        status=parse_status(source, fallback_name="Demo", owner_tag=owner_tag),
    )


def test_map_tab_and_enable_and_save_endpoints(tmp_path, monkeypatch):
    current_source = [LEGACY_STATUS]
    writes: list[dict] = []
    project = _make_project(current_source[0])

    async def fake_load(self, force=False):
        if force:
            project.status = parse_status(current_source[0], fallback_name="Demo", owner_tag="Marco")
        return [project], []

    def fake_github(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == "/repos/me/demo/contents/STATUS.md":
            body = json.loads(request.content)
            if body["sha"] != project.status_sha:
                return httpx.Response(409, json={"message": "sha mismatch"})
            writes.append(body)
            current_source[0] = base64.b64decode(body["content"]).decode("utf-8")
            return httpx.Response(200, json={"content": {"sha": "ok"}})
        return httpx.Response(404)

    monkeypatch.setattr(Hub, "load", fake_load)
    monkeypatch.setattr(Hub, "_client", lambda self: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(fake_github)
    ))

    app = create_app(Settings(
        data_dir=tmp_path, github_token="tok", lang="es", owner_tag="Marco", self_update=False
    ))
    csrf = app.state.ctx.csrf_token
    client = TestClient(app)

    # 1. Page renders the Map tab without meta-refresh, with embedded JSON and assets.
    page = client.get("/p/me/demo?tab=map")
    assert page.status_code == 200
    assert 'http-equiv="refresh"' not in page.text
    assert "/static/task-map.css" in page.text
    assert "/static/task-map.js" in page.text
    assert 'id="task-map-data"' in page.text

    # 2. GET API returns preview mode before enable.
    r = client.get("/api/projects/me/demo/map")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False
    assert data["can_write"] is True
    assert [t["id"] for t in data["tasks"]] == ["GN-01", "GN-02", "GN-03"]

    # 3. CSRF is strictly enforced.
    assert client.post("/api/projects/me/demo/map/enable", json={"sha": data["sha"]}).status_code == 403

    # 4. Enable map writes one commit converting legacy STATUS.md.
    r_en = client.post(
        "/api/projects/me/demo/map/enable",
        headers={"X-Baton-CSRF": csrf},
        json={"sha": data["sha"]},
    )
    assert r_en.status_code == 200
    en_data = r_en.json()
    assert en_data["enabled"] is True
    assert en_data["sha"] != data["sha"]
    assert "### General (GN)" in current_source[0]
    assert "- [ ] [GN-02] Implement handlers" in current_source[0]
    assert "## Blockers\n- Waiting on review" in current_source[0]

    # 5. Save an edit (link GN-01 -> GN-02 and GN-01/GN-02 -> GN-03) immediately.
    new_groups = en_data["groups"] + [{"id": "RL", "name": "Release"}]
    new_tasks = list(en_data["tasks"])
    new_tasks[1]["deps"] = ["GN-01"]
    new_tasks[2]["group"] = "RL"
    new_tasks[2]["deps"] = ["GN-01", "GN-02"]
    r_save = client.post(
        "/api/projects/me/demo/map",
        headers={"X-Baton-CSRF": csrf},
        json={
            "sha": en_data["sha"],
            "message": "Link GN-01 -> GN-02",
            "groups": new_groups,
            "tasks": new_tasks,
        },
    )
    assert r_save.status_code == 200
    saved = r_save.json()
    assert "(after: GN-01)" in current_source[0]
    assert "### Release (RL)" in current_source[0]
    assert saved["transitive_edges"] == [["GN-01", "GN-03"]]
    assert len(writes) == 2

    # 6. Cycle submission is rejected with 400 and does not write to GitHub.
    bad_tasks = json.loads(json.dumps(saved["tasks"]))
    bad_tasks[0]["deps"] = ["GN-02"]  # GN-01 -> GN-02 -> GN-01 cycle
    r_cyc = client.post(
        "/api/projects/me/demo/map",
        headers={"X-Baton-CSRF": csrf},
        json={"sha": saved["sha"], "groups": saved["groups"], "tasks": bad_tasks},
    )
    assert r_cyc.status_code == 400
    assert len(writes) == 2

    # 7. Stale SHA returns 409 with fresh map payload so the browser can recover cleanly.
    r_409 = client.post(
        "/api/projects/me/demo/map",
        headers={"X-Baton-CSRF": csrf},
        json={"sha": "stale-sha", "groups": saved["groups"], "tasks": saved["tasks"]},
    )
    assert r_409.status_code == 409
    conflict = r_409.json()
    assert conflict["conflict"] is True
    assert conflict["map"]["sha"] == saved["sha"]


def test_map_api_requires_auth_when_access_key_is_set(tmp_path, monkeypatch):
    project = _make_project(MAP_STATUS)

    async def fake_load(self, force=False):
        return [project], []

    monkeypatch.setattr(Hub, "load", fake_load)
    app = create_app(Settings(data_dir=tmp_path, access_key="secret", self_update=False))
    client = TestClient(app)
    assert client.get("/api/projects/me/demo/map").status_code == 401
    assert client.post("/api/projects/me/demo/map", json={}).status_code == 401


def test_complete_item_keeps_id_before_date(tmp_path, monkeypatch):
    project = _make_project(MAP_STATUS)
    written: list[str] = []

    async def fake_load(self, force=False):
        return [project], []

    def fake_github(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            body = json.loads(request.content)
            written.append(base64.b64decode(body["content"]).decode("utf-8"))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    monkeypatch.setattr(Hub, "load", fake_load)
    monkeypatch.setattr(Hub, "_client", lambda self: httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(fake_github)
    ))
    app = create_app(Settings(data_dir=tmp_path, github_token="tok", owner_tag="Marco", self_update=False))
    csrf = app.state.ctx.csrf_token
    client = TestClient(app)
    item = project.status.sections.upcoming[0]
    res = client.post(
        "/p/me/demo/check",
        data={"csrf": csrf, "line": item.line, "sha": project.status_sha},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert written
    # Canonical `- [x] [AP-02] YYYY-MM-DD Implement handlers (after: AP-01)`
    assert "- [x] [AP-02] " in written[0]
    reparsed = task_map.parse_status_map(written[0], owner_tag="Marco")
    ap02 = next(t for t in reparsed.tasks if t.id == "AP-02")
    assert ap02.done is True and ap02.date is not None
