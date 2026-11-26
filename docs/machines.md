# Machine Monitoring & Alerts

Baton monitors **this computer** automatically and can receive read-only
health heartbeats from extra computers or servers (over Tailscale or HTTPS).

## Phase M1: Local + remote machines & push agent (2026-11-25)

- **Push architecture (read-only):** remote machines run
  `baton monitor-agent --url <BATON_URL> --token <TOKEN>` (sending JSON metrics
  every 30 s to `POST /api/machines/heartbeat`). No inbound port is opened on
  remote machines, and the agent never executes commands from Baton.
- **Per-machine tokens (`baton/machines.py`):** registering a machine on
  `/machines` generates a random 256-bit token shown **once** and stores only
  its **SHA-256 hash** in `baton.sqlite3` (`machines` table). The HTTP auth
  middleware lets `POST /api/machines/heartbeat` validate that Bearer token
  directly while keeping every other UI and API route locked behind
  `BATON_ACCESS_KEY`. A machine token can never access tasks, models, or chat.
- **Rolling samples (`machine_samples`):** both the local PC (`local`) and
  remote machines record compact 24-hour samples (CPU, RAM, disk, GPU util/temp,
  network RX/TX, active login count) in `baton.sqlite3` for Phase M2 alerting
  and anomaly baselines.
- **UI (`/machines`):** dedicated sidebar tab showing **This PC** plus any
  registered remote machines (`Online` / `Offline` after 3 min without
  heartbeats / `Waiting for agent`), refreshing live every 5 seconds via
  `/api/machines`.

## Phase M2: Alert engine & history (2026-11-25)

- **SQLite tables (`baton/db.py`):** `alerts` (active, resolved, acknowledged,
  plus `notified_at` ready for Phase M3) and `known_logins` (per-machine
  baseline of `user@host` sessions).
- **Anti-spam rules (`baton/alerts.py`):**
  - **2-strike confirmation** on metric thresholds so brief spikes never noise.
  - **Hysteresis** (separate open vs clear values: GPU temp 80/85 °C open, <75 °C
    clear; CPU temp 85/95 °C open, <80 °C clear; RAM 90/96% open, <85% clear;
    disk 92/97% open, <88% clear; unusual GPU ≥70% when no Baton model is active,
    <40% clear; sustained network spike ≥25 MiB/s and ≥5× recent median).
  - **Cooldown (5 min)** after resolution prevents rapid flapping.
  - **Immediate rules:** `offline` (auto-resolves on return) and `login_unusual`
    (seeds existing logins silently on first run, alerts immediately on any new
    `user@host`, and stays open until acknowledged).
- **UI & API (`baton/routes/machines.py`, `base.html`, `machines.html`):**
  - Sidebar badge on **Machines** showing unacknowledged active alerts across
    every page, active alerts panel with **Acknowledge** / **Acknowledge all**
    (CSRF-protected), collapsible resolved history, and `GET /api/alerts`.

## Phase M3: Telegram & Webhook notifications (2026-11-25)

- **Notifier (`baton/notify.py`):** pluggable async notification sender using
  `httpx` (zero extra dependencies), configured via `.env`:
  - **Telegram Bot:** `BATON_TELEGRAM_BOT_TOKEN` + `BATON_TELEGRAM_CHAT_ID`
    (100% free, official, no templates or inbound webhook).
  - **Generic Webhook / ntfy (optional):** `BATON_ALERT_WEBHOOK_URL` (sends JSON
    payload + `Title` header).
  - **Task completion toggle:** `BATON_NOTIFY_TASKS=1` (default) notifies on
    `done` (merged or open PR) and `failed` tasks via `runner.on_finished`,
    skipping `cancelled` tasks.
- **Security:** all `BATON_*` notification variables are stripped from every
  agent subprocess by `agent_env()`, and bot tokens / webhook URLs are always
  masked before returning or logging any HTTP error.
- **UI:** `/machines` shows configured channels and a CSRF-protected
  **Send test notification** button (`POST /machines/notify/test`), and
  successful alert deliveries stamp `alerts.notified_at` in `baton.sqlite3`.
  (A conversational Telegram assistant using OpenCode models is intentionally
  kept as a separate project so it stays isolated from Baton's credentials.)
