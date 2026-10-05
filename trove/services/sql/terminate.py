"""超时/取消后的**主动终止**(设计 §7.3 / §10 / I4)。

为什么单独成模块 —— 因为终止这件事在存量代码里**已经发生了,只是没人看得见**:
``DatabaseAdapter.interrupt()`` 早已存在(注册的方言如今都实现了它),各适配器在
``execute()`` 的 ``except asyncio.CancelledError`` 里已经调它,而
``asyncio.wait_for`` 超时时取消的正是那个协程。也就是说 ``timeout_ms`` 一到,
**kill 其实已经发出去了** —— 但 ``interrupt()`` 返回 None、异常吞进
``logger.debug``。

于是 §10 那两行处置是无处落地的:

| 情形 | 设计要求的处置 | 今天为什么做不到 |
|---|---|---|
| kill 不被方言支持 | 记 ``kill_unsupported`` | 没人问过适配器「你能不能」 |
| kill 发出但未生效 | 记 ``terminated="timeout"`` + WARN | 「发出」这个事实没有被观测 |

本模块把这两件事变成**可观测的事实**,而**不再发第二次 kill**:设计 §7.3 的
``QueryTerminator.kill(datasource, session_id)`` 让本模块自己发,可超时路径上
适配器已经发过一次 —— 照做就是重发,而 §10 明说「不重试 kill(可能误杀他人
会话)」。这是与 §7.3 的**有意偏差**(实施记录见设计 §17)。

同样有意的一处签名偏差:终止的**身份在适配器手里**(MySQL 的 ``thread_id``、
PostgreSQL 的连接、ClickHouse 的 ``query_id``),调用方给不出 ``session_id``
也**不该**给 —— 一个由上层拼出来的连接 ID 正是 R4 想避免的那种东西(拼错了
就杀别人)。所以判定按**数据源**问适配器,不按方言字符串:

    QueryTerminator.supports_terminate(datasource) -> bool
    QueryTerminator.terminate(datasource) -> TerminationResult

两条方向,贯穿全文件:

- **永不抛**(§10 / I6):调用点已经在超时收尾,这里再抛就是把「查询超时」换
  成「护栏自己崩了」。所有失败都折进 ``TerminationResult``。
- **不可终止 ≠ 终止失败**:``kill_unsupported`` 是静态可知的能力缺席(预期内),
  ``kill_failed`` 是运行时故障(要人看)。混成一个值,运维看到的东西完全不同
  —— 与 ``data_as_of`` 的三态是同一条纪律:不同的事实给不同的值。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from trove.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "DEFAULT_TERMINATE_TIMEOUT_S",
    "KILL_FAILED",
    "KILL_SENT",
    "KILL_UNSUPPORTED",
    "QueryTerminator",
    "TerminationResult",
]

#: 终止指令已发出(或本就没有在飞的查询),驱动没报错。**不是**「服务端已确认
#: 停止」—— 后者要再查一次库才知道,而复查一次的代价与收益不成比例(§10 那格
#: 「kill 发出但未生效」就是为它留的:答案照实写 ``terminated="timeout"``)。
KILL_SENT = "kill_sent"

#: 适配器**声明**不能终止(§7.3 矩阵里的 SQLite / DuckDB)。静态事实,不是故障。
KILL_UNSUPPORTED = "kill_unsupported"

#: 试过了,没成:适配器报 False、抛异常、或超过硬超时。
KILL_FAILED = "kill_failed"

#: 本模块自己的硬超时(秒)。**必须大于适配器自身的界**(MySQL 是 2.0s):
#: 一个有界的适配器不该被上层截断,这一层只兜住「没给自己设界」的实现
#: (SQLite / DuckDB / PostgreSQL 的 interrupt 原本都没有界)。
DEFAULT_TERMINATE_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class TerminationResult:
    """一次终止尝试的结果。**只有三个 kind**,值域即事实,不做二次解释。"""

    datasource: str
    kind: str
    #: 失败原因(``KILL_FAILED`` 时非空);成功与不支持时为空串
    error: str = ""

    @property
    def sent(self) -> bool:
        """终止指令发出去了吗。``KILL_UNSUPPORTED`` 与 ``KILL_FAILED`` 都是否
        —— 但**原因不同**,别把它们当成一回事去记账。
        """
        return self.kind == KILL_SENT


class QueryTerminator:
    """按数据源问适配器「能不能终止」,并在需要时发出终止(§7.3 / I4)。

    Args:
        registry: ``ConnectorRegistry``。判定与终止都经它解析到**具体的适配器
            —— 能力是适配器的属性,不是方言字符串的属性(同一个 `mysql` 方言,
            配了旁路连接的和只有单连接的适配器,能做的事不一样)。
        timeout_s: 硬超时(秒)。适配器没给自己设界时由这一层兜住。

    两个方法**都不抛**:解析不到数据源、适配器抛异常、适配器卡住,一律折成
    ``TerminationResult`` / ``False``,并至少留一条 WARN。
    """

    def __init__(
        self,
        registry: Any = None,
        *,
        timeout_s: float = DEFAULT_TERMINATE_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self.timeout_s = float(timeout_s)

    # ── 能力查询(执行前/超时前就能问)────────────────────

    async def supports_terminate(self, datasource: str | None) -> bool:
        """这个数据源能不能主动终止。解析不出来 → ``False``(**不抛**)。

        缺省方向是「不能」:没有声明能力的适配器一律按不能终止处理 ——
        少声明是安全方向(调用方退到「放弃等待,asyncio cancel 已是能做的
        全部」),多声明才是误诊。
        """
        try:
            adapter = await self._resolve(datasource)
        except Exception as e:
            logger.warning("terminate: 解析不到数据源 %r(%s),按不可终止处理",
                           datasource, e)
            return False
        return bool(getattr(adapter, "supports_interrupt", False))

    # ── 终止 ──────────────────────────────────────────────

    async def terminate(self, datasource: str | None) -> TerminationResult:
        """发出终止并如实报告结果(§10 / I4)。**永不抛**。

        ``KILL_SENT`` 的含义精确到「指令交给了驱动、驱动没报错」;终止是否在
        服务端生效,这一层**不声称**知道 —— 声称了就是 §10 那格的谎。
        """
        try:
            adapter = await self._resolve(datasource)
        except Exception as e:
            return TerminationResult(self._name(datasource), KILL_FAILED, str(e))

        name = self._name(datasource) or str(getattr(adapter, "name", "") or "")
        if not getattr(adapter, "supports_interrupt", False):
            # R4:声明不能终止就一个字都不发 —— 发了才有误杀他人会话的面
            return TerminationResult(name, KILL_UNSUPPORTED)

        try:
            sent = await asyncio.wait_for(adapter.interrupt(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            error = f"{name}: 终止超过 {self.timeout_s:g}s 硬超时,放弃等待"
            logger.warning("terminate: %s", error)
            return TerminationResult(name, KILL_FAILED, error)
        except Exception as e:
            error = f"{name}: {e}"
            logger.warning("terminate: 终止失败(%s)", error)
            return TerminationResult(name, KILL_FAILED, error)

        if not sent:
            error = f"{name}: 适配器报告终止指令未能发出"
            logger.warning("terminate: %s", error)
            return TerminationResult(name, KILL_FAILED, error)
        return TerminationResult(name, KILL_SENT)

    # ── 内部 ──────────────────────────────────────────────

    async def _resolve(self, datasource: str | None) -> Any:
        """解析到具体适配器;缺失/解析失败都抛给调用方折成结果。

        空名字 == 默认数据源(与 ``ProfileService`` 同一处理):把空串传下去
        会问出一个「没有这个数据源」,而真实意图是默认库。
        """
        if self._registry is None:
            raise RuntimeError("未装配数据源注册表")
        getter = getattr(self._registry, "get", None)
        if getter is None:
            raise RuntimeError("数据源注册表不支持按名解析")
        return await _maybe_await(getter(self._name(datasource) or None))

    @staticmethod
    def _name(datasource: str | None) -> str:
        return str(datasource or "").strip()


async def _maybe_await(value: Any) -> Any:
    """同步/异步依赖都接受 —— 真实注册表是 async,测试替身可能是同步的。"""
    if inspect.isawaitable(value):
        return await value
    return value
