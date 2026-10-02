"""GET /v1/admin/overview —— 总览 Dashboard 的一次性只读聚合(admin)。

设计稿:``~/Downloads/trove-page-overview.html`` §5(方案 B)。服务端复用既有
service 在进程内扇出,前端一次往返拿到全部块。**不产生新业务逻辑** —— 每个
数字都能追到既有 service,或一条只读 SQL(审计窗口计数 / jobs.runs 窗口计数,
两者都走既有 ``StorageBackend``,占位符 ``?`` 跨 SQLite/Postgres)。

三条纪律(逐条对应设计稿):

1. **``degraded[]`` 是一等返回。** 每条扇出腿有独立超时
   (:data:`_SOURCE_TIMEOUT_S`),超时/异常写成一条 degraded 记录,**绝不让整页
   500**。唯一的整页级失败是内部存储探不通(与 ``/v1/health`` 同一判定):
   那时 503 + 其余块为 null —— 连审计与 KB 都读不到,没有「部分结果」可言。
2. **诚实缺口。** 数据面不存在的不装:成本/缓存指标归 P4(响应里没有这一块);
   ``error_class`` 未落库,失败分布只能对审计 ``details.error`` 里的
   ``[ERR:<id>]`` 文本做粗桶,并标 ``failures_approximate: true``;数不准的
   标 ``count_exact: false``(漂移计数在既有端点的 500 处饱和)。取不到的块是
   ``null`` 而不是 0 —— **0 与 null 是两条不同的信息**。
3. **形状固定。** 字段始终齐全(缺失 = null);来源未装配(jobs/skills/memory
   缺席)归一为 ``available: false, count: 0`` 而不是降级 —— 与记忆偏好 404 的
   归一处理一致(设计稿 §5「命名陷阱」表)。

窗口参数只影响 ``usage`` / ``recent_events`` / ``job_failed``;``health`` 与
数据源状态**与窗口无关**(设计稿 §5 注)。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from trove.api.deps import require_admin
from trove.core.types import BASIS_NOT_PROBED

router = APIRouter()

#: 单个数据源/来源的扇出上限(设计稿 §5:每个源加 ≤1.5s 的独立超时)。
_SOURCE_TIMEOUT_S = 1.5
#: 内部存储 ping 上限(与 ``/v1/health`` 同一值)。
_STORAGE_PING_TIMEOUT_S = 2.0
#: 最近事件条数(设计稿「近 24h · 敏感动作全量」的呈现位)。
_EVENT_LIMIT = 10
#: 窗口内审计取样的封顶;到顶标 ``sample_capped``(不装成完整)。
_USAGE_SAMPLE_MAX = 2000
#: 漂移计数的饱和帽 —— 与既有 ``/admin/drift`` 端点同一值,到顶标
#: ``count_exact: false``(设计稿 §5:服务端逐源调用 + 计数标注)。
_DRIFT_CAP = 500
#: 每类待办最多带回的样例数(设计稿「每类一条计数 + 前 3 个样例」)。
_SAMPLES = 3

_WINDOW_RE = re.compile(r"^(\d{1,3})([hd])$")
_WINDOW_MAX_HOURS = 24 * 90
#: 分类学的打标形式(``classify.py`` 的 ``ErrorClass.tag()`` = ``[ERR:<id>]``)。
#: 审计只存原始 error 文本,粗桶只能从这里来 —— 根治(error_class 落库)归 P4。
#: 字符集放宽到数字:存量行里还有旧的数字 id(``[ERR:DS_001]``),同样归桶。
_ERR_TAG_RE = re.compile(r"\[ERR:([A-Z0-9_]+)\]")

_USAGE_SQL = (
    "SELECT details_json FROM audit_log "
    "WHERE action = ? AND ts >= ? ORDER BY ts DESC, id DESC LIMIT ?"
)
_USAGE_TOTAL_SQL = "SELECT COUNT(*) FROM audit_log WHERE action = ? AND ts >= ?"
_EVENTS_SQL = (
    "SELECT ts, action, username, status FROM audit_log "
    "WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT ?"
)
_RUNS_FAILED_SQL = (
    "SELECT job_id, COUNT(*) AS n FROM runs "
    "WHERE status = ? AND started_at >= ? GROUP BY job_id "
    "ORDER BY MAX(started_at) DESC"
)

#: 待办 8 类来源与其深链(设计稿 §5 的 items;顺序即展示顺序,shape 固定)。
#: ``memory_preference`` 的落点尚无页面(P4),href 如实为 null。
_TODO_HREFS: dict[str, str | None] = {
    "kb_lesson": "/admin/kb?tab=lessons",
    "kb_example": "/admin/kb?tab=examples",
    "semantic_draft": "/admin/semantic?pending=1",
    "skill_draft": "/admin/skills",
    "memory_preference": None,
    "drift": "/admin/datasources",
    "job_failed": "/admin/jobs?status=error",
    "user_nogrant": "/admin/users?status=nogrant",
}
#: 逐数据源扇出的三类(KB lesson / example / 语义草稿)。
_PER_SOURCE_TODO_KINDS = ("kb_lesson", "kb_example", "semantic_draft")


# ── 通用小件 ─────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _err_name(exc: BaseException) -> str:
    """错误只报类型名(沿用 ``/v1/health`` 的纪律:不回传驱动原文)。"""
    if isinstance(exc, asyncio.TimeoutError):
        return "Timeout"
    return type(exc).__name__


def _parse_window(value: str) -> timedelta:
    """``24h`` / ``7d`` → timedelta;非法值抛 ValueError(调用方映射 400)。"""
    m = _WINDOW_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"invalid window: {value!r} (expected e.g. 24h, 7d)")
    n, unit = int(m.group(1)), m.group(2)
    hours = n if unit == "h" else n * 24
    if hours < 1 or hours > _WINDOW_MAX_HOURS:
        raise ValueError(f"window out of range: {value!r} (1h .. 90d)")
    return timedelta(hours=hours)


async def _leg(
    block: str, source: str, degraded: list[dict],
    fn: Callable[[], Awaitable[Any]], *, timeout: float = _SOURCE_TIMEOUT_S,
) -> Any:
    """跑一条扇出腿:独立超时;失败 → degraded 条目并返回 None。

    **绝不让整页失败** —— 这是本端点存在的理由本身(设计稿 §5 方案 B)。
    ``CancelledError`` 是 BaseException,不在此列:调用方被取消时必须继续上抛。
    """
    try:
        return await asyncio.wait_for(fn(), timeout=timeout)
    except Exception as e:
        degraded.append({
            "block": block, "source": source,
            "error": _err_name(e), "at": _now_iso(),
        })
        return None


def _todo_item(
    kind: str, *, count: int | None, samples: list[str] | None = None,
    exact: bool = True, available: bool = True, note: str = "",
) -> dict:
    return {
        "kind": kind,
        "count": count,
        "count_exact": exact,
        "available": available,
        "samples": [s for s in (samples or []) if s][: _SAMPLES],
        "href": _TODO_HREFS.get(kind),
        "note": note,
    }


# ── 健康横幅(镜像 /v1/health 的判定与措辞) ──────────────


async def _storage_ping(request: Request) -> dict:
    """内部存储一次真实往返;永不抛(health 是 liveness 断言,探针也是)。"""
    backend = getattr(getattr(request.app.state, "session_store", None), "_backend", None)
    if backend is None:
        return {"ok": True, "skipped": "no backend"}
    try:
        cursor = await asyncio.wait_for(
            backend.execute("SELECT 1"), timeout=_STORAGE_PING_TIMEOUT_S,
        )
        await cursor.fetchone()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": _err_name(e)}
    finally:
        # 归还操作作用域:execute() 取锁,只有 commit()/close() 释放(见
        # app.py health 的同款注释 —— 漏掉会让后续存储操作阻塞到 60s)。
        try:
            await backend.close()
        except Exception:
            pass


async def _datasource_pings(request: Request) -> dict[str, dict]:
    """逐源 ``SELECT 1``(每源独立超时)+ 只读自检结论。

    错误只报类型名;``readonly`` 并进同一源条目但**不参与 ok**(与 health
    同一取舍:能不能用 / 边界在不在,修法毫无关系)。
    """
    registry = getattr(request.app.state, "connector_registry", None)
    if registry is None:
        return {}
    names = registry.list_names()

    async def _ping_one(name: str) -> tuple[str, dict]:
        async def _do() -> dict:
            adapter = await registry.get(name)
            if not getattr(adapter, "is_connected", False):
                return {"ok": False, "error": "NotConnected"}
            await adapter.execute("SELECT 1")
            return {"ok": True}

        try:
            return name, await asyncio.wait_for(_do(), timeout=_SOURCE_TIMEOUT_S)
        except Exception as e:
            return name, {"ok": False, "error": _err_name(e)}

    pairs = await asyncio.gather(*(_ping_one(n) for n in names))
    probes = getattr(request.app.state, "readonly_probes", None) or {}
    out: dict[str, dict] = {}
    for name, entry in pairs:
        hit = probes.get(name)
        entry["readonly"] = hit.to_health() if hit is not None else {
            "verified": None, "basis": BASIS_NOT_PROBED,
        }
        out[name] = entry
    return out


def _llm_facts(request: Request) -> dict:
    """LLM 只报事实(mock/target/providers),不下「能不能用」的结论。"""
    gateway = getattr(request.app.state, "llm_gateway", None)
    providers = getattr(gateway, "_providers", None)
    config = getattr(request.app.state, "config", None)
    return {
        "mock": bool(
            getattr(gateway, "_mock_response", None)
            or getattr(gateway, "_mock_stream_chunks", None)
        ),
        "target": getattr(config, "target", "") or "",
        "providers": len(providers) if providers else 0,
    }


async def _health_block(request: Request, degraded: list[dict]) -> dict | None:
    storage = await _leg(
        "health", "storage", degraded,
        lambda: _storage_ping(request), timeout=_STORAGE_PING_TIMEOUT_S + 0.5,
    )
    if storage is None:
        return None
    if not storage.get("ok"):
        degraded.append({
            "block": "health", "source": "storage",
            "error": storage.get("error") or "Unknown", "at": _now_iso(),
        })
    pings = await _leg(
        "health", "datasources", degraded,
        lambda: _datasource_pings(request), timeout=_SOURCE_TIMEOUT_S + 1.0,
    )
    if not storage.get("ok"):
        status = "unavailable"
    elif pings is None or any(not v.get("ok") for v in pings.values()):
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "storage": storage,
        "llm": _llm_facts(request),
        "datasources": pings,
    }


# ── 窗口内使用量(审计只读聚合) ──────────────────────────


def _audit_backend(request: Request):
    """审计表所在的 StorageBackend(auth store);没有 auth → None。"""
    auth = getattr(request.app.state, "auth", None)
    store = getattr(auth, "store", None)
    return getattr(store, "_backend", None)


def _classify_error(error_text: str, verdict: str) -> str:
    """粗桶:先认 ``[ERR:<id>]`` 打标,退到 verdict,再退到 UNKNOWN。

    这是**过渡期**办法 —— ``error_class`` 没有落任何可查询的表(设计稿 §5
    「失败分布」行),所以整块标 ``failures_approximate``。
    """
    m = _ERR_TAG_RE.search(error_text or "")
    if m:
        return m.group(1)
    return (verdict or "").strip().upper() or "UNKNOWN"


def _domain_of(cls: str) -> str:
    try:
        from trove.services.errors.classify import CLASSES

        hit = CLASSES.get(cls)
        return hit.domain if hit is not None else ""
    except Exception:
        return ""


def _usage_unavailable() -> dict:
    """审计面不存在(无 auth store):如实说不可用,不报降级、更不报 0。"""
    return {
        "available": False,
        "questions": None, "ok": None, "success_rate": None, "failures": None,
        "failures_by_class": [],
        "failures_source": "audit_text", "failures_approximate": True,
        "sample_size": None, "sample_capped": False, "count_exact": True,
    }


async def _usage_block(request: Request, cutoff: str, degraded: list[dict]) -> dict | None:
    auth = getattr(request.app.state, "auth", None)
    backend = _audit_backend(request)
    if auth is None or backend is None:
        return _usage_unavailable()

    async def _fetch() -> tuple[int, list]:
        try:
            # 公开入口先跑一次:保证 audit_log 已建表(与 store 第一次使用同一路径)
            await auth.count_audit()
            cursor = await backend.execute(_USAGE_TOTAL_SQL, ("query.execute", cutoff))
            row = await cursor.fetchone()
            total = int(row[0]) if row else 0
            cursor = await backend.execute(
                _USAGE_SQL, ("query.execute", cutoff, _USAGE_SAMPLE_MAX),
            )
            return total, await cursor.fetchall()
        finally:
            await backend.close()

    got = await _leg("usage", "audit", degraded, _fetch)
    if got is None:
        return None
    total, rows = got

    ok = 0
    buckets: dict[str, dict] = {}
    for (details_json,) in rows:
        try:
            details = json.loads(details_json) if details_json else {}
        except (TypeError, ValueError):
            details = {}
        verdict = str(details.get("verdict") or "")
        if verdict == "OK":
            ok += 1
            continue
        cls = _classify_error(str(details.get("error") or ""), verdict)
        bucket = buckets.setdefault(
            cls, {"class": cls, "domain": _domain_of(cls), "count": 0},
        )
        bucket["count"] += 1

    sample_capped = len(rows) >= _USAGE_SAMPLE_MAX
    # 取样到顶 → ok / success_rate / failures 一并留 null:截断过的计数不是计数。
    return {
        "available": True,
        "questions": total,
        "ok": None if sample_capped else ok,
        "success_rate": (
            round(ok / total, 4) if (total and not sample_capped) else None
        ),
        "failures": None if sample_capped else max(total - ok, 0),
        "failures_by_class": sorted(
            buckets.values(), key=lambda b: (-b["count"], b["class"]),
        ),
        "failures_source": "audit_text",
        "failures_approximate": True,
        "sample_size": len(rows),
        "sample_capped": sample_capped,
        "count_exact": not sample_capped,
    }


async def _recent_events_block(
    request: Request, cutoff: str, degraded: list[dict],
) -> list | None:
    auth = getattr(request.app.state, "auth", None)
    backend = _audit_backend(request)
    if auth is None or backend is None:
        return None

    async def _fetch() -> list:
        try:
            await auth.count_audit()
            cursor = await backend.execute(_EVENTS_SQL, (cutoff, _EVENT_LIMIT))
            return await cursor.fetchall()
        finally:
            await backend.close()

    rows = await _leg("recent_events", "audit", degraded, _fetch)
    if rows is None:
        return None
    return [
        {
            "ts": r[0], "action": r[1], "username": r[2], "status": r[3],
            # 深链带筛选态:审计页支持按 action 过滤。
            "href": f"/admin/audit?action={r[1]}",
        }
        for r in rows
    ]


# ── 数据源行 ────────────────────────────────────────────


def _project_root(request: Request) -> Path:
    """项目根:从 ``KbService.kb_dir``(``<root>/.trove/kb``)反推。

    与 ``routers/drift.py`` 同一手法;反推不出退回 cwd(只影响 drift store
    的位置,不值得抛错)。
    """
    kb_dir = getattr(getattr(request.app.state, "kb", None), "kb_dir", None)
    if kb_dir is not None:
        try:
            return Path(kb_dir).parent.parent
        except (TypeError, ValueError):
            pass
    return Path.cwd()


async def _datasource_names(request: Request) -> list[tuple[str, str]]:
    """``(name, status)`` 列表:已连接在前,持久化未连接在后(去重)。

    status 语义与 ``/admin/datasources`` 一致(connected / disconnected ——
    「连不上」与「KB 没建」是两件事,分开报)。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    registry = getattr(request.app.state, "connector_registry", None)
    if registry is not None:
        for info in registry.list_info():
            name = info["name"]
            seen.add(name)
            out.append((name, "connected"))
    store = getattr(request.app.state, "config_store", None)
    if store is not None:
        for cfg in store.load_configs():
            if cfg.name not in seen:
                seen.add(cfg.name)
                out.append((cfg.name, "disconnected"))
    return out


