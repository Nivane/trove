"""``DriftService`` —— 检测、查询、处置漂移的唯一服务入口。

**它补的是「两条断头路」**:在此之前,物理层漂移只有生命周期巡检在调
(``MemoryService.detect_schema_drift``),语义层漂移只有 ``check_drift.py``
在调,两者在服务层从不相遇;而且都止步于「报出一组名字」—— 报完即结束,
没有持久化,于是下次跑还是同一批,分不出「新漂移」与「已知未处理」。

**schema 获取是注入的**。服务不负责连数据库:它需要的是「这个数据源现在的
表→列集合」。把这个做成 ``schema_provider`` 回调,好处是服务本身不依赖连接器
注册表,单测不需要起库;真正的接线在 API / CLI 层,那里本来就是 I/O 的地盘。

**``levels`` 过滤的是「跑哪几级」,不是「返回哪几级」**。差别在 I3 上:若跑完
所有级别再过滤,一次只请求 L1 的调用会拿到一份 status=ok 的报告,而 L2 其实
没查 —— 又是一次「未知被洗成通过」。所以报告带 ``levels_checked``,让调用方
能分辨「查过且干净」与「没查」。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from trove.core.logging import get_logger
from trove.services.drift import adapters
from trove.services.drift.models import (
    L1,
    L2,
    LEVELS,
    RUN_SKIPPED,
    SEVERITY_ORDER,
    STATUS_ACKNOWLEDGED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    STATUS_WAIVED,
    DriftReport,
    DriftRow,
    ImpactSet,
    normalize_subject,
)
from trove.services.drift.store import DriftStore

logger = get_logger(__name__)

#: 检测器目前实现的级别。L3(取值可锚定)需要活库探测、L4(口径)只承接
#: 外部声明 —— 两者都还没接。写在这里而不是靠 ``if`` 散落各处:
#: 请求一个未实现的级别应当**明确报错**,而不是安静地返回空。
SUPPORTED_LEVELS = (L1, L2)

#: 哪些状态可以再被 resolve / waive。已 resolved 的重复 resolve → 409。
RESOLVABLE_FROM = (STATUS_OPEN, STATUS_ACKNOWLEDGED)


class DriftError(Exception):
    """服务层错误的基类。"""


class UnknownLevel(DriftError):
    """请求了未实现/非法的级别。"""


class DriftNotFound(DriftError):
    """条目不存在。"""


class IllegalTransition(DriftError):
    """状态不允许该操作(如对已 resolved 的条目再 resolve)。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExternalDrift:
    """外部声明的漂移 —— L4「口径变了」没有检测器,只有人的告知。

    这不是权宜之计,是分类学的一部分:上游改了聚合逻辑、换了口径,物理 schema
    可以完全不变,任何确定性比对都看不见。系统能做的只有**承接并留痕**。
    """

    datasource: str
    subject: str
    kind: str = "semantics_changed"
    detail: dict[str, Any] | None = None
    severity: str = "warning"
    author: str = ""


