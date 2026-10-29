"""Gen chain: retrieval → assembly → generation for the query pipeline.

Extracted from ``graphs.py`` (change axis: generation strategy). The three
top-level nodes — ``gen_retrieve`` / ``gen_assemble`` / ``gen_generate`` — the
candidate pool, and the classic ``generate → validate`` degradation subgraph
live here; graph topology, routers and builders deliberately stay in
``graphs.py``. External import paths remain stable because ``graphs.py``
re-exports this module's names.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.llm.agent_loop import run_agent_loop
from trove.llm.token_calibration import factor as token_calibration_factor
from trove.llm.token_calibration import record as record_token_calibration
from trove.prompts import render
from trove.prompts.skills import append_skill_block
from trove.services.authz.egress import egress_entry, merge_egress_reports
from trove.tracing.assembly import (
    block_entry,
    lazy_tool_names,
    tools_from_registry,
)
from trove.workflow.complexity import grade_complexity
from trove.workflow.context_budget import (
    ContextItem,
    assemble_context,
    count_tokens,
    ref_label,
)
from trove.workflow.context_score import history_items, relevance_score
from trove.workflow.graph_services import (
    GraphServices,
    _build_authorizer,
    _egress_redactor,
)
from trove.workflow.nodes.analyze_error import render_reasoning_context
from trove.workflow.nodes.gen_sql import (
    build_sql_prompt_from_state,
    make_generate,
    make_validate,
    render_cache_prefix,
    render_entities,
    render_episodes,
    render_lessons,
    render_metrics,
    render_profile,
    render_rules,
    render_shots,
    render_terms,
    render_user_facts,
)
from trove.workflow.schema_budget import trim_schema
from trove.workflow.state import GenSQLState, WorkflowState

logger = get_logger(__name__)

# 复杂度档位预算：simple 瘦身 / standard 默认 / complex 放开。
# 修正轮强制 standard（见 grade_complexity），档位只作用于首轮生成。
COMPLEXITY_BUDGET_TOKENS = {"simple": 1500, "standard": 2500, "complex": 4000}
# Schema 段（gen prompt 的稳定缓存前缀）自身的 token 上限：大库宽表下
# 避免 schema 全量注入挤占窗口；被裁的表经 lookup_schema 工具懒加载。
SCHEMA_BUDGET_TOKENS = {"simple": 900, "standard": 1800, "complex": 3500}


def _tier_budget(
    overrides: dict[str, int] | None,
    defaults: dict[str, int],
    tier: str,
) -> int:
    """复杂度档位预算：config 覆盖优先（未配置该档回落默认），否则默认常量。

    overrides 来自 AgentConfig.context_budget_tokens / schema_budget_tokens
    （可空），让 200k/1m 窗口下按模型窗口比例放大预算，而不是写死固定档位。
    """
    if overrides:
        value = overrides.get(tier)
        if value is not None and value > 0:
            return int(value)
    return defaults.get(tier, defaults["standard"])


def generation_temperature(retry_count: int) -> float:
    """修正轮升温：温度 0 的确定性生成会让重试产出相同 SQL。"""
    return min(0.3, retry_count * 0.1)


def _word_overlap(q1: str, q2: str) -> float:
    """两个问题的词重叠率(min 侧归一):KB 示例精确命中的判定依据。"""
    w1 = set(re.findall(r"[a-z0-9_]+", (q1 or "").lower()))
    w2 = set(re.findall(r"[a-z0-9_]+", (q2 or "").lower()))
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / min(len(w1), len(w2))


KB_EXACT_OVERLAP = 0.95  # KB 示例与问题几乎逐词一致 → 直接用示例 SQL
DEFAULT_GEN_SQL_RETRIES = 3


def _rotate_few_shots(sub_state: GenSQLState, offset: int) -> GenSQLState:
    """按候选索引轮换 few-shot 首条目(候选多样性:示例锚点各异)。

    各候选温度固定之外,轮换参考示例的出场顺序——模型通常以第一个
    示例为模式锚点,轮换 = 不同候选采纳不同示例的口径/风格,投票才有
    真正的分歧可统计(所有候选共享同一组示例会趋同)。少于此两条示例
    或未注入(few_shots 为 None)时原样返回。
    """
    shots = sub_state.few_shots or []
    if len(shots) < 2:
        return sub_state
    off = offset % len(shots)
    return sub_state.model_copy(update={
        "few_shots": shots[off:] + shots[:off],
    })


def _candidate_schedule(n_alt: int) -> list[tuple[float, str]]:
    """去相关候选配置:温度 + 风格提示,确定性生成。

    scaling=5 时精确保持历史 4 个温度子图(0.3/0.5/0.7/1.0,无风格
    提示)——既有行为逐字节不变。更大规模(n=49/199):温度在 [0.2, 1.2]
    均匀铺开 + 风格提示轮换,让大候选池覆盖不同的正确解释表述,
    而不是温度差太小导致几乎同构的重复样本(select 只会按 SQL 归一
    文本去重,重复候选取不到额外的票)。

    Returns:
        [(temperature, style_mode), ...] 每候选一个配置。
    """
    if n_alt == 4:
        return [(float(t), "") for t in (0.3, 0.5, 0.7, 1.0)]
    modes = ["", "cte", "explicit-join", "subquery"]
    out: list[tuple[float, str]] = []
    for i in range(n_alt):
        frac = (i + 0.5) / n_alt
        temp = round(0.2 + 1.0 * frac, 3)
        out.append((temp, modes[i % len(modes)]))
    return out


_STYLE_HINTS = {
    "cte": "Prefer a WITH (CTE) formulation where it makes the query clearer.",
    "explicit-join": "Prefer explicit JOIN ... ON ... syntax over comma joins.",
    "subquery": "Prefer a single statement with subqueries where possible.",
}


def _payload_sql(raw: str, dialect: str) -> str:
    """收尾文本/工具载荷里的 SQL 必须过解析门才允许进 state.sql。

    0479 实证:agentic 收尾 payload 是散文,``extract_sql`` 的最后手段原样
    回吐,散文被当 SQL 接受、直到执行层 authz 才拦(ERR:AUTHZ_UNRESOLVED
    could not be parsed)。这里把门槛前移:sqlglot 解析不过(非单条
    SELECT/WITH/UNION 查询)→ 返回空串,调用方走经典降级/重试,而不是把
    不可执行文本当产物交付。校验器自身异常不阻断(与 validate_sql 的
    导入缺失兜底同哲学)。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        from trove.workflow.nodes.gen_sql import validate_sql

        ok, _errors = validate_sql(text, dialect)
    except Exception as e:
        # The parser is the gate that keeps prose out of state.sql; when it
        # itself fails we keep the old permissive behaviour, but never
        # silently — a bypassed SQL gate must be visible in the run log.
        logger.warning(
            "SQL parser unavailable; passing finish payload through "
            "unvalidated: %s", e,
        )
        return text
    return text if ok else ""


