"""Tests for Baton's local model tool-calling harness (Internet + utilities + workspace tools)."""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from baton import github, local_harness, models, web
from baton.config import Settings
from test_models import model_item, write_config


# ------------------------------------------------------ Unit tests: utilities

def test_safe_calculate_valid_and_blocks_unsafe_expressions():
    assert local_harness.safe_calculate("2 + 3 * 4") == "2 + 3 * 4 = 14"
    assert local_harness.safe_calculate("sqrt(81) + factorial(5)") == "sqrt(81) + factorial(5) = 129"
    assert "3.14159" in local_harness.safe_calculate("pi")

    for bad in (
        "",
        "__import__('os').system('echo pwned')",
        "(1).__class__.__bases__",
        "10 ** 100000",
        "factorial(500)",
        "True + 1",
        "open('/etc/passwd')",
        "1+" * 210 + "1",
    ):
        with pytest.raises(local_harness.ToolError):
            local_harness.safe_calculate(bad)


def test_get_datetime_returns_local_and_utc():
    out = local_harness.get_datetime()
    assert "Local:" in out and "UTC:" in out


# --------------------------------------------------- Unit tests: SSRF & web

def _fake_resolve_public(host, port, type=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _fake_resolve_loopback(host, port, type=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


def test_validate_public_url_blocks_ssrf_vectors():
    assert local_harness.validate_public_url(
        "https://example.com/docs", resolve_fn=_fake_resolve_public
    ) == "https://example.com/docs"
    assert local_harness.validate_public_url("https://93.184.216.34/page") == "https://93.184.216.34/page"

    for bad in (
        "",
        "file:///etc/passwd",
        "ftp://example.com/a",
        "http://user:pass@example.com/",
        "http://localhost:8787/",
        "http://sub.localhost/api",
        "http://127.0.0.1:8787/tasks",
        "http://[::1]:8787/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.0.0.1/admin",
        "http://172.16.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://printer.local/",
    ):
        with pytest.raises(local_harness.ToolError):
            local_harness.validate_public_url(bad, resolve_fn=_fake_resolve_public)

    # Hostname resolving via DNS to 127.0.0.1 must be blocked.
    with pytest.raises(local_harness.ToolError):
        local_harness.validate_public_url("https://rebind.example/", resolve_fn=_fake_resolve_loopback)


def test_parse_ddg_html_and_web_search_mocked():
    sample_html = """
    <html><body>
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=abc">Python 3 Docs</a>
        <a class="result__snippet">Official Python documentation and library reference.</a>
      </div>
      <div class="result">
        <a class="result__a" href="https://example.org/guide">Example Guide</a>
        <div class="result__snippet">A concise guide to tools.</div>
      </div>
    </body></html>
    """
    parsed = local_harness.parse_ddg_html(sample_html, limit=5)
    assert len(parsed) == 2
    assert parsed[0]["url"] == "https://docs.python.org/3/"
    assert parsed[0]["title"] == "Python 3 Docs"
    assert "Official Python" in parsed[0]["snippet"]

    def ddg_handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://html.duckduckgo.com/html/")
        return httpx.Response(200, text=sample_html)

    out = local_harness.web_search(
        "python docs",
        client_factory=lambda t: httpx.Client(transport=httpx.MockTransport(ddg_handler)),
    )
    assert "UNTRUSTED WEB SEARCH RESULTS" in out
    assert "https://docs.python.org/3/" in out

    # Optional SearXNG JSON backend (BATON_SEARCH_URL)
    def searx_handler(request: httpx.Request) -> httpx.Response:
        assert "q=fastapi+sse" in str(request.url)
        return httpx.Response(200, json={
            "results": [{"title": "FastAPI SSE", "url": "https://fastapi.tiangolo.com/", "content": "Streaming guide"}]
        })

    out2 = local_harness.web_search(
        "fastapi sse",
        search_url="http://127.0.0.1:8888/search?format=json&q={query}",
        client_factory=lambda t: httpx.Client(transport=httpx.MockTransport(searx_handler)),
    )
    assert "FastAPI SSE" in out2 and "https://fastapi.tiangolo.com/" in out2


def test_fetch_url_strips_scripts_blocks_binary_and_blocks_ssrf_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://example.com/article":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><head><title>Hello</title><style>body{color:red}</style></head>"
                     "<body><script>alert(1)</script><h1>Headline</h1><p>Body text here.</p></body></html>",
            )
        if str(request.url) == "https://example.com/redirect-local":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8787/tasks"})
        if str(request.url) == "https://example.com/bin":
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\x00\x01\x02")
        return httpx.Response(404)

    factory = lambda t: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)  # noqa: E731
    out = local_harness.fetch_url(
        "https://example.com/article", client_factory=factory, resolve_fn=_fake_resolve_public
    )
    assert "UNTRUSTED WEB PAGE CONTENT" in out
    assert "Title: Hello" in out
    assert "Headline" in out and "Body text here." in out
    assert "alert(1)" not in out and "color:red" not in out

    # Redirect from public site to loopback must be blocked!
    with pytest.raises(local_harness.ToolError, match="Blocked"):
        local_harness.fetch_url(
            "https://example.com/redirect-local", client_factory=factory, resolve_fn=_fake_resolve_public
        )

    # Binary content rejected cleanly.
    with pytest.raises(local_harness.ToolError, match="Unsupported content type"):
        local_harness.fetch_url(
            "https://example.com/bin", client_factory=factory, resolve_fn=_fake_resolve_public
        )


