"""IM 塑形与投递:信封形状 / generic 逐字节兼容 / 未知 kind 响亮失败。

这一层住在中立包里(行动包不得 import jobs,jobs 不得 import 行动包),
所以测试也钉住两件事:**形状**(每种 kind 的嵌套外壳是给对面平台看的契约)
与**默认档兼容**(没有 kind 时载荷原样出去,与没有这个包时逐字节一致)。
"""

from __future__ import annotations

import pytest

from trove.services.im.shape import (
    CHANNEL_KINDS,
    MAX_TEXT,
    channel_kind,
    render_text,
    shape_payload,
    title_of,
)
from trove.services.im.transport import deliver

PAYLOAD = {
    "rule_id": "revenue-drop",
    "message": "delta -12%",
    "current": 1234,
    "_trove": {"proposal_id": "p1"},
}


# ── kind 归一化 ─────────────────────────────────────────

def test_kind_set_is_the_documented_five():
    assert CHANNEL_KINDS == ("generic", "slack", "feishu", "dingtalk", "wecom")


def test_channel_kind_normalizes_and_refuses_unknown():
    assert channel_kind(" Slack ") == "slack"
    assert channel_kind("") == "generic"
    assert channel_kind(None) == "generic"
    with pytest.raises(ValueError, match="unknown channel kind"):
        channel_kind("SaLck")


# ── generic:默认档逐字节兼容 ────────────────────────────

def test_generic_is_a_verbatim_copy():
    """没有 kind 的部署外送的仍是同一份载荷(浅拷贝,不是同一对象)。"""
    out = shape_payload(PAYLOAD)
    assert out == PAYLOAD
    assert out is not PAYLOAD


def test_generic_keeps_internal_fields():
    """generic 不做文本渲染,``_trove`` 信封照旧原样出去。"""
    assert shape_payload(PAYLOAD, kind="generic")["_trove"] == {
        "proposal_id": "p1"}


# ── 文本渲染 ────────────────────────────────────────────

def test_render_text_is_insertion_ordered_and_skips_internal_keys():
    assert render_text(PAYLOAD) == "rule_id: revenue-drop\nmessage: delta -12%\ncurrent: 1234"


def test_render_text_nested_objects_are_deterministic_json():
    a = render_text({"x": {"b": 1, "a": 2}})
    b = render_text({"x": {"a": 2, "b": 1}})
    assert a == b == 'x: {"a": 2, "b": 1}'


def test_render_text_caps_length_with_an_ellipsis():
    text = render_text({"blob": "x" * (MAX_TEXT * 2)})
    assert len(text) == MAX_TEXT
    assert text.endswith("…")


def test_title_prefers_named_fields_then_falls_back():
    assert title_of({"title": "T", "job_name": "J"}) == "T"
    assert title_of({"job_name": "J"}) == "J"
    assert title_of({}) == "Trove"


# ── 四种 IM 外壳 ────────────────────────────────────────

def test_slack_gets_text_and_a_mrkdwn_block():
    body = shape_payload(PAYLOAD, kind="slack")
    assert body["text"] == render_text(PAYLOAD)
    assert body["blocks"] == [
        {"type": "section", "text": {"type": "mrkdwn", "text": body["text"]}}]


def test_feishu_gets_an_interactive_card_with_a_header():
    body = shape_payload(PAYLOAD, kind="feishu")
    assert body["msg_type"] == "interactive"
    card = body["card"]
    assert card["header"]["title"]["content"] == "revenue-drop"
    assert card["elements"][0]["tag"] == "markdown"
    assert card["elements"][0]["content"] == render_text(PAYLOAD)


def test_dingtalk_and_wecom_are_markdown_envelopes():
    dd = shape_payload(PAYLOAD, kind="dingtalk")
    assert dd["msgtype"] == "markdown"
    assert dd["markdown"]["title"] == "revenue-drop"
    assert dd["markdown"]["text"] == render_text(PAYLOAD)

    wc = shape_payload(PAYLOAD, kind="wecom")
    assert wc == {"msgtype": "markdown",
                  "markdown": {"content": render_text(PAYLOAD)}}


def test_shaping_is_deterministic():
    assert shape_payload(PAYLOAD, kind="slack") == shape_payload(PAYLOAD, kind="slack")


def test_shape_payload_refuses_unknown_kind():
    with pytest.raises(ValueError):
        shape_payload(PAYLOAD, kind="matrix")


# ── deliver:塑形后投递,transport 可注入 ────────────────

async def test_deliver_shapes_then_posts_with_default_headers():
    seen: list[dict] = []

    async def transport(url, payload, headers, timeout):
        seen.append({"url": url, "payload": payload,
                     "headers": headers, "timeout": timeout})
        return 200, "ok"

    status, text = await deliver("https://im.invalid/hook", PAYLOAD,
                                 kind="slack", timeout_s=3.0,
                                 transport=transport)
    assert (status, text) == (200, "ok")
    assert seen[0]["url"] == "https://im.invalid/hook"
    assert seen[0]["headers"] == {"Content-Type": "application/json"}
    assert seen[0]["timeout"] == 3.0
    assert seen[0]["payload"] == shape_payload(PAYLOAD, kind="slack")


async def test_deliver_passes_generic_payload_through_untouched():
    seen: list[dict] = []

    async def transport(url, payload, headers, timeout):
        seen.append(payload)
        return 200, "ok"

    await deliver("https://im.invalid/hook", PAYLOAD, transport=transport)
    assert seen[0] == PAYLOAD
