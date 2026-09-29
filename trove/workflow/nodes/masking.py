"""脱敏节点 —— 结果集的最后一站(设计 §5.5 G4 / I5)。

**为什么是一个节点,而不是挂在 ``select`` 后面。** 设计原文写的是「``select``
节点之后串 ``Masker.apply``」,落地时改了,理由是快径:``select`` 在
``state.candidates`` 为空时**直接返回** ``{}``(单候选 / 快径命中那条路),挂在
它里面的后置步在那条路上根本不执行 —— 与 P2 修的 G3 是同一类洞(快径绕过了
本该系统性地发生的后置步)。所以脱敏做成**图上的一个节点**,位置在 ``validate``
的成功分支之后、任何面向 LLM 的节点之前:

* refusal 图:``validate → masking → reflect``(reflect 的输入里有 ``rows``);
* fixed 图:``validate → masking → attribution``。

**为什么在 ``validate`` 之后而不是之前**:规则链判的是「这行数据对不对」,
``null`` 模式会把列抹掉、``partial`` 会改长度 —— 先脱敏再判,等于让规则在一份
被改写过的数据上下结论(空值率、类型检查全都会误判)。顺序是:先判数据,
再脱敏给人看。

**为什么在 LLM 节点之前**:I5。``insights`` / ``conclusion`` 拿到的必须是脱敏
后的行,否则模型会在结论里复述身份证号 —— 那时再做任何拦截都晚了(文本已经
生成)。这也解释了为什么本节点不调 LLM、也不问 LLM 任何问题。

失败方向(§8.1 判据:有没有下层兜底)
------------------------------------

脱敏没有下层兜底 —— 没有第二个东西会在结果出库前检查「这列该不该改写」。
所以两处都取严:

* ``hash`` 要应用而 salt 解析不出来 → **拒绝**(``MaskingError``,§10:降级即
  泄漏);
* 语义模型读不出来(provider 抛异常)→ **拒绝**。读不出来与「没有声明面」是
  两回事:后者是没接语义层的部署(惰性,A11),前者是故障 —— 把故障当惰性,
  等于让一次 provider 抖动变成一次 PII 披露。

**拒绝时连结果集一起清空**。下游的 ``insights`` / ``chart`` / ``conclusion``
确实都看 ``state.error``,但那是节点之间的约定;原文一旦留在 state 里,将来
任何一个新节点忘了这条约定就是一次泄漏。清空把约定变成结构。
"""

from __future__ import annotations

from typing import Any, Callable

from trove.core.config import AgentConfig
from trove.core.logging import get_logger
from trove.core.metrics import record_masking_applied
from trove.services.authz.masking import MaskingError, build_masker
from trove.workflow.state import WorkflowState

log = get_logger(__name__)

__all__ = ["make_masking"]

#: 拒绝时的错误码(与 ``AUTHZ_*`` 同一套 ``[ERR:<id>]`` 约定,见 §10)。
ERR_TAG = "[ERR:MASKING]"


def _refusal(reason: str) -> dict[str, Any]:
    """拒绝 + **清空结果集**(见模块 docstring 的失败方向)。"""
    return {"error": f"{ERR_TAG} {reason}", "rows": [], "columns": []}


def make_masking(
    *,
    semantic_layer: Any | None = None,
    config: AgentConfig | None = None,
) -> Callable[[WorkflowState], Any]:
    """脱敏节点工厂。

    Args:
        semantic_layer: 语义层提供方(只用 ``model()``)。``None`` = 没接语义层
            → 没有声明面 → 节点惰性(不是拒绝:没声明过脱敏的部署本来就无事
            可做,A11)。
        config: 读 ``masking.enabled`` 与部署级 ``masking.hash_salt_ref``。
    """
    # 构造收在 build_masker 里(与 select 的预览脱敏共用同一份规则)。
    # ``None`` = 部署级关闭 → 节点整体惰性。
    apply_masking = build_masker(semantic_layer=semantic_layer, config=config)

    async def masking(state: WorkflowState) -> dict[str, Any]:
        if apply_masking is None:
            return {}
        if not state.rows:
            # 没有结果集(未执行 / 空表 / 已在上一步失败)→ 没有可改写的行,
            # 也不写报告:Named「这一步没产出结论」与「产出了『什么都没改』
            # 的结论」是两种状态(同 execution_evidence 的三态纪律)。
            return {}
        try:
            rows, report = apply_masking(
                state.rows, state.columns,
                principal=state.principal,
                sql=state.sql,
                contract=state.contract,
            )
        except MaskingError as e:
            log.warning(
                "masking refused run %s for datasource %r: %s",
                state.run_id, state.datasource, e,
            )
            return _refusal(str(e))

        for field, mode in (report.get("fields") or {}).items():
            record_masking_applied(field, str(mode))
        return {"rows": rows, "masking_applied": report}

    return masking
