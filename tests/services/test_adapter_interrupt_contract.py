"""适配器的**终止能力声明**必须与代码一致(设计 §7.3 / §10 / I4)。

为什么值得一条专门的元测试:``supports_interrupt`` 是给 ``QueryTerminator``
判「能不能发终止」用的,而它的两个错法都很安静 ——

* **声明 True 却没实现** → 超时路径记下 ``kill_sent``,而其实一个字都没发;
  运维看着证据以为查询被停掉了。
* **实现了却没声明** → 超时路径记 ``kill_unsupported``,能力白写了;
  更糟的是没人会发现,因为两边都不报错。

两个方向都是「证据说了谎」,而证据的价值全在它不说谎。声明与代码的一致性因此
**由测试钉住**,不靠 review 记得。
"""

from trove.services.datasource.adapters.base import DatabaseAdapter
from trove.services.datasource.registry import _ADAPTER_REGISTRY


class TestTheDeclarationMatchesTheCode:
    def test_declared_capability_equals_an_actual_override(self):
        """``supports_interrupt`` 为真 ⇔ 这个类真的覆写了 ``interrupt``。"""
        for dialect, cls in _ADAPTER_REGISTRY.items():
            overridden = cls.interrupt is not DatabaseAdapter.interrupt
            assert cls.supports_interrupt == overridden, (
                f"{dialect}: supports_interrupt={cls.supports_interrupt} "
                f"但覆写 interrupt={overridden}"
            )

    def test_every_registered_dialect_can_be_terminated(self):
        """今天六个方言**都**能终止 —— 与设计 §7.3 的矩阵有一处不一致:
        矩阵把 SQLite / DuckDB 写成「不支持(进程内,asyncio cancel 已足够)」,
        而存量代码里两者都实现了真正的跨线程取消。

        代码是对的:``asyncio`` 取消只是**放弃等待**,那个阻塞在驱动里的工作
        线程照跑(``aiosqlite`` 的 ``interrupt()`` 甚至排队到同一个工作线程,
        卡住时永远排不到)。所以「进程内」不构成「不需要终止」的理由 ——
        这条测试的作用是:将来有人按那张矩阵把声明改回 False 时,他必须先
        回答这里的问题。
        """
        for dialect, cls in _ADAPTER_REGISTRY.items():
            assert cls.supports_interrupt is True, f"{dialect} 未声明可终止"


class TestTheBaseContract:
    async def test_the_base_default_is_a_no_op_that_reports_nothing_sent(self):
        """基类不声明能力,``interrupt`` 也就没什么可报的 —— 返回 False
        (「没有发出任何东西」),而不是 True(「已终止」)。
        """

        class Bare(DatabaseAdapter):
            @staticmethod
            def dialect() -> str: return "bare"
            async def connect(self) -> None: ...
            async def disconnect(self) -> None: ...
            async def execute(self, sql): ...
            async def get_schema(self): ...
            async def get_capabilities(self): ...

        adapter = Bare(name="bare", config={})

        assert adapter.supports_interrupt is False
        assert await adapter.interrupt() is False
