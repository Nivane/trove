"""只读角色自检(设计 §4 I1)的折向与三态。

这一层要防的是**假阳性比假阴性坏得多**:把「不知道」报成「确认只读」,
等于替一道并不存在的硬边界背书 —— 而 I1 的全部价值就是那道边界真的存在。
所以下面每条用例断的都是同一件事的两面:不知道就说不知道,别往安全那边倒。
"""

import asyncio

import pytest

from trove.core.types import (
    BASIS_GRANTS,
    BASIS_PROBE_FAILED,
    BASIS_UNVERIFIABLE,
    ReadonlyProbe,
)
from trove.services.datasource import readonly
from trove.services.datasource.adapters.sqlite import SQLiteAdapter


class _Adapter:
    """只实现 ``probe_readonly`` 的桩 —— 自检只看这一个方法。"""

    def __init__(self, result=None, error=None, delay=0.0):
        self._result = result
        self._error = error
        self._delay = delay
        self.calls = 0

    async def probe_readonly(self) -> ReadonlyProbe:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return self._result


class _Registry:
    def __init__(self, adapters):
        self._adapters = adapters

    def list_names(self):
        return list(self._adapters)

    async def get(self, name):
        return self._adapters[name]


class TestUnknownIsNotSafe:
    async def test_an_adapter_without_a_probe_is_unknown_not_safe(self):
        """没实现探测的适配器必须报「不知道」。

        基类的缺省值只有两个诚实选项:``None/unverifiable``(不知道)或
        ``False``(写得动)。**绝不能是 ``True``** —— 那正是「我们没实现」被
        渲染成「已确认只读」的地方,而这句谎的成本由运维承担。
        """
        adapter = SQLiteAdapter(name="t", config={"path": ":memory:"})

        probe = await readonly.probe(adapter)

        assert probe.verified is None
        assert probe.basis == BASIS_UNVERIFIABLE

    async def test_a_probe_that_explodes_is_probe_failed_not_safe(self):
        """探测自己炸了 ≠ 账号只读。两者在布尔里同形,所以不能只有布尔。"""
        adapter = _Adapter(error=RuntimeError("permission denied"))

        probe = await readonly.probe(adapter)

        assert probe.verified is None
        assert probe.basis == BASIS_PROBE_FAILED

    async def test_a_probe_that_hangs_is_bounded(self, monkeypatch):
        """自检不能把启动拖住(§10:画像是增强,自检同样是)。

        超时与异常折成同一个结论,但**都要留下那句原始信息** ——
        「卡住了」与「被拒了」对运维是两个修法。
        """
        monkeypatch.setattr(readonly, "PROBE_TIMEOUT_S", 0.05)
        adapter = _Adapter(
            result=ReadonlyProbe(True, BASIS_GRANTS), delay=5,
        )

        probe = await readonly.probe(adapter)

        assert probe.verified is None
        assert probe.basis == BASIS_PROBE_FAILED
        assert "timeout" in probe.detail.lower()


class TestProbeAll:
    async def test_every_registered_datasource_gets_a_row(self):
        """一个源一条记录 —— 缺行会让健康检查把它渲染成「没探过」,那是另一回事。"""
        registry = _Registry({
            "a": _Adapter(result=ReadonlyProbe(True, BASIS_GRANTS, "no write grants")),
            "b": _Adapter(result=ReadonlyProbe(False, BASIS_GRANTS, "GRANT ALL")),
        })

        report = await readonly.probe_all(registry)

        assert set(report) == {"a", "b"}
        assert report["a"].verified is True
        assert report["b"].verified is False

    async def test_one_broken_datasource_does_not_drop_the_others(self):
        """自检是**逐个的**,不是一次的:一个源炸了不该让别的源看起来没探过。

        与 §10「画像故障退下一档而非打断主链路」同一条:I6 在自检层的形态。
        """
        registry = _Registry({
            "good": _Adapter(result=ReadonlyProbe(True, BASIS_GRANTS)),
            "bad": _Adapter(error=RuntimeError("boom")),
        })

        report = await readonly.probe_all(registry)

        assert report["good"].verified is True
        assert report["bad"].verified is None
        assert report["bad"].basis == BASIS_PROBE_FAILED

    async def test_a_registry_that_cannot_even_list_is_an_empty_report(self):
        """列不出名字 → 空报告,不抛。启动自检永远不能让进程起不来。"""

        class _Broken:
            def list_names(self):
                raise RuntimeError("registry is on fire")

        assert await readonly.probe_all(_Broken()) == {}


