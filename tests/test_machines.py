"""Tests for Phase M1: local + remote machine monitoring, Bearer heartbeat auth, and monitor-agent."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from baton import db, web
from baton.config import Settings
from baton.machines import MachineStore, collect_host_metrics, hash_token, sanitize_metrics
from baton.monitor_agent import send_heartbeat


def test_machine_store_hashes_tokens_tracks_online_offline_and_samples(tmp_path):
    store = MachineStore(tmp_path, offline_after=120.0)
    machine, raw_token = store.create_machine("Home Server")
    mid = machine["id"]

    # Raw token is never stored in SQLite — only its SHA-256 hash.
    with db.connect(tmp_path) as conn:
        row = conn.execute("SELECT token_hash FROM machines WHERE id = ?", (mid,)).fetchone()
    assert row["token_hash"] == hash_token(raw_token)
    assert raw_token not in row["token_hash"]

    assert store.verify_token(raw_token) == {"id": mid, "name": "Home Server"}
    assert store.verify_token("wrong-token") is None

    # Before first heartbeat, status is pending.
    cards = {m["id"]: m for m in store.list_machines(refresh_local=True)}
    assert "local" in cards and cards["local"]["status"] == "online"
    assert cards[mid]["status"] == "pending"

    # After heartbeat, status is online and sample is recorded.
    t0 = 1_700_000_000.0
    store.record_heartbeat(mid, {
        "hostname": "homesrv", "os": "Linux 6.8", "uptime_s": 12345,
        "cpu_pct": 22.5, "cpu_temp_c": 54.0, "ram_pct": 48.0, "ram_used_gb": 7.6, "ram_total_gb": 16.0,
        "disk_pct": 61.0, "disk_free_gb": 180.0, "disk_total_gb": 500.0,
        "gpu": {"name": "RTX", "util_pct": 12.0, "temp_c": 41.0, "vram_used_gb": 1.2, "vram_total_gb": 16.0},
        "net": {"rx_bps": 10240.0, "tx_bps": 2048.0},
        "logins": [{"user": "alice", "host": "100.64.0.2", "started": t0 - 60}],
    }, now=t0)
    cards_on = {m["id"]: m for m in store.list_machines(now=t0 + 30.0, refresh_local=False)}
    assert cards_on[mid]["status"] == "online"
    assert cards_on[mid]["metrics"]["cpu_pct"] == 22.5
    assert len(store.recent_samples(mid)) == 0  # cutoff uses time.time()

    # After offline_after expires, status flips to offline.
    cards_off = {m["id"]: m for m in store.list_machines(now=t0 + 200.0, refresh_local=False)}
    assert cards_off[mid]["status"] == "offline"

    # Deleting revokes the token and removes the machine, while refusing to delete "local".
    assert store.delete_machine("local") is False
    assert store.delete_machine(mid) is True
    assert store.verify_token(raw_token) is None


def test_heartbeat_auth_isolation_under_access_key_and_payload_validation(tmp_path):
    app = web.create_app(Settings(data_dir=tmp_path, access_key="master-key-xyz", lang="en"))
    machines = app.state.ctx.machines
    m, token = machines.create_machine("VPS EU")

    with TestClient(app) as client:
        # Unauthenticated UI / API calls are still blocked by BATON_ACCESS_KEY.
        assert client.get("/machines", follow_redirects=False).status_code == 303
        assert client.get("/api/machines").status_code == 401
        # A machine Bearer token NEVER unlocks any other Baton route.
        assert client.get("/api/machines", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert client.get("/api/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 401

        # Heartbeat without or with invalid Bearer token returns 401.
        assert client.post("/api/machines/heartbeat", json={"cpu_pct": 10}).status_code == 401
        assert client.post("/api/machines/heartbeat", json={"cpu_pct": 10},
                           headers={"Authorization": "Bearer wrong"}).status_code == 401

        # Oversized payload returns 413; non-object JSON returns 400.
        big = b'{"x":"' + b"a" * 20_000 + b'"}'
        assert client.post("/api/machines/heartbeat", content=big,
                           headers={"Authorization": f"Bearer {token}"}).status_code == 413
        assert client.post("/api/machines/heartbeat", json=["not", "an", "object"],
                           headers={"Authorization": f"Bearer {token}"}).status_code == 400

        # Valid Bearer heartbeat succeeds even with BATON_ACCESS_KEY enabled!
        metrics, _ = collect_host_metrics()
        ok, msg = send_heartbeat("http://testserver", token, metrics, client=client)
        assert ok is True and msg == "ok"

        # Verify machine is now online via authenticated UI session.
        login_page = client.get("/login").text
        lcsrf = login_page.split('name="csrf" value="')[1].split('"')[0]
        assert client.post("/login", data={"key": "master-key-xyz", "csrf": lcsrf},
                           follow_redirects=False).status_code == 303

        api_resp = client.get("/api/machines")
        assert api_resp.status_code == 200
        by_id = {row["id"]: row for row in api_resp.json()["machines"]}
        assert by_id["local"]["status"] == "online"
        assert by_id[m["id"]]["status"] == "online"
        assert len(machines.recent_samples(m["id"])) == 1


def test_machines_ui_add_and_delete_with_csrf(tmp_path):
    app = web.create_app(Settings(data_dir=tmp_path, lang="es"))
    with TestClient(app) as client:
        page = client.get("/machines")
        assert page.status_code == 200
        assert "Máquinas" in page.text
        assert "Este PC" in page.text
        csrf = app.state.ctx.csrf_token

        # Missing CSRF is refused.
        assert client.post("/machines/add", data={"name": "Portatil", "csrf": "bad"}).status_code == 403

        # Creating a machine shows the one-time token + monitor-agent command.
        created_page = client.post("/machines/add", data={"name": "Portatil", "csrf": csrf})
        assert created_page.status_code == 200
        assert "baton monitor-agent --url" in created_page.text
        assert "Portatil" in created_page.text

        cards = app.state.ctx.machines.list_machines(refresh_local=False)
        remote = [c for c in cards if not c["local"]]
        assert len(remote) == 1
        mid = remote[0]["id"]

        # Deleting removes it cleanly.
        r_del = client.post(f"/machines/{mid}/delete", data={"csrf": csrf}, follow_redirects=False)
        assert r_del.status_code == 303
        assert len(app.state.ctx.machines.list_machines(refresh_local=False)) == 1


def test_sanitize_metrics_clamps_and_drops_non_finite_values():
    clean = sanitize_metrics({
        "hostname": "  host\tname  ",
        "cpu_pct": 150.0,
        "ram_pct": float("nan"),
        "disk_pct": -5.0,
        "gpu": {"name": "GPU", "util_pct": 42.5, "temp_c": float("inf")},
        "net": {"rx_bps": -10, "tx_bps": 500},
        "logins": [{"user": "u1", "host": "h1", "started": time.time()}] * 30,
    })
    assert clean["hostname"] == "host name"
    assert clean["cpu_pct"] == 100.0
    assert clean["ram_pct"] is None
    assert clean["disk_pct"] == 0.0
    assert clean["gpu"]["temp_c"] is None
    assert clean["net"]["rx_bps"] == 0.0
    assert len(clean["logins"]) == 20
    # Ensure round-trip JSON is strictly valid (no NaN/Infinity).
    json.dumps(clean, allow_nan=False)
