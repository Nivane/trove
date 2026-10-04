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
limits, the failed status — with zero real network. A transport may also
report a third value (``retry_after_s``) when the channel sent ``Retry-After``;
two-value transports stay valid, so the contract grew without a break.

**Failure classes decide retryability, not the transport.** Every failed send
is folded through :func:`~trove.services.errors.classify.classify_error` with
``context="action"``: 5xx / 429 / timeout / connection-class failures are
transient (a second attempt can succeed), 4xx and configuration failures are
not (the same config produces the same error forever). The service reads
``retryable`` off this result and re-derives it from the stored receipt when
it sweeps for due retries — one classifier, both paths.

**Channel kind.** A channel spec may carry ``kind`` (see
:data:`trove.services.im.shape.CHANNEL_KINDS`); the payload is then wrapped by
the neutral IM shaper before the POST. Absent kind (or ``generic``) = the
payload goes out verbatim, exactly as before there was a shaper at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from trove.core.logging import get_logger
from trove.services.errors.classify import ClassifiedError, classify_error
from trove.services.im.shape import shape_payload

logger = get_logger(__name__)

#: Bounded receipt text stored per delivery row: enough to debug a rejection
#: body, small enough that a hostile/garrulous endpoint cannot fill the store.
MAX_EXCERPT = 512

#: transport 返回 ``(status, text)`` 或 ``(status, text, retry_after_s)`` ——
#: 2 元组是原契约,继续有效(注入的假 transport 一行都不用改)。
Transport = Callable[[str, dict, dict, float], Awaitable[Any]]

#: 重试等待秒数(``Retry-After``)的解析上限:超过一天的值按一天记,免得一条
#: 手滑的响应头把"什么时候能重试"推到无限远。
MAX_RETRY_AFTER_S = 86400.0


async def _httpx_transport(
    url: str, payload: dict, headers: dict, timeout_s: float,
) -> tuple[int | None, str, float | None]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=payload, headers=headers)
        return resp.status_code, resp.text or "", _retry_after_of(resp)


def _retry_after_of(resp: Any) -> float | None:
    """``Retry-After`` 头(秒)或 ``None``;HTTP-date 形式不解析(罕见且歧义)。"""
    try:
        raw = resp.headers.get("retry-after")
    except Exception:  # 非 httpx 的响应对象(测试替身)没有 headers
        return None
    if not raw:
        return None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


def _unpack(raw: Any) -> tuple[int | None, str, float | None]:
    """transport 返回 2 元组(旧契约)或 3 元组(带 retry-after)。"""
    if isinstance(raw, (tuple, list)):
        if len(raw) >= 3:
            return raw[0], raw[1] or "", raw[2]
        if len(raw) == 2:
            return raw[0], raw[1] or "", None
    raise ValueError(f"transport must return (status, text[, retry_after_s]): {raw!r}")


def classify_failure(
    http_status: int | None = None, error: str = "", excerpt: str = "",
) -> ClassifiedError:
    """Fold a failed outbound attempt into one ``ErrorClass`` (context=action).

    Public and pure on purpose: the dispatch path classifies what it just saw,
    and the retry sweep re-classifies the **stored receipt** (``http_status`` +
    ``error``) with the very same function — a retry policy that could disagree
    with the original failure would be worse than none.
    """
    text = f"HTTP {http_status} {error}".strip() if http_status is not None \
        else str(error or "")
    if excerpt:
        text = f"{text} {excerpt}".strip()
    return classify_error(text, context="action")


@dataclass
class DispatchResult:
    ok: bool
    channel: str = ""
    http_status: int | None = None
    response_excerpt: str = ""
    error: str = ""
    #: 这次失败重试有没有意义(action 上下文的分类结果);成功恒 False。
    retryable: bool = False
    #: 通道给的 ``Retry-After``(秒,若有)—— 只作观测量上抛,不落库:
    #: 重试时点一律由 ``guard.next_attempt_at`` 从 (attempts, 上次尝试)
    #: 纯派生,零新列。服务侧把它记进日志,供人诊断"为什么对面在退我们"。
    retry_after_s: float | None = None


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

    def _resolve(
        self, channel: str, payload: dict[str, Any],
    ) -> tuple[str, dict, str, dict] | DispatchResult:
        """Shared pre-flight: channel lookup + payload shaping.

        Returns ``(name, spec, url, headers)`` on success, or a failed
        :class:`DispatchResult` (same shape ``send`` returns) instead of
        raising. ``send`` and ``preflight`` both go through here, so a dry run
        cannot drift from the real thing: the *only* difference between them
        is the POST itself.
        """
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
        try:
            shape_payload(payload, kind=spec.get("kind") or "generic")
        except ValueError as e:
            # 通道 kind 配错 = 配置错:不重试,原样报给人。
            return DispatchResult(ok=False, channel=name, error=str(e)[:300])
        headers = {"Content-Type": "application/json"}
        secret = str(spec.get("secret") or "")
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        return name, spec, url, headers

    def preflight(self, channel: str, payload: dict[str, Any]) -> DispatchResult:
        """``send`` minus the POST: channel known, url usable, payload shapes.

        Pure local work, zero network — dry runs and config health checks
        share this entry point, and it judges exactly what ``send`` judges
        before it presses the button.
        """
        resolved = self._resolve(channel, payload)
        if isinstance(resolved, DispatchResult):
            return resolved
        return DispatchResult(ok=True, channel=resolved[0])

    async def send(self, channel: str, payload: dict[str, Any]) -> DispatchResult:
        """POST ``payload`` to ``channel``; every failure is a result, not a raise."""
        resolved = self._resolve(channel, payload)
        if isinstance(resolved, DispatchResult):
            return resolved
        name, spec, url, headers = resolved
        body = shape_payload(payload, kind=spec.get("kind") or "generic")
        try:
            status, text, retry_after = _unpack(
                await self._transport(url, body, headers, self.timeout_s))
        except Exception as e:  # network, DNS, TLS, timeout — all one outcome
            logger.warning("action dispatch to channel %s failed: %s", name, e)
            error = f"{type(e).__name__}: {e}"[:300]
            verdict = classify_failure(None, error)
            return DispatchResult(
                ok=False, channel=name, error=error,
                retryable=verdict.retryable)
        excerpt = (text or "")[:MAX_EXCERPT]
        ok = status is not None and 200 <= int(status) < 300
        error = "" if ok else f"HTTP {status}"
        verdict = classify_failure(None if ok else status, error, excerpt)
        return DispatchResult(
            ok=ok, channel=name, http_status=status,
            response_excerpt=excerpt, error=error,
            retryable=(not ok) and verdict.retryable,
            retry_after_s=retry_after if not ok else None,
        )
