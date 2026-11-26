"""Alert engine for local and remote machines (Phase M2).

Evaluates machine snapshots against thresholds and rolling baselines with:
  - Strike confirmation (`confirm_strikes = 2`) for metric rules so brief
    1-sample spikes never trigger noise;
  - Hysteresis (separate open vs clear thresholds) so oscillating readings
    near a boundary never flap;
  - Cooldown after resolution so repeated transitions do not spam;
  - Silent initial login seeding (`known_logins`) so your normal account
    never false-alarms on first run, while any new `user@host` alerts
    immediately and stays open until acknowledged;
  - An `on_opened(alert)` callback hook ready for Phase M3 Telegram/Webhook
    notifications (plus `notified_at` column already present in SQLite).
"""

from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db

MIB = 1024.0 * 1024.0
SEVERITY_RANK = {"warn": 1, "bad": 2}
ONE_SHOT_KINDS = frozenset({"login_unusual"})


@dataclass(frozen=True)
class AlertThresholds:
    gpu_temp_warn: float = 80.0
    gpu_temp_bad: float = 85.0
    gpu_temp_clear: float = 75.0
    cpu_temp_warn: float = 85.0
    cpu_temp_bad: float = 95.0
    cpu_temp_clear: float = 80.0
    ram_warn: float = 90.0
    ram_bad: float = 96.0
    ram_clear: float = 85.0
    disk_warn: float = 92.0
    disk_bad: float = 97.0
    disk_clear: float = 88.0
    gpu_unusual_pct: float = 70.0
    gpu_unusual_clear: float = 40.0
    net_spike_mbps: float = 25.0       # MiB/s floor for sustained network spike
    net_baseline_mult: float = 5.0     # multiple over median when >= 10 samples exist
    net_clear_ratio: float = 0.6
    confirm_strikes: int = 2
    cooldown_seconds: float = 300.0


