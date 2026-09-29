"""Server-Sent Events helpers (hand-rolled; no sse-starlette dependency).

Wire format per event:
    event: <type>
    data: <json>

"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse


def with_answer_source(data: dict[str, Any]) -> dict[str, Any]:
    """把终态总结里的答案来源档位提到事件顶层(设计 §7.3)。

    为什么在传输层再提一次:档位是前端**每一轮都要读**的一个标量(视觉上区分
    「已认证」/「语义编译」/「LLM 生成」),而 ``summary`` 是回答元数据的总包 ——
    让消费者为了一个字符串去里面挖,披露就变成了「可选的理解」。顶层一个字段,
    读到就是读到。

    读不出值(非终态事件 / 元数据问答 / 澄清反问 / 错误路径)就**不加字段**:这里
    不编造档位。I6 的「判定不出标 generated」是**判定期**的规则,那时确实有一条
    SQL 需要交代;而这些事件的回答根本不是 SQL 产的,贴一个 generated 是无中生有。

    返回新 dict,不动入参:同一个 summary 还被 trace 与会话落库引用。
    """
    summary = data.get("summary")
    source = summary.get("answer_source") if isinstance(summary, dict) else None
    if not isinstance(source, str) or not source:
        return data
    return {**data, "answer_source": source}


def sse_response(events: AsyncIterator[dict]) -> StreamingResponse:
    """Wrap an async iterator of {"type": str, "data": dict} into an SSE
    StreamingResponse (text/event-stream).

    出海前统一过一遍 ``with_answer_source``:披露字段只在**一处**成形,任何一条
    新的事件路径都不会漏掉它(漏掉的表现是前端静默不区分,没人报错)。
    """

    async def body() -> AsyncIterator[str]:
        async for event in events:
            payload = with_answer_source(event["data"])
            data = json.dumps(payload, ensure_ascii=False, default=str)
            yield f"event: {event['type']}\ndata: {data}\n\n"

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
