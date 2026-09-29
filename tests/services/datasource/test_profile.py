"""元数据画像(执行画像设计 §6.1 / §7.1 / §8.2 / §8.4)。

两件事在这个文件里被钉死,它们都是**方向**问题而不是功能问题:

* **不可得 ≠ 0**(§6.1)。画像查不到就是 ``None`` 并且**不进 capabilities** ——
  用 0 或空串冒充「已知」,下游会把它当成一个真实的观测值。这与能力①的
  「空报告 ≠ 无漂移」是同一条原则。
* **I5:未知必须说未知**。``data_as_of`` 不可得时是 ``null`` + ``basis="unknown"``,
  **不得用查询时间冒充** —— 那会让「这份数据截止到什么时候」这个产品级承诺
  变成一句谎话,而且用户看不出来。

第三件是工程约束(§10):画像查库本身可能很慢,所以有 TTL 缓存 + 超时跳过;
超时跳过之后必须退到 ``None`` 而不是抛出去打断主链路(I6)。
"""

from __future__ import annotations


from trove.core.types import ColumnInfo, SchemaInfo, TableInfo
from trove.services.datasource.profile import (
    DatasetFreshness,
    ProfileService,
    TableProfile,
)


def _table(name, rows=None, **kw):
    return TableInfo(
        name=name, schema="main",
        columns=[ColumnInfo(name="id", type="INTEGER")],
        row_count_estimate=rows, **kw,
    )


class _FakeRegistry:
    """只实现画像要用的那一面:``get``(取适配器)、``get_schema``、``dialect_of``。"""

    default_name = "db"

    def __init__(self, tables=(), *, dialect="postgres", explode=False, adapter=None):
        self.tables = list(tables)
        self.calls = 0
        self._dialect = dialect
        self._explode = explode
        self._adapter = adapter

    async def get(self, datasource=None):
        if self._adapter is None:
            # 真实 registry 在数据源未注册时也会抛 —— 走回退路径
            raise KeyError(f"datasource {datasource!r} not registered")
        return self._adapter

    async def get_schema(self, datasource=None):
        self.calls += 1
        if self._explode:
            raise RuntimeError("registry is on fire")
        return SchemaInfo(tables=self.tables)

    def dialect_of(self, datasource=None) -> str:
        return self._dialect


class TestAdapterIsTheSource:
    """画像的**首选来源是适配器**(§8.2 B),``get_schema`` 只是回退。

    这条接法决定了逐方言实现能不能落地:ClickHouse 能用 ``system.tables`` 一行
    拿到字节数,SQLite 什么都拿不到 —— 差异由适配器自己声明,画像层不判断方言。
    """

    async def test_uses_the_adapter_profile_when_available(self):
        from trove.core.types import TableProfile

        class _Adapter:
            async def table_profiles(self):
                return {"a": TableProfile(
                    table="a", row_count=100, bytes=4096,
                    capabilities=frozenset({"row_count", "bytes"}),
                )}

        reg = _FakeRegistry([_table("a", 100)], adapter=_Adapter())
        svc = ProfileService(reg)
        p = await svc.table_profile("db", "a")
        assert p.bytes == 4096
        assert "bytes" in await svc.capabilities("db")
        # 适配器给了画像就不该再抓一遍 schema —— 一次刷新一个往返(§10)
        assert reg.calls == 0

    async def test_falls_back_to_schema_when_there_is_no_adapter(self):
        """``get()`` 抛(数据源未注册 / 拿不到连接)→ 退到 schema,不抛。"""
        reg = _FakeRegistry([_table("a", 100)])
        svc = ProfileService(reg)
        assert await svc.estimate_rows("db", ["a"]) == 100
        assert reg.calls == 1

    async def test_adapter_failure_yields_no_basis_not_a_schema_fallback(self):
        """适配器报错(**数据源在,但画像查询失败**)→ 这一档没有依据,不抛。

        这里刻意**不**回退去抓 ``get_schema``:画像查询是这条链上**更便宜**的那
        个(ClickHouse 一次 ``system.tables`` vs ``get_schema`` 的每表一次列查询),
        便宜的那个失败了,再去做贵的那个只会把延迟加上去,而且多半同样失败
        —— 拿 §10 的延迟约束去赌一个大概率不成立的回退。

        没有依据就走保守预算(I3),那条路本来就是为这一刻准备的。
        """

        class _Broken:
            async def table_profiles(self):
                raise RuntimeError("system.tables 权限不足")

        reg = _FakeRegistry([_table("a", 100)], adapter=_Broken())
        svc = ProfileService(reg)
        assert await svc.estimate_rows("db", ["a"]) is None
        assert reg.calls == 0  # 没有偷偷去抓一遍全库 schema


