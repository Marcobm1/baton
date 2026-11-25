"""Models page: models.json, readings, and starting/stopping a (fake) llama-server."""
import json
import socket
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton import github, models
from baton.config import Settings
from baton.i18n import strings
from baton.web import create_app

FAKE = Path(__file__).with_name("fake_llama_server.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_config(tmp_path, items, server=None):
    server = server or sys.executable
    (tmp_path / "models.json").write_text(json.dumps({"server": server, "models": items}), encoding="utf-8")


def model_item(tmp_path, mid="demo", port=None, vram=10.0, **extra):
    gguf = tmp_path / f"{mid}.gguf"
    gguf.write_bytes(b"GGUF")
    return {"id": mid, "name": mid.title(), "file": str(gguf), "port": port or free_port(),
            "vram_gb": vram, **extra}


def test_parse_gpus_and_metrics():
    gpus = models.parse_gpus("NVIDIA GeForce RTX 4070 Ti SUPER, 71, 15823, 16376, 45, 118.82, 285.00\n"
                             "Other, [N/A], 10, 100, [N/A], [N/A], [N/A]\nbroken line\n")
    assert gpus[0] == {"name": "NVIDIA GeForce RTX 4070 Ti SUPER", "util": 71.0, "vram_used_mib": 15823.0,
                       "vram_total_mib": 16376.0, "temp": 45.0, "power": 118.82, "power_limit": 285.0}
    assert gpus[1]["util"] is None and len(gpus) == 2
    m = models.parse_metrics("# HELP x\nllamacpp:tokens_predicted_total 100\nllamacpp:bad x\nother 3\n")
    assert m == {"tokens_predicted_total": 100.0}


def test_config_problems(tmp_path):
    assert not models.load_config(tmp_path).found
    a = model_item(tmp_path, "a", port=9001)
    write_config(tmp_path, [
        a,
        {**model_item(tmp_path, "b"), "port": 9001},                 # same port
        {**model_item(tmp_path, "c"), "args": ["--host", "0.0.0.0"]},   # reserved
        {**model_item(tmp_path, "d"), "args": "-ngl 99"},               # not a list
        {**model_item(tmp_path, "Bad Id")},
        {"id": "e"},                                                     # no file/port
    ])
    cfg = models.load_config(tmp_path)
    assert [m.id for m in cfg.models] == ["a"]
    text = " ".join(cfg.problems)
    for needle in ("port 9001", "--host", "\"args\" must be a list", "Bad Id", "needs \"id\""):
        assert needle in text, needle


def test_command_keeps_the_server_local():
    m = models.Model(id="x", name="X", file="f.gguf", port=8080, args=["-ngl", "99"])
    cmd = m.command("llama-server")
    assert cmd[:3] == ["llama-server", "-m", "f.gguf"]
    assert cmd[-4:] == ["--host", "127.0.0.1", "--port", "8080"] and "--metrics" in cmd


def test_tokens_served_add_up_across_restarts():
    entry = {}
    assert models.LocalModels._count(entry, {"prompt_tokens_total": 10, "tokens_predicted_total": 50}) \
        == {"prompt": 10, "generated": 50}
    # The server restarted: its counters start again from zero.
    assert models.LocalModels._count(entry, {"prompt_tokens_total": 4, "tokens_predicted_total": 5}) \
        == {"prompt": 14, "generated": 55}
    models.LocalModels._close_counts(entry)
    assert models.LocalModels._count(entry, {}) == {"prompt": 14, "generated": 55}


@pytest.fixture
def fake_server(monkeypatch):
    # Run the fake server script instead of llama-server.
    monkeypatch.setattr(models.Model, "command",
                        lambda self, server: [sys.executable, str(FAKE), "--port", str(self.port)])
    monkeypatch.setattr(models, "_is_llama_server", lambda proc: True)


def wait_for(lm, mid, status, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        row = next(r for r in lm.overview()["models"] if r["id"] == mid)
        if row["status"] == status:
            return row
        time.sleep(0.3)
    raise AssertionError(f"{mid} never became {status}: {row['status']}")


def test_start_stats_budget_and_stop(tmp_path, fake_server, monkeypatch):
    monkeypatch.setattr(models.LocalModels, "gpus", lambda self: [
        {"name": "GPU", "util": 50.0, "vram_used_mib": 15000.0, "vram_total_mib": 16384.0,
         "temp": 60.0, "power": 100.0, "power_limit": 285.0}])
    write_config(tmp_path, [model_item(tmp_path, "one", vram=15.6), model_item(tmp_path, "two", vram=14.6)])
    lm = models.LocalModels(tmp_path)
    lm.start("one")
    try:
        row = wait_for(lm, "one", "running")
        assert row["gen_tps"] == 40 and row["prompt_tps"] == 240 and row["busy"] == 1
        assert row["served"] == {"prompt": 120, "generated": 400}
        view = lm.overview("es")
        assert view["budget"]["reserved_gb"] == 15.6 and view["show"]["m.one.gen"] == "40,0 tok/s"
        # Both don't fit in 16 GiB: the second one is refused, naming the first.
        with pytest.raises(models.ModelError) as err:
            lm.start("two")
        assert err.value.key == "m_err_vram" and err.value.params["other"] == "One"
        with pytest.raises(models.ModelError) as err:
            lm.start("one")
        assert err.value.key == "m_err_running"
    finally:
        lm.stop("one")
    row = wait_for(lm, "one", "stopped")
    assert row["served"] == {"prompt": 120, "generated": 400} and not row["crashed"]
    with pytest.raises(models.ModelError) as err:
        lm.stop("one")
    assert err.value.key == "m_err_not_running"


def test_a_crash_shows_the_log(tmp_path, fake_server):
    write_config(tmp_path, [model_item(tmp_path, "one")])
    lm = models.LocalModels(tmp_path)
    lm.start("one")
    wait_for(lm, "one", "running")
    entry = json.loads((tmp_path / "models-state.json").read_text(encoding="utf-8"))["one"]
    import psutil
    psutil.Process(entry["pid"]).kill()
    row = wait_for(lm, "one", "stopped")
    assert "start" in row["crashed"]          # the log header Baton writes at each start


@pytest.mark.parametrize("hot_at_startup", [True, False])
def test_thermal_guard_without_page_visits(tmp_path, fake_server, monkeypatch, hot_at_startup):
    readings = models.parse_gpus("First, 0, 0, 100, 85, 0, 0\nSecond, 0, 0, 100, 85, 0, 0")
    monkeypatch.setattr(models.LocalModels, "gpus", lambda self: readings)
    write_config(tmp_path, [model_item(tmp_path, "one")])
    lm = models.LocalModels(tmp_path)
    lm.start("one")
    try:
        if hot_at_startup:
            readings[1]["temp"] = 86
        # The web service inherits the loading server from a previous instance.
        with TestClient(create_app(Settings(data_dir=tmp_path, self_update=False))):
            readings[1]["temp"] = 86
            deadline = time.monotonic() + 10
            while lm._state()["one"].get("pid") and time.monotonic() < deadline:
                time.sleep(0.05)
            assert lm.overview()["models"][0]["status"] == "stopped"
            with pytest.raises(models.ModelError, match="m_err_temperature"):
                lm.start("one")
        assert lm.check_temperature()
        readings[1]["temp"] = None
        assert lm.check_temperature()
        readings[1]["temp"] = 85
        assert not lm.check_temperature()
    finally:
        if lm._proc(lm._state()["one"]):
            lm.stop("one")


def test_port_used_by_another_program(tmp_path, monkeypatch):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        write_config(tmp_path, [model_item(tmp_path, "one", port=port)])
        monkeypatch.setattr(models, "_is_llama_server", lambda proc: False)
        lm = models.LocalModels(tmp_path)
        assert lm.overview()["models"][0]["status"] == "port_busy"
        with pytest.raises(models.ModelError) as err:
            lm.start("one")
        assert err.value.key == "m_err_port"
        with pytest.raises(models.ModelError) as err:
            lm.stop("one")
        assert err.value.key == "m_err_not_ours"


def test_missing_file_and_server(tmp_path):
    item = model_item(tmp_path, "one")
    Path(item["file"]).unlink()
    write_config(tmp_path, [item])
    with pytest.raises(models.ModelError) as err:
        models.LocalModels(tmp_path).start("one")
    assert err.value.key == "m_err_file"
    write_config(tmp_path, [item], server=str(tmp_path / "nope.exe"))
    with pytest.raises(models.ModelError) as err:
        models.LocalModels(tmp_path).start("one")
    assert err.value.key == "m_err_server"


def test_every_error_has_text():
    for key in ("m_err_unknown", "m_err_server", "m_err_file", "m_err_running", "m_err_vram",
                "m_err_port", "m_err_not_running", "m_err_not_ours", "m_err_start"):
        for lang in ("en", "es"):
            assert models.ModelError(key, file="f", name="n", other="o", total=16, port=1,
                                     error="e").text(strings(lang))


def test_family_distinction_and_running_model_in_first_position(tmp_path, fake_server, monkeypatch):
    async def no_projects(self, force=False):
        return [], []
    monkeypatch.setattr(github.Hub, "load", no_projects)
    monkeypatch.setattr(models.LocalModels, "gpus", lambda self: [
        {"name": "GPU", "util": 40.0, "vram_used_mib": 14500.0, "vram_total_mib": 16384.0,
         "temp": 55.0, "power": 110.0, "power_limit": 285.0}])

    assert models.model_family("Inkling 2 Mini (oficial)", "inkling-2-mini") == ("inkling", "Inkling")
    assert models.model_family("Qwen 3.8 27B", "qwen3.8-27b") == ("qwen", "Qwen")
    assert models.model_family("DeepSeek R1 Distill Qwen 14B", "deepseek-r1") == ("deepseek", "DeepSeek")
    assert models.model_family("Custom", "custom-1", explicit="Inkling") == ("inkling", "Inkling")

    write_config(tmp_path, [
        model_item(tmp_path, "inkling-2-mini", name="Inkling 2 Mini (oficial)", args=["-c", "32768"]),
        model_item(tmp_path, "qwen3.8-27b", name="Qwen 3.8 27B (oficial)", args=["--ctx-size", "16384"]),
        model_item(tmp_path, "inkling-2-mini-abliterated", name="Inkling 2 Mini (abliterated)"),
        model_item(tmp_path, "qwen3.8-27b-abliterated", name="Qwen 3.8 27B (abliterated)"),
    ])
    lm = models.LocalModels(tmp_path)
    view = lm.overview("es")
    # Stopped: grouped by family in appearance order (Inkling first, Qwen second).
    assert [m["id"] for m in view["models"]] == [
        "inkling-2-mini",
        "inkling-2-mini-abliterated",
        "qwen3.8-27b",
        "qwen3.8-27b-abliterated",
    ]
    assert [g["label"] for g in view["groups"]] == ["Inkling", "Qwen"]
    assert view["models"][0]["ctx"] == 32768 and view["show"]["m.inkling-2-mini.ctx"] == "32 768"
    assert not view["models"][0]["abliterated"] and view["models"][1]["abliterated"]

    # Starting the last model puts it in first position (and its family first).
    lm.start("qwen3.8-27b-abliterated")
    try:
        wait_for(lm, "qwen3.8-27b-abliterated", "running")
        active_view = lm.overview("es")
        assert [m["id"] for m in active_view["models"]] == [
            "qwen3.8-27b-abliterated",
            "qwen3.8-27b",
            "inkling-2-mini",
            "inkling-2-mini-abliterated",
        ]
        assert [g["key"] for g in active_view["groups"]] == ["qwen", "inkling"]
        assert active_view["groups"][0]["active"] == 1 and active_view["groups"][1]["active"] == 0

        client = TestClient(create_app(Settings(data_dir=tmp_path, lang="es", self_update=False)))
        models_html = client.get("/models").text
        # The running Qwen model appears before Inkling on the rendered page.
        assert models_html.index('data-model="qwen3.8-27b-abliterated"') < models_html.index('data-model="inkling-2-mini"')
        assert 'data-family-filter="qwen"' in models_html and 'data-family-filter="inkling"' in models_html
        assert "En uso" in models_html and "Abliterated" in models_html and "32 768" in models_html

        chat_html = client.get("/chat").text
        assert '<optgroup label="Qwen">' in chat_html and '<optgroup label="Inkling">' in chat_html
        assert chat_html.index('<optgroup label="Qwen">') < chat_html.index('<optgroup label="Inkling">')
        assert "● Qwen 3.8 27B (abliterated)" in chat_html
    finally:
        lm.stop("qwen3.8-27b-abliterated")


@pytest.mark.parametrize("lang", ["en", "es"])
def test_page_and_actions(tmp_path, monkeypatch, lang):
    async def no_projects(self, force=False):
        return [], []
    monkeypatch.setattr(github.Hub, "load", no_projects)
    client = TestClient(create_app(Settings(data_dir=tmp_path, lang=lang, self_update=False)))
    t = strings(lang)
    page = client.get("/models").text
    assert t["m_machine"] in page and 'href="/models" aria-current="page"' in page
    assert "models.json" in page                       # how to create the list
    write_config(tmp_path, [model_item(tmp_path, "one")])
    page = client.get("/models").text
    assert "One" in page and t["m_status_stopped"] in page and t["m_start"] in page
    data = client.get("/api/models").json()
    assert data["models"][0]["status"] == "stopped" and "cpu" in data["show"]
    # Start/stop need the page's form token, like every other form.
    assert client.post("/models/one/start", data={"csrf": "wrong"}).status_code == 403
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    r = client.post("/models/nope/start", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert client.post("/models/one/explode", data={"csrf": csrf}).status_code == 422
