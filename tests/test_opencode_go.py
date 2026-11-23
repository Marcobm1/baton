import json
import time

import httpx

from baton import delegate, quotas

GO_ANSWER = {"usage": {
    "rolling": {"status": "ok", "percent": 12, "resetsAt": "2030-01-01T05:00:00Z"},
    "weekly": {"status": "rate-limited", "percent": 100, "resetsAt": "2030-01-06T00:00:00Z"},
    "monthly": {"status": "ok", "percent": 40, "resetsAt": "2030-01-30T00:00:00Z"},
}}


def client(status, body):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(status, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_go_usage_is_saved_without_the_key(tmp_path):
    c, seen = client(200, GO_ANSWER)
    ok, msg = quotas.fetch_opencode_go("sk-secret", tmp_path, client=c)
    assert ok, msg
    assert seen == {"auth": "Bearer sk-secret", "url": quotas.GO_USAGE_URL}
    saved = (tmp_path / "opencode-go.json").read_text()
    assert "sk-secret" not in saved
    wins = {w.kind: w for w in quotas.opencode_go_windows(tmp_path)}
    assert set(wins) == {"5h", "week", "month"}
    assert wins["5h"].used == 12 and wins["week"].used == 100 and wins["month"].used == 40
    views = quotas.all_windows(tmp_path, tmp_path)
    assert [v["kind"] for v in views if v["provider"] == "opencode"] == ["5h", "week", "month"]


def test_go_errors_are_explained_and_save_nothing(tmp_path):
    for status, text in ((401, "401"), (403, "403"), (500, "500")):
        c, _ = client(status, {"type": "EntitlementError"})
        ok, msg = quotas.fetch_opencode_go("k", tmp_path, client=c)
        assert not ok and text in msg
    assert not (tmp_path / "opencode-go.json").exists()


def test_settings_never_print_the_go_key(monkeypatch):
    from baton.config import Settings
    assert "sk-x" not in repr(Settings(opencode_go_key="sk-x"))


def test_quotas_page_shows_go_month_and_tokens_in_the_body(tmp_path):
    from fastapi.testclient import TestClient
    from baton import web
    from baton.config import Settings
    (tmp_path / "opencode-go.json").write_text(json.dumps({"seen_at": time.time(), "usage": {
        k: dict(v) for k, v in GO_ANSWER["usage"].items()}}))
    s = Settings(github_token="t", owner="x", data_dir=tmp_path, codex_home=tmp_path,
                 claude_home=tmp_path, opencode_db=tmp_path / "none.db", lang="es")
    html = TestClient(web.create_app(s)).get("/quotas").text
    title = html.split("<title>")[1].split("</title>")[0]
    assert "Tokens" not in title
    assert "Tokens gastados" in html.split("</title>")[1]
    assert 'src="/static/opencode.svg"' in html
    assert "OpenCode" in html and ">Mes<" in html


def test_delegate_read_only_by_default():
    cfg = delegate.config()
    helper = cfg["agents"]["baton-helper"]["permissions"]
    assert helper[0] == {"action": "*", "resource": "*", "effect": "deny"}  # deny first
    allowed = {r["action"] for r in helper if r["effect"] == "allow"}
    assert allowed == {"read", "glob", "grep"}
    for rules in (helper, cfg["agents"]["baton"]["permissions"]):
        assert rules[-4:] == [{"action": a, "resource": "*", "effect": "deny"}
                              for a in ("opencode_*", "skill", "subagent", "execute")]
    task = cfg["agents"]["baton"]["permissions"]  # the write mode reuses the tasks' agent
    assert task[0]["effect"] == "deny"
    assert {"action": "shell", "resource": "git push*", "effect": "deny"} in task
    last = max(i for i, r in enumerate(task) if r == {"action": "shell", "resource": "*", "effect": "allow"})
    assert all(task.index({"action": "shell", "resource": c, "effect": "deny"}) > last
               for c in ("git commit*", "gh *"))
    web = {r["action"] for r in delegate.config(web=True)["agents"]["baton-helper"]["permissions"]
           if r["effect"] == "allow"}
    assert web == {"read", "glob", "grep", "webfetch", "websearch"}
    cmd = delegate.command("oc.exe", "opencode-go/kimi-k3", "A & B", write=False)
    assert cmd[cmd.index("--agent") + 1] == "baton-helper"
    assert cmd[-1] == "A & B" and "--archived" in cmd
    cmd = delegate.command("oc.exe", "m", "x", write=True)
    assert cmd[cmd.index("--agent") + 1] == "baton"