# ── 行数画像(预算降级链的第 2 档)────────────────────────


class TestEstimateRows:
    async def test_sums_the_row_counts_of_the_named_tables(self):
        reg = _FakeRegistry([_table("a", 100), _table("b", 200), _table("c", 999)])
        svc = ProfileService(reg)
        assert await svc.estimate_rows("db", ["a", "b"]) == 300

    async def test_unknown_table_contributes_nothing(self):
        """SQL 里的 CTE 名、别名、schema 前缀都可能对不上真实表 —— 跳过它们,
        不要因为其中一个认不出来就放弃整次估算。"""
        reg = _FakeRegistry([_table("a", 100)])
        svc = ProfileService(reg)
        assert await svc.estimate_rows("db", ["a", "cte_x", "other.t"]) == 100

    async def test_no_known_table_at_all_is_none_not_zero(self):
        """**0 不是依据。** 全部认不出来 → ``None`` → 退保守预算。

        返回 0 会让一条扫全表的查询被判成零成本,方向恰好是**放宽** ——
        而这正是本模块要修的那个方向(同 ``budget._positive``)。
        """
        reg = _FakeRegistry([_table("a", 100)])
        svc = ProfileService(reg)
        assert await svc.estimate_rows("db", ["nope"]) is None

    async def test_missing_row_count_is_not_a_zero(self):
        """适配器报不出行数时 ``row_count_estimate`` 是 ``None``,不是 0。

        存量有适配器写成 ``int(row_count or 0)`` —— 那让「不知道」和「空表」
        在画像里长得一模一样。这里把两者都当**没有依据**,方向安全。
        """
        reg = _FakeRegistry([_table("a", None), _table("b", 0)])
        svc = ProfileService(reg)
        assert await svc.estimate_rows("db", ["a", "b"]) is None

    async def test_empty_table_list_short_circuits(self):
        """没有表名可查就别去查 —— 一次全库 schema 抓取不便宜。"""
        reg = _FakeRegistry([_table("a", 100)])
        svc = ProfileService(reg)
        assert await svc.estimate_rows("db", []) is None
        assert reg.calls == 0

    async def test_a_broken_registry_yields_none_not_an_exception(self):
        """I6:画像故障退到下一档(保守预算),不打断查询链路。"""
        svc = ProfileService(_FakeRegistry([_table("a", 1)], explode=True))
        assert await svc.estimate_rows("db", ["a"]) is None


# ── 缓存与超时(§10)──────────────────────────────────────


class TestCacheAndTimeout:
    async def test_second_lookup_within_ttl_does_not_hit_the_source(self):
        """§10 R3:画像查询会增加延迟 —— 缓存住,不进主链路重复抓。"""
        reg = _FakeRegistry([_table("a", 100)])
        now = [0.0]
        svc = ProfileService(reg, ttl_s=300, clock=lambda: now[0])

        await svc.estimate_rows("db", ["a"])
        await svc.estimate_rows("db", ["a"])
        assert reg.calls == 1

    async def test_lookup_after_ttl_refetches(self):
        reg = _FakeRegistry([_table("a", 100)])
        now = [0.0]
        svc = ProfileService(reg, ttl_s=300, clock=lambda: now[0])

        await svc.estimate_rows("db", ["a"])
        now[0] = 301.0
        await svc.estimate_rows("db", ["a"])
        assert reg.calls == 2

    async def test_cache_is_per_datasource(self):
        """缓存键漏掉数据源 → 多数据源部署里 A 库的行数被当成 B 库的。"""
        reg = _FakeRegistry([_table("a", 100)])
        now = [0.0]
        svc = ProfileService(reg, ttl_s=300, clock=lambda: now[0])

        await svc.estimate_rows("db1", ["a"])
        await svc.estimate_rows("db2", ["a"])
        assert reg.calls == 2

    async def test_slow_lookup_is_skipped_not_awaited_forever(self):
        """§10:画像很慢时**跳过** —— 它是增强,不该拖慢主链路。

        超时后给 ``None``(退保守预算),不是抛异常。
        """
        import asyncio

        class _Slow(_FakeRegistry):
            async def get_schema(self, datasource=None):
                await asyncio.sleep(5)
                return SchemaInfo(tables=[_table("a", 100)])

        svc = ProfileService(_Slow(), timeout_s=0.01)
        assert await svc.estimate_rows("db", ["a"]) is None