async def _kb_facts(
    request: Request, names: list[tuple[str, str]], degraded: list[dict],
) -> dict:
    """一次读完 KB 事实:各 kind 计数 + refused 资产 + 每个源的初始化判定。

    ``ensure_synced(None)`` 与 ``/admin/datasources`` 同一手法(镜像未建时先
    建表,否则 list_items 直接 500)。取不到 → 各字段 None(不冒充 0)。
    """
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        return {"items": None, "refused": None, "initialized": None}

    async def _load() -> dict:
        await kb.ensure_synced(None)
        return {
            "items": await kb.list_items(),
            "refused": dict(kb.refused_assets),
            "initialized": {n: kb.kb_initialized(n) for n, _ in names},
        }

    got = await _leg("datasources", "kb", degraded, _load)
    if got is None:
        return {"items": None, "refused": None, "initialized": None}
    return got


async def _drift_open(request: Request, name: str) -> dict:
    """一个源待处置(open)的漂移条数;500 处饱和(与 ``/admin/drift`` 同帽)。"""
    from trove.services.drift import DriftStore
    from trove.services.drift.store import STATUS_OPEN

    store = DriftStore(_project_root(request))
    try:
        rows = await store.list_items(name, status=STATUS_OPEN, limit=_DRIFT_CAP)
    finally:
        await store.dispose()
    return {"count": len(rows), "samples": [], "exact": len(rows) < _DRIFT_CAP}


