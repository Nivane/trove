"""Retrieval ranking metrics — pure functions, zero IO (no LLM, no network).

Recall@k / MRR / nDCG@k over ``(gold_doc_ids, ranked_doc_ids)`` pairs. Used by
``scripts/eval_hybrid_retrieval.py`` to score both the RRF-fused order and the
reranked order (before/after 精排对比), and by ``scripts/tune_rrf.py`` to grid
search RRF weights/k. Same conventions as corvid's ``eval/metrics.py`` so the
two projects' numbers are directly comparable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

#: 默认评估截断点(与 corvid / 通用 RAG 评测一致)。
DEFAULT_KS = (1, 3, 5, 10)


def recall_at_k(gold: Iterable[str], ranked: Sequence[str], k: int) -> float:
    """top-k 是否含任一 gold doc(二值 recall@k)。"""
    g = set(gold)
    if not g:
        return 0.0
    return 1.0 if any(x in g for x in ranked[:k]) else 0.0


def mrr(gold: Iterable[str], ranked: Sequence[str]) -> float:
    """第一个 gold doc 的位置倒数(未命中 → 0)。"""
    g = set(gold)
    for i, x in enumerate(ranked, start=1):
        if x in g:
            return 1.0 / i
    return 0.0


def ndcg_at_k(gold: Iterable[str], ranked: Sequence[str], k: int) -> float:
    """二进制相关性的 nDCG@k:相关 = gold 命中。

    DCG = sum rel_i / log2(i+1);IDCG 假设全部相关文档排在最优位置。
    """
    g = set(gold)
    rel = [1.0 if x in g else 0.0 for x in ranked[:k]]
    dcg = sum(r / (1.0 if i == 0 else _log2(i + 1)) for i, r in enumerate(rel))
    n_rel = min(len(g), k)
    idcg = sum(1.0 / (1.0 if i == 0 else _log2(i + 1)) for i in range(n_rel))
    return dcg / idcg if idcg > 0 else 0.0


def _log2(x: float) -> float:
    import math

    return math.log2(x)


def evaluate(
    gold_by_query: dict[str, Iterable[str]],
    ranked_by_query: dict[str, Sequence[str]],
    ks: tuple[int, ...] = DEFAULT_KS,
) -> dict:
    """按 query 聚合平均 Recall@k / MRR / nDCG@k。

    Args:
        gold_by_query: query → gold doc_id 集合。
        ranked_by_query: query → 排序后的 doc_id 列表。

    Returns:
        {"n": .., "recall@k": {..}, "mrr": float, "ndcg@k": {..}, "zero_recall": int}
    """
    queries = [q for q in gold_by_query if q in ranked_by_query]
    n = len(queries)
    out: dict = {"n": n}
    recall: dict[int, float] = {k: 0.0 for k in ks}
    ndcg: dict[int, float] = {k: 0.0 for k in ks}
    mrr_sum = 0.0
    zero = 0
    for q in queries:
        gold = list(gold_by_query[q])
        ranked = list(ranked_by_query[q])
        for k in ks:
            recall[k] += recall_at_k(gold, ranked, k)
            ndcg[k] += ndcg_at_k(gold, ranked, k)
        m = mrr(gold, ranked)
        mrr_sum += m
        if m == 0.0:
            zero += 1
    out["recall@k"] = {k: (recall[k] / n) if n else 0.0 for k in ks}
    out["ndcg@k"] = {k: (ndcg[k] / n) if n else 0.0 for k in ks}
    out["mrr"] = (mrr_sum / n) if n else 0.0
    out["zero_recall"] = zero
    return out
