"""Local models: start and stop llama-server models listed in models.json, and
read this computer's CPU, RAM and GPU plus each running server's /metrics.

The model list is a file the owner writes on this computer
(``<data dir>/models.json``, see docs/models.md). The web page only sends a
model id: the program, the model file and the arguments always come from that
file, and Baton always adds ``--host 127.0.0.1`` so a model started from here is
never reachable from the network.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil

CONFIG_NAME = "models.json"
STATE_NAME = "models-state.json"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
# Baton sets these itself (always local, always with metrics), so models.json
# can't change them. An API key would also lock Baton out of /metrics.
RESERVED = {"-m", "--model", "--host", "--port", "--alias", "-a", "--metrics",
            "--api-key", "--api-key-file"}
GPU_FIELDS = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
GIB = 1024 ** 3
# Windows: a console program started by pythonw would flash a window; this hides it.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BREAKAWAY_FROM_JOB = 0x01000000   # keep running when Baton's scheduled task restarts


class ModelError(Exception):
    """A start/stop that can't be done. ``key`` is a UI string, ``params`` fill it."""

    def __init__(self, key: str, **params):
        super().__init__(key)
        self.key, self.params = key, params

    def text(self, t: dict) -> str:
        return t[self.key].format(**self.params)


@dataclass
class Model:
    id: str
    name: str
    file: str
    port: int
    args: list[str] = field(default_factory=list)
    description: str = ""
    vram_gb: float = 0.0

    def command(self, server: str) -> list[str]:
        # The reserved options go last, after the owner's arguments.
        return [server, "-m", self.file, *self.args, "--alias", self.id, "--metrics",
                "--host", "127.0.0.1", "--port", str(self.port)]


@dataclass
class Config:
    path: Path
    found: bool = False
    server: str = ""
    models: list[Model] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def load_config(data_dir: Path) -> Config:
    cfg = Config(path=Path(data_dir) / CONFIG_NAME)
    if not cfg.path.exists():
        return cfg
    cfg.found = True
    try:
        raw = json.loads(cfg.path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        cfg.problems.append(f"{CONFIG_NAME}: {exc}")
        return cfg
    if not isinstance(raw, dict):
        cfg.problems.append(f"{CONFIG_NAME}: expected an object with \"server\" and \"models\"")
        return cfg
    cfg.server = str(raw.get("server") or shutil.which("llama-server") or "")
    if not cfg.server:
        cfg.problems.append("\"server\": the path to llama-server is missing")
    elif not Path(cfg.server).is_file():
        cfg.problems.append(f"\"server\": {cfg.server} not found")
    ids, ports = set(), set()
    items = raw.get("models") or []
    if not isinstance(items, list):
        cfg.problems.append("\"models\" must be a list")
        items = []
    for i, item in enumerate(items):
        where = f"models[{i}]"
        if not isinstance(item, dict):
            cfg.problems.append(f"{where}: expected an object")
            continue
        args = item.get("args", [])
        if not isinstance(args, list):
            cfg.problems.append(f"{where}: \"args\" must be a list of strings")
            continue
        try:
            model = Model(id=str(item["id"]), name=str(item.get("name") or item["id"]),
                          file=str(item["file"]), port=int(item["port"]),
                          args=[str(a) for a in args],
                          description=str(item.get("description", "")),
                          vram_gb=float(item.get("vram_gb") or 0))
        except (KeyError, TypeError, ValueError) as exc:
            cfg.problems.append(f"{where}: needs \"id\", \"file\" and \"port\" ({exc})")
            continue
        reserved = sorted({a.split("=", 1)[0] for a in model.args} & RESERVED)
        if not ID_RE.match(model.id):
            cfg.problems.append(f"{where}: id \"{model.id}\" must use a-z, 0-9 and -")
        elif model.id in ids:
            cfg.problems.append(f"{where}: id \"{model.id}\" is repeated")
        elif not 1024 <= model.port <= 65535:
            cfg.problems.append(f"{where}: port {model.port} must be between 1024 and 65535")
        elif model.port in ports:
            cfg.problems.append(f"{where}: port {model.port} is used by another model")
        elif reserved:
            cfg.problems.append(f"{where}: Baton sets {', '.join(reserved)} itself; remove it from \"args\"")
        else:
            ids.add(model.id)
            ports.add(model.port)
            cfg.models.append(model)
    return cfg


# ------------------------------------------------------------ readings
def parse_gpus(text: str) -> list[dict]:
    """nvidia-smi --query-gpu=<GPU_FIELDS> --format=csv,noheader,nounits"""
    def num(value: str) -> float | None:
        try:
            return float(value)
        except ValueError:          # "[N/A]" on cards that don't report it
            return None

    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 7:
            continue
        name, util, used, total, temp, power, limit = parts
        gpus.append({"name": name, "util": num(util), "vram_used_mib": num(used),
                     "vram_total_mib": num(total), "temp": num(temp),
                     "power": num(power), "power_limit": num(limit)})
    return gpus


def parse_metrics(text: str) -> dict[str, float]:
    """llama-server's Prometheus text: ``llamacpp:<name> <value>`` lines."""
    out = {}
    for line in text.splitlines():
        if line.startswith("llamacpp:"):
            name, _, value = line.partition(" ")
            try:
                out[name.removeprefix("llamacpp:")] = float(value)
            except ValueError:
                pass
    return out


def _health(port: int) -> str | None:
    """"ok" when the model answers, "loading" while it loads, None if nothing answers."""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
    except httpx.HTTPError:
        return None
    return "ok" if r.status_code == 200 else "loading" if r.status_code == 503 else None


def _metrics(port: int) -> dict[str, float]:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=1.0)
    except httpx.HTTPError:
        return {}
    return parse_metrics(r.text) if r.status_code == 200 else {}


