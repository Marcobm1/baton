# Models page

`/models` shows this computer (CPU, RAM, free disk, uptime), its NVIDIA
graphics card (load, VRAM, temperature, power draw), a VRAM budget, and one
card per local model with **Start / Stop** and live statistics. It refreshes
itself every 5 seconds (`/api/models`, read by `static/models.js`).

## The model list: `~/.baton/models.json`

Baton never decides what to run from the web: the page only sends a model id.
The program, the model file and its arguments come from this file, which you
write on the computer itself (`BATON_DATA_DIR` changes the folder).

```json
{
  "server": "C:\\IA\\llama.cpp\\llama-server.exe",
  "models": [
    {
      "id": "inkling-2-mini",
      "name": "Inkling 2 Mini (official)",
      "description": "UD-Q4_K_XL, experts of layers 0-5 in RAM: about 40 tok/s.",
      "file": "C:\\IA\\modelos\\Inkling-2-Mini-UD-Q4_K_XL.gguf",
      "port": 8080,
      "vram_gb": 15.6,
      "args": ["--jinja", "-ngl", "99", "--n-cpu-moe", "6", "-c", "32768", "-np", "1"]
    }
  ]
}
```

- `server`: path to `llama-server` (if empty, the one on `PATH`).
- `id`: a-z, 0-9 and `-`; also used as the API model name (`--alias`).
- `port`: one per model, 1024-65535.
- `vram_gb`: what the model takes on the card once loaded (measure it with
  `nvidia-smi`). Used for the budget: a second model only starts if both fit.
- `args`: anything else for `llama-server`, as a list (no shell is involved).
  Baton always adds `-m <file> --alias <id> --metrics --host 127.0.0.1 --port <port>`,
  so `args` can't contain those options (or `--api-key`, which would lock Baton
  out of `/metrics`). A model started from Baton is never reachable from the network.

Problems in the file are listed on the page instead of breaking it.

## Chat page

`/chat` talks to any model in `models.json` from any device that can open
Baton (also the phone through Tailscale), because the browser never calls the
model's port: it posts to `/api/chat/<id>`, and Baton relays the request to
`127.0.0.1:<port>/v1/chat/completions` and streams the answer back as
server-sent events. The model keeps listening only on `127.0.0.1`.

- Only text messages (system/user/assistant) and a reasoning effort
  (`none`, `low`, `medium`, `high`) are forwarded; sampling and everything else
  come from the model's `args`, so each model keeps its own settings.
- The request needs the page's form token in an `X-Baton-CSRF` header, like
  every other change Baton makes; a stopped model answers 409.
- Stop closes the request; llama-server notices and stops generating.
- The conversation lives only in that browser (`localStorage`, one per model);
  "New chat" clears it. The page has no automatic reload, so nothing is lost.
- Answers are shown with a small built-in Markdown renderer that escapes
  everything first (no external libraries, no HTML from the model).
- A stopped model can be started from the chat; Baton comes back to the chat
  with that model selected and the page waits until it answers.

## Decisions

- **One `llama-server` process per model**, not llama-server's router mode.
  Each model keeps its own tuned options (`--n-cpu-moe`, context, sampling), and
  with a 16 GB card only one large model fits at a time anyway. The cost: a
  switch reloads the model (~25 s), which the router mode would also need when
  memory is short.
- **Detached processes.** Servers are started without a console, in their own
  process group and outside Baton's job object, so a Baton restart or
  self-update doesn't stop a running model. Baton remembers the PID *and* its
  start time in `~/.baton/models-state.json`, so a reused PID is never mistaken
  for the model. Output goes to `~/.baton/models/<id>.log`; if a model stops on
  its own, its card shows the last lines.
- **Servers started outside Baton** (for example with a script) are detected
  by the port: if a `llama-server` answers there, the card says so and Stop
  works. Baton never stops a different program that happens to use the port.
- **Statistics come from `llama-server --metrics`** (Prometheus text at
  `/metrics`): average generation and prompt speed since that server started,
  requests in progress, and tokens served. The counters restart with the
  server, so Baton adds the last reading to a running total; "tokens generated"
  therefore adds up across restarts.
- **Readings** use `psutil` (CPU, RAM, disk, processes, ports) and
  `nvidia-smi` (GPU). CPU temperature is not shown: Windows only exposes it to
  administrator tools. Per-process VRAM isn't reported by Windows drivers, so
  the budget shows the measured total plus each model's `vram_gb` estimate.
- **Start button bug (fixed):** project cards make the whole card clickable
  with `.c-name::after`; on model cards that layer covered the Start button,
  so clicks never reached Baton (Stop worked because it had the `.btn` class,
  which sits above that layer). Model cards don't use it now.
- Levels turn amber/red at: CPU 85/97 %, RAM 85/95 %, disk 90/97 % used,
  GPU 75/85 °C, VRAM 90/98 %.