class AlertEngine:
    """Evaluates machine telemetry and persists alert lifecycle in `baton.sqlite3`."""

    def __init__(
        self,
        data_dir: Path,
        *,
        thresholds: AlertThresholds | None = None,
        on_opened: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.thresholds = thresholds or AlertThresholds()
        self.on_opened = on_opened
        self._lock = threading.Lock()
        self._strikes: dict[tuple[str, str], int] = {}
        self._last_resolved: dict[tuple[str, str], float] = {}

    def _emit_opened(self, alert: dict[str, Any]) -> None:
        if self.on_opened is not None:
            try:
                self.on_opened(alert)
            except Exception:
                pass

    def _open_or_update(
        self,
        conn,
        *,
        machine_id: str,
        machine_name: str,
        kind: str,
        severity: str,
        message: str,
        now: float,
        immediate: bool = False,
    ) -> dict[str, Any] | None:
        key = (machine_id, kind)
        active = conn.execute(
            "SELECT * FROM alerts WHERE machine_id = ? AND kind = ? AND resolved_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (machine_id, kind),
        ).fetchone()

        if active is not None:
            old_rank = SEVERITY_RANK.get(active["severity"], 0)
            new_rank = SEVERITY_RANK.get(severity, 0)
            final_sev = severity if new_rank > old_rank else active["severity"]
            conn.execute(
                "UPDATE alerts SET machine_name = ?, severity = ?, message = ?, updated_at = ? WHERE id = ?",
                (machine_name, final_sev, message, now, active["id"]),
            )
            if new_rank > old_rank:
                return {
                    "id": active["id"], "machine_id": machine_id, "machine_name": machine_name,
                    "kind": kind, "severity": final_sev, "message": message,
                    "opened_at": active["opened_at"], "escalated": True,
                }
            return None

        # Require confirm_strikes consecutive hits unless immediate (offline / unusual login).
        if not immediate:
            strikes = self._strikes.get(key, 0) + 1
            self._strikes[key] = strikes
            if strikes < self.thresholds.confirm_strikes:
                return None

        # Cooldown after a recent resolution prevents flapping spam.
        last_res = self._last_resolved.get(key, 0.0)
        if not immediate and last_res and (now - last_res) < self.thresholds.cooldown_seconds:
            return None

        cur = conn.execute(
            "INSERT INTO alerts (machine_id, machine_name, kind, severity, message, opened_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (machine_id, machine_name, kind, severity, message, now, now),
        )
        self._strikes.pop(key, None)
        return {
            "id": cur.lastrowid, "machine_id": machine_id, "machine_name": machine_name,
            "kind": kind, "severity": severity, "message": message,
            "opened_at": now, "escalated": False,
        }

    def _resolve_if_clear(self, conn, machine_id: str, kind: str, now: float) -> None:
        key = (machine_id, kind)
        self._strikes.pop(key, None)
        cur = conn.execute(
            "UPDATE alerts SET resolved_at = ?, updated_at = ? "
            "WHERE machine_id = ? AND kind = ? AND resolved_at IS NULL",
            (now, now, machine_id, kind),
        )
        if cur.rowcount > 0:
            self._last_resolved[key] = now

    def _eval_hysteresis(
        self,
        conn,
        *,
        machine_id: str,
        machine_name: str,
        kind: str,
        value: float | None,
        warn_at: float,
        bad_at: float | None,
        clear_below: float,
        message_fn: Callable[[float, str], str],
        now: float,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if bad_at is not None and value >= bad_at:
            sev = "bad"
        elif value >= warn_at:
            sev = "warn"
        else:
            if value < clear_below:
                self._resolve_if_clear(conn, machine_id, kind, now)
            else:
                # Inside the hysteresis band: reset pending strikes, keep open alert open.
                self._strikes.pop((machine_id, kind), None)
            return None
        return self._open_or_update(
            conn,
            machine_id=machine_id,
            machine_name=machine_name,
            kind=kind,
            severity=sev,
            message=message_fn(value, sev),
            now=now,
        )

    def _eval_logins(
        self,
        conn,
        *,
        machine_id: str,
        machine_name: str,
        logins: list[dict[str, Any]],
        now: float,
    ) -> dict[str, Any] | None:
        keys = sorted({
            f"{str(item.get('user') or '?')}@{str(item.get('host') or 'local')}"
            for item in logins
            if isinstance(item, dict) and item.get("user")
        })
        if not keys:
            return None
        existing = {
            row["login_key"]
            for row in conn.execute(
                "SELECT login_key FROM known_logins WHERE machine_id = ?", (machine_id,)
            ).fetchall()
        }
        if not existing:
            # First sighting: seed baseline silently so normal logins do not false-alarm.
            for lk in keys:
                conn.execute(
                    "INSERT OR IGNORE INTO known_logins (machine_id, login_key, first_seen) VALUES (?, ?, ?)",
                    (machine_id, lk, now),
                )
            return None

        unseen = [lk for lk in keys if lk not in existing]
        if not unseen:
            return None
        for lk in unseen:
            conn.execute(
                "INSERT OR IGNORE INTO known_logins (machine_id, login_key, first_seen) VALUES (?, ?, ?)",
                (machine_id, lk, now),
            )
        return self._open_or_update(
            conn,
            machine_id=machine_id,
            machine_name=machine_name,
            kind="login_unusual",
            severity="warn",
            message=f"New login session detected: {', '.join(unseen[:5])}",
            now=now,
            immediate=True,
        )

    def evaluate(
        self,
        machines: list[dict[str, Any]],
        *,
        local_gpu_busy: bool = False,
        now: float | None = None,
        recent_samples_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate all machines and return any newly opened or escalated alerts."""
        ts = time.time() if now is None else float(now)
        th = self.thresholds
        opened: list[dict[str, Any]] = []

        with self._lock, db.connect(self.data_dir) as conn:
            for m in machines:
                mid = str(m.get("id") or "")
                mname = str(m.get("name") or mid)
                status = str(m.get("status") or "")
                if not mid:
                    continue

                if status == "offline":
                    age_m = int(round((m.get("age_s") or 0) / 60.0))
                    ev = self._open_or_update(
                        conn,
                        machine_id=mid,
                        machine_name=mname,
                        kind="offline",
                        severity="bad",
                        message=f"Machine offline (no heartbeat for {max(age_m, 1)} min).",
                        now=ts,
                        immediate=True,
                    )
                    if ev:
                        opened.append(ev)
                    continue

                if status != "online":
                    continue

                self._resolve_if_clear(conn, mid, "offline", ts)
                metrics = m.get("metrics") if isinstance(m.get("metrics"), dict) else {}
                gpu = metrics.get("gpu") if isinstance(metrics.get("gpu"), dict) else {}
                net = metrics.get("net") if isinstance(metrics.get("net"), dict) else {}

                # 1. GPU temperature
                ev = self._eval_hysteresis(
                    conn, machine_id=mid, machine_name=mname, kind="gpu_temp",
                    value=gpu.get("temp_c"),
                    warn_at=th.gpu_temp_warn, bad_at=th.gpu_temp_bad, clear_below=th.gpu_temp_clear,
                    message_fn=lambda v, _s: f"High GPU temperature: {v:.0f} °C.",
                    now=ts,
                )
                if ev:
                    opened.append(ev)

                # 2. CPU temperature (Linux / sensors when available)
                ev = self._eval_hysteresis(
                    conn, machine_id=mid, machine_name=mname, kind="cpu_temp",
                    value=metrics.get("cpu_temp_c"),
                    warn_at=th.cpu_temp_warn, bad_at=th.cpu_temp_bad, clear_below=th.cpu_temp_clear,
                    message_fn=lambda v, _s: f"High CPU temperature: {v:.0f} °C.",
                    now=ts,
                )
                if ev:
                    opened.append(ev)

                # 3. Low RAM
                ev = self._eval_hysteresis(
                    conn, machine_id=mid, machine_name=mname, kind="ram_high",
                    value=metrics.get("ram_pct"),
                    warn_at=th.ram_warn, bad_at=th.ram_bad, clear_below=th.ram_clear,
                    message_fn=lambda v, _s: f"High RAM usage: {v:.0f}%.",
                    now=ts,
                )
                if ev:
                    opened.append(ev)

                # 4. Low disk
                ev = self._eval_hysteresis(
                    conn, machine_id=mid, machine_name=mname, kind="disk_high",
                    value=metrics.get("disk_pct"),
                    warn_at=th.disk_warn, bad_at=th.disk_bad, clear_below=th.disk_clear,
                    message_fn=lambda v, _s: f"Low free disk space ({v:.0f}% used).",
                    now=ts,
                )
                if ev:
                    opened.append(ev)

                # 5. Unusual GPU load when local Baton has no active model loaded
                if m.get("local") and local_gpu_busy:
                    self._resolve_if_clear(conn, mid, "gpu_unusual", ts)
                else:
                    ev = self._eval_hysteresis(
                        conn, machine_id=mid, machine_name=mname, kind="gpu_unusual",
                        value=gpu.get("util_pct"),
                        warn_at=th.gpu_unusual_pct, bad_at=None, clear_below=th.gpu_unusual_clear,
                        message_fn=lambda v, _s: f"Sustained GPU activity ({v:.0f}%) with no Baton model active.",
                        now=ts,
                    )
                    if ev:
                        opened.append(ev)

                # 6. Unusual sustained network traffic
                rx = float(net.get("rx_bps") or 0.0)
                tx = float(net.get("tx_bps") or 0.0)
                total_mbps = (rx + tx) / MIB
                threshold_mbps = th.net_spike_mbps
                if recent_samples_fn is not None:
                    samples = recent_samples_fn(mid)
                    rates = [
                        ((s.get("net_rx_bps") or 0.0) + (s.get("net_tx_bps") or 0.0)) / MIB
                        for s in samples
                        if s.get("net_rx_bps") is not None or s.get("net_tx_bps") is not None
                    ]
                    if len(rates) >= 10:
                        med = statistics.median(rates)
                        threshold_mbps = max(th.net_spike_mbps, med * th.net_baseline_mult)
                ev = self._eval_hysteresis(
                    conn, machine_id=mid, machine_name=mname, kind="net_unusual",
                    value=total_mbps,
                    warn_at=threshold_mbps, bad_at=None,
                    clear_below=threshold_mbps * th.net_clear_ratio,
                    message_fn=lambda v, _s: f"Unusual network throughput: {v:.1f} MiB/s.",
                    now=ts,
                )
                if ev:
                    opened.append(ev)

                # 7. Unusual login sessions (new user@host)
                logins = metrics.get("logins") if isinstance(metrics.get("logins"), list) else []
                ev = self._eval_logins(conn, machine_id=mid, machine_name=mname, logins=logins, now=ts)
                if ev:
                    opened.append(ev)

        for ev in opened:
            self._emit_opened(ev)
        return opened

    def active_alerts(self) -> list[dict[str, Any]]:
        with db.connect(self.data_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY opened_at DESC, id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_alerts(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with db.connect(self.data_dir) as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE resolved_at IS NOT NULL ORDER BY resolved_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def active_count(self) -> int:
        """Number of active (unresolved, unacknowledged) alerts for the sidebar badge."""
        with db.connect(self.data_dir) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE resolved_at IS NULL AND acked_at IS NULL"
            ).fetchone()
        return int(row["n"] if row else 0)

    def acknowledge(self, alert_id: int, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else float(now)
        with db.connect(self.data_dir) as conn:
            row = conn.execute("SELECT kind FROM alerts WHERE id = ?", (alert_id,)).fetchone()
            if row is None:
                return False
            if row["kind"] in ONE_SHOT_KINDS:
                cur = conn.execute(
                    "UPDATE alerts SET acked_at = ?, resolved_at = COALESCE(resolved_at, ?), updated_at = ? "
                    "WHERE id = ?",
                    (ts, ts, ts, alert_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE alerts SET acked_at = ?, updated_at = ? WHERE id = ?",
                    (ts, ts, alert_id),
                )
        return cur.rowcount > 0

    def acknowledge_all(self, *, now: float | None = None) -> int:
        ts = time.time() if now is None else float(now)
        with db.connect(self.data_dir) as conn:
            cur1 = conn.execute(
                "UPDATE alerts SET acked_at = ?, resolved_at = ?, updated_at = ? "
                "WHERE resolved_at IS NULL AND kind = 'login_unusual'",
                (ts, ts, ts),
            )
            cur2 = conn.execute(
                "UPDATE alerts SET acked_at = ?, updated_at = ? "
                "WHERE resolved_at IS NULL AND acked_at IS NULL",
                (ts, ts),
            )
        return int(cur1.rowcount + cur2.rowcount)
