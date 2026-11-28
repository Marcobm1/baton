"""Design system guards: theme switch on every page, colours only as tokens,
both dark blocks identical, and WCAG AA contrast for text colours."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton import github
from baton.config import Settings
from baton.web import create_app

STATIC = Path(__file__).resolve().parents[1] / "baton" / "static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _block(css: str, selector: str) -> str:
    start = css.index(selector)
    return css[start:css.index("}", start)]


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", block))


LIGHT = _tokens(_block(CSS, ":root {\n  color-scheme: light;"))
DARK = _tokens(_block(CSS, ':root[data-theme="dark"]'))
DARK_AUTO = _tokens(_block(CSS, ':root:not([data-theme="light"])'))


@pytest.mark.parametrize("lang", ["en", "es"])
def test_theme_switch_on_every_page(tmp_path, monkeypatch, lang):
    async def no_projects(self, force=False):
        return [], []
    monkeypatch.setattr(github.Hub, "load", no_projects)
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang=lang, self_update=False)))
    for path in ("/", "/tasks", "/tasks/new", "/quotas", "/models", "/chat", "/townhall", "/ranking"):
        page = client.get(path).text
        head = page[:page.index("</head>")]
        # The saved theme is applied by an inline script before the stylesheet loads.
        assert head.index('localStorage.getItem("baton-theme")') < head.index("/static/style.css"), path
        assert page.count("data-theme-choice=") == 3 and "data-theme-cycle" in page, path
        assert 'class="tab-bar"' in page and 'class="sidebar"' in page, path
    assert client.get("/static/theme.js").status_code == 200


def test_colours_live_only_in_the_token_blocks():
    rest = CSS[CSS.index(":root {\n  --sans"):]          # everything after the tokens
    assert not HEX.findall(rest) and "rgb(" not in rest
    for name in ("quotas.css", "models.css", "machines.css", "chat.css", "townhall.css", "task-map.css"):
        assert not HEX.findall((STATIC / name).read_text(encoding="utf-8")), name


def test_dark_blocks_match():
    # "Auto" on a dark system and the explicit "Dark" choice must look the same.
    assert DARK == DARK_AUTO and set(DARK) <= set(LIGHT)


def _contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", [LIGHT, DARK], ids=["light", "dark"])
def test_text_contrast_is_wcag_aa(theme):
    for fg in ("ink", "muted", "faint", "accent", "ok", "warn", "bad"):
        for bg in ("panel", "panel-2", "bg", "sidebar", "selected"):
            assert _contrast(theme[fg], theme[bg]) >= 4.5, (fg, bg)
    assert _contrast(theme["on-accent"], theme["accent"]) >= 4.5
    assert _contrast(theme["on-primary"], theme["primary"]) >= 4.5


def _rules(css: str):
    """Yield (selector, body) for every plain rule, including ones inside @media."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        yield " ".join(sel.split()), body


def _is_bare(selector: str) -> bool:
    # A selector made only of element names (button, a:hover, ul li) matches
    # every such element: nothing in it narrows it with a class, id or attribute.
    narrowed = re.sub(r":[\w-]+(\([^)]*\))?", "", selector)
    return not re.search(r"[.#\[]", narrowed)


def test_no_important_on_bare_element_selectors():
    # `button { color: x !important }` beats every class on every button, so a
    # chip that sets its own text colour turns invisible. That happened in the
    # dark theme of /chat (suggestion chips, the attach button, Sign out).
    for name in ("style.css", "quotas.css", "models.css", "machines.css", "chat.css", "townhall.css", "task-map.css"):
        for selectors, body in _rules((STATIC / name).read_text(encoding="utf-8")):
            if "!important" not in body or selectors.lstrip().startswith("@"):
                continue
            for sel in selectors.split(","):
                if sel.strip() == "*" and "transition: none" in body:
                    continue  # prefers-reduced-motion: meant to beat everything
                assert not _is_bare(sel.strip()), (name, sel.strip())


def test_hidden_attribute_always_wins():
    # .btn sets display: inline-flex; without a global !important rule the JS
    # `el.hidden = true` did nothing on buttons (e.g. "Convert to task" showed
    # on an empty chat).
    assert "[hidden] { display: none !important; }" in CSS