# ---------------------------------------------- Unit tests: workspace safety

def test_workspace_path_confinement_and_env_exclusion(tmp_path: Path):
    ws = tmp_path / "ws"
    sibling = tmp_path / "secret_sibling"
    ws.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")
    (ws / "notes.txt").write_text("hello world\nsecond line\n", encoding="utf-8")
    (ws / ".env").write_text("API_KEY=SHOULD_NOT_LEAK\n", encoding="utf-8")

    # Traversal outside workspace is blocked.
    with pytest.raises(local_harness.ToolError, match="outside"):
        local_harness.resolve_workspace_path(ws, "../secret_sibling/secret.txt")

    # Symlink pointing outside workspace is blocked.
    link = ws / "escape.txt"
    try:
        link.symlink_to(sibling / "secret.txt")
    except OSError:
        pass
    else:
        with pytest.raises(local_harness.ToolError, match="outside"):
            local_harness.resolve_workspace_path(ws, "escape.txt")

    # search_files finds notes.txt and skips .env silently.
    found = local_harness.search_files(ws, "hello")
    assert "notes.txt:1: hello world" in found
    not_found = local_harness.search_files(ws, "SHOULD_NOT_LEAK")
    assert "No matches" in not_found


# ----------------------------------------- End-to-end streaming harness tests

