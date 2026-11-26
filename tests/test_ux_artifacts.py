"""UX-3 tests: structured reports, secure .baton-artifacts/ collection, lightbox gallery, and Telegram photo."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from baton import github, runner as runner_mod
from baton.artifacts import (
    ARTIFACTS_DIR_NAME,
    MAX_ARTIFACT_BYTES,
    MAX_ARTIFACTS,
    collect_worktree_artifacts,
    detect_image_mime,
    parse_artifact_captions,
    parse_structured_report,
    resolve_artifact_path,
)
from baton.config import Settings
from baton.db import connect as db_connect, db_path
from baton.notify import Notifier
from baton.runner import LocalRepos, Runner, Task, TaskStore
from baton.status import parse
from baton.web import create_app

# Minimal valid raster headers for security & magic-byte tests.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF_BYTES = b"GIF89a" + b"\x00" * 32
WEBP_BYTES = b"RIFF\x20\x00\x00\x00WEBP" + b"\x00" * 24


def test_detect_image_mime_accepts_only_safe_rasters():
    assert detect_image_mime(PNG_BYTES) == "image/png"
    assert detect_image_mime(JPEG_BYTES) == "image/jpeg"
    assert detect_image_mime(GIF_BYTES) == "image/gif"
    assert detect_image_mime(WEBP_BYTES) == "image/webp"
    assert detect_image_mime(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>") is None
    assert detect_image_mime(b"<!doctype html><html><body>xss</body></html>") is None
    assert detect_image_mime(b"") is None


def test_parse_artifact_captions_and_structured_report():
    summary = (
        "Summary: Reworked the HUD layout for 1080p.\n"
        "Changes:\n- Compacted health bar\n- Fixed minimap overlap\n"
        "Checks: pytest -q (260 passed)\n"
        "Next: Verify on 4K displays\n"
        "Artifacts:\n- after.png | HUD after cleanup\n- map.webp: Updated minimap\n"
    )
    caps = parse_artifact_captions(summary)
    assert caps == {"after.png": "HUD after cleanup", "map.webp": "Updated minimap"}

    rep = parse_structured_report(summary)
    assert rep is not None
    assert rep["summary"] == "Reworked the HUD layout for 1080p."
    assert "Compacted health bar" in rep["changes"]
    assert "260 passed" in rep["checks"]
    assert "4K displays" in rep["next"]

    # Spanish headings work too.
    es_rep = parse_structured_report("Resumen: Limpieza lista.\nComprobaciones: todo en verde")
    assert es_rep == {"summary": "Limpieza lista.", "checks": "todo en verde"}

    # Legacy free-form prose falls back to None so raw Markdown is preserved verbatim.
    assert parse_structured_report("Just fixed a small typo in README.md.") is None
    assert parse_structured_report("Summary: Only one section here.") is None


def test_collect_worktree_artifacts_security_and_cleanup(tmp_path):
    wt = tmp_path / "wt"
    data = tmp_path / "data"
    art = wt / ARTIFACTS_DIR_NAME
    art.mkdir(parents=True)

    # Valid images
    (art / "shot.png").write_bytes(PNG_BYTES)
    (art / "photo.jpg").write_bytes(JPEG_BYTES)
    (art / "ui.webp").write_bytes(WEBP_BYTES)

    # Hostile / disallowed files
    (art / "xss.svg").write_text("<svg onload=alert(1)/>", encoding="utf-8")
    (art / "page.html").write_text("<html><script>alert(1)</script></html>", encoding="utf-8")
    (art / "fake.png").write_text("<html>not a png</html>", encoding="utf-8")
    (art / "mismatch.jpg").write_bytes(PNG_BYTES)  # extension/magic mismatch
    (art / "empty.png").write_bytes(b"")
    (art / "huge.png").write_bytes(PNG_BYTES + b"\x00" * MAX_ARTIFACT_BYTES)

    # Hardlink pointing outside the worktree must be refused (st_nlink > 1)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    try:
        os.link(outside, art / "hardlink.png")
    except OSError:
        pass

    # Symlink pointing outside the worktree must be refused
    try:
        os.symlink(outside, art / "symlink.png")
    except OSError:
        pass

    collected = collect_worktree_artifacts(
        wt, data, "task-sec-1",
        summary="Artifacts:\n- shot.png | Main screenshot\n",
    )
    # .baton-artifacts MUST always be deleted from the worktree.
    assert not art.exists()

    names = [c["name"] for c in collected]
    assert set(names) == {"photo.jpg", "shot.png", "ui.webp"}
    shot = next(c for c in collected if c["name"] == "shot.png")
    assert shot["mime"] == "image/png"
    assert shot["caption"] == "Main screenshot"
    assert (data / "artifacts" / "task-sec-1" / "shot.png").is_file()


def test_collect_worktree_artifacts_enforces_max_count_and_symlink_dir(tmp_path):
    wt = tmp_path / "wt"
    data = tmp_path / "data"
    art = wt / ARTIFACTS_DIR_NAME
    art.mkdir(parents=True)
    for i in range(MAX_ARTIFACTS + 4):
        (art / f"img_{i:02d}.png").write_bytes(PNG_BYTES)

    collected = collect_worktree_artifacts(wt, data, "task-cap")
    assert len(collected) == MAX_ARTIFACTS
    assert not art.exists()

    # If .baton-artifacts itself is a symlink to another folder, refuse & unlink it without touching target.
    secret_dir = tmp_path / "secret"
    secret_dir.mkdir()
    (secret_dir / "keep.png").write_bytes(PNG_BYTES)
    try:
        os.symlink(secret_dir, art, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    assert collect_worktree_artifacts(wt, data, "task-sym") == []
    assert not art.exists() and not art.is_symlink()
    assert (secret_dir / "keep.png").is_file()


def test_resolve_artifact_path_blocks_traversal_and_tampering(tmp_path):
    base = tmp_path / "artifacts" / "task-1"
    base.mkdir(parents=True)
    good = base / "ok.png"
    good.write_bytes(PNG_BYTES)
    tampered = base / "bad.png"
    tampered.write_bytes(b"<script>alert(1)</script>")
    (tmp_path / "artifacts" / "other.png").write_bytes(PNG_BYTES)

    assert resolve_artifact_path(tmp_path, "task-1", "ok.png") == (good.resolve(), "image/png")
    assert resolve_artifact_path(tmp_path, "task-1", "bad.png") is None
    assert resolve_artifact_path(tmp_path, "task-1", "../other.png") is None
    assert resolve_artifact_path(tmp_path, "../artifacts", "other.png") is None
    assert resolve_artifact_path(tmp_path, "task-1", "evil.svg") is None


def test_db_migrates_tasks_table_adding_artifacts_column(tmp_path):
    path = db_path(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE tasks (
            id TEXT PRIMARY KEY, repo TEXT NOT NULL, title TEXT NOT NULL,
            details TEXT NOT NULL, agent TEXT NOT NULL, where_kind TEXT NOT NULL,
            created_at REAL NOT NULL, status TEXT NOT NULL, branch TEXT NOT NULL,
            base TEXT NOT NULL, worktree TEXT NOT NULL, pr_url TEXT NOT NULL,
            issue_url TEXT NOT NULL, message TEXT NOT NULL, finished_at REAL,
            open_pr INTEGER NOT NULL, model TEXT NOT NULL, steps TEXT NOT NULL,
            effort TEXT NOT NULL, summary TEXT NOT NULL, issue_number INTEGER,
            reviewer TEXT NOT NULL, review TEXT NOT NULL, reviewer_model TEXT NOT NULL,
            reviewer_effort TEXT NOT NULL, merged INTEGER NOT NULL, priority TEXT NOT NULL,
            started_at REAL, review_rating TEXT, diff_stat TEXT
        )"""
    )
    conn.commit()
    conn.close()

    with db_connect(tmp_path) as upgraded:
        cols = {row[1] for row in upgraded.execute("PRAGMA table_info(tasks)")}
    assert "artifacts" in cols

    store = TaskStore(tmp_path)
    arts = [{"name": "after.png", "mime": "image/png", "size": 40, "caption": "After"}]
    store.save(Task("t-art", "u/demo", "UI", "", "gemini", "local", 1.0, status="done", finished_at=2.0, artifacts=arts))
    loaded = store.get("t-art")
    assert loaded is not None and loaded.artifacts == arts


