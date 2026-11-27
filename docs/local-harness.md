# Local model tool-calling harness

Baton's **Chat** page (`Agent → Local model`) stays completely offline by default:
`POST /api/chat/{model_id}` simply relays messages to your local `llama-server` on
`127.0.0.1` with zero tools and zero network access.

When you explicitly switch **Tools** from **Offline** to **Internet + tools**,
Baton enables a native OpenAI-compatible tool-calling harness
(`baton/local_harness.py`) for that conversation.

## How the harness loop works

A local LLM never executes code or makes HTTP requests itself. Instead:

1. **Tool advertisement:** Baton sends the JSON Schema definitions of the active
   tools in the `tools` field (`tool_choice: "auto"`) to
   `http://127.0.0.1:<port>/v1/chat/completions` (`stream: true`).
2. **Streaming & tool-call capture:** Baton streams `reasoning_content` and text
   `content` tokens live to the browser over SSE while accumulating any
   incremental `delta.tool_calls` chunks by `index`.
3. **Execution & confirmation:** When the model requests one or more tools,
   Baton validates the arguments, pauses for interactive **Allow / Deny** on any
   sensitive action, executes the tool on the server, and appends each result as
   a `{"role": "tool", "tool_call_id": ..., "content": ...}` message.
4. **Next round & synthesis:** Baton calls `llama-server` again with the updated
   message list (compacting older `role: "tool"` outputs beyond the most recent
   4 so 32K local contexts do not overflow) until the model writes its final
   answer, capped at `MAX_TOOL_ROUNDS = 8` plus one closing synthesis round with
   an explicit instruction to stop calling tools (`tool_choice: "none"`, with
   automatic fallback if a build rejects `"none"`). A streaming `<tool_call>`
   filter strips any leaked/truncated XML/JSON tool tags from plain text and can
   parse complete Qwen3.8 XML or JSON `<tool_call>` blocks as a fallback during
   normal rounds.

> **Note for `llama-server`:** modern `llama.cpp` builds parse tool calls using
> the model's Jinja chat template (`--jinja`, enabled by default on recent
> releases for models such as Qwen3.8 and Inkling 2 Mini). If a model is launched
> without tool-template support, Baton surfaces `llama-server`'s error with a
> hint to check `--jinja`.

## Available tools

### Always enabled when Tools = Internet + tools

| Tool | Kind | Confirmation | What it does |
|---|---|---|---|
| `web_search` | search | Direct | Searches the web using (in priority order): `BATON_SEARCH_URL` (local SearXNG JSON), `BATON_BRAVE_API_KEY` (Brave Search API), `BATON_TAVILY_API_KEY` (Tavily API), or DuckDuckGo HTML fallback (fails honestly on HTTP 202/anti-bot challenges). |
| `fetch_url` | fetch | Direct | Downloads a public `http://` or `https://` page (up to 512 KiB, text/HTML/JSON/XML only; default 6,000 chars, max 12,000) and extracts clean readable text. |
| `get_datetime` | utility | Direct | Returns the current local and UTC date, time, weekday, and timezone. |
| `calculate` | utility | Direct | Evaluates mathematical expressions using Python's `ast` module (zero `eval()`, strict node/exponent limits). |

### Enabled only when an allowlisted Folder is selected

The folder picker only offers Baton's known clones (`BATON_LOCAL_ROOT`,
`BATON_LOCAL_PATHS`) and the optional scratch folder (`BATON_CHAT_SCRATCH`). The
browser never sends arbitrary filesystem paths.

| Tool | Kind | Confirmation | What it does |
|---|---|---|---|
| `list_files` | read | Direct | Lists files and subdirectories inside the chosen folder (skips `.git`). |
| `read_file` | read | Direct (**Allow / Deny** for `.env*`) | Reads numbered lines from a UTF-8 text file inside the chosen folder (up to 2 MiB, refuses binaries). |
| `search_files` | search | Direct | Searches text or regex across files inside the folder (skips `.git`, `node_modules`, `.venv`, `__pycache__`, and `.env*`). |
| `write_file` | edit | **Always Allow / Deny** | Shows a line-by-line diff preview; writes UTF-8 content inside the folder only after approval. |
| `run_command` | execute | **Always Allow / Deny** | Shows the exact command; executes inside the folder with `agent_env()`, 60 s timeout, 64 KiB output cap, and process-tree teardown. |

