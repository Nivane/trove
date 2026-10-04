"""Attribution node — 分析服务的图内适配器(薄壳)。

确定分解数学与多跳编排已升格为 ``trove/services/analysis/``
(``decompose`` / ``expr_tree`` / ``engine`` / ``render``,三个调用方:
图内节点、前端分析卡、未来的 agentic 循环与决策 what-if)。本模块只剩:

  1. 状态解包(state.attribution_plan / time_context / matched_tables)
     → ``AnalysisRequest``;
  2. 调 ``AnalysisEngine``(执行器 = connectors 的只读一跳);
  3. 叙事 LLM(唯一非确定性,ground 在确定性表上);
  4. 状态双写:``attribution``(兼容旧形状)+ ``analysis``(结构化出门)。

模块级 re-export(``_base_period`` / ``_contribution`` / ``_metric_ratio_parts``
/ ``_ratio_share`` / ``_shift_share`` / ``_waterfall_chart``)是
``tests/workflow/test_attribution.py`` 的 import 面 —— 搬迁期必须保留,
它们同时是"数学行为不变"的安全网。

Node shape: `make_attribution(llm, config, connectors, semantic_layer)
-> async def attribution(state) -> dict` 返回部分状态更新;归因未启用 /
无计划 / 无连接器 / 一跳未成 → 返回 {} 静默跳过,绝不阻断主链。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.core.periods import base_period as _base_period
from trove.llm.gateway import LLMGateway
from trove.prompts import render
from trove.prompts.skills import append_skill_block, render_skills
from trove.services.analysis.decompose import (
    breakdown_signal as _breakdown_signal,
    contribution as _contribution,
    num as _num,
    ratio_share as _ratio_share,
    shift_share as _shift_share,
)
from trove.services.analysis.engine import (
    AnalysisEngine,
    AnalysisLimits,
    AnalysisRequest,
    analysis_payload,
)
from trove.services.analysis.expr_tree import metric_ratio_parts as _metric_ratio_parts
from trove.services.analysis.render import (
    ratio_waterfall_chart as _ratio_waterfall_chart,
    waterfall_chart as _waterfall_chart,
)
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

MAX_ATTRIBUTION_ROWS = 50  # 归因表注入叙事 prompt 的行数上限

__all__ = [
    "MAX_ATTRIBUTION_ROWS",
    "make_attribution",
    "_base_period",
    "_breakdown_signal",
    "_contribution",
    "_metric_ratio_parts",
    "_num",
    "_ratio_share",
    "_shift_share",
    "_waterfall_chart",
]


async def _run_hop(
    connectors: Any,
    sql: str,
    datasource: str,
    timeout_s: float = 5.0,
) -> tuple[list[str], list[list[Any]]]:
    """只读执行一跳(复用 connectors.execute 的只读守卫);超时/失败抛错。"""
    result = await asyncio.wait_for(
        connectors.execute(sql, datasource or None),
        timeout=timeout_s,
    )
    return list(result.columns), list(result.rows)


def _limits_from_config(config: AgentConfig) -> AnalysisLimits:
    """AgentConfig → 引擎预算(attribution 老配置 + analysis 新配置)。"""
    attr = config.attribution
    an = getattr(config, "analysis", None)
    return AnalysisLimits(
        max_dimensions=attr.max_dimensions,
        max_hops=attr.max_hops,
        probe_dimensions=attr.probe_dimensions,
        ratio_decomposition=attr.ratio_decomposition,
        driver_tree=bool(getattr(an, "driver_tree", True)),
        max_components=int(getattr(an, "max_components", 4)),
        max_queries=int(getattr(an, "max_queries", 12)),
    )


#: 交互式假设轮的开场白(模板 ``scan/hypothesis`` 的 ``intro`` 槽)。
#: 扫描路径的默认开场讲「超出历史噪声带」,归因路径必须讲「变动分解」
#: —— 这个轮次的输入是归因表,不是块序列的噪声带。
_HYPOTHESIS_INTRO = {
    "zh": (
        "你是一名数据归因助手。下面是确定性归因刚刚算出的主要变动点"
        "(当期 vs 基期的按维度分解;contribution 是该维度贡献的变动量)。"
    ),
    "en": (
        "You are a data-attribution analyst. Below are the main movers a "
        "deterministic attribution just computed (current vs baseline, broken "
        "down by dimension; contribution is that dimension's contribution to "
        "the total change)."
    ),
}


def _hypothesis_windows(
    time_context: str, baseline: str,
) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    """(当期窗口, 基期窗口)。share 基线 / time_context 解析不了 → (None, None)。

    走**同一个** ``base_period`` —— 假设验证用的基期与归因表 base 列必须
    是同一个跨度,否则裁决的就是另一道题。
    """
    if baseline == "share":
        return None, None
    periods = _base_period(time_context or "", baseline)
    if periods is None:
        return None, None
    return periods


def _skipped_hypotheses(reason: str) -> dict[str, Any]:
    """跳过 → **可见的一行**(与「跑了但一条都没产出」区分得开)。"""
    return {
        "status": "skipped",
        "reason": reason,
        "verified": [],
        "rejected": [],
        "queries": 0,
    }


async def _hypothesis_round(
    *,
    llm: Any,
    semantic_layer: Any,
    runner: Callable[[str, str], Awaitable[tuple[list[str], list[list[Any]]]]],
    datasource: str,
    dialect: str,
    lang: str,
    model: str,
    intro: str,
    data: str,
    limit: int,
    max_queries: int,
    window: tuple[str, str],
    base_window: tuple[str, str],
) -> dict[str, Any]:
    """一轮 propose → verify(编排薄壳;起草/解析/裁决全在 ``scan.hypotheses``)。"""
    from trove.services.analysis.budget import QueryLedger
    from trove.services.scan import hypotheses as hyp

    ledger = QueryLedger(total=max(2, int(max_queries)))
    evidence: list[dict[str, Any]] = []
    accepted, rejected = await hyp.propose(
        llm, semantic_layer=semantic_layer, data=data, lang=lang,
        limit=int(limit), model=model, intro=intro,
    )
    verified = await hyp.verify(
        accepted, semantic_layer=semantic_layer, runner=runner,
        datasource=datasource, dialect=dialect,
        window=window, base_window=base_window,
        ledger=ledger, evidence=evidence,
    )
    return {
        "status": "ran",
        "verified": [v.to_dict() for v in verified],
        "rejected": rejected,
        "window": list(window),
        "base_window": list(base_window),
        "queries": ledger.used,
        "evidence": evidence,
    }


def make_attribution(
    llm: LLMGateway,
    config: AgentConfig,
    connectors=None,
    semantic_layer=None,
    skills: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the attribution node bound to services.

    Args:
        llm: LLM gateway (narrative generation; grounded in the table).
        config: AgentConfig (attribution.enabled / max_hops / node model).
        connectors: ConnectorRegistry used to run hop queries (None → skip).
        semantic_layer: Live semantic provider (metrics/dimensions/time).
        skills: Optional ``SkillService`` — confirmed org methodology skills
            matching this node join the narrative system prompt. 方法论
            写死在服务层,而选 shift-share 还是贡献度分解**是口径不是
            算法**:代码提供方法,组织决定什么时候用哪个。
    """

    async def attribution(state: WorkflowState) -> dict[str, Any]:
        if state.error or not state.attribution_plan:
            return {}
        if not config.attribution.enabled:
            return {}
        if connectors is None or semantic_layer is None:
            return {}
        if not state.matched_tables:
            return {}

        plan = state.attribution_plan
        metric_name = str(plan.get("target_metric") or "").strip()
        dims = [
            str(d).strip()
            for d in (plan.get("dimensions") or [])
            if str(d or "").strip()
        ][: config.attribution.max_dimensions]
        if not metric_name or not dims:
            return {}
        baseline = str(plan.get("baseline") or "prev_period").strip().lower()
        if baseline not in ("prev_period", "yoy", "share"):
            baseline = "prev_period"
        depth = min(int(plan.get("depth") or 1), config.attribution.max_hops)

        request = AnalysisRequest(
            question=state.question,
            lang=state.lang,
            datasource=state.datasource or "",
            dialect=state.dialect or "sqlite",
            matched=list(state.matched_tables),
            metric=metric_name,
            dimensions=dims,
            baseline=baseline,
            depth=depth,
            focus=plan.get("focus"),
            time_context=state.time_context,
        )

        async def _runner(sql: str, datasource: str) -> tuple[list[str], list[list[Any]]]:
            return await _run_hop(connectors, sql, datasource)

        engine = AnalysisEngine(semantic_layer, _runner, _limits_from_config(config))
        outcome = await engine.run(request)
        if outcome is None:
            return {}

        # 异常中途降级(表未产出):保持旧形状原样(表空、只有 hops)。
        if outcome.degraded_result:
            result = {
                "table": [],
                "hops": outcome.hops,
                "narrative": "",
                "baseline": outcome.baseline,
            }
            return {"attribution": result, "attribution_hops": outcome.hops}

        table = outcome.table
        effects = outcome.effects
        is_ratio = outcome.is_ratio
        metric_name = outcome.metric
        primary_dim = outcome.primary_dim
        total_delta = outcome.total_delta
        base_total = outcome.base_total
        cur_total = outcome.cur_total

        zh = state.lang == "zh"
        baseline_label = {
            "prev_period": "环比" if zh else "previous period",
            "yoy": "同比" if zh else "year-over-year",
            "share": "占比" if zh else "share",
        }[outcome.baseline]

        # 归因叙事(LLM,ground 在归因表;走 node_models["attribution"]
        # 覆盖,缺省回落 model_for → model_fast,与 insights 一致)。
        # 比率指标:表格列带率/权重/三效应,并注入分解汇总。
        narrative = ""
        if is_ratio:
            if effects is not None:
                # shift-share:贡献 = 对整体率的绝对贡献(点),与 delta 同量纲
                cols = (
                    "dim / base_rate / current_rate / base_weight / "
                    "current_weight / within / composition / interaction / contribution"
                )
                table_text = "\n".join(
                    f"{it['dim']}\t{it['base_rate']:g}\t{it['current_rate']:g}\t"
                    f"{it['base_weight']:.1%}\t{it['current_weight']:.1%}\t"
                    f"{it['within']:g}\t{it['composition']:g}\t{it['interaction']:g}\t"
                    f"{it['contribution']:g}"
                    for it in table[:MAX_ATTRIBUTION_ROWS]
                )
            else:
                # share 基线:贡献 = 分子占比(无基期,无率变化可拆)
                cols = "dim / current_rate / current_weight / contribution"
                table_text = "\n".join(
                    f"{it['dim']}\t{it['current_rate']:g}\t{it['current_weight']:.1%}\t"
                    f"{it['contribution']:+.1%}"
                    for it in table[:MAX_ATTRIBUTION_ROWS]
                )
        else:
            cols = "dim / base / current / delta / contribution"
            table_text = "\n".join(
                f"{it['dim']}\t{it['base']:g}\t{it['current']:g}\t{it['delta']:g}\t{it['contribution']:+.1%}"
                for it in table[:MAX_ATTRIBUTION_ROWS]
            )

        # 交互式假设轮(可选,上限 N):确定性分析已产出表 → LLM 起草
        # 可证伪假设 → 确定性裁决(每条恰一行四态)。LLM 调用**发生在这
        # 里**(workflow 节点层)—— ``analysis_payload`` 本体零 LLM,假设
        # 节只是节点层附加的附录;起草/裁决失败只降级附录,绝不阻断主链。
        # 跳过也写成可见的一行(status=skipped + reason),与「跑了但一条
        # 都没产出」区分得开。
        hypotheses_section: dict[str, Any] | None = None
        if (
            config.scan.interactive_hypotheses
            and config.scan.max_hypotheses > 0
            and llm is not None
            and table_text
        ):
            cur_window, base_window = _hypothesis_windows(
                state.time_context or "", outcome.baseline)
            if base_window is None:
                hypotheses_section = _skipped_hypotheses(
                    "share_baseline" if outcome.baseline == "share"
                    else "no_time_window")
            else:
                try:
                    hypotheses_section = await _hypothesis_round(
                        llm=llm,
                        semantic_layer=semantic_layer,
                        runner=_runner,
                        datasource=state.datasource or "",
                        dialect=state.dialect or "sqlite",
                        lang=state.lang,
                        model=config.model_for_node("attribution", state.complexity),
                        intro=_HYPOTHESIS_INTRO.get(
                            state.lang, _HYPOTHESIS_INTRO["en"]),
                        data="\n".join(x for x in (
                            f"metric: {metric_name}",
                            f"dimension: {primary_dim}",
                            f"baseline: {baseline_label} "
                            f"({base_window[0]} ~ {base_window[1]})",
                            f"columns: {cols}",
                            table_text,
                        ) if x),
                        limit=config.scan.max_hypotheses,
                        max_queries=config.scan.max_queries,
                        window=cur_window,
                        base_window=base_window,
                    )
                except Exception as e:  # noqa: BLE001 — 假设是附录,不拖垮主链
                    logger.warning(
                        "Attribution hypothesis round failed (%s); skipping", e)
                    hypotheses_section = _skipped_hypotheses(
                        f"failed: {str(e)[:160]}")

        if llm is not None and table_text:
            model = config.model_for_node("attribution", state.complexity)
            try:
                prompt_kwargs = dict(
                    question=state.question,
                    metric=metric_name,
                    dimension=primary_dim,
                    baseline=baseline_label,
                    total_delta=total_delta,
                    table=table_text,
                )
                if is_ratio:
                    prompt_kwargs["effects"] = effects
                # ratio + share 基线(无率变化可拆)走普通归因提示词
                prompt_name = (
                    "attribution/ratio_user"
                    if (is_ratio and effects is not None)
                    else "attribution/user"
                )
                response = await llm.chat(
                    model=model,
                    messages=[
                        # 原地改,不是插入:这是这条 system 消息的唯一来源。
                        {"role": "system", "content": append_skill_block(
                            render("attribution/system", lang=state.lang),
                            skills.render_skills("attribution", **state.skill_ctx())
                            if skills is not None
                            else render_skills("attribution", **state.skill_ctx()),
                        )},
                        {"role": "user", "content": render(
                            prompt_name,
                            lang=state.lang,
                            **prompt_kwargs,
                        )},
                    ],
                    max_tokens=16000,
                    metadata={
                        "node": "attribution",
                        "session_id": state.session_id,
                        "run_id": state.run_id,
                        "question": state.question[:80],
                    },
                )
                narrative = (response or "").strip()
            except Exception as e:
                logger.warning("Attribution narrative failed (%s); skipping", e)

        baseline_period_label = {
            "prev_period": "基期" if zh else "Base period",
            "yoy": "去年同期" if zh else "Same period last year",
            "share": "本期" if zh else "Current period",
        }[outcome.baseline]
        if is_ratio and effects is not None:
            chart = _ratio_waterfall_chart(
                state.question, state.lang, base_total, effects, cur_total,
            )
        else:
            chart = _waterfall_chart(
                state.question, baseline_period_label, base_total, cur_total,
                table, state.lang,
            )

        result = {
            "total_delta": total_delta,
            "table": table,
            "narrative": narrative,
            "hops": outcome.hops,
            "dimensions": outcome.dimensions,
            "baseline": outcome.baseline,
            "chart": chart,
            "metric": metric_name,
            "kind": "ratio" if is_ratio else "additive",
        }
        if effects is not None:
            result["effects"] = effects
        # 下钻表并入(有则挂到 result,供前端分析面板/后续洞察复用)
        if outcome.drilldown:
            result["drilldown"] = outcome.drilldown

        payload = analysis_payload(
            outcome,
            question=state.question,
            chart=chart,
            baseline_label=baseline_period_label,
            datasource=state.datasource or "",
        )
        if hypotheses_section is not None:
            # 节点层附录:LLM 在节点层调用,分析包本体(analysis_payload)
            # 保持零 LLM —— 回放/缓存拿到的是确定性骨架 + 假设裁决记录。
            payload["hypotheses"] = hypotheses_section
        return {
            "attribution": result,
            "attribution_hops": outcome.hops,
            "analysis": payload,
        }

    return attribution
