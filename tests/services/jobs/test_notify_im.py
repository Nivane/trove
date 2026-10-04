"""``notify.py`` 的 IM 家族:通道前缀 → 塑形投递,且不改变老通道的语义。

报警路径的契约与行动路径不同:**失败吞掉只记日志**(告警会重复,下一跳
会再送)。所以这里断言的是"发得对、失败报 False",而不是落库。
"""

from __future__ import annotations

import pytest

from trove.services.im.shape import shape_payload
from trove.services.jobs.notify import (
    ConsoleNotifier,
    IMNotifier,
    WebhookNotifier,
    build_notifier,
)

PAYLOAD = {"job_name": "daily-revenue", "message": "delta -12%", "kind": "ALERT"}


def _recorder(reply=(200, "ok"), raise_exc=None):
    seen: list[dict] = []

    async def transport(url, payload, headers, timeout):
        seen.append({"url": url, "payload": payload, "headers": headers})
        if raise_exc is not None:
            raise raise_exc
        return reply

    return seen, transport


# ── 工厂:前缀闭集,老通道一字不变 ───────────────────────

def test_console_and_webhook_are_untouched():
    assert isinstance(build_notifier("console"), ConsoleNotifier)
    assert isinstance(build_notifier("webhook:https://x.test/hook"), WebhookNotifier)
    assert build_notifier("unknown") is None
    assert build_notifier("webhook:not-a-url") is None
    assert build_notifier("") is None


@pytest.mark.parametrize("prefix,kind", [
    ("slack", "slack"), ("feishu", "feishu"),
    ("dingtalk", "dingtalk"), ("wecom", "wecom"),
])
def test_im_prefixes_build_im_notifiers(prefix, kind):
    notifier = build_notifier(f"{prefix}:https://im.test/hook")
    assert isinstance(notifier, IMNotifier)
    assert notifier.channel_kind == kind
    assert notifier.url == "https://im.test/hook"


def test_im_prefix_without_usable_url_is_none():
    assert build_notifier("slack:not-a-url") is None
    assert build_notifier("feishu:") is None


def test_unknown_kind_is_refused_at_construction():
    with pytest.raises(ValueError, match="unknown channel kind"):
        IMNotifier("https://im.test/hook", channel_kind="matrix")


# ── 投递:塑形后 POST,失败报 False ─────────────────────

async def test_im_notifier_posts_the_shaped_payload():
    seen, transport = _recorder()
    notifier = IMNotifier("https://im.test/hook", channel_kind="slack",
                          transport=transport)
    assert await notifier.send(PAYLOAD) is True
    assert seen[0]["url"] == "https://im.test/hook"
    assert seen[0]["payload"] == shape_payload(PAYLOAD, kind="slack")
    assert seen[0]["headers"] == {"Content-Type": "application/json"}


async def test_im_notifier_reports_http_failure_as_false():
    seen, transport = _recorder(reply=(500, "boom"))
    notifier = IMNotifier("https://im.test/hook", channel_kind="wecom",
                          transport=transport)
    assert await notifier.send(PAYLOAD) is False


async def test_im_notifier_swallows_transport_exceptions():
    _seen, transport = _recorder(raise_exc=TimeoutError("timed out"))
    notifier = IMNotifier("https://im.test/hook", channel_kind="dingtalk",
                          transport=transport)
    assert await notifier.send(PAYLOAD) is False
