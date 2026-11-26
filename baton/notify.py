"""Pluggable async mobile notifications for machine alerts and finished/failed tasks (Phase M3).

Supports:
  - Telegram Bot API (`BATON_TELEGRAM_BOT_TOKEN` + `BATON_TELEGRAM_CHAT_ID`)
  - Generic Webhook / ntfy (`BATON_ALERT_WEBHOOK_URL`)

Security invariants:
  - Uses `httpx` directly (zero extra dependencies).
  - Never includes `BATON_TELEGRAM_BOT_TOKEN` or `BATON_ALERT_WEBHOOK_URL` in
    error messages or logs (all tokens are masked before returning/logging).
  - Runs outbound HTTP posts on daemon threads so HTTP handlers, heartbeats,
    and task completion hooks never block.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import db

_log = logging.getLogger(__name__)


def _mask(text: str, *secrets_to_hide: str) -> str:
    out = str(text or "")
    for sec in secrets_to_hide:
        if sec:
            out = out.replace(sec, "***")
    return out


class Notifier:
    """Dispatches alert and task-completion notifications to configured channels."""

    def __init__(
        self,
        *,
        telegram_bot_token: str = "",
        telegram_chat_id: str = "",
        webhook_url: str = "",
        notify_tasks: bool = True,
        data_dir: Path | None = None,
        lang: str = "en",
        client_factory: Callable[[], httpx.Client] | None = None,
    ):
        self.telegram_bot_token = (telegram_bot_token or "").strip()
        self.telegram_chat_id = (telegram_chat_id or "").strip()
        self.webhook_url = (webhook_url or "").strip()
        self.notify_tasks = bool(notify_tasks)
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.lang = lang
        self._client_factory = client_factory or (lambda: httpx.Client(timeout=10.0))

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def webhook_ready(self) -> bool:
        return bool(self.webhook_url)

    @property
    def configured(self) -> bool:
        return self.telegram_ready or self.webhook_ready

    @property
    def channels(self) -> list[str]:
        out: list[str] = []
        if self.telegram_ready:
            out.append("Telegram")
        if self.webhook_ready:
            out.append("Webhook")
        return out

    # ------------------------------------------------------------ formatting
    def format_alert(self, alert: dict[str, Any]) -> tuple[str, str]:
        sev = str(alert.get("severity") or "warn")
        es = self.lang == "es"
        if sev == "bad":
            icon = "🔴"
            label = "CRÍTICA" if es else "CRITICAL"
        else:
            icon = "⚠️"
            label = "AVISO" if es else "WARNING"
        mname = str(alert.get("machine_name") or alert.get("machine_id") or "Baton")
        esc = (" (escalada)" if es else " (escalated)") if alert.get("escalated") else ""
        title = f"{icon} Baton · {label}{esc} · {mname}"
        body = str(alert.get("message") or alert.get("kind") or "")[:400]
        return title, body

    def format_task(self, task: Any) -> tuple[str, str]:
        es = self.lang == "es"
        status = str(getattr(task, "status", "") or "")
        merged = bool(getattr(task, "merged", False))
        if status == "done":
            icon = "✅"
            state = ("Fusionada" if merged else "Terminada") if es else ("Merged" if merged else "Done")
        else:
            icon = "❌"
            state = "Fallida" if es else "Failed"
        repo = str(getattr(task, "repo", "") or "")
        ttitle = str(getattr(task, "title", "") or "")[:120]
        agent = str(getattr(task, "agent", "") or "")
        model = str(getattr(task, "model", "") or "")
        who = f"{agent} ({model})" if model else agent
        title = f"{icon} Baton · {state} · {repo}" if repo else f"{icon} Baton · {state}"
        lines = [ttitle]
        if who:
            lines.append(f"Agente: {who}" if es else f"Agent: {who}")
        summary = str(getattr(task, "summary", "") or getattr(task, "message", "") or "").strip()
        if summary:
            first = " ".join(summary.splitlines()[0].split())[:260]
            lines.append(first)
        pr_url = str(getattr(task, "pr_url", "") or "").strip()
        if pr_url:
            lines.append(pr_url)
        return title, "\n".join(lines)

    # ------------------------------------------------------------ delivery
    def _send_telegram(self, client: httpx.Client, title: str, body: str) -> tuple[bool, str]:
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        text = f"{title}\n{body}".strip()[:4000]
        try:
            resp = client.post(
                url,
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
            if 200 <= resp.status_code < 300:
                return True, "Telegram: ok"
            return False, _mask(
                f"Telegram HTTP {resp.status_code}: {resp.text[:160]}",
                self.telegram_bot_token, self.webhook_url,
            )
        except httpx.HTTPError as exc:
            return False, _mask(f"Telegram error: {exc}", self.telegram_bot_token, self.webhook_url)

    def _send_webhook(self, client: httpx.Client, title: str, body: str, severity: str) -> tuple[bool, str]:
        ascii_title = " ".join(title.encode("ascii", "ignore").decode("ascii").split())[:200] or "Baton"
        try:
            resp = client.post(
                self.webhook_url,
                json={"title": title, "message": body, "severity": severity, "source": "baton"},
                headers={"Title": ascii_title},
            )
            if 200 <= resp.status_code < 300:
                return True, "Webhook: ok"
            return False, _mask(
                f"Webhook HTTP {resp.status_code}: {resp.text[:160]}",
                self.telegram_bot_token, self.webhook_url,
            )
        except httpx.HTTPError as exc:
            return False, _mask(f"Webhook error: {exc}", self.telegram_bot_token, self.webhook_url)

    def send_sync(self, title: str, body: str, *, severity: str = "info") -> tuple[bool, list[str]]:
        """Send to all configured channels synchronously. Returns `(any_ok, messages)`."""
        if not self.configured:
            return False, ["No notification channels configured."]
        results: list[str] = []
        any_ok = False
        with self._client_factory() as client:
            if self.telegram_ready:
                ok, msg = self._send_telegram(client, title, body)
                any_ok = any_ok or ok
                results.append(msg)
            if self.webhook_ready:
                ok, msg = self._send_webhook(client, title, body, severity)
                any_ok = any_ok or ok
                results.append(msg)
        return any_ok, results

    def send_async(
        self,
        title: str,
        body: str,
        *,
        severity: str = "info",
        on_sent: Callable[[bool], None] | None = None,
    ) -> None:
        if not self.configured:
            return

        def _worker() -> None:
            try:
                ok, msgs = self.send_sync(title, body, severity=severity)
                if not ok:
                    _log.warning("Notification delivery failed: %s", "; ".join(msgs))
                if on_sent is not None:
                    on_sent(ok)
            except Exception as exc:
                _log.warning("Notification worker error: %s", _mask(str(exc), self.telegram_bot_token, self.webhook_url))

        threading.Thread(target=_worker, daemon=True).start()

    def _mark_alert_notified(self, alert_id: int) -> None:
        if self.data_dir is None or not alert_id:
            return
        try:
            with db.connect(self.data_dir) as conn:
                conn.execute("UPDATE alerts SET notified_at = ? WHERE id = ?", (time.time(), int(alert_id)))
        except Exception:
            pass

    def notify_alert(self, alert: dict[str, Any]) -> None:
        """Hook for `AlertEngine.on_opened`: sends alert asynchronously and stamps `notified_at`."""
        if not self.configured:
            return
        title, body = self.format_alert(alert)
        aid = int(alert.get("id") or 0)
        sev = str(alert.get("severity") or "warn")
        self.send_async(
            title,
            body,
            severity=sev,
            on_sent=lambda ok: self._mark_alert_notified(aid) if (ok and aid) else None,
        )

    def notify_task(self, task: Any) -> None:
        """Hook for `runner.on_finished`: notifies on done/failed tasks (skips cancelled)."""
        if not self.configured or not self.notify_tasks:
            return
        status = str(getattr(task, "status", "") or "")
        if status not in ("done", "failed"):
            return
        title, body = self.format_task(task)
        self.send_async(title, body, severity="bad" if status == "failed" else "info")

    def send_test(self) -> tuple[bool, str]:
        es = self.lang == "es"
        title = "🔔 Baton · Aviso de prueba" if es else "🔔 Baton · Test notification"
        body = (
            "Las notificaciones de alertas de máquinas y tareas terminadas funcionan correctamente."
            if es else
            "Machine alerts and finished-task notifications are working properly."
        )
        ok, msgs = self.send_sync(title, body, severity="info")
        return ok, " · ".join(msgs)
