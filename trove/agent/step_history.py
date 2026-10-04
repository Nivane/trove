"""步骤历史落盘 —— SSE step 事件 → 有界消息 metadata(方案 ①;纯函数)。

对话历史补存(方案 ``trove-chat-persist-plan.html``):直播时前端从 SSE 收
step 事件渲染「分析过程」面板;历史轮此前无步骤可恢复,面板整块空白。这里
把每个 run 的 step 事件(**SSE 形状原样**:``type/seq/node/elapsed_ms/lang/
detail``)在有界预算内裁剪后随 assistant 消息 metadata 落盘,恢复端走与
直播同一映射,历史卡片与直播卡片因此长得一样。

预算(确定性、可测):

============  ===============  ===============================================
约束          值               触顶行为
============  ===============  ===============================================
步数上限      60               保留前 60 步(前缀语义),其余计入截断计数
单步体积      ≈3KB             先截长字符串(>500 字 → 前 500 字),仍超 →
                               只留骨架键(type/seq/node/elapsed_ms/lang,
                               detail 清空)
总预算        64KB             从尾部提前截断(前缀语义),计入截断计数
============  ===============  ===============================================

信息面 = SSE 已发内容,不引入新暴露面:detail 里 LLM 相关字段本就只有
200 字预览,``_step_event`` 里 prompt/输出原文从不进 SSE、也就不进落盘。

纯函数、零 I/O、零 LLM;采集上限(防跑飞的进程内缓冲闸)也在这里声明,
消费方(``trove/agent/session.py``)只做缓冲与调用。
"""

from __future__ import annotations

import json
from typing import Any

# 采集上限:进程内缓冲的防跑飞上限(裁剪前的硬闸)。
MAX_COLLECTED_STEPS = 200
# 落盘裁剪:步数上限(前缀保留)。
MAX_STEPS = 60
# 落盘裁剪:单步内的长字符串截断阈值(字符数)。
MAX_STRING_CHARS = 500
# 落盘裁剪:单步体积预算(字节,JSON 编码后)。
STEP_BUDGET_BYTES = 3 * 1024
# 落盘裁剪:整轮总预算(字节,JSON 编码后)。
TOTAL_BUDGET_BYTES = 64 * 1024

# 单步超预算时的骨架键:前端映射所需的最小集(type/lang 维持形状一致)。
_SKELETON_KEYS = ("type", "seq", "node", "elapsed_ms", "lang")


def _json_size(value: Any) -> int:
    """JSON 编码后的字节数(与落盘列同为 UTF-8)。"""
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _slim_strings(value: Any, limit: int = MAX_STRING_CHARS) -> Any:
    """递归截断长字符串(>limit 字 → 前 limit 字);容器原样重建。"""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit]
    if isinstance(value, dict):
        return {k: _slim_strings(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_slim_strings(v, limit) for v in value]
    return value


def _skeleton(step: dict[str, Any]) -> dict[str, Any]:
    """超预算单步的骨架:detail 清空,保留前端映射所需最小键。"""
    out = {k: _slim_strings(step.get(k)) for k in _SKELETON_KEYS}
    out["detail"] = {}
    return out


def trim_steps(
    steps: list[dict[str, Any]],
    *,
    max_steps: int = MAX_STEPS,
    max_string_chars: int = MAX_STRING_CHARS,
    step_budget_bytes: int = STEP_BUDGET_BYTES,
    total_budget_bytes: int = TOTAL_BUDGET_BYTES,
) -> tuple[list[dict[str, Any]], int]:
    """裁剪 SSE step 事件列表 → ``(保留步骤, 被截步数)``。

    前缀语义:一旦触顶(步数上限,或单步降级后仍超总预算),其后全部计入
    截断计数 —— 保留的总是**开头**,面板注记「其后 N 步未随历史保存」。
    """
    kept: list[dict[str, Any]] = []
    total = 0
    for step in steps:
        if len(kept) >= max_steps:
            break
        slim = _slim_strings(step, max_string_chars)
        if _json_size(slim) > step_budget_bytes:
            slim = _skeleton(step)
        size = _json_size(slim)
        if total + size > total_budget_bytes:
            break
        kept.append(slim)
        total += size
    return kept, len(steps) - len(kept)
