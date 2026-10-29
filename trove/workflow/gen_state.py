"""Gen subgraph state — generate → validate retry loop.

Extracted from ``state.py`` (change axis: gen subgraph internals). It is
independent of the main-graph ``WorkflowState``; ``from_workflow`` is the only
bridge, and it only reads attributes. That bridge is typed ``Any`` on purpose:
importing ``state`` here — even under ``TYPE_CHECKING`` — would close a
sibling cycle (``state`` re-exports ``GenSQLState``, and the dependency matrix
counts type-checking imports), so no import edge exists in either direction.
``state.py`` re-exports ``GenSQLState`` for every existing import path.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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

    # 目标数据源(由外层 WorkflowState 经 from_workflow 注入)
    datasource: str = ""

    # conversation history for follow-up questions
    history: str = ""

    # official hint for the question (injected as its own prompt section)
    evidence: str = ""

    # parse_date 节点产物:解析出的时间范围 "YYYY-MM-DD ~ YYYY-MM-DD"(未命中为空)
    time_context: str = ""

    # LLM query plan (query_sketch node) injected into the generation prompt
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
    user_facts: list[dict[str, Any]] | None = None  # 用户级记忆(偏好/口径,user+datasource)
    episodes: list[dict[str, Any]] | None = None    # 情景记忆(过去类似查询,episodic)
    metrics: list[dict[str, Any]] | None = None     # 相关性选择的指标(表达式+口径)
    entities: list[dict[str, Any]] | None = None    # 匹配的维度/枚举字段(值确认提示)
    # ⑦ 用户×数据源失败画像提示(opt-in profile_boost;已渲染文本,
    # 空串 = 未注入)
    profile: str = ""
    llm: dict[str, Any] | None = None                    # last generate call detail

    @classmethod
    def from_workflow(
        cls,
        state: Any,
        *,
        dialect: str,
        included: set[str] | None = None,
        reasoning_context: str = "",
        few_shots: list[dict[str, Any]] | None = None,
        term_notes: list[dict[str, Any]] | None = None,
        lessons: list[dict[str, Any]] | None = None,
        rules: list[str] | None = None,
        user_facts: list[dict[str, Any]] | None = None,
        episodes: list[dict[str, Any]] | None = None,
        metrics: list[dict[str, Any]] | None = None,
        entities: list[dict[str, Any]] | None = None,
        profile: str = "",
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
            datasource=state.datasource,
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
            user_facts=user_facts if _include("user_facts") else None,
            episodes=episodes if _include("episodes") else None,
            metrics=metrics if _include("metrics") else None,
            profile=profile if _include("profile") else "",
            entities=entities if _include("entities") else None,
        )
