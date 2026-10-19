"""production / synthetic 冷启动采集 —— 真实行为记录与 schema 合成两类候选。

本模块只**产出候选**(``GeneratedItem``):不落盘、不去重、不碰确认门 ——
合并进 ``evalset.yml``、去重与泄漏标记由编排层(Task 7)负责。两条路径:

1. **production**(设计 §6.3):从 ``episodes`` + ``query.execute`` 审计 +
   ``query.refused`` 审计收割真实问答。判定逐条独立,同一问题按
   ``ledger.normalize_question`` 合并:episodes 先入(信息更多),审计只做
   ``(question.lower(), sql)`` 精确去重,拒绝条目只在没有同题可答条目时补入。
   失败/空结果/被拒的问题同样**值得收录**(``needs_reference=True``)——
   验收集不只需要"能答对的题",还需要"现在答不了的题"作为覆盖缺口。
   输入路径走 ``history_distill.validate_source_path`` 的同一道白名单闸
   (``eval/`` 产物绝不进采集)。

2. **synthetic**(设计 §6.2):复用 ``kb.synthetic`` 的 LLM 合成 +
   SQLGlot/试执行双重护栏。无 LLM → ``status="no_llm"``(不报成功);
   护栏后为空 → ``status="ok"`` 但 ``degraded`` 记 ``synthetic_empty``
   —— 「生成了 0 条」不是成功,也不能被读成"这批题已验证"。

红线:

- **只读**:episodes/审计/lineage 一律不写;本模块构造上没有任何写入面。
- **隐私**:产物不带用户归属 —— 采集回答「问过什么」,不回答「谁问的」;
  全部自动条目 ``created_by="system"``。
- **零自动确认**:产物只是候选,确认门在编排层与服务层。
- **禁背题**:只消费真实行为记录与数据源 schema,不 import 任何 benchmark。
"""

from __future__ import annotations

import re
from typing import Any

from trove.core.logging import get_logger
from trove.services.evalset.generate import (
    GenerateContext,
    GeneratedItem,
    SourceResult,
)
from trove.services.evalset.models import MAX_GENERATE_PER_RUN
from trove.services.kb.history_distill import validate_source_path
from trove.services.kb.ledger import normalize_question
from trove.services.kb.synthetic import (
    generate_synthetic_examples,
    validate_examples,
)

logger = get_logger(__name__)

#: production 产物的来源 / 参考 SQL 来源 / 生成器标识(闭集值)。
SOURCE_PRODUCTION = "production"
ORIGIN_PIPELINE = "pipeline"
GENERATOR_PRODUCTION = "evalset.production.v1"

#: synthetic 产物的来源 / 参考 SQL 来源 / 生成器标识。
SOURCE_SYNTHETIC = "synthetic"
ORIGIN_LLM = "llm"
GENERATOR_SYNTHETIC = "evalset.synthetic.v1"

#: 自动来源的统一归属(隐私边界:不写具体用户)。
CREATED_BY_SYSTEM = "system"

_SUCCESS_HEAD = "OK"
_EMPTY_HEAD = "EMPTY"

#: 与 ``history_distill._verdict_head`` 逐字同款(跨层不 import 的副本;
#: 相等性由 ``test_verdict_head_matches_distill`` 钉住)。
_VERDICT_TOKEN_RE = re.compile(r"^\s*([A-Za-z_]+)")


def _verdict_head(verdict: str) -> str:
    """``OK`` / ``RETRY: …`` / ``NO_SQL`` → 首个词的上档形态(空 = 无判定)。"""
    m = _VERDICT_TOKEN_RE.match(verdict or "")
    return m.group(1).upper() if m else ""


def _cap_limit(limit: int) -> int:
    """单次采集上限:与其它源同一条硬上限(组合/历史再多也不越过)。"""
    return max(0, min(int(limit or 0), MAX_GENERATE_PER_RUN))


def _note(*parts: str) -> str:
    """拼接非空注记段(空段不留分隔符)。"""
    return "; ".join(part for part in parts if part)


