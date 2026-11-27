"""Tests for Phase M2: alert engine (strikes, hysteresis, cooldown, anomalies, logins) and UI/API."""

from __future__ import annotations

from fastapi.testclient import TestClient

from baton import web
from baton.alerts import MIB, AlertEngine, AlertThresholds
from baton.config import Settings


def _card(
    mid: str = "local",
    *,
    name: str = "My PC",
    local: bool = True,
    status: str = "online",
    age_s: float = 0.0,
    cpu_pct: float = 10.0,
    cpu_temp_c: float | None = None,
    ram_pct: float = 40.0,
    disk_pct: float = 50.0,
    gpu_util: float | None = 5.0,
    gpu_temp: float | None = 45.0,
    rx_mbps: float = 0.2,
    tx_mbps: float = 0.1,
    logins: list[dict] | None = None,
) -> dict:
    gpu = None if (gpu_util is None and gpu_temp is None) else {"util_pct": gpu_util, "temp_c": gpu_temp}
    return {
        "id": mid,
        "name": name,
        "local": local,
        "status": status,
        "age_s": age_s,
        "metrics": {
            "cpu_pct": cpu_pct,
            "cpu_temp_c": cpu_temp_c,
            "ram_pct": ram_pct,
            "disk_pct": disk_pct,
            "gpu": gpu,
            "net": {"rx_bps": rx_mbps * MIB, "tx_bps": tx_mbps * MIB},
            "logins": logins or [],
        },
    }


def test_hysteresis_strike_confirmation_escalation_and_cooldown(tmp_path):
    notified: list[dict] = []
    engine = AlertEngine(
        tmp_path,
        thresholds=AlertThresholds(confirm_strikes=2, cooldown_seconds=300.0),
        on_opened=notified.append,
    )
    t = 1_700_000_000.0

    # 1st reading at 82 °C: strike 1, does NOT open yet.
    assert engine.evaluate([_card(gpu_temp=82.0)], now=t) == []
    assert engine.active_count() == 0

    # 2nd consecutive reading at 82 °C: opens warn alert + fires on_opened hook.
    opened = engine.evaluate([_card(gpu_temp=82.0)], now=t + 30)
    assert len(opened) == 1 and opened[0]["kind"] == "gpu_temp" and opened[0]["severity"] == "warn"
    assert engine.active_count() == 1

    # Drops into hysteresis band (78 °C, between clear=75 and warn=80): stays open, no duplicate.
    assert engine.evaluate([_card(gpu_temp=78.0)], now=t + 60) == []
    assert len(engine.active_alerts()) == 1

    # Rises to 86 °C: escalates warn -> bad and emits escalated event.
    esc = engine.evaluate([_card(gpu_temp=86.0)], now=t + 90)
    assert len(esc) == 1 and esc[0]["severity"] == "bad" and esc[0]["escalated"] is True
    assert engine.active_alerts()[0]["severity"] == "bad"

    # Drops below clear threshold (74 °C < 75): auto-resolves!
    assert engine.evaluate([_card(gpu_temp=74.0)], now=t + 120) == []
    assert engine.active_alerts() == []
    assert len(engine.recent_alerts()) == 1

    # Cooldown (300 s): two spikes at t+150 and t+180 do NOT reopen inside cooldown.
    assert engine.evaluate([_card(gpu_temp=83.0)], now=t + 150) == []
    assert engine.evaluate([_card(gpu_temp=83.0)], now=t + 180) == []
    assert engine.active_alerts() == []

    # After cooldown expires (t+500): reopens cleanly.
    reopened = engine.evaluate([_card(gpu_temp=83.0)], now=t + 500)
    assert len(reopened) == 1
    assert len(notified) == 3  # open + escalate + reopen


