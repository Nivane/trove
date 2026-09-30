"""答案级置信度:证据逐条、档位分带、I1~I8 断言(设计 2026-09-30)。"""

from __future__ import annotations

import pytest

from trove.agent.confidence import TIER_BANDS, sql_confidence
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


def conf(source=AnswerSource.GENERATED, **kwargs):
    """``sql_confidence`` 的测试入口:档位显式传入。

    生产里这个参数就是 ``output`` 已经判定好的那个 ``AnswerSource``。它**不能**
    自己去 ``state.answer_source`` 里翻 —— 一轮开始时那个字段还是空串,自己翻的
    结果是每条答案都被当成 generated。
    """
    return sql_confidence(make_state(**kwargs), source)


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
        score, evidence = conf(gen_ctx={"examples": [{"score": 0.9}]})
        assert score == pytest.approx(0.55)
        assert evidence[0]["name"] == "strong_retrieval"

    def test_weak_retrieval_is_not_evidence(self):
        """检索分低于阈值就**不倒扣也不记** —— 缺席不是坏消息(设计 §8-1)。"""
        score, evidence = conf(gen_ctx={"examples": [{"score": 0.3}]})
        assert score == 0.5
        assert evidence == []

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
            source, self_check_passed=1, gen_ctx={"examples": [{"score": 1.0}]})
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

    def test_ceiling_holds_at_ninety_nine(self):
        """I8 上界:certified + 全额加分不越过 0.99(没执行完就不该满)。"""
        score, _ = conf(
            AnswerSource.CERTIFIED,
            self_check_passed=1, gen_ctx={"examples": [{"score": 1.0}]})
        assert score == pytest.approx(0.99)

    def test_every_adjustment_is_named_and_explained(self):
        """I1:分数 ≠ 基准 ⟹ 证据非空,且每条含 name / effect / why。"""
        score, evidence = conf(
            self_check_passed=1, generation_degraded=True,
            gen_ctx={"examples": [{"score": 0.9}]},
        )
        assert evidence, "分数偏离了基准却没有任何证据"
        for item in evidence:
            assert item["kind"] == "sql"
            assert item["name"] and item["why"]
            assert isinstance(item["effect"], float)