def _production_item(
    *,
    question: str,
    reference_sql: str,
    needs_reference: bool = False,
    note: str = "",
) -> GeneratedItem:
    return GeneratedItem(
        question=question,
        reference_sql=reference_sql,
        source=SOURCE_PRODUCTION,
        reference_origin=ORIGIN_PIPELINE,
        generator=GENERATOR_PRODUCTION,
        tags={},
        note=note,
        needs_reference=needs_reference,
        created_by=CREATED_BY_SYSTEM,
    )


def _episode_item(row: dict[str, Any]) -> GeneratedItem | None:
    """episodes 行 → 候选(设计 §6.3 表;空问题不成题)。"""
    question = str(row.get("question") or "").strip()
    if not question:
        return None
    sql = str(row.get("sql") or "").strip()
    verdict = str(row.get("verdict") or "")
    corrections = [str(c) for c in (row.get("correction_history") or [])]
    head = _verdict_head(verdict)

    if corrections:
        # 修正过说明最终 SQL 仍可疑:参考 SQL 留空,但把它写进 note 供
        # 确认人参考(按 §6.3 表仍判 needs_reference=true)。
        return _production_item(
            question=question,
            reference_sql="",
            needs_reference=True,
            note=_note(
                f"历史修正 {len(corrections)} 次",
                f"最终 SQL(仅供参考): {sql}" if sql else "",
            ),
        )
    if head == _SUCCESS_HEAD:
        return _production_item(
            question=question, reference_sql=sql, needs_reference=not sql,
        )
    if head == _EMPTY_HEAD:
        # 空结果是弱信号:SQL 保留供参考,但 oracle 不可信。
        return _production_item(
            question=question, reference_sql=sql, needs_reference=True,
        )
    if head:
        return _production_item(
            question=question,
            reference_sql="",
            needs_reference=True,
            note=f"verdict: {verdict.strip()}",
        )
    # 无 verdict、无 error、无修正史 = 视为交付过(与 is_success 兜底一致)。
    return _production_item(
        question=question, reference_sql=sql, needs_reference=not sql,
    )


def _audit_item(row: dict[str, Any]) -> GeneratedItem | None:
    """``query.execute`` 审计行 → 候选(设计 §6.3 表)。"""
    question = str(row.get("question") or "").strip()
    if not question:
        return None
    sql = str(row.get("sql") or "").strip()
    verdict = str(row.get("verdict") or "")
    error = str(row.get("error") or "").strip()
    head = _verdict_head(verdict)

    if error:
        return _production_item(
            question=question,
            reference_sql="",
            needs_reference=True,
            note=_note(
                f"verdict: {verdict.strip()}" if verdict.strip() else "",
                f"error: {error}",
            ),
        )
    if head == _SUCCESS_HEAD:
        return _production_item(
            question=question, reference_sql=sql, needs_reference=not sql,
        )
    if head == _EMPTY_HEAD:
        return _production_item(
            question=question, reference_sql=sql, needs_reference=True,
        )
    # 其余判定(含无判定的执行记录)= 失败。
    return _production_item(
        question=question,
        reference_sql="",
        needs_reference=True,
        note=f"verdict: {verdict.strip()}" if verdict.strip() else "",
    )


def _refusal_item(entry: dict[str, Any]) -> GeneratedItem | None:
    """``query.refused`` 条目 → 候选(问题本身值得收录,但没有参考 SQL)。"""
    question = str(entry.get("question") or "").strip()
    if not question:
        return None
    reason = str(entry.get("reason") or "").strip()
    miss_reason = str(entry.get("miss_reason") or "").strip()
    return _production_item(
        question=question,
        reference_sql="",
        needs_reference=True,
        note=_note(
            f"reason: {reason}" if reason else "",
            f"miss_reason: {miss_reason}" if miss_reason else "",
        ),
    )