# ── capabilities 与「不可得 ≠ 0」─────────────────────────


class TestCapabilities:
    async def test_declares_only_what_the_adapter_actually_reports(self):
        """§6.1:显式声明支持哪些字段,而不是返回 0 或空串冒充「已知」。"""
        reg = _FakeRegistry([_table("a", 100)])  # 只有行数
        svc = ProfileService(reg)
        caps = await svc.capabilities("db")
        assert "row_count" in caps
        assert "bytes" not in caps

    async def test_unsupported_fields_are_none_not_empty(self):
        reg = _FakeRegistry([_table("a", 100)])
        svc = ProfileService(reg)
        p = await svc.table_profile("db", "a")
        assert p.row_count == 100
        assert p.bytes is None
        assert p.last_modified is None
        assert p.latest_partition is None
        assert p.partition_column is None

    async def test_unknown_table_yields_a_profile_with_no_capabilities(self):
        """表都不认识 → 每个字段都是 None、capabilities 为空。

        **不返回 None**:「这张表查不到」与「查询失败了」对调用方是两回事,
        前者是信息,后者是故障。
        """
        svc = ProfileService(_FakeRegistry([_table("a", 100)]))
        p = await svc.table_profile("db", "nope")
        assert p.table == "nope"
        assert p.row_count is None
        assert p.capabilities == frozenset()


# ── 新鲜度:I5 与 §8.4 ──────────────────────────────────


class TestFreshness:
    async def test_reports_unknown_rather_than_pretending(self):
        """**I5:不得用查询时间冒充 ``data_as_of``。**

        没有任何一个表能报出截止时间时,``as_of`` 是 ``None`` 且
        ``basis="unknown"`` —— 产品级承诺宁缺毋滥。
        """
        svc = ProfileService(_FakeRegistry([_table("a", 100)]))
        f = await svc.freshness("db", ["a"])
        assert isinstance(f, DatasetFreshness)
        assert f.as_of is None
        assert f.basis == "unknown"

    async def test_as_of_is_the_oldest_of_the_referenced_tables(self):
        """答案的截止时间 = 所引用表里**最旧**的那个 —— 取最新的会让答案
        看起来比它实际依据的数据更新。"""
        reg = _FakeRegistry([_table("a", 1), _table("b", 1)])
        svc = ProfileService(reg, profiles={
            "a": TableProfile(table="a", row_count=1, bytes=None,
                              last_modified="2026-09-20T00:00:00Z", last_analyzed=None,
                              partition_column=None, partition_count=None,
                              latest_partition=None, capabilities=frozenset({"last_modified"})),
            "b": TableProfile(table="b", row_count=1, bytes=None,
                              last_modified="2026-09-25T00:00:00Z", last_analyzed=None,
                              partition_column=None, partition_count=None,
                              latest_partition=None, capabilities=frozenset({"last_modified"})),
        })
        assert (await svc.freshness("db", ["a", "b"])).as_of == "2026-09-20T00:00:00Z"

    async def test_partition_basis_wins_over_last_modified(self):
        """§8.4 C:分区表上 ``last_modified`` 反映的是**元数据变更**(可能是加了
        个分区),不代表数据新鲜 → 有分区就优先按分区判断。"""
        reg = _FakeRegistry([_table("a", 1)])
        svc = ProfileService(reg, profiles={
            "a": TableProfile(table="a", row_count=1, bytes=None,
                              last_modified="2026-09-28T00:00:00Z", last_analyzed=None,
                              partition_column="dt", partition_count=30,
                              latest_partition="2026-09-20",
                              capabilities=frozenset({"last_modified", "latest_partition"})),
        })
        f = await svc.freshness("db", ["a"])
        assert f.as_of == "2026-09-20"
        assert f.basis == "latest_partition"

    async def test_mixed_basis_prefers_the_conservative_one(self):
        """两张表口径不同(A 只有 last_modified、B 有分区)→ 取**最旧**的那个,
        ``basis`` 报产生它的那张表用的口径。

        诱人的替代规则是「分区口径更可信,那就整片用分区的」—— 它会把这边的
        A **整个丢出聚合**:A 哪怕三年没更新,只要同库另一张表有分区,截止时间
        就与 A 无关了,答案于是看起来比它实际依据的数据更新。取最旧不会犯这个
        错:代价只是低估新鲜度,而低估是安全方向。
        """
        reg = _FakeRegistry([_table("a", 1), _table("b", 1)])
        svc = ProfileService(reg, profiles={
            "a": TableProfile(table="a", row_count=1, bytes=None,
                              last_modified="2026-09-01T00:00:00Z", last_analyzed=None,
                              partition_column=None, partition_count=None,
                              latest_partition=None,
                              capabilities=frozenset({"last_modified"})),
            "b": TableProfile(table="b", row_count=1, bytes=None,
                              last_modified=None, last_analyzed=None,
                              partition_column="dt", partition_count=5,
                              latest_partition="2026-09-20",
                              capabilities=frozenset({"latest_partition"})),
        })
        f = await svc.freshness("db", ["a", "b"])
        assert f.basis == "last_modified"
        assert f.as_of == "2026-09-01T00:00:00Z"


