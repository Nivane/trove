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
from trove.core.i18n import L
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
#: 「检索证据强」的阈值。设计 §5.2 的原意是 ``sim ≥ 0.8``,但 ``examples`` 里那个
#: ``score`` **不是相似度**:它是 ``rerank_score(det, sim) = det + 3.0·sim``
#: (``services/kb/embeddings.py`` 的 ``_RERANK_WEIGHT``),而 ``det`` 是
#: ``_score_example(...) -> int`` 的**正整数**(``service.py`` 里 ``if det <= 0:
#: continue`` 把所有非正的都丢了)⟹ **凡返回的候选,score 一律 ≥ 1.0**。
#: 于是 0.8 这个阈值**恒真、等于没有条件**:只要 KB 返回了任何一条,它就 +0.05。
#: 把它写成 0.8 是照抄设计里那个数字,而设计的论证还引了一处「已在
#: ``graphs.py:485``」的既有阈值 —— 实测那一行是**把 score 抄进 dict 的管道**
#: (``"score": float(getattr(h, "score", 0) or 0)``),全仓并没有这样一个阈值。
#:
#: 落到**真实刻度**上,同一份意图是:``sim ≥ 0.8`` ⟹ ``score ≥ 1 + 3×0.8 = 3.4``。
#: 这是**下界** —— 满足设计原意的必过,另有一类确定性命中强、语义相似度不高的
#: 也过。后者正是本行该收的:本行的名字是「检索**证据**强」,确定性锚点同样是
#: 证据。3.4 与 1.0 之间是真空档:弱候选(``det`` 小、``sim`` 低)确实被挡在外面。
_STRONG_RETRIEVAL_SCORE = 3.4


def _clamp(value: float, lo: float = SCORE_FLOOR, hi: float = SCORE_CEIL) -> float:
    return max(lo, min(hi, value))


def _evidence(kind: str, name: str, effect: float, why: str) -> dict[str, Any]:
    """一条具名证据。

    ``effect`` 的单位**随 kind 变**:``kind="sql"`` 是带符号的**加法增量**;
    ``kind="result"`` 是**乘性因子**。两者形状相同、读法不同 —— 由 kind 决定,
    消费方(前端 / SSE)按 kind 分派,不要靠猜。
    """
    return {"kind": kind, "name": name, "effect": effect, "why": why}


def _sql_adjustments(state: WorkflowState, lang: str) -> list[dict[str, Any]]:
    """SQL 置信度的具名微调(正负都记)。

    ``why`` 是**给人读的文案**,所以随 ``lang`` 走(设计 §5「文案(i18n,
    随 ``state.lang``)」)。它是中英混排事故的现场:``why`` 在这里写死中文,
    而 ``render_line`` 只会按 ``lang`` 换标签与括号 —— 英文用户会读到
    ``*Confidence: 60% (agent 自检 1 次通过规则链)*``。文案必须有 ``lang``
    在手,而只有 ``output`` 知道 ``state.lang``,所以一路传进来。
    """
    out: list[dict[str, Any]] = []
    misses = len(state.compile_misses or [])
    if misses:
        out.append(_evidence(
            "sql", "compile_miss", _COMPILE_MISS_DELTA * misses,
            L(lang, f"语义编译有 {misses} 个组件未解析,由模型补齐",
               f"{misses} semantic component(s) unresolved, filled in by the model"),
        ))
    if state.self_check_passed:
        out.append(_evidence(
            "sql", "self_check", _SELF_CHECK_DELTA,
            L(lang, f"agent 自检 {state.self_check_passed} 次通过规则链",
               f"agent self-check passed the rule chain "
               f"{state.self_check_passed}x"),
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
            L(lang, "本轮主候选的 agent 循环未走完,降级到单发生成",
               "this round's primary agent loop did not finish; "
               "fell back to single-shot generation"),
        ))
    # 检索分:只在**够强**时才是证据。低于阈值与「拿不到检索分」都不记 ——
    # 缺席不是坏消息(设计 §8-1),倒扣会让「KB 空」被读成「答案差」。
    examples = (state.gen_ctx or {}).get("examples") or []
    # 取 ``max``,不是 ``examples[0]``:``per_table`` 分组时 ``_rank_examples``
    # 返回 ``(picks + rest)``,而 ``picks`` 是**按表顺序**的每表 top1 ——
    # ``[0]`` 是「第一个命中表里最好的那条」,不一定是全场最高分,与这里
    # 「最强的那条」以及下面那句证据说的不是一回事。
    top = max((float(e.get("score") or 0) for e in examples), default=0.0)
    if top >= _STRONG_RETRIEVAL_SCORE:
        out.append(_evidence(
            "sql", "strong_retrieval", _STRONG_RETRIEVAL_DELTA,
            # 原本这里是 ``f"...(相似度 {top:.2f})"``。``top`` 是**融合分**
            # (``det + 3·sim``),不是相似度 —— 实测同一句话在 demo KB 上会
            # 渲染成「相似度 13.00」。披露行里展示一个不在 [0,1] 的「相似度」
            # 会连累整条披露的可信度,而这个数字对用户本就没有意义(机器面有
            # ``name`` 与 ``effect``)。所以**去掉数字**,只留结论。
            L(lang, "检索到强相关的参考示例",
               "strongly relevant reference examples retrieved"),
        ))
    return out