async def _read_episodes(
    ctx: GenerateContext, datasource: str, *, since: str, fetch: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读 episodes(只读;读失败降级不抛,空 home 不造文件)。"""
    episodes_path = validate_source_path(
        ctx.home_dir / "memory" / "episodes.sqlite",
        home_dir=ctx.home_dir, project_root=ctx.project_root,
    )
    if not episodes_path.exists():
        return [], []

    from trove.services.memory.episode import EpisodeStore

    store = EpisodeStore(episodes_path)
    try:
        rows = await store.iter_episodes(
            datasource, since=since or None, limit=fetch,
        )
        return rows, []
    except Exception as e:  # noqa: BLE001 — 读失败降级,但必须可见
        logger.warning("evalset production: episode read failed (%s): %s",
                       datasource, e)
        return [], [{"code": "episode_read_failed", "message": str(e)}]
    finally:
        # 短生命周期所有者:不 dispose 会挂住进程退出(与 collect_history 同)。
        await store.dispose()


async def _read_audit(
    ctx: GenerateContext, datasource: str, *, since: str, fetch: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读 ``query.execute`` 审计行(只读;失败降级)。"""
    if ctx.auth_store is None:
        return [], []
    try:
        rows = await ctx.auth_store.aggregate_query_audit(
            datasource=datasource, since=since or "", limit=fetch,
        )
        return list(rows or []), []
    except Exception as e:  # noqa: BLE001
        logger.warning("evalset production: audit read failed (%s): %s",
                       datasource, e)
        return [], [{"code": "audit_read_failed", "message": str(e)}]


async def _read_refusals(
    ctx: GenerateContext, *, since: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读 ``query.refused`` 窗口(只读;失败降级)。"""
    if ctx.auth_store is None:
        return {}, []
    try:
        report = await ctx.auth_store.aggregate_refusal_audit(since=since or "")
        return (report if isinstance(report, dict) else {}), []
    except Exception as e:  # noqa: BLE001
        logger.warning("evalset production: refusal read failed: %s", e)
        return {}, [{"code": "refusal_read_failed", "message": str(e)}]


async def collect_production(
    ctx: GenerateContext, *, limit: int, since: str = "",
) -> SourceResult:
    """从真实行为记录收割候选(设计 §6.3;只读、无用户归属、零确认)。

    合并优先级:episodes(信息更多)先入,同一 ``normalize_question`` 的
    audit/refusal 后到不覆盖;audit 之间只按 ``(question.lower(), sql)``
    精确去重;refusal 只在没有同题可答条目时补入,并按归一问题去重。
    全部条目截断到 ``limit``(硬上限 ``MAX_GENERATE_PER_RUN``)。
    """
    limit = _cap_limit(limit)
    datasource = str(ctx.datasource or "")
    if not datasource or limit == 0:
        return SourceResult()

    degraded: list[dict[str, Any]] = []
    items: list[GeneratedItem] = []
    # episodes 收录过的归一问题(后续 audit/refusal 不再覆盖)。
    episode_questions: set[str] = set()
    # 已有可答条目的归一问题(拒绝条目只在没有可答版本时补入)。
    answerable_questions: set[str] = set()

    fetch = limit * 2
    episode_rows, episode_degraded = await _read_episodes(
        ctx, datasource, since=since, fetch=fetch,
    )
    degraded.extend(episode_degraded)
    for row in episode_rows:
        if not isinstance(row, dict):
            continue
        item = _episode_item(row)
        if item is None:
            continue
        key = normalize_question(item.question)
        if key in episode_questions:
            # 同一问题多条 episode:最近一条(iter 已按 updated_at 倒序)胜出。
            continue
        episode_questions.add(key)
        items.append(item)
        if not item.needs_reference:
            answerable_questions.add(key)

    audit_rows, audit_degraded = await _read_audit(
        ctx, datasource, since=since, fetch=fetch,
    )
    degraded.extend(audit_degraded)
    seen_audit: set[tuple[str, str]] = set()
    for row in audit_rows:
        if not isinstance(row, dict):
            continue
        pair = (
            str(row.get("question") or "").strip().lower(),
            str(row.get("sql") or "").strip(),
        )
        if pair in seen_audit:
            continue
        seen_audit.add(pair)
        item = _audit_item(row)
        if item is None:
            continue
        key = normalize_question(item.question)
        if key in episode_questions:
            continue  # episodes 信息更多:不覆盖
        items.append(item)
        if not item.needs_reference:
            answerable_questions.add(key)

    refusal_report, refusal_degraded = await _read_refusals(ctx, since=since)
    degraded.extend(refusal_degraded)
    entries = refusal_report.get("entries") or []
    if not isinstance(entries, list):
        entries = []
    if refusal_report.get("capped"):
        degraded.append({
            "code": "refusal_scan_capped",
            "total": refusal_report.get("total"),
            "scanned": len(entries),
            "message": (
                "拒绝审计窗口超过读取上限;拒绝条目聚合只是下界"
            ),
        })
    seen_refusals: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # aggregate_refusal_audit 不接 datasource,由调用方过滤。
        if str(entry.get("datasource") or "") != datasource:
            continue
        item = _refusal_item(entry)
        if item is None:
            continue
        key = normalize_question(item.question)
        if key in seen_refusals or key in answerable_questions:
            continue
        seen_refusals.add(key)
        items.append(item)

    truncated = items[:limit]
    return SourceResult(
        items=truncated,
        skipped=len(items) - len(truncated),
        degraded=degraded,
        status="ok",
    )


async def generate_synthetic(
    ctx: GenerateContext, *, limit: int,
) -> SourceResult:
    """LLM 合成 + 双重护栏 → 候选(设计 §6.2;只做覆盖/回归,不进头部准确率)。

    ``ctx.llm`` 缺失 → ``status="no_llm"``(不是成功);护栏后为空 →
    ``status="ok"`` 但 ``degraded`` 记 ``synthetic_empty`` —— 不把「零条」
    读成「这批题都验证过」。LLM 调用失败如实报 ``status="error"``。
    """
    limit = _cap_limit(limit)
    if ctx.llm is None:
        return SourceResult(
            [],
            status="no_llm",
            degraded=[{"code": "no_llm", "message": "LLM 不可用"}],
        )
    if limit == 0:
        return SourceResult()

    try:
        examples = await generate_synthetic_examples(
            ctx.llm,
            ctx.draft_model or "mock/model",
            ctx.tables,
            samples=ctx.samples,
            stats=ctx.stats,
            lang=ctx.lang,
        )
    except Exception as e:  # noqa: BLE001 — 失败响亮,不吞成空结果
        logger.warning("evalset synthetic generation failed: %s", e)
        return SourceResult(
            [],
            status="error",
            degraded=[{"code": "synthetic_failed", "message": str(e)}],
        )

    kept = await validate_examples(examples or [], ctx.registry, ctx.datasource)
    items: list[GeneratedItem] = []
    for ex in kept:
        question = str(ex.get("question") or "").strip()
        sql = str(ex.get("sql") or "").strip()
        if not question or not sql:
            continue
        items.append(GeneratedItem(
            question=question,
            reference_sql=sql,
            source=SOURCE_SYNTHETIC,
            reference_origin=ORIGIN_LLM,
            generator=GENERATOR_SYNTHETIC,
            tags={"pattern": [str(t) for t in (ex.get("tags") or [])]},
            created_by=CREATED_BY_SYSTEM,
        ))

    truncated = items[:limit]
    degraded: list[dict[str, Any]] = []
    if not truncated:
        degraded.append({"code": "synthetic_empty"})
    return SourceResult(
        items=truncated,
        skipped=len(items) - len(truncated),
        degraded=degraded,
        status="ok",
    )


__all__ = [
    "CREATED_BY_SYSTEM",
    "GENERATOR_PRODUCTION",
    "GENERATOR_SYNTHETIC",
    "ORIGIN_LLM",
    "ORIGIN_PIPELINE",
    "SOURCE_PRODUCTION",
    "SOURCE_SYNTHETIC",
    "collect_production",
    "generate_synthetic",
]
