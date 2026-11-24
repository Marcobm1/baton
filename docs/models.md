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
- Levels turn amber/red at: CPU 85/97 %, RAM 85/95 %, disk 90/97 % used,
  GPU 75/85 °C, VRAM 90/98 %.
