"""Context budget assembly tests — priority fill + usage report."""

from trove.workflow.context_budget import (
    ContextItem,
    assemble_blocks,
    assemble_context,
    count_tokens,
    estimate_tokens,
)


class TestEstimateTokens:
    def test_rough_estimate(self):
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("") == 1
        assert estimate_tokens("x" * 400) == 100

    def test_cjk_weighted_heuristic(self):
        # 中文按 2 字符加权:400 个中文字 ≈ 800 字符当量 → 200 tokens
        assert estimate_tokens("中" * 400) == 200


class TestCountTokens:
    def test_min_one(self):
        assert count_tokens("") == 1

    def test_falls_back_to_char_estimate_without_tokenizer(self):
        # 与 estimate_tokens 同口径(无 tokenizer 或 encoding 缺失时)
        assert count_tokens("x" * 400) >= 100

    def test_cjk_not_undercounted(self):
        # 中文绝不会比 4 字符/token 的口径更低(避免低估超预算)
        assert count_tokens("中" * 400) >= 100


class TestAssembleBlocks:
    def test_priority_fill_within_budget(self):
        blocks = {"a": "x" * 100, "b": "y" * 100, "c": "z" * 100}
        included, usage = assemble_blocks(
            blocks, {"a": 1, "b": 2, "c": 3}, budget_tokens=60, count=estimate_tokens,
        )
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


class TestAssembleContext:
    """Item-level trimming: a block keeps its best items instead of
    all-or-nothing when the budget is tight."""

    def _items(self, scores, n=0):
        """scores: list of (score, text_len) → ContextItems keyed shot<i>."""
        return [
            ContextItem(key=f"shot{i}", text="x" * ln, score=sc)
            for i, (sc, ln) in enumerate(scores)
        ]

    def test_keeps_high_score_items_within_budget(self):
        # 每条 25 tokens；预算 60 → 高分 3 条(75)塞不下,取前 2 条
        blocks = {"shots": self._items([(9, 100), (5, 100), (1, 100)])}
        included, usage = assemble_context(
            blocks, {"shots": 1}, budget_tokens=60, count=estimate_tokens,
        )
        assert included == {"shots": ["shot0", "shot1"]}
        assert usage[0]["items_total"] == 3
        assert usage[0]["items_included"] == 2

    def test_low_score_item_dropped_high_kept(self):
        # 预算只够 1 条:最低分的被裁剪
        blocks = {"shots": self._items([(1, 100), (9, 100)])}
        included, _ = assemble_context(
            blocks, {"shots": 1}, budget_tokens=30, count=estimate_tokens,
        )
        assert included == {"shots": ["shot1"]}

    def test_skip_item_that_does_not_fit_keep_smaller(self):
        # 高分长条目超预算 → 被跳过,后续能塞下的小条目保留(而非整块丢弃)
        blocks = {"shots": self._items([(9, 2000), (1, 100)])}
        included, _ = assemble_context(
            blocks, {"shots": 1}, budget_tokens=300, count=estimate_tokens,
        )
        assert included == {"shots": ["shot1"]}

    def test_block_priority_order_still_respected(self):
        # 低优先级块(history)整体塞不下 → 被排除,高优先级仍在
        blocks = {
            "shots": self._items([(5, 100)]),
            "history": [ContextItem(key="history", text="h" * 2000, score=0.0)],
        }
        included, usage = assemble_context(
            blocks, {"shots": 1, "history": 2}, budget_tokens=60,
        )
        assert included == {"shots": ["shot0"]}
        by_name = {u["name"]: u for u in usage}
        assert by_name["history"]["included"] is False

    def test_empty_blocks_ignored(self):
        assert assemble_context({}, {}, 100) == ({}, [])
        assert assemble_context({"a": []}, {"a": 1}, 100) == ({}, [])

    def test_usage_reports_item_counts(self):
        blocks = {"shots": self._items([(9, 100), (1, 100)])}
        _, usage = assemble_context(
            blocks, {"shots": 1}, budget_tokens=30, count=estimate_tokens,
        )
        assert usage == [{
            "name": "shots", "tokens": 25, "included": True,
            "items_total": 2, "items_included": 1,
        }]
