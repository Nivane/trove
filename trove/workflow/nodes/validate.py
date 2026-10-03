"""Deterministic validation node — rule checks between execute and reflect.

Rule failures use the same error_feedback correction channel as
execution errors (shared retry budget): the regenerated SQL gets the
concrete rule reason in its prompt. This is the code-side counterpart
to the LLM reflect judge — what can be checked deterministically
should not be left to the model.

层2 的列检查以 query_sketch 的结构化计划为输入,所以它**可能没有输入**:
散文计划(模型没按 JSON 输出)下 ``plan_json is None``,两个检查函数一律返回
``[]``。此时本节点写 ``plan_validation.status = "untyped"`` 并记日志 —— 降级
可以说,但不能不说(A1-3)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.i18n import L
from trove.core.logging import get_logger
from trove.llm.observability import record_span
from trove.services.skills.validators import (
    VALIDATOR_HOST,
    format_hit,
    run_validators,
)
from trove.workflow.nodes.query_sketch import answer_columns_mismatch, extra_columns_mismatch
from trove.workflow.rules import REWRITE_RULES as _REWRITE_RULES
from trove.workflow.rules import verify as run_rules
from trove.workflow.state import WorkflowState, budget_exhausted

logger = get_logger(__name__)


def _authoritative_sql(state: WorkflowState) -> tuple[bool, str]:
    """这条 SQL 是否**就是要照抄的那条权威编译 SQL**(零 LLM)。

    改写型规则(``_REWRITE_RULES``)对它的豁免前提 —— 判据必须比粘滞位
    ``state.compiled`` 强:那个布尔值只说明"某轮编译过",而豁免是**放松**
    一道守卫,只有"这一条 SQL 确实复现了本轮契约"才配。

    逐项(全过才算):
      ① ``compiled`` 且非 ``compile_partial``;
      ② ``compile_meta.outcome == "compiled"``(miss / partial 都不算);
      ③ wire 契约解得出来(``contract_from_wire`` 形状异常一律 None);
      ④ 契约不是 advisory / partial(Lane A 的降级档:advisory 骨架允许
         投影/过滤放宽,不能当权威照抄对象;键缺失 → 老 checkpoint 行为);
      ⑤ 契约带形状签名 —— 无签名时 ``compiled_sql_matches`` 是**放行**
         (编译期就抽不出结构),拿一个不校验的契约去豁免等于双重放松;
      ⑥ ``compiled_sql_matches``:这条 SQL 复现契约的结果形状。

    判据缺失/抽取失败 → 不豁免(与 execute_sql 的保真校验同一条纪律:
    读不到契约就退回老路径,不静默放松)。
    """
    from trove.services.semantic_layer.compiler import compiled_sql_matches
    from trove.services.semantic_layer.contract import contract_from_wire

    if not state.compiled or state.compile_partial:
        return False, "not a full compile"
    meta = state.compile_meta or {}
    if meta.get("outcome") != "compiled":
        return False, f"outcome={meta.get('outcome') or 'missing'}"
    contract = contract_from_wire(state.contract)
    if contract is None:
        return False, "no contract"
    if getattr(contract, "advisory", False) or contract.partial:
        return False, "advisory/partial contract"
    if contract.signature is None:
        return False, "contract without shape signature"
    if not state.sql:
        return False, "empty SQL"
    ok, why = compiled_sql_matches(contract, state.sql, state.dialect)
    return (True, "") if ok else (False, why or "SQL does not reproduce the contract")


def make_validate_rules(
    max_retries: int = 10,
    skills: Any | None = None,
) -> Callable[[WorkflowState], Awaitable[dict[str, Any]]]:
    """Build the validate node.

    Args:
        max_retries: Shared correction budget — rule failures feed back
            to gen_sql while retry_count < max_retries; once exhausted,
            failures degrade gracefully via state.error.
        skills: Optional ``SkillService``. When present, confirmed org
            skills at ``tier: validator`` run as a **parallel pass** after
            the deterministic rule chain — blocking verdicts share this
            node's feedback channel and retry budget (no new node, no new
            graph edge); advisory verdicts and "cannot evaluate" go to
            ``state.validator_hits`` only.

    The verify_step assertion layer reports structured hits
    (rule name + reason) via state.validation_hits for eval attribution.
    """

    async def validate(state: WorkflowState) -> dict[str, Any]:
        # Upstream failure / pending execution feedback — pass through
        if state.error or state.error_feedback:
            return {}

        # 权威编译 SQL 的改写型规则豁免(收窄):这条 SQL 是"逐字照抄"对象
        # (execute_sql 的编译保真校验),而改写型规则命中即要求**改写它** ——
        # 两条断言互斥,honoring 规则只会烧修正预算(0480 实测 10 轮死锁)。
        # 豁免命中**只 log + span**:不进 error_feedback(不烧 retry)、不写
        # validation_hits(那是 replay.tried_recovery 的判据,混入非拦截事件
        # 会污染 eval 归因)。结果域规则从不豁免 —— 它们判的是行。
        authoritative, auth_reason = _authoritative_sql(state)
        exempted: list[dict[str, Any]] = []
        # 规则链结果进 langfuse(hits = 规则名+原因,即修正指令的证据)
        with record_span(
            "rules.verify",
            input={"question": state.question, "sql": state.sql},
        ) as span:
            reason, hits = run_rules(
                state.question, state.sql, state.columns, state.rows, state.row_count,
                lang=state.lang,
                exempt=_REWRITE_RULES if authoritative else (),
                skipped=exempted,
            )
            if span is not None:
                span.update(output={
                    "passed": reason is None,
                    "failures": hits,
                    # 权威 SQL 被豁免的改写型命中 + 判定依据:豁免是"少拦了
                    # 一次",必须留痕(降级可以说,但不能不说)。
                    "authoritative": authoritative,
                    "authoritative_reason": auth_reason,
                    "exempted": exempted,
                })
        if exempted:
            logger.info(
                "rewrite rules exempted for authoritative compiled SQL (%r): %s",
                state.question[:80],
                "; ".join(str(e.get("name")) for e in exempted),
            )
        if reason is not None:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": reason, "rules_passed": False}
            feedback = L(
                state.lang,
                f"校验规则: {reason}",
                f"Validation rule: {reason}",
            )
            return {
                "error_feedback": feedback,
                "retry_count": state.retry_count + 1,
                "correction_history": [feedback],
                "validation_hits": hits,
                "rules_passed": False,
            }

        # 层2:answer_columns 与执行结果列的一致性检查(plan 的钦点列必须
        # 能在结果里看到;全部缺失才是冲突——别名/表达式会制造单列噪音,
        # 全缺才是 SELECT 列表整体背离计划的强信号)。走 analyze_error
        # 通道,反馈文本把归因指向 query_sketch 的 answer_columns。
        ac_errors = answer_columns_mismatch(state.plan_json, state.columns)
        if ac_errors:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": "; ".join(ac_errors), "rules_passed": False}
            feedback = L(
                state.lang,
                f"计划校验: {'; '.join(ac_errors)}。"
                "查询计划的 answer_columns 与执行结果列不符——重新规划并修正输出列。",
                f"Plan check [answer-columns]: {'; '.join(ac_errors)}. "
                "The query plan's answer_columns do not match the executed "
                "result columns — re-plan and fix the answer columns.",
            )
            return {
                "error_feedback": feedback,
                "retry_count": state.retry_count + 1,
                "correction_history": [feedback],
                "validation_hits": [{
                    "rule": "answer-columns",
                    "reason": "; ".join(ac_errors),
                }],
                "rules_passed": False,
            }

        # 层2补充:结果列"多余"检查(plan 的 answer_columns 必须覆盖结果列;
        # 结果多出计划外的列 → 打回重规划)。保守:全部 refs 都在结果里才
        # 判定;question 点名列豁免——宁漏勿误,误伤成本=一次重试轮。
        extra_errors = extra_columns_mismatch(
            state.plan_json, state.columns, state.question, state.sql,
        )
        if extra_errors:
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": "; ".join(extra_errors), "rules_passed": False}
            feedback = L(
                state.lang,
                f"计划校验: {'; '.join(extra_errors)}。"
                "只输出查询计划的 answer_columns——去掉多余列。",
                f"Plan check [extra-columns]: {'; '.join(extra_errors)}. "
                "Output only the plan's answer_columns — drop the extra columns.",
            )
            return {
                "error_feedback": feedback,
                "retry_count": state.retry_count + 1,
                "correction_history": [feedback],
                "validation_hits": [{
                    "rule": "extra-columns",
                    "reason": "; ".join(extra_errors),
                }],
                "rules_passed": False,
            }

        # ── org validator 档（管理员维护的结果断言）────────────────────
        #
        # **位置**：必须在这一段与 untyped 分支之间。untyped 分支是 ``return``
        # （见下），插在它之后 org validator 会在散文计划下静默消失 —— 而
        # 散文计划恰恰是确定性守卫最少、最需要它兜底的时候。同理 untyped 与
        # 全过两条 return 都要**合并**这里的结果，不能各返回各的。
        #
        # **为什么是平行一遍而不是往 _RULES 里加规则**：规则链是"第一条失败
        # 即止、只回一个 reason"，装不下逐条 severity（blocking 拦截 /
        # advisory 只报告）。复用它的通道，不复用它的形状。
        org_hits: list[dict[str, Any]] = []
        if skills is not None:
            # 宿主名与声明的宿主(``VALIDATOR_HOST``,写入校验与 validators_for
            # 都用它)共用同一个字面量 —— 两处各写一份字符串正是"声明的宿主"与
            # "实际调用点"漂移的入口(``align_schema`` 那类事故)。
            org_hits = run_validators(
                skills.validators_for(VALIDATOR_HOST, **state.skill_ctx()),
                columns=state.columns,
                rows=state.rows,
                row_count=state.row_count,
                lang=state.lang,
            )
        # 只有**明确违反**的 blocking 才拦。"判不了"(verdict is None)绝不拦 ——
        # 拿不准就拦下正确结果,比不检查更坏。
        blocking = [
            h for h in org_hits
            if h["severity"] == "blocking" and h["verdict"] is False
        ]
        # 有 SkillService 时**总是**重写该字段(零命中即空列表):trigger 维度
        # 里的 complexity 会被修正轮强制成 standard(graphs.py:390-392),所以
        # "上一轮命中、这一轮选不出人"是常规路径。不写键 = 上一轮的判词活到
        # 交付答案上,渲染成一条针对它从未检查过的结果的告警。
        # skills is None 时逐字保持旧形状(省略键)——那是既有的向后兼容契约。
        vh = {"validator_hits": org_hits} if skills is not None else {}
        if blocking:
            # ``format_hit``:判词是手写 YAML 的自由文本 —— 空名字会拼出
            # ``[] 判词``,换行会在预算耗尽那条路上把用户的错误卡片劈成两行
            # (另一处同一渲染在 ``output.py::_validator_notice``)。
            joined = "; ".join(format_hit(h) for h in blocking)
            if budget_exhausted(state.retry_count, max_retries):
                return {"error": joined, "rules_passed": False, **vh}
            feedback = L(
                state.lang,
                f"校验规则: {joined}。",
                f"Validation rule: {joined}.",
            )
            return {
                "error_feedback": feedback,
                "retry_count": state.retry_count + 1,
                "correction_history": [feedback],
                # 进 validation_hits 是对的 —— 它**确实**是一次拦截,
                # 语义与 replay 的归因判据相符。
                "validation_hits": [
                    {"rule": f"validator:{h['name']}", "reason": h["message"]}
                    for h in blocking
                ],
                "rules_passed": False,
                **vh,
            }

        # 列检查"本该跑却跑不了"——必须说出来。
        #
        # answer_columns_mismatch / extra_columns_mismatch 都以 ``plan_json``
        # 为输入,而它在散文计划(query_sketch 没按 JSON 输出)下是 None;两个
        # 函数对 None 一律返回 [],于是**整层列守卫静默消失**。回答照常交付,
        # 只是少了那道闸,而且外部看不出来。
        #
        # 判据用 ``columns`` 而不是"有没有 plan":没有结果列就无所谓列检查
        # (快径/未启用 query_sketch 的路径都落在这一侧),不该被记一笔。
        # 注意 ``compiled=True`` 不会走到这里——它只在 plan_json 是 dict 时置位
        # (query_sketch.py),那条路由契约负责校验。
        #
        # 状态写进已有的 ``plan_validation`` 通道:query_sketch 用同一字段报
        # "dropped"(plan 没过 schema 校验,此时 plan 被清空、列检查同样无从跑)
        # / "ok"。**不写 validation_hits** —— 那个通道的语义是"被规则拦过",
        # 是 eval 恢复机制归因的判据(trove/eval/replay.py::tried_recovery),
        # 混进非拦截事件会污染归因。
        if state.plan_json is None and state.columns:
            logger.info(
                "column guards skipped (no typed plan) for %r",
                state.question[:80],
            )
            return {
                "rules_passed": True,
                "plan_validation": {
                    "status": "untyped",
                    "reason": "no typed plan — answer/extra column checks skipped",
                },
                **vh,
            }

        # 全过:确定性规则链 + 层2 计划检查全通过 → 显式正向信号,
        # reflect 据此(配合复杂度)决定是否跳过 LLM 裁决。
        return {"rules_passed": True, **vh}

    return validate
