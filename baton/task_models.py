"""Model pickers from the installed CLIs. Discovery never submits a user prompt."""
import json
import re
import subprocess
import time

from .ping import _NO_WINDOW, _clean_env, find_claude, find_codex

_cache = {}


def catalog(agent: str) -> dict:
    now = time.monotonic()
    expires, previous = _cache.get(agent, (0, {}))
    if now < expires:
        return previous
    models = {}
    cmd = find_claude() if agent == "claude-code" else find_codex()
    if not cmd:
        return {}
    try:
        args = [cmd, "debug", "models"]
        request = ""
        if agent == "claude-code":
            args = [cmd, "-p", "--input-format", "stream-json", "--output-format",
                    "stream-json", "--verbose", "--no-session-persistence"]
            request = json.dumps({"type": "control_request", "request_id": "baton",
                                  "request": {"subtype": "initialize"}}) + "\n"
        result = subprocess.run(args, input=request, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=15,
                                env=_clean_env(), check=True, **_NO_WINDOW)
        if agent == "codex":
            rows = [(m["slug"], m["display_name"], [v["effort"] for v in m["supported_reasoning_levels"]])
                    for m in json.loads(result.stdout)["models"] if m.get("visibility") == "list"]
        else:
            rows = []
            for line in result.stdout.splitlines():
                message = json.loads(line)
                response = message.get("response", {})
                if message.get("type") == "control_response" and response.get("request_id") == "baton":
                    rows = [(m["value"], m["displayName"], m.get("supportedEffortLevels") or [])
                            for m in response["response"]["models"] if m["value"] != "default"]
                    break
        for value, label, efforts in rows:
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
                models[value] = {"label": label, "efforts": [e for e in efforts if re.fullmatch("[a-z]+", e)]}
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        pass
    _cache[agent] = (now + (600 if models else 30), models)
    return models
