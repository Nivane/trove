"""IM 外壳塑形 —— **代码**,不是模板,而且住在中立包里。

两个设计事实决定了这个模块的位置与形态:

1. **IM 的入站格式是嵌套 JSON。** Slack 的 ``blocks[].text.text``、飞书的
   ``card.elements[].content``、钉钉/企微的 ``msgtype + markdown{}`` —— 模板层
   是**扁平**的 ``{{var}}`` 插值(见 ``action/template_render.py``),它表达不了
   嵌套结构。所以"把一份载荷装进 IM 外壳"这件事只能是代码。
2. **两个消费者互不相识。** 行动包(``services/action/``)按 AST 姿态守卫
   **禁止 import ``services/jobs``**,jobs 也不 import 行动包;而行动提案与
   任务告警都要能发到同一批 IM。塑形放在任一侧都会让另一侧欠一个反向依赖 ——
   中立包是唯一合法位置(守卫测试对新文件自动生效,见
   ``tests/services/action/test_action_readonly_posture.py``)。

``shape_payload`` 是**纯函数**:同一份载荷、同一个 kind → 逐字节同一个 body
(键序、截断、标题取值全确定)。``generic`` 原样返回 —— 没配 kind 的部署因此与
"没有这个包"逐字节一致,这是默认档兼容性的落点。
"""

from __future__ import annotations

import json
from typing import Any

#: 支持的通道类型。``generic`` = 载荷原样进、原样出(POST 一份 JSON 给自定义
#: 端点);其余四种各有一个 platform-specific 的嵌套外壳。
CHANNEL_KINDS = ("generic", "slack", "feishu", "dingtalk", "wecom")

#: 投递文本上限:IM 侧对消息体长度都有硬限制,而载荷是任意 JSON。截断在这里
#: 显式发生(带省略号标记),不指望平台报错来兜。
MAX_TEXT = 4000

#: 渲染文本时**跳过**的键:下划线前缀是 Trove 的内部信封字段(``_trove`` 带
#: proposal_id/idempotency_key 等),给人看的消息里它们是噪声。
_INTERNAL_PREFIX = "_"


def channel_kind(value: Any) -> str:
    """归一化 kind;未知值**响亮失败**(调用方按配置错处理,不静默降级)。

    静默回退到 ``generic`` 会让一个拼错的 ``kind: SaLck`` 变成"发得出去、
    但对面收到一份看不懂的 JSON" —— 这类故障最难查:通道回 200。
    """
    kind = str(value or "generic").strip().lower() or "generic"
    if kind not in CHANNEL_KINDS:
        raise ValueError(
            f"unknown channel kind {kind!r}: must be one of "
            f"{', '.join(CHANNEL_KINDS)}")
    return kind


def title_of(payload: dict[str, Any]) -> str:
    """标题:载荷里第一个像标题的字段,兜底 ``Trove``(绝不抛)。"""
    for key in ("title", "job_name", "rule_id", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return "Trove"


def render_text(payload: dict[str, Any]) -> str:
    """把扁平载荷渲染成一段确定性的 markdown 文本。

    规则:按载荷**插入序**逐行 ``key: value``;标量直接写,嵌套结构写紧凑
    JSON(``sort_keys`` 保证跨进程确定);下划线前缀的内部字段跳过。整体按
    :data:`MAX_TEXT` 截断。
    """
    lines: list[str] = []
    for key, value in payload.items():
        name = str(key)
        if name.startswith(_INTERNAL_PREFIX):
            continue
        if isinstance(value, str):
            lines.append(f"{name}: {value}")
        elif isinstance(value, (int, float, bool)) or value is None:
            lines.append(f"{name}: {value}")
        else:
            lines.append(
                f"{name}: {json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)}")
    text = "\n".join(lines)
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    return text


def shape_payload(payload: dict[str, Any], *, kind: str = "generic") -> dict[str, Any]:
    """把一份扁平载荷装进 ``kind`` 的 IM 外壳。

    Args:
        payload: 待外送的 JSON 对象(模板渲染产物 / 任务告警体,可能有
            ``_trove`` 信封)。
        kind: :data:`CHANNEL_KINDS` 之一。

    Returns:
        直接作为 HTTP body 的嵌套 dict。``generic`` 返回载荷的浅拷贝(与
        改动前逐字节一致)。

    Raises:
        ValueError: ``kind`` 不在闭集里 —— 响亮,由调用方记成配置错。
    """
    kind = channel_kind(kind)
    if kind == "generic":
        return dict(payload)
    text = render_text(payload)
    title = title_of(payload)
    if kind == "slack":
        return {
            "text": text,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            ],
        }
    if kind == "feishu":
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                },
                "elements": [{"tag": "markdown", "content": text}],
            },
        }
    if kind == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    # wecom
    return {"msgtype": "markdown", "markdown": {"content": text}}