def sql_confidence(
    state: WorkflowState, source: AnswerSource, *, lang: str = "zh",
) -> tuple[float, list[dict[str, Any]]]:
    """SQL 置信度:执行前,「这条 SQL 有多可信」。

    骨架**就是** ``answer_source`` 的四档(不另造排序,I2):档位说的「从哪来」
    与分数说的「有多可信」在这种情况下不是两个判断,是一个判断的两种表述。
    微调只在**本档的带内**生效 —— 越档会让分数与档位打架。

    ``state.sql`` 为空 ⟹ ``(0.0, [])``:没有答案就没有分数(I4),而不是 0 分。

    **``lang`` 为什么也是参数**:证据里的 ``why`` 是要**原样渲染给用户**的文案,
    而设计 §5 明写「文案(i18n,随 ``state.lang``)」。这个模块拿不到 ``state``
    之外的东西,``output`` 也拿不到 —— 它手上只有 ``state``,``lang`` 就在里面,
    所以是调用方把 ``state.lang`` 传进来(默认为 ``"zh"``,与
    ``core/config.py`` 的默认语言一致,测试里可以省略)。

    **``source`` 为什么是参数,而不是自己从 state 里读**:因为**写这个字段的
    正是 ``output`` 自己** —— 它返回字典里那句 ``"answer_source": ...`` 是这个
    字段**唯一**的写点,而它**读**档位的那一行(``source = _answer_source(state)``)
    在同一次调用里更靠前 —— 读在自己那次写之前。
    ``output`` 是终局节点,哪条路由进来都只跑一次,所以函数里自读到的永远是字段
    默认值 ``""``(``state.py`` 里 ``answer_source: str = ""`` 那个字段),而不是
    **刚判定出来的那一档** ——
    ``AnswerSource("")`` 只会抛 ValueError,不是"读到了但读错了"。
    参数化让档位只有一个来源:``output`` 里那个**已经判好**的
    ``_answer_source(state)``,再把它交给需要档位的每一处(披露行、
    ``_build_details`` 的折叠区),而不是各自去 state 里翻。
    **"每一处"从 Task 8 起才成立**:本任务落地时 ``output`` 还没有第二处
    消费方,传参的收益要到那里才兑现 —— 这里先把形态定下来,不是为了
    眼下那个调用点。
    (三处位置引用改内容锚点:``:565``/``:531`` 实测是 565/534,而 **Task 8 要改
    那个返回字典与 ``_build_details`` 的签名** —— 报数字必再漂。)
    """
    if not state.sql:
        return 0.0, []
    base, lo, hi = TIER_BANDS[source]
    evidence = _sql_adjustments(state, lang)
    score = _clamp(base + sum(e["effect"] for e in evidence), lo, hi)
    return score, evidence


#: I8 的折损下限。没有它,`retry_count=10` 的连乘会把分数压到 0 —— 而 0 是
#: 留给「没有可披露的答案」的码位(I4),不能被一个算出来的折扣占用。
DISCOUNT_FLOOR = 0.25

_FORCED_DISCOUNT = 0.5        # 重试预算耗尽后强行交付 —— 最诚实的低置信信号
_EMPTY_DISCOUNT = 0.7
_EXEC_DEGRADED_DISCOUNT = 0.85
_KILL_DISCOUNT = 0.7
_RETRY_DISCOUNT = 0.9         # 每轮修正
_STALLED_DISCOUNT = 0.8       # 已止损
_RULES_FAILED_DISCOUNT = 0.5  # 交付路径上不该出现,出现即异常


