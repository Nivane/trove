"""Tests for embedder / reranker / channel resolution (build_store / helpers)."""

from trove.core.types import DatasourceConfig
from trove.services.kb.backends.dense import (
    BgeM3Embedder,
    GatewayEmbedder,
    build_embedder,
)
from trove.services.retrieval import (
    BgeReranker,
    CrossEncoderReranker,
    DeterministicReranker,
    PgHybridStore,
    SqliteHybridStore,
)
from trove.services.retrieval.factory import _reranker_for, build_store, channel_cfg


def _cfg(**kw):
    return DatasourceConfig(name="d", type="sqlite", **kw)


def test_build_embedder_api():
    emb = build_embedder(_cfg(embedding_model="text-embedding-3-small"), gateway="gw")
    assert isinstance(emb, GatewayEmbedder)


def test_build_embedder_bge_m3():
    emb = build_embedder(_cfg(embedder_backend="bge-m3"), gateway=None)
    assert isinstance(emb, BgeM3Embedder)


def test_build_embedder_none_when_no_model():
    assert build_embedder(_cfg(), gateway=None) is None


def test_reranker_default_is_off():
    """默认不精排:默认档曾是确定性 n-gram,与下游 _sim 同源,纯重复计算。"""
    assert _reranker_for(_cfg(), None) is None
    assert _reranker_for(_cfg(rerank_backend="auto"), None) is None
    assert _reranker_for(_cfg(rerank_backend="none"), None) is None


def test_reranker_deterministic_still_opt_in():
    assert isinstance(
        _reranker_for(_cfg(rerank_backend="deterministic"), None),
        DeterministicReranker)


def test_reranker_unknown_backend_disables():
    """未知后端名不该悄悄打开一个精排器。"""
    assert _reranker_for(_cfg(rerank_backend="wat"), None) is None


def test_reranker_bge_backend():
    r = _reranker_for(_cfg(rerank_backend="bge", rerank_model="m"), None)
    assert isinstance(r, BgeReranker)


def test_reranker_http_backend():
    r = _reranker_for(_cfg(rerank_backend="http", rerank_endpoint="https://x"), None)
    assert isinstance(r, CrossEncoderReranker)
    assert r._endpoint == "https://x"


def test_channel_cfg_roundtrip():
    cfg = _cfg(rrf_k=100, rrf_weights={"keyword": 1.5, "dense": 1.0})
    rrf_k, weights = channel_cfg(cfg)
    assert rrf_k == 100
    assert weights == {"keyword": 1.5, "dense": 1.0}


async def test_build_store_sqlite_recorder(tmp_path):
    cfg = _cfg(embedder_backend="bge-m3")
    store = build_store(cfg, None, tmp_path)
    assert isinstance(store, SqliteHybridStore)
    assert store._recorder is not None


async def test_build_store_pg_dsn(tmp_path):
    cfg = DatasourceConfig(
        name="d", type="postgres", retrieval_dsn="postgresql://x",
        embedder_backend="bge-m3")
    store = build_store(cfg, None, tmp_path)
    assert isinstance(store, PgHybridStore)
