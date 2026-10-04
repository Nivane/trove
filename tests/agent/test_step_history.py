"""step_history 裁剪纯函数:预算常量/上限/截断/透传(方案 ①)。"""

from trove.agent.step_history import (
    MAX_COLLECTED_STEPS,
    MAX_STEPS,
    MAX_STRING_CHARS,
    STEP_BUDGET_BYTES,
    TOTAL_BUDGET_BYTES,
    trim_steps,
)


def _step(seq: int, **detail) -> dict:
    return {
        "type": "step",
        "seq": seq,
        "node": "gen_sql",
        "elapsed_ms": 12,
        "lang": "zh",
        "detail": detail,
    }


def test_budget_constants_pinned():
    """预算数值即方案契约,钉死防漂移。"""
    assert MAX_STEPS == 60
    assert MAX_STRING_CHARS == 500
    assert STEP_BUDGET_BYTES == 3 * 1024
    assert TOTAL_BUDGET_BYTES == 64 * 1024
    assert MAX_COLLECTED_STEPS == 200


class TestTrimSteps:
    def test_empty(self):
        assert trim_steps([]) == ([], 0)

    def test_passthrough_under_budget(self):
        steps = [_step(i, sql=f"SELECT {i}") for i in range(3)]
        kept, truncated = trim_steps(steps)
        assert kept == steps
        assert truncated == 0
        # 副本语义:调用方的缓冲不被就地改
        assert kept is not steps
        assert kept[0] is not steps[0]

    def test_step_cap_keeps_prefix(self):
        steps = [_step(i) for i in range(MAX_STEPS + 7)]
        kept, truncated = trim_steps(steps)
        assert len(kept) == MAX_STEPS
        assert truncated == 7
        assert [s["seq"] for s in kept] == list(range(MAX_STEPS))

    def test_long_strings_slimmed(self):
        long_plan = "x" * (MAX_STRING_CHARS * 3)
        kept, truncated = trim_steps([_step(0, plan=long_plan)])
        assert truncated == 0
        assert len(kept[0]["detail"]["plan"]) == MAX_STRING_CHARS

    def test_oversized_step_falls_back_to_skeleton(self):
        # 10 个 500 字串(≈5KB+)超单步 3KB 预算 → 骨架(detail 清空)
        heavy = _step(0, fields=["y" * MAX_STRING_CHARS for _ in range(10)])
        kept, truncated = trim_steps([heavy])
        assert truncated == 0
        assert kept[0]["detail"] == {}
        assert kept[0]["node"] == "gen_sql"
        assert kept[0]["seq"] == 0
        assert kept[0]["type"] == "step"

    def test_total_budget_cuts_tail_by_prefix(self):
        # 每步 ≈2.6KB(5×500 字串,< 单步 3KB),64KB 总预算在约 25 步处截断
        steps = [_step(i, fields=["z" * 500 for _ in range(5)]) for i in range(60)]
        kept, truncated = trim_steps(steps)
        assert 0 < len(kept) < 60
        assert truncated == 60 - len(kept)
        assert [s["seq"] for s in kept] == list(range(len(kept)))
