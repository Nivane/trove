"""Graph state schemas for Trove workflows.

Two state classes:
  - WorkflowState: main-graph state, carried through a full query run
  - GenSQLState: gen_sql subgraph state for the internal validate-retry loop

Conversation message history is NOT part of graph state (dual-track
persistence): session messages live in SessionStore, graph state lives
in the LangGraph checkpointer.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from pydantic import BaseModel, Field


def budget_exhausted(retry_count: int, max_retries: int) -> bool:
    """共享修正预算判定:retry_count 达到上限即耗尽。

    各节点的降级分支不同(execute→error / reflect→forced OK /
    select→低置信交付),因此只统一"比较 + 耗尽判定",分支保留在各节点。
    """
    return retry_count >= max_retries


# 修正轮累积产物的硬上限:这些列表随 retry 轮次只增不减(修正最多 10 轮),
# 不封顶会在第 10 轮把整条失败 SQL × 10 全注入 prompt。写入即裁剪
# (带 cap 的 reducer,模块级函数保证 checkpointer 可 pickle)。
# 回归检查只读最近一版(sql_versions[-1]),裁掉旧版不影响判定。
MAX_SQL_VERSIONS = 3           # 版本链保留最近 N 版
MAX_REJECTED_HYPOTHESES = 5    # 解释黑名单保留最近 N 条
MAX_REASONING_ENTRIES = 8      # 思考痕迹保留最近 N 条


def _cap_add(
    existing: list | None, update: list | None, limit: int,
) -> list:
    """``operator.add`` 的封顶变体:追加后裁剪到最近 ``limit`` 条。"""
    if update is None:
        return existing or []
    return (list(existing or []) + list(update))[-limit:]


def _cap_sql_versions(existing, update):
    return _cap_add(existing, update, MAX_SQL_VERSIONS)


def _cap_rejected_hypotheses(existing, update):
    return _cap_add(existing, update, MAX_REJECTED_HYPOTHESES)


def _cap_reasoning_history(existing, update):
    return _cap_add(existing, update, MAX_REASONING_ENTRIES)


class WorkflowState(BaseModel):
    """State carried through one workflow (graph) execution."""

    session_id: str
    question: str
    run_id: str = ""  # trace identity of this run (set by SessionManager)

    # 交互语言(配置驱动: config.language,zh/en;不按问题语言检测)
    lang: str = "zh"

    # Compact conversation history (prior exchanges) for follow-up questions
    history: str = ""

    # Official hint for the question (evaluation/reference use); kept
    # separate so classification and rules see the pure question only
    evidence: str = ""

    # parse_date 节点产物:解析出的时间范围 "YYYY-MM-DD ~ YYYY-MM-DD"(未命中为空)
    time_context: str = ""

    # Alternative candidate SQLs (multi-candidate generation, reflection
    # workflow only) — consumed by the consensus select node
    candidates: list[str] = Field(default_factory=list)

    # When set, the pipeline asks the user for clarification instead of
    # generating SQL (e.g. no tables matched the question)
    clarification_question: str = ""

    # LLM query plan (planner node) injected into SQL generation
    plan: str = ""

    # 结构化计划(planner 原始 JSON):answer_columns 双层验证的输入——
    # 层1 校验计划引用的表/列存在性,层2 执行后校验结果列一致性
    plan_json: dict[str, Any] | None = None

    # 计划校验观测(planner/validate 写入):status ok/dropped + errors,
    # 供 eval 归因「plan 层拦了什么」
    plan_validation: dict[str, Any] = Field(default_factory=dict)

    # 复杂度分级(gen_sql 写入,reflect 读取):"simple"/"standard"/"complex",
    # 驱动负载削减开关(经典子图/跳多候选/跳裁决);修正轮强制 standard
    complexity: str = "standard"

    # Multi-candidate agreement; False = candidates disagreed and the
    # answer is delivered with a low-confidence note
    consensus: bool = True

    # User intent (route_intent node): query / metadata (two-way)
    intent: str = "query"

    # 意图判定的证据链(route_intent 观测):strong/table/term/data_signal
    # 命中与 LLM 原始判定/失败原因,供日志与诊断还原路由决策
    intent_evidence: dict[str, Any] = Field(default_factory=dict)

    # Direct answer for non-query intents (metadata/lineage questions)
    intent_answer: str = ""

    # 意图层改写痕迹:省略式追问补全 / 纯反馈重跑上一问时,记录原始问题
    # (当前 question 是改写后的有效问题);空 = 本次未发生改写
    rewritten_question: str = ""

    # Correction reasons accumulated across the run (Hint Bank capture)
    correction_history: Annotated[list[str], operator.add] = Field(default_factory=list)

    # LLM 思考痕迹(节点 → 紧凑轨迹:模型文本+工具调用+观测/推理),
    # 回退修正时注入诊断与重生成 prompt。cap 版累积(最近 N 条)。
    reasoning_history: Annotated[list[dict[str, str]], _cap_reasoning_history] = Field(default_factory=list)

    # Context budget usage of the last gen pass (observability)
    context_usage: list[dict[str, Any]] = Field(default_factory=list)

    # 稳定可缓存前缀(dialect+schema)的 token 数——prompt caching 观测:
    # 同一数据源+方言下该前缀字节级稳定,跨调用可复用(缓存折扣)。
    cache_prefix_tokens: int = 0

    # LLM call detail of the last LLM node (model/elapsed/io previews)
    llm: dict[str, Any] | None = None

    # Knowledge base hits (term matches + example matches).
    # operator.add: updates from different nodes accumulate.
    kb_hits: Annotated[list[dict[str, Any]], operator.add] = Field(default_factory=list)

    # KB 精确命中:gen_sql 直接采用了与问题几乎逐词一致的示例 SQL
    # (未经过模型生成)。reflect 对这类答案跳过语义裁决(执行与
    # 确定性规则已通过,KB 是标准写法)。
    kb_exact_match: bool = False

    # 确定性快径命中:fast_match 节点用 kb init 模板直接产出 SQL
    # (未经过 planner/生成)。reflect 对这类答案跳过语义裁决,理由同
    # kb_exact_match——模板是确定性产物,不是模型解释。
    fast_path: bool = False

    # schema_linking artifacts
    matched_tables: list[str] = Field(default_factory=list)
    schema_context: str = ""

    # gen_sql artifacts
    sql: str = ""
    dialect: str = "sqlite"

    # 语义说明(semantics 节点):生成 SQL 后 LLM 用自然语言解释其含义,
    # 在执行前展示给用户(HITL 确认时同时呈现)。空 = 未生成(未开启/无 SQL/失败)。
    semantics: str = ""

    # 洞察(insights 节点):执行完成后 LLM 基于结果表格生成的自然语言洞察。
    insights: list[str] = Field(default_factory=list)

    # HITL 状态(hitl 节点):"" = 未参与/未开启;"pending" = 已中断等待确认;
    # "approved" = 用户批准继续执行;"rejected" = 用户否决(中止,不再执行)。
    hitl_status: str = ""

    # execute_sql artifacts
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = -1  # -1 = not executed
    execution_time_ms: float = 0.0

    # reflect artifacts + retry loop counter
    verdict: str = ""  # OK / RETRY / EMPTY / NO_SQL
    reason: str = ""
    retry_count: int = 0  # shared correction budget (reflect → gen_sql loop)
    forced: bool = False  # reflect accepted a RETRY at the retry cap
    semantic_retries: int = 0  # 连续纯语义 RETRY 计数(执行成功仍被打回)

    # reflect/analyze_error verdict: the question is not answerable by SQL
    # (table meaning / term definition) → route to answer_metadata
    no_sql: bool = False

    # LLM-judged rollback (analyze_error): which upstream step to rerun.
    # last_rollback_target feeds the deterministic anti-loop escalation.
    rollback_target: str = ""   # gen_sql / planner / schema_linking
    last_rollback_target: str = ""

    # graceful degradation channel: first node failure message wins
    error: str = ""

    # execution-error feedback: execute_sql failures route back to gen_sql
    # with this message (shared retry budget); cleared on success
    error_feedback: str = ""

    # verify_step 断言层命中记录(断言名 + 失败原因);规则失败的那一轮写入,
    # 供日志/eval 归因「哪条断言拦了什么」
    validation_hits: list[dict] = Field(default_factory=list)

    # 确定性规则全过信号(validate 节点写入):本轮执行结果通过了
    # rules.verify 全链 + 层2 计划列检查,无 error/error_feedback。
    # reflect 据此 + 复杂度决定是否跳过 LLM 裁决。
    rules_passed: bool = False

    # select 节点投票归因:各结果组票数、赢家(primary/候选)、是否采纳、
    # 被 verify/执行失败过滤掉的候选
    selection: dict[str, Any] = Field(default_factory=dict)

    # 平局专用计数(select 打回时 +1):自适应降级信号——拉锯轮次达阈值
    # (adopt_after_tie_rounds)后采纳票王,不再烧完共享 retry 预算
    tie_rounds: int = 0

    # 修复模式(analyze_error 判定,缺口3): fixer = 实现级定点修(保持语义
    # 解释不变); revisor = 语义重写(重新评估问题意图)。注入重生成 prompt。
    fix_mode: str = ""

    # 修复进展量化(analyze_error 维护,缺口5): 最近一轮 regression_state
    # 标签(first/invalid/none/shift/improved) + 连续无进展轮数计数。
    # 计数达 MAX_NO_PROGRESS_ROUNDS → analyze_error 提前止损(不再打回)。
    last_progress: str = ""
    no_progress_rounds: int = 0

    # select 置信度(票王得票率): 候选投票分布的确定性信号,供降级/输出观测
    confidence: float = 0.0

    # SQL 版本链(analyze_error 记录,cap 版累积):
    # [{"sql": 失败SQL全文, "sig": 结果集签名, "issues": [规则名], "round": N}]
    # 注入重生成 prompt 支撑定点修复;回归硬检查对比相邻版本。
    # 只保留最近 MAX_SQL_VERSIONS 版(回归判定只看上一版,旧版无增益)。
    sql_versions: Annotated[list[dict[str, Any]], _cap_sql_versions] = Field(default_factory=list)

    # LLM diagnosis of the failed SQL (error type / judgment / fix plan)
    error_analysis: str = ""

    # 已试错的解释黑名单(analyze_error 累积,指纹去重 + cap 上限):
    # [{"sql": 失败SQL摘要, "reason": 失败原因}] — 注入重生成 prompt,
    # 防止模型在修正轮重复同样的错误假设(撞预算题的典型形态)
    rejected_hypotheses: Annotated[list[dict[str, str]], _cap_rejected_hypotheses] = Field(default_factory=list)

    # output artifact
    final_response: str = ""


class GenSQLState(BaseModel):
    """State for the gen_sql subgraph (generate → validate retry loop)."""

    question: str
    session_id: str = ""  # for trace metadata
    run_id: str = ""      # for trace metadata
    schema_context: str = ""
    dialect: str = "sqlite"
    reflect_reason: str = ""  # reason from a previous reflect RETRY (empty on first pass)

    sql: str = ""
    attempts: int = 0
    validation_errors: list[str] = Field(default_factory=list)
    error: str = ""

    # execution-error feedback from a previous pass (injected into the prompt)
    error_feedback: str = ""

    # LLM diagnosis of the failed SQL (error type / judgment / fix plan)
    error_analysis: str = ""

    # 上一轮思考痕迹(诊断方/生成方轨迹),注入重生成 prompt
    reasoning_context: str = ""

    # 已试错的解释黑名单(跨轮累积),注入重生成 prompt 禁止重复假设
    rejected_hypotheses: list[dict[str, str]] = Field(default_factory=list)

    # 上一版失败 SQL 全文(Fixer 模式):修正轮注入 prompt,指示局部修改
    # 而非整体重写——诊断后的修复路径比从头生成更可靠
    previous_sql: str = ""

    # 失败版本链(跨轮累积):[{"sql", "sig", "issues", "round"}] — 定点修复
    # 注入全部历史版本,回归检查对比相邻版本
    sql_versions: list[dict[str, Any]] = Field(default_factory=list)

    # 修复模式(analyze_error 判定): fixer 实现级 / revisor 语义级,注入 prompt
    fix_mode: str = ""

    # 交互语言(由外层 WorkflowState 注入)
    lang: str = "zh"

    # conversation history for follow-up questions
    history: str = ""

    # official hint for the question (injected as its own prompt section)
    evidence: str = ""

    # parse_date 节点产物:解析出的时间范围 "YYYY-MM-DD ~ YYYY-MM-DD"(未命中为空)
    time_context: str = ""

    # LLM query plan (planner node) injected into the generation prompt
    plan: str = ""

    # 复杂度分档(由外层 WorkflowState 注入):模型分层用——simple/standard
    # 走 model_fast,complex 走 target;缺省 standard 保证直接构造行为不变
    complexity: str = "standard"

    # Knowledge base material for prompt injection
    # None = 未注入(context budget 排除或未检索);消费方统一 ``or None`` 处理
    few_shots: list[dict[str, Any]] | None = None   # reference examples/templates
    term_notes: list[dict[str, Any]] | None = None  # terminology definitions
    lessons: list[dict[str, Any]] | None = None     # known pitfalls (Hint Bank)
    rules: list[str] | None = None                  # data source business rules
    llm: dict[str, Any] | None = None                    # last generate call detail

    @classmethod
    def from_workflow(
        cls,
        state: WorkflowState,
        *,
        dialect: str,
        included: set[str] | None = None,
        reasoning_context: str = "",
        few_shots: list[dict[str, Any]] | None = None,
        term_notes: list[dict[str, Any]] | None = None,
        lessons: list[dict[str, Any]] | None = None,
        rules: list[str] | None = None,
        history: str | None = None,
        schema_context: str | None = None,
    ) -> "GenSQLState":
        """从外层 WorkflowState 构造子图状态:集中字段映射 + 派生 + 预算门控。

        - 直接复制:question/session/run/schema/lang/time/evidence 等上下文;
        - 派生:reflect_reason ← state.reason;previous_sql 仅修正轮取上一版
          失败 SQL;reasoning_context 由调用方按思考痕迹渲染后传入;
        - 预算门控:history/plan/few_shots/term_notes/lessons/rules 只在
          included 包含对应块时注入(included 为 None = 全注入);
        - 覆盖参数:history/schema_context 非 None 时优先于 state 对应字段
          ——item 级预算裁剪后的历史轮 / 裁剪后的 schema 由调用方传入。
        """

        def _include(name: str) -> bool:
            return included is None or name in included

        in_correction = bool(state.error_feedback or state.error_analysis or state.reason)
        return cls(
            question=state.question,
            session_id=state.session_id,
            run_id=state.run_id,
            schema_context=(
                schema_context if schema_context is not None else state.schema_context
            ),
            dialect=dialect,
            lang=state.lang,
            time_context=state.time_context,
            reflect_reason=state.reason,
            error_feedback=state.error_feedback,
            error_analysis=state.error_analysis,
            reasoning_context=reasoning_context,
            rejected_hypotheses=state.rejected_hypotheses,
            sql_versions=state.sql_versions,
            fix_mode=state.fix_mode,
            previous_sql=state.sql if in_correction else "",
            history=(
                history
                if history is not None
                else (state.history if _include("history") else "")
            ),
            plan=state.plan if _include("plan") else "",
            complexity=state.complexity,
            evidence=state.evidence,
            few_shots=few_shots if _include("few_shots") else None,
            term_notes=term_notes if _include("term_notes") else None,
            lessons=lessons if _include("lessons") else None,
            rules=rules if _include("rules") else None,
        )
