"""答案级置信度披露(设计 2026-09-30)。

**决策一:只披露,不改行为。** 这两个分数不进任何条件分支 —— 判据是开/关两跑
``sql``/``rows``/``row_count``/``verdict``/``answer_source`` 逐字节相同(I7)。

**判定只有这一份。** 与 ``agent/answer_source.py`` 同构:两个分数都在 ``output``
一处算,SSE / 前端 / 评测 / markdown 同源同上文。分两处判迟早出现「行说 82%、
字段说 55%」——那比不披露更坏。

**开关为什么是进程级模块变量。** ``output`` 是模块级普通函数(不是
``make_xxx(...)`` 工厂),拿不到 ``AgentConfig``。仓库对这件事的既有答案是
``services/limits.py`` 的进程级镜像(它的 docstring 写明了理由),本模块沿用同一
范式:启动时与 admin 热更新时各同步一次。
"""

from __future__ import annotations

from typing import Any

from trove.agent.answer_source import AnswerSource
from trove.workflow.state import WorkflowState

_enabled = True


def set_confidence_enabled(flag: bool) -> None:
    global _enabled
    _enabled = bool(flag)


def confidence_enabled() -> bool:
    return _enabled


def reset_confidence_flag() -> None:
    """恢复默认(测试隔离)。"""
    global _enabled
    _enabled = True


#: I8:真分数的下界与上界。真分数不下探到 0,所以 ``0.0`` 永远只可能是
#: 「没有可披露的答案」—— 三态消歧因此是免费的,不需要额外的是否位。上界不到
#: 1.0:没执行完的预测不该是满分。
SCORE_FLOOR = 0.05
SCORE_CEIL = 0.99

#: 档位基准与**允许区间** ``(base, lo, hi)``,``hi`` 是**闭**区间那一端。
#: 分带互不重叠是刻意选择:重叠时「缺口多的 compiled」会落进 generated 的带,
#: 于是分数与档位互相矛盾 —— 而 I6 防的正是这种不自洽(虚高与虚低都是)。
#: 不重叠让 I2 从愿望变成可断言的性质(见 tests/agent/test_confidence.py)。
#:
#: **``hi`` 比下一档的 ``lo`` 低一个展示单位(0.01),而不是与它相等。**
#: ``_clamp`` 在闭区间上取值,``hi`` 一旦与下一档的 ``lo`` 重合,``reused``
#: 加满额(0.90+0.10+0.05=1.05)就会夹到 0.95 —— 那个数字同时属于 ``reused``
#: 与 ``certified``,而它正是用户看到的那个数字(I5 取整后仍是 95%)。
#: 让出的 0.01 在展示上不可见,却让「一个分数读得回它自己的档」真的成立。
#: 四档的可达区间都仍是设计 §5.2 那张表里区间的**子集**。
TIER_BANDS: dict[AnswerSource, tuple[float, float, float]] = {
    AnswerSource.CERTIFIED: (0.95, 0.95, SCORE_CEIL),
    AnswerSource.REUSED:    (0.90, 0.88, 0.94),
    AnswerSource.COMPILED:  (0.85, 0.70, 0.87),
    AnswerSource.GENERATED: (0.50, SCORE_FLOOR, 0.69),
}

#: 具名微调。**全部是判断,不是量出来的**(设计 §7.4)—— 不得在任何地方
#: 声称它们经过校准。P3 才有校准,那时改的是权重,不是结构。
_COMPILE_MISS_DELTA = -0.05   # 每个软 MISS 缺口
_SELF_CHECK_DELTA = 0.10      # agent 自检通过(check_result 全过)
_DEGRADED_DELTA = -0.10       # 降级到经典子图
_STRONG_RETRIEVAL_DELTA = 0.05
_STRONG_RETRIEVAL_SCORE = 0.8  # gen_ctx["examples"][0]["score"] 的阈值


def _clamp(value: float, lo: float = SCORE_FLOOR, hi: float = SCORE_CEIL) -> float:
    return max(lo, min(hi, value))


def _evidence(kind: str, name: str, effect: float, why: str) -> dict[str, Any]:
    """一条具名证据。

    ``effect`` 的单位**随 kind 变**:``kind="sql"`` 是带符号的**加法增量**;
    ``kind="result"`` 是**乘性因子**。两者形状相同、读法不同 —— 由 kind 决定,
    消费方(前端 / SSE)按 kind 分派,不要靠猜。
    """
    return {"kind": kind, "name": name, "effect": effect, "why": why}