class TestProbeShape:
    def test_unknown_carries_no_verdict_in_either_direction(self):
        """``verified`` 是三值,不是「真/假」—— 断言它不会退化成布尔。"""
        assert ReadonlyProbe(True, BASIS_GRANTS).verified is True
        assert ReadonlyProbe(False, BASIS_GRANTS).verified is False
        assert ReadonlyProbe(None, BASIS_PROBE_FAILED).verified is None

    def test_detail_is_kept_out_of_the_health_payload(self):
        """``detail`` 是给人看的**日志**字段,不进 HTTP 响应。

        健康检查的既有纪律是「错误只报类型名,不回传驱动原文」(避免凭据/主机
        信息入响应);而这个 detail 还会带上库里的对象名与授权原文。它进入
        ``to_health()`` 的那天,那条纪律就被绕过去了。
        """
        probe = ReadonlyProbe(
            False, BASIS_GRANTS, "GRANT ALL PRIVILEGES ON `orders`.* TO `u`@`%`",
        )

        health = probe.to_health()

        assert health == {"verified": False, "basis": BASIS_GRANTS}
        assert "orders" not in str(health)


@pytest.mark.parametrize(
    "probe, expected",
    [
        (ReadonlyProbe(True, BASIS_GRANTS), "true"),
        (ReadonlyProbe(False, BASIS_GRANTS), "false"),
        (ReadonlyProbe(None, BASIS_UNVERIFIABLE), "unknown"),
        (ReadonlyProbe(None, BASIS_PROBE_FAILED), "unknown"),
    ],
)
def test_log_line_says_all_three_states_in_words(probe, expected):
    """日志里三态各自说得出口 —— 「unknown」不能被打成「不能验证=没事」。"""
    assert expected in readonly.describe(probe)


@pytest.mark.parametrize(
    "probe",
    [
        ReadonlyProbe(True, BASIS_GRANTS, "3 grant line(s), none of them a write"),
        ReadonlyProbe(False, BASIS_GRANTS, "GRANT INSERT ON `shop`.* TO `trove`@`%`"),
        ReadonlyProbe(None, BASIS_PROBE_FAILED, "timeout after 3.0s"),
    ],
)
def test_the_log_line_carries_the_evidence(probe):
    """依据要**真的**进日志。

    ``types.py`` 对 ``detail`` 的承诺是「人话依据,**只进日志**」,而 health 是
    刻意不要它的(免鉴权端点不回传库名/账号原文)。两边都不收的话它就谁也不到:
    ``verified=false`` 只在日志里留一句「写得动」,运维看不到**为什么** ——
    而「为什么」是这个字段存在的全部理由(MySQL 留授权原文、PG 留三个计数、
    CH 留写权限条数,都是给这一刻准备的)。
    """
    assert probe.detail in readonly.describe(probe)


def test_an_empty_detail_leaves_no_dangling_separator():
    """没有依据时不能留一个空的「 —— 」。

    空 detail 是正常情形(``unverifiable`` 这种压根没有依据可言),拼一个悬空
    的分隔符会让日志看起来像「有话说但被吞了」。
    """
    assert readonly.describe(ReadonlyProbe(True, BASIS_GRANTS)) == (
        "readonly check: verified=true (basis=grants)"
    )
