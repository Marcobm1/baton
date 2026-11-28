# Town Hall Audit (2026-11-28 · `00df1695b2bb`)

- **Topic:** *Revision general de la pagina, fallos, bugs, psobiles mejoras, futura tareas.*
- **Session ID:** `00df1695b2bb` (`2026-11-28 14:56 · open_floor`)
- **Participants:**
  - `opencode-go/gemini-4-argon` (OpenCode #1)
  - `opencode-go/glm-5.3` (OpenCode #2)
  - `opencode-go/deepseek-v4-pro` (4th guest elected by Borda consensus)
- **Workspace:** `Marcobm1/baton`

This document distills the technical findings, verified non-issues, and concrete implementation specs from the 2026-11-28 Town Hall audit so agents picking up the resulting `STATUS.md` tasks (`[GN-41]` – `[GN-43]`, `[GN-33]`) have exact file and line pointers.

---

## 1. Silent exception handling & background loop isolation (`[GN-41]`)

### 1.1 Unisolated steps in `_quota_tick()` (`baton/web.py:231-261`)
- **Problem:** `_loop()` calls `_quota_tick()` inside a single `try / except Exception: pass` (`baton/web.py:258-261`). Inside `_quota_tick()`, `ctx.refresh_usage()`, `machines.list_machines(refresh_local=True)`, `alerts.evaluate(...)`, `scheduler.tick()`, `townhall_svc.tick_schedules()`, `ctx.refresh_go()`, and `ctx.refresh_gemini()` run sequentially without per-step error isolation.
- **Impact:** If `ctx.refresh_usage()` or `machines.list_machines()` raises an exception (e.g. a locked SQLite database or malformed `.jsonl`), the remaining steps in `_quota_tick()` are skipped for that minute—halting `scheduler.tick()` and `townhall_svc.tick_schedules()` silently while `_loop()` swallows the traceback. By contrast, `thermal_loop()` right below (`baton/web.py:280-281`) logs with `logging.getLogger(__name__).exception(...)`.
- **Fix:**
  1. Wrap each independent step inside `_quota_tick()` in its own `try / except Exception:` block that logs via `logging.getLogger(__name__).exception(...)`.
  2. In `_loop()` (`baton/web.py:260-261`), log unexpected top-level failures with `logging.getLogger(__name__).exception("Background quota tick failed")` instead of bare `pass`.

### 1.2 Swallowed background errors in `AppContext` (`baton/routes/deps.py:132-148, 194-205`)
- **Problem:**
  - In `schedule_usage_refresh()` (`baton/routes/deps.py:132-143`), if `usage.collect(...)` raises, `except Exception: pass` leaves `self.usage_cache` untouched (or `None`), so `token_usage()` returns `{"days": 7, "apps": []}` with no distinction between "still loading" and "collection failed".
  - In `schedule_gemini_refresh()` (`baton/routes/deps.py:194-200`), if `fetch_gemini_quota(...)` raises an unexpected exception, `except Exception: pass` discards it instead of recording the failure in `self.gemini_state["msg"]` (unlike `quotas.py:179-180`, which reports `f"no answer ({type(e).__name__})"`).
- **Fix:**
  1. In `schedule_usage_refresh()`, on exception store `"error": f"{type(e).__name__}: {e}"` (or `type(e).__name__`) inside `self.usage_cache` alongside `"data": {"days": 7, "apps": [], "error": ...}` and log a warning so callers/tests can see why usage collection failed.
  2. In `schedule_gemini_refresh()`, on exception update `self.gemini_state.update(at=time.time(), msg=f"no answer ({type(e).__name__})")`.

### 1.3 Tri-state `local_gpu_busy` for `gpu_unusual` alerts (`baton/routes/machines.py:25-30`, `baton/web.py:235-243`, `baton/alerts.py:242-341`)
- **Problem:** Both `_local_gpu_busy(ctx)` (`baton/routes/machines.py:25-30`) and `_quota_tick()` (`baton/web.py:235-238`) catch `Exception` when calling `models.overview(...)` and return `False`.
- **Semantic bug:** In `AlertEngine.evaluate()` (`baton/alerts.py:328-340`), `local_gpu_busy` guards the `gpu_unusual` alert (*"Sustained GPU activity with no Huo model active"*). Returning `False` when `models.overview()` fails tells `AlertEngine` that **no local model is using the GPU**, which can trigger a false-positive `gpu_unusual` alert while a local model actually is running (or conversely, misrepresent an unknown state as known-idle).
- **Fix:**
  1. Change `local_gpu_busy` to tri-state `bool | None` (`True` = busy, `False` = idle, `None` = unknown because `models.overview()` raised).
  2. In `AlertEngine.evaluate()` (`baton/alerts.py:328-340`), when `m.get("local") and local_gpu_busy is None`, skip evaluating/resolving `gpu_unusual` for that tick rather than treating unknown as idle (`False`).

### 1.4 Warning logs on `notify.py` and `runner.py` + AST test gate
- **`baton/notify.py:252-253`:** Log a warning (`_log.warning(...)`) if stamping `UPDATE alerts SET notified_at = ?` fails so repeated alert notifications are diagnosable.
- **`baton/runner.py:397-400`:** In `Runner._prompt()`, `self.model_choices()` only populates the `Recommended:` choices list in the prompt instructions for the *next* task (`build_prompt` in `baton/task_models.py`); it does **not** alter `task.model` for the task being launched. Do **not** abort the task if `self.model_choices()` fails—just log `_log.warning("Model catalog discovery for prompt choices failed", exc_info=True)` and fall back to `choices = ""`.
- **AST test gate (`tests/test_silent_exceptions.py`):** Parse `baton/**/*.py` with Python's `ast` module and fail if any `ExceptHandler` catching `Exception` or bare `except:` has a body consisting solely of `pass` (or `...`), unless the `except` line carries an explicit `# noqa: BLE001 — <reason>` comment explaining why swallowing without logging is intentional (e.g. `baton/alerts.py:79` notification callback isolation or `baton/agent_chat.py:229` reader loop resilience).
- **Bonus found while running `pytest` inside an OpenCode task (`baton/security.py` / `agent_env()`):** `Runner._run()` sets `OPENCODE_CONFIG_CONTENT` in the environment of OpenCode task subprocesses. Because `agent_env()` strips `OPENCODE_PERMISSION` but not `OPENCODE_CONFIG_CONTENT`, running `pytest` from inside an OpenCode task inherits `OPENCODE_CONFIG_CONTENT` into `agent_chat.chat_env("gemini")` and `local` runner tests (`tests/test_gemini.py:243`, `tests/test_security.py:81, 158`). Include `"OPENCODE_CONFIG_CONTENT"` in `agent_env()`'s strip list in `baton/security.py` so non-OpenCode child processes and tests run inside OpenCode tasks always start clean.

---

## 2. Town Hall session lifecycle & scheduled SSE parity (`[GN-42]`)

### 2.1 Unbounded retention in `TownHallService._active` (`baton/townhall_bridge.py:273, 317-342`)
- **Problem:** `start_session()` inserts `self._active[sid] = active` (`ActiveTownHall`, holding the full list of `events` dicts in memory for SSE clients). When `_worker()` finishes (`finally: active.mark_done()`), `sid` is never evicted from `self._active`.
- **Fix:** Evict finished entries from `self._active` once a retention cap (e.g. keep at most the 20 most recent finished sessions, or prune finished sessions older than a short grace window when inserting/querying) is exceeded, while always keeping active (`not r.done`) sessions. When `get_active(sid)` returns `None`, `/api/townhall/{sid}/events` (`baton/routes/townhall.py:413-415`) already falls back cleanly to `store.get(sid)`.

### 2.2 Scheduled runs bypass `_active` (`baton/townhall_bridge.py:360-372`)
- **Problem:** `tick_schedules()` delegates directly to `TownHallScheduler(self.store, ...).tick()`, which runs sessions synchronously inside the scheduler without registering an `ActiveTownHall` in `self._active`. As a result, while a scheduled review is running:
  - `active_count()` returns `0`,
  - `/api/townhall/{sid}/events` has no live SSE stream, and
  - `cancel_session(sid)` cannot cancel it (unlike clicking **Run now**, which calls `trigger_schedule_now()` → `start_session()`).
- **Fix:** Have `tick_schedules()` check due schedules (or hook `start_session` / register an `ActiveTownHall` in `self._active`) so scheduled sessions get live SSE events, accurate `active_count()`, and cooperative cancellation without blocking `_quota_tick()` for up to `max_duration_seconds`.

---

## 3. Task Map (`?tab=map`) Markdown drawer & SSE stream resilience (`[GN-43]`)

### 3.1 Leading blank lines rejected in `parse_status_map(..., strict=True)` (`baton/task_map.py:381-387`)
- **Problem:** The editable `STATUS.md` drawer in TM-3 validates submitted `## Next` Markdown with `parse_status_map(markdown, owner_tag=..., strict=True)` (`baton/routes/projects.py:266`). Line 386 checks:
  ```python
  if strict and (not span or span[:3] != (0, 1, len(source.splitlines())) or span[3].strip() != "## Next"):
      raise ValueError("Submit only the ## Next section, starting with its heading.")
  ```
  If the user leaves any leading blank lines or whitespace before `## Next` in the drawer textarea, `span[0]` is `> 0` and the save is rejected with `400 Bad Request`.
- **Fix:** When `strict=True`, strip leading blank lines (`source = source.lstrip("\r\n")`, or normalize leading whitespace-only lines) before computing `_find_next_span(source)` and `len(source.splitlines())`.

### 3.2 Transient `ctx.hub.load()` errors drop `/api/projects/{pid}/map/stream` (`baton/routes/projects.py:167-189`)
- **Problem:** Inside the SSE generator `stream()`, lines 185–188 call `projects, _ = await ctx.hub.load()` on every poll tick (`task_sse_poll`) and terminate the stream (`if current is None: return`) if the project is missing or if `ctx.hub.load()` raises on a transient GitHub API error when the cache expires.
- **Fix:** Wrap `await ctx.hub.load()` inside the SSE loop in `try / except Exception:`; if it raises or temporarily fails to return `pid`, keep the last known `current` project snapshot so active local `TaskStore` status updates and keepalives continue uninterrupted.

---

## 4. Input to `TM-4` (`[GN-33]`): explicit task-to-map primary keys

- In `baton/task_map.py:876` (`match_running_tasks`), matching a running `Task` back to a map node currently relies on regex/title heuristics (`[ID]` in `task.title` or normalized title equality). If a user edits the task title on `/tasks/new` before launching, the map loses track of the running task.
- When implementing `[GN-33]` (`TM-4: Dependency-aware runner & safe chain execution`), bind tasks explicitly by adding `status_project_id: str = ""` and `status_task_id: str = ""` (or `project_id` / `map_task_id`) to `Task` in `baton/task_store.py` + SQLite migration, and prioritize exact `status_task_id` matching in `match_running_tasks()` before falling back to title heuristics.

---

## 5. Verified non-issues & backlog notes

- **Verified solid (do not re-audit):**
  - `baton/auth.py` (constant-time HMAC verification, login rate limiter, `SameSite=Strict` cookies).
  - HTML templates (escaped rendering, no unsafe `innerHTML` sinks in server templates).
  - `runner.on_finished` wiring in `baton/web.py:154, 295-300, 313` (base notifier is always attached even when `updater.enabled` is false, and restored on shutdown).
- **Future backlog (`docs/ideas.md`):**
  - Idea #1 (*Approve agent permissions directly from Telegram*) was highlighted by the council as the highest-ROI security/UX idea once the current Task Map and reliability items are done.
  - Added Idea #5: optional external `agy` CLI session token accounting in `baton/usage.py` if Antigravity exposes local numerical session logs in the future (keeping `usage.py`'s numbers-only privacy rule).
