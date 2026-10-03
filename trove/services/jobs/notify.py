"""Alert/report notifiers — channel abstractions (console / webhook).

Channel string on a job config:
  console                      → log + terminal line
  webhook:https://host/path     → POST JSON to the URL

Notifying never blocks a schedule tick: webhook failures are logged and
swallowed. ``send`` still *reports* the outcome (True/False) so callers
that keep a delivery log (subscriptions) can record failures — swallowing
the exception and hiding the failure are different contracts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class Notifier:
    kind = "base"

    async def send(self, payload: dict[str, Any]) -> bool:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    kind = "console"

    async def send(self, payload: dict[str, Any]) -> bool:
        # `kind` defaults to ALERT so alert payloads keep their historic
        # line; report deliveries set "REPORT" and print as such.
        tag = str(payload.get("kind") or "ALERT")
        logger.warning(
            "[%s] %s | %s | %s",
            tag,
            payload.get("job_name", "?"),
            payload.get("expr", ""),
            payload.get("message", ""),
        )
        print(f"[{tag}] {payload.get('job_name', '?')}: {payload.get('message', '')}")
        return True


class WebhookNotifier(Notifier):
    kind = "webhook"

    def __init__(self, url: str, timeout_s: float = 10.0):
        self.url = url
        self.timeout_s = timeout_s

    async def send(self, payload: dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(self.url, json=payload)
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.warning("[ALERT] webhook delivery failed: %s", e)
            return False


def build_notifier(channel: str) -> Notifier | None:
    """Instantiate a notifier from a channel spec; None when unrecognized."""
    channel = (channel or "").strip()
    if not channel:
        return None
    if channel == "console":
        return ConsoleNotifier()
    if channel.lower().startswith("webhook:"):
        url = channel.split(":", 1)[1].strip()
        if url.startswith("http://") or url.startswith("https://"):
            return WebhookNotifier(url)
    return None