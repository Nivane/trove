"""元数据画像(执行画像设计 §6.1 / §7.1 / §8.2 / §8.4)。

两个用途,复杂度差得很远:

1. **给成本轨供第 2 档依据**(§5.2):表行数之和,用来在 EXPLAIN 拿不到时
   给一个「粗但有」的估算。这一档今天就能对**全部六个方言**生效 —— 因为
   ``adapters/base.get_schema()`` 早就把 ``row_count_estimate`` 填好了,
   画像只是它的**归一入口**,不需要为它新写任何方言 SQL。
2. **给答案供 ``data_as_of``**(§8.4 / I5):数据截止到什么时候。

设计上最要紧的两条方向,贯穿全文件:

**不可得 ≠ 0**(§6.1)。查不到就是 ``None`` 且**不进** ``capabilities``。
用 0 或空串冒充「已知」,下游会把它当成一个真实的观测值 —— 这与能力①的
「空报告 ≠ 无漂移」是同一条原则。特别注意存量有的适配器写
``int(row_count or 0)``:那让「不知道」与「空表」在画像里长得一模一样,
本模块把两者都当**没有依据**(方向安全)。

**I5:未知必须说未知**。``data_as_of`` 不可得时是 ``None`` + ``basis="unknown"``,
**不得用查询时间冒充**。

工程约束(§10):画像要查库,可能慢 → TTL 缓存 + 超时跳过;跳过/故障一律退到
``None`` 而不是抛出去打断主链路(I6 —— 画像是增强,不该拖慢或打断查询)。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from trove.core.logging import get_logger
from trove.core.types import TableProfile, positive_int as _positive

logger = get_logger(__name__)

__all__ = ["DEFAULT_TTL_S", "DatasetFreshness", "ProfileService", "TableProfile"]

#: 画像缓存 TTL(§10:缓存 TTL 5min)。画像描述的是表级统计,分钟级陈旧完全
#: 可接受;而每次执行都重抓一遍全库 schema 是不可接受的。
DEFAULT_TTL_S = 300.0

#: 单次画像抓取的超时(秒)。超时 → 跳过这一档,退到下一档。
DEFAULT_TIMEOUT_S = 2.0

#: ``as_of`` 的两个口径。分区表优先 ``latest_partition``(§8.4 C):
#: 分区表上的 ``last_modified`` 反映的是**元数据变更**(可能是加了个分区),
#: 不代表数据新鲜。
BASIS_LAST_MODIFIED = "last_modified"
BASIS_LATEST_PARTITION = "latest_partition"
BASIS_UNKNOWN = "unknown"


# `TableProfile` 定义在 `trove.core.types`(适配器的返回类型,与 `TableInfo` /
# `Capabilities` 同级),此处 re-export 以保持设计 §7.1 的导入路径。


@dataclass(frozen=True)
class DatasetFreshness:
    """数据源级新鲜度聚合,用于答案的 ``data_as_of``(§6.1)。"""

    datasource: str
    #: 所引用表里**最旧**的那个截止时间 —— 取最新会让答案看起来比它实际
    #: 依据的数据更新
    as_of: str | None = None
    #: "last_modified" | "latest_partition" | "unknown"
    basis: str = BASIS_UNKNOWN
    #: 明显滞后于同源其他表者
    stale_tables: list[str] = field(default_factory=list)


class ProfileService:
    """表级画像与新鲜度(§7.1)。

    Args:
        registry: ``ConnectorRegistry``。画像从它的 ``get_schema`` 取行数,
            不新写方言 SQL(见模块 docstring)。
        profiles: 已算好的 ``TableProfile`` 覆盖(表名 → 画像)。生产由各
            adapter 的 ``table_profile`` 提供;未提供的表落到「只有行数」的
            基础剖面。**这一层是可选的** —— 没有它,第 2 档照样工作。
        ttl_s: 缓存 TTL。
        timeout_s: 单次抓取超时;超时 → ``None``(退下一档)。
        clock: 注入时钟,供测试免 sleep。
    """

    def __init__(
        self,
        registry: Any,
        *,
        profiles: dict[str, TableProfile] | None = None,
        ttl_s: float = DEFAULT_TTL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._profiles = dict(profiles or {})
        self.ttl_s = float(ttl_s)
        self.timeout_s = float(timeout_s)
        self._clock = clock
        # 缓存键必须含数据源:多数据源部署里漏掉它,A 库的行数会被当成 B 库的
        self._cache: dict[str, tuple[float, dict[str, TableProfile]]] = {}

    # ── 成本轨第 2 档 ─────────────────────────────────────

    async def estimate_rows(
        self, datasource: str, tables: Iterable[str] | None,
    ) -> int | None:
        """涉及表的行数之和;一个都认不出来 → ``None``(**不是 0**)。

        认不出单个表时**跳过它**而不是放弃整次估算:SQL 里的 CTE 名、别名、
        schema 前缀都可能对不上真实表名,为其中一个放弃整次是过度反应。

        ``None`` 会让成本轨退到保守预算(§5.2 的降级链),方向安全。
        """
        names = [str(t or "").strip() for t in (tables or [])]
        names = [n for n in names if n]
        if not names:
            return None

        known = await self._table_profiles(datasource)
        total = 0
        hit = False
        for name in names:
            profile = known.get(_normalize(name))
            rows = _positive(profile.row_count) if profile else None
            if rows is not None:
                total += rows
                hit = True
        return total if hit else None

    # ── 表级画像 ──────────────────────────────────────────

    async def table_profile(self, datasource: str, table: str) -> TableProfile:
        """单表画像。表不认识 → 各字段 ``None``、``capabilities`` 为空。

        **不返回 ``None``**:「这张表查不到」与「查询失败了」对调用方是两回事,
        前者是信息,后者是故障。
        """
        known = await self._table_profiles(datasource)
        found = known.get(_normalize(table))
        if found is not None:
            return found
        return TableProfile(table=table)

    async def capabilities(self, datasource: str) -> frozenset[str]:
        """本数据源的**并集**能力”—— 用于前端按能力隐藏、以及文档化能力矩阵。"""
        caps: set[str] = set()
        for profile in (await self._table_profiles(datasource)).values():
            caps |= profile.capabilities
        return frozenset(caps)

    # ── 新鲜度(I5 / §8.4)────────────────────────────────

    async def freshness(
        self, datasource: str, tables: Iterable[str] | None,
    ) -> DatasetFreshness:
        """所引用表的新鲜度聚合;无从判断时 ``basis="unknown"`` 且 ``as_of=None``。

        规则分两级,§8.4 C 的「按能力择一」落在**表**这一级:

        1. 每张表按它**自己**最可信的口径给出一个截止值(有 ``latest_partition``
           就用它,否则 ``last_modified``);
        2. 数据源级取这些值里**最旧**的那个,``basis`` 如实报出产生它的那张表
           用的是哪个口径。

        第 2 步是**跨口径比大小**,这是有意为之 —— 唯一的替代品是「哪个口径更
        可信就整片用它」,而那会把另一个口径的表**整个丢出聚合**:一张三年没更新
        的维表,因为同库另一张表有分区,就被从截止时间里抹掉了,答案于是看起来
        (比它实际依据的数据)更新。取最旧不会犯这个错:它只会**低估**新鲜度,
        而低估是安全方向 —— 用户以为数据比实际旧,不会因此下错结论。

        报出的 ``basis`` 因此是**产生这个数的那张表的口径**,不是「参与聚合的
        口径的全集」。两者不等时,后者才是谎话(用一个口径的名字去标一个用另一
        个口径算出来的数)。

        假定:分区值与时间戳是同一量纲(可比的日期串)。``latest_partition``
        存在的全部意义就是 ``dt=`` 这类日期分区;若某库拿它装非日期值,这里会
        退化成字符串序 —— 已知边界,不为它引入日期嗅探启发式。
        """
        names = [str(t or "").strip() for t in (tables or [])]
        names = [n for n in names if n]
        # 记**实际量的是哪个数据源**:留空会让调用方拿到一个「datasource: ""」
        # 的记录,而它本来是用来回答「这份数据截止到什么时候」的
        ds = (datasource or "").strip() or _default_name(self._registry)
        if not names:
            return DatasetFreshness(datasource=ds)

        known = await self._table_profiles(datasource)
        found = [known[_normalize(n)] for n in names if _normalize(n) in known]
        if not found:
            return DatasetFreshness(datasource=ds)

        candidates = [c for c in (_table_as_of(p) for p in found) if c is not None]
        if not candidates:
            return DatasetFreshness(datasource=ds)
        value, basis = min(candidates, key=lambda pair: str(pair[0]))
        return DatasetFreshness(datasource=ds, as_of=str(value), basis=basis)

    # ── 内部 ──────────────────────────────────────────────

    async def _table_profiles(self, datasource: str) -> dict[str, TableProfile]:
        """数据源的表画像(带缓存);故障/超时 → 空表(退下一档,不抛)。"""
        key = (datasource or "").strip() or _default_name(self._registry)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self.ttl_s:
            return cached[1]

        try:
            profiles = await asyncio.wait_for(
                self._load(datasource), timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            # §10:画像很慢时跳过 —— 它是增强,不该拖慢主链路
            logger.warning("profile: 抓取超时(>%.1fs),本档跳过", self.timeout_s)
            return cached[1] if cached else {}
        except Exception as e:
            # I6:画像故障退到下一档,不打断查询链路
            logger.warning("profile: 抓取失败(%s),本档跳过", e)
            return cached[1] if cached else {}

        self._cache[key] = (now, profiles)
        return profiles

    async def _load(self, datasource: str) -> dict[str, TableProfile]:
        """表画像的来源链(§8.2 B):适配器 → schema → 显式覆盖。

        1. **适配器自报**``table_profiles()`` —— 逐方言实现的落点。能力矩阵由各
           适配器显式声明,画像层不判断方言(§8.2 否掉统一 SQL 的理由:六个方言
           差异太大)。适配器**报错**就到此为止,不回退(见 ``_adapter_profiles``)。
        2. 拿不到适配器(注册表是鸭子类型的,或数据源还没注册)→ 退到
           ``get_schema`` 的行数,**只报观察到的事实**:这条路没有适配器可以问,
           所以它只能声明自己确实看到了什么。值缺失就是空的 ``capabilities``
           —— 少声明是安全方向(调用方退下一档),多声明才是误诊。
        3. 构造时注入的 ``profiles`` 覆盖在最上层,给 P3 的渲染测试与手工指定用。
        """
        out = await self._adapter_profiles(datasource)
        if out is None:
            out = {}
            schema = await self._registry.get_schema(datasource or None)
            for table in getattr(schema, "tables", []) or []:
                name = str(getattr(table, "name", "") or "").strip()
                if not name:
                    continue
                rows = _positive(getattr(table, "row_count_estimate", None))
                out[_normalize(name)] = TableProfile(
                    table=name,
                    row_count=rows,
                    capabilities=(
                        frozenset({"row_count"}) if rows is not None else frozenset()
                    ),
                )
        for name, profile in self._profiles.items():
            out[_normalize(name)] = profile
        return out

    async def _adapter_profiles(self, datasource: str) -> dict[str, TableProfile] | None:
        """问适配器要画像;拿不到适配器 → ``None``(退 schema);适配器报错 → ``{}``。

        两者返回不同的东西是**有意的**:``None`` 是「没人可问,换条路」,``{}`` 是
        「问了,答不上来」—— 后者不该再去做更贵的 ``get_schema``(画像查询是这条
        链上更便宜的那个,便宜的先失败了,贵的多半也不行,只是把延迟加上去)。
        """
        getter = getattr(self._registry, "get", None)
        if getter is None:
            return None
        try:
            adapter = await _maybe_await(getter(datasource or None))
        except Exception as e:
            logger.debug("profile: 取不到适配器(%s),退到 schema 行数", e)
            return None
        loader = getattr(adapter, "table_profiles", None)
        if loader is None:
            return None
        try:
            profiles = await _maybe_await(loader())
        except Exception as e:
            logger.warning("profile: 适配器画像查询失败(%s),本档无依据", e)
            return {}
        return {
            _normalize(str(name)): profile
            for name, profile in (profiles or {}).items()
            if str(name or "").strip()
        }


async def _maybe_await(value: Any) -> Any:
    """同步/异步依赖都接受 —— 真实适配器是 async,测试替身可能是同步的。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _normalize(name: str) -> str:
    """表名归一:去 schema 前缀 + 小写。

    SQL 里写 ``public.students``,schema 里报 ``students`` —— 不归一会两边
    都认不出来,而表现为「画像说没有依据」,看不出是名字没对上。
    """
    return str(name or "").strip().rsplit(".", 1)[-1].strip().lower()


def _table_as_of(profile: TableProfile) -> tuple[str, str] | None:
    """单表的截止值 + 它是按哪个口径得到的(§8.4 C 落在这里)。

    分区优先于 ``last_modified``:分区表上的 ``last_modified`` 反映的是**元数据
    变更** —— 今天往表里加一个空分区,``last_modified`` 就是今天,而数据可能还是
    上周的。拿它当截止时间就是把答案说新了。
    """
    if profile.latest_partition:
        return str(profile.latest_partition), BASIS_LATEST_PARTITION
    if profile.last_modified:
        return str(profile.last_modified), BASIS_LAST_MODIFIED
    return None


def _default_name(registry: Any) -> str:
    return str(getattr(registry, "default_name", "") or "")
