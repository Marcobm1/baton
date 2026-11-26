"""Read-only push monitoring agent (`baton monitor-agent`).

Collects CPU, RAM, disk, optional NVIDIA GPU, network rates, and active login
sessions on a computer or server and POSTs a JSON heartbeat to Baton's
`/api/machines/heartbeat` using a per-machine Bearer token.

The agent is strictly read-only: it never executes commands from Baton.
"""

from __future__ import annotations

import sys
import time

import httpx

from .machines import collect_host_metrics


def send_heartbeat(
    url: str,
    token: str,
    metrics: dict,
    *,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    endpoint = url.rstrip("/") + "/api/machines/heartbeat"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        if client is not None:
            resp = client.post(endpoint, json=metrics, headers=headers)
        else:
            resp = httpx.post(endpoint, json=metrics, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, str(exc)
    if 200 <= resp.status_code < 300:
        return True, "ok"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def run_agent(url: str, token: str, *, interval: float = 30.0, once: bool = False) -> int:
    if not url or not token:
        print("Error: --url (or BATON_URL) and --token (or BATON_AGENT_TOKEN) are required.", file=sys.stderr)
        return 2
    step = max(5.0, float(interval))
    prev_net = None
    with httpx.Client() as client:
        while True:
            metrics, prev_net = collect_host_metrics(prev_net)
            ok, msg = send_heartbeat(url, token, metrics, client=client)
            if not ok:
                print(f"heartbeat failed: {msg}", file=sys.stderr)
                if once:
                    return 1
            elif once:
                print("heartbeat ok")
                return 0
            try:
                time.sleep(step)
            except KeyboardInterrupt:
                return 0
