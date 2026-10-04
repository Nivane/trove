"""DecisionBudget —— 判定预算的额度、优先级让路与配置自检(纯单测)。

账本语义本身由 ``analysis/budget.py::QueryLedger`` 承担(它有自己的
测试);这里钉的是**判定层的预算口径**:阶段顺序即优先级、总量盖不
满预留时靠后的先被切、以及 `validate` 的失败条件。
"""

from __future__ import annotations

from trove.services.decision.budget import STAGES, DecisionBudget


class TestAllocate:
    def test_default_budget_is_the_sum_of_stage_reserves(self):
        b = DecisionBudget()
        assert b.judge + b.significance + b.causal + b.bridge == b.total == 12

    def test_allocate_gives_a_ledger_with_the_total_as_hard_limit(self):
        ledger = DecisionBudget().allocate()
        assert ledger.total == 12
        assert ledger.can(12) and not ledger.can(13)
        for _ in range(3):
            ledger.record("judge")
        assert ledger.remaining() == 9

    def test_allocate_is_a_fresh_ledger_every_call(self):
        b = DecisionBudget()
        a1, a2 = b.allocate(), b.allocate()
        a1.record("judge")
        assert a2.used == 0

    def test_bad_total_degrades_to_zero_not_an_exception(self):
        # 配置面写坏不该让判定运行崩,而该让每个阶段都拿不到额度(响亮地
        # 降级)——总上限不是判定本身,判定的执行与否由服务层的错误语义管。
        ledger = DecisionBudget(total=None).allocate()   # type: ignore[arg-type]
        assert ledger.total == 0 and not ledger.can(1)


class TestYield:
    def test_yield_records_the_stage_and_remaining(self):
        ledger = DecisionBudget(total=2).allocate()
        ledger.record("judge")
        ledger.record("judge")
        assert not ledger.can(1)
        entry = ledger.yield_("significance", needed=1)
        assert entry["stage"] == "significance"
        assert entry["remaining"] == 0
        assert entry["reason"] == "query_budget_exceeded"
        assert ledger.snapshot()["yielded"] == [entry]

    def test_priority_order_puts_judge_first(self):
        # 顺序即优先级:判定永远最先花,让路的永远是它后面的阶段。
        assert STAGES[0] == "judge"
        assert STAGES.index("significance") < STAGES.index("bridge")


class TestAllowance:
    def test_full_budget_grants_every_stage_its_reserve(self):
        assert DecisionBudget().allowance() == {
            "judge": 2, "significance": 2, "causal": 4, "bridge": 4}

    def test_short_budget_starves_the_later_stages_first(self):
        cut = DecisionBudget(total=3).allowance()
        assert cut == {"judge": 2, "significance": 1, "causal": 0, "bridge": 0}

    def test_budget_that_cannot_cover_judge_starves_everything(self):
        cut = DecisionBudget(total=1).allowance()
        assert cut == {"judge": 1, "significance": 0, "causal": 0, "bridge": 0}


class TestValidate:
    def test_default_budget_is_valid(self):
        assert DecisionBudget().validate() == []

    def test_negative_reserve_is_flagged(self):
        issues = DecisionBudget(causal=-1).validate()
        assert issues and "causal" in issues[0]

    def test_total_below_judge_is_flagged(self):
        issues = DecisionBudget(total=1).validate()
        assert any("cannot cover the judge reserve" in i for i in issues)

    def test_bool_is_not_an_int(self):
        issues = DecisionBudget(judge=True).validate()   # type: ignore[arg-type]
        assert issues and "judge" in issues[0]
