"""Retrieval backend registry — name → factory(仿 datasource adapter registry).

后端工厂接收 KbService 实例(HybridBackend 等需要读 kb_items/kb_fts
镜像),按数据源在 KbService 内读时 dispatch。builtin 由 KbService
自身实现(resolver 对 builtin 返回 None,即"当前行为")。
"""

from __future__ import annotations

from typing import Any, Callable

from trove.core.logging import get_logger

logger = get_logger(__name__)

_BACKEND_REGISTRY: dict[str, Callable[[Any], Any]] = {}


def register_retrieval_backend(name: str, factory: Callable[[Any], Any]) -> None:
    """注册一个检索后端工厂(名称 → 工厂,工厂接收 KbService)。"""
    _BACKEND_REGISTRY[name] = factory
    logger.info(
        "Registered retrieval backend: %s → %s", name,
        getattr(factory, "__name__", factory),
    )


def backend_names() -> list[str]:
    return sorted(_BACKEND_REGISTRY)


def build_retrieval_backend(name: str, kb: Any) -> Any:
    """按名称实例化后端(kb = KbService);未注册 → None。"""
    factory = _BACKEND_REGISTRY.get(name)
    if factory is None:
        return None
    return factory(kb)


def _effective_backend(cfg: Any) -> str:
    """解析后端名:显式 hybrid/rag 沿用;缺省(builtin/空)时,若具备混合检索库
    (retrieval_dsn 或 postgres 业务库同实例)且配置了 embedding_model,则
    升级为 pg_hybrid 作为默认检索后端。"""
    rb = str(getattr(cfg, "retrieval_backend", "") or "builtin").strip()
    if rb not in ("", "builtin"):
        return rb
    dsn = str(getattr(cfg, "retrieval_dsn", "") or "").strip()
    if not dsn and getattr(cfg, "type", "") == "postgres":
        from trove.services.kb.backends.dense import _vector_dsn
        dsn = _vector_dsn(cfg)
    if dsn and str(getattr(cfg, "embedding_model", "") or "").strip():
        return "pg_hybrid"
    return "builtin"


def resolver_from_configs(
    configs: list[Any],
    embedder_factory: Callable[[Any], Any] | None = None,
) -> tuple[Callable[[str], Any], Callable[[Any], None]]:
    """从持久化数据源配置构建 resolver(bind 用于回填 KbService 实例)。

    Args:
        configs: 持久化数据源配置列表。
        embedder_factory: (cfg) → Embedder | None。rag / pg_hybrid 后端用它
            构造稠密通道(None/缺 embedding_model → 退化为纯稀疏,不报错)。

    Returns:
        (resolve, bind):
        - resolve(datasource) → RetrievalBackend | None(builtin/未知/构造失败 → None);
        - bind(kb) 把 KbService 实例注入闭包(构造后端需要读 KB 镜像)。
    """
    backend_map: dict[str, str] = {}
    cfg_map: dict[str, Any] = {}
    for cfg in configs:
        name = getattr(cfg, "name", "")
        if name:
            cfg_map[name] = cfg
        eff = _effective_backend(cfg)
        if name and (eff in _BACKEND_REGISTRY or eff == "pg_hybrid"):
            backend_map[name] = eff

    holder: dict[str, Any] = {}

    def _build(name: str, cfg: Any, kb: Any) -> Any:
        if name == "hybrid":
            from trove.services.kb.backends.hybrid import HybridBackend
            return HybridBackend(kb)
        if name == "rag":
            from trove.services.kb.backends.dense import vector_store_for
            from trove.services.kb.backends.rag import RagBackend
            embedder = embedder_factory(cfg) if embedder_factory is not None else None
            store = vector_store_for(kb, cfg)
            return RagBackend(kb, embedder=embedder, vector_store=store)
        if name == "pg_hybrid":
            from trove.services.kb.backends.pg_hybrid import PgHybridKbBackend
            from trove.services.retrieval import (
                CrossEncoderReranker,
                DeterministicReranker,
                PgHybridStore,
                SqliteHybridStore,
            )

            embedder = embedder_factory(cfg) if embedder_factory is not None else None
            reranker_model = str(getattr(cfg, "rerank_model", "") or "")
            reranker = (
                CrossEncoderReranker(embedder, model=reranker_model)
                if reranker_model else DeterministicReranker())
            dsn = str(getattr(cfg, "retrieval_dsn", "") or "").strip()
            if not dsn and getattr(cfg, "type", "") == "postgres":
                from trove.services.kb.backends.dense import _vector_dsn
                dsn = _vector_dsn(cfg)
            if dsn:
                store = PgHybridStore(
                    dsn, embedder=embedder, reranker=reranker,
                    dims=int(getattr(cfg, "embedding_dims", 1536) or 1536),
                    fts_tokenizer=str(getattr(cfg, "fts_tokenizer", "") or "en_stem"),
                )
            else:
                # SQLite 兜底须与索引器同一 home(否则检索库为空);取 kb 目录父级
                home = getattr(cfg, "home", "") or (
                    str(kb.kb_dir.parent) if kb is not None and getattr(kb, "kb_dir", None) else "")
                store = SqliteHybridStore.for_home(home, embedder, reranker)
            return PgHybridKbBackend(kb, store, embedder=embedder)
        return build_retrieval_backend(name, kb)

    def resolve(datasource: str) -> Any:
        name = backend_map.get(datasource or "", "builtin")
        if name == "builtin":
            return None
        cache = holder.get("backends")
        if cache is not None and datasource in cache:
            return cache[datasource]
        kb = holder.get("kb")
        if kb is None:
            return None
        try:
            backend = _build(name, cfg_map.get(datasource or ""), kb)
        except Exception as e:
            logger.warning("backend build failed for %s (%s); using builtin", datasource, e)
            backend = None
        if "backends" not in holder:
            holder["backends"] = {}
        holder["backends"][datasource] = backend
        return backend

    def bind(kb: Any) -> None:
        holder["kb"] = kb
        holder["backends"] = {}

    return resolve, bind
