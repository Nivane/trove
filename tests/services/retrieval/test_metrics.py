"""Tests for retrieval ranking metrics (pure functions, no IO)."""

import math

import pytest

from trove.services.retrieval.metrics import (
    evaluate,
    mrr,
    ndcg_at_k,
    recall_at_k,
)


def test_recall_at_k():
    assert recall_at_k(["a"], ["a", "b"], 1) == 1.0
    assert recall_at_k(["a"], ["b", "a"], 1) == 0.0
    assert recall_at_k(["a"], ["b", "a"], 2) == 1.0
    assert recall_at_k([], ["a"], 1) == 0.0


def test_mrr():
    assert mrr(["a"], ["a", "b"]) == pytest.approx(1.0)
    assert mrr(["a"], ["b", "a"]) == pytest.approx(0.5)
    assert mrr(["x"], ["a", "b"]) == 0.0


def test_ndcg_orders_relevant_first():
    # 单个 gold 排在第 1 vs 排在第 3 → ndcg 前大后小
    first = ndcg_at_k(["a"], ["a", "b", "c"], 3)
    last = ndcg_at_k(["a"], ["b", "c", "a"], 3)
    assert first == pytest.approx(1.0)
    assert last < first
    assert last == pytest.approx(1.0 / math.log2(3))  # 1 个相关排第 3 位


def test_evaluate_aggregates():
    gold = {"q1": ["a"], "q2": ["x", "y"]}
    ranked = {"q1": ["a", "b"], "q2": ["z", "y"]}
    m = evaluate(gold, ranked, ks=(1, 3))
    assert m["n"] == 2
    assert m["recall@k"][1] == pytest.approx(0.5)  # q1 命中, q2 top1 未命中
    assert m["recall@k"][3] == pytest.approx(1.0)
    assert m["mrr"] == pytest.approx(0.75)  # 1.0 + 0.5
    assert m["zero_recall"] == 0