def test_runner_collects_artifacts_keeps_git_clean_and_renders_gallery_with_auth(tmp_path, monkeypatch):
    def git(cwd: Path, *args: str) -> str:
        res = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
        return res.stdout

    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    origin.mkdir()
    work.mkdir()
    git(origin, "init", "--bare", "-b", "main")
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "Test")
    (work / "app.txt").write_text("v1\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "init")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "-u", "origin", "main")

    script = tmp_path / "fake_visual_agent.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('app.txt').write_text('v2\\n', encoding='utf-8')\n"
        "d = Path('.baton-artifacts')\n"
        "d.mkdir(exist_ok=True)\n"
        f"(d / 'after.png').write_bytes({PNG_BYTES!r})\n"
        "(d / 'evil.svg').write_text('<svg onload=alert(1)/>', encoding='utf-8')\n"
        "print('BATON_SUMMARY_BEGIN')\n"
        "print('Summary: Polished UI contrast and spacing.')\n"
        "print('Changes: Updated app.txt layout tokens.')\n"
        "print('Checks: pytest -q (all green)')\n"
        "print('Next: Check mobile viewport.')\n"
        "print('Artifacts:')\n"
        "print('- after.png | Updated dashboard view')\n"
        "print('BATON_SUMMARY_END')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_mod, "build_agent_cmd", lambda *a, **kw: [sys.executable, str(script)])

    data_dir = tmp_path / "data"
    store = TaskStore(data_dir)
    repos = LocalRepos("", f"u/demo={work}")
    r = Runner(
        store, repos, timeout=60, local_model="", local_provider="ollama",
        git_name="Baton", git_email="baton@localhost", open_pr=lambda *_: "",
    )
    task = Task(
        "t-vis", "u/demo", "Polish UI", "", "gemini", "local", 1.0,
        status="queued", branch="baton/t-vis", base="main", open_pr=False,
    )
    store.save(task)
    r._run(task)
    store.save(task)

    saved = store.get("t-vis")
    assert saved is not None and saved.status == "done"
    assert len(saved.artifacts) == 1
    assert saved.artifacts[0]["name"] == "after.png"
    assert saved.artifacts[0]["caption"] == "Updated dashboard view"

    # Verify .baton-artifacts NEVER entered the git commit!
    committed = git(work, "show", "--stat", "baton/t-vis")
    assert "app.txt" in committed
    assert ".baton-artifacts" not in committed
    assert "after.png" not in committed
    assert "evil.svg" not in committed

    # Mock Hub so /p/u/demo?tab=done renders cleanly.
    proj = github.Project(
        "u/demo", "u/demo", "STATUS.md", "main", False, "https://github.com/u/demo", None,
        parse("---\nname: Demo\nstatus: active\npriority: high\nupdated: 2026-11-26\n---\n"),
    )

    async def fake_load(self, force=False):
        return [proj], []

    async def no_pulls(self, repo):
        return []

    monkeypatch.setattr(github.Hub, "load", fake_load)
    monkeypatch.setattr(github.Hub, "open_pulls", no_pulls)

    # Open access (no access_key): verify structured report, gallery, thumbnails, and secure headers.
    client = TestClient(create_app(Settings(data_dir=data_dir, lang="es", self_update=False)))
    task_html = client.get("/tasks/t-vis").text
    assert "structured-report" in task_html
    assert "Resumen" in task_html and "Cambios" in task_html and "Comprobaciones" in task_html and "Siguiente" in task_html
    assert "Capturas y adjuntos" in task_html
    assert "Updated dashboard view" in task_html
    assert 'id="artifact-lightbox"' in task_html

    done_html = client.get("/p/u/demo?tab=done").text
    assert "delivery-thumbs" in done_html
    assert "/api/tasks/t-vis/artifacts/after.png" in done_html

    img_resp = client.get("/api/tasks/t-vis/artifacts/after.png")
    assert img_resp.status_code == 200
    assert img_resp.headers["content-type"].startswith("image/png")
    assert img_resp.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in img_resp.headers["content-security-policy"]
    assert img_resp.content == PNG_BYTES

    assert client.get("/api/tasks/t-vis/artifacts/evil.svg").status_code == 404
    assert client.get("/api/tasks/unknown/artifacts/after.png").status_code == 404

    # With BATON_ACCESS_KEY enabled, unauthenticated requests to artifact endpoint MUST be 401.
    locked = TestClient(create_app(Settings(data_dir=data_dir, lang="es", self_update=False, access_key="secret-key")))
    assert locked.get("/api/tasks/t-vis/artifacts/after.png").status_code == 401


def test_notifier_sends_telegram_photo_with_fallback_and_token_masking(tmp_path):
    art_dir = tmp_path / "artifacts" / "t-tg"
    art_dir.mkdir(parents=True)
    (art_dir / "first.png").write_bytes(PNG_BYTES)
    (art_dir / "second.png").write_bytes(PNG_BYTES)

    calls: list[tuple[str, bytes]] = []

    def ok_handler(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), request.content))
        return httpx.Response(200, json={"ok": True})

    token = "999999:SECRET_TG_TOKEN"
    n = Notifier(
        telegram_bot_token=token,
        telegram_chat_id="12345",
        notify_tasks=True,
        data_dir=tmp_path,
        lang="es",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(ok_handler)),
    )
    task = Task(
        "t-tg", "u/demo", "Render map", "", "gemini", "local", 1.0,
        status="done", finished_at=2.0, summary="Summary: Done!",
        artifacts=[
            {"name": "first.png", "mime": "image/png", "size": len(PNG_BYTES), "caption": "Map"},
            {"name": "second.png", "mime": "image/png", "size": len(PNG_BYTES), "caption": "HUD"},
        ],
    )
    # Run synchronously via send_sync path used by notify_task.
    photo = n._first_task_photo(task)
    assert photo is not None and photo[0].name == "first.png"
    title, body = n.format_task(task)
    body = f"{body}\n(+1 imagen más en Baton)"
    ok, msgs = n.send_sync(title, body, photo=photo)
    assert ok is True
    assert msgs == ["Telegram: ok (photo)"]
    assert len(calls) == 1
    assert calls[0][0].endswith("/sendPhoto")
    assert b"first.png" in calls[0][1]
    assert b"(+1 imagen" in calls[0][1]

    # If sendPhoto fails, send_sync must fall back to sendMessage cleanly.
    fb_calls: list[str] = []

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        fb_calls.append(request.url.path)
        if request.url.path.endswith("/sendPhoto"):
            return httpx.Response(400, text=f"bad photo {token}")
        return httpx.Response(200, json={"ok": True})

    n_fb = Notifier(
        telegram_bot_token=token,
        telegram_chat_id="12345",
        data_dir=tmp_path,
        lang="es",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(fallback_handler)),
    )
    ok2, msgs2 = n_fb.send_sync(title, body, photo=photo)
    assert ok2 is True
    assert msgs2 == ["Telegram: ok"]
    assert fb_calls == [f"/bot{token}/sendPhoto", f"/bot{token}/sendMessage"]

    # Direct _send_telegram_photo error masks the bot token.
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(500, text=f"err {token}"))) as cli:
        ok3, msg3 = n_fb._send_telegram_photo(cli, title, body, photo[0], photo[1])
    assert ok3 is False
    assert token not in msg3
    assert "***" in msg3
