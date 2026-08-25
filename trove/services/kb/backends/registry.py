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


def resolver_from_configs(
    configs: list[Any],
    embedder_factory: Callable[[Any], Any] | None = None,
) -> tuple[Callable[[str], Any], Callable[[Any], None]]:
    """从持久化数据源配置构建 resolver(bind 用于回填 KbService 实例)。

    Args:
        configs: 持久化数据源配置列表。
        embedder_factory: (cfg) → Embedder | None。rag 后端用它构造稠密通道
            (None/缺 embedding_model → rag 退化为纯稀疏,不报错)。

    Returns:
        (resolve, bind):
        - resolve(datasource) → RetrievalBackend | None(builtin/未知 → None);
        - bind(kb) 把 KbService 实例注入闭包(构造后端需要读 KB 镜像)。
    """
    backend_map: dict[str, str] = {}
    cfg_map: dict[str, Any] = {}
    for cfg in configs:
        name = getattr(cfg, "name", "")
        rb = str(getattr(cfg, "retrieval_backend", "") or "builtin").strip()
        if name:
            cfg_map[name] = cfg
        if name and rb in _BACKEND_REGISTRY:
            backend_map[name] = rb

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
        return build_retrieval_backend(name, kb)

    def resolve(datasource: str) -> Any:
        name = backend_map.get(datasource or "", "builtin")
        if name == "builtin":
            return None
        kb = holder.get("kb")
        if kb is None:
            return None
        return _build(name, cfg_map.get(datasource or ""), kb)

    def bind(kb: Any) -> None:
        holder["kb"] = kb

    return resolve, bind
