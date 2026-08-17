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

    # Correction reasons accumulated across the run (Hint Bank capture)
    correction_history: Annotated[list[str], operator.add] = Field(default_factory=list)

    # LLM 思考痕迹(节点 → 紧凑轨迹:模型文本+工具调用+观测/推理),
    # 回退修正时注入诊断与重生成 prompt。operator.add 累积。
    reasoning_history: Annotated[list[dict[str, str]], operator.add] = Field(default_factory=list)

    # Context budget usage of the last gen pass (observability)
    context_usage: list[dict[str, Any]] = Field(default_factory=list)

    # LLM call detail of the last LLM node (model/elapsed/io previews)
    llm: dict[str, Any] | None = None

    # Knowledge base hits (term matches + example matches).
    # operator.add: updates from different nodes accumulate.
    kb_hits: Annotated[list[dict[str, Any]], operator.add] = Field(default_factory=list)

    # KB 精确命中:gen_sql 直接采用了与问题几乎逐词一致的示例 SQL
    # (未经过模型生成)。reflect 对这类答案跳过语义裁决(执行与
    # 确定性规则已通过,KB 是标准写法)。
    kb_exact_match: bool = False

    # schema_linking artifacts
    matched_tables: list[str] = Field(default_factory=list)
    schema_context: str = ""

    # gen_sql artifacts
    sql: str = ""
    dialect: str = "sqlite"

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

    # select 节点投票归因:各结果组票数、赢家(primary/候选)、是否采纳、
    # 被 verify/执行失败过滤掉的候选
    selection: dict[str, Any] = Field(default_factory=dict)

    # 平局专用计数(select 打回时 +1):自适应降级信号——拉锯轮次达阈值
    # (adopt_after_tie_rounds)后采纳票王,不再烧完共享 retry 预算
    tie_rounds: int = 0

    # LLM diagnosis of the failed SQL (error type / judgment / fix plan)
    error_analysis: str = ""

    # 已试错的解释黑名单(analyze_error 累积,指纹去重):
    # [{"sql": 失败SQL摘要, "reason": 失败原因}] — 注入重生成 prompt,
    # 防止模型在修正轮重复同样的错误假设(撞预算题的典型形态)
    rejected_hypotheses: Annotated[list[dict[str, str]], operator.add] = Field(default_factory=list)

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

    # Knowledge base material for prompt injection
    few_shots: list[dict[str, Any]] = Field(default_factory=list)   # reference examples/templates
    term_notes: list[dict[str, Any]] = Field(default_factory=list)  # terminology definitions
    lessons: list[dict[str, Any]] = Field(default_factory=list)     # known pitfalls (Hint Bank)
    rules: list[str] = Field(default_factory=list)                    # data source business rules
    llm: dict[str, Any] | None = None                                    # last generate call detail