def _sql_adjustments(state: WorkflowState) -> list[dict[str, Any]]:
    """SQL 置信度的具名微调(正负都记)。"""
    out: list[dict[str, Any]] = []
    misses = len(state.compile_misses or [])
    if misses:
        out.append(_evidence(
            "sql", "compile_miss", _COMPILE_MISS_DELTA * misses,
            f"语义编译有 {misses} 个组件未解析,由模型补齐",
        ))
    if state.self_check_passed:
        out.append(_evidence(
            "sql", "self_check", _SELF_CHECK_DELTA,
            f"agent 自检 {state.self_check_passed} 次通过规则链",
        ))
    # 降级标记说的是**本轮的主候选那条路**降级了(Task 4 的注释:「本轮**是否**
    # 降级」)。若 select 随后采纳了某个备选候选,交付出去的是**备选的那条 SQL**
    # —— 它没经历过降级(备选生成失败时返回 ``None``,不走兜底到经典子图那条路),
    # 拿这次降级去折损它,就是 I6 禁止的**虚低**(与 Task 3/4 修的两个方向相反)。
    #
    # ``winner`` 只有 ``"primary"`` / ``"candidate"`` 两个取值(全部写点在
    # ``nodes/select.py``);``selection`` 缺席(快径 / simple 档 / KB 精确命中 ——
    # select 没投票)时 ``winner`` 是 ``None``,此时交付的就是主候选,该扣。
    # 本守卫由 Task 4 复审实测发现:``multi_candidate=True`` + ``agentic=True``
    # 下主候选第 1 轮降级、select 采纳备选,终态 ``sql`` 是备选那条而标记仍为
    # ``True`` —— 复审用一个插桩跑实测出来的,不是推演。
    winner = (state.selection or {}).get("winner")
    if state.generation_degraded and winner != "candidate":
        out.append(_evidence(
            "sql", "generation_degraded", _DEGRADED_DELTA,
            "本轮主候选的 agent 循环未走完,降级到单发生成",
        ))
    # 检索分:只在**够强**时才是证据。低于阈值与「拿不到检索分」都不记 ——
    # 缺席不是坏消息(设计 §8-1),倒扣会让「KB 空」被读成「答案差」。
    examples = (state.gen_ctx or {}).get("examples") or []
    top = float(examples[0].get("score") or 0) if examples else 0.0
    if top >= _STRONG_RETRIEVAL_SCORE:
        out.append(_evidence(
            "sql", "strong_retrieval", _STRONG_RETRIEVAL_DELTA,
            f"检索到高度相关的参考示例(相似度 {top:.2f})",
        ))
    return out


def sql_confidence(
    state: WorkflowState, source: AnswerSource,
) -> tuple[float, list[dict[str, Any]]]:
    """SQL 置信度:执行前,「这条 SQL 有多可信」。

    骨架**就是** ``answer_source`` 的四档(不另造排序,I2):档位说的「从哪来」
    与分数说的「有多可信」在这种情况下不是两个判断,是一个判断的两种表述。
    微调只在**本档的带内**生效 —— 越档会让分数与档位打架。

    ``state.sql`` 为空 ⟹ ``(0.0, [])``:没有答案就没有分数(I4),而不是 0 分。

    **``source`` 为什么是参数,而不是自己从 state 里读**:因为**写这个字段的
    正是 ``output`` 自己** —— ``:565`` 的返回字典里那句 ``"answer_source": ...``
    是它唯一的写点,而它在 ``:531`` 就把档位读出来了:读在自己那次写之前。
    ``output`` 是终局节点,哪条路由进来都只跑一次,所以函数里自读到的永远是字段
    默认值 ``""``(``state.py:267``),而不是**刚判定出来的那一档** ——
    ``AnswerSource("")`` 只会抛 ValueError,不是"读到了但读错了"。
    参数化让档位只有一个来源:``output`` 里那个**已经判好**的
    ``_answer_source(state)``,与 ``_build_details(state, source)`` 用的是同一个
    值。判一次,用两处。
    """
    if not state.sql:
        return 0.0, []
    base, lo, hi = TIER_BANDS[source]
    evidence = _sql_adjustments(state)
    score = _clamp(base + sum(e["effect"] for e in evidence), lo, hi)
    return score, evidence