def _start_fake_tool_llm(script_fn):
    """Start an in-process HTTP server mimicking llama-server /v1/chat/completions with tool_calls."""
    recorded: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                self.send_response(404)
                self.end_headers()
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            recorded.append(body)
            chunks = script_fn(len(recorded), body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, recorded


def _parse_sse(raw_text: str) -> list[dict]:
    out = []
    for line in raw_text.splitlines():
        if line.startswith("data: "):
            data = line[6:].strip()
            if data and data != "[DONE]":
                out.append(json.loads(data))
    return out


def test_harness_multi_round_tool_call_and_page_controls(tmp_path: Path, monkeypatch):
    def llm_script(call_num: int, body: dict) -> list[dict]:
        if call_num == 1:
            # Verify tools schema was sent and emit chunked tool_calls delta.
            tool_names = {t["function"]["name"] for t in body.get("tools") or []}
            assert {"web_search", "fetch_url", "get_datetime", "calculate"} <= tool_names
            assert "write_file" not in tool_names  # no folder selected
            return [
                {"choices": [{"delta": {"reasoning_content": "Let me calculate."}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call_calc_1", "type": "function", "function": {"name": "calculate", "arguments": '{"expr'}}
                ]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": 'ession": "6 * 7"}'}}
                ]}}]},
            ]
        # Second round must receive the tool output message.
        tool_msgs = [m for m in body["messages"] if m.get("role") == "tool"]
        assert tool_msgs and "42" in tool_msgs[-1]["content"]
        return [
            {"choices": [{"delta": {"content": "The answer is 42."}}],
             "timings": {"prompt_n": 100, "predicted_n": 6, "predicted_per_second": 55.5}},
        ]

    server, port, recorded = _start_fake_tool_llm(llm_script)
    try:
        async def no_projects(self, force=False):
            return [], []

        monkeypatch.setattr(github.Hub, "load", no_projects)
        monkeypatch.setattr(models, "_is_llama_server", lambda proc: True)
        item = model_item(tmp_path, "qwen-abliterated", port=port)
        item["name"] = "Qwen Abliterated"
        write_config(tmp_path, [item])
        scratch = tmp_path / "scratch"
        app = web.create_app(Settings(data_dir=tmp_path, lang="es", self_update=False, chat_scratch=str(scratch)))
        with TestClient(app) as client:
            html = client.get("/chat").text
            assert 'data-harness-mode' in html
            assert 'value="off" checked data-harness-mode' in html
            assert 'data-harness-folder' in html
            assert 'data-abliterated="1"' in html

            csrf = app.state.ctx.csrf_token
            headers = {"X-Baton-CSRF": csrf}
            # Missing CSRF -> 403
            assert client.post(
                "/api/chat/qwen-abliterated/harness",
                json={"messages": [{"role": "user", "content": "6*7?"}]},
            ).status_code == 403
            # Unknown folder -> 400 (fail closed)
            assert client.post(
                "/api/chat/qwen-abliterated/harness",
                json={"messages": [{"role": "user", "content": "hi"}], "folder": "/etc"},
                headers=headers,
            ).status_code == 400

            resp = client.post(
                "/api/chat/qwen-abliterated/harness",
                json={"messages": [{"role": "user", "content": "What is 6*7?"}], "reasoning_effort": "low"},
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse(resp.text)
            assert any(e.get("type") == "run" for e in events)
            assert any(e.get("type") == "thought" and "calculate" in e["text"] for e in events)
            completed_tools = [e for e in events if e.get("type") == "tool" and e.get("status") == "completed"]
            assert len(completed_tools) == 1 and "42" in completed_tools[0]["output"]
            text = "".join(e["text"] for e in events if e.get("type") == "text")
            assert text == "The answer is 42."
            done = [e for e in events if e.get("type") == "done"]
            assert done and done[0]["rounds"] == 2
            assert len(recorded) == 2
    finally:
        server.shutdown()


def test_harness_permissions_write_deny_allow_and_command_env_isolation(tmp_path: Path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / ".env").write_text("SECRET=TOP\n", encoding="utf-8")

    # Script 1: asks to write_file (will be DENIED) -> model sees denial
    # Script 2: asks to write_file (will be ALLOWED) + run_command checking env (ALLOWED)
    mode = {"scenario": "deny_write"}

    def llm_script(call_num: int, body: dict) -> list[dict]:
        sc = mode["scenario"]
        tool_msgs = [m for m in body["messages"] if m.get("role") == "tool"]
        if not tool_msgs:
            if sc in ("deny_write", "allow_write"):
                return [{"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "c1", "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps({"path": "out.txt", "content": "hello"})},
                }]}}]}]
            if sc == "allow_cmd":
                cmd = f'"{sys.executable}" -c "import os; print(os.environ.get(\'BATON_ACCESS_KEY\', \'STRIPPED\'), os.environ.get(\'GITHUB_TOKEN\', \'STRIPPED\'))"'
                return [{"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "c2", "type": "function",
                    "function": {"name": "run_command", "arguments": json.dumps({"command": cmd})},
                }]}}]}]
            if sc == "deny_env":
                return [{"choices": [{"delta": {"tool_calls": [{
                    "index": 0, "id": "c3", "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": ".env"})},
                }]}}]}]
        return [{"choices": [{"delta": {"content": "TOOL_RESULT:" + tool_msgs[-1]["content"]}}]}]

    server, port, _ = _start_fake_tool_llm(llm_script)
    monkeypatch.setenv("BATON_ACCESS_KEY", "super-secret-access-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_super_secret_token")
    try:
        async def no_projects(self, force=False):
            return [], []

        monkeypatch.setattr(github.Hub, "load", no_projects)
        monkeypatch.setattr(models, "_is_llama_server", lambda proc: True)
        write_config(tmp_path, [model_item(tmp_path, "one", port=port)])
        app = web.create_app(Settings(data_dir=tmp_path, lang="en", self_update=False, chat_scratch=str(scratch)))
        app.state.local_harness.notify_delay = 0  # disable background timer in tests

        def run_turn_and_answer(client: TestClient, headers: dict, allow: bool) -> tuple[list[dict], list[int]]:
            statuses: list[int] = []
            events: list[dict] = []
            with client.stream(
                "POST",
                "/api/chat/one/harness",
                json={"messages": [{"role": "user", "content": "do it"}], "folder": "scratch"},
                headers=headers,
            ) as resp:
                assert resp.status_code == 200
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    ev = json.loads(line[6:])
                    events.append(ev)
                    if ev.get("type") == "permission":
                        # Answer via separate thread/request while stream waits.
                        r1 = client.post(
                            f"/api/chat-harness/{ev['run_id']}/permission",
                            json={"id": ev["id"], "allow": allow},
                            headers=headers,
                        )
                        statuses.append(r1.status_code)
                        # One-shot: answering the same permission ID twice must return 404.
                        r2 = client.post(
                            f"/api/chat-harness/{ev['run_id']}/permission",
                            json={"id": ev["id"], "allow": allow},
                            headers=headers,
                        )
                        statuses.append(r2.status_code)
            return events, statuses

        with TestClient(app) as client:
            headers = {"X-Baton-CSRF": app.state.ctx.csrf_token}

            # 1. Deny write_file -> file NOT created
            mode["scenario"] = "deny_write"
            evs, st = run_turn_and_answer(client, headers, allow=False)
            assert st == [200, 404]
            assert not (scratch / "out.txt").exists()
            assert any(e.get("type") == "permission" and e.get("diffs") for e in evs)
            assert any(e.get("type") == "permission_done" and e.get("outcome") == "denied" for e in evs)

            # 2. Allow write_file -> file IS created
            mode["scenario"] = "allow_write"
            evs, st = run_turn_and_answer(client, headers, allow=True)
            assert st == [200, 404]
            assert (scratch / "out.txt").read_text(encoding="utf-8") == "hello"

            # 3. Allow run_command -> executes with sanitized agent_env() (no Baton/GitHub secrets)
            mode["scenario"] = "allow_cmd"
            evs, st = run_turn_and_answer(client, headers, allow=True)
            assert st == [200, 404]
            text = "".join(e["text"] for e in evs if e.get("type") == "text")
            assert "STRIPPED STRIPPED" in text
            assert "super-secret-access-key" not in text
            assert "ghp_super_secret_token" not in text

            # 4. Reading .env MUST ask permission (and denying blocks reading it)
            mode["scenario"] = "deny_env"
            evs, st = run_turn_and_answer(client, headers, allow=False)
            assert st == [200, 404]
            assert any(e.get("type") == "permission" and ".env" in e.get("title", "") for e in evs)
            text = "".join(e["text"] for e in evs if e.get("type") == "text")
            assert "TOP" not in text
    finally:
        server.shutdown()