async def _drift_facts(
    request: Request, names: list[tuple[str, str]], degraded: list[dict],
) -> dict[str, dict | None]:
    """逐源漂移计数;每源独立超时(数不出来的源不拖累其它源)。"""

    async def _one(name: str) -> tuple[str, dict | None]:
        got = await _leg(
            "datasources", f"drift:{name}", degraded,
            lambda: _drift_open(request, name),
        )
        return name, got

    return dict(await asyncio.gather(*(_one(n) for n, _ in names)))


def _datasource_rows(
    names: list[tuple[str, str]], kb_facts: dict,
    drift: dict[str, dict | None], probes: dict,
) -> list[dict]:
    rows: list[dict] = []
    items = kb_facts.get("items")
    initialized = kb_facts.get("initialized")
    refused = kb_facts.get("refused")
    for name, status in names:
        hit = probes.get(name)
        d = drift.get(name)
        rows.append({
            "name": name,
            "status": status,
            "kb_initialized": (
                initialized.get(name) if initialized is not None else None
            ),
            "kb_items": dict(items.get(name, {})) if items is not None else None,
            "refused": (
                sum(1 for rel in refused if rel.startswith(f"{name}/"))
                if refused is not None else None
            ),
            "drift_open": d.get("count") if d else None,
            "drift_count_exact": bool(d) and d.get("exact", True),
            "readonly": hit.to_health() if hit is not None else {
                "verified": None, "basis": BASIS_NOT_PROBED,
            },
        })
    return rows


