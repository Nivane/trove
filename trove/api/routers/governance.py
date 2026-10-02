"""治理中心(设计稿 P5 §4):三个只读端点 —— 六类待办条目级聚合 / 建模覆盖 / 血缘包装。

设计稿:``~/Downloads/trove-page-p5-governance.html`` §4.1(冻结契约)+ §4.3(权限)。

三条纪律(与 ``overview.py`` 同一份,不另立):

1. **0 与 null 是两条信息。** 某类取不到 → 该类计数 ``null`` + ``degraded[]``
   条目,绝不落成 0(把"没数到"洗成"干净"是本页最严重的实现错误);拿得到
   但为空才是 0。
2. **降级是一等返回。** 每条腿独立超时(照抄 ``overview._leg``:
   ``_SOURCE_TIMEOUT_S`` + 只报异常类型名),失败只进 ``degraded[]``,
   绝不整页 500。
3. **同源同值(验收 R2)。** 六类待办的枚举 + 逐源扇出只有**一份实现**
   —— :func:`trove.api.routers.overview.collect_todo_sources`;
   ``/v1/admin/todos`` 与 ``/v1/admin/overview`` 的 ``todos[]`` 都吃它,
   只在投影粒度上不同(条目列表 vs 计数 + 前 3 样例)。各写一遍必然漂移。

权限(§4.3):``/admin/todos`` 与 ``/admin/coverage`` 是 ``require_admin``
自持门禁(含受限 token 的 admin scope 二次校验 —— deps.py:120-134,不在这层
重写角色判断);``/lineage/tables/{name}`` 是用户面,与 ``/v1/catalog/*``
同档(``get_current_user`` + ``require_datasource``,deps.py:156-190)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import get_current_user, require_admin, require_datasource
from trove.api.routers import overview
from trove.services.lineage.service import LineageService
from trove.services.semantic_layer.manage import SemanticManager

router = APIRouter()          # admin 面:/admin/todos · /admin/coverage
lineage_router = APIRouter()  # 用户面:/lineage/tables/{name}

#: 条目级端点的漂移扫描帽 —— 比 overview 的 500 大得多:条目端点要给**精确**
#: 计数(§4.1① 的 counts 是全集的计数,不是样例数);到顶仍诚实降级
#: (计数按扫到的算 + degraded 条目),绝不冒充精确。
_TODO_SCAN_CAP = 2000
#: ``limit`` 默认/上限(§4.1①:默认 50,≤200)。越界**钳制**而不是 422 ——
#: 与 ``/v1/admin/semantic/{name}/history`` 同一手法(semantic.py:203-207)。
_LIMIT_DEFAULT = 50
_LIMIT_MAX = 200
#: 排序键(§3.2 的收件箱 sort);未知值 → 400。
_SORTS = ("oldest", "newest", "confidence", "severity")
#: 严重度排序秩(漂移的 severity 词表,services/drift/models.py)。
_SEVERITY_RANK = {"critical": 0, "error": 0, "warning": 1, "info": 2}
#: 覆盖体检的默认窗口(设计稿未写默认;30d 让「被问了但没建模」不被 24h
#: 静默截断,可信区间仍受 _parse_window 的 1h..90d 约束)。
_COVERAGE_WINDOW_DEFAULT = "30d"

#: 逐数据源取数的四类(drift 走 _drift_facts,其余三类走共享扇出)。
_ENUM_KINDS = ("kb_lesson", "kb_example", "semantic_draft", "drift")


# ── 通用小件 ─────────────────────────────────────────────


def _items_for_kind(collected: dict, kind: str) -> list[dict]:
    """取某类待办的条目(来自共享扇出的 ``entries``;降级腿照实缺席)。"""
    if kind == "drift":
        legs = collected["drift"]
    elif kind in collected["per_source"]:
        legs = collected["per_source"][kind]
    else:
        legs = [collected["global"].get(kind)]
    out: list[dict] = []
    for leg in legs:
        if leg is None:
            continue
        out.extend(leg.get("entries") or [])
    return out


def _fetchable(
    collected: dict, kind: str, names: list[tuple[str, str]] | None, ds_filter: str,
) -> bool:
    """该类当前是否「数得出来」。全腿降级 / 枚举失败 → False(计数给 null)。

    ``ds_filter`` 非空时只看该源那条腿 —— 别的源腿失败不影响本源的确定性
    (反过来也成立:本源腿失败不能靠别源的成对来圆场)。
    """
    if kind in _ENUM_KINDS:
        if collected["enumeration_failed"]:
            return False
        legs = collected["drift"] if kind == "drift" else collected["per_source"][kind]
        if ds_filter:
            pairs = [
                (name, leg) for (name, _), leg in zip(names or [], legs)
                if name == ds_filter
            ]
        else:
            pairs = list(zip([n for n, _ in (names or [])], legs))
        if not pairs:
            return True  # 没有匹配的源 = 真空,不是降级
        return any(leg is not None for _, leg in pairs)
    return collected["global"].get(kind) is not None


def _todo(
    kind: str, *, item_id: str, ds: str | None, title: str, summary: str,
    href: str, source: str | None = None,
    severity: str | None = None, confidence: float | None = None,
    created_at: str | None = None, diff: dict | None = None,
    confirm: bool = True, reject: bool = True, batch: bool = True,
    edit_url: str | None = None,
) -> dict:
    """TodoItem(§4.1① 的字段名逐字)。可选项缺失 = null(形状固定)。"""
    return {
        "kind": kind,
        "id": item_id,
        "ds": ds,
        "title": title,
        "summary": summary,
        "severity": severity,
        "confidence": confidence,
        "created_at": created_at,
        "href": href,
        "actionable": {
            "confirm": confirm,
            "reject": reject,
            "batch": batch,
            "edit_url": edit_url,
        },
        "diff": diff,
        "source": source,
    }


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


# ── TodoItem 构造(逐类) ────────────────────────────────


def _kb_lesson_item(e: dict) -> dict:
    pattern = str(e.get("pattern") or e.get("question") or "")
    note = str(e.get("note") or "")
    sql_snippet = str(e.get("sql_snippet") or "")
    rows = [{"f": "note", "before": "", "after": note, "changed": True}]
    if sql_snippet:
        rows.append({"f": "sql_snippet", "before": "", "after": sql_snippet, "changed": True})
    return _todo(
        "kb_lesson", item_id=pattern, ds=e.get("ds"), title=pattern or "(无 pattern)",
        summary=note or sql_snippet, href=overview._TODO_HREFS["kb_lesson"],
        source=_str_or_none(e.get("source")), confidence=_num(e.get("confidence")),
        created_at=_str_or_none(e.get("created_at")),
        # 待审教训是**新增**条目(没有版本可言),before 如实为 null;
        # 语义草稿那种「改口径」的 diff 在 semantic 侧由服务端算。
        diff={"before": None, "after": note or sql_snippet, "fields": rows},
        edit_url="/admin/kb?tab=lessons",
    )


def _kb_example_item(e: dict) -> dict:
    question = str(e.get("question") or "")
    sql = str(e.get("sql") or "")
    return _todo(
        "kb_example", item_id=question, ds=e.get("ds"), title=question,
        summary=sql, href=overview._TODO_HREFS["kb_example"],
        source=_str_or_none(e.get("source")),
        created_at=_str_or_none(e.get("created_at")),
        edit_url="/admin/kb?tab=examples",
    )


def _semantic_summary(e: dict) -> str:
    note = str(e.get("note") or "").strip()
    if note:
        return note
    diff = e.get("diff") or {}
    changed = [r for r in (diff.get("fields") or []) if r.get("changed")]
    if changed:
        r = changed[0]
        return f"{r.get('f', '')}: {r.get('before', '')} → {r.get('after', '')}"
    err = str(diff.get("error") or "").strip()
    if err:
        return f"diff 不可得:{err}"
    return f"{e.get('action', '')} {e.get('kind', '')}".strip()


def _semantic_draft_item(e: dict) -> dict:
    name = str(e.get("name") or e.get("id") or "")
    diff = e.get("diff") or None
    return _todo(
        "semantic_draft", item_id=str(e.get("id") or name), ds=e.get("ds"), title=name,
        summary=_semantic_summary(e), href=overview._TODO_HREFS["semantic_draft"],
        source=_str_or_none(e.get("kind")),
        created_at=_str_or_none(e.get("created_at")),
        # diff 是服务端算的 (manage.py:_draft_diff,带 carryover 语义),原样透传
        # —— 契约要的 before/after/fields 都在;error 是「干跑失败」的唯一表达,
        # 裁掉它前端就分不清「没变化」与「算不出来」。
        diff=diff, edit_url="/admin/semantic?pending=1",
    )


def _skill_draft_item(e: dict) -> dict:
    name = str(e.get("name") or "")
    return _todo(
        "skill_draft", item_id=name, ds=e.get("ds"), title=name,
        summary=str(e.get("description") or ""),
        href=overview._TODO_HREFS["skill_draft"],
        source=_str_or_none(e.get("source")),
        created_at=_str_or_none(e.get("created_at")),
        edit_url="/admin/skills",
    )


def _memory_preference_item(e: dict) -> dict:
    return _todo(
        "memory_preference", item_id=str(e.get("id") or ""), ds=e.get("ds"),
        title=str(e.get("fact") or ""), summary=str(e.get("evidence") or ""),
        href=overview._TODO_HREFS["memory_preference"],
        source=_str_or_none(e.get("source")), confidence=_num(e.get("confidence")),
        created_at=_str_or_none(e.get("created_at")),
        # 记忆偏好草稿没有编辑器:就地确认/驳回(admin.py:1223/1247)。
        edit_url=None,
    )


def _drift_item(e: dict) -> dict:
    return _todo(
        "drift", item_id=str(e.get("id") or ""), ds=e.get("ds"),
        title=str(e.get("subject") or ""),
        summary=f"{e.get('level', '')} · {e.get('kind', '')}".strip(" ·"),
        href=overview._TODO_HREFS["drift"],
        source=_str_or_none(e.get("source")), severity=_str_or_none(e.get("severity")),
        created_at=_str_or_none(e.get("first_seen_at")),
        # 裁定要看 live schema 对比,收件箱只给入口(去 Tab3)。
        confirm=False, reject=False, batch=False, edit_url=None,
    )


_ITEM_BUILDERS: dict[str, Callable[[dict], dict]] = {
    "kb_lesson": _kb_lesson_item,
    "kb_example": _kb_example_item,
    "semantic_draft": _semantic_draft_item,
    "skill_draft": _skill_draft_item,
    "memory_preference": _memory_preference_item,
    "drift": _drift_item,
}


# ── 过滤 / 排序 / 分页 ──────────────────────────────────


def _matches(it: dict, *, ds_filter: str, query: str) -> bool:
    if ds_filter and it.get("ds") != ds_filter:
        return False
    if query:
        needle = query.lower()
        hay = " ".join(str(it.get(k) or "") for k in ("title", "summary", "ds"))
        if needle not in hay.lower():
            return False
    return True


def _sort_items(items: list[dict], sort: str) -> list[dict]:
    """稳定排序;**缺失值恒在最后**(没时间的条目排到"最新"第一位 = 造假)。

    ``tail`` 是确定性打散键,同值条目跨请求顺序一致(分页不抖动)。
    """
    def tail(it: dict) -> tuple[str, str]:
        return (str(it.get("kind") or ""), str(it.get("id") or ""))

    if sort == "oldest":
        present = [i for i in items if i.get("created_at")]
        missing = [i for i in items if not i.get("created_at")]
        present.sort(key=lambda i: (i["created_at"], tail(i)))
        missing.sort(key=tail)
        return present + missing
    if sort == "newest":
        present = [i for i in items if i.get("created_at")]
        missing = [i for i in items if not i.get("created_at")]
        present.sort(key=lambda i: (i["created_at"], tail(i)), reverse=True)
        missing.sort(key=tail)
        return present + missing
    if sort == "confidence":
        present = [i for i in items if i.get("confidence") is not None]
        missing = [i for i in items if i.get("confidence") is None]
        present.sort(key=lambda i: (-float(i["confidence"]), tail(i)))
        missing.sort(key=tail)
        return present + missing
    # severity:critical > warning > info;未知词表/缺失恒最后
    present = [i for i in items if str(i.get("severity") or "").lower() in _SEVERITY_RANK]
    missing = [i for i in items if str(i.get("severity") or "").lower() not in _SEVERITY_RANK]
    present.sort(key=lambda i: (_SEVERITY_RANK[str(i["severity"]).lower()], tail(i)))
    missing.sort(key=tail)
    return present + missing


def _degraded_entries(degraded: list[dict]) -> list[dict]:
    """把 overview 纪律的 ``{block, source, error, at}`` 翻译成治理载荷形状。

    - ``todos:<kind>[:<ds>]`` → ``{kind, ds}``(全局类是 ``ds: null``);
    - ``datasources:drift:<ds>`` → ``{kind: "drift", ds}``;
    - ``datasources:registry``(源都列不出来)→ 受影响的逐源四类各一条
      (ds: null)—— 不知道是哪个源,但知道哪几类数不出来。
    其它块的降级不属本端点职责,不透传。
    """
    out: list[dict] = []
    for d in degraded:
        block = str(d.get("block") or "")
        src = str(d.get("source") or "")
        base = {"error": d.get("error"), "at": d.get("at")}
        if block == "todos":
            kind, _, ds = src.partition(":")
            out.append({"kind": kind, "ds": ds or None, **base})
        elif block == "datasources" and src.startswith("drift:"):
            out.append({"kind": "drift", "ds": src.split(":", 1)[1] or None, **base})
        elif block == "datasources" and src == "registry":
            for kind in _ENUM_KINDS:
                out.append({"kind": kind, "ds": None, **base})
    return out


# ── GET /v1/admin/todos ─────────────────────────────────


@router.get("/admin/todos")
async def admin_todos(
    request: Request,
    kind: str = Query(default="", description="comma-separated kinds; empty = all six"),
    ds: str = Query(default="", description="datasource filter; empty = all sources"),
    q: str = Query(default="", description="case-insensitive substring over title/summary/ds"),
    sort: str = Query(default="oldest", description="oldest|newest|confidence|severity"),
    admin: dict = Depends(require_admin),
) -> dict:
    """六类审批待办的条目级聚合(§4.1①)。counts 恒含六类(筛选片要靠它)。"""
    wanted = [k.strip() for k in (kind or "").split(",") if k.strip()]
    if not wanted:
        wanted = list(overview.APPROVAL_TODO_KINDS)
    unknown = [k for k in wanted if k not in overview.APPROVAL_TODO_KINDS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind(s): {', '.join(unknown)} "
                   f"(known: {', '.join(overview.APPROVAL_TODO_KINDS)})",
        )
    if sort not in _SORTS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown sort: {sort!r} (known: {', '.join(_SORTS)})",
        )
    raw_limit = request.query_params.get("limit", str(_LIMIT_DEFAULT))
    try:
        limit = max(1, min(_LIMIT_MAX, int(raw_limit)))
    except (TypeError, ValueError):
        limit = _LIMIT_DEFAULT
    raw_offset = request.query_params.get("offset", "0")
    try:
        offset = max(0, int(raw_offset))
    except (TypeError, ValueError):
        offset = 0

    generated_at = overview._now_iso()
    degraded: list[dict] = []
    names = await overview._leg(
        "datasources", "registry", degraded,
        lambda: overview._datasource_names(request),
    )
    drift = (
        {} if names is None
        else await overview._drift_facts(
            request, names, degraded, limit=_TODO_SCAN_CAP,
        )
    )
    # 唯一的取数实现(与 overview.todos[] 同源);条目级投影要 diff。
    collected = await overview.collect_todo_sources(
        request, names, drift, degraded, with_diff=True,
    )

    all_items: list[dict] = []
    for k in overview.APPROVAL_TODO_KINDS:
        builder = _ITEM_BUILDERS[k]
        for e in _items_for_kind(collected, k):
            item = builder(e)
            if _matches(item, ds_filter=ds, query=q):
                all_items.append(item)
    # counts 忽略 kind 筛选(筛选片要一直能看到六类的数);ds/q 筛选生效。
    counts: dict[str, int | None] = {}
    for k in overview.APPROVAL_TODO_KINDS:
        counts[k] = (
            len([i for i in all_items if i["kind"] == k])
            if _fetchable(collected, k, names, ds) else None
        )
    filtered = _sort_items([i for i in all_items if i["kind"] in wanted], sort)
    total: int | None = (
        sum(int(counts[k]) for k in wanted)
        if all(counts[k] is not None for k in wanted) else None
    )
    page = filtered[offset: offset + limit]

    degraded_entries = _degraded_entries(degraded)
    # 漂移计数到扫描帽 = 只是下界(与 overview 的 count_exact: false / note:
    # "capped" 同一件事)。契约没有 count_exact 字段,下界信号只能走
    # degraded[] —— 宁可多报一条降级,也不让「≥2000」被读成精确值。
    for (name, _status), leg in zip(names or [], collected["drift"]):
        if leg is not None and leg.get("exact") is False:
            degraded_entries.append({
                "kind": "drift", "ds": name, "error": "capped", "at": generated_at,
            })

    return {
        "items": page,
        "total": total,
        "counts": counts,
        "generated_at": generated_at,
        "degraded": degraded_entries,
    }


# ── GET /v1/admin/coverage ──────────────────────────────


def _table_key(name: str) -> str:
    """表名归一键:大小写不敏感 + 容忍 ``schema.table`` 限定(两侧同归一)。"""
    return str(name or "").strip().lower().split(".")[-1]


async def _leg_ds(
    name: str, degraded: list[dict],
    fn: Callable[[], Awaitable[Any]], *, timeout: float = overview._SOURCE_TIMEOUT_S,
) -> Any:
    """逐源扇出腿(照抄 ``overview._leg`` 的纪律,degraded 形状换成 ``{ds, error, at}``)。

    独立超时;失败 → 一条 degraded 并返回 None。``CancelledError`` 是
    BaseException,不在此列(调用方被取消时继续上抛)。
    """
    try:
        return await asyncio.wait_for(fn(), timeout=timeout)
    except Exception as e:
        degraded.append({
            "ds": name, "error": overview._err_name(e), "at": overview._now_iso(),
        })
        return None


async def _semantic_facts(request: Request, name: str) -> dict:
    """语义模型事实(纯本地文件读,零 LLM):数据集 / 指标 / 已声明表。

    模型文件存在但解析失败 → 抛错(→ degraded + 相关字段 null):「有文件但
    读不懂」与「没有模型」是两条信息,后者才是结构事实(declared_tables [])。
    """
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        raise RuntimeError("kb service not assembled")
    manager = SemanticManager(kb)
    if not manager.enabled(name):
        return {"enabled": False, "datasets": 0, "metrics": 0, "declared_tables": []}
    model = manager.model(name)
    if model is None:
        raise ValueError("semantics.yml unparseable")
    return {
        "enabled": True,
        "datasets": len(model.datasets),
        "metrics": len(model.metrics),
        # 声明表 = 数据集的物理锚定,``source or name`` 与编译器的解析一致
        # (compiler.py:1250:dataset 没显式 source 时表名就是数据集名);
        # 只看 source 会把「声明了 students 却没写 source」误报成未覆盖。
        "declared_tables": sorted(
            {str(d.source or d.name) for d in model.datasets if (d.source or d.name)}
        ),
    }


async def _physical_facts(request: Request, name: str) -> dict:
    """物理 schema 事实:表集合来自既有 catalog 面(§4.3 附录 B-5)。"""
    catalog = getattr(request.app.state, "catalog_service", None)
    if catalog is None:
        raise RuntimeError("catalog service not assembled")
    tables = await catalog.list_tables(name)
    return {"names": [str(t.get("name") or "") for t in tables], "count": len(tables)}


async def _asked_facts(request: Request, name: str, cutoff: str) -> list[dict]:
    """查询历史里真实被问过的表(§4.1②:lineage 查询历史 − 已声明表)。"""
    return await _lineage(request).asked_tables(name, since=cutoff)


async def _refused_facts(request: Request, name: str) -> dict:
    """未采纳 KB 资产(文件级;reason 原文,来自 KB 的 refused 记账)。"""
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        raise RuntimeError("kb service not assembled")
    await kb.ensure_synced(name)
    assets = dict(getattr(kb, "refused_assets", None) or {})
    files = [
        {"file": rel, "reason": str(reason)}
        for rel, reason in sorted(assets.items())
        if rel.startswith(f"{name}/")
    ]
    return {"count": len(files), "files": files}


async def _coverage_source(
    request: Request, name: str, degraded: list[dict], cutoff: str,
) -> dict:
    """一个源的覆盖体检行。四条腿各自独立降级,绝不互相拖累。"""
    model = await _leg_ds(name, degraded, lambda: _semantic_facts(request, name))
    physical = await _leg_ds(name, degraded, lambda: _physical_facts(request, name))
    asked = await _leg_ds(name, degraded, lambda: _asked_facts(request, name, cutoff))
    refused = await _leg_ds(name, degraded, lambda: _refused_facts(request, name))

    declared = (model or {}).get("declared_tables")
    declared_keys = {_table_key(t) for t in (declared or [])}
    # 集合差要两边在场:模型读不到 或 物理 schema 读不到 → 差不可得(null),
    # 绝不把「少了一边」算成「全都没建模」。
    uncovered: list[str] | None = None
    if physical is not None and model is not None:
        uncovered = sorted(
            t for t in physical["names"] if _table_key(t) not in declared_keys
        )
    if asked is not None and model is not None:
        asked_unmodeled: list[dict] | None = [
            r for r in asked if _table_key(str(r.get("table"))) not in declared_keys
        ]
    else:
        asked_unmodeled = None
    return {
        "ds": name,
        "model": (
            None if model is None
            else {
                "enabled": bool(model.get("enabled")),
                "datasets": model.get("datasets"),
                "metrics": model.get("metrics"),
                # 没有语义模型 → [](结构事实,不是「覆盖率 0%」);解析失败 → null。
                "declared_tables": declared if model.get("enabled") else [],
            }
        ),
        "physical": (
            None if physical is None
            else {"tables": physical["count"], "source": "catalog"}
        ),
        "uncovered_tables": uncovered,
        "asked_unmodeled": asked_unmodeled,
        "refused": refused,
    }


def _coverage_degraded(degraded: list[dict]) -> list[dict]:
    """归一覆盖端的 degraded 形状为 ``{ds, error, at}``。

    ``_leg_ds`` 的条目本来就是这个形状;唯一要翻译的是 registry 列举失败
    (``overview._leg`` 写的 ``{block: datasources, source: registry}``)→
    ``ds: null``(不知道是哪个源 —— 全都没体检成)。
    """
    out: list[dict] = []
    for d in degraded:
        if "ds" in d:
            out.append({"ds": d.get("ds"), "error": d.get("error"), "at": d.get("at")})
        else:
            out.append({"ds": None, "error": d.get("error"), "at": d.get("at")})
    return out


@router.get("/admin/coverage")
async def admin_coverage(
    request: Request,
    ds: str = Query(default="", description="datasource filter; empty = all registered"),
    window: str = Query(default=_COVERAGE_WINDOW_DEFAULT, description="1h .. 90d"),
    admin: dict = Depends(require_admin),
) -> dict:
    """建模覆盖 + 被问未建模(§4.1②)。零 LLM、纯集合差。"""
    try:
        delta = overview._parse_window(window)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    generated_at = overview._now_iso()
    cutoff = (datetime.now(timezone.utc) - delta).isoformat()
    degraded: list[dict] = []

    names = await overview._leg(
        "datasources", "registry", degraded,
        lambda: overview._datasource_names(request),
    )
    if names is None:
        # 源都列不出来 → 不知道体检谁;null 而不是 [](0 与 null 是两条信息)。
        return {
            "generated_at": generated_at, "window": window,
            "sources": None, "degraded": _coverage_degraded(degraded),
        }
    if ds:
        if not any(n == ds for n, _ in names):
            raise HTTPException(status_code=404, detail=f"datasource not found: {ds}")
        names = [(n, s) for n, s in names if n == ds]

    sources = [
        await _coverage_source(request, name, degraded, cutoff)
        for name, _ in names
    ]
    return {
        "generated_at": generated_at,
        "window": window,
        "sources": sources,
        "degraded": _coverage_degraded(degraded),
    }


# ── GET /v1/lineage/tables/{name} ───────────────────────


def _lineage(request: Request) -> LineageService:
    svc = getattr(request.app.state, "lineage", None)
    if svc is None:
        # 生产装配有 app.state.lineage(main.py);测试/嵌入路径按项目根兜底。
        svc = LineageService(overview._project_root(request))
        request.app.state.lineage = svc
    return svc


@lineage_router.get("/lineage/tables/{name}")
async def lineage_table(
    name: str,
    request: Request,
    datasource: str | None = None,
    column: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """血缘只读包装(§4.1③):薄包装 LineageService 的既有读方法,零新逻辑。

    冷启动(表从未被查过)是真实空态:``query_log.count: 0``、``definitions: []``
    —— 没记录 ≠ 没依赖,界面文案按「还没有查询历史」写。
    """
    ds = await require_datasource(request, datasource, user)
    lineage = _lineage(request)
    upstream = await lineage.table_upstream(ds, name)
    downstream = await lineage.table_downstream(ds, name)
    known = await lineage.known_tables(ds)
    # 名字按血缘库里的既有拼写归一(大小写不敏感);库里没有就原样回显。
    canonical = next((t for t in known if t.lower() == name.lower()), name)

    definitions: list[dict] = []
    seen_sql: set[str] = set()
    for d in [*upstream, *downstream]:
        kind = str(d.get("kind") or "")
        sql = str(d.get("sql") or "")
        if kind in ("create_view", "create_table_as") and sql and sql not in seen_sql:
            seen_sql.add(sql)
            definitions.append({
                "kind": "view" if kind == "create_view" else "ctas",
                "sql": sql,
            })

    cols = await lineage.table_columns(ds, canonical)
    if column:
        wanted = column.lower()
        cols = [c for c in cols if c.lower() == wanted] or [column]
    columns: list[dict] = []
    for c in cols:
        lin = await lineage.column_lineage(ds, canonical, c)
        columns.append({
            "column": c,
            "upstream": lin.get("producers", []),
            "downstream": lin.get("consumers", []),
        })

    asked = await lineage.asked_tables(ds)
    hit = next((t for t in asked if str(t.get("table", "")).lower() == canonical.lower()), None)
    return {
        "table": canonical,
        "datasource": ds,
        "upstream": upstream,
        "downstream": downstream,
        "columns": columns,
        "definitions": definitions,
        "query_log": {
            "count": int(hit["queries"]) if hit else 0,
            "last_at": (hit or {}).get("last_asked_at") or None,
        },
    }
