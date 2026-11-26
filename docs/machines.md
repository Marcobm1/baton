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

## Upcoming phases

- **Phase M2 — Alert engine & history:** threshold + anomaly rules (offline,
  high GPU/CPU temperature, low RAM/disk, unexpected GPU load, sustained network
  spike, unusual login sessions) with hysteresis and cooldowns to avoid spam,
  plus sidebar badge and Acknowledge button.
- **Phase M3 — Mobile push notifications:** pluggable async senders via `.env`
  (WhatsApp via CallMeBot, Telegram Bot, and generic Webhook / ntfy) + "Send
  test alert" button.
