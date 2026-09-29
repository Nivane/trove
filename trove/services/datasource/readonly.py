"""只读角色的启动自检(设计 §4 I1 / §10)。

权限轨的失败方向是 fail-open,这一条不改(§2.3):误拒会阻断正常业务,而真实
边界在数据库侧的只读角色上。**但「边界在那边」这句话本身得有人验** —— 这个模块
就是那个验证,它只做一件事:问清楚那个账号到底能不能写,然后**照实说**。

三条纪律:

* **只查权限表,绝不试写**。设计稿 §4 I1 给的另一条路是「尝试一条必然失败的写
  语句」——那条路永远不走:它「必然失败」的前提正是「账号只读」这个待证的假设,
  账号其实可写时它**真的会写进去**。把一个探测变成一次生产写入,比不做探测更坏。
* **不知道就说不知道**(``None``,见 ``ReadonlyProbe``)。缺省实现与所有异常路径
  都往这里倒,不往 ``True``(替边界背书)也不往 ``False``(误报故障)倒。
* **自检永远不能拖住启动**(I6 在启动层的形态):单个源有界,一个源炸了不影响
  别的源,连名字都列不出来也只留一份空报告。
"""

from __future__ import annotations

import asyncio
from typing import Any

from trove.core.logging import get_logger
from trove.core.types import (
    BASIS_PROBE_FAILED,
    BASIS_UNVERIFIABLE,
    ReadonlyProbe,
)

logger = get_logger(__name__)

#: 单个数据源的探测上限(秒)。比 ``INTERRUPT_TIMEOUT_S`` 宽一分:这条要往返
#: 一次权限表,不是置一个标志位。启动路径上等得起 3 秒,等不起 30 秒。
PROBE_TIMEOUT_S = 3.0


async def probe(adapter: Any) -> ReadonlyProbe:
    """问一个适配器它连的账号能不能写。**永不抛**。

    适配器自己承诺不抛,这里仍兜一层:自检挂在启动路径上,让一个探测异常把
    整个进程拦下来,与 I6「护栏故障不阻断」正好相反。
    """
    try:
        result = await asyncio.wait_for(
            adapter.probe_readonly(), timeout=PROBE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return ReadonlyProbe(
            None, BASIS_PROBE_FAILED, f"timeout after {PROBE_TIMEOUT_S}s",
        )
    except Exception as e:
        return ReadonlyProbe(
            None, BASIS_PROBE_FAILED, f"{type(e).__name__}: {e}",
        )
    if not isinstance(result, ReadonlyProbe):
        # 适配器返回了个别的东西 —— 说不知道,而不是把它当真值用
        return ReadonlyProbe(
            None, BASIS_PROBE_FAILED,
            f"probe returned {type(result).__name__}, not ReadonlyProbe",
        )
    return result


async def probe_all(registry: Any) -> dict[str, ReadonlyProbe]:
    """逐个源探测(并发),键为数据源名。

    并发而不是串行:每个源一次权限表往返,串行会让启动时间随数据源个数线性涨。
    每个源各自有界(``probe`` 里),所以并发不会因为一个慢源拖住整批。
    """
    try:
        names = list(registry.list_names())
    except Exception as e:
        logger.warning("readonly: 列不出数据源,自检跳过: %s", e)
        return {}
    if not names:
        return {}
    # 先顺序取实例再并发探测:``_adapter`` 是 async 的,写成生成器表达式塞进
    # gather 会得到一个 async generator(不能解包)。取实例这一步是连接复用,
    # 不是往返,不值得为它并发。
    adapters = [await _adapter(registry, n) for n in names]
    results = await asyncio.gather(*(probe(a) for a in adapters))
    return dict(zip(names, results))


async def _adapter(registry: Any, name: str) -> Any:
    """取适配器实例;取不到就给一个「什么都不会答」的替身。

    ``registry.get`` 在未连接时会抛 —— 那正是「这个源没探成」,不是自检的故障:
    交给 ``probe`` 折成一个 ``probe_failed`` 记录,而不是在这里中断整批。
    """

    class _NoAdapter:
        async def probe_readonly(self) -> ReadonlyProbe:
            raise RuntimeError(f"datasource {name!r} is not available")

    try:
        return await registry.get(name)
    except Exception:
        return _NoAdapter()


def describe(probe_result: ReadonlyProbe) -> str:
    """一行日志。**三态各自说得出口** —— 尤其 ``None`` 不能被写成「没事」。

    ``true`` / ``false`` / ``unknown`` 三个字面量留在串里:日志是给人读的,
    但「这台机器上到底有没有那道边界」也常是 grep 出来的,而 `verified=`
    后面跟的那个词一旦被改写成近义词,grep 就开始漏。

    **``detail`` 挂在末尾**。``types.py`` 对它的承诺是「人话依据,只进日志」,
    而 health 是刻意不要它的(免鉴权端点不回传库名/账号原文)—— 这里再不收,
    它就谁也不到:``verified=false`` 只在日志里留一句「写得动」,而运维要的
    是**为什么**(哪条授权、几个计数)。没有依据时不留悬空的分隔符。
    """
    if probe_result.verified is True:
        line = f"readonly check: verified=true (basis={probe_result.basis})"
    elif probe_result.verified is False:
        line = (
            "readonly check: verified=false — the account CAN write, so the "
            f"read-only boundary does not exist (basis={probe_result.basis})"
        )
    elif probe_result.basis == BASIS_UNVERIFIABLE:
        line = (
            "readonly check: verified=unknown — this dialect has no role "
            "concepts (file permissions only)"
        )
    else:
        line = (
            "readonly check: verified=unknown — the probe did not complete "
            f"(basis={probe_result.basis})"
        )
    return f"{line} — {probe_result.detail}" if probe_result.detail else line