# ── 待办队列 ────────────────────────────────────────────


async def _pending_lessons(request: Request, name: str) -> dict:
    kb = request.app.state.kb
    all_lessons = await kb.list_lessons(name, confirmed_only=False)
    pending = [ln for ln in all_lessons if not ln.get("confirmed")]
    return {
        "configured": True,
        "count": len(pending),
        "samples": [str(ln.get("pattern") or "") for ln in pending],
        "exact": True,
    }


async def _pending_examples(request: Request, name: str) -> dict:
    drafts = await request.app.state.kb.list_pending_examples(name)
    return {
        "configured": True,
        "count": len(drafts),
        "samples": [str(d.get("question") or "") for d in drafts],
        "exact": True,
    }


async def _pending_semantic_drafts(request: Request, name: str) -> dict:
    from trove.services.semantic_layer.manage import SemanticManager

    drafts = SemanticManager(request.app.state.kb).drafts(name).get("pending", [])
    return {
        "configured": True,
        "count": len(drafts),
        "samples": [str(d.get("name") or d.get("id") or "") for d in drafts],
        "exact": True,
    }


async def _per_source_todos(
    request: Request, names: list[tuple[str, str]], degraded: list[dict],
) -> dict[str, list[dict | None]]:
    """KB lesson / example / 语义草稿三类:逐源扇出,每源独立超时。"""
    kb = getattr(request.app.state, "kb", None)
    fns: dict[str, Callable[[Request, str], Awaitable[dict]]] = {
        "kb_lesson": _pending_lessons,
        "kb_example": _pending_examples,
        "semantic_draft": _pending_semantic_drafts,
    }
    out: dict[str, list[dict | None]] = {}
    for kind, fn in fns.items():
        if kb is None:
            out[kind] = [None] * len(names)
            continue

        async def _one(name: str, fn=fn, kind=kind) -> dict | None:
            return await _leg(
                "todos", f"{kind}:{name}", degraded, lambda: fn(request, name),
            )

        out[kind] = list(await asyncio.gather(*(_one(n) for n, _ in names)))
    return out