def test_offline_unusual_gpu_network_and_new_login_rules(tmp_path):
    engine = AlertEngine(tmp_path, thresholds=AlertThresholds(confirm_strikes=2, cooldown_seconds=0.0))
    t = 1_700_000_000.0

    # 1. Offline alert opens immediately and resolves when back online.
    ev = engine.evaluate([_card("srv-1", name="Server", local=False, status="offline", age_s=240.0)], now=t)
    assert len(ev) == 1 and ev[0]["kind"] == "offline" and ev[0]["severity"] == "bad"
    engine.evaluate([_card("srv-1", name="Server", local=False, status="online")], now=t + 60)
    assert engine.active_alerts() == []

    # 2. Unusual GPU on local PC: opens when no Baton model is active; resolves when model becomes active.
    assert engine.evaluate([_card(gpu_util=85.0)], local_gpu_busy=False, now=t + 100) == []
    ev_gpu = engine.evaluate([_card(gpu_util=88.0)], local_gpu_busy=False, now=t + 130)
    assert len(ev_gpu) == 1 and ev_gpu[0]["kind"] == "gpu_unusual"
    assert "no Huo model active" in ev_gpu[0]["message"]
    engine.evaluate([_card(gpu_util=88.0)], local_gpu_busy=True, now=t + 160)
    assert engine.active_alerts() == []

    # 3. Unusual sustained network throughput compared to low baseline.
    baseline = [{"net_rx_bps": 100_000.0, "net_tx_bps": 50_000.0}] * 15
    assert engine.evaluate([_card(rx_mbps=40.0)], recent_samples_fn=lambda _m: baseline, now=t + 200) == []
    ev_net = engine.evaluate([_card(rx_mbps=42.0)], recent_samples_fn=lambda _m: baseline, now=t + 230)
    assert len(ev_net) == 1 and ev_net[0]["kind"] == "net_unusual"
    engine.evaluate([_card(rx_mbps=1.0)], recent_samples_fn=lambda _m: baseline, now=t + 260)
    assert engine.active_alerts() == []

    # 4. Login sessions: first sighting seeds silently; unseen user@host fires immediately until acked.
    assert engine.evaluate([_card(logins=[{"user": "alice", "host": "local"}])], now=t + 300) == []
    ev_login = engine.evaluate(
        [_card(logins=[{"user": "alice", "host": "local"}, {"user": "guest", "host": "10.0.0.9"}])],
        now=t + 330,
    )
    assert len(ev_login) == 1 and ev_login[0]["kind"] == "login_unusual"
    assert "guest@10.0.0.9" in ev_login[0]["message"]
    # Even if guest logs out, one-shot login alert stays open until the owner acknowledges it!
    engine.evaluate([_card(logins=[{"user": "alice", "host": "local"}])], now=t + 360)
    assert len(engine.active_alerts()) == 1
    assert engine.acknowledge(ev_login[0]["id"], now=t + 390) is True
    assert engine.active_alerts() == []


def test_alerts_sidebar_badge_ack_csrf_and_api_auth(tmp_path):
    app = web.create_app(Settings(data_dir=tmp_path, lang="es"))
    # Seed one active alert directly via AlertEngine.
    opened = app.state.ctx.alerts.evaluate(
        [_card("srv-9", name="VPS", local=False, status="offline", age_s=300.0)]
    )
    assert len(opened) == 1
    aid = opened[0]["id"]

    with TestClient(app) as client:
        # Badge shows on any page (e.g. /quotas) and active alert renders on /machines.
        q_page = client.get("/quotas").text
        assert 'data-alert-badge>1<' in q_page

        m_page = client.get("/machines").text
        assert "Alertas activas" in m_page
        assert "VPS" in m_page
        csrf = app.state.ctx.csrf_token

        # CSRF required to acknowledge.
        assert client.post(f"/machines/alerts/{aid}/ack", data={"csrf": "bad"}).status_code == 403

        # Acknowledging clears the unacked sidebar badge.
        r_ack = client.post(f"/machines/alerts/{aid}/ack", data={"csrf": csrf}, follow_redirects=False)
        assert r_ack.status_code == 303
        assert app.state.ctx.alerts.active_count() == 0
        assert "data-alert-badge" not in client.get("/quotas").text

        api_resp = client.get("/api/alerts")
        assert api_resp.status_code == 200
        assert api_resp.json()["unacked"] == 0

    # /api/alerts enforces BATON_ACCESS_KEY auth (401 without cookie).
    locked = web.create_app(Settings(data_dir=tmp_path, access_key="lock-alerts-123"))
    with TestClient(locked) as locked_client:
        assert locked_client.get("/api/alerts").status_code == 401
