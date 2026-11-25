"""Isolated Docker sandbox for Agents chat ("All permissions" mode).

Runs OpenCode ACP inside a disposable Linux container with:
  - ONLY the chosen folder bind-mounted at `/workspace` (no host profile,
    no Baton data, no Docker socket, no sibling folders);
  - all Linux capabilities dropped (`--cap-drop=ALL`) and `no-new-privileges`;
  - PID and memory limits (`--pids-limit`, `--memory`);
  - `host.docker.internal` wired to the host gateway so local `llama-server`
    models work without passing any cloud credentials into the container;
  - inline `OPENCODE_CONFIG_CONTENT` only (zero host `BATON_*` or GitHub tokens).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from .agents import ALWAYS_DENIED, NO_GIT_CHANGES, _rule
from .ping import _NO_WINDOW

DEFAULT_SANDBOX_IMAGE = "baton-opencode-sandbox:latest"
CONTAINER_WORKSPACE = "/workspace"
CHAT_AGENT_FULL = "baton-chat-full"

# Allowed inside the isolated Docker container only: read, edit, and run shell
# commands in /workspace without asking, while still denying git push/commit,
# gh/sudo, external_directory, and OpenCode meta-tools as defense-in-depth.
FULL_RULES = (
    [_rule("*", "deny")]
    + [_rule(a, "allow") for a in ("read", "glob", "grep", "list", "edit", "shell")]
    + [_rule("shell", "deny", c) for c in NO_GIT_CHANGES]
    + ALWAYS_DENIED
)


def find_hardlinks(folder: Path, *, limit: int = 3) -> list[str]:
    """Return relative paths of regular files with st_nlink > 1 inside `folder`.

    A pre-existing hardlink inside the mounted workspace (on NTFS or POSIX)
    shares an inode with a file outside `/workspace` and remains readable from
    inside a bind-mounted Docker container. Refusing Full permissions when any
    hardlink is present closes that host-side exposure.

    Note: calls `os.lstat(entry.path)` rather than `entry.stat()`, because on
    Windows `DirEntry.stat()` reads `WIN32_FIND_DATA` where `st_nlink` is 0.
    """
    found: list[str] = []
    root = Path(os.path.realpath(folder))
    stack = [root]
    while stack and len(found) < limit:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            st = os.lstat(entry.path)
                            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                                found.append(os.path.relpath(entry.path, root))
                                if len(found) >= limit:
                                    break
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def is_local_opencode_model(model: str) -> bool:
    """True for OpenCode `local-*` models backed by a host llama-server (no credentials)."""
    provider = (model or "").split("/", 1)[0].strip().lower()
    return provider.startswith("local-") and "/" in (model or "")


def find_docker() -> str | None:
    return shutil.which("docker")


def docker_available(image: str = DEFAULT_SANDBOX_IMAGE) -> bool:
    """True only if Docker daemon is reachable AND the sandbox image exists locally."""
    if not image:
        return False
    exe = find_docker()
    if not exe:
        return False
    try:
        r = subprocess.run(
            [exe, "image", "inspect", image],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL, **_NO_WINDOW,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def local_provider_config(local_models: list[dict[str, Any]], *,
                          host: str = "host.docker.internal") -> dict[str, dict[str, Any]]:
    """Build OpenCode OpenAI-compatible provider entries for local llama-server models."""
    providers: dict[str, dict[str, Any]] = {}
    for m in local_models or []:
        port = m.get("port")
        mid = str(m.get("id") or "").strip()
        if not port or not mid:
            continue
        name = str(m.get("name") or mid)
        family = str(m.get("family_key") or "").strip()
        keys = [f"local-{mid}"]
        if family:
            keys.append(f"local-{family}")
        for key in keys:
            prov = providers.setdefault(key, {
                "npm": "@ai-sdk/openai-compatible",
                "name": key,
                "options": {"baseURL": f"http://{host}:{port}/v1"},
                "models": {},
            })
            prov["models"][mid] = {"name": name}
    return providers


def sandbox_opencode_config(local_models: list[dict[str, Any]], *,
                            mode: str = "full",
                            host: str = "host.docker.internal") -> dict[str, Any]:
    """Inline OpenCode config injected into the isolated container."""
    agent_name = CHAT_AGENT_FULL if mode == "full" else "baton-chat"
    rules = FULL_RULES if mode == "full" else None
    cfg: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "agents": {
            agent_name: {
                "mode": "primary",
                "description": "Baton isolated Docker sandbox (All permissions inside /workspace)",
                "permissions": rules,
            }
        },
    }
    providers = local_provider_config(local_models, host=host)
    if providers:
        cfg["provider"] = providers
    return cfg


def safe_container_name(chat_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", chat_id).strip("-")[:40] or "session"
    return f"baton-sandbox-{slug}"


def sandbox_docker_command(
    folder: Path,
    image: str,
    config_json: str,
    *,
    container_name: str,
    cmd: list[str] | None = None,
    docker_bin: str | None = None,
) -> list[str]:
    """Hardened `docker run` argv (never passed through a shell)."""
    exe = docker_bin or find_docker() or "docker"
    src = os.path.realpath(folder)
    args = [
        exe, "run", "--rm", "-i", "--init",
        "--name", container_name,
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=256",
        "--memory=4g",
        "--add-host=host.docker.internal:host-gateway",
    ]
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        # Match host UID/GID on Linux so --cap-drop=ALL can write to /workspace
        # and created files keep host ownership instead of root.
        args.extend(["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"])
    args.extend([
        "--mount", f"type=bind,src={src},dst={CONTAINER_WORKSPACE}",
        "-w", CONTAINER_WORKSPACE,
        "-e", f"OPENCODE_CONFIG_CONTENT={config_json}",
        image,
        *(cmd or ["opencode", "acp"]),
    ])
    return args


def stop_container(container_name: str) -> None:
    """Best-effort container cleanup when closing or cancelling a sandboxed chat."""
    if not container_name:
        return
    exe = find_docker()
    if not exe:
        return
    try:
        subprocess.run(
            [exe, "rm", "-f", container_name],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL, **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass
