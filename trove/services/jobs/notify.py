"""Alert/report notifiers — channel abstractions (console / webhook / IM).

Channel string on a job config:
  console                      → log + terminal line
  webhook:https://host/path     → POST JSON to the URL (payload verbatim)
  slack:https://...             → Slack incoming webhook (``text`` + ``blocks``)
  feishu:https://...            → Feishu/Lark bot webhook (interactive card)
  dingtalk:https://...          → DingTalk bot webhook (markdown)
  wecom:https://...             → WeCom group bot webhook (markdown)

The IM family shapes the payload through the **neutral** ``services.im``
shaper — jobs may import it and so may the action pillar, while neither may
import the other (the action package's posture guard pins that). A nested
envelope (``{"text": ..., "blocks": ...}``, a card) is the reason this is
code rather than a config template.

Notifying never blocks a schedule tick: delivery failures are logged and
swallowed. ``send`` still *reports* the outcome (True/False) so callers
that keep a delivery log (subscriptions) can record failures — swallowing
the exception and hiding the failure are different contracts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from trove.services.im.shape import CHANNEL_KINDS
from trove.services.im.transport import deliver

logger = logging.getLogger(__name__)

#: ``<prefix>:<url>`` 里前缀 → 通道 kind。只有四项:``generic`` 不是前缀
#: (它就是 ``webhook:`` 的老行为 —— payload 原样出去)。
_IM_PREFIXES = {
    "slack": "slack", "feishu": "feishu",
    "dingtalk": "dingtalk", "wecom": "wecom",
}


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


class IMNotifier(Notifier):
    """IM 家族投递:载荷经 ``im.shape`` 塑形后 POST(零 LLM、零新依赖)。

    通道 kind 决定信封长什么样(见 :data:`CHANNEL_KINDS`);失败与
    :class:`WebhookNotifier` 同契约 —— 记日志、返回 False、不抛。
    """

    kind = "im"

    def __init__(self, url: str, *, channel_kind: str = "generic",
                 timeout_s: float = 10.0, transport=None):
        if channel_kind not in CHANNEL_KINDS:
            raise ValueError(f"unknown channel kind: {channel_kind!r}")
        self.url = url
        self.channel_kind = channel_kind
        self.timeout_s = timeout_s
        self._transport = transport

    async def send(self, payload: dict[str, Any]) -> bool:
        try:
            status, text = await deliver(
                self.url, payload, kind=self.channel_kind,
                timeout_s=self.timeout_s, transport=self._transport)
            ok = status is not None and 200 <= int(status) < 300
            if not ok:
                logger.warning(
                    "[ALERT] %s delivery failed: HTTP %s %s",
                    self.channel_kind, status, (text or "")[:200])
            return ok
        except Exception as e:
            logger.warning("[ALERT] %s delivery failed: %s", self.channel_kind, e)
            return False


def build_notifier(channel: str) -> Notifier | None:
    """Instantiate a notifier from a channel spec; None when unrecognized."""
    channel = (channel or "").strip()
    if not channel:
        return None
    if channel == "console":
        return ConsoleNotifier()
    lowered = channel.lower()
    if lowered.startswith("webhook:"):
        url = channel.split(":", 1)[1].strip()
        if url.startswith("http://") or url.startswith("https://"):
            return WebhookNotifier(url)
        return None
    for prefix, kind in _IM_PREFIXES.items():
        if lowered.startswith(f"{prefix}:"):
            url = channel.split(":", 1)[1].strip()
            if url.startswith("http://") or url.startswith("https://"):
                return IMNotifier(url, channel_kind=kind)
            return None
    return None