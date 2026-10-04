"""IM 投递传输 —— 一个可注入的 ``transport`` + 默认 httpx 实现。

与 ``action/dispatcher.py`` 同款契约:``transport(url, payload, headers,
timeout_s) -> (status, text)``。可注入是测试纪律的一部分 —— 行动/任务两侧
的 IM 路径都要能在**零真实网络**下被完整走一遍(仓库硬约束)。

``httpx`` 在函数内惰性导入,与数据源驱动同一手法:不投递的部署不为它付
导入成本。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

#: 与 ``action.dispatcher.Transport`` 同形 —— 两侧都用这一个形状,注入的假
#: transport 因此可以直接复用(行动测试里的那个也能喂给 IMNotifier)。
Transport = Callable[[str, dict, dict, float], Awaitable[tuple[int | None, str]]]

DEFAULT_HEADERS = {"Content-Type": "application/json"}


async def post_json(
    url: str, payload: dict, headers: dict, timeout_s: float,
) -> tuple[int | None, str]:
    """默认 transport:POST 一份 JSON,返回 ``(status, text)``。"""
    import httpx

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=payload, headers=headers)
        return resp.status_code, resp.text or ""


async def deliver(
    url: str, payload: dict[str, Any], *, kind: str = "generic",
    headers: dict | None = None, timeout_s: float = 10.0,
    transport: Transport | None = None,
) -> tuple[int | None, str]:
    """塑形后投递:``shape_payload`` → ``transport``(默认 :func:`post_json`)。

    这是 jobs 侧 IM 家族的发送内核;行动侧走的是自己的分发器(它按通道
    spec 里的 ``kind`` 调同一个 :func:`~trove.services.im.shape.shape_payload`)。
    """
    from trove.services.im.shape import shape_payload

    body = shape_payload(payload, kind=kind)
    return await (transport or post_json)(
        url, body, dict(headers or DEFAULT_HEADERS), float(timeout_s))
