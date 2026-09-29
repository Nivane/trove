"""答案来源四档披露:certified / reused / compiled / generated(设计 §6.4 / §7.3 / I6)。

**为什么是四档而不是设计稿的三档**:三档把「机制」(这条 SQL 从哪来)与「有没有人
背书」压进了同一个维度,于是两条根本没经过模型的路径 —— KB 精确命中、非 certified
的模板命中 —— 都掉进 GENERATED,显示「来源: LLM 生成」。修订把两个问题拆开:
CERTIFIED 答背书,REUSED 答来源。拆开之后「复用了多少」这件事才第一次可披露,
而它正是这套能力的标题。

**判定只有这一份**。判定点不止一处(答案渲染、SSE 事件、评测归因、台账落账、
双跑诊断),每处各写一段 if,迟早一处升档一处不升 —— 而 I6(置信度不得虚高)是
这套披露唯一的资产:用户对「已认证」的信任被看见虚高一次,三档就一起作废。

**判定不出的一律 GENERATED**。方向刻意不对称:把编译器的产物标低,用户少得
一点信息;把模型编的标高,用户得到一个不该有的信任。所以每一处「拿不到证据」
的分支都倒向最保守的那一档,而不是倒向「看起来更像的那一档」。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from trove.core.i18n import L
from trove.workflow.state import WorkflowState


class AnswerSource(str, Enum):
    """一条答案的来源档位(取值即 wire 契约:答案文本 / SSE / 台账落账同一份)。"""

    CERTIFIED = "certified"   # 复用了一条**已认证**模板
    REUSED = "reused"         # 复用了知识库资产(模板或示例),未认证
    COMPILED = "compiled"     # 语义编译器拼出的权威 SQL
    GENERATED = "generated"   # LLM 生成(含编译失败回退、软 MISS 骨架)


# ── 判定 ─────────────────────────────────────────────────


def for_template_status(status: str) -> AnswerSource:
    """快径命中的模板 → 档位:``certified`` 升 CERTIFIED,其余一律 REUSED。

    **两档答的不是同一个问题**,这是本函数唯一需要说清的事:

    * CERTIFIED 答「有没有人**具名背书**」—— 所以只有 ``status == "certified"``
      才配,模板是确定性产物不算数(语义编译也是确定性的,一样没人背书,§8.4);
    * REUSED 答「这条 SQL **从哪来**」—— 由「模板命中」这件事本身保证,与治理
      字段写没写对无关。

    早先的实现把两者压成一档,于是 ``draft`` / 空 / 坏元数据的模板命中都落进
    GENERATED,对用户显示「来源: LLM 生成」:一条**根本没进过模型**的 SQL 被说成
    模型编的。披露装置的资产是它说的话可信;说一次假话,四档一起作废(I6 的
    反面 —— I6 防虚高,这里防虚**低**,而虚低同样是在编造机制)。

    认不出的取值仍然**不升 CERTIFIED**:坏元数据没有资格声称有人认证。
    """
    return AnswerSource.CERTIFIED if status == "certified" else AnswerSource.REUSED


def for_compile(*, compiled: bool, partial: bool, latest_outcome: str) -> AnswerSource:
    """编译通道 → 档位。三个条件**同时**成立才升 COMPILED。

    三个条件各挡一类虚高:

    * ``compiled`` —— 这一轮根本没编译出权威 SQL(编译 MISS 后模型自己写),没有
      编译器产物可声称;「含编译失败回退」按契约也落在这一档;
    * ``latest_outcome == "compiled"`` —— **最新一轮**编译的结论。``compiled`` 是
      粘滞位:query_sketch 只在编译成功时置 True,重跑 MISS 时**不回清**。只看
      ``compiled``,「先编译成功 → 一路回退重编译失败 → 最后模型写出答案」的运行
      会把模型写的 SQL 标成编译器产物;
    * ``not partial`` —— 软 MISS 骨架:权威结构(join/过滤/分组)之外还有模型补的
      组件,这条 SQL 不全是编译器产物。

    **修正轮不额外降档**:执行层对编译通道有照抄校验(``execute_sql`` 的
    ``compiled_sql_matches``),偏离编译结构的 SQL 会被打回重生成 —— 能执行到最后
    的,就仍是编译器定下的那份结构。
    """
    if not compiled or partial:
        return AnswerSource.GENERATED
    if latest_outcome != "compiled":
        return AnswerSource.GENERATED
    return AnswerSource.COMPILED


# 只认最后一条模板命中:一轮里快径最多命中一次,取最后一条与取唯一一条等价;
# 真出现多条时,后来者才是这一轮 SQL 的来源。其它 kind(term/example)身上就算
# 恰好有个 ``status`` 键,也与模板的治理状态无关 —— 判据是 kind,不是键名。
def _template_status(kb_hits: list[dict[str, Any]]) -> str:
    for hit in reversed(kb_hits or []):
        if isinstance(hit, dict) and hit.get("kind") == "template":
            return str(hit.get("status") or "")
    return ""


def resolve(state: WorkflowState) -> AnswerSource:
    """三档判定的唯一入口。

    **快径优先**:两条路径语法互斥(§8.5),真同时成立时以快径为准 —— 它才是
    这一轮 SQL 的实际来源。快径命中标记在、命中证据不在(状态被裁剪 / 旧
    checkpoint)时,``_template_status`` 返回空串 → 保守 GENERATED(I6)。
    """
    # KB 精确命中要**排在快径前面**判:它跳过模型生成直接取示例 SQL
    # (``graphs.py:925``),但 ``compiled`` 是粘滞位、可能在这一轮之前就置上了 ——
    # 只看编译位会把「逐字复用的示例」说成「编译器产物」。两者都没经过模型,
    # 可来源不同,而披露的全部内容就是来源。
    if state.kb_exact_match:
        return AnswerSource.REUSED
    if state.fast_path:
        return for_template_status(_template_status(state.kb_hits))
    return for_compile(
        compiled=state.compiled,
        partial=state.compile_partial,
        latest_outcome=str((state.compile_meta or {}).get("outcome") or ""),
    )


# ── 文案 ─────────────────────────────────────────────────

#: §7.3 的三句原话。**刻意不说「准确」**(R5):certified 说的是「有人验证过」,
#: 不是「这次是对的」—— 前者是台账里的事实,后者我们无法承诺。
_COPY: dict[AnswerSource, tuple[str, str]] = {
    AnswerSource.CERTIFIED: ("来源: 已认证模板", "Source: certified template"),
    AnswerSource.REUSED: ("来源: 复用知识库资产", "Source: reused knowledge-base asset"),
    # §7.3 原文这里是「语义编译 · 未命中资产库」。后半句是**事实断言**,而它可以是
    # 假的:走了编译路径 ≠ 示例检索一定为空(检索可能命中、只是这一轮没采用)。
    # 披露行只该说机制能保证的部分 —— 与 REUSED 那一档同一个道理。
    AnswerSource.COMPILED: ("来源: 语义编译", "Source: semantic compile"),
    AnswerSource.GENERATED: ("来源: LLM 生成 · 建议核对", "Source: LLM generated · please verify"),
}


def source_line(
    source: AnswerSource,
    *,
    lang: str = "zh",
    runs: int | None = None,
    p50_ms: int | None = None,
) -> str:
    """来源行(设计 §7.3)。统计段是**三态**的,与 ``output._freshness_line`` 同一套纪律:

    * 台账拿不到(``runs is None``)—— 统计一个字都不写,只报档位;
    * 跑过、但没有耗时样本(``p50_ms is None``)—— 只写命中次数,不写 P50;
    * 都有 —— 写全。

    「没查过」与「查过、是 0」必须分得开:拿不到台账时渲染成「命中 0 次」,是在
    替台账说话 —— 用户会读成「这条认证资产从没被用过」,而事实是我们没查。

    ``runs`` / ``p50_ms`` 由调用方从台账取(§6.2)。拿不到就不传,这一档的文案
    自然退化成「已认证模板」—— 披露的**档位**不依赖统计,统计只是加分项。
    """
    zh, en = _COPY[source]
    label = L(lang, zh, en)
    if source is AnswerSource.CERTIFIED:
        stats: list[str] = []
        if runs is not None:
            stats.append(L(lang, f"命中 {runs} 次", f"{runs} runs"))
        if p50_ms is not None:
            stats.append(f"P50 {p50_ms}ms")
        if stats:
            label = f"{label} · {' · '.join(stats)}"
    return f"*{label}*\n"
