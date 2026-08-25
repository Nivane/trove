"""Retrieval backends — per-datasource switchable retrieval (builtin / hybrid / rag).

- builtin: 确定性分(表锚/词重叠)+ hashed n-gram 重排(现状,零依赖)。
- hybrid: 稀疏通道 FTS5 + BM25(倒排索引召回 + IDF 排序),确定性门
  仍在门内融合。
- rag: 稀疏 + 稠密(embedding)双通道 RRF 融合,向量库按数据源选
  SQLite(默认)/ pgvector;dense 失败自动降级稀疏。

后端按数据源经 KbService 注入的 resolver 读时 dispatch;导入本包即
注册默认后端(builtin 由 KbService 自身实现,hybrid/rag 在此注册)。
"""

from trove.services.kb.backends.base import (
    RetrievalBackend,
    SearchIndexBackend,
)
from trove.services.kb.backends.hybrid import HybridBackend
from trove.services.kb.backends.rag import RagBackend
from trove.services.kb.backends.registry import (
    backend_names,
    build_retrieval_backend,
    register_retrieval_backend,
    resolver_from_configs,
)

register_retrieval_backend("hybrid", HybridBackend)
register_retrieval_backend("rag", RagBackend)

__all__ = [
    "RetrievalBackend",
    "SearchIndexBackend",
    "HybridBackend",
    "RagBackend",
    "backend_names",
    "build_retrieval_backend",
    "register_retrieval_backend",
    "resolver_from_configs",
]
