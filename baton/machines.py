"""Machine monitoring: local host telemetry, remote machine heartbeats, and sample storage.

Phase M1 of multi-machine monitoring & alerts:
  - Monitors this computer (`local`) automatically using `psutil` + `nvidia-smi`.
  - Registers extra computers/servers with a per-machine Bearer token stored
    only as a SHA-256 hash in `baton.sqlite3` (never `BATON_ACCESS_KEY`).
  - Records rolling 24-hour samples (`machine_samples`) for both `local` and
    remote machines so Phase M2 alerts can evaluate thresholds and baselines.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from . import db

GIB = 1024 ** 3
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GPU_FIELDS = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
MAX_HEARTBEAT_BYTES = 16_384
SAMPLE_RETENTION_SECONDS = 24 * 3600
LOCAL_SAMPLE_INTERVAL = 30.0
DEFAULT_OFFLINE_AFTER = 180.0

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _clean_float(val: Any, *, lo: float | None = None, hi: float | None = None) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if lo is not None and f < lo:
        f = lo
    if hi is not None and f > hi:
        f = hi
    return round(f, 2)


def _clean_str(val: Any, limit: int = 120) -> str:
    return " ".join(str(val or "").split())[:limit]


def _read_gpu() -> dict[str, Any] | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, f"--query-gpu={GPU_FIELDS}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        parts = [p.strip() for p in r.stdout.splitlines()[0].split(",")]
        if len(parts) < 7:
            return None
        mem_used_mb = _clean_float(parts[2], lo=0)
        mem_total_mb = _clean_float(parts[3], lo=0)
        vram_used = round(mem_used_mb / 1024, 2) if mem_used_mb is not None else None
        vram_total = round(mem_total_mb / 1024, 2) if mem_total_mb is not None else None
        vram_pct = round(100.0 * mem_used_mb / mem_total_mb, 1) if (mem_used_mb is not None and mem_total_mb) else None
        return {
            "name": _clean_str(parts[0], 80),
            "util_pct": _clean_float(parts[1], lo=0, hi=100),
            "vram_used_gb": vram_used,
            "vram_total_gb": vram_total,
            "vram_pct": vram_pct,
            "temp_c": _clean_float(parts[4], lo=-40, hi=150),
            "power_w": _clean_float(parts[5], lo=0, hi=2000),
            "power_limit_w": _clean_float(parts[6], lo=0, hi=2000),
        }
    except (OSError, subprocess.SubprocessError):
        return None


def _read_cpu_temp() -> float | None:
    if not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        temps = psutil.sensors_temperatures() or {}
    except (OSError, AttributeError):
        return None
    for key in ("coretemp", "k10temp", "cpu_thermal", "zenpower", "acpitz"):
        entries = temps.get(key)
        if entries:
            vals = [e.current for e in entries if e.current is not None and math.isfinite(e.current)]
            if vals:
                return round(max(vals), 1)
    for entries in temps.values():
        vals = [e.current for e in entries if e.current is not None and math.isfinite(e.current)]
        if vals:
            return round(max(vals), 1)
    return None


def _read_logins() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        users = psutil.users()
    except OSError:
        return out
    for u in users[:20]:
        out.append({
            "user": _clean_str(getattr(u, "name", ""), 64),
            "host": _clean_str(getattr(u, "host", ""), 64),
            "started": _clean_float(getattr(u, "started", None), lo=0),
        })
    return out


def collect_host_metrics(
    prev_net: tuple[float, int, int] | None = None,
    *,
    now: float | None = None,
) -> tuple[dict[str, Any], tuple[float, int, int]]:
    """Read CPU, RAM, disk, optional GPU/temp, network rates, and active logins."""
    ts = time.time() if now is None else float(now)
    vm = psutil.virtual_memory()
    disk_root = Path.home().anchor or ("C:\\" if os.name == "nt" else "/")
    du = psutil.disk_usage(disk_root)
    net_io = psutil.net_io_counters()
    rx_bytes = int(getattr(net_io, "bytes_recv", 0) or 0)
    tx_bytes = int(getattr(net_io, "bytes_sent", 0) or 0)
    rx_bps = 0.0
    tx_bps = 0.0
    if prev_net is not None:
        dt = max(ts - prev_net[0], 0.0)
        if dt > 0.1:
            rx_bps = max(0.0, (rx_bytes - prev_net[1]) / dt)
            tx_bps = max(0.0, (tx_bytes - prev_net[2]) / dt)

    boot = psutil.boot_time()
    metrics: dict[str, Any] = {
        "hostname": _clean_str(socket.gethostname(), 80),
        "os": _clean_str(f"{platform.system()} {platform.release()}", 80),
        "uptime_s": max(0.0, round(ts - boot, 0)),
        "cpu_pct": _clean_float(psutil.cpu_percent(interval=None), lo=0, hi=100) or 0.0,
        "cpu_temp_c": _read_cpu_temp(),
        "ram_pct": _clean_float(vm.percent, lo=0, hi=100) or 0.0,
        "ram_used_gb": round(vm.used / GIB, 2),
        "ram_total_gb": round(vm.total / GIB, 2),
        "disk_pct": _clean_float(du.percent, lo=0, hi=100) or 0.0,
        "disk_free_gb": round(du.free / GIB, 2),
        "disk_total_gb": round(du.total / GIB, 2),
        "gpu": _read_gpu(),
        "net": {
            "rx_bps": round(rx_bps, 1),
            "tx_bps": round(tx_bps, 1),
        },
        "logins": _read_logins(),
    }
    return metrics, (ts, rx_bytes, tx_bytes)


def sanitize_metrics(raw: Any) -> dict[str, Any]:
    """Validate and sanitize a heartbeat metrics object from a remote monitor agent."""
    if not isinstance(raw, dict):
        raise ValueError("Metrics must be a JSON object.")
    gpu_raw = raw.get("gpu")
    gpu = None
    if isinstance(gpu_raw, dict):
        gpu = {
            "name": _clean_str(gpu_raw.get("name"), 80),
            "util_pct": _clean_float(gpu_raw.get("util_pct"), lo=0, hi=100),
            "vram_used_gb": _clean_float(gpu_raw.get("vram_used_gb"), lo=0, hi=4096),
            "vram_total_gb": _clean_float(gpu_raw.get("vram_total_gb"), lo=0, hi=4096),
            "vram_pct": _clean_float(gpu_raw.get("vram_pct"), lo=0, hi=100),
            "temp_c": _clean_float(gpu_raw.get("temp_c"), lo=-40, hi=150),
            "power_w": _clean_float(gpu_raw.get("power_w"), lo=0, hi=5000),
            "power_limit_w": _clean_float(gpu_raw.get("power_limit_w"), lo=0, hi=5000),
        }
    net_raw = raw.get("net") if isinstance(raw.get("net"), dict) else {}
    net = {
        "rx_bps": _clean_float(net_raw.get("rx_bps"), lo=0, hi=1e12) or 0.0,
        "tx_bps": _clean_float(net_raw.get("tx_bps"), lo=0, hi=1e12) or 0.0,
    }
    logins: list[dict[str, Any]] = []
    raw_logins = raw.get("logins")
    if isinstance(raw_logins, list):
        for item in raw_logins[:20]:
            if isinstance(item, dict):
                logins.append({
                    "user": _clean_str(item.get("user"), 64),
                    "host": _clean_str(item.get("host"), 64),
                    "started": _clean_float(item.get("started"), lo=0),
                })
    return {
        "hostname": _clean_str(raw.get("hostname"), 80),
        "os": _clean_str(raw.get("os"), 80),
        "uptime_s": _clean_float(raw.get("uptime_s"), lo=0, hi=1e10) or 0.0,
        "cpu_pct": _clean_float(raw.get("cpu_pct"), lo=0, hi=100),
        "cpu_temp_c": _clean_float(raw.get("cpu_temp_c"), lo=-40, hi=150),
        "ram_pct": _clean_float(raw.get("ram_pct"), lo=0, hi=100),
        "ram_used_gb": _clean_float(raw.get("ram_used_gb"), lo=0, hi=65536),
        "ram_total_gb": _clean_float(raw.get("ram_total_gb"), lo=0, hi=65536),
        "disk_pct": _clean_float(raw.get("disk_pct"), lo=0, hi=100),
        "disk_free_gb": _clean_float(raw.get("disk_free_gb"), lo=0, hi=1e7),
        "disk_total_gb": _clean_float(raw.get("disk_total_gb"), lo=0, hi=1e7),
        "gpu": gpu,
        "net": net,
        "logins": logins,
    }


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _slug(name: str) -> str:
    base = _SLUG_RE.sub("-", name.strip().lower()).strip("-")[:32]
    if not base or base == "local":
        base = "machine"
    return f"{base}-{secrets.token_hex(3)}"


class MachineStore:
    """SQLite-backed registry for remote machines + local host rolling samples."""

    def __init__(self, data_dir: Path, *, offline_after: float = DEFAULT_OFFLINE_AFTER):
        self.data_dir = Path(data_dir)
        self.offline_after = float(offline_after)
        self._lock = threading.Lock()
        self._prev_net: tuple[float, int, int] | None = None
        self._local_metrics: dict[str, Any] | None = None
        self._local_seen: float = 0.0
        self._last_local_sample: float = 0.0

    def create_machine(self, name: str) -> tuple[dict[str, Any], str]:
        """Register a remote machine; returns `(machine, raw_token)` (token shown once)."""
        clean_name = _clean_str(name, 60)
        if not clean_name:
            raise ValueError("Machine name is required.")
        mid = _slug(clean_name)
        raw_token = secrets.token_urlsafe(32)
        now = time.time()
        with db.connect(self.data_dir) as conn:
            conn.execute(
                "INSERT INTO machines (id, name, token_hash, os_info, last_seen, metrics_json, thresholds_json, created_at) "
                "VALUES (?, ?, ?, '', 0.0, '{}', '{}', ?)",
                (mid, clean_name, hash_token(raw_token), now),
            )
        return {"id": mid, "name": clean_name, "created_at": now}, raw_token

    def verify_token(self, raw_token: str) -> dict[str, Any] | None:
        """Look up a machine by Bearer token using constant-time digest comparison."""
        if not raw_token or len(raw_token) > 256:
            return None
        digest = hash_token(raw_token)
        with db.connect(self.data_dir) as conn:
            rows = conn.execute("SELECT id, name, token_hash FROM machines").fetchall()
        for row in rows:
            if secrets.compare_digest(row["token_hash"], digest):
                return {"id": row["id"], "name": row["name"]}
        return None

    def _insert_sample(self, conn, machine_id: str, ts: float, metrics: dict[str, Any]) -> None:
        gpu = metrics.get("gpu") if isinstance(metrics.get("gpu"), dict) else {}
        net = metrics.get("net") if isinstance(metrics.get("net"), dict) else {}
        logins = metrics.get("logins") if isinstance(metrics.get("logins"), list) else []
        conn.execute(
            "INSERT INTO machine_samples "
            "(machine_id, ts, cpu_pct, ram_pct, disk_pct, gpu_pct, gpu_temp_c, net_rx_bps, net_tx_bps, logins_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                machine_id, ts,
                metrics.get("cpu_pct"), metrics.get("ram_pct"), metrics.get("disk_pct"),
                gpu.get("util_pct"), gpu.get("temp_c"),
                net.get("rx_bps"), net.get("tx_bps"),
                len(logins),
            ),
        )
        conn.execute(
            "DELETE FROM machine_samples WHERE machine_id = ? AND ts < ?",
            (machine_id, ts - SAMPLE_RETENTION_SECONDS),
        )

    def record_heartbeat(self, machine_id: str, raw_metrics: Any, *, now: float | None = None) -> dict[str, Any]:
        """Validate and persist a remote machine heartbeat + rolling sample."""
        metrics = sanitize_metrics(raw_metrics)
        ts = time.time() if now is None else float(now)
        os_info = metrics.get("os") or ""
        payload = json.dumps(metrics, ensure_ascii=False)
        with db.connect(self.data_dir) as conn:
            cur = conn.execute(
                "UPDATE machines SET last_seen = ?, os_info = ?, metrics_json = ? WHERE id = ?",
                (ts, os_info, payload, machine_id),
            )
            if cur.rowcount == 0:
                raise KeyError(machine_id)
            self._insert_sample(conn, machine_id, ts, metrics)
        return metrics

    def refresh_local(self, *, now: float | None = None, force_sample: bool = False) -> dict[str, Any]:
        """Collect fresh local host metrics and persist a sample every ~30 s."""
        ts = time.time() if now is None else float(now)
        with self._lock:
            prev = self._prev_net
        metrics, next_net = collect_host_metrics(prev, now=ts)
        with self._lock:
            self._prev_net = next_net
            self._local_metrics = metrics
            self._local_seen = ts
            should_store = force_sample or (ts - self._last_local_sample >= LOCAL_SAMPLE_INTERVAL)
            if should_store:
                self._last_local_sample = ts
        if should_store:
            with db.connect(self.data_dir) as conn:
                self._insert_sample(conn, "local", ts, metrics)
        return metrics

    def recent_samples(self, machine_id: str, *, hours: float = 24.0, limit: int = 200) -> list[dict[str, Any]]:
        cutoff = time.time() - hours * 3600.0
        with db.connect(self.data_dir) as conn:
            rows = conn.execute(
                "SELECT ts, cpu_pct, ram_pct, disk_pct, gpu_pct, gpu_temp_c, net_rx_bps, net_tx_bps, logins_count "
                "FROM machine_samples WHERE machine_id = ? AND ts >= ? ORDER BY ts DESC LIMIT ?",
                (machine_id, cutoff, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_machine(self, machine_id: str) -> bool:
        if not machine_id or machine_id == "local":
            return False
        with db.connect(self.data_dir) as conn:
            cur = conn.execute("DELETE FROM machines WHERE id = ?", (machine_id,))
            conn.execute("DELETE FROM machine_samples WHERE machine_id = ?", (machine_id,))
        return cur.rowcount > 0

    def list_machines(self, *, now: float | None = None, refresh_local: bool = True) -> list[dict[str, Any]]:
        """Return `[local_machine, *remote_machines]` with live online/offline status."""
        ts = time.time() if now is None else float(now)
        if refresh_local or self._local_metrics is None:
            local_m = self.refresh_local(now=ts)
            local_seen = ts
        else:
            with self._lock:
                local_m = self._local_metrics or {}
                local_seen = self._local_seen or ts

        cards: list[dict[str, Any]] = [{
            "id": "local",
            "name": local_m.get("hostname") or "This PC",
            "local": True,
            "status": "online",
            "os_info": local_m.get("os") or "",
            "last_seen": local_seen,
            "age_s": max(0.0, ts - local_seen),
            "metrics": local_m,
        }]

        with db.connect(self.data_dir) as conn:
            rows = conn.execute(
                "SELECT id, name, os_info, last_seen, metrics_json, created_at "
                "FROM machines ORDER BY name COLLATE NOCASE"
            ).fetchall()

        for row in rows:
            last_seen = float(row["last_seen"] or 0.0)
            try:
                metrics = json.loads(row["metrics_json"] or "{}")
            except ValueError:
                metrics = {}
            if last_seen <= 0.0:
                status = "pending"
                age_s = None
            elif ts - last_seen <= self.offline_after:
                status = "online"
                age_s = max(0.0, ts - last_seen)
            else:
                status = "offline"
                age_s = max(0.0, ts - last_seen)
            cards.append({
                "id": row["id"],
                "name": row["name"],
                "local": False,
                "status": status,
                "os_info": row["os_info"] or metrics.get("os") or "",
                "last_seen": last_seen,
                "age_s": age_s,
                "metrics": metrics,
            })
        return cards
