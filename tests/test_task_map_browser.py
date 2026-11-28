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
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="en", self_update=False)))
    # Serve the real read-only template and assets without starting background
    # workers or connecting to GitHub. SSE has separate route coverage.
    def serve(route):
        res = client.get(route.request.url)
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