Consecutive read-only / search / fetch / utility calls are collapsed into a
single expandable `.ag-tool-group` summary in `/chat`, while writes, commands,
and **Allow / Deny** cards stay prominent.

## Security boundaries and honest limits

### What Baton enforces

- **Off by default:** reloading `/chat` always starts in **Offline** mode so
  regular local chats never expose tools or web access accidentally.
- **SSRF guard on `fetch_url`:** only `http://` and `https://` without embedded
  credentials are allowed. Hostnames are resolved before connecting and every
  resolved IP (including IPv4-mapped IPv6 such as `::ffff:127.0.0.1`) is checked
  to block loopback (`127.0.0.0/8`, `::1`), private LAN ranges (`10/8`,
  `172.16/12`, `192.168/16`, `fc00::/7`), link-local / cloud metadata
  (`169.254.169.254`), multicast, and reserved ranges. Redirects are followed
  manually (max 5 hops) and re-validated on every hop so a public site cannot
  redirect the model to `http://127.0.0.1:8787/`.
- **Path confinement:** every workspace tool resolves canonical paths with
  `os.path.realpath` and verifies `is_relative_to(workspace_root)` to block
  `../` traversal and symlink escapes.
- **Secret isolation:** `run_command` runs with `agent_env()`, stripping all
  `BATON_*`, `GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_PAT`, `ANTHROPIC_API_KEY`, and
  `OPENCODE_PERMISSION` variables. `search_files` skips `.env*` files, and
  `read_file` asks Allow/Deny before opening `.env*`.
- **One-shot CSRF-protected permissions:** every `Allow / Deny` prompt has a
  random per-run ID bound to that single `run_id`, works at most once, expires
  automatically after 10 minutes (default Deny), and triggers Baton's delayed
  mobile approval alert (>30 s) if Telegram or a Webhook is configured.
- **Abliterated models:** abliterated/uncensored local models can use the
  harness in controlled situations, display an extra caution banner, and
  **never** receive unattended/full permissions — sensitive actions always
  require explicit owner confirmation.
- **Untrusted content framing:** `web_search` and `fetch_url` outputs are
  wrapped with an explicit untrusted-data banner as defense-in-depth against
  indirect prompt injection.

### Honest limits (why this is for controlled situations)

- **Prompt injection cannot be fully solved at the prompt layer:** a malicious
  web page can try to trick a local model (especially an abliterated one) into
  calling `write_file` or `run_command` with harmful arguments, or into
  exfiltrating workspace text via a crafted `web_search` / `fetch_url` query.
  Read every diff and command carefully before clicking **Allow**, and avoid
  attaching sensitive folders when browsing untrusted pages.
- **No OS sandbox for `run_command`:** an *approved* shell command runs as your
  OS user inside the chosen folder and can reach anything your user can. For
  unattended autonomous coding with a non-abliterated local model, use
  **Agent → OpenCode** in **All permissions (Docker)** mode (`baton/sandbox.py`).
- **DNS rebinding:** `validate_public_url` validates DNS immediately before the
  request; a sophisticated attacker controlling a TTL=0 authoritative DNS server
  could still attempt a race, so always keep `BATON_ACCESS_KEY` set in `.env`.

## Reliable web search backends

Public DuckDuckGo HTML frequently rate-limits automated requests with HTTP 202
anti-bot challenges. Baton detects this honestly (failing the tool instead of
returning empty green results) and supports three reliable backends, checked in
this order:

### 1. Local SearXNG in Docker (recommended, free, zero API keys)

Start the hardened localhost-only SearXNG container shipped with Baton:

```bash
docker compose -f docker/searxng/docker-compose.yml up -d
```

Then add this line to Baton's `.env` and restart Baton:

```dotenv
BATON_SEARCH_URL=http://127.0.0.1:8888/search?format=json&q={query}
```

SearXNG listens strictly on `127.0.0.1:8888` with JSON output enabled
(`docker/searxng/settings.yml`).

### 2. Brave Search API or Tavily API (optional, without Docker)

If you prefer a cloud search API instead of running SearXNG locally, set one of
these in `.env`:

```dotenv
BATON_BRAVE_API_KEY=...
# or:
BATON_TAVILY_API_KEY=...
```

All `BATON_*` variables are masked in errors and stripped from all child
processes by `agent_env()`.