def test_git_branch_helper_handles_repo_worktree_detached_and_plain(tmp_path):
    from baton.routes.deps import _git_branch

    # Plain directory -> ""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _git_branch(plain) == ""

    # Standard repository (.git/HEAD -> ref)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature/ui-2\n", encoding="utf-8")
    assert _git_branch(repo) == "feature/ui-2"

    # Detached HEAD -> 7-char SHA
    (repo / ".git" / "HEAD").write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    assert _git_branch(repo) == "0123456"

    # Git worktree (.git file pointing via relative gitdir)
    wt_git = tmp_path / "wt-meta"
    wt_git.mkdir()
    (wt_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../wt-meta\n", encoding="utf-8")
    assert _git_branch(wt) == "main"


def test_ui2_chat_full_height_layout_header_and_branch_badges(tmp_path, monkeypatch):
    from baton import web
    from test_models import model_item, write_config

    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        web, "catalog",
        lambda agent: {"gemini-3.8-flash": {"label": "Gemini 3.8 Flash", "efforts": ["low"]}} if agent == "gemini" else {},
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_config(data_dir, [model_item(data_dir, "qwen")])
    repo_dir = tmp_path / "demo-repo"
    (repo_dir / ".git").mkdir(parents=True)
    (repo_dir / ".git" / "HEAD").write_text("ref: refs/heads/ui-2-branch\n", encoding="utf-8")
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()

    app = web.create_app(Settings(data_dir=data_dir, lang="es", self_update=False, chat_scratch=str(scratch_dir)))
    app.state.ctx.runner.repos.all = lambda: {"u/demo": repo_dir}
    client = TestClient(app)

    # Non-chat pages keep their own body class and are not affected by body.page-chat rules.
    home_html = client.get("/").text
    assert '<body class="page-projects">' in home_html

    # Both Local and Agent views on /chat render the full-height UI-2 shell, slim topbar,
    # ⋯ overflow menu, centred conversation column, bottom composer dock, and mobile drawer/tab bar.
    for path in ("/chat", "/chat?agent=gemini"):
        html = client.get(path).text
        assert '<body class="page-chat">' in html, path
        assert "data-chat-topbar" in html, path
        assert "data-chat-title" in html, path
        assert "data-chat-folder-badge" in html, path
        assert "data-chat-branch-badge" in html, path
        assert "data-chat-menu" in html, path
        assert 'data-to-task hidden' in html, path
        assert "data-thread-rename" in html, path
        assert "data-thread-delete" in html, path
        assert "data-chat-scroll" in html and 'class="chat-column"' in html, path
        assert "data-chat-dock" in html and 'class="chat-dock-inner"' in html, path
        assert 'class="chat-sidebar"' in html and "data-sidebar-toggle" in html, path
        assert 'class="tab-bar"' in html, path
        assert 'data-branch="ui-2-branch"' in html, path

    chat_css = (STATIC / "chat.css").read_text(encoding="utf-8")
    assert "body.page-chat main" in chat_css
    assert "max-width: 760px" in chat_css


def test_readable_model_name_formats_slugs_and_keeps_catalog_labels():
    from baton.task_models import readable_model_name

    assert readable_model_name("gemini-4-neon", "Gemini 4 Neon") == "Gemini 4 Neon"
    assert readable_model_name("opencode-go/qwen3.8-max") == "Qwen 3.8 Max"
    assert readable_model_name("opencode-go/claude-haiku-5-5") == "Claude Haiku 5.5"
    assert readable_model_name("opencode-go/deepseek-v4.1-flash") == "DeepSeek V4.1 Flash"
    assert readable_model_name("opencode-go/glm-5.3-flash") == "GLM 5.3 Flash"
    assert readable_model_name("opencode-go/gpt-5.6-luna") == "GPT 5.6 Luna"
    assert readable_model_name("opencode-go/minimax-m3.1-flash") == "MiniMax M3.1 Flash"
    assert readable_model_name("local-inkling/inkling-2-mini") == "Inkling 2 Mini"


def test_ui3_composer_control_centre_chips_and_readable_names(tmp_path, monkeypatch):
    from baton import web
    from test_models import model_item, write_config

    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        web, "catalog",
        lambda agent: {"gemini-4-neon": {"label": "Gemini 4 Neon", "efforts": ["low", "high"]}} if agent == "gemini" else {},
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_config(data_dir, [model_item(data_dir, "qwen")])
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    app = web.create_app(Settings(data_dir=data_dir, lang="es", self_update=False, chat_scratch=str(scratch_dir)))
    client = TestClient(app)

    for path in ("/chat", "/chat?agent=gemini"):
        html = client.get(path).text
        assert 'data-composer-bar' in html, path
        assert 'data-cc-popover' in html, path
        for chip in ("agent", "model", "effort", "folder", "mode"):
            assert f'data-cc-chip="{chip}"' in html, (path, chip)
        assert 'class="composer-native" hidden' in html, path

    gem_html = client.get("/chat?agent=gemini").text
    assert ">Gemini 4 Neon</option>" in gem_html
    assert 'class="composer-kbd' in gem_html or "data-composer-status" in gem_html


def test_ui4_claude_code_messages_diffs_approvals_and_status_line(tmp_path, monkeypatch):
    from baton import web
    from test_models import model_item, write_config

    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        web, "catalog",
        lambda agent: {"gemini-4-neon": {"label": "Gemini 4 Neon", "efforts": ["low"]}} if agent == "gemini" else {},
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_config(data_dir, [model_item(data_dir, "qwen")])
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    app = web.create_app(Settings(data_dir=data_dir, lang="es", self_update=False, chat_scratch=str(scratch_dir)))
    client = TestClient(app)

    for path in ("/chat", "/chat?agent=gemini"):
        html = client.get(path).text
        assert "data-composer-status" in html, path
        assert "data-status-text" in html, path
        assert '"thinking_s"' in html and '"thought_for"' in html, path
        assert '"perm_keys"' in html and '"diff_unchanged"' in html, path

    chat_css = (STATIC / "chat.css").read_text(encoding="utf-8")
    assert ".msg-assistant { align-self: stretch; background: transparent; border: 0;" in chat_css
    assert ".ag-dot" in chat_css
    assert ".ag-diff-table" in chat_css and ".ag-ln" in chat_css
    assert ".ag-kbd" in chat_css
    assert ".composer-status" in chat_css

    common_js = (STATIC / "chat-common.js").read_text(encoding="utf-8")
    assert "registerPendingPerm" in common_js
    assert "createThinkTimer" in common_js
    assert "ag-diff-row" in common_js


def test_ui5_unified_chat_template_and_shared_renderer(tmp_path, monkeypatch):
    from baton import web
    from test_models import model_item, write_config

    tpl = (Path(__file__).resolve().parents[1] / "baton" / "templates" / "chat.html").read_text(encoding="utf-8")
    # Template must have ONE unified chat root, composer, log, and i18n payload — not duplicated agent/local branches.
    assert tpl.count('<section class="chat"') == 1
    assert tpl.count('<form class="chat-input composer"') == 1
    assert tpl.count('<div class="chat-log"') == 1
    assert tpl.count('<script id="chat-strings"') == 1
    assert 'id="agents-strings"' not in tpl

    common_js = (STATIC / "chat-common.js").read_text(encoding="utf-8")
    chat_js = (STATIC / "chat.js").read_text(encoding="utf-8")
    agents_js = (STATIC / "agents-chat.js").read_text(encoding="utf-8")
    for sym in ("createAssistantBubble", "createToolsView", "attachCopyButton", "selectedLabel", "shortFolder", "bindAutosize"):
        assert sym in common_js, sym
    for js_name, js_src in (("chat.js", chat_js), ("agents-chat.js", agents_js)):
        assert "function readToolCategory" not in js_src, js_name
        assert "function updateGroupSummary" not in js_src, js_name
        assert "function updateToolGroupSummary" not in js_src, js_name
        assert "function selectedLabel" not in js_src, js_name
        assert "function shortFolder" not in js_src, js_name
        assert "function autosize" not in js_src, js_name
        assert "createAssistantBubble" in js_src, js_name
        assert "createToolsView" in js_src, js_name
    assert 'getElementById("chat-strings")' in agents_js
    assert "agents-strings" not in agents_js

    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(web, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        web, "catalog",
        lambda agent: {"gemini-4-neon": {"label": "Gemini 4 Neon", "efforts": ["low"]}} if agent == "gemini" else {},
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    write_config(data_dir, [model_item(data_dir, "qwen")])
    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    app = web.create_app(Settings(data_dir=data_dir, lang="es", self_update=False, chat_scratch=str(scratch_dir)))
    client = TestClient(app)

    for path in ("/chat", "/chat?agent=gemini"):
        html = client.get(path).text
        assert "data-chat-root" in html, path
        assert 'id="chat-strings"' in html, path
        assert 'id="chat-bootstrap"' in html, path
        assert 'id="agents-strings"' not in html, path


def test_huo_brand_favicon_and_dynamic_title(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    app = create_app(Settings(data_dir=tmp_path, lang="es", self_update=False, access_key="secret-k"))
    client = TestClient(app)

    # Favicon asset is served as SVG.
    icon = client.get("/static/huo-icon.svg")
    assert icon.status_code == 200
    assert "image/svg+xml" in icon.headers.get("content-type", "")
    assert "<svg" in icon.text

    # Login page shows Huo title, dynamic heading, and the Huo icon (no legacy diamond).
    login_html = client.get("/login").text
    assert '<link rel="icon" type="image/svg+xml" href="/static/huo-icon.svg?v=' in login_html
    assert 'class="mark-img" src="/static/huo-icon.svg?v=' in login_html
    assert ">◆<" not in login_html
    assert "Desbloquear Huo" in login_html
    assert '<span class="brand-name">Huo</span>' in login_html

    # Authenticated main shell and custom BATON_TITLE propagate cleanly.
    custom_app = create_app(
        Settings(data_dir=tmp_path, lang="es", self_update=False, title="MiHuo"),
        restart=lambda: None,
    )
    custom_client = TestClient(custom_app)
    page = custom_client.get("/machines").text
    assert '<link rel="icon" type="image/svg+xml" href="/static/huo-icon.svg?v=' in page
    assert 'class="mark-img" src="/static/huo-icon.svg?v=' in page
    assert ">◆<" not in page
    assert '<span class="brand-name">MiHuo</span>' in page
    assert "Reiniciar MiHuo" in page
    assert "Actualizaciones de MiHuo" in page
    assert "reinicia MiHuo" in page


def test_collapsible_sidebar_rail_and_chat_default_collapsed(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es", self_update=False)))

    home = client.get("/").text
    home_head = home[:home.index("</head>")]
    assert home_head.index('localStorage.getItem("baton-sidebar")') < home_head.index("/static/style.css")
    assert 'if (!side) side = "expanded";' in home_head
    assert "data-sidebar-toggle" in home
    assert "side-theme-cycle" in home

    chat = client.get("/chat").text
    chat_head = chat[:chat.index("</head>")]
    assert 'if (!side) side = "collapsed";' in chat_head
    assert "data-sidebar-toggle" in chat

    assert ':root[data-sidebar="collapsed"] { --side-w: 56px; }' in CSS


def test_pwa_manifest_icons_and_unauthenticated_access(tmp_path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    app = create_app(Settings(data_dir=tmp_path, lang="es", self_update=False, title="HuoLab", access_key="secret-k"))
    client = TestClient(app)

    # Manifest and icons must stay reachable without a session cookie so mobile browsers can install the PWA.
    manifest_resp = client.get("/manifest.webmanifest")
    assert manifest_resp.status_code == 200
    assert "application/manifest+json" in manifest_resp.headers.get("content-type", "")
    manifest = manifest_resp.json()
    assert manifest["name"] == "HuoLab"
    assert manifest["short_name"] == "HuoLab"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/"
    icon_srcs = [item["src"] for item in manifest["icons"]]
    assert "/static/huo-icon-192.png" in icon_srcs
    assert "/static/huo-icon-512.png" in icon_srcs
    assert "/static/huo-icon.svg" in icon_srcs

    for png in ("/static/apple-touch-icon.png", "/static/huo-icon-192.png", "/static/huo-icon-512.png"):
        r = client.get(png)
        assert r.status_code == 200, png
        assert r.content.startswith(b"\x89PNG\r\n\x1a\n"), png

    # Login and normal pages expose PWA manifest, apple-touch-icon, and light/dark theme-color tags.
    open_client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es", self_update=False)))
    for html in (client.get("/login").text, open_client.get("/").text):
        assert '<link rel="manifest" href="/manifest.webmanifest">' in html
        assert '<link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png?v=' in html
        assert 'name="theme-color" content="#faf9f5"' in html
        assert 'name="theme-color" content="#0c0c0d"' in html