def _listener(port: int) -> psutil.Process | None:
    """The process listening on 127.0.0.1/0.0.0.0:port, if any."""
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid:
                return psutil.Process(c.pid)
    except (psutil.Error, OSError):
        pass
    return None


def _is_llama_server(proc: psutil.Process) -> bool:
    try:
        return proc.name().lower().removesuffix(".exe") == "llama-server"
    except psutil.Error:
        return False


def _detach() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW | BREAKAWAY_FROM_JOB}
    return {"start_new_session": True}


def _tail(path: Path, lines: int = 12) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _fmt(value: float | None, digits: int, lang: str) -> str:
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}"
    return text.replace(",", " ").replace(".", ",") if lang == "es" else text


def _dur(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h" if d else f"{h}h {rem // 60:02d}m" if h else f"{rem // 60}m"


def _level(value: float | None, warn: float, bad: float) -> str:
    if value is None:
        return "ok"
    return "bad" if value >= bad else "warn" if value >= warn else "ok"


class LocalModels:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.lock = threading.Lock()
        self._gpu_at, self._gpu = 0.0, []
        psutil.cpu_percent(None)   # the first call only starts the measurement

    # ------------------------------------------------------------ state
    @property
    def state_path(self) -> Path:
        return self.data_dir / STATE_NAME

    def log_path(self, model_id: str) -> Path:
        return self.data_dir / "models" / f"{model_id}.log"

    def _state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    @staticmethod
    def _proc(entry: dict) -> psutil.Process | None:
        """The server Baton started, if it is still that same process (PIDs get reused)."""
        try:
            proc = psutil.Process(int(entry["pid"]))
            if abs(proc.create_time() - float(entry["created"])) < 1 and proc.status() != psutil.STATUS_ZOMBIE:
                return proc
        except (psutil.Error, KeyError, TypeError, ValueError):
            pass
        return None

    @staticmethod
    def _count(entry: dict, metrics: dict) -> dict:
        """Tokens served across restarts: the server's counters start at 0 each
        time, so the last reading before a restart is added to a running base."""
        base = entry.setdefault("base", {"prompt": 0, "generated": 0})
        if metrics:
            now = {"prompt": metrics.get("prompt_tokens_total", 0.0),
                   "generated": metrics.get("tokens_predicted_total", 0.0)}
            last = entry.get("last") or {"prompt": 0, "generated": 0}
            if now["prompt"] < last["prompt"] or now["generated"] < last["generated"]:
                for k in base:
                    base[k] += last.get(k, 0)
            entry["last"] = now
        last = entry.get("last") or {"prompt": 0, "generated": 0}
        return {k: base[k] + last.get(k, 0) for k in base}

    @staticmethod
    def _close_counts(entry: dict) -> None:
        base = entry.setdefault("base", {"prompt": 0, "generated": 0})
        for k, v in (entry.pop("last", None) or {}).items():
            base[k] = base.get(k, 0) + v

    # --------------------------------------------------------- readings
    def gpus(self) -> list[dict]:
        if time.time() - self._gpu_at < 2:
            return self._gpu
        exe = shutil.which("nvidia-smi")
        gpus = []
        if exe:
            try:
                out = subprocess.run([exe, f"--query-gpu={GPU_FIELDS}", "--format=csv,noheader,nounits"],
                                     capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
                gpus = parse_gpus(out.stdout) if out.returncode == 0 else []
            except (OSError, subprocess.SubprocessError):
                gpus = []
        self._gpu_at, self._gpu = time.time(), gpus
        return gpus

    @staticmethod
    def machine() -> dict:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(Path.home().anchor or "/")
        return {"host": platform.node(), "cpu": psutil.cpu_percent(None),
                "cores": psutil.cpu_count(logical=False), "threads": psutil.cpu_count(),
                "ram_used": vm.used, "ram_total": vm.total, "ram_pct": vm.percent,
                "disk_free": disk.free, "disk_total": disk.total, "disk_pct": disk.percent,
                "uptime": time.time() - psutil.boot_time()}

    def _rows(self, cfg: Config, state: dict) -> list[dict]:
        rows = []
        for m in cfg.models:
            entry = state.setdefault(m.id, {})
            proc = self._proc(entry) if entry.get("pid") else None
            crashed = ""
            if entry.get("pid") and proc is None:
                # It exited without Baton stopping it (crash, closed, reboot).
                for k in ("pid", "created", "started"):
                    entry.pop(k, None)
                self._close_counts(entry)
                entry["crashed"] = True
            if entry.get("crashed") and proc is None:
                crashed = _tail(self.log_path(m.id))
            other = None if proc else _listener(m.port)
            health = _health(m.port) if proc or other else None
            if proc:
                status = "running" if health == "ok" else "loading"
            elif other and _is_llama_server(other):
                status = "external" if health == "ok" else "loading"
            elif other:
                status = "port_busy"
            else:
                status = "stopped"
            metrics = _metrics(m.port) if health == "ok" and status != "port_busy" else {}
            served = self._count(entry, metrics)
            gen_s = metrics.get("tokens_predicted_seconds_total") or 0
            prompt_s = metrics.get("prompt_seconds_total") or 0
            rows.append({
                "id": m.id, "name": m.name, "description": m.description, "port": m.port,
                "vram_gb": m.vram_gb, "file": m.file, "file_name": Path(m.file).name,
                "file_ok": Path(m.file).is_file(), "status": status,
                "uses_gpu": status in ("running", "loading", "external"),
                "started": entry.get("started"), "crashed": crashed,
                "gen_tps": metrics.get("tokens_predicted_total", 0) / gen_s if gen_s else None,
                "prompt_tps": metrics.get("prompt_tokens_total", 0) / prompt_s if prompt_s else None,
                "busy": int(metrics.get("requests_processing", 0)),
                "served": served, "since_start": {
                    "prompt": metrics.get("prompt_tokens_total", 0),
                    "generated": metrics.get("tokens_predicted_total", 0)},
            })
        return rows

    def overview(self, lang: str = "en") -> dict:
        cfg = load_config(self.data_dir)
        with self.lock:
            state = self._state()
            rows = self._rows(cfg, state)
            self._save(state)
        machine, gpus = self.machine(), self.gpus()
        gpu = gpus[0] if gpus else None
        total_gb = gpu["vram_total_mib"] / 1024 if gpu and gpu["vram_total_mib"] else None
        used_gb = gpu["vram_used_mib"] / 1024 if gpu and gpu["vram_used_mib"] is not None else None
        reserved = sum(r["vram_gb"] for r in rows if r["uses_gpu"])
        f = lambda v, d=1: _fmt(v, d, lang)  # noqa: E731
        show = {
            "cpu": f"{f(machine['cpu'], 0)} %", "cpu_w": machine["cpu"],
            "ram": f"{f(machine['ram_used'] / GIB)} / {f(machine['ram_total'] / GIB)} GiB",
            "ram_w": machine["ram_pct"],
            "disk": f"{f(machine['disk_free'] / GIB, 0)} GiB", "disk_w": machine["disk_pct"],
            "uptime": _dur(machine["uptime"]),
        }
        levels = {"cpu": _level(machine["cpu"], 85, 97), "ram": _level(machine["ram_pct"], 85, 95),
                  "disk": _level(machine["disk_pct"], 90, 97)}
        if gpu:
            vram_pct = 100 * used_gb / total_gb if used_gb is not None and total_gb else None
            show.update({
                "gpu_load": f"{f(gpu['util'], 0)} %", "gpu_load_w": gpu["util"] or 0,
                "vram": f"{f(used_gb)} / {f(total_gb)} GiB", "vram_w": vram_pct or 0,
                "temp": f"{f(gpu['temp'], 0)} °C",
                "power": f"{f(gpu['power'], 0)} / {f(gpu['power_limit'], 0)} W",
                "power_w": 100 * gpu["power"] / gpu["power_limit"]
                if gpu["power"] is not None and gpu["power_limit"] else 0,
            })
            levels.update(temp=_level(gpu["temp"], 75, 85), vram=_level(vram_pct, 90, 98))
        for r in rows:
            p = f"m.{r['id']}."
            show.update({
                p + "gen": f"{f(r['gen_tps'])} tok/s", p + "prompt": f"{f(r['prompt_tps'], 0)} tok/s",
                p + "served": f"{f(r['served']['generated'], 0)}",
                p + "served_prompt": f"{f(r['served']['prompt'], 0)}",
                p + "busy": str(r["busy"]),
                p + "vram": f"{f(r['vram_gb'])} GiB" if r["vram_gb"] else "—",
                p + "uptime": _dur(time.time() - r["started"]) if r["started"] else "—",
            })
        return {"config": {"found": cfg.found, "path": str(cfg.path), "problems": cfg.problems},
                "machine": machine, "gpu": gpu, "levels": levels, "models": rows, "show": show,
                "budget": {"total_gb": total_gb, "used_gb": used_gb, "reserved_gb": reserved,
                           "parts": [{"id": r["id"], "name": r["name"], "gb": r["vram_gb"],
                                      "pct": 100 * r["vram_gb"] / total_gb if total_gb else 0}
                                     for r in rows if r["uses_gpu"]]},
                "at": time.time()}

    # ------------------------------------------------------- start / stop
    def start(self, model_id: str) -> None:
        cfg = load_config(self.data_dir)
        model = next((m for m in cfg.models if m.id == model_id), None)
        if model is None:
            raise ModelError("m_err_unknown")
        if not cfg.server or not Path(cfg.server).is_file():
            raise ModelError("m_err_server")
        if not Path(model.file).is_file():
            raise ModelError("m_err_file", file=model.file)
        view = self.overview()
        row = next(r for r in view["models"] if r["id"] == model_id)
        if row["status"] == "port_busy":
            raise ModelError("m_err_port", port=model.port)
        if row["uses_gpu"]:
            raise ModelError("m_err_running", name=model.name)
        # Two models only run together if their measured sizes fit in the card.
        total = view["budget"]["total_gb"]
        for other in (r for r in view["models"] if r["uses_gpu"]):
            if not total or not other["vram_gb"] or not model.vram_gb \
                    or view["budget"]["reserved_gb"] + model.vram_gb > total:
                raise ModelError("m_err_vram", other=other["name"], total=_fmt(total, 0, "en") if total else "?")
        log = self.log_path(model.id)
        log.parent.mkdir(parents=True, exist_ok=True)
        command = model.command(cfg.server)
        with self.lock, open(log, "ab") as fh:
            fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start\n".encode())
            fh.flush()
            kwargs = dict(stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                          cwd=str(Path(cfg.server).parent))
            try:
                proc = subprocess.Popen(command, **kwargs, **_detach())
            except OSError as exc:
                if os.name != "nt":
                    raise ModelError("m_err_start", error=str(exc)) from exc
                try:   # the job object may not allow breakaway: start it inside
                    proc = subprocess.Popen(command, **kwargs, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW)
                except OSError as exc2:
                    raise ModelError("m_err_start", error=str(exc2)) from exc2
            try:
                created = psutil.Process(proc.pid).create_time()
            except psutil.Error:
                created = time.time()
            state = self._state()
            entry = state.setdefault(model.id, {})
            entry.update(pid=proc.pid, created=created, started=time.time())
            entry.pop("crashed", None)
            self._save(state)

    def stop(self, model_id: str) -> None:
        cfg = load_config(self.data_dir)
        model = next((m for m in cfg.models if m.id == model_id), None)
        if model is None:
            raise ModelError("m_err_unknown")
        with self.lock:
            state = self._state()
            entry = state.setdefault(model.id, {})
            proc = self._proc(entry) if entry.get("pid") else None
            if proc is None:
                other = _listener(model.port)
                if other is None:
                    entry.pop("crashed", None)
                    self._save(state)
                    raise ModelError("m_err_not_running", name=model.name)
                if not _is_llama_server(other):
                    raise ModelError("m_err_not_ours", port=model.port)
                proc = other
            try:
                proc.terminate()
                proc.wait(10)
            except psutil.TimeoutExpired:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
            for k in ("pid", "created", "started", "crashed"):
                entry.pop(k, None)
            self._close_counts(entry)
            self._save(state)