# ── 与 registry 的形状对齐 ──────────────────────────────


class TestRegistrySeam:
    async def test_defaults_to_the_registry_default_datasource(self):
        reg = _FakeRegistry([_table("a", 100)])
        svc = ProfileService(reg)
        assert await svc.estimate_rows("", ["a"]) == 100

    def test_is_constructible_from_a_registry_alone(self):
        """生产只传 registry —— 其余全有默认值,免得装配处抄一遍常量。"""
        assert ProfileService(_FakeRegistry()).ttl_s == 300


class TestFailuresAreCachedToo:
    """§11 R3:故障也要缓存 —— 否则一个坏后端让**每次回答**都等满超时。

    缓存住失败不是「假装成功」:返回的仍是空画像(退保守预算),变的只是
    「多久问一次」。窗口比正常 TTL 短得多 —— 后端恢复了要尽快能恢复,
    30 秒的陈旧远比每次 2 秒的等待便宜。
    """

    async def _always_slow(self, *, explode=False):
        import asyncio

        class _Slow(_FakeRegistry):
            async def get_schema(self, datasource=None):
                self.calls += 1
                if explode:
                    raise RuntimeError("registry is on fire")
                await asyncio.sleep(5)
                return SchemaInfo(tables=[_table("a", 100)])

        return _Slow()

    async def test_a_timing_out_source_is_asked_once_not_twice(self):
        reg = await self._always_slow()
        now = [0.0]
        svc = ProfileService(reg, timeout_s=0.01, ttl_s=300, clock=lambda: now[0])

        assert await svc.estimate_rows("db", ["a"]) is None
        assert await svc.estimate_rows("db", ["a"]) is None
        assert reg.calls == 1, "坏后端被问了两次 —— 每次回答都要再等满超时"

    async def test_an_exploding_source_is_asked_once_not_twice(self):
        """故障与超时同等对待:两者都是「这一档暂时没依据」。"""
        reg = await self._always_slow(explode=True)
        now = [0.0]
        svc = ProfileService(reg, timeout_s=0.01, ttl_s=300, clock=lambda: now[0])

        assert await svc.estimate_rows("db", ["a"]) is None
        assert await svc.estimate_rows("db", ["a"]) is None
        assert reg.calls == 1

    async def test_the_source_is_retried_once_the_window_expires(self):
        """窗口过后必须**重试** —— 永久记住失败就是永久放弃这份依据。"""
        reg = await self._always_slow()
        now = [0.0]
        svc = ProfileService(reg, timeout_s=0.01, ttl_s=300, clock=lambda: now[0])

        await svc.estimate_rows("db", ["a"])
        now[0] = 31.0
        await svc.estimate_rows("db", ["a"])
        assert reg.calls == 2

    async def test_a_stale_profile_is_still_served_during_the_window(self):
        """窗口内返回**旧值**而不是空 —— 有过一次真实观测就别丢掉它。"""
        reg = _FakeRegistry([_table("a", 100)])
        now = [0.0]
        svc = ProfileService(reg, timeout_s=0.01, ttl_s=300, clock=lambda: now[0])
        assert await svc.estimate_rows("db", ["a"]) == 100

        reg._explode = True
        now[0] = 301.0  # 正缓存过期 → 抓取失败 → 进负缓存
        assert await svc.estimate_rows("db", ["a"]) == 100
        now[0] = 310.0  # 负缓存窗口内
        assert await svc.estimate_rows("db", ["a"]) == 100
        assert reg.calls == 2
