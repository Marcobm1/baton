"""Lightweight session authentication to prevent local privilege escalation.

When BATON_ACCESS_KEY is configured, every route except /healthz, /login and
/static/* requires an HttpOnly, SameSite=Strict cookie signed with HMAC-SHA256
derived from BATON_ACCESS_KEY.

Why signed cookies instead of trusting Tailscale identity headers on loopback:
Baton listens on 127.0.0.1:8787, which is reachable both by Tailscale Serve and
by any restricted local agent (OpenCode, local AI, or a container reaching host
loopback). Any local process can forge HTTP headers such as Tailscale-User-Login,
so loopback headers cannot distinguish the owner from a local agent. By contrast,
BATON_ACCESS_KEY is stripped from every agent subprocess by agent_env(), and an
HttpOnly browser cookie is invisible to scripts and local agent subprocesses.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

COOKIE_NAME = "baton_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days; survives restarts until the key rotates
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SECONDS = 60.0


def _signing_key(access_key: str) -> bytes:
    return hmac.new(access_key.encode("utf-8"), b"baton-session-v1", hashlib.sha256).digest()


def make_session_token(access_key: str, *, now: float | None = None,
                       max_age: int = SESSION_MAX_AGE) -> str:
    """Create a signed, expiring session token derived from access_key."""
    if not access_key:
        raise ValueError("access_key must be non-empty")
    exp = int((time.time() if now is None else now) + max_age)
    nonce = secrets.token_urlsafe(16)
    payload = f"v1.{exp}.{nonce}"
    sig = hmac.new(_signing_key(access_key), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str | None, access_key: str, *, now: float | None = None) -> bool:
    """Return True only when token has a valid HMAC for access_key and has not expired."""
    if not token or not access_key:
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    version, exp_str, nonce, sig = parts
    if version != "v1" or not exp_str.isdigit() or not nonce or len(sig) != 64:
        return False
    payload = f"{version}.{exp_str}.{nonce}"
    expected = hmac.new(_signing_key(access_key), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(sig, expected):
        return False
    current = time.time() if now is None else now
    return int(exp_str) > current


def check_access_key(submitted: str, access_key: str) -> bool:
    """Constant-time check of the submitted access key."""
    if not access_key:
        return False
    return secrets.compare_digest(submitted.strip().encode("utf-8"), access_key.encode("utf-8"))


def safe_next_path(raw: str | None) -> str:
    """Allow only local relative paths as post-login redirects (no open redirects)."""
    path = (raw or "").strip()
    if (not path or not path.startswith("/") or path.startswith("//")
            or "\\" in path or "\r" in path or "\n" in path
            or path == "/login" or path.startswith("/login?")):
        return "/"
    return path


class LoginRateLimiter:
    """Small in-memory sliding-window throttle against local brute force on /login."""

    def __init__(self, max_failures: int = LOGIN_MAX_FAILURES,
                 window_seconds: float = LOGIN_WINDOW_SECONDS):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    def is_limited(self, client: str, *, now: float | None = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._failures.get(client)
            if not bucket:
                return False
            self._prune(bucket, t)
            if not bucket:
                self._failures.pop(client, None)
                return False
            return len(bucket) >= self.max_failures

    def record_failure(self, client: str, *, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._failures.setdefault(client, deque())
            self._prune(bucket, t)
            bucket.append(t)

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