def _result_discounts(state: WorkflowState, lang: str) -> list[dict[str, Any]]:
    """结果置信度的具名折损(乘性)。**只记异常**,正常项不记。

    ``lang`` 的来路与 ``_sql_adjustments`` 完全相同:``why`` 是要原样渲染给
    用户的文案(设计 §5「文案(i18n,随 ``state.lang``)」),而只有 ``output``
    手里有 ``state.lang``,所以一路传进来。

    回归 0.9^N:``retry_count`` 是**本轮累计**的修正次数,不是历史轮次之和
    —— 图状态在每个用户轮次按 ``model_dump()`` 重置,所以这里不需要额外
    的去重逻辑。
    """
    out: list[dict[str, Any]] = []
    # 候选分歧:折扣就是票王得票率。``selection`` 为空 ⟹ select 没跑
    # (单候选 / simple 档 / KB 精确命中)—— **不扣分**,把缺席当 0 是把
    # 「没问」读成「答错」(设计 §8-2)。
    share = (state.selection or {}).get("confidence")
    if isinstance(share, (int, float)) and share < 1.0:
        out.append(_evidence(
            "result", "vote_share", float(share),
            L(lang, f"多候选结果不完全一致(票王得票率 {float(share):.0%})",
               f"multi-candidate results disagreed "
               f"(winning share {float(share):.0%})"),
        ))
    if state.forced:
        out.append(_evidence(
            "result", "forced", _FORCED_DISCOUNT,
            L(lang, "重试预算耗尽后强行交付",
               "delivered after the retry budget ran out"),
        ))
    if state.verdict == "EMPTY":
        out.append(_evidence(
            "result", "empty_result", _EMPTY_DISCOUNT,
            L(lang, "查询返回 0 行", "the query returned 0 rows"),
        ))
    # 执行期降级:与 ``output._degradation_notice`` 用**同一个门**(那里是
    # ``str(ev.get("verdict")) != "degrade"`` 就放行),不另立判据 —— 两处
    # 各判一次迟早出现「正文说降级了、置信度没扣」。
    ev = state.execution_evidence or {}
    if str(ev.get("verdict") or "") == "degrade":
        out.append(_evidence(
            "result", "execution_degraded", _EXEC_DEGRADED_DISCOUNT,
            L(lang, "执行期降级,结果被加了行数上限或收窄",
               "execution degraded; the result was row-capped or narrowed"),
        ))
    if ev.get("kill"):
        out.append(_evidence(
            "result", "timeout_kill", _KILL_DISCOUNT,
            L(lang, "执行超时被中止", "execution was killed on timeout"),
        ))
    if state.retry_count:
        out.append(_evidence(
            "result", "retry", _RETRY_DISCOUNT ** state.retry_count,
            L(lang, f"经过 {state.retry_count} 轮修正才交付",
               f"delivered after {state.retry_count} correction round(s)"),
        ))
    # ``semantic_retries`` **不是**与 ``retry_count`` 并列的另一批轮次 ——
    # 纯语义 RETRY 那条 return(``reflect.py`` 里同时写 ``retry_count + 1``
    # 与 ``semantic_retries`` 的那个 dict)把**同一轮**记进了两个计数器,
    # 所以 ``semantic_retries ⊆ retry_count``:这一条是**在轮次折扣之上的
    # 追加折扣**,读作「纯语义重试比机械错误更可疑」,不是重复计费两次
    # ——那是设计 §5.3 表里两行各自的措辞(「修正轮」/「纯语义重试」)。
    # 追加量有界:``MAX_SEMANTIC_RETRIES = 2`` 时计数到 2 即**清零并置
    # ``forced``**,所以能活到交付点的 ``semantic_retries`` 恰为 1 ——
    # 追加折扣最多一个 ×0.9。
    if state.semantic_retries:
        out.append(_evidence(
            "result", "semantic_retry", _RETRY_DISCOUNT ** state.semantic_retries,
            L(lang, f"经过 {state.semantic_retries} 轮纯语义重试",
               f"delivered after {state.semantic_retries} "
               f"semantic-only retry round(s)"),
        ))
    if state.no_progress_rounds:
        out.append(_evidence(
            "result", "stalled", _STALLED_DISCOUNT,
            L(lang, "修正已无进展,提前止损",
               "corrections stopped making progress; bailed out early"),
        ))
    # **必须用 ``is False``,不能写 ``if not state.rules_passed``**:
    # ``None``(没跑)与 ``False``(跑了没过)是两件事,后者才该罚(设计 §8-1)。
    if state.rules_passed is False:
        out.append(_evidence(
            "result", "rules_not_passed", _RULES_FAILED_DISCOUNT,
            L(lang, "结果未通过确定性规则链",
               "the result failed the deterministic rule chain"),
        ))
    return out


def result_confidence(
    state: WorkflowState, sql_score: float, *, lang: str = "zh",
) -> tuple[float, list[dict[str, Any]]]:
    """结果置信度:交付前,「这个结果有多可信」。

    **= SQL 置信度 × Π(具名折损)**,不是独立的第二个分数 —— 这样两个数天然
    自洽:结果不会比它的 SQL 更可信(设计 §5.3)。反过来(两个独立分数)会出现
    「SQL 0.5 而结果 0.9」这种读不通的组合。

    折损连乘有下限 ``DISCOUNT_FLOOR``,结果再 clamp 到 ``[0.05, 0.99]``(I8)。
    """
    if not state.sql:
        return 0.0, []
    discounts = _result_discounts(state, lang)
    product = max(DISCOUNT_FLOOR, _product(e["effect"] for e in discounts))
    return _clamp(sql_score * product), discounts


def _product(factors) -> float:
    out = 1.0
    for f in factors:
        out *= f
    return out