async def _run_candidate_subagent(
    services,
    state: WorkflowState,
    sub_state: GenSQLState,
    dialect: str,
    *,
    temperature: float,
    mode: str = "",
    rotation: int = 1,
    probe_cache: dict | None = None,
    budget: Any = None,
    profiles: Any = None,
    egress: Any = None,
    egress_sink: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    """独立 subagent 并行打磨单个备选候选("主 agent 规划 + subagent 执行")。

    每个备选 = 一个独立的 ReAct loop(自建工具注册表、自定终止),以去相关
    温度 + few-shot 轮换 + 风格提示生成候选 SQL,供 select 共识投票。
    失败/空手/护栏降级返回 None——静默跳过,保持经典候选路径的容错语义。

    ``egress`` 是出境脱敏口(候选的 probe 观测同样要脱敏);``egress_sink``
    由调用方持有,本函数的注册表汇总并入其中 —— 候选各有自己的注册表,
    不汇入就只审计到主候选的出境。
    """
    from trove.workflow.nodes.gen_sql import build_sql_registry, extract_sql

    rotated = _rotate_few_shots(sub_state, rotation)
    registry = build_sql_registry(
        services.connectors, rotated.question, rotated.lang, dialect,
        matched_tables=state.matched_tables or None,
        datasource=state.datasource,
        complexity=state.complexity,
        roles=state.tool_roles or None,
        user_id=state.user_id,
        run_id=state.run_id,  # 与主 agent 同一作用域:同运行的观测可互相复用
        probe_cache=probe_cache,
        budget=budget,
        profiles=profiles,
        semantic_provider=services.semantic_layer,
        egress=egress,
        # 候选的 probe/check/search 也是用户可见取数路径:同一道执行门与
        # 同一份行/列 scope(与主 agent 注册表同参,不吃"候选更宽松"的亏)。
        authorizer=_build_authorizer(services),
        principal=state.principal,
    )
    prompt = build_sql_prompt_from_state(rotated)
    # ① Prompt caching:与主路径同策略——前缀校验在 agent_loop 内部,
    # 这里只按配置门控。
    cache_prefix = (
        render_cache_prefix(dialect, rotated.schema_context)
        if (services.config or AgentConfig()).prompt_caching
        else None
    )
    hint = _STYLE_HINTS.get(mode)
    if hint:
        prompt = f"{prompt}\n\n{hint}"
    # 每节点覆盖优先(node_models.gen_sql_subagent / gen_sql),再复杂度分档
    model = (
        services.config.model_for_node("gen_sql_subagent", rotated.complexity)
        if services.config else "openai/gpt-4o"
    )
    try:
        result = await run_agent_loop(
            services.llm, model,
            system=render(
                "gen_sql/system",
                lang=rotated.lang,
                has_probe=services.connectors is not None,
                full_rules=state.complexity != "simple",
            ),
            user=prompt,
            cache_prefix=cache_prefix,
            registry=registry,
            tool_timeout_s=20.0,
            time_budget_s=120.0,
            # ④ 循环内压缩兜底:max_rounds 4→8——早期轮已转确定性摘要
            # ([compacted] 行),不再有上下文复利爆炸,护栏可以放宽
            max_rounds=8,
            max_total_tokens=2500,
            # 软轮预算(配置默认关;conf/agent.yml 写 5):子代理同主路径
            soft_rounds_budget=(
                services.config.gen_sql_soft_rounds if services.config else 0
            ),
            metadata={
                "node": "gen_sql_subagent",
                "session_id": state.session_id,
                "run_id": state.run_id,
            },
            temperature=temperature,
        )
    except Exception:
        if egress_sink is not None and registry.egress_report:
            egress_sink.update(
                merge_egress_reports(egress_sink, registry.egress_report)
            )
        return None
    if egress_sink is not None and registry.egress_report:
        egress_sink.update(
            merge_egress_reports(egress_sink, registry.egress_report)
        )
    sql = _payload_sql(
        extract_sql(result.get("content", "")) if result.get("content") else "",
        dialect,
    )
    if not sql:
        # 模型可能在工具里给出 SQL(validate/probe/check/finish 过都算)。
        # 每个候选同样过解析门:不可解析的载荷不进投票池——坏候选只会让
        # select 共识投票在垃圾上收敛。
        for entry in reversed(result.get("tool_history") or []):
            args = entry.get("arguments") or {}
            payload = args.get("sql") or args.get("answer")
            if entry.get("name") in ("validate_sql", "probe_query", "check_result", "finish") and payload:
                sql = _payload_sql(payload, dialect)
                if sql:
                    break
    return sql or None


# ── gen_sql subgraph ─────────────────────────────────────


def build_gen_sql_subgraph(
    services: GraphServices,
    max_retries: int = DEFAULT_GEN_SQL_RETRIES,
    temperature: float = 0.0,
    mode: str = "",
) -> CompiledStateGraph:
    """Build the gen_sql subgraph: generate → validate retry loop.

    Args:
        temperature: Sampling temperature (alternative candidates use
            a higher value for diversity).
        mode: Optional de-correlation style hint ("cte"/"explicit-join"/
            "subquery") appended to the generation prompt — keeps large
            candidate pools from converging on identical formulations.
    """
    config = services.config or AgentConfig()

    def route_after_validate(state: GenSQLState) -> str:
        if state.error or not state.validation_errors:
            return END
        return "generate"

    g = StateGraph(GenSQLState)
    g.add_node("generate", make_generate(services.llm, config, temperature=temperature, mode=mode))
    g.add_node("validate", make_validate(max_retries=max_retries))
    g.add_edge(START, "generate")
    g.add_edge("generate", "validate")
    g.add_conditional_edges(
        "validate",
        route_after_validate,
        {"generate": "generate", END: END},
    )
    return g.compile()


# ── Main graph nodes ─────────────────────────────────────


def make_gen_retrieve(services: GraphServices):
    """gen_retrieve — 检索与信号采集(零生成)。

    dialect 探测 + 复杂度分级 + KB 六路检索(示例/规则/教训/术语/指标/实体)
    + 实时语义层 term 合并 + 用户事实/情景记忆/失败画像 + KB 精确命中判定。
    全部产物落 gen_ctx(纯 JSON,checkpoint 安全),由 gen_assemble 消费。
    """

    async def gen_retrieve(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}

        # Detect dialect from the active datasource adapter
        dialect = state.dialect
        if services.connectors:
            try:
                adapter = await services.connectors.get(state.datasource or None)
                dialect = adapter.dialect()
            except Exception:
                pass

        # Knowledge base: reference examples + terminology for the prompt.
        # Scoped to the active datasource; without a datasource context the
        # KB is not consulted.
        datasource = ""
        if services.connectors is not None:
            datasource = state.datasource or services.connectors.default_name or ""

        few_shots: list[dict[str, Any]] = []
        term_notes: list[dict[str, Any]] = []
        lessons: list[dict[str, Any]] = []
        rules: list[str] = []
        metric_items: list[dict[str, Any]] = []
        entity_items: list[dict[str, Any]] = []
        example_hits: list[dict[str, Any]] = []
        matched = list(state.matched_tables or [])
        # 复杂度分级(纯函数,零 IO/LLM):结构信号(plan) + 语义信号(锚定表)。
        # 修正轮强制 standard,保证修正循环行为不变;simple 用于跳过多候选等。
        in_correction = bool(
            state.error_feedback or state.error_analysis or state.reason
        )
        complexity = "standard" if in_correction else grade_complexity(
            state.plan_json, matched,
            term_hit=bool(state.intent_evidence.get("term_hit")),
            kb_hit=bool(state.kb_hits),
            plan_validation=state.plan_validation,
        )
        all_table_names: list[str] = []
        if services.catalog is not None and matched:
            try:
                all_table_names = [
                    t["name"] for t in await services.catalog.list_tables(datasource)
                ]
            except Exception:
                all_table_names = []
        if services.kb is not None and datasource:
            # ensure_synced 必须领先所有读(mtime 检查/懒加载);其后的四个
            # KB 调用互相独立(各自开自己的 aiosqlite 连接)→ asyncio.gather
            # 并行化。失败语义不变:KB 异常原本未捕获直接中止节点,
            # 现在按原调用顺序 re-raise 第一个异常。
            await services.kb.ensure_synced(default_datasource=datasource)
            # limit=5:检索注入 5 个示例(实测 3 个时注入列覆盖仅 28%,
            # 5 个 → 42%,token 成本 ~+60,预算(2500)完全装得下)
            _kb_results = await asyncio.gather(
                services.kb.search_examples(
                    state.question, datasource, limit=5,
                    tables=matched or None, all_tables=all_table_names or None,
                    # 多表锚定:每表分组 top 再合并,避免单表模板挤占
                    per_table=bool(matched),
                ),
                services.kb.list_rules(datasource),
                # Hint Bank 语义检索:embedding 相似度 × 投票加权 × 时效衰减,
                # 表锚过滤在检索内完成;不再用纯子串过滤(近义改写也能命中)
                services.kb.search_lessons(
                    state.question, datasource, limit=3,
                    tables=matched or None, all_tables=all_table_names or None,
                ),
                services.kb.search_terms(
                    state.question, datasource,
                    tables=matched or None, all_tables=all_table_names or None,
                ),
                # typed corpus 检索:指标(表达式+口径)与维度/枚举实体(值确认)
                services.kb.search_metrics(
                    state.question, datasource,
                    tables=matched or None, all_tables=all_table_names or None,
                    limit=4,
                ),
                services.kb.search_entities(
                    state.question, datasource,
                    tables=matched or None, all_tables=all_table_names or None,
                    limit=8,
                ),
                return_exceptions=True,
            )
            for r in _kb_results[1:]:  # 首个异常(原顺序)→ 中止节点,同旧语义
                if isinstance(r, BaseException):
                    raise r
            raw_examples, rules, lessons, term_hits, metric_hits, entity_hits = _kb_results

            # 模板参数化(A1):template 且 SQL 含 {{var}} 时,确定性静态分析
            # 出参数类型/声明列/枚举样例值,注入 few-shot(LLM 据此填真实值,
            # 不把 {{var}} 当字面量)。零 LLM,失败静默降级为普通模板。
            _semantic_model = None
            if services.semantic_layer is not None:
                try:
                    _semantic_model = services.semantic_layer.model()
                except Exception:
                    _semantic_model = None

            def _shot(h: Any) -> dict[str, Any]:
                shot: dict[str, Any] = {
                    "question": h.question, "sql": h.sql, "template": bool(h.template),
                }
                if h.template and "{{" in (h.sql or ""):
                    try:
                        from trove.services.kb.template_params import analyze_template

                        params = analyze_template(h.sql, _semantic_model)
                        if params:
                            shot["parameters"] = params
                    except Exception:
                        pass
                return shot

            few_shots = [_shot(h) for h in raw_examples]
            term_notes = [
                {"term": h.term, "mapping": h.mapping, "definition": h.definition}
                for h in term_hits
            ]
            example_hits = [
                {
                    "question": h.question, "sql": h.sql,
                    "tags": list(getattr(h, "tags", []) or []),
                    "template": bool(getattr(h, "template", False)),
                    "score": float(getattr(h, "score", 0) or 0),
                }
                for h in raw_examples
            ]
            metric_items = [
                {
                    "name": m.name, "expression": m.expression,
                    "definition": m.definition,
                    "score": float(getattr(m, "score", 0) or 0),
                }
                for m in metric_hits
            ]
            entity_items = [
                {
                    "field": e.field, "dataset": e.dataset, "role": e.role,
                    "enum_values": list(getattr(e, "enum_values", []) or []),
                    "enum_labels": list(getattr(e, "enum_labels", []) or []),
                    "score": float(getattr(e, "score", 0) or 0),
                }
                for e in entity_hits
            ]

        # 实时语义层 metric 作为 term 源(KB 优先,同名去重):
        # 与 KB term 同语义的名称/同义词匹配,表锚定由 provider 完成。
        # 不进 kb_exact_match 快捷通道(那是 KB 示例的专用路径)。
        if services.semantic_layer is not None and datasource:
            try:
                live_hits = services.semantic_layer.terms_for(
                    state.question, tables=matched or None,
                    all_tables=all_table_names or None,
                )
            except Exception as e:
                logger.warning("Semantic layer terms failed (%s): %s", datasource, e)
                live_hits = []
            seen = {t["term"] for t in term_notes}
            for h in live_hits:
                if h.term not in seen:
                    term_notes.append(
                        {"term": h.term, "mapping": h.mapping, "definition": h.definition})
                    seen.add(h.term)

        # 用户级记忆(用户+数据源作用域):检索该用户在当前数据源下的
        # 偏好/口径事实,按与问句的相关度取前 N 条注入 personalization
        # 上下文块。故障静默降级——记忆检索不得阻断生成(与 KB 同语义:
        # 检索失败不算生成失败)。
        user_fact_items: list[dict[str, Any]] = []
        if services.user_facts is not None and datasource and state.user_id:
            try:
                user_fact_items = await services.user_facts.search(
                    state.user_id, datasource, state.question, limit=3,
                )
            except Exception as e:
                logger.warning(
                    "User facts retrieval failed (%s): %s", datasource, e)

        # 情景记忆(episodic):该用户在当前数据源下过去对类似问题的
        # 查询结果(成功 SQL 是最强 few-shot 锚,失败 + 修正要点防重犯)。
        # 统一走 memory facade;未启用/无用户时为空,静默降级。
        episode_items: list[dict[str, Any]] = []
        memory_backend = ""
        if (
            services.memory is not None and datasource and state.user_id
            and services.memory.enabled
        ):
            try:
                from trove.services.memory.models import MemoryScope

                _eps = await services.memory.retrieve(
                    MemoryScope(datasource=datasource, user_id=state.user_id),
                    state.question,
                    kinds=["episode"],
                    limit=2,
                )
                episode_items = [
                    {"question": e.content.get("question", ""),
                     "sql": e.content.get("sql", ""),
                     "verdict": e.content.get("verdict", ""),
                     "correction_history": e.content.get("correction_history", []),
                     "score": e.score,
                     "channels": list(getattr(e, "channels", []) or [])}
                    for e in _eps
                ]
                # 情景记忆通道标记:任一条目走了混合(词面+向量) → hybrid
                _chans = {c for it in episode_items for c in it.get("channels", [])}
                if episode_items:
                    memory_backend = "hybrid" if "hybrid" in _chans else "lexical"
            except Exception as e:
                logger.warning(
                    "Episode memory retrieval failed (%s): %s", datasource, e)

        # ⑦ profile_boost 激活:用户×数据源失败画像(重复失败模式)作最低
        # 优先级提示块(priority 10,预算余量才进)。opt-in(memory.profile_boost);
        # 无失败模式 → 空串不注入(零噪音零成本)。facts 不入块:user_facts
        # 块(priority 2)已注入偏好,避免重复。best-effort:画像失败静默降级。
        profile_text = ""
        if (
            services.memory is not None and datasource and state.user_id
            and services.memory.enabled
            and getattr(getattr(services.memory, "config", None),
                        "profile_boost", False)
        ):
            try:
                _profile = await services.memory.profile(state.user_id, datasource)
                profile_text = render_profile(_profile)
            except Exception as e:
                logger.warning("Profile hint failed (%s): %s", datasource, e)

        # KB 精确命中:示例问题与当前问题几乎逐词一致 → 直接采用示例 SQL。
        # KB 保存的是该数据源的标准写法;对已收录的问题让模型"再解释一遍"
        # 只会产出歧义变体(实测:disp vs client 口径摇摆 5 轮)。SQL 仍会
        # 走 execute → validate → reflect,确定性规则与裁决不受影响。
        # 修正轮禁用快捷通道:KB 标准写法已被证明失败(执行/规则/裁决),
        # 再重发同一 SQL 只会空转烧预算——此时走生成,示例照常进 few_shots,
        # 模型基于错误反馈做定点修复。
        kb_exact_match: dict[str, Any] | None = None
        if not in_correction and example_hits:
            for h in example_hits:
                if _word_overlap(state.question, h["question"]) >= KB_EXACT_OVERLAP and h["sql"]:
                    kb_exact_match = {"question": h["question"], "sql": h["sql"]}
                    break

        gen_ctx: dict[str, Any] = {
            "dialect": dialect,
            "complexity": complexity,
            "in_correction": in_correction,
            "few_shots": few_shots,
            "term_notes": term_notes,
            "lessons": lessons,
            "rules": rules,
            "metrics": metric_items,
            "entities": entity_items,
            "user_facts": user_fact_items,
            "episodes": episode_items,
            "profile": profile_text,
            "examples": example_hits,
            "kb_exact_match": kb_exact_match,
        }
        update: dict[str, Any] = {
            "gen_ctx": gen_ctx,
            "complexity": complexity,
            # gen_sql 是权威生成路径:清掉陈旧快径标记,重生成的修正轮
            # 不享受快径的 reflect 跳过
            "fast_path": False,
        }
        # 情景记忆通道标记(best-effort,无 episodes 命中为空)
        if memory_backend:
            update["memory_backend"] = memory_backend
        # 检索来源标识:内置 SQLite FTS5 镜像 vs pg_hybrid 统一检索库
        # (仅作分析面板展示,不参与生成逻辑)。
        if services.kb is not None and datasource:
            try:
                update["retrieval_backend"] = services.kb.retrieval_backend_label(
                    datasource)
            except Exception:
                update["retrieval_backend"] = "builtin"
        return update

    return gen_retrieve


def make_gen_assemble(services: GraphServices):
    """gen_assemble — 预算裁剪与生成输入装配(零检索/零生成)。

    消费 gen_ctx:按复杂度分档预算组装 context 块(item 级裁剪)、schema
    预算化裁剪、构造 GenSQLState 并序列化为 gen_sub_state 供生成节点
    重建。plan 是规格而非补充材料,先预留额度再让其余块竞争。
    """

    async def gen_assemble(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        ctx = state.gen_ctx or {}
        if not ctx:
            return {}
        dialect = ctx["dialect"]
        complexity = ctx["complexity"]
        few_shots: list[dict[str, Any]] = ctx["few_shots"] or []
        # 出境治理:few-shot 的样例值在进入预算装配与 prompt 渲染**之前**
        # 脱敏。render_shots(token 估算)与 Jinja 模板读的是同一份列表 ——
        # 在这里替换一次,两处天然同源(分别脱敏会让估算与真实提示词不一致)。
        few_shots, shots_egress = _egress_redactor(services).redact_shots(few_shots)
        term_notes: list[dict[str, Any]] = ctx["term_notes"] or []
        lessons: list[dict[str, Any]] = ctx["lessons"] or []
        rules: list[str] = ctx["rules"] or []
        metric_items: list[dict[str, Any]] = ctx["metrics"] or []
        entity_items: list[dict[str, Any]] = ctx["entities"] or []
        user_fact_items: list[dict[str, Any]] = ctx["user_facts"] or []
        episode_items: list[dict[str, Any]] = ctx["episodes"] or []
        profile_text: str = ctx.get("profile") or ""
        example_hits: list[dict[str, Any]] = ctx["examples"] or []

        # Context budget: optional blocks filled by priority; within each
        # block, items are selected by relevance score, all capped at a
        # complexity-tiered token budget (simple 瘦身 / complex 放开).
        # Usage reported for observability (what the model actually saw).
        budget = _tier_budget(
            (services.config or AgentConfig()).context_budget_tokens,
            COMPLEXITY_BUDGET_TOKENS,
            complexity,
        )
        # ref = 装配 dump 的来源标识(示例问题/规则原文/术语名…);取不到的
        # 回退到 key(位置标识),由 assemble_context 的明细统一处理。
        optional_blocks: dict[str, list[ContextItem]] = {}
        if few_shots:
            # 每表/每条示例带 KB 相关度分数(score 降序=检索顺序)——预算
            # 内保留最相关条目,而非整块丢弃。
            optional_blocks["few_shots"] = [
                ContextItem(
                    key=f"shot{i}",
                    text=render_shots([s]),
                    score=float(h.get("score") or 0),
                    ref=ref_label(str(s.get("question", ""))),
                )
                for i, (h, s) in enumerate(zip(example_hits, few_shots))
            ]
        if rules:
            # rules/terms/lessons 无检索分数(0.0 = 纯检索序,裁剪无区分度)
            # ——用词重叠相关度补上,item 级裁剪才真正生效
            optional_blocks["rules"] = [
                ContextItem(
                    key=f"rule{i}",
                    text=render_rules([r]),
                    score=relevance_score(r, state.question),
                    ref=ref_label(r),
                )
                for i, r in enumerate(rules)
            ]
        if term_notes:
            optional_blocks["term_notes"] = [
                ContextItem(
                    key=f"term{i}",
                    text=render_terms([t]),
                    score=relevance_score(
                        " ".join([str(t.get("term", "")), str(t.get("mapping", "")),
                                  str(t.get("definition", ""))]),
                        state.question,
                    ),
                    ref=ref_label(str(t.get("term", ""))),
                )
                for i, t in enumerate(term_notes)
            ]
        if metric_items:
            optional_blocks["metrics"] = [
                ContextItem(
                    key=f"metric{i}",
                    text=render_metrics([m]),
                    score=float(m.get("score") or 0),
                    ref=ref_label(str(m.get("name", ""))),
                )
                for i, m in enumerate(metric_items)
            ]
        if entity_items:
            optional_blocks["entities"] = [
                ContextItem(
                    key=f"entity{i}",
                    text=render_entities([e]),
                    score=float(e.get("score") or 0),
                    ref=ref_label(
                        f"{e.get('dataset', '')}.{e.get('field', '')}".strip(".")),
                )
                for i, e in enumerate(entity_items)
            ]
        if lessons:
            optional_blocks["lessons"] = [
                ContextItem(
                    key=f"lesson{i}",
                    text=render_lessons([ln]),
                    score=float(ln.get("score") or relevance_score(
                        " ".join([str(ln.get("pattern", "")), str(ln.get("note", "")),
                                  str(ln.get("sql_snippet", ""))]),
                        state.question,
                    ) or 0),
                    ref=ref_label(str(ln.get("pattern") or ln.get("note") or "")),
                )
                for i, ln in enumerate(lessons)
            ]
        if user_fact_items:
            optional_blocks["user_facts"] = [
                ContextItem(
                    key=f"ufact{i}",
                    text=render_user_facts([f]),
                    score=relevance_score(str(f.get("fact", "")), state.question),
                    ref=ref_label(str(f.get("fact", ""))),
                )
                for i, f in enumerate(user_fact_items)
            ]
        if episode_items:
            # 情景记忆:跨会话过去查询(成功=few-shot 锚,失败+修正=反例)。
            # 优先级介于 lessons 与 plan 之间——作为经验信号而非硬约束。
            optional_blocks["episodes"] = [
                ContextItem(
                    key=f"ep{i}",
                    text=render_episodes([e]),
                    score=float(e.get("score") or 0),
                    ref=ref_label(str(e.get("question", ""))),
                )
                for i, e in enumerate(episode_items)
            ]
        if profile_text:
            # 失败画像:用户级而非问题级信号,score 0.0(中性);priority 10
            # 垫底——预算余量才进,不挤占任何问题相关的上下文。
            optional_blocks["profile"] = [
                ContextItem(key="profile", text=profile_text, score=0.0, ref="profile"),
            ]
        if state.history:
            # 历史拆成逐轮条目:score = 相关度(与问句词重叠) + 最近度——
            # 预算内保留最相关的轮次,而非整块全有全无
            optional_blocks["history"] = history_items(state.history, state.question)

        # 估算校准:实测/估算比例(按 model+dialect 的 EMA)放大单条成本,
        # 把系统性的低估反馈进预算,避免真实 prompt 超窗。冷启动 = 1.0。
        # 复杂度分档选模:simple/standard → model_fast,complex → target;
        # node_models.gen_sql 配了则覆盖(与生成段同键,估算校准也同基准)。
        model = (
            services.config.model_for_node("gen_sql", complexity)
            if services.config else "openai/gpt-4o"
        )
        cal = token_calibration_factor(model, dialect)

        def _count(text: str) -> int:
            n = count_tokens(text)
            return max(1, int(n * cal)) if cal > 0 else n

        # plan(含编译器契约块)是**规格而非补充材料**,不参与预算裁剪:
        # 先按实际成本预留额度,再让其余块在剩余预算里竞争。否则预算紧张时
        # 整个块被丢掉,而 execute_sql 仍按编译产物校验 —— gen_sql 可能
        # 从未见过计划却被判"偏离计划"。要裁就裁 KB 材料(few_shots/
        # lessons/episodes),它们才是补充信息。
        plan_block = state.plan or ""
        plan_cost = _count(plan_block) if plan_block else 0
        # detail=True:同一份装配结果顺带给出逐块逐项的 tokens/truncated
        # (装配 dump 的 blocks 节,不重算 token)。
        included, usage = assemble_context(
            optional_blocks,
            {"few_shots": 1, "user_facts": 2, "rules": 3, "term_notes": 4,
             "metrics": 5, "entities": 6, "lessons": 7, "episodes": 8,
             "history": 9, "profile": 10},
            max(0, budget - plan_cost),
            count=_count,
            detail=True,
        )
        # context_usage 保持**旧形状**(SSE/前端契约):逐项明细只进
        # assembly_blocks(run 级装配 dump),不随步骤事件扩散。
        context_usage = [
            {k: v for k, v in u.items() if k not in ("items", "truncated")}
            for u in usage
        ]
        assembly_blocks: list[dict[str, Any]] = [
            {"name": u["name"], "tokens": u["tokens"],
             "truncated": u["truncated"], "items": u["items"]}
            for u in usage
        ]
        if plan_block:
            included["plan"] = ["plan"]
            context_usage.append({
                "name": "plan", "tokens": plan_cost, "included": True,
                "items_total": 1, "items_included": 1,
            })
            assembly_blocks.append({
                "name": "plan", "tokens": plan_cost, "truncated": False,
                "items": [{"ref": "plan", "tokens": plan_cost, "truncated": False}],
            })
        # 按预算选中的 item key 过滤各源列表(保留检索顺序)
        def _trim(block: str, prefix: str, items: list[Any]) -> list[Any]:
            keys = set(included.get(block, ()))
            return [it for i, it in enumerate(items) if f"{prefix}{i}" in keys]

        few_shots = _trim("few_shots", "shot", few_shots)
        term_notes = _trim("term_notes", "term", term_notes)
        lessons = _trim("lessons", "lesson", lessons)
        rules = _trim("rules", "rule", rules)
        user_facts = _trim("user_facts", "ufact", user_fact_items)
        episodes = _trim("episodes", "ep", episode_items)
        profile = profile_text if "profile" in included else ""
        metrics = _trim("metrics", "metric", metric_items) if metric_items else None
        entities = _trim("entities", "entity", entity_items) if entity_items else None
        # history 是逐轮条目:按预算保留的轮次重拼回字符串注入
        history_trimmed = ""
        if "history" in included:
            kept_texts = [
                it.text for it in optional_blocks["history"]
                if it.key in set(included["history"])
            ]
            history_trimmed = "\n".join(kept_texts)

        # Schema 预算化:表块按信号裁剪到复杂度分档的 schema 上限,尾部
        # 段(值提示/语义)始终保留;被裁的表经 lookup_schema 工具懒加载。
        schema_budget = _tier_budget(
            (services.config or AgentConfig()).schema_budget_tokens,
            SCHEMA_BUDGET_TOKENS,
            complexity,
        )
        # 被裁掉的表经 lookup_schema 工具懒加载——裁剪提示引用该工具,
        # 让模型在需要时取回完整 DDL(agent 始终可触达物理 schema)。
        schema_for_gen = trim_schema(
            state.schema_context, schema_budget, state.question, state.plan,
        )

        sub_state = GenSQLState.from_workflow(
            state,
            dialect=dialect,
            included=set(included.keys()),
            reasoning_context=render_reasoning_context(
                state.reasoning_history, ("gen_sql",),
            ),
            few_shots=few_shots,
            term_notes=term_notes,
            lessons=lessons,
            rules=rules,
            user_facts=user_facts,
            episodes=episodes,
            metrics=metrics,
            profile=profile,
            entities=entities,
            history=history_trimmed,
            schema_context=schema_for_gen,
        )
        # 稳定可缓存前缀(dialect+schema)的 token 数——prompt caching 观测:
        # 同一数据源+方言下跨调用字节级一致,值越大缓存复用空间越大。
        # count_tokens 真实分词(与 agent_loop 首轮实测 prompt_tokens 同口径,
        # 字符估算会系统性低估多字节语言),前缀文本同时复用为缓存断点。
        cache_prefix_text = render_cache_prefix(dialect, schema_for_gen)
        cache_prefix_tokens = count_tokens(cache_prefix_text)
        update: dict[str, Any] = {
            "gen_sub_state": sub_state.model_dump(),
            "context_usage": context_usage,
            "cache_prefix_tokens": cache_prefix_tokens,
            # 装配 dump 的 blocks 节(gen_generate 再追加 skill_injections)
            "assembly_blocks": assembly_blocks,
        }
        if shots_egress:
            # 样例值脱敏的汇总是 run 级累积的一部分(gen_generate 再并入
            # 工具观测的报告)。只写非空,避免给未声明部署塞一个空键。
            update["egress_report"] = merge_egress_reports(
                state.egress_report, egress_entry(shots_egress),
            )
        return update

    return gen_assemble


def make_gen_generate(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    subgraph_alt: CompiledStateGraph | None = None,
    alt_subgraphs: list[CompiledStateGraph] | None = None,
    agentic: bool = True,
    budget: Any = None,
    profiles: Any = None,
):
    """gen_generate — SQL 生成与候选共识池装配。

    生成前问价(执行画像 §7.2)需要 ①:``budget``/``profiles`` 非空才给模型挂
    ``describe_cost``(未装配 = 没这道门,而不是判过了)。

    消费 gen_retrieve/gen_assemble 的产物(gen_ctx / gen_sub_state):
    - KB 精确命中 → 直接采用标准 SQL,跳过模型生成;
    - agentic 默认:ReAct loop 模型自校验并自定终止,护栏/空手降级到
      经典 generate→validate 子图;
    - multi-candidate:extra generations at higher temperatures stored in
      state.candidates for the consensus select node's execution voting。
    """
    # 跨修正轮的 probe/check 结果缓存:闭包持有 → 同一次运行的多轮修正共享
    # (registry 每轮重建,缓存独立于 registry 存活)。TTL 内同一数据源同一
    # SQL 不重复执行;容量封顶防无限膨胀。
    probe_cache: dict[Any, Any] = {}

    async def gen_generate(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        ctx = state.gen_ctx or {}
        if not ctx or not state.gen_sub_state:
            return {}
        sub_state = GenSQLState.model_validate(state.gen_sub_state)
        dialect = ctx["dialect"]
        complexity = ctx["complexity"]
        in_correction = bool(ctx.get("in_correction"))
        kb_exact_match: dict[str, Any] | None = ctx.get("kb_exact_match")
        example_hits: list[dict[str, Any]] = ctx.get("examples") or []

        update: dict[str, Any] = {
            "dialect": dialect,
            "candidates": [],
            "fast_path": False,
            # 本轮**是否**降级。默认必须写在这里(每轮重建 update 时都写),
            # 不能只靠降级分支补写:普通 state 通道不写就保留上一轮的值,
            # 第 1 轮降级、第 2 轮正常时会带着 True 进最终 state —— I6 禁止
            # 用一次没发生在**这条 SQL** 上的降级去折损它(与 self_check_passed
            # 归零同型、反向)。覆盖到的三条路径:KB 精确命中 / 经典生成不标降级
            # (它们是配置形态,不是降级);agentic 正常轮不标;只有走兜底才置 True。
            "generation_degraded": False,
            # ``selection`` 是同一个道理的**第四个成员**:上面三个都写了默认值
            # 就是因为它必须每轮复位,漏了就等于没有复位。``select`` 从第 2 轮起
            # 基本不投票 —— 它开头就是 ``if state.error or state.error_feedback
            # or not state.candidates: return {}``,而 ``candidates`` 正是这里被
            # 清成 ``[]`` 的那个字段 —— 所以不清的话 ``selection`` 永远停在第 1
            # 轮的投票上:下游 ``_sql_adjustments`` 的 ``winner`` 守卫会认错人
            # (该扣的降级分不扣),Task 7 的票率折扣会拿上一轮的票率折这一轮的
            # SQL。断言它的用例:``tests/workflow/test_graphs.py``
            # ``TestSelectionIsRoundScoped``。
            "selection": {},
            # 装配 dump 的 tools 节:与 fast_path 同族,**每轮复位** ——
            # 记的是最终交付那条 SQL 的那一轮用了什么工具(没进 agentic 分支
            # 的轮次 = 这一轮没建注册表,空列表就是实情)。
            "assembly_tools": [],
        }

        if kb_exact_match is not None:
            # KB 精确命中:直接用标准 SQL,跳过模型生成(避免歧义变体)
            update["sql"] = kb_exact_match["sql"]
            update["kb_exact_match"] = True
            logger.info(
                "KB exact match used: %r", kb_exact_match["question"][:80],
            )
        elif agentic and complexity != "simple":
            from trove.workflow.nodes.gen_sql import (
                build_sql_registry,
                extract_sql,
            )

            # 工具统一由注册表提供(定义+处理器+finish 协议+超时/并行策略);
            # check_hits 收集 check_result 的规则命中,循环结束后随 update
            # 带出(归因)。validate_sql 始终可用,probe/check 依赖 connectors。
            # 任务自适应工具集(complexity 分档):standard → validate+probe+
            # check;complex → 全量含 catalog(search/lookup/explain)。角色
            # ACL 按 state.tool_roles 裁剪;probe_cache 在节点闭包持有 →
            # 本运行的修正轮间共享同一 SQL 的 probe/check 结果(防重复执行)。
            # run_id 传入 = 缓存作用域:闭包 dict 是进程级的(图只编译一次),
            # 键里带 run_id 才不会把上一次运行的观测喂给本次运行。
            registry = build_sql_registry(
                services.connectors, sub_state.question, sub_state.lang, dialect,
                matched_tables=state.matched_tables or None,
                datasource=state.datasource,
                complexity=complexity,
                roles=state.tool_roles or None,
                user_id=state.user_id,
                run_id=state.run_id,
                probe_cache=probe_cache,
                skills=services.skills,
                # load_skill 的触发器判定读它 —— 与下方广告块同一个 ctx 源。
                # 漏传 = role/lang 类收窄恒不匹配:广告点了名、按名调过去被拒。
                skill_ctx=state.skill_ctx(),
                budget=budget,
                profiles=profiles,
                semantic_provider=services.semantic_layer,
                egress=_egress_redactor(services),
                # 生成工具的真实执行(probe/check/search)是取数路径:授权门
                # 与 execute_sql 同源,主体 wire 原样传 —— 两次解析会漂移。
                authorizer=_build_authorizer(services),
                principal=state.principal,
            )
            # 懒工具名快照:装配 dump 要靠"跑之前的懒集合 + 跑之后还剩谁在
            # 懒集合里"才能判出 lazy/activated(解锁过的会移出 _lazy_specs,
            # 事后单独看注册表分不出来)。
            lazy_names = lazy_tool_names(registry)

            prompt = build_sql_prompt_from_state(sub_state)
            # ① Prompt caching:prompt 头两节(dialect+schema)与
            # render_cache_prefix 逐字一致,交给 agent_loop 按字节前缀
            # 拆稳定块打 ephemeral 断点(跨调用复用)。前缀校验/回退在
            # loop 内部——这里只受配置开关门控,关闭时整串单块。
            cache_prefix = (
                render_cache_prefix(dialect, sub_state.schema_context)
                if (services.config or AgentConfig()).prompt_caching
                else None
            )
            system_text = render(
                "gen_sql/system",
                lang=sub_state.lang,
                has_probe=services.connectors is not None,
                full_rules=complexity != "simple",
            )
            # 方法论 skill:required 档 org skill 全量注入(org 约定必须遵守),
            # available 档只广告描述 + load_skill 按需取正文(注册表已挂工具)。
            skills = services.skills
            if skills is not None:
                # ctx 取自外层 WorkflowState 而非 sub_state:GenSQLState 只带
                # lang/datasource/complexity,没有 intent/tool_roles —— 用它拼
                # ctx 会让这两类 trigger 在 gen_sql 上恒不命中,正是本任务在
                # 修的那类静默失效。
                required_block = skills.render_skills("gen_sql", **state.skill_ctx())
                available_block = skills.available_skills_block(
                    "gen_sql", **state.skill_ctx())
                system_text = append_skill_block(system_text, required_block)
                system_text = append_skill_block(system_text, available_block)
                # 装配 dump 的 skill_injections 块:真正进了 system prompt 的
                # 技能文本的 token 数(required 正文 + available 广告)。
                # 逐 skill 拆分只有 service 内部按条目渲染时才有,公开面拿不到
                # ——按块只报总量(items 留空,见 tracing/assembly 的约定)。
                # 空块不算成本(count_tokens("") 恒 ≥1,直接相加会凭空造出
                # 一个假的 skill_injections 块)。
                skill_injected_tokens = (
                    (count_tokens(required_block) if required_block else 0)
                    + (count_tokens(available_block) if available_block else 0)
                )
                if skill_injected_tokens:
                    update["assembly_blocks"] = [
                        *(state.assembly_blocks or []),
                        block_entry("skill_injections", tokens=skill_injected_tokens),
                    ]
            # 每节点覆盖优先(node_models.gen_sql),再复杂度分档 —— 与
            # 经典路径 gen_sql.py 的选模同键;下方估算校准读写同一 model。
            model = (
                services.config.model_for_node("gen_sql", complexity)
                if services.config else "openai/gpt-4o"
            )
            result = None
            try:
                result = await run_agent_loop(
                    services.llm, model,
                    system=system_text,
                    user=prompt,
                    cache_prefix=cache_prefix,
                    registry=registry,
                    tool_timeout_s=20.0,
                    time_budget_s=120.0,
                    max_rounds=8,  # ④ 早期轮转 [compacted] 摘要后护栏放宽
                    max_total_tokens=2500,
                    soft_rounds_budget=(
                        services.config.gen_sql_soft_rounds if services.config else 0
                    ),
                    metadata={"node": "gen_sql", "session_id": state.session_id, "run_id": state.run_id},
                    temperature=generation_temperature(state.retry_count),
                )
            except Exception as e:
                logger.warning("Agentic gen_sql failed (%s); falling back to classic", e)
                result = None

            # 装配 dump 的 tools 节:注册表逐工具记录(level/roles/懒注册与
            # 激活状态),calls 取自 tool_history(每次真实执行的工具调用)。
            # loop 中途异常(result=None)也照记 —— 注册表已建,这一轮确实
            # 备了这些工具;calls 归零而已。
            update["assembly_tools"] = tools_from_registry(
                registry, lazy_names=lazy_names,
                tool_history=(result or {}).get("tool_history") or [],
            )
            if registry.egress_report:
                # 出境脱敏汇总(字段名 + 计数,不含值):与装配阶段的样例值
                # 脱敏合并,run 结束由 session best-effort 写 llm.egress 审计。
                update["egress_report"] = merge_egress_reports(
                    state.egress_report, registry.egress_report,
                )

            async def _classic_fallback() -> None:
                """经典单发子图生成(异常 / agent loop 空手而归 / 护栏降级兜底)。"""
                # 降级是可披露的**降低**信号(设计 §5.2 的 −0.10 挂在它上面)。
                # 三个调用点(异常 / guard_hit / 空手)都汇到这一个闭包,所以
                # 标记写在这里 —— 写在各调用点上迟早漏一处。
                update["generation_degraded"] = True
                out = await subgraph.ainvoke(sub_state)
                if out["sql"]:
                    update["sql"] = out["sql"]
                if out.get("attempts"):
                    update["attempts"] = out["attempts"]
                if out["error"]:
                    update["error"] = out["error"]
                # 降级到经典子图 ⇒ 交付的 SQL 没经过 check_result(经典路径根本
                # 不注册这个工具)。**必须显式写 0**:它是普通 state 通道,不写就
                # 保留上一轮的值 —— 第 1 轮自检通过、第 2 轮降级,那条没被验证
                # 的 SQL 会带着上一轮的通过进 state。三个调用点(异常 / 护栏降级
                # / 空手而归)都走这里,所以写 0 覆盖全部;其中「护栏降级」与
                # 「空手而归」发生在下面写 check_passed 之后,不写 0 会让**本轮**
                # 的自检分数留给一条它没检查过的 SQL,同样是虚高(I6)。
                update["self_check_passed"] = 0

            if result is not None:
                # 估算校准闭环:est 与 actual 同基准——actual(agent_loop 首轮
                # prompt_tokens)是完整首轮输入(system + tools JSON + user),
                # est 也必须是完整首轮估算。旧口径只算可选块,系统性低估让
                # factor>1 反而过度裁剪可选块(低估被放大成伤害);观测本身
                # 不加校准因子——因子是装配时的预算修正,不是观测的输入。
                est = (
                    count_tokens(system_text)
                    + count_tokens(json.dumps(registry.defs(), ensure_ascii=False))
                    + count_tokens(prompt)
                )
                actual = result.get("first_input_tokens") or 0
                if est and actual:
                    record_token_calibration(model, dialect, est, actual)
                raw_sql = extract_sql(result["content"]) if result.get("content") else ""
                # 解析门:收尾文本/工具载荷必须是一条可解析的查询才允许落
                # state.sql(0479:散文收尾曾被当 SQL 接受,直到执行层才拦)。
                sql = _payload_sql(raw_sql, dialect)
                gate_dropped = bool(raw_sql.strip()) and not sql
                if not sql:
                    # 模型可能只在工具里给出 SQL,最终 content 无 SQL 回显
                    # (validate/probe/check/finish 过都算——捞最近一次**可解析**
                    # 的工具载荷;散文/半截 SQL 被解析门挡下)。
                    for entry in reversed(result.get("tool_history") or []):
                        args = entry["arguments"] or {}
                        payload = args.get("sql") or args.get("answer")
                        if entry["name"] in ("validate_sql", "probe_query", "check_result", "finish") and payload:
                            sql = _payload_sql(payload, dialect)
                            if sql:
                                break
                            gate_dropped = True
                update["attempts"] = result["rounds"]
                # check_result 规则命中随状态带出(与既有 hits 合并,不覆盖
                # validate 层已记录的拦截);解析门拦下也记一笔归因。
                if registry.check_hits or gate_dropped:
                    hits = list(state.validation_hits) + registry.check_hits
                    if gate_dropped:
                        # 与 rules.verify / validate 节点的命中同形({name, reason}):
                        # 它就是一次真实拦截(载荷被拒、随后经典兜底),是 eval
                        # tried_recovery 该看到的恢复前因,不是旁观事件。
                        hits.append({
                            "name": "sql_gate",
                            "reason": "unparseable payload dropped before execution",
                        })
                    update["validation_hits"] = hits
                # 自检通过次数**每轮覆盖**(不 += 上一轮):记的是最终那一轮
                # 的自检结果。修正轮之后交付的 SQL 是新的,用旧轮的通过给它
                # 加分是虚高(I6)。
                update["self_check_passed"] = registry.check_passed
                trail = " ".join(
                    p for p in (result.get("reasoning", ""), result.get("transcript", "")) if p
                )
                if trail:
                    update["reasoning_history"] = [{"node": "gen_sql", "text": trail[:800]}]
                if sql:
                    update["sql"] = sql
                elif result.get("guard_hit"):
                    # 护栏降级:agent loop 预算耗尽/原地打转未产出 SQL →
                    # 降到经典生成,而不是直接判失败
                    logger.warning(
                        "gen_sql agent loop guard (%s, %d rounds); degrading to classic",
                        result.get("budget_why"), result["rounds"],
                    )
                    await _classic_fallback()
                else:
                    # loop 正常结束但没产出 SQL(如最后一轮只有 reasoning、
                    # content 为空且未调工具)——兜底到经典生成,不静默空转
                    logger.warning(
                        "Agentic gen_sql produced no SQL (%d rounds); falling back to classic",
                        result["rounds"],
                    )
                    await _classic_fallback()
            else:
                await _classic_fallback()
        else:
            out = await subgraph.ainvoke(sub_state)
            if out["sql"]:
                update["sql"] = out["sql"]
            if out.get("attempts"):
                update["attempts"] = out["attempts"]  # 子图内校验重试次数
            if out.get("llm"):
                update["llm"] = out["llm"]
            if out["error"]:
                update["error"] = out["error"]
        # Multi-candidate: extra generations at higher temperatures;
        # failures fall back to the single-candidate path silently.
        # KB 精确命中时跳过备选生成——备选的"另一种解释"只会引发
        # 无谓的一致性拉锯。
        # 每个温度子图各跑一次(总候选 = 1 primary + N alt),重复文本
        # 不重复投票(与 primary 相同或彼此相同则跳过,继续下个温度)。
        if (
            (subgraph_alt is not None or alt_subgraphs)
            and update.get("sql") and not update.get("error")
            and not update.get("kb_exact_match")
            # 简单题(单表/单聚合/无 join 等)跳过多候选:共识投票是给
            # 歧义准备的,简单结构没有歧义值得花 4 次额外生成
            and complexity != "simple"
            # 修正轮默认跳过候选:模型已拿到错误反馈做定点修复,再花
            # 4 次生成纯属烧预算(执行/规则/裁决失败与"歧义"无关)。
            # 唯一例外是上一轮 select 平局(consensus=False)——候选池
            # 跨轮加票正是为那种歧义设计的,保留。
            and not (in_correction and state.consensus)
        ):
            alt_graphs = alt_subgraphs if alt_subgraphs else [subgraph_alt]
            # 候选池跨轮累积(重试=加票):旧候选保留,新候选加入后一起投票。
            # 平局轮打回 gen_sql 时 state.candidates 是上一轮的池——同组票数
            # 跨轮相加,正确解释的证据可以跨轮聚合,而不是每轮推倒重来。
            seen = {" ".join(update["sql"].split()).lower()}
            seen.update(" ".join(c.split()).lower() for c in state.candidates)
            candidates = list(state.candidates)
            schedule = _candidate_schedule(len(alt_graphs))
            if agentic:
                # subagent 委派(主 agent 规划 + subagent 并行执行):每个备选
                # 是一个独立 ReAct loop(自建工具注册表、自定终止),以去相关
                # 温度 + few-shot 轮换 + 风格提示并行打磨,产出候选供 select
                # 共识投票。asyncio.gather 把 N 个 subagent 压到单次耗时;
                # 容错:返回 None/异常即静默跳过。
                egress_sink: dict[str, dict[str, Any]] = {}
                outs = await asyncio.gather(
                    *(
                        _run_candidate_subagent(
                            services, state, sub_state.model_copy(deep=True),
                            dialect, temperature=t, mode=m, rotation=idx + 1,
                            probe_cache=probe_cache,
                            budget=budget, profiles=profiles,
                            egress=_egress_redactor(services),
                            egress_sink=egress_sink,
                        )
                        for idx, (t, m) in enumerate(schedule)
                    ),
                    return_exceptions=True,
                )
                if egress_sink:
                    update["egress_report"] = merge_egress_reports(
                        update.get("egress_report") or state.egress_report,
                        egress_sink,
                    )
                for out_alt in outs:
                    if isinstance(out_alt, BaseException):
                        continue
                    alt_sql = out_alt or ""
                    if not alt_sql:
                        continue
                    key = " ".join(alt_sql.split()).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(alt_sql)
            else:
                # 经典候选:固定 generate→validate 子图并行(非 agentic 档)。
                # 并行生成:各温度子图互不依赖,asyncio.gather 把候选生成时间
                # 从 N×串行压到单次耗时(容错:异常子图静默跳过)。
                # P2-7:每个备选按索引轮换 few-shot 首条目——不同候选锚定
                # 不同参考示例,避免共享同一组示例导致候选趋同。
                outs = await asyncio.gather(
                    *(
                        g.ainvoke(_rotate_few_shots(
                            sub_state.model_copy(deep=True), idx + 1))
                        for idx, g in enumerate(alt_graphs)
                    ),
                    return_exceptions=True,
                )
                for out_alt in outs:
                    if isinstance(out_alt, BaseException):
                        continue
                    alt_sql = out_alt.get("sql", "")
                    if not alt_sql or out_alt.get("error"):
                        continue
                    key = " ".join(alt_sql.split()).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(alt_sql)
            if candidates:
                update["candidates"] = candidates
        if example_hits:
            update["kb_hits"] = [
                {"kind": "example", "question": h["question"], "sql": h["sql"], "tags": h["tags"]}
                for h in example_hits
            ]
        return update

    return gen_generate


def _make_gen_sql_node(
    services: GraphServices,
    subgraph: CompiledStateGraph,
    subgraph_alt: CompiledStateGraph | None = None,
    alt_subgraphs: list[CompiledStateGraph] | None = None,
    agentic: bool = True,
    budget: Any = None,
    profiles: Any = None,
):
    """Compatibility composite: gen_retrieve → gen_assemble → gen_generate.

    The reflection/fixed graphs wire the three stages as separate nodes for
    finer checkpointing and step observability; this composite keeps the old
    single-call contract for direct-node callers and tests.
    """
    retrieve = make_gen_retrieve(services)
    assemble = make_gen_assemble(services)
    generate = make_gen_generate(
        services, subgraph, subgraph_alt=subgraph_alt,
        alt_subgraphs=alt_subgraphs, agentic=agentic,
        budget=budget, profiles=profiles,
    )

    async def gen_sql(state: WorkflowState) -> dict[str, Any]:
        if state.error:
            return {}
        update: dict[str, Any] = {}
        for step in (retrieve, assemble, generate):
            if state.error:
                break
            if update:
                state = state.model_copy(update=update)
            update.update(await step(state))
        return update

    return gen_sql
