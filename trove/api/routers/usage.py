"""``GET /v1/admin/usage/overview`` —— 成本与性能运营台(admin,只读聚合)。

设计稿:``~/Downloads/trove-page-p4-ops.html`` §4.2(冻结契约)+ 2026-10-02
五项裁决。三个数据面各有各的诚实口径,混用任何一个都会撒不同的谎:

- **成本** = 会话消息投影(裁决④):assistant 消息的 ``metadata_json.token_usage``
  (``session.py`` 写入,字段含 prompt/completion/total + 两个拼法的缓存字段)。
  采样上限 5000 条;**精确计数**(COUNT)与采样求和分开报 —— 采样到顶时求和类
  字段一律 null(截断过的和在数学上就是错的),计数类字段仍然精确。
- **预算**(Prometheus 快照)与 **连接器缓存命中**:进程生命周期(记账起点是
  进程启动,不是窗口起点,所以不带窗口过滤 —— 快照计数器没有时间维度;
  这是口径,不是近似)。降级计数与预算裁决是两类计数器,分开报。
- **延迟** = ``audit_log`` 的 ``query.execute`` 行,``details.execution_time_ms``。
  **只在 serve 模式有数据**(``session.py::_audit_query`` 要求 auth service
  存在):CLI/嵌入式跑出来的行不在表里,页面据此显示"未测到"(诚实降级,
  不伪造)。端到端时延本轮不测(end_to_end 恒 null,见设计稿 §7)。

唯一整页级失败 = 内部存储探不通(与 P3 ``/admin/overview`` 同一判定):
storage 报挂时三块读数里有两块直接读不到,没有部分结果可言 → 503。
其余每条腿独立超时、失败只 degraded;本页绝不因一条腿而整体失败。

未来公式(裁决③,本轮只落注释、不加表/列):
- token 归因到模型:读侧从 message metadata 投影(数据已在,缺的只是聚合端点);
- 错误分类:写审计时按 details JSON 分类(现在 audit 只存自由文本 error,
  P3 ``_classify_error`` 已在读侧做粗桶,质量台沿用同一套桶即可,无需新列)。
"""

from __future__ import annotations

import asyncio
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from trove.api.deps import require_admin_or_analyst
from trove.api.routers.overview import (
    _audit_backend,
    _leg,
    _now_iso,
    _parse_window,
    _project_root,
    _storage_ping,
)
from trove.llm.token_accounting import cache_hit_tokens

router = APIRouter()

#: 成本/延迟合并采样的上限(两侧同一帽;超过 → sample_capped 且和类字段 null)。
_SAMPLE_MAX = 5000

#: 与 session.py 写入侧一致的 JSON 片段探测(metadata_json LIKE)。
_TOKEN_USAGE_LIKE = '%"token_usage"%'

_COST_TOTAL_SQL = "SELECT COUNT(*) FROM messages WHERE role = ? AND timestamp >= ?"
_COST_WITH_USAGE_SQL = (
    "SELECT COUNT(*) FROM messages WHERE role = ? AND timestamp >= ? "
    "AND metadata_json LIKE ?"
)
_COST_ROWS_SQL = (
    "SELECT metadata_json FROM messages WHERE role = ? AND timestamp >= ? "
    "AND metadata_json LIKE ? ORDER BY timestamp DESC LIMIT ?"
)
_LATENCY_SQL = (
    "SELECT ts, details_json FROM audit_log WHERE action = ? AND ts >= ? "
    "ORDER BY ts DESC LIMIT ?"
)

#: 基线 scorecard(缓存命中率的唯一离线来源;路径与 EvalConfig 同根)。
_SCORECARD_RELPATH = Path("eval") / "baseline" / "scorecard.json"

_NOT_MEASURED = ["cost.by_model", "cache.answer", "latency.end_to_end", "cost.spend"]


# ── 小工具 ────────────────────────────────────────────────────


