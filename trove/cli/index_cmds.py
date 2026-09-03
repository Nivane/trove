"""CLI for hybrid-retrieval indexing.

  trove index kb [--datasource X] [--all] [--rebuild]      # 重建 KB 文档索引
  trove index schema [--datasource X] [--all] [--rebuild]  # 重建物理 schema 文档索引
  trove index sync [--datasource X] [--all] [--rebuild]    # KB + 增量 schema(变化才重嵌)

All commands require a configured retrieval store (``retrieval_dsn`` or a
postgres business DB). With no explicit ``--datasource`` the default datasource
is indexed. ``--all`` indexes every configured datasource (for cron).

Cron example (hourly reindex of all datasources)::

    # m h  dom mon dow  command
    0 * * * *  cd /path/to/trove && uv run trove index sync --all >> /var/log/trove-index.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import types

from trove.core.logging import get_logger
from trove.services.retrieval.factory import build_store
from trove.services.retrieval.indexer import Indexer

logger = get_logger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trove index", description="Hybrid retrieval indexing (KB + schema)")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    for name in ("kb", "schema", "sync"):
        p = sub.add_parser(name, help=f"index {name}")
        p.add_argument("--datasource", "-d", default="", help="Target datasource (default = configured default)")
        p.add_argument("--all", action="store_true", help="Index every configured datasource (cron-friendly)")
        p.add_argument("--rebuild", action="store_true", help="Drop and re-index all (ignore incremental cache)")
        p.add_argument("--force-schema", action="store_true", help="(sync only) force schema re-index even if unchanged")
    return parser


async def main_index(argv: list[str]) -> None:
    args = _parser().parse_args(argv)
    from trove.main import _load_config, create_app_components
    from trove.storage.checkpoint_store import build_checkpointer

    # 组件以持久化数据源启动(boot_register);目标列表从 config_store 取,
    # 不把 --datasource 名透传给 setup_datasource(它只认 demo / scheme:// URL)。
    cfg_args = types.SimpleNamespace(datasource="", config=None, model=None)
    config = await _load_config(cfg_args)
    async with build_checkpointer(config.home) as checkpointer:
        components = await create_app_components(cfg_args, config, checkpointer)

    connectors = components["connector_registry"]
    kb = components["kb"]
    gateway = components["llm_gateway"]
    config_store = components["config_store"]
    default_name = connectors.default_name or "default"

    if args.all:
        targets = [getattr(c, "name", "") for c in config_store.load_configs()
                   if getattr(c, "name", "")]
    else:
        targets = [args.datasource] if args.datasource else [default_name]
    summary: dict[str, dict] = {}
    for ds in targets:
        cfg = None
        for c in config_store.load_configs():
            if getattr(c, "name", "") == ds:
                cfg = c
                break
        if cfg is None:
            logger.warning("datasource %s not configured; skipping", ds)
            summary[ds] = {"error": "not configured"}
            continue
        store = build_store(cfg, gateway, config.home)
        indexer = Indexer(store, kb, connectors, config.home)
        if args.subcommand == "kb":
            n = await indexer.index_kb(ds, rebuild=args.rebuild)
            summary[ds] = {"kb": n}
        elif args.subcommand == "schema":
            await kb.ensure_synced(default_datasource=ds)
            n = await indexer.index_schema(ds, rebuild=args.rebuild)
            summary[ds] = {"schema": n}
        else:  # sync
            res = await indexer.sync(ds, rebuild=args.rebuild, force_schema=args.force_schema)
            summary[ds] = res
    print(json.dumps(summary, ensure_ascii=False, default=str))