class DriftService:
    """检测 / 查询 / 处置漂移。

    Args:
        project_root: 仓库根,store 落在 ``<root>/.trove/drift/``。
        kb: ``KbService``;给了才能跑 L1(schema_notes vs 活库)。
        schema_provider: ``async (datasource) -> {table: {columns}}``。
            给了才能跑 L1、L2 —— 没有活库快照就无从比对。
        semantic_factory: ``async (datasource, catalog) -> provider | None``,
            返回带 ``drift()`` 的语义层 provider(通常
            ``SemanticLayerProvider``)。返回 None 表示该数据源没有语义层
            —— 那是**不适用**,不是查失败。
            ``catalog`` 是**本次已取到的活库快照**,不是让工厂自己去取:
            L1 与 L2 必须判同一个世界。两次取之间库变了,一份报告里就会
            同时含两个时刻的结论,而这种错乱看不出来。
        store: 注入用(测试);默认自建。
    """

    def __init__(
        self,
        project_root: str | Path,
        *,
        kb: Any = None,
        schema_provider: Callable[[str], Awaitable[dict[str, set[str]]]] | None = None,
        semantic_factory: (
            Callable[[str, dict[str, set[str]]], Awaitable[Any]] | None) = None,
        store: DriftStore | None = None,
    ) -> None:
        self.root = Path(project_root)
        self.kb = kb
        self._schema_provider = schema_provider
        self._semantic_factory = semantic_factory
        self.store = store if store is not None else DriftStore(self.root)

    async def dispose(self) -> None:
        await self.store.dispose()

    # ── 检测 ─────────────────────────────────────────────

    async def detect(
        self,
        datasource: str,
        *,
        levels: set[str] | None = None,
        with_impact: bool = True,
    ) -> DriftReport:
        """跑检测、合流、落库,返回报告。

        ``with_impact`` 目前恒为无影响面(P2 的 ``ImpactResolver`` 接进来后
        才会算)。保留这个开关是为了让「这次不算影响面」在调用侧仍然显式 ——
        未来它意味着省掉一次 KB 反查。
        """
        wanted = self._resolve_levels(levels)

        schema: dict[str, set[str]] | None = None
        if self._schema_provider is not None:
            try:
                schema = await self._schema_provider(datasource)
            except Exception as exc:  # noqa: BLE001
                # 拿不到活库 schema = 两条路都跑不成。**不能**当成「无漂移」:
                # 这正是 I3 要挡的那种静默。降级为 skipped 并说明原因。
                logger.warning(
                    "drift: schema unavailable for %s: %s", datasource, exc)
                schema = None

        schema_report = await self._run_l1(datasource, schema, wanted)
        semantic_report = await self._run_l2(datasource, schema, wanted)

        # ``levels_verified`` 由 collect() 自行推导,这里**不覆写**:它只知道
        # 「想查哪几级」,collect() 才知道「哪几级真跑出了结论」。用 wanted 覆写
        # 会让「没有语义层的库」被标成 L2 已验证 —— 又是一次未知洗成通过。
        report = adapters.collect(schema_report, semantic_report, datasource)

        new_count = await self.store.record(report)
        return replace(report, new_count=new_count)

    def _resolve_levels(self, levels: set[str] | None) -> tuple[str, ...]:
        if levels is None:
            return tuple(SUPPORTED_LEVELS)
        unknown = {lv for lv in levels if lv not in LEVELS}
        if unknown:
            raise UnknownLevel(f"未知级别: {sorted(unknown)}")
        unsupported = {lv for lv in levels if lv not in SUPPORTED_LEVELS}
        if unsupported:
            # 明确报错而非返回空:请求 L3 拿到一份「干净」报告,是最糟的结果。
            raise UnknownLevel(
                f"级别尚未实现: {sorted(unsupported)}(当前支持 "
                f"{list(SUPPORTED_LEVELS)})")
        return tuple(lv for lv in LEVELS if lv in levels)

    async def _run_l1(self, datasource: str, schema: dict[str, set[str]] | None,
                      wanted: tuple[str, ...]) -> dict[str, Any] | None:
        if L1 not in wanted:
            return None
        if self.kb is None or schema is None:
            return {"datasource": datasource, "new_tables": [], "gone_tables": [],
                    "column_changes": {}, "status": RUN_SKIPPED,
                    "skip_reason": ("kb_missing" if self.kb is None
                                    else "catalog_unreachable")}
        from trove.services.memory.schema_drift import detect_drift

        return await detect_drift(datasource, self.kb, _DictCatalog(schema))

    async def _run_l2(self, datasource: str, schema: dict[str, set[str]] | None,
                      wanted: tuple[str, ...]) -> dict[str, Any] | None:
        if L2 not in wanted:
            return None
        if self._semantic_factory is None or schema is None:
            return {"stale": False, "gone_tables": [], "missing_fields": {},
                    "missing_keys": {}, "relationship_breaks": [],
                    "status": RUN_SKIPPED,
                    "skip_reason": ("no_semantic_model"
                                    if self._semantic_factory is None
                                    else "no_catalog")}
        provider = await self._semantic_factory(datasource, schema)
        if provider is None:
            # 该数据源没有语义层 —— **不适用**,不是查失败。返回 None 让
            # collect() 只看 L1,而不是把它算成 skipped。
            return None
        return provider.drift()

    # ── 查询 ─────────────────────────────────────────────

    async def list(self, datasource: str, *, status: str | None = None,
                   level: str | None = None, limit: int = 100) -> list[DriftRow]:
        return await self.store.list_items(
            datasource, status=status, level=level, limit=limit)

    async def get(self, drift_id: int) -> DriftRow:
        row = await self.store.get_item(drift_id)
        if row is None:
            raise DriftNotFound(f"drift {drift_id} 不存在")
        return row

    async def impact(self, drift_id: int) -> ImpactSet:
        """受影响面 —— 读的是**发现时冻结的快照**,不是现在重算的结果。

        重算会让复盘问「当初为什么这么判」时得到一个今天的答案。
        """
        return ImpactSet.from_dict((await self.get(drift_id)).affected)

    async def runs(self, datasource: str, limit: int = 20) -> list[dict]:
        return await self.store.list_runs(datasource, limit=limit)

    async def open_subjects(self, datasource: str) -> set[str]:
        return await self.store.open_subjects(datasource)

    # ── 处置 ─────────────────────────────────────────────

    async def resolve(self, drift_id: int, *, by: str, reason: str) -> None:
        await self._transition(drift_id, STATUS_RESOLVED, by=by, reason=reason)

    async def waive(self, drift_id: int, *, by: str, reason: str) -> None:
        await self._transition(drift_id, STATUS_WAIVED, by=by, reason=reason)

    async def _transition(self, drift_id: int, status: str, *,
                          by: str, reason: str) -> None:
        if not (reason or "").strip():
            raise IllegalTransition("处置必须给出 reason —— 无理由的 resolve 等于删记录")
        # 先确认存在,好把「不存在」与「状态不允许」分成 404 与 409 两种回答。
        if await self.store.get_item(drift_id) is None:
            raise DriftNotFound(f"drift {drift_id} 不存在")
        ok = await self.store.set_status(
            drift_id, status, by=by, reason=reason, at=now_iso(),
            allowed_from=RESOLVABLE_FROM)
        if not ok:
            raise IllegalTransition(
                f"drift {drift_id} 当前状态不允许该操作(仅 "
                f"{list(RESOLVABLE_FROM)} 可处置)")

    async def declare_external(self, payload: ExternalDrift) -> DriftRow:
        """承接外部口径漂移声明(L4)。"""
        subject = normalize_subject(payload.subject)
        if not subject:
            raise DriftError("subject 不能为空")
        if payload.severity not in SEVERITY_ORDER:
            raise DriftError(f"未知严重度: {payload.severity}")
        await self.store.declare_external(
            datasource=payload.datasource, level="L4", kind=payload.kind,
            subject=subject, detail=payload.detail or {},
            severity=payload.severity, declared_at=now_iso(),
            author=payload.author,
        )
        matches = await self.store.list_items(
            payload.datasource, level="L4", limit=1000)
        for row in matches:
            if row.subject == subject and row.kind == payload.kind:
                return row
        # 刚写进去却查不到 —— 只可能是并发删除。报出来,不要返回 None 让
        # 调用方以为成功。
        raise DriftError(f"外部声明已写入但未能读回: {subject}")


class _DictCatalog:
    """把已取到的 ``{table: {columns}}`` 包成 ``detect_drift`` 认识的 catalog。

    ``detect_drift`` 只用到 ``column_sets`` —— 包一层是为了让它走「一次性取全
    列名」的快路径,而不是退到 ``list_tables``(那条路列集合可能是 count,
    列漂移判不了)。
    """

    def __init__(self, data: dict[str, set[str]]) -> None:
        self._data = data

    async def column_sets(self, datasource: str) -> dict[str, set[str]]:
        return self._data
