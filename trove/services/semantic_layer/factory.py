"""语义层的统一构造 —— 生产启动与覆盖率体检共用这一条。

**为什么必须抽成一处**:``SemanticLayerProvider`` 的构造参数(kb semantics
路径、配置目录、``table_exists``、``catalog``、方言)是一个**隐式契约** ——
少传一样,provider 不看报错,只是安静地少一条能力(漂移检测瞎掉、表名校验
放行幻觉表,或者干脆 ``enabled`` 为假把整层关掉)。生产路径与体检各写一遍
构造,偏差不会在任一侧显形,只会在「体检说能编译、线上却拒答」时暴露 ——
而那时已经没人记得两份构造从哪开始分叉。

本模块零状态:调一次建一个 provider,行为与原先 ``main.py`` 内联的构造
逐字节一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trove.services.semantic_layer.provider import SemanticLayerProvider


async def build_provider(
    kb: Any,
    config: Any,
    datasource: str,
    adapter: Any,
    *,
    schema: Any | None = None,
    root: str | Path | None = None,
) -> SemanticLayerProvider | None:
    """为该数据源构造 provider;未启用(无 KB 模型且配置目录为空)→ ``None``。

    Args:
        kb: ``KbService`` —— 取该数据源的 ``semantics.yml`` 路径(单一真源)。
        config: ``AgentConfig`` —— 读 ``semantic_layer_path`` 补充目录。
        datasource: 数据源名(provider 的路由键,也是目录名)。
        adapter: 活跃数据源适配器 —— 取方言。
        schema: 已取的 ``SchemaInfo``;``None`` 时现场取一次(体检一次 run
            只读一次 schema,传进来避免第二跳)。
        root: 配置目录的锚(默认当前工作目录,与生产启动时一致)。
    """
    if schema is None:
        schema = await adapter.get_schema()
    known_tables = {t.name.lower() for t in schema.tables}
    catalog = {
        t.name.lower(): {c.name.lower() for c in t.columns}
        for t in schema.tables
    }
    base = Path(root) if root is not None else Path.cwd()
    semantic_dir = (
        base / config.semantic_layer_path / datasource
        if config.semantic_layer_path
        else base / ".trove" / "semantic" / datasource
    )
    provider = SemanticLayerProvider(
        directory=semantic_dir,
        datasource=datasource,
        dialect=adapter.dialect(),
        table_exists=lambda t: t.lower() in known_tables,
        kb_semantics_path=kb.semantics_path(datasource),
        catalog=catalog,
    )
    return provider if provider.enabled else None


__all__ = ["build_provider"]