def _percentile_ms(values: list[int], q: float) -> int | None:
    """最近秩百分位(免 numpy):n 个样本取第 ceil(q·n) 个。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(q * len(ordered)) - 1)
    return int(ordered[idx])


def _counter_series(counter: Any, labels: tuple[str, ...]) -> list[dict]:
    """把一个 Prometheus Counter 的样本摊成行(空/无客户端 → [])。"""
    out: list[dict] = []
    if counter is None:
        return out
    try:
        families = counter.collect()
    except Exception:
        return out
    for family in families:
        total_name = f"{family.name}_total"
        for sample in family.samples:
            if not sample.name.endswith("_total"):
                continue
            if sample.name != total_name:
                continue
            row: dict[str, Any] = {name: sample.labels.get(name, "") for name in labels}
            row["count"] = int(sample.value)
            out.append(row)
    out.sort(key=lambda r: (-r["count"], tuple(str(r[k]) for k in labels)))
    return out


def _counter_total(counter: Any) -> int | None:
    total = 0
    seen = False
    try:
        families = counter.collect()
    except Exception:
        return None
    for family in families:
        for sample in family.samples:
            if sample.name.endswith("_total"):
                seen = True
                total += int(sample.value)
    return total if seen else None


# ── 成本块:消息 metadata 投影 ────────────────────────────────


def _cost_unavailable() -> dict:
    """没有会话存储(SessionStore 未装配):如实说不可用,不报 0。"""
    return {
        "source": "message_metadata",
        "sampled": None,
        "sample_capped": False,
        "sample_max": _SAMPLE_MAX,
        "tokens": None,
        "cache_tokens": None,
        "unmeasured": {"assistant_messages": None, "without_usage": None, "ratio": None},
        "per_question": None,
        "by_model": None,
    }


async def _cost_block(request: Request, cutoff: str) -> dict | None:
    store = getattr(request.app.state, "session_store", None)
    backend = getattr(store, "backend", None)
    if backend is not None and callable(backend):
        backend = backend()          # SessionStore.backend() 是方法,不是属性
    if store is None or backend is None:
        return _cost_unavailable()

    async def _fetch() -> tuple[int, int, list]:
        try:
            # 公开入口先跑一次:保证 messages 表已建(与 store 第一次使用同一路径)。
            await store.list_sessions(limit=1)
            cur = await backend.execute(_COST_TOTAL_SQL, ("assistant", cutoff))
            total = int((await cur.fetchone() or [0])[0])
            cur = await backend.execute(_COST_WITH_USAGE_SQL, ("assistant", cutoff, _TOKEN_USAGE_LIKE))
            with_usage = int((await cur.fetchone() or [0])[0])
            cur = await backend.execute(
                _COST_ROWS_SQL, ("assistant", cutoff, _TOKEN_USAGE_LIKE, _SAMPLE_MAX),
            )
            return total, with_usage, await cur.fetchall()
        finally:
            # 归还操作作用域:execute() 取锁,只有 commit()/close() 释放。
            try:
                await backend.close()
            except Exception:
                pass

    total_assistant, with_usage, rows = await _fetch()

    sampled = len(rows)
    sample_capped = with_usage > sampled
    prompt = completion = total = 0
    cache_tokens = 0
    cache_seen = False
    measured = 0
    per_message: list[int] = []
    for (meta_json,) in rows:
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except (TypeError, ValueError):
            continue
        usage = meta.get("token_usage") or {}
        if not usage:
            continue
        measured += 1
        prompt += int(usage.get("prompt") or 0)
        completion += int(usage.get("completion") or 0)
        total += int(usage.get("total") or 0)
        per_message.append(int(usage.get("total") or 0))
        hit = cache_hit_tokens(usage)
        if hit is not None:
            cache_tokens += int(hit)
            cache_seen = True

    # 一切求和类字段在「采样到顶」或「一条都没测到」时留 null —— 0 是"测到且为零"。
    partial = sample_capped or measured == 0
    return {
        "source": "message_metadata",
        "sampled": sampled,
        "sample_capped": sample_capped,
        "sample_max": _SAMPLE_MAX,
        "tokens": None if partial else {"prompt": prompt, "completion": completion, "total": total},
        "cache_tokens": (None if partial or not cache_seen else cache_tokens),
        "unmeasured": {
            "assistant_messages": total_assistant,
            "without_usage": max(total_assistant - with_usage, 0),
            "ratio": (
                round(max(total_assistant - with_usage, 0) / total_assistant, 4)
                if total_assistant
                else None
            ),
        },
        "per_question": (
            None if partial else {
                "mean_total": int(round(statistics.fmean(per_message))),
                "median_total": int(round(statistics.median(per_message))),
            }
        ),
        "by_model": None,
    }


# ── 预算块:Prometheus 快照 ───────────────────────────────────


def _budget_block() -> dict:
    from trove.core import metrics

    return {
        "source": "prometheus_snapshot",
        "lifetime": "process",
        "decisions": _counter_series(
            metrics.SQL_BUDGET_DECISIONS, ("datasource", "source", "verdict"),
        ),
        # 降级 ≠ 预算裁决:这两条是独立计数器,不并入 decisions。
        "degraded": _counter_series(metrics.SQL_DEGRADED, ("datasource",)),
        "kills": _counter_series(metrics.SQL_KILL, ("datasource", "result")),
    }


# ── 缓存块 ────────────────────────────────────────────────────


def _prompt_cache_sync(path: Path) -> dict | None:
    """基线 scorecard 里的 ``cache_hit_rate``;文件在但无该指标 → None(未测)。"""
    if not path.exists():
        raise FileNotFoundError(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    rate = (data.get("metrics") or {}).get("cache_hit_rate")
    if rate is None:
        return None
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        mtime = None
    return {"cache_hit_rate": float(rate), "source": "eval_baseline_scorecard", "at": mtime}


async def _cache_block(request: Request, degraded: list[dict]) -> dict:
    from trove.core import metrics

    connector = None
    hits = _counter_total(metrics.SQL_CACHE_HITS)
    if hits is not None:
        connector = {"hits": hits, "lifetime": "process"}
    scorecard = _project_root(request) / _SCORECARD_RELPATH
    prompt = await _leg(
        "cache", str(scorecard), degraded,
        lambda: asyncio.to_thread(_prompt_cache_sync, scorecard),
    )
    return {"connector": connector, "answer": None, "prompt": prompt}


# ── 延迟块:审计投影 ──────────────────────────────────────────


async def _latency_block(request: Request, cutoff: str, degraded: list[dict]) -> dict | None:
    auth = getattr(request.app.state, "auth", None)
    backend = _audit_backend(request)
    if auth is None or backend is None:
        return None  # CLI/嵌入式:审计面不存在 —— 未测到,不是降级

    async def _fetch() -> list:
        try:
            # 公开入口先跑一次:保证 audit_log 已建表(与 store 第一次使用同一路径)。
            await auth.count_audit()
            cur = await backend.execute(_LATENCY_SQL, ("query.execute", cutoff, _SAMPLE_MAX))
            return await cur.fetchall()
        finally:
            try:
                await backend.close()
            except Exception:
                pass

    rows = await _leg("latency", "audit", degraded, _fetch)
    if rows is None:
        return None

    samples: list[int] = []
    by_day: dict[str, list[int]] = {}
    for ts, details_json in rows:
        try:
            details = json.loads(details_json) if details_json else {}
        except (TypeError, ValueError):
            details = {}
        ms = details.get("execution_time_ms")
        if not isinstance(ms, (int, float)) or isinstance(ms, bool):
            continue
        value = int(ms)
        samples.append(value)
        by_day.setdefault(str(ts)[:10], []).append(value)

    return {
        "basis": "audit.details.execution_time_ms",
        "n": len(samples),
        "sample_capped": len(rows) >= _SAMPLE_MAX,
        "p50_ms": _percentile_ms(samples, 0.5),
        "p95_ms": _percentile_ms(samples, 0.95),
        "series": [
            {"date": day, "p50_ms": _percentile_ms(values, 0.5), "n": len(values)}
            for day, values in sorted(by_day.items())
        ],
        "end_to_end": None,
    }


# ── 端点 ──────────────────────────────────────────────────────


def _payload(
    *, available: bool, generated_at: str, window: dict | None, cost: dict | None,
    budget: dict | None, cache: dict | None, latency: dict | None,
    degraded: list[dict],
) -> dict:
    """固定形状:字段始终齐全,取不到的块就是 null。

    ``available`` = 本页是否接上了任何"有账可查"的面(会话存储或审计面)——
    两者都没有时是"没装配",不是"坏了":不报 degraded,但也不许说可用。
    """
    return {
        "available": available,
        "window": window,
        "cost": cost,
        "budget": budget,
        "cache": cache,
        "latency": latency,
        "not_measured": list(_NOT_MEASURED),
        "degraded": degraded,
        "generated_at": generated_at,
    }


@router.get("/admin/usage/overview")
async def admin_usage_overview(
    request: Request,
    window: str = Query(default="7d", description="lookback window, e.g. 7d / 30d / 90d"),
    _admin: dict = Depends(require_admin_or_analyst),
) -> JSONResponse:
    """成本与性能总览(见模块 docstring 的三条口径纪律)。"""
    try:
        delta = _parse_window(window)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    generated_at = _now_iso()
    until = datetime.now(timezone.utc)
    cutoff_dt = until - delta
    cutoff = cutoff_dt.isoformat()
    window_block = {
        "kind": window,
        "since": cutoff,
        "until": until.isoformat(),
        "basis": "audit.ts",
    }
    degraded: list[dict] = []
    wired = (
        getattr(request.app.state, "session_store", None) is not None
        or _audit_backend(request) is not None
    )

    # 唯一的整页级失败:内部存储探不通(与 P3 /admin/overview 同一判定)。
    storage = await _storage_ping(request)
    if not storage.get("ok"):
        degraded.append({
            "block": "usage", "source": "storage",
            "error": storage.get("error") or "Unknown", "at": generated_at,
        })
        return JSONResponse(status_code=503, content=_payload(
            available=False, generated_at=generated_at, window=window_block,
            cost=None, budget=None, cache=None, latency=None, degraded=degraded,
        ))

    cost = await _leg("cost", "messages", degraded, lambda: _cost_block(request, cutoff))
    budget = await _leg("budget", "metrics", degraded, lambda: asyncio.to_thread(_budget_block))
    cache = await _leg("cache", "metrics", degraded, lambda: _cache_block(request, degraded))
    # _cache_block 自己会往 degraded 里写 prompt 腿,不能再包一层 _leg 重复记账;
    # 上面的 _leg 只兜"整个缓存块构造失败"(如 metrics 模块导入坏了)。
    latency = await _latency_block(request, cutoff, degraded)

    return JSONResponse(content=_payload(
        available=wired, generated_at=generated_at, window=window_block, cost=cost,
        budget=budget, cache=cache, latency=latency, degraded=degraded,
    ))
