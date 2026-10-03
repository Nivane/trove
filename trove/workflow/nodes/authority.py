"""权威编译产物判据 —— 被两处守卫共用(零 LLM)。

同一个问题(「这条 SQL 是不是就是要照抄的那条权威编译 SQL」)有两个问者:

- ``validate`` 的改写型规则豁免(``_REWRITE_RULES``:命中的规则要求**改写**
  这条 SQL,而照抄对象不能被改写 —— 0480 实测 10 轮死锁);
- ``reflect`` 的复杂度档豁免(编译产物 + 规则全过 = 语义层权威答案,
  不该再交给 LLM 判官做语义再裁决 —— 0492 实测:与 gold 逐字节相等的
  结果仅因 ``complexity=complex`` 超过 ``reflect_skip=standard`` 档被判官
  打回,回滚后反而变错)。

两处是**同一条**判据,判据只有一份(本模块)——分开写迟早会漂移成
两套"什么算权威"的定义。本模块不 import validate / reflect,避免环。
"""

from __future__ import annotations

from trove.workflow.state import WorkflowState


def authoritative_compiled_sql(state: WorkflowState) -> tuple[bool, str]:
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
