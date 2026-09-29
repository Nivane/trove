"""QueryTerminator —— 超时/取消后的主动终止(设计 §7.3 / §10 / I4)。

这一层存在的**唯一**理由是让终止**可观测**:终止动作本身早就发生了
(``DatabaseAdapter.interrupt()``,各适配器在 ``execute()`` 的
``except CancelledError`` 里调),但它返回 None、异常吞进 ``logger.debug``,
于是「发没发出去、这个方言支不支持」在答案里一个字都没有 —— §10 的两行
处置(``kill_unsupported`` / ``terminated="timeout"``)因此无处落地。

两个方向要钉住:

1. **永不抛**(§10 / I6):调用点已经在超时收尾,这里再抛就是把「查询超时」
   换成「护栏自己崩了」。
2. **不可终止 ≠ 终止失败**(三态纪律):``kill_unsupported`` 是「这个方言/适配器
   没有这个能力」(预期内的、静态可知的),``kill_failed`` 是「试了,没成」
   (运行时故障)。两者混成一个值,运维看到的东西就完全不一样了。
"""

import asyncio

import pytest

from trove.services.sql.terminate import (
    KILL_FAILED,
    KILL_SENT,
    KILL_UNSUPPORTED,
    QueryTerminator,
)


class FakeAdapter:
    """鸭子类型的适配器:只需要 ``supports_interrupt`` + ``interrupt()``。"""

    def __init__(
        self,
        *,
        supports: bool = True,
        result: bool = True,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.supports_interrupt = supports
        self._result = result
        self._error = error
        self._delay = delay
        self.interrupts = 0

    async def interrupt(self) -> bool:
        self.interrupts += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result


class FakeRegistry:
    def __init__(self, adapter=None, error: Exception | None = None) -> None:
        self._adapter = adapter
        self._error = error
        self.asked: list[str | None] = []

    async def get(self, name=None):
        self.asked.append(name)
        if self._error is not None:
            raise self._error
        return self._adapter


class TestTerminationOutcome:
    async def test_an_adapter_that_cannot_be_interrupted_is_recorded_as_unsupported(self):
        adapter = FakeAdapter(supports=False)
        term = QueryTerminator(FakeRegistry(adapter))

        result = await term.terminate("warehouse")

        assert result.kind == KILL_UNSUPPORTED
        # 声明不可终止就**一个字都不发** —— 发了才有「打错连接 ID 误杀他人会话」
        # 的风险(R4),而这个方言连发的意义都没有
        assert adapter.interrupts == 0

    async def test_the_termination_goes_through_the_adapters_own_interrupt(self):
        adapter = FakeAdapter()
        term = QueryTerminator(FakeRegistry(adapter))

        result = await term.terminate("warehouse")

        assert result.kind == KILL_SENT
        assert adapter.interrupts == 1

    async def test_an_interrupt_that_reports_failure_is_not_dressed_up_as_sent(self):
        adapter = FakeAdapter(result=False)
        term = QueryTerminator(FakeRegistry(adapter))

        result = await term.terminate("warehouse")

        assert result.kind == KILL_FAILED
        assert "warehouse" in result.error

    async def test_a_raising_interrupt_never_reaches_the_caller(self):
        adapter = FakeAdapter(error=RuntimeError("side connection refused"))
        term = QueryTerminator(FakeRegistry(adapter))

        result = await term.terminate("warehouse")  # 不抛

        assert result.kind == KILL_FAILED
        assert "side connection refused" in result.error

    async def test_a_hanging_interrupt_is_bounded_by_the_terminator(self):
        adapter = FakeAdapter(delay=5.0)
        term = QueryTerminator(FakeRegistry(adapter), timeout_s=0.05)

        result = await asyncio.wait_for(term.terminate("warehouse"), timeout=1.0)

        assert result.kind == KILL_FAILED
        # 与「驱动报错」分开说:一种是适配器坏了(它自己会 WARN 原因),一种是
        # 适配器根本没给自己设界 —— 后者的修法在适配器里,不在调用方
        assert "超时" in result.error and "0.05" in result.error

    async def test_an_unresolvable_datasource_is_reported_not_raised(self):
        term = QueryTerminator(FakeRegistry(error=RuntimeError("no such datasource")))

        result = await term.terminate("ghost")  # 不抛

        assert result.kind == KILL_FAILED
        assert "no such datasource" in result.error

    async def test_an_adapter_that_never_declared_is_treated_as_unsupported(self):
        """**没有声明 ≠ 可以终止**。缺省方向必须是「不能」(与 §8.2 的
        ``capabilities`` 同一条纪律:少声明是安全方向,多声明是误诊)。
        """

        class Undeclared:
            async def interrupt(self) -> bool:  # pragma: no cover - 不该被调用
                raise AssertionError("不该被调用")

        term = QueryTerminator(FakeRegistry(Undeclared()))

        result = await term.terminate("warehouse")

        assert result.kind == KILL_UNSUPPORTED


class TestCapabilityQuery:
    async def test_a_dialect_without_the_capability_says_so_before_the_timeout(self):
        term = QueryTerminator(FakeRegistry(FakeAdapter(supports=False)))

        assert await term.supports_terminate("warehouse") is False

    async def test_a_capable_dialect_says_so(self):
        term = QueryTerminator(FakeRegistry(FakeAdapter()))

        assert await term.supports_terminate("warehouse") is True

    async def test_an_unresolvable_datasource_answers_false_not_raises(self):
        term = QueryTerminator(FakeRegistry(error=RuntimeError("boom")))

        assert await term.supports_terminate("ghost") is False

    async def test_the_datasource_name_is_the_one_asked_about(self):
        registry = FakeRegistry(FakeAdapter())
        term = QueryTerminator(registry)

        await term.supports_terminate("warehouse")

        assert registry.asked == ["warehouse"]

    async def test_an_empty_datasource_falls_back_to_the_default(self):
        """名字为空 = 默认数据源(与 ``ProfileService`` 同一处理)。传空串下去
        会问出一个「没有这个数据源」,而真实意图是默认库。
        """
        registry = FakeRegistry(FakeAdapter())
        term = QueryTerminator(registry)

        await term.supports_terminate("")

        assert registry.asked == [None]


class TestTheQueryPathOfTheContract:
    """``terminate`` 的第三条路:数据源缺席。

    ``datasource=""``(未指定)与``datasource=None``都要落到默认源上 —— 传空串
    下去会问出一个「没有这个数据源」,把「没指定」误报成「不存在」。
    """

    @pytest.mark.parametrize("name", ["", None])
    async def test_an_absent_datasource_falls_back_to_the_default(self, name):
        registry = FakeRegistry(FakeAdapter())
        term = QueryTerminator(registry)

        await term.terminate(name)

        assert registry.asked == [None]

    async def test_sent_is_the_only_kind_that_reads_as_done(self):
        outcomes = {}
        for kind, adapter in (
            (KILL_SENT, FakeAdapter()),
            (KILL_UNSUPPORTED, FakeAdapter(supports=False)),
            (KILL_FAILED, FakeAdapter(result=False)),
        ):
            outcomes[kind] = (await QueryTerminator(FakeRegistry(adapter)).terminate("w")).sent

        assert outcomes == {KILL_SENT: True, KILL_UNSUPPORTED: False, KILL_FAILED: False}
