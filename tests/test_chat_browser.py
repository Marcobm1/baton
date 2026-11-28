"""Opt-in rendered contrast and screenshot checks for /chat."""
import os
import socket
import sys
from pathlib import Path
from threading import Thread

import pytest
import uvicorn

from baton import web
from baton.config import Settings
from test_gemini import CATALOG, FAKE_ACP

pytestmark = pytest.mark.skipif(
    os.environ.get("BATON_VISUAL_SMOKE") != "1", reason="Set BATON_VISUAL_SMOKE=1 to run chat visual checks"
)

ARTIFACTS = Path(__file__).resolve().parents[1] / ".baton-artifacts"
BUTTONS = 'button, [role="button"], a.btn, input:is([type="button"], [type="submit"], [type="reset"])'

# Sample computed CSS colors, including color-mix(), in sRGB. Compare the
# foreground and background after compositing translucent fills and opacity.
BUTTON_CONTRAST = """async buttons => {
    const visible = buttons.filter(button => button.checkVisibility({
        opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true
    }));
    const inView = await new Promise(resolve => {
        if (!visible.length) {
            resolve([]);
            return;
        }
        const observer = new IntersectionObserver(entries => {
            observer.disconnect();
            resolve(entries.filter(entry => entry.intersectionRatio > 0).map(entry => entry.target));
        });
        visible.forEach(button => observer.observe(button));
    });
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext('2d', {willReadFrequently: true});

    function rgba(color) {
        ctx.clearRect(0, 0, 1, 1);
        ctx.fillStyle = color;
        ctx.fillRect(0, 0, 1, 1);
        return Array.from(ctx.getImageData(0, 0, 1, 1).data, channel => channel / 255);
    }
    function over(upper, lower) {
        const below = lower[3] * (1 - upper[3]);
        const alpha = upper[3] + below;
        if (!alpha) return [0, 0, 0, 0];
        return [0, 1, 2].map(i => (upper[i] * upper[3] + lower[i] * below) / alpha).concat(alpha);
    }
    function luminance(rgb) {
        const [r, g, b] = rgb.map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    }

    return inView.map(button => {
        let fg = rgba(getComputedStyle(button).color);
        let bg = [0, 0, 0, 0];
        for (let node = button; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            const fill = rgba(style.backgroundColor);
            fg = over(fg, fill);
            bg = over(bg, fill);
            fg[3] *= Number(style.opacity);
            bg[3] *= Number(style.opacity);
        }
        const canvasColor = [1, 1, 1, 1];
        const front = luminance(over(fg, canvasColor));
        const back = luminance(over(bg, canvasColor));
        const label = button.getAttribute('aria-label') || button.title || button.textContent.trim() || button.value;
        const selector = [button.localName, ...button.classList].join('.');
        const ratio = (Math.max(front, back) + 0.05) / (Math.min(front, back) + 0.05);
        return {label, selector, ratio};
    });
}"""


@pytest.fixture(scope="module")
def chromium():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(channel=os.environ.get("PLAYWRIGHT_CHANNEL") or None)
        except pw.Error as error:
            message = str(error)
            if "Executable doesn't exist at" in message or (
                "Chromium distribution" in message and "is not found" in message
            ):
                pytest.skip("Install Playwright Chromium or set PLAYWRIGHT_CHANNEL to an installed browser")
            raise
        try:
            yield browser, pw.expect
        finally:
            browser.close()


@pytest.fixture
def chat_server(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "find_agy", lambda: "fake-agy")
    monkeypatch.setattr(web, "catalog", lambda agent: CATALOG if agent == "gemini" else {})
    app = web.create_app(Settings(data_dir=tmp_path / "data", self_update=False,
                                  chat_scratch=str(tmp_path / "scratch")))
    ctx = app.state.ctx
    monkeypatch.setattr(ctx.models, "overview", lambda lang: {"models": []})
    monkeypatch.setattr(ctx.agent_chats, "sandbox_ready", lambda agent: False)
    monkeypatch.setattr(ctx.agent_chats, "gemini_command", lambda: [sys.executable, str(FAKE_ACP), "--gemini"])

    # Seed a saved turn to exercise the same Markdown and diff renderers as production.
    # Relative file names keep the screenshots independent of the host's paths.
    history = ctx.chat_history
    thread = history.create_thread(agent="gemini", model="gemini-3.8-flash", effort="low",
                                   folder_key="scratch", title="Update the preview settings")
    thread_id = thread["id"]
    history.append_message(thread_id, "user", "Can you update the preview timeout?")
    history.append_message(
        thread_id, "assistant", "The preview now waits **10 seconds**.\n\n```toml\ntimeout = 10\n```",
        tools=[{"id": "edit-settings", "kind": "edit", "title": "Edit settings.toml", "status": "completed",
                "diffs": [{"path": "settings.toml", "old": "timeout = 5\n", "new": "timeout = 10\n"}]}],
    )

    # A real loopback server keeps EventSource streaming while the fake agent
    # waits for permission. Skip the lifespan to keep unrelated workers offline.
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False))
    with socket.create_server(("127.0.0.1", 0)) as listener:
        port = listener.getsockname()[1]
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{port}/chat?agent=gemini", thread_id
        finally:
            ctx.agent_chats.close_all()
            server.should_exit = True
            worker.join(timeout=10)
            assert not worker.is_alive(), "Visual smoke test server did not shut down"


def contrast_failures(page, state):
    buttons = page.locator(BUTTONS).evaluate_all(BUTTON_CONTRAST)
    assert buttons, f"No visible buttons in the {state} view"
    return [f"{state}: {b['label']!r} ({b['selector']}): {b['ratio']:.2f}:1"
            for b in buttons if b["ratio"] < 3]


@pytest.mark.parametrize("layout,width,height", [("desktop", 1280, 900), ("mobile", 390, 844)])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_chat_visual_smoke(chromium, chat_server, layout, width, height, theme):
    browser, expect = chromium
    url, thread_id = chat_server
    mobile = layout == "mobile"
    context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile,
                                  has_touch=mobile, color_scheme=theme, reduced_motion="reduce")
    try:
        page = context.new_page()
        assert page.goto(url).ok
        expect(page.locator("[data-suggest]").first).to_be_visible()
        failures = contrast_failures(page, "empty")

        assert page.goto(f"{url}&thread={thread_id}").ok
        expect(page.locator(".code-copy")).to_be_visible()
        expect(page.locator(".ag-diff-row.add")).to_contain_text("timeout = 10")
        failures += contrast_failures(page, "history")

        composer = page.locator("[data-text]")
        composer.fill("run echo Preview ready")
        composer.press("Enter")
        approval = page.locator(".ag-perm-actions")
        expect(approval.get_by_role("button", name="Allow")).to_be_visible()
        expect(approval.get_by_role("button", name="Deny")).to_be_visible()
        approval.scroll_into_view_if_needed()
        expect(approval).to_be_in_viewport(ratio=1)
        failures += contrast_failures(page, "approval")

        ARTIFACTS.mkdir(exist_ok=True)
        page.screenshot(path=ARTIFACTS / f"chat-{layout}-{theme}.png", animations="disabled")
        assert not failures, f"{layout}/{theme} buttons below 3:1 contrast:\n" + "\n".join(failures)
    finally:
        context.close()
