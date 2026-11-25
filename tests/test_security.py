"""Security hardening tests: agent environment isolation (1.1) and local auth (1.2)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from baton import agent_chat, delegate, ping
from baton.runner import LocalRepos, Runner, Task, TaskStore
from test_runner import make_repo

SENSITIVE_VARS = {
    "ANTHROPIC_API_KEY": "sk-ant-secret",
    "GITHUB_TOKEN": "ghp_secret_token",
    "GH_TOKEN": "ghp_secret_gh",
    "GITHUB_PAT": "ghp_secret_pat",
    "OPENCODE_PERMISSION": '{"*":"allow"}',
    "BATON_OPENCODE_GO_KEY": "go-secret-key",
    "BATON_ACCESS_KEY": "access-secret-key",
    "BATON_OWNER": "owner-secret",
    "baton_custom_lower": "lower-secret",
    "github_token_not_exact": "keep-unrelated",
}

MUST_BE_STRIPPED = (
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "OPENCODE_PERMISSION",
    "BATON_OPENCODE_GO_KEY",
    "BATON_ACCESS_KEY",
    "BATON_OWNER",
    "baton_custom_lower",
)


def _set_sensitive(monkeypatch) -> None:
    for key, value in SENSITIVE_VARS.items():
        monkeypatch.setenv(key, value)


def _wait_task(store: TaskStore, task_id: str, timeout: float = 30.0) -> Task:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = store.get(task_id)
        if task and task.status not in ("queued", "running"):
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish")


def test_agent_env_strips_secrets_case_insensitively_without_mutating_parent(monkeypatch):
    _set_sensitive(monkeypatch)
    monkeypatch.setenv("SAFE_KEEP_VAR", "hello")
    sample = {**os.environ, "GitHub_Token": "mixed-case-token", "Baton_Extra": "mixed-baton"}

    cleaned = ping.agent_env(sample)

    for key in (*MUST_BE_STRIPPED, "GitHub_Token", "Baton_Extra"):
        assert key not in cleaned
    assert cleaned["SAFE_KEEP_VAR"] == "hello"
    assert cleaned["github_token_not_exact"] == "keep-unrelated"
    # Baton's own process environment stays intact for GitHub API / git calls.
    assert os.environ["GITHUB_TOKEN"] == "ghp_secret_token"
    assert os.environ["BATON_OPENCODE_GO_KEY"] == "go-secret-key"
    assert os.environ["BATON_ACCESS_KEY"] == "access-secret-key"


def test_chat_env_uses_agent_env(monkeypatch):
    _set_sensitive(monkeypatch)
    for agent in ("opencode", "gemini"):
        env = agent_chat.chat_env(agent)
        for key in MUST_BE_STRIPPED:
            assert key not in env
        assert ("OPENCODE_CONFIG_CONTENT" in env) == (agent == "opencode")


def test_runner_strips_secrets_for_opencode_local_and_reviewer(tmp_path, monkeypatch):
    _set_sensitive(monkeypatch)
    work, _origin = make_repo(tmp_path)
    captured: dict[str, dict[str, str]] = {}

    class InspectEnvRunner(Runner):
        def _agent_cmd(self, task, wt, restricted=False):
            role = "review" if restricted else task.agent
            out_file = tmp_path / f"env-{role}.json"
            if restricted:
                script = (
                    "import json, os, pathlib; "
                    f"pathlib.Path({str(out_file)!r}).write_text(json.dumps(dict(os.environ))); "
                    "print('BATON_REVIEW_BEGIN\\nVERDICT: APPROVE\\nBATON_REVIEW_END')"
                )
            else:
                script = (
                    "import json, os, pathlib; "
                    f"pathlib.Path({str(out_file)!r}).write_text(json.dumps(dict(os.environ))); "
                    f"pathlib.Path({f'out-{role}.txt'!r}).write_text('ok')"
                )
            return [sys.executable, "-c", script]

    class FakeGitHub:
        def checks(self, repo, sha):
            return {"pending": [], "passed": ["test"], "failed": []}

        def comment(self, repo, number, body):
            pass

        def merge(self, repo, number, sha):
            pass

    store = TaskStore(tmp_path / "data")
    runner = InspectEnvRunner(
        store,
        LocalRepos("", f"x/demo={work}"),
        timeout=60,
        local_model="demo-local",
        local_provider="ollama",
        git_name="T",
        git_email="t@example.com",
        open_pr=lambda repo, branch, base, title, body: "https://github.com/x/demo/pull/1",
        github=FakeGitHub(),
        auto_merge=True,
    )
    monkeypatch.setattr(runner, "_pick_reviewer", lambda _author: "claude-code")

    for agent in ("opencode", "local"):
        task_id = f"env-{agent}"
        task = Task(
            id=task_id,
            repo="x/demo",
            title=f"Check env {agent}",
            details="",
            agent=agent,
            where="local",
            created_at=time.time(),
            branch=f"baton/{task_id}",
            base="main",
            model="opencode-go/glm-5.3" if agent == "opencode" else "demo-local",
        )
        runner.submit(task)
        done = _wait_task(store, task_id)
        assert done.status == "done", done.message
        assert done.merged, done.message
        captured[agent] = json.loads((tmp_path / f"env-{agent}.json").read_text(encoding="utf-8"))

    captured["review"] = json.loads((tmp_path / "env-review.json").read_text(encoding="utf-8"))

    for role, env in captured.items():
        for key in MUST_BE_STRIPPED:
            assert key not in env, f"{key} leaked into {role} subprocess"
    assert "OPENCODE_CONFIG_CONTENT" in captured["opencode"]
    assert "OPENCODE_CONFIG_CONTENT" not in captured["local"]
    # The reviewer gets only the worktree-scoped BATON_DELEGATION_DIR, nothing else.
    assert "BATON_DELEGATION_DIR" in captured["review"]
    assert captured["review"]["BATON_DELEGATION_DIR"] != os.environ.get("BATON_DELEGATION_DIR")
    # Baton's own process environment is untouched.
    assert os.environ["GITHUB_TOKEN"] == "ghp_secret_token"


def test_delegate_strips_secrets_while_keeping_ranking(tmp_path, monkeypatch):
    _set_sensitive(monkeypatch)
    ranking_dir = tmp_path / "ranking"
    monkeypatch.setenv("BATON_DELEGATION_DIR", str(ranking_dir))
    seen_env: dict[str, str] = {}

    def fake_run(argv, *, cwd, env, **kwargs):
        seen_env.update(env)
        return subprocess.CompletedProcess(argv, 0, stdout="delegated answer\n", stderr="")

    monkeypatch.setattr(delegate, "find_opencode", lambda: "fake-opencode")
    monkeypatch.setattr(delegate.subprocess, "run", fake_run)

    rc = delegate.run("opencode-go/glm-5.3", "check env", str(tmp_path), write=True, check_model=False)
    assert rc == 0
    for key in (*MUST_BE_STRIPPED, "BATON_DELEGATION_DIR"):
        assert key not in seen_env, f"{key} leaked into delegate subprocess"
    assert "OPENCODE_CONFIG_CONTENT" in seen_env
    # The parent delegate process still read BATON_DELEGATION_DIR to store the job.
    assert (ranking_dir / "ranking.sqlite3").is_file()


def test_ping_codex_strips_secrets(monkeypatch):
    _set_sensitive(monkeypatch)
    seen_env: dict[str, str] = {}

    def fake_run(argv, *, env=None, **kwargs):
        if env is not None:
            seen_env.update(env)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(ping.subprocess, "run", fake_run)
    ok, _ = ping.ping_codex(binary="fake-codex")
    assert ok
    assert seen_env, "ping_codex must pass an explicit sanitized env"
    for key in MUST_BE_STRIPPED:
        assert key not in seen_env


# ------------------------------------------------------------------ 1.2 auth


def test_auth_token_sign_verify_expiry_and_rotation():
    from baton import auth

    tok = auth.make_session_token("super-secret", now=1000, max_age=60)
    assert auth.verify_session_token(tok, "super-secret", now=1030)
    assert not auth.verify_session_token(tok, "super-secret", now=1060)  # expired
    assert not auth.verify_session_token(tok, "rotated-secret", now=1030)  # key rotated
    # Tampered payload or signature must fail.
    parts = tok.split(".")
    tampered = f"{parts[0]}.{int(parts[1]) + 100}.{parts[2]}.{parts[3]}"
    assert not auth.verify_session_token(tampered, "super-secret", now=1030)
    assert not auth.verify_session_token("not.a.token", "super-secret", now=1030)
    assert not auth.verify_session_token("", "super-secret", now=1030)


def test_safe_next_path_rejects_open_redirects():
    from baton.auth import safe_next_path

    assert safe_next_path("/tasks/new?repo=x/demo") == "/tasks/new?repo=x/demo"
    assert safe_next_path("https://evil.example/phish") == "/"
    assert safe_next_path("//evil.example/phish") == "/"
    assert safe_next_path(r"/\evil.example") == "/"
    assert safe_next_path("/login?next=/tasks") == "/"
    assert safe_next_path("/ok\r\nSet-Cookie: a=b") == "/"


def test_login_rate_limiter_window_and_reset():
    from baton.auth import LoginRateLimiter

    limiter = LoginRateLimiter(max_failures=3, window_seconds=60.0)
    assert not limiter.is_limited("127.0.0.1", now=10.0)
    for t in (10.0, 20.0, 30.0):
        limiter.record_failure("127.0.0.1", now=t)
    assert limiter.is_limited("127.0.0.1", now=40.0)
    # After the first attempt ages out (>60s), count drops below max_failures.
    assert not limiter.is_limited("127.0.0.1", now=71.0)
    limiter.record_failure("127.0.0.1", now=72.0)
    assert limiter.is_limited("127.0.0.1", now=72.0)
    limiter.reset("127.0.0.1")
    assert not limiter.is_limited("127.0.0.1", now=72.0)


def test_settings_loads_access_key_and_hides_it_from_repr(monkeypatch):
    from baton.config import Settings

    monkeypatch.setenv("BATON_ACCESS_KEY", "my-secret-access-key")
    monkeypatch.setenv("BATON_USE_GH_CLI", "0")
    s = Settings.from_env()
    assert s.access_key == "my-secret-access-key"
    assert "my-secret-access-key" not in repr(s)


def _auth_app(tmp_path, monkeypatch, access_key: str = "top-secret-key"):
    from baton import github, web
    from baton.config import Settings

    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    return web.create_app(Settings(
        data_dir=tmp_path / "data",
        codex_home=tmp_path,
        self_update=False,
        access_key=access_key,
    ))


def _extract_csrf(html: str) -> str:
    import re

    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "csrf input not found"
    return m.group(1)


def test_unauthenticated_requests_and_scraped_csrf_are_blocked(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    app = _auth_app(tmp_path, monkeypatch)
    client = TestClient(app)

    # /healthz and /static/* stay public.
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/static/style.css").status_code == 200

    # Protected HTML pages redirect to /login without rendering forms.
    for path in ("/", "/tasks/new", "/models", "/chat", "/quotas"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith("/login?next=")

    # Spoofing a Tailscale identity header on loopback MUST NOT bypass auth.
    spoof = {"Tailscale-User-Login": "owner@example.com"}
    assert client.get("/api/projects", headers=spoof).status_code == 401
    assert client.post("/tasks", headers=spoof, data={"title": "pwn"}).status_code == 401

    # Grab the login page's csrf token (and even an authenticated session's real CSRF token
    # from another client) to verify that CSRF alone never unlocks any endpoint.
    login_html = client.get("/login").text
    login_csrf = _extract_csrf(login_html)

    authed = TestClient(app)
    r_ok = authed.post("/login", data={"key": "top-secret-key", "csrf": login_csrf, "next": "/tasks/new"},
                       follow_redirects=False)
    assert r_ok.status_code == 303
    real_csrf = _extract_csrf(authed.get("/tasks/new").text)
    assert real_csrf != login_csrf

    # Unauthenticated client with the real CSRF token is STILL rejected (401) everywhere.
    assert client.post("/tasks", data={"repo": "x/y", "where": "local:claude-code",
                                       "title": "Escalate", "csrf": real_csrf}).status_code == 401
    assert client.post("/models/demo/start", data={"csrf": real_csrf}).status_code == 401
    assert client.post("/models/demo/stop", data={"csrf": real_csrf}).status_code == 401
    assert client.post("/self-update", data={"csrf": real_csrf}).status_code == 401
    assert client.post("/api/agents", json={"agent": "opencode"},
                       headers={"X-Baton-CSRF": real_csrf}).status_code == 401
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/quotas").status_code == 401
    assert client.get("/api/models").status_code == 401


def test_login_cookie_flags_restart_persistence_rotation_and_logout(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from baton.auth import COOKIE_NAME

    app = _auth_app(tmp_path, monkeypatch, access_key="secret-1")
    client = TestClient(app)
    login_csrf = _extract_csrf(client.get("/login?next=/quotas").text)

    # Wrong key -> 401, no cookie.
    bad = client.post("/login", data={"key": "wrong", "csrf": login_csrf, "next": "/quotas"},
                      follow_redirects=False)
    assert bad.status_code == 401
    assert COOKIE_NAME not in client.cookies

    # Cross-site origin or bad login CSRF -> 403.
    assert client.post("/login", data={"key": "secret-1", "csrf": "bad"}).status_code == 403
    assert client.post("/login", data={"key": "secret-1", "csrf": login_csrf},
                       headers={"origin": "https://evil.example"}).status_code == 403

    # Valid login over HTTPS (e.g. Tailscale Serve) sets HttpOnly, SameSite=Strict, Secure.
    https_client = TestClient(app, base_url="https://baton.tailnet.ts.net")
    https_csrf = _extract_csrf(https_client.get("/login?next=/quotas").text)
    ok_https = https_client.post(
        "/login",
        data={"key": "secret-1", "csrf": https_csrf, "next": "/quotas"},
        headers={"x-forwarded-proto": "https"},
        follow_redirects=False,
    )
    assert ok_https.status_code == 303 and ok_https.headers["location"] == "/quotas"
    https_cookie = ok_https.headers.get("set-cookie", "").lower()
    assert f"{COOKIE_NAME}=" in https_cookie
    assert "httponly" in https_cookie
    assert "samesite=strict" in https_cookie
    assert "secure" in https_cookie
    assert https_client.get("/", follow_redirects=False).status_code == 200

    # Valid login over plain HTTP (http://127.0.0.1:8787) sets HttpOnly, SameSite=Strict without Secure.
    ok = client.post("/login", data={"key": "secret-1", "csrf": login_csrf, "next": "/quotas"},
                     follow_redirects=False)
    assert ok.status_code == 303
    assert ok.headers["location"] == "/quotas"
    set_cookie = ok.headers.get("set-cookie", "").lower()
    assert f"{COOKIE_NAME}=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "secure" not in set_cookie

    # Authenticated requests work seamlessly.
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/api/projects").status_code == 200
    saved_cookie = client.cookies[COOKIE_NAME]

    # Session survives Baton restart (new app instance with same access_key).
    restarted = TestClient(_auth_app(tmp_path, monkeypatch, access_key="secret-1"))
    restarted.cookies.set(COOKIE_NAME, saved_cookie)
    assert restarted.get("/", follow_redirects=False).status_code == 200

    # Rotating BATON_ACCESS_KEY immediately invalidates existing cookies.
    rotated = TestClient(_auth_app(tmp_path, monkeypatch, access_key="secret-2"))
    rotated.cookies.set(COOKIE_NAME, saved_cookie)
    assert rotated.get("/", follow_redirects=False).status_code == 303

    # Logout clears session cookie.
    page_csrf = _extract_csrf(client.get("/").text)
    out = client.post("/logout", data={"csrf": page_csrf}, follow_redirects=False)
    assert out.status_code == 303 and out.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_rate_limits_repeated_failures(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from baton.auth import LOGIN_MAX_FAILURES

    client = TestClient(_auth_app(tmp_path, monkeypatch, access_key="secret-1"))
    login_csrf = _extract_csrf(client.get("/login").text)
    for _ in range(LOGIN_MAX_FAILURES):
        r = client.post("/login", data={"key": "wrong", "csrf": login_csrf}, follow_redirects=False)
        assert r.status_code == 401
    throttled = client.post("/login", data={"key": "secret-1", "csrf": login_csrf}, follow_redirects=False)
    assert throttled.status_code == 429


# ------------------------------------------------------------ 1.5 find_codex


def test_find_codex_prefers_native_exe_on_path_over_cmd(tmp_path, monkeypatch):
    exe = tmp_path / "codex.exe"
    cmd = tmp_path / "codex.cmd"
    exe.write_text("")
    cmd.write_text("")
    monkeypatch.delenv("BATON_CODEX_BIN", raising=False)
    monkeypatch.setattr(ping.os, "name", "nt")
    monkeypatch.setattr(ping.shutil, "which", lambda name: str(exe) if name == "codex.exe" else str(cmd))

    assert ping.find_codex() == str(exe)


def test_find_codex_resolves_native_exe_next_to_npm_wrapper(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    wrapper = npm / "codex.cmd"
    native = npm / "node_modules" / "@openai" / "codex-win32-x64" / "vendor" / "x86_64-pc-windows-msvc" / "codex" / "codex.exe"
    native.parent.mkdir(parents=True)
    wrapper.write_text("")
    native.write_text("")
    monkeypatch.delenv("BATON_CODEX_BIN", raising=False)
    monkeypatch.setattr(ping.os, "name", "nt")
    monkeypatch.setattr(ping.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(ping.shutil, "which", lambda name: None if name == "codex.exe" else str(wrapper))

    assert ping.find_codex() == str(native)


def test_find_codex_resolves_nested_arm64_native_exe(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    wrapper = npm / "codex.cmd"
    native = (npm / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
              / "codex-win32-arm64" / "vendor" / "aarch64-pc-windows-msvc" / "codex" / "codex.exe")
    native.parent.mkdir(parents=True)
    wrapper.write_text("")
    native.write_text("")
    monkeypatch.delenv("BATON_CODEX_BIN", raising=False)
    monkeypatch.setattr(ping.os, "name", "nt")
    monkeypatch.setattr(ping.platform, "machine", lambda: "ARM64")
    monkeypatch.setattr(ping.shutil, "which", lambda name: None if name == "codex.exe" else str(wrapper))

    assert ping.find_codex() == str(native)


def test_find_codex_falls_back_to_cmd_only_when_no_native_binary_exists(tmp_path, monkeypatch):
    wrapper = tmp_path / "codex.cmd"
    wrapper.write_text("")
    monkeypatch.delenv("BATON_CODEX_BIN", raising=False)
    monkeypatch.setattr(ping.os, "name", "nt")
    monkeypatch.setattr(ping.shutil, "which", lambda name: None if name == "codex.exe" else str(wrapper))

    assert ping.find_codex() == str(wrapper)


