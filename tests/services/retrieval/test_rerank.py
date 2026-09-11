"""Tests for rerankers and RRF fusion (no external services)."""

import math

import pytest

from trove.services.kb.embeddings import cosine
from trove.services.retrieval.rerank import (
    BgeReranker,
    CrossEncoderReranker,
    DeterministicReranker,
)
from trove.services.retrieval.store import (
    RetrievalHit,
    normalize_scores,
    rrf_fuse,
    rrf_scores,
)


class FakeEmbedder:
    """Deterministic bag-of-chars embedder (normalized, dim 16)."""

    dim = 16

    async def embed(self, texts):
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            for ch in t:
                vec[ord(ch) % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def _hit(doc_id, content, score=0.0, kind="kb"):
    return RetrievalHit(doc_id=doc_id, content=content, score=score, kind=kind)


async def test_deterministic_rerank_orders_by_coverage():
    reranker = DeterministicReranker()
    q = "贷款 平均 金额"
    hits = [
        _hit("a", "无关的天气内容 今天下雨"),
        _hit("b", "贷款 金额 的平均 值 是 多少"),
        _hit("c", "贷款"),
    ]
    out = await reranker.rerank(q, hits, k=3)
    assert out[0].doc_id == "b"
    assert out[0].score >= out[1].score >= out[2].score


async def test_cross_encoder_rerank_by_cosine():
    emb = FakeEmbedder()
    reranker = CrossEncoderReranker(emb)
    q = "贷款平均金额"
    hits = [
        _hit("a", "完全不相关的话题 足球比赛"),
        _hit("b", "贷款 平均 金额 怎么 算"),
    ]
    out = await reranker.rerank(q, hits, k=2)
    assert out[0].doc_id == "b"
    assert out[0].score == pytest.approx(cosine(
        (await emb.embed([q]))[0], (await emb.embed(["贷款 平均 金额 怎么 算"]))[0]))


def test_rrf_fuse_combines_lists():
    fused = rrf_fuse([["x", "y", "z"], ["y", "x", "w"]])
    # x and y appear in both lists → highest fused scores
    assert fused[0] in ("x", "y")
    assert set(fused) == {"x", "y", "z", "w"}


def test_rrf_fuse_single_channel():
    fused = rrf_fuse([["a", "b"]])
    assert fused == ["a", "b"]


def test_rrf_fuse_respects_weights():
    fused = rrf_fuse([["a", "b"], ["b", "a"]], k=60, weights=[1.0, 10.0])
    assert fused[0] == "b"


def test_rrf_scores_spread_is_narrow():
    """RRF 分本身压缩在极窄区间 —— 这正是必须归一化的理由。"""
    scores = rrf_scores([["a", "b", "c"], ["a", "c", "b"]], k=60)
    assert len(set(scores.values())) > 1
    assert max(scores.values()) - min(scores.values()) < 0.05


def test_normalize_scores_spreads_to_unit_range():
    scores = normalize_scores({"a": 0.033, "b": 0.030, "c": 0.022})
    assert scores["a"] == pytest.approx(1.0)
    assert scores["c"] == pytest.approx(0.0)
    assert scores["b"] == pytest.approx((0.030 - 0.022) / 0.011)


def test_normalize_scores_all_equal_is_one():
    """全等(或只有一条)没有可分辨的相对信息 → 一律 1.0,而不是 0。"""
    assert normalize_scores({"a": 0.03, "b": 0.03}) == {"a": 1.0, "b": 1.0}
    assert normalize_scores({"a": 0.03}) == {"a": 1.0}


def test_normalize_scores_empty():
    assert normalize_scores({}) == {}


def test_bge_reranker_requires_flag_embedding():
    try:
        import FlagEmbedding  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="FlagEmbedding"):
            BgeReranker()._load()
    else:  # 环境装了 FlagEmbedding:直接走真实加载(不下发重载验证)
        pytest.skip("FlagEmbedding installed; skipping lazy-import check")
