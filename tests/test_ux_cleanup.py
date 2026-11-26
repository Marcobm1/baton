"""UX-1 cleanup regression tests: owner tag display, mobile alert badge, decluttered templates."""

from __future__ import annotations

from fastapi.testclient import TestClient

from baton import github
from baton.config import Settings
from baton.i18n import strings
from baton.project_detail import ChecklistItem
from baton.runner import Task, TaskStore
from baton.status import parse
from baton.web import create_app


def test_owner_action_display_title_works_for_any_tag_length():
    for tag in ("(Owner)", "(Marco)", "(Administrator)", "[Human]", "OWNER:"):
        item = ChecklistItem(f"{tag} Verify deployment", False, "", owner_tag=tag)
        assert item.owner_action is True
        assert item.display_title == "Verify deployment"

    regular = ChecklistItem("Ship release", False, "", owner_tag="(Administrator)")
    assert regular.owner_action is False
    assert regular.display_title == "Ship release"


def test_project_page_strips_long_custom_owner_tag(tmp_path, monkeypatch):
    source = (
        "---\nname: Demo\nstatus: active\nupdated: 2026-11-26\n---\n"
        "## Next\n- [ ] (Administrator) Check mobile layout\n"
    )
    proj = github.Project(
        "u/demo", "u/demo", "STATUS.md", "main", False, "", None,
        parse(source, owner_tag="(Administrator)"),
    )

    async def fake_load(self, force=False):
        return [proj], []

    monkeypatch.setattr(github.Hub, "load", fake_load)
    client = TestClient(create_app(Settings(data_dir=tmp_path, owner_tag="(Administrator)")))
    html = client.get("/p/u/demo").text
    assert "<strong>Check mobile layout</strong>" in html
    assert "<strong>istrator)" not in html


def test_mobile_tab_bar_shows_alert_badge_and_self_update_lives_on_machines(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    app = create_app(Settings(data_dir=tmp_path, lang="es", self_update=False))
    app.state.ctx.alerts.evaluate(
        [{"id": "vps", "name": "VPS", "local": False, "status": "offline", "age_s": 300.0, "metrics": {}}]
    )
    client = TestClient(app)

    quotas_html = client.get("/quotas").text
    assert 'id="self-update"' not in quotas_html
    assert strings("es")["quotas_help"] not in quotas_html
    assert strings("es")["tokens_help"] in quotas_html  # kept as tooltip, not prose block
    assert '<span class="tab-badge" data-alert-badge>1</span>' in quotas_html

    machines_html = client.get("/machines").text
    assert 'id="self-update"' in machines_html
    assert strings("es")["mach_sub"] not in machines_html
    assert strings("es")["mach_none_remote"] not in machines_html


def test_task_page_does_not_duplicate_summary_or_print_empty_filler(tmp_path):
    store = TaskStore(tmp_path)
    store.save(Task(
        id="t-clean", repo="u/demo", title="Polish UI", details="",
        agent="codex", where="local", created_at=1.0, finished_at=2.0,
        status="done", merged=True, pr_url="https://github.com/u/demo/pull/9",
        summary="Summary: Removed duplicate prose across pages.\nChecks: pytest -q (250 passed)",
    ))
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es", self_update=False)))
    html = client.get("/tasks/t-clean").text
    assert html.count("Removed duplicate prose across pages.") == 1
    assert strings("es")["no_summary"] not in html
    assert strings("es")["no_checks"] not in html
    assert 'class="task-actions"' in html
    assert "https://github.com/u/demo/pull/9" in html
