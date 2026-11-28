"""Optional task map smoke test. Install Playwright and Chromium or set
PLAYWRIGHT_CHANNEL to an installed Chromium channel (e.g. msedge).
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton.config import Settings
from baton.github import Hub
from baton.web import create_app
from test_task_map import MAP_STATUS, _make_project


def test_graph_and_board(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    project = _make_project(MAP_STATUS)

    async def projects(self, force=False):
        return [project], []

    monkeypatch.setattr(Hub, "load", projects)
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="en", owner_tag="(Marco)", self_update=False)))
    ctx = client.app.state.ctx
    monkeypatch.setattr(ctx.runner, "agents_available", lambda: {"claude-code": ""})
    monkeypatch.setattr(ctx.runner.repos, "get", lambda repo: tmp_path)
    monkeypatch.setattr(ctx, "catalog_fn", lambda agent: {"opus": {"efforts": ["medium"]}})
    # Serve the real read-only template and assets without starting background
    # workers or connecting to GitHub. SSE has separate route coverage.
    def serve(route):
        res = client.request(route.request.method, route.request.url, content=route.request.post_data,
                             headers=route.request.headers)
        route.fulfill(status=res.status_code, content_type=res.headers["content-type"], body=res.content)

    with pw.sync_playwright() as driver:
        channel = os.environ.get("PLAYWRIGHT_CHANNEL")
        if not channel and not Path(driver.chromium.executable_path).exists():
            pytest.skip("Install Playwright Chromium or set PLAYWRIGHT_CHANNEL")
        browser = driver.chromium.launch(channel=channel)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.add_init_script("window.EventSource = undefined")
        page.route("http://testserver/**", serve)
        page.goto("http://testserver/p/me/demo?tab=map#AP-01")
        pw.expect(page.locator("#tm-graph")).to_be_visible()
        page.keyboard.press("ArrowRight")
        assert page.url.endswith("#AP-02")
        page.keyboard.press("Enter")
        pw.expect(page.locator("#tm-p-close")).to_be_focused()
        page.keyboard.press("Escape")
        page.keyboard.press("?")
        pw.expect(page.locator("#tm-help")).to_be_visible()
        page.keyboard.press("Escape")

        page.locator('#tm-graph .tm-card[data-id="RL-01"]').click(button="right")
        page.get_by_text("Queue up to here", exact=True).filter(visible=True).click()
        dialog = page.locator("#tm-chain")
        pw.expect(dialog).to_be_visible()
        pw.expect(dialog.locator("li")).to_have_count(1)
        pw.expect(dialog.locator("li")).to_contain_text("[AP-02] Implement handlers")
        pw.expect(dialog.locator("p")).to_contain_text("RL-01 Approve release")
        page.screenshot(path=tmp_path / "chain-preview.png")
        page.keyboard.press("Escape")
        pw.expect(dialog).to_be_hidden()
        assert ctx.store.all() == []

        page.locator('[data-view="board"]').click()
        board = page.locator("#tm-board")
        columns = board.locator(".tm-column")
        pw.expect(columns).to_have_count(5)
        for state, tid in (("ready", "AP-02"), ("blocked", "RL-01"), ("done", "AP-01")):
            pw.expect(board.locator(f'[data-state="{state}"] [data-id="{tid}"]')).to_be_visible()
        assert len(set(columns.evaluate_all("els => els.map(e => e.offsetTop)"))) == 1
        page.locator("#tm-md").click()
        assert page.locator("#tm-md-text").evaluate("el => el.readOnly")
        pw.expect(page.locator("#tm-md-apply")).to_be_hidden()
        page.keyboard.press("Escape")

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        pw.expect(board).to_be_visible()
        assert len(set(columns.evaluate_all("els => els.map(e => e.offsetLeft)"))) == 1
        assert board.evaluate("el => el.scrollWidth === el.clientWidth")
        board.locator('[data-id="RL-01"]').click()
        pw.expect(page.locator("#tm-p-title")).to_have_value("Approve release")
        page.set_viewport_size({"width": 640, "height": 844})
        pw.expect(page.locator("#tm-graph")).to_be_visible()
        browser.close()