def test_harness_routes_require_auth_when_access_key_is_set(tmp_path: Path, monkeypatch):
    async def no_projects(self, force=False):
        return [], []

    monkeypatch.setattr(github.Hub, "load", no_projects)
    app = web.create_app(Settings(data_dir=tmp_path, access_key="secret-key-123", self_update=False))
    with TestClient(app) as client:
        assert client.post("/api/chat/one/harness", json={"messages": []}).status_code == 401
        assert client.post("/api/chat-harness/abc/permission", json={"id": "1", "allow": True}).status_code == 401
        assert client.post("/api/chat-harness/abc/cancel", json={}).status_code == 401


def test_web_search_ddg_antibot_fails_honestly_and_brave_tavily_work():
    # DuckDuckGo HTTP 202 anti-bot challenge MUST raise ToolError (never green success).
    ddg_202 = lambda req: httpx.Response(202, text="<html><div class='anomaly-modal'>Bots use DuckDuckGo</div></html>")  # noqa: E731
    with pytest.raises(local_harness.ToolError, match="anti-bot"):
        local_harness.web_search(
            "test query",
            client_factory=lambda t: httpx.Client(transport=httpx.MockTransport(ddg_202)),
        )

    # Brave Search API
    def brave_handler(req: httpx.Request) -> httpx.Response:
        assert req.headers.get("X-Subscription-Token") == "brave-secret-key"
        return httpx.Response(200, json={
            "web": {"results": [{"title": "Brave Hit", "url": "https://brave.example/1", "description": "Fast search"}]}
        })

    out_brave = local_harness.web_search(
        "hello",
        brave_api_key="brave-secret-key",
        client_factory=lambda t: httpx.Client(transport=httpx.MockTransport(brave_handler)),
    )
    assert "Brave Hit" in out_brave and "https://brave.example/1" in out_brave

    # Tavily Search API + error key masking
    def tavily_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["api_key"] == "tvly-secret-key"
        return httpx.Response(200, json={
            "results": [{"title": "Tavily Hit", "url": "https://tavily.example/2", "content": "Clean result"}]
        })

    out_tavily = local_harness.web_search(
        "hello",
        tavily_api_key="tvly-secret-key",
        client_factory=lambda t: httpx.Client(transport=httpx.MockTransport(tavily_handler)),
    )
    assert "Tavily Hit" in out_tavily and "https://tavily.example/2" in out_tavily

    def leaking_err(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed with tvly-secret-key")

    with pytest.raises(local_harness.ToolError) as exc_info:
        local_harness.web_search(
            "hello",
            tavily_api_key="tvly-secret-key",
            client_factory=lambda t: httpx.Client(transport=httpx.MockTransport(leaking_err)),
        )
    assert "tvly-secret-key" not in str(exc_info.value) and "***" in str(exc_info.value)


def test_tool_call_stream_filter_and_fallback_parser():
    flt = local_harness._ToolCallStreamFilter()
    chunks = [
        "Checking ",
        "now.<to",
        "ol_call>\n<function=fetch_url>\n<parameter=url>\nhttps://example.com\n</parameter>\n"
        "<parameter=max_chars>\n3000\n</parameter>\n</function>\n</tool_call>",
        " Done.<tool_call>\n<function=fetch_url>",  # truncated at EOF
    ]
    visible = "".join(flt.feed(c) for c in chunks) + flt.flush()
    assert visible == "Checking now. Done."
    assert "<tool_call>" not in visible and "<function=" not in visible
    assert flt.had_truncated_block is True
    assert len(flt.blocks) == 1

    calls = local_harness.parse_fallback_tool_calls(flt.blocks)
    assert len(calls) == 1 and calls[0]["name"] == "fetch_url"
    assert json.loads(calls[0]["arguments"]) == {"url": "https://example.com", "max_chars": 3000}

    json_calls = local_harness.parse_fallback_tool_calls(['{"name": "calculate", "arguments": {"expression": "2+2"}}'])
    assert len(json_calls) == 1 and json_calls[0]["name"] == "calculate"
    assert json.loads(json_calls[0]["arguments"]) == {"expression": "2+2"}


def test_compact_tool_messages_preserves_recent_and_trims_old():
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    for i in range(6):
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": "fetch_url", "content": f"P{i}:" + ("x" * 2000)})

    compacted = local_harness.compact_tool_messages(msgs, keep_recent=4, max_old_chars=200)
    tool_msgs = [m for m in compacted if m["role"] == "tool"]
    assert "older tool output compacted" in tool_msgs[0]["content"]
    assert "older tool output compacted" in tool_msgs[1]["content"]
    assert len(tool_msgs[0]["content"]) < 300
    # Last 4 tool outputs remain intact.
    for recent in tool_msgs[2:]:
        assert len(recent["content"]) > 2000


def test_harness_fallback_xml_tool_execution_and_synthesis_strips_raw_tags(tmp_path: Path, monkeypatch):
    # Round 1: model emits Qwen3.8 XML <tool_call> in plain delta.content (no structured tool_calls).
    # Round 2 (synthesis, max_rounds=1): server rejects tool_choice="none" with 400 once,
    # then retries without tools and model still emits a stray <tool_call> before answering.
    seen: list[dict] = []

    class CustomHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            if self.path == "/health":
                self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if body.get("tool_choice") == "none":
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"tool_choice none unsupported"}')
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if len(seen) == 1:
                chunks = [
                    {"choices": [{"delta": {"content": "<tool_call>\n<function=calculate>\n<parameter=expression>\n9 * 9\n</parameter>\n</function>\n</tool_call>"}}],
                     "timings": {"prompt_n": 50, "predicted_n": 10, "predicted_per_second": 40.0}},
                ]
            else:
                # Synthesis round: closing instruction must be present.
                assert any("reached the tool limit" in str(m.get("content", "")) for m in body["messages"])
                chunks = [
                    {"choices": [{"delta": {"content": "Final result is 81.<tool_call><function=fetch_url>"}}],
                     "timings": {"prompt_n": 80, "predicted_n": 8, "predicted_per_second": 42.0}},
                ]
            for c in chunks:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), CustomHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(local_harness, "MAX_TOOL_ROUNDS", 1)

        async def no_projects(self, force=False):
            return [], []

        monkeypatch.setattr(github.Hub, "load", no_projects)
        monkeypatch.setattr(models, "_is_llama_server", lambda proc: True)
        write_config(tmp_path, [model_item(tmp_path, "qwen", port=port)])
        app = web.create_app(Settings(data_dir=tmp_path, lang="es", self_update=False))
        # Override route default max_rounds via monkeypatch on run_harness_stream default.
        orig = local_harness.run_harness_stream

        def _patched(**kw):
            kw.setdefault("max_rounds", 1)
            return orig(**kw)

        monkeypatch.setattr("baton.routes.chat.run_harness_stream", _patched)

        with TestClient(app) as client:
            resp = client.post(
                "/api/chat/qwen/harness",
                json={"messages": [{"role": "user", "content": "9*9"}]},
                headers={"X-Baton-CSRF": app.state.ctx.csrf_token},
            )
            assert resp.status_code == 200
            events = _parse_sse(resp.text)
            assert any(e.get("type") == "tool" and e.get("name") == "calculate" and "81" in e.get("output", "") for e in events)
            assert any(e.get("type") == "timings" for e in events)
            text = "".join(e["text"] for e in events if e.get("type") == "text")
            assert text == "Final result is 81."
            assert "<tool_call>" not in text and "<function=" not in text
    finally:
        server.shutdown()