def _unconfigured() -> dict:
    return {"configured": False, "count": 0, "samples": [], "exact": True}


async def _skill_drafts(request: Request) -> dict:
    skills = getattr(request.app.state, "skills", None)
    if skills is None:
        return _unconfigured()
    pending = [s for s in skills.list_all() if s.get("status") == "pending"]
    return {
        "configured": True,
        "count": len(pending),
        "samples": [str(s.get("name") or "") for s in pending],
        "exact": True,
    }


async def _memory_drafts(request: Request) -> dict:
    memory = getattr(request.app.state, "memory", None)
    if memory is None or not getattr(memory, "enabled", False):
        return _unconfigured()
    drafts = await memory.preferences.list_pending()
    return {
        "configured": True,
        "count": len(drafts),
        "samples": [str(d.get("fact") or "") for d in drafts],
        "exact": True,
    }


async def _users_nogrant(request: Request) -> dict:
    auth = request.app.state.auth
    users, total = await auth.list_users_page(status="nogrant", limit=_SAMPLES)
    return {
        "configured": True,
        "count": int(total),
        "samples": [str(u.get("username") or "") for u in users],
        "exact": True,
    }


async def _jobs_failed(request: Request, cutoff: str) -> dict:
    """窗口内失败的任务运行数(跨 job 一次 SQL,不再 N+1)。"""
    jobs = getattr(request.app.state, "jobs", None)
    backend = getattr(getattr(jobs, "store", None), "_backend", None)
    if jobs is None or backend is None:
        return _unconfigured()
    jobs_all = await jobs.list_jobs()  # 公开入口,顺带保证 runs 表已建
    names = {job.id: job.name for job in jobs_all}
    try:
        cursor = await backend.execute(_RUNS_FAILED_SQL, ("error", cutoff))
        rows = await cursor.fetchall()
    finally:
        await backend.close()
    return {
        "configured": True,
        "count": sum(int(r[1]) for r in rows),
        "samples": [str(names.get(str(r[0])) or r[0]) for r in rows],
        "exact": True,
    }


