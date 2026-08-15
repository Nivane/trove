"""Context budget assembly tests — priority fill + usage report."""

from trove.workflow.context_budget import assemble_blocks, estimate_tokens


class TestEstimateTokens:
    def test_rough_estimate(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("") == 1
        assert estimate_tokens("x" * 400) == 100


class TestAssembleBlocks:
    def test_priority_fill_within_budget(self):
        blocks = {"a": "x" * 100, "b": "y" * 100, "c": "z" * 100}
        included, usage = assemble_blocks(blocks, {"a": 1, "b": 2, "c": 3}, budget_tokens=60)
        # a (25) + b (25) = 50 ≤ 60；c (25) 超预算
        assert included == {"a", "b"}
        by_name = {u["name"]: u for u in usage}
        assert by_name["c"]["included"] is False
        assert by_name["a"]["included"] is True

    def test_all_included_when_plenty(self):
        blocks = {"a": "x" * 10, "b": "y" * 10}
        included, usage = assemble_blocks(blocks, {"a": 1, "b": 2}, budget_tokens=1000)
        assert included == {"a", "b"}
        assert all(u["included"] for u in usage)

    def test_empty_blocks(self):
        assert assemble_blocks({}, {}, 100) == (set(), [])
