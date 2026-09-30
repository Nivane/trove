"""答案级置信度:证据逐条、档位分带、I1~I8 断言(设计 2026-09-30)。"""

from __future__ import annotations

import pytest

from trove.agent.confidence import (
    DISCOUNT_FLOOR,
    TIER_BANDS,
    result_confidence,
    sql_confidence,
)
from trove.agent.answer_source import AnswerSource
from trove.workflow.state import WorkflowState


def make_state(**kwargs) -> WorkflowState:
    defaults = {
        "session_id": "s1", "question": "How many students are there?",
        "sql": "SELECT COUNT(*) FROM students",
        "columns": ["n"], "rows": [[42]], "row_count": 1, "verdict": "OK",
        # 档位由 output 判定后写进 state;这里直接给,单测不重复测 resolve
        "answer_source": AnswerSource.GENERATED.value,
        # 规则链跑过且全过 —— 这是"一路顺利"的基线。刻意写 True 而非留空:
        # 留空是 ``None``(没跑),那是另一条测试的对象(Task 6 的三态)。
        "rules_passed": True,
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def conf(source=AnswerSource.GENERATED, lang="zh", **kwargs):
    """``sql_confidence`` 的测试入口:档位显式传入。

    生产里这个参数就是 ``output`` 已经判定好的那个 ``AnswerSource``。它**不能**
    自己去 ``state.answer_source`` 里翻 —— 一轮开始时那个字段还是空串,自己翻的
    结果是每条答案都被当成 generated。
    """
    return sql_confidence(make_state(**kwargs), source, lang=lang)


class TestTierBands:
    def test_bands_do_not_overlap(self):
        """分带两两不重叠 —— 这是 I2 可断言的前提(设计 §5.2)。

        重叠的话「compiled 带 5 次编译缺口」会掉进 generated 的带里,于是
        分数与档位互相矛盾。
        """
        bands = sorted(TIER_BANDS.values(), key=lambda b: b[0])
        for (_, _, hi), (_, lo_next, _) in zip(bands, bands[1:]):
            # 严格小于。相等也算重叠:``_clamp`` 闭区间取值,``hi == lo_next``
            # 时上界那一分是**可达**的(见 TIER_BANDS 的注释)。
            assert hi < lo_next, f"分带重叠:hi={hi} >= 下一个 lo={lo_next}"

    def test_every_tier_contains_its_own_base(self):
        for source, (base, lo, hi) in TIER_BANDS.items():
            assert lo <= base <= hi, f"{source} 的基准 {base} 落在自己的带外"


class TestSqlConfidence:
    def test_no_sql_means_no_score(self):
        """I4:没有答案就没有分数。0.0 单义表示「没有答案」。"""
        score, evidence = conf(sql="")
        assert score == 0.0
        assert evidence == []

    def test_generated_base_has_no_evidence(self):
        """裸 generated:没有微调 ⟹ 证据为空(不编造理由)。"""
        score, evidence = conf()
        assert score == 0.5
        assert evidence == []

    def test_certified_base(self):
        score, _ = conf(AnswerSource.CERTIFIED)
        assert score == 0.95

    def test_self_check_adds_ten_points(self):
        score, evidence = conf(self_check_passed=1)
        assert score == pytest.approx(0.60)
        assert [e["name"] for e in evidence] == ["self_check"]

    def test_degraded_subtracts_ten_points(self):
        score, evidence = conf(generation_degraded=True)
        assert score == pytest.approx(0.40)
        assert evidence[0]["name"] == "generation_degraded"
        assert evidence[0]["effect"] == pytest.approx(-0.10)

    def test_degraded_primary_still_subtracts_when_no_candidate_was_adopted(self):
        """select 没采纳备选时交付的就是主候选 ⟹ 照扣。

        ``selection`` 的「缺席」形态是**空字典**(``state.py`` 里
        ``selection: dict[str, Any] = Field(default_factory=dict)`` —— 不是
        ``None``,往这个字段传 ``None`` 会被 pydantic 拒掉,所以这里不试它),
        覆盖快径 / simple 档 / KB 精确命中这些 select 不投票的路径。

        与下面那条**成对**:守卫只该放过 ``winner == "candidate"`` 这一种。
        反过来写成 ``if winner == "primary"`` 会把「select 没跑」也当成
        「不是主候选」而漏扣 —— 这条就是那个错误方向的钉子。
        """
        for selection in ({}, {"winner": "primary"}):
            score, evidence = conf(generation_degraded=True, selection=selection)
            assert score == pytest.approx(0.40), selection
            assert [e["name"] for e in evidence] == ["generation_degraded"]

    def test_adopted_candidate_is_not_penalised_for_the_primary_degrading(self):
        """select 采纳了备选 ⟹ 交付的 SQL 没经历过降级,I6 禁止虚低。

        Task 4 复审实测到的路径(``multi_candidate=True`` + ``agentic=True``:
        主候选第 1 轮降级、select 采纳备选):终态 ``sql`` 是**备选那条**,
        而 ``generation_degraded`` 仍为 ``True``。标记本身是对的(它说的是本轮
        主候选那条路),错的是**拿它折损另一条 SQL**。
        """
        score, evidence = conf(
            generation_degraded=True, selection={"winner": "candidate"},
        )
        assert score == pytest.approx(0.50)
        assert evidence == []

    def test_compile_misses_subtract_five_each(self):
        score, evidence = conf(
            AnswerSource.COMPILED,
            compile_misses=[{"component": "a"}, {"component": "b"}],
        )
        assert score == pytest.approx(0.85 - 0.10)
        assert evidence[0]["name"] == "compile_miss"
        assert "2" in evidence[0]["why"]

    def test_strong_retrieval_adds_five_points(self):
        """3.5 是**融合分**刻度(``det + 3·sim``),不是相似度 —— 见 Step 3 的注释。"""
        score, evidence = conf(gen_ctx={"examples": [{"score": 3.5}]})
        assert score == pytest.approx(0.55)
        assert evidence[0]["name"] == "strong_retrieval"

    @pytest.mark.parametrize("top", [1.0, 3.0])
    def test_weak_retrieval_is_not_evidence(self, top):
        """检索分低于阈值就**不倒扣也不记** —— 缺席不是坏消息(设计 §8-1)。

        ``1.0`` 不是随手挑的:**它是 ``_rank_examples`` 能返回的最低分**。
        那边先把 ``det = _score_example(...) -> int`` 算出来,``if det <= 0:
        continue`` 丢掉所有非正的,再返回 ``det + 3.0·sim``(``sim ≥ 0``)
        ⟹ **凡返回的候选,score 一律 ≥ 1.0**。阈值若被写回 ``0.8``(设计
        §5.2 的原始数字,那是**相似度**刻度),这条测试立刻转红 —— 这正是
        它要钉住的东西。
        """
        score, evidence = conf(gen_ctx={"examples": [{"score": top}]})
        assert score == 0.5
        assert evidence == []

    def test_strongest_example_wins_not_the_first(self):
        """读的是**最强**那条,不是 ``examples[0]``。

        ``_rank_examples`` 的 ``per_table`` 分支返回 ``(picks + rest)``,而
        ``picks`` 是**按表顺序**的每表 top1 —— ``[0]`` 是「第一个命中表里
        最好的那条」,不保证全场最高。第一条弱、第二条强时必须记证据。
        """
        score, evidence = conf(
            gen_ctx={"examples": [{"score": 1.0}, {"score": 3.5}]})
        assert score == pytest.approx(0.55)
        assert evidence[0]["name"] == "strong_retrieval"

    def test_why_copy_follows_lang(self):
        """设计 §5:文案是 i18n 的,随 ``state.lang``。

        英文档下 ``why`` 里**一个中日韩字符都不该有** —— 这条断言比「等于
        某个英文字符串」更耐改,却一样能把写死中文的实现钉红。
        """
        _, zh = conf(compile_misses=[{"component": "a"}], lang="zh")
        _, en = conf(compile_misses=[{"component": "a"}], lang="en")
        assert any("一" <= c <= "鿿" for c in zh[0]["why"])
        assert not any("一" <= c <= "鿿" for c in en[0]["why"])
        assert "1" in en[0]["why"]      # 数字照旧带着

    def test_missing_retrieval_is_not_evidence(self):
        """KB 空 / 检索关闭:证据条目缺省,不是 0 分(设计 §8-1)。"""
        score, evidence = conf(gen_ctx=None)
        assert score == 0.5
        assert evidence == []

    @pytest.mark.parametrize("source", list(AnswerSource))
    def test_adjustments_stay_inside_the_band(self, source):
        """I2:微调不跨档 —— **四档都要验**,且验的是「读得回自己那一档」。

        只测 generated 是不够的:它基价最低,是四档里唯一加满额也够不到上界的
        (0.50+0.15=0.65),风险恰好落在它测不到的地方 —— compiled 加满额
        0.85+0.15=1.00、reused 0.90+0.15=1.05,都会顶到上界。上界若与上一档
        的下界重合,同一个数字就同时属于两档,而 I5 取整之后用户看到的正是
        那个数字。
        """
        lo, hi = TIER_BANDS[source][1], TIER_BANDS[source][2]
        score, _ = conf(
            source, self_check_passed=1, gen_ctx={"examples": [{"score": 3.5}]})
        assert lo <= score <= hi
        higher = [b[1] for b in TIER_BANDS.values() if b[1] > lo]
        if higher:                      # 最高档之上没有别的档
            assert score < min(higher), "加满额后落进了上一档"

    def test_floor_holds_at_five_percent(self):
        """I8 下界:再多的负向证据也不压到 0.05 以下。"""
        score, _ = conf(
            generation_degraded=True,
            compile_misses=[{"component": str(i)} for i in range(20)],
        )
        assert score == pytest.approx(0.05)

    def test_band_floor_holds_overhead_downward(self):
        """I2 的**向下**方向:缺口吃满时夹在本档下界,不掉进下一档。

        上面那条 parametrize 只压上界。compiled 是唯一一个**下界离下一档
        很远**的档(0.70 对 generated 的上界 0.69,只差 0.01),而它同时
        又是缺口扣分最主要的承受者 —— 5 个软 MISS 就是 −0.25,底价 0.85
        扛不住。不夹的话它会跌到 0.60,落进 generated 的带里,于是
        「档位说 compiled、分数说 generated」—— I6 禁止的正是这种不自洽,
        只不过方向是**虚低**而非虚高(设计 §7.2 明确:虚高与虚低都要防)。
        """
        score, _ = conf(
            AnswerSource.COMPILED,
            compile_misses=[{"component": str(i)} for i in range(5)],
        )
        assert score == pytest.approx(0.70)
        assert score > TIER_BANDS[AnswerSource.GENERATED][2]

    def test_ceiling_holds_at_ninety_nine(self):
        """I8 上界:certified + 全额加分不越过 0.99(没执行完就不该满)。"""
        score, _ = conf(
            AnswerSource.CERTIFIED,
            self_check_passed=1, gen_ctx={"examples": [{"score": 3.5}]})
        assert score == pytest.approx(0.99)

    def test_every_adjustment_is_named_and_explained(self):
        """I1:分数 ≠ 基准 ⟹ 证据非空,且每条含 name / effect / why。"""
        score, evidence = conf(
            self_check_passed=1, generation_degraded=True,
            gen_ctx={"examples": [{"score": 3.5}]},
        )
        assert evidence, "分数偏离了基准却没有任何证据"
        for item in evidence:
            assert item["kind"] == "sql"
            assert item["name"] and item["why"]
            assert isinstance(item["effect"], float)


class TestResultConfidence:
    def test_no_sql_means_no_score(self):
        score, evidence = result_confidence(make_state(sql="", answer_source=""), 0.0)
        assert score == 0.0
        assert evidence == []

    def test_clean_run_equals_sql_confidence(self):
        """一路顺利 = 没有折损,结果置信度就是 SQL 置信度。"""
        score, evidence = result_confidence(make_state(), 0.5)
        assert score == 0.5
        assert evidence == []

    def test_never_exceeds_its_sql(self):
        """结果不会比它的 SQL 更可信 —— 这是「SQL × 折扣」而非两个独立
        分数的全部理由(设计 §5.3):两个独立分数会出现「SQL 0.5、结果 0.9」。

        **三条无折损的用例是不够的**:前三条里 ``result_confidence`` 走的都是
        「没有折扣」那条路(``product`` 恒为 1.0),断言退化成 ``x <= x`` ——
        它证明得了「没折扣时不虚高」,证明不了「**有折扣时**乘法真的发生了」。
        后三条带上真折扣(forced 0.5 / retry 0.9² / empty 0.7),让这条不变量
        真的走过连乘那条路。"""
        for kwargs in (
            {}, {"self_check_passed": 1}, {"retry_count": 0},
            {"forced": True}, {"retry_count": 2}, {"verdict": "EMPTY"},
        ):
            state = make_state(**kwargs)
            sql_score, _ = sql_confidence(state, AnswerSource.GENERATED)
            result, _ = result_confidence(state, sql_score)
            assert result <= sql_score

    def test_forced_halves_it(self):
        """强行交付(重试预算耗尽后接受 RETRY)是**最诚实**的低置信信号。"""
        score, evidence = result_confidence(make_state(forced=True), 0.5)
        assert score == pytest.approx(0.25)
        assert evidence[0]["name"] == "forced"
        assert evidence[0]["effect"] == pytest.approx(0.5)

    def test_disagreement_multiplies_by_the_vote_share(self):
        """候选不一致:折扣就是票王得票率(2:1 → 2/3)。"""
        score, evidence = result_confidence(
            make_state(consensus=True, selection={"confidence": 2 / 3}), 0.6)
        assert score == pytest.approx(0.4)
        assert evidence[0]["name"] == "vote_share"

    def test_missing_selection_is_not_a_discount(self):
        """单候选 / simple 档 / KB 精确命中:select 没跑,**不扣分**。

        它与「平局 1/3」必须分得开 —— 后者是真分歧,前者是压根没有分歧
        可言(设计 §8-2)。把缺席当 0 是把「没问」读成「答错」。
        """
        score, evidence = result_confidence(make_state(selection={}), 0.5)
        assert score == 0.5
        assert evidence == []

    def test_empty_result_discounts(self):
        score, _ = result_confidence(make_state(verdict="EMPTY"), 0.5)
        assert score == pytest.approx(0.35)

    def test_degraded_execution_discounts(self):
        """执行降级:`execution_evidence["verdict"] == "degrade"` ——
        与 ``output._degradation_notice`` **同一个门**(那里是
        ``if str(ev.get("verdict") or "") != "degrade": return ""``),不另立判据。

        正是这一步加了 LIMIT / 收窄了查询,答案的覆盖面被削过。
        """
        score, evidence = result_confidence(
            make_state(execution_evidence={"verdict": "degrade",
                                           "limit_applied": 1000}), 0.5)
        assert score == pytest.approx(0.425)
        assert evidence[0]["name"] == "execution_degraded"

    def test_pass_verdict_is_not_evidence(self):
        """放行的查询(``verdict == "pass"``)不留证据 —— 只记异常。"""
        score, evidence = result_confidence(
            make_state(execution_evidence={"verdict": "pass"}), 0.5)
        assert score == 0.5
        assert evidence == []

    def test_timeout_kill_discounts(self):
        score, _ = result_confidence(
            make_state(execution_evidence={"kill": True}), 0.5)
        assert score == pytest.approx(0.35)

    def test_retry_rounds_discount_compounds(self):
        score, evidence = result_confidence(make_state(retry_count=2), 0.5)
        assert score == pytest.approx(0.5 * 0.9 ** 2)
        assert evidence[0]["name"] == "retry"

    def test_discount_floor_holds(self):
        """I8:折扣叠加有下限 ×0.25,不压穿。

        没有它,``retry_count=10`` 会把任何分数压成 0 —— 而 0 是留给「没有
        答案」的码位(I4),不能被一个算出来的折扣占用。
        """
        score, evidence = result_confidence(
            make_state(forced=True, retry_count=10, verdict="EMPTY",
                       no_progress_rounds=1), 0.9)
        # 组合折扣远小于 0.25,被地板托住
        assert score == pytest.approx(max(0.05, 0.9 * DISCOUNT_FLOOR))

    def test_a_rules_failure_is_heavily_penalised(self):
        """``rules_passed is False`` = 跑了但没过 —— 交付路径上不该出现,
        出现即异常,故重罚。"""
        score, evidence = result_confidence(make_state(rules_passed=False), 0.5)
        assert score == pytest.approx(0.25)
        assert evidence[0]["name"] == "rules_not_passed"
        assert "未通过" in evidence[0]["why"]

    def test_rules_not_run_is_not_evidence(self):
        """``rules_passed is None`` = 规则链**没跑**,与「跑了没过」分开。

        ``empty`` 工作流是这条:调用方带着结果集进来,``_answer_source`` 非空、
        分数照算,而验证从没发生过。罚它等于**陈述一件没发生的事**——缺席不是
        坏消息(设计 §8-1),这条断言钉的就是 Review Focus 第 1 条。Task 6 之前
        这两件事同形,所以这条测试在 Task 6 落地前**不可能通过**。

        它测的是**纯函数契约**,不依赖某条生产路径真的把 ``None`` 送到交付点:
        HITL 否决虽然也没跑过 validate,但 ``output`` 里「见 ``intent_answer``
        就提前 return」那一句(``if state.intent_answer:`` 那次 return)把它挡在
        计分点之外(那正是 I4 要的三态)。
        """
        score, evidence = result_confidence(make_state(rules_passed=None), 0.5)
        assert score == 0.5
        assert evidence == []

    def test_rules_passed_is_not_evidence(self):
        """规则链全过是**默认**,不是加分项 —— 只记坏消息的反面同样成立:
        把默认值记成证据会让每条正常答案都背着一条噪声。"""
        score, evidence = result_confidence(make_state(rules_passed=True), 0.5)
        assert score == 0.5
        assert evidence == []

    def test_every_discount_is_named_and_explained(self):
        score, evidence = result_confidence(
            make_state(forced=True, verdict="EMPTY", retry_count=1), 0.8)
        for item in evidence:
            assert item["kind"] == "result"
            assert item["name"] and item["why"]
            assert 0 < item["effect"] <= 1.0

    def test_semantic_only_retry_is_a_surcharge_not_a_second_round(self):
        """裁决 29:``semantic_retries ⊆ retry_count`` —— 同一轮被记进两个
        计数器,这一行是**在轮次折扣之上的追加折扣**,不是第二个轮次。

        所以断言两件事,而不是一件:折扣**确实**两处都收(那是裁决 29 的决定,
        不是 bug),而文案**不许**把同一轮说成两轮 —— 后者是 Task 7 复审从
        披露面抓出来的:算术可辩护,渲染不可。
        """
        score, ev = result_confidence(
            make_state(retry_count=1, semantic_retries=1), 0.85)
        # 0.85 × 0.9(retry) × 0.9(semantic) = 0.6885 —— 追加量有界:最多一个 ×0.9
        assert score == pytest.approx(0.6885, abs=1e-4)
        assert {e["name"] for e in ev} == {"retry", "semantic_retry"}
        semantic = next(e for e in ev if e["name"] == "semantic_retry")
        assert "轮" not in semantic["why"]   # 文案不许再报一个轮次数

    def test_stalled_discounts_the_score(self):
        """``no_progress_rounds`` 非零 → 修正已无进展、提前止损(×0.8)。

        这一支此前零覆盖:分支零覆盖的折扣项在回归时不会变红。
        """
        score, ev = result_confidence(make_state(no_progress_rounds=2), 0.5)
        assert score == pytest.approx(0.5 * 0.8, abs=1e-4)
        assert [e["name"] for e in ev] == ["stalled"]