def _merge_todo(kind: str, partials: list[dict | None]) -> dict:
    """把逐源部分结果合成一条待办项。

    任一源的腿降级(``None``)→ 计数照实少算并标 ``count_exact: false``
    (设计稿 §3:降级时给「≥ N」);逐源都成但计数自身饱和(exact=False)同理。
    """
    failed = any(p is None for p in partials)
    ok_parts = [p for p in partials if p is not None]
    count = sum(int(p.get("count") or 0) for p in ok_parts)
    samples = [s for p in ok_parts for s in p.get("samples", [])]
    exact = (not failed) and all(p.get("exact", True) for p in ok_parts)
    note = "" if exact else ("degraded" if failed else "capped")
    return _todo_item(
        kind, count=count, samples=samples, exact=exact,
        available=not failed, note=note,
    )


def _todo_from_leg(kind: str, got: dict | None, *, failed: bool) -> dict:
    """非逐源待办项的归一:腿降级 → null/degraded;未装配 → 0/available false。"""
    if failed or got is None:
        return _todo_item(
            kind, count=None, exact=False, available=False, note="degraded",
        )
    if got.get("configured") is False:
        return _todo_item(kind, count=0, exact=True, available=False)
    return _todo_item(
        kind, count=int(got.get("count") or 0), samples=got.get("samples", []),
        exact=bool(got.get("exact", True)), available=True,
    )


def _degraded_item(kind: str) -> dict:
    return _todo_item(
        kind, count=None, exact=False, available=False, note="degraded",
    )


async def _todos_block(
    request: Request, names: list[tuple[str, str]] | None,
    drift: dict[str, dict | None], shared: dict[str, dict | None],
    failed_legs: set[str], degraded: list[dict],
) -> dict:
    """待办 8 类,固定顺序。``names is None`` = 源都列不出来(登记扇出降级)。"""
    enumeration_failed = names is None
    per_source = (
        {kind: [] for kind in _PER_SOURCE_TODO_KINDS}
        if enumeration_failed
        else await _per_source_todos(request, names, degraded)
    )
    drift_partials: list[dict | None] = []
    for name, _ in (names or []):
        got = drift.get(name)
        drift_partials.append({**got, "samples": []} if got is not None else None)

    items: list[dict] = []
    for kind in _TODO_HREFS:
        if kind in per_source:
            items.append(
                _degraded_item(kind)
                if enumeration_failed
                else _merge_todo(kind, per_source[kind])
            )
        elif kind == "drift":
            items.append(
                _degraded_item(kind)
                if enumeration_failed
                else _merge_todo(kind, drift_partials)
            )
        else:
            items.append(
                _todo_from_leg(kind, shared.get(kind), failed=kind in failed_legs)
            )

    total = sum(int(i["count"]) for i in items if i["count"] is not None)
    exact = all(i["count_exact"] and i["count"] is not None for i in items)
    return {"total": total, "count_exact": exact, "items": items}


