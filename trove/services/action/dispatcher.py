"""Outbound dispatch — named channels, one POST, a receipt either way.

The dispatcher is the only thing in the action pillar that talks to the
outside world, and it takes **no connector registry, no SQL, no datasource**:
its entire input is a channel name and a payload dict. That is the structural
half of the read-only posture — the module physically cannot write to a
business database, whatever a template says.

Channels are **named** (``ops-alerts``), never bare URLs: the URL and its
secret live in the deployment config (``agent.action.channels``), so a
template is a reviewable org asset that carries no credentials, and an admin
confirming one never has to reason about where it points. An unknown channel
is a *failed dispatch*, not an exception: the failure is recorded on the
proposal and in ``deliveries`` — see ``service.py`` for why silence is the one
thing this path must never do.

``transport`` is injectable (``async (url, payload, headers, timeout) ->
(status, text)``) so tests exercise the whole dispatch flow — receipts, retry
limits, the failed status — with zero real network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from trove.core.logging import get_logger

logger = get_logger(__name__)

#: Bounded receipt text stored per delivery row: enough to debug a rejection
#: body, small enough that a hostile/garrulous endpoint cannot fill the store.
MAX_EXCERPT = 512

Transport = Callable[[str, dict, dict, float], Awaitable[tuple[int | None, str]]]


async def _httpx_transport(
    url: str, payload: dict, headers: dict, timeout_s: float,
) -> tuple[int | None, str]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=payload, headers=headers)
        return resp.status_code, resp.text or ""


@dataclass
class DispatchResult:
    ok: bool
    channel: str = ""
    http_status: int | None = None
    response_excerpt: str = ""
    error: str = ""


class ActionDispatcher:
    """Sends one payload to one named channel; reports, never raises."""

    def __init__(
        self, channels: dict[str, dict] | None = None, *,
        timeout_s: float = 10.0, transport: Transport | None = None,
    ):
        self.channels = {str(k): dict(v or {}) for k, v in (channels or {}).items()}
        self.timeout_s = float(timeout_s or 10.0)
        self._transport: Transport = transport or _httpx_transport

    def channel_names(self) -> list[str]:
        return sorted(self.channels)

    def has_channel(self, name: str) -> bool:
        return str(name or "") in self.channels

    async def send(self, channel: str, payload: dict[str, Any]) -> DispatchResult:
        """POST ``payload`` to ``channel``; every failure is a result, not a raise."""
        name = str(channel or "").strip()
        if not name:
            return DispatchResult(
                ok=False, error="the template names no channel")
        spec = self.channels.get(name)
        if spec is None:
            known = ", ".join(self.channel_names()) or "none"
            return DispatchResult(
                ok=False, channel=name,
                error=f"channel {name!r} is not configured "
                      f"(configured channels: {known})")
        url = str(spec.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return DispatchResult(
                ok=False, channel=name,
                error=f"channel {name!r} has no usable url "
                      "(must start with http:// or https://)")
        headers = {"Content-Type": "application/json"}
        secret = str(spec.get("secret") or "")
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        try:
            status, text = await self._transport(
                url, payload, headers, self.timeout_s)
        except Exception as e:  # network, DNS, TLS, timeout — all one outcome
            logger.warning("action dispatch to channel %s failed: %s", name, e)
            return DispatchResult(
                ok=False, channel=name, error=f"{type(e).__name__}: {e}"[:300])
        excerpt = (text or "")[:MAX_EXCERPT]
        ok = status is not None and 200 <= int(status) < 300
        return DispatchResult(
            ok=ok, channel=name, http_status=status,
            response_excerpt=excerpt,
            error="" if ok else f"HTTP {status}",
        )
