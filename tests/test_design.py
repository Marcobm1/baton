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
    for path in ("/", "/tasks", "/tasks/new", "/quotas", "/models", "/chat", "/ranking"):
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
    for name in ("quotas.css", "models.css", "machines.css", "chat.css"):
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
        for bg in ("panel", "panel-2", "bg"):
            assert _contrast(theme[fg], theme[bg]) >= 4.5, (fg, bg)
    assert _contrast(theme["on-accent"], theme["accent"]) >= 4.5


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
    for name in ("style.css", "quotas.css", "models.css", "machines.css", "chat.css"):
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