def _wizard_block(
    names: list[tuple[str, str]] | None, kb_facts: dict,
    nogrant: dict | None, *, nogrant_failed: bool,
) -> dict:
    """接入向导三步:注册 / KB init / 授权 —— 全部读既有字段,无新状态。"""
    initialized = kb_facts.get("initialized")
    # 授权步数拿不到就是 null(降级或未装配),不冒充 0。
    users_without_grant = (
        None if (nogrant_failed or nogrant is None) else int(nogrant["count"])
    )
    return {
        "registered": len(names) if names is not None else None,
        "kb_initialized": (
            sum(1 for v in initialized.values() if v) if initialized is not None else None
        ),
        "users_without_grant": users_without_grant,
    }


# ── 端点 ────────────────────────────────────────────────


def _payload(
    *, generated_at: str, elapsed_ms: int, window: str, health: dict | None,
    usage: dict | None, todos: dict | None, datasources: list | None,
    wizard: dict | None, recent_events: list | None, degraded: list[dict],
) -> dict:
    """固定的响应形状:字段始终齐全,取不到的块就是 null。"""
    return {
        "generated_at": generated_at,
        "elapsed_ms": elapsed_ms,
        "window": window,
        "health": health,
        "usage": usage,
        "todos": todos,
        "datasources": datasources,
        "wizard": wizard,
        "recent_events": recent_events,
        "degraded": degraded,
    }


@router.get("/admin/overview")
async def admin_overview(
    request: Request,
    window: str = Query(default="24h", description="lookback window, e.g. 24h / 7d"),
    admin: dict = Depends(require_admin),
) -> JSONResponse:
    """总览 Dashboard 的一次性只读聚合(见模块 docstring 的三条纪律)。"""
    started = time.perf_counter()
    try:
        delta = _parse_window(window)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    generated_at = _now_iso()
    cutoff = (datetime.now(timezone.utc) - delta).isoformat()
    degraded: list[dict] = []

    health = await _health_block(request, degraded)

    # 唯一的整页级失败:内部存储探不通(与 /v1/health 同一判定)。storage 报
    # 挂时连审计与 KB 都读不到,没有部分结果可言 —— 503 + 其余块 null。
    if health is not None and health["storage"].get("ok") is False:
        return JSONResponse(status_code=503, content=_payload(
            generated_at=generated_at, window=window,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            health=health, usage=None, todos=None, datasources=None,
            wizard=None, recent_events=None, degraded=degraded,
        ))

    shared: dict[str, dict | None] = {}
    failed_legs: set[str] = set()
    for kind, fn in (
        ("skill_draft", lambda: _skill_drafts(request)),
        ("memory_preference", lambda: _memory_drafts(request)),
        ("job_failed", lambda: _jobs_failed(request, cutoff)),
        ("user_nogrant", lambda: _users_nogrant(request)),
    ):
        before = len(degraded)
        shared[kind] = await _leg("todos", kind, degraded, fn)
        if len(degraded) > before:
            failed_legs.add(kind)

    usage = await _usage_block(request, cutoff, degraded)
    recent_events = await _recent_events_block(request, cutoff, degraded)

    names = await _leg(
        "datasources", "registry", degraded, lambda: _datasource_names(request),
    )
    if names is None:
        datasources = None
        kb_facts: dict = {"items": None, "refused": None, "initialized": None}
        drift: dict[str, dict | None] = {}
    else:
        kb_facts = await _kb_facts(request, names, degraded)
        drift = await _drift_facts(request, names, degraded)
        datasources = _datasource_rows(
            names, kb_facts, drift,
            getattr(request.app.state, "readonly_probes", None) or {},
        )

    todos = await _todos_block(request, names, drift, shared, failed_legs, degraded)
    wizard = _wizard_block(
        names, kb_facts, shared.get("user_nogrant"),
        nogrant_failed="user_nogrant" in failed_legs,
    )

    return JSONResponse(status_code=200, content=_payload(
        generated_at=generated_at, window=window,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        health=health, usage=usage, todos=todos, datasources=datasources,
        wizard=wizard, recent_events=recent_events, degraded=degraded,
    ))
