"""Huo host bridge for the open-source `townhall` library.

Mounts `TownHallStore`, `TownHallEngine`, and `TownHallScheduler` inside Huo
with:
  - Durable SQLite storage at `<data_dir>/townhall.sqlite3`,
  - Live per-session event buffers for FastAPI SSE streaming,
  - Cooperative cancellation,
  - Automatic Telegram/Webhook completion notifications via `Notifier`,
  - Conversion of any Town Hall conclusion into a `/tasks/new` draft.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from townhall import (
    ClaudeCLIAdapter,
    CodexCLIAdapter,
    DemoAdapter,
    GeminiCLIAdapter,
    OpenCodeCLIAdapter,
    Participant,
    ParticipantAdapter,
    ScheduleSpec,
    SessionConfig,
    SessionEvent,
    SessionRecord,
    TownHallEngine,
    TownHallScheduler,
    TownHallStore,
)


DEFAULT_GUEST_POOL = (
    "opencode-go/deepseek-v4-pro",
    "opencode-go/qwen4-max",
    "opencode-go/kimi-k3.1-pro",
    "opencode-go/glm-5.3",
)


@dataclass
class ActiveTownHall:
    """In-memory state for a currently running Town Hall session."""

    session_id: str
    topic: str
    mode: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)

    def push(self, ev: SessionEvent) -> None:
        with self.cond:
            self.events.append(ev.to_dict())
            if ev.type in ("session_completed", "session_cancelled", "session_failed"):
                self.done = True
            self.cond.notify_all()

    def mark_done(self) -> None:
        with self.cond:
            self.done = True
            self.cond.notify_all()


def pick_strong_model(catalog: dict[str, dict], preferred_tokens: tuple[str, ...]) -> str:
    """Choose the strongest matching model ID from a CLI catalog, or the first entry."""
    if not catalog:
        return ""
    keys = list(catalog.keys())
    for token in preferred_tokens:
        for k in keys:
            if token in k.casefold():
                return k
    return keys[0]


def build_core_participants(
    *,
    gemini_model: str = "",
    codex_model: str = "",
    claude_model: str = "",
) -> list[Participant]:
    return [
        Participant(id="gemini", name="Gemini", provider="gemini", model=gemini_model, role="core"),
        Participant(id="codex", name="Codex", provider="codex", model=codex_model, role="core"),
        Participant(id="claude", name="Claude", provider="claude-code", model=claude_model, role="core"),
    ]


def townhall_task_draft(
    record: SessionRecord,
    *,
    repo: str = "",
    lang: str = "en",
) -> dict[str, str]:
    """Turn a completed Town Hall session into a prefilled `/tasks/new` draft."""
    es = lang == "es"
    title = f"Town Hall: {record.topic}".strip()[:160]
    parts: list[str] = [
        f"Objetivo debatido en Town Hall ({record.id}):\n{record.topic}"
        if es else
        f"Topic deliberated in Town Hall ({record.id}):\n{record.topic}"
    ]
    if record.conclusion:
        c = record.conclusion
        parts.append(
            (f"Conclusión del consejo ({c.moderator_name}):\n{c.conclusion}"
             if es else f"Council conclusion ({c.moderator_name}):\n{c.conclusion}")
        )
        if c.summary and c.summary != c.conclusion:
            parts.append((f"Resumen:\n{c.summary}" if es else f"Summary:\n{c.summary}"))
        if c.agreements:
            hdr = "Acuerdos:" if es else "Agreements:"
            parts.append(hdr + "\n" + "\n".join(f"- {a}" for a in c.agreements))
        if c.disagreements:
            hdr = "Discrepancias / matices a tener en cuenta:" if es else "Open disagreements / caveats:"
            parts.append(hdr + "\n" + "\n".join(f"- {d}" for d in c.disagreements))
        if c.action_items:
            hdr = "Plan de acción priorizado:" if es else "Prioritized action plan:"
            parts.append(hdr + "\n" + "\n".join(f"{i}. {a}" for i, a in enumerate(c.action_items, 1)))
    if record.guest_selection:
        g = record.guest_selection
        parts.append(
            f"4º modelo invitado por consenso ({g.method}): {g.chosen_model}"
            if es else
            f"4th guest model chosen by consensus ({g.method}): {g.chosen_model}"
        )
    return {
        "repo": repo,
        "title": title,
        "details": "\n\n".join(parts)[:12000],
    }


class TownHallService:
    """Per-app Town Hall manager owned by `AppContext` (no global singletons)."""

    def __init__(
        self,
        data_dir: Path,
        *,
        on_finished: Callable[[SessionRecord, str], None] | None = None,
        adapter: ParticipantAdapter | dict[str, ParticipantAdapter] | None = None,
        find_claude: Callable[[], str | None] | None = None,
        find_codex: Callable[[], str | None] | None = None,
        find_agy: Callable[[], str | None] | None = None,
        find_opencode: Callable[[], str | None] | None = None,
    ):
        self.store = TownHallStore(Path(data_dir) / "townhall.sqlite3")
        self._on_finished = on_finished
        self._custom_adapter = adapter
        self._find_claude = find_claude
        self._find_codex = find_codex
        self._find_agy = find_agy
        self._find_opencode = find_opencode
        self._lock = threading.Lock()
        self._active: dict[str, ActiveTownHall] = {}
        self._sched_lock = threading.Lock()

    def _resolve_adapter(self, *, demo: bool = False) -> ParticipantAdapter | dict[str, ParticipantAdapter]:
        if demo:
            return DemoAdapter()
        if self._custom_adapter is not None:
            return self._custom_adapter
        return {
            "claude-code": ClaudeCLIAdapter(binary=self._find_claude() if self._find_claude else None),
            "codex": CodexCLIAdapter(binary=self._find_codex() if self._find_codex else None),
            "gemini": GeminiCLIAdapter(binary=self._find_agy() if self._find_agy else None),
            "opencode": OpenCodeCLIAdapter(binary=self._find_opencode() if self._find_opencode else None),
        }

    def get_active(self, session_id: str) -> ActiveTownHall | None:
        with self._lock:
            return self._active.get(session_id)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._active.values() if not r.done)

    def start_session(
        self,
        config: SessionConfig,
        *,
        demo: bool = False,
        schedule_name: str = "",
    ) -> str:
        sid = uuid.uuid4().hex[:12]
        active = ActiveTownHall(session_id=sid, topic=config.topic.strip(), mode=config.mode)
        stub = SessionRecord(
            id=sid,
            topic=config.topic.strip(),
            mode=config.mode,
            status="running",
            workspace=config.workspace,
            language=config.language,
            participants=list(config.participants),
            started_at=time.time(),
        )
        self.store.save(stub)
        with self._lock:
            self._active[sid] = active

        adapter = self._resolve_adapter(demo=demo)

        def _worker() -> None:
            rec: SessionRecord | None = None
            try:
                engine = TownHallEngine(adapter, on_event=active.push)
                rec = engine.run(config, session_id=sid, cancel_event=active.cancel_event)
                self.store.save(rec)
            except Exception as exc:
                stub.status = "failed"
                stub.error = str(exc) or exc.__class__.__name__
                stub.finished_at = time.time()
                self.store.save(stub)
                rec = stub
            finally:
                active.mark_done()
            if rec is not None and rec.status in ("completed", "failed") and self._on_finished is not None:
                try:
                    self._on_finished(rec, schedule_name)
                except Exception:
                    pass

        threading.Thread(target=_worker, name=f"huo-townhall-{sid}", daemon=True).start()
        return sid

    def cancel_session(self, session_id: str) -> bool:
        active = self.get_active(session_id)
        if active is None or active.done:
            return False
        active.cancel_event.set()
        return True

    def trigger_schedule_now(self, schedule_id: str, *, demo: bool = False) -> str | None:
        spec = self.store.get_schedule(schedule_id)
        if spec is None:
            return None
        cfg = replace(spec.to_session_config(), isolate_workspace=not demo)
        sid = self.start_session(cfg, demo=demo, schedule_name=spec.name)
        self.store.mark_schedule_run(spec.id, time.time(), sid)
        return sid

    def tick_schedules(self) -> list[SessionRecord]:
        """Run any due recurring schedules without blocking concurrent ticks."""
        if not self._sched_lock.acquire(blocking=False):
            return []
        try:
            sched = TownHallScheduler(
                self.store,
                self._resolve_adapter(demo=False),
                on_finished=lambda sp, rec: self._on_finished(rec, sp.name) if self._on_finished else None,
            )
            return sched.tick()
        finally:
            self._sched_lock.release()

    def stop_all(self) -> None:
        with self._lock:
            for active in self._active.values():
                active.cancel_event.set()
                active.mark_done()


def build_schedule_spec(
    *,
    name: str,
    topic: str,
    participants: list[Participant],
    workspace: str = "",
    guest_pool: list[str] | None = None,
    mode: str = "council",
    debate_rounds: int = 1,
    max_duration_seconds: float = 600.0,
    moderator_id: str = "claude",
    interval_hours: float = 168.0,
    allowed_hours: str = "",
    language: str = "es",
) -> ScheduleSpec:
    return ScheduleSpec(
        id=uuid.uuid4().hex[:10],
        name=name,
        topic=topic,
        participants=participants,
        mode="open_floor" if mode == "open_floor" else "council",
        workspace=workspace,
        guest_pool=list(guest_pool or ()),
        debate_rounds=max(0, min(3, int(debate_rounds))),
        max_duration_seconds=max(30.0, min(3600.0, float(max_duration_seconds))),
        moderator_id=moderator_id or "claude",
        interval_hours=max(1.0, float(interval_hours)),
        allowed_hours=allowed_hours.strip(),
        language=language,
    )
