"""Main entry point for Trove.

Supports multiple modes:
  - trove (REPL): Interactive terminal UI
  - trove-cli --datasource demo: Command-line mode
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from trove.core.config import ConfigLoader, AgentConfig
from trove.core.logging import get_logger
from trove.core.types import DatasourceConfig
from trove.storage.session_store import SessionStore
from trove.storage.checkpoint_store import build_checkpointer
from trove.llm.gateway import LLMGateway
from trove.services.datasource.registry import ConnectorRegistry, register_adapter
from trove.services.datasource.adapters.sqlite import SQLiteAdapter
from trove.services.datasource.catalog import CatalogService
from trove.workflow.graphs import GraphServices, build_graphs
from trove.agent.session import SessionManager

logger = get_logger(__name__)


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Trove — Intelligent Data Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--datasource", "-d",
        default="demo",
        help="Datasource to use (default: demo uses built-in SQLite)",
    )
    parser.add_argument(
        "--config", "-f",
        default=None,
        help="Path to agent.yml config file",
    )
    parser.add_argument(
        "--model", "-m",
        default=None,
        help="LLM model to use (overrides config)",
    )
    parser.add_argument(
        "--print", "-p",
        action="store_true",
        dest="print_mode",
        help="Print raw JSON output (for pipelining)",
    )
    parser.add_argument(
        "--workflow", "-w",
        default="reflection",
        choices=["reflection", "fixed", "empty"],
        help="Workflow to use (default: reflection)",
    )
    parser.add_argument(
        "--version", "-v",
        action="store_true",
        help="Print version and exit",
    )
    return parser.parse_args()


async def setup_demo_datasource(registry: ConnectorRegistry) -> None:
    """Set up the built-in demo SQLite database with BIRD financial data.

    Args:
        registry: The connector registry to register with.
    """
    from trove.demo import create_demo_database

    demo_path = Path.home() / ".trove" / "demo.db"
    demo_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove old demo db to start fresh each time
    if demo_path.exists():
        demo_path.unlink()

    adapter = SQLiteAdapter(name="demo", config={"path": str(demo_path)})
    await adapter.connect()
    await create_demo_database(adapter)
    await adapter.disconnect()

    config = DatasourceConfig(
        name="demo",
        type="sqlite",
        connection_params={"path": str(demo_path)},
        default=True,
    )
    await registry.register(config, set_default=True)


async def setup_datasource(args, registry: ConnectorRegistry) -> None:
    """Register the --datasource target.

    Accepts:
      - "demo": the built-in BIRD financial demo database
      - scheme:// URLs: sqlite://, mysql://, clickhouse://, duckdb://

    Raises:
        DatasourceError: Unknown target, malformed URL, or connection failure.
    """
    from trove.core.errors import DatasourceError
    from trove.services.datasource.urls import parse_datasource_url

    target = args.datasource
    if target == "demo":
        await setup_demo_datasource(registry)
    elif "://" in target:
        adapter_config = parse_datasource_url(target)
        await registry.register(adapter_config, set_default=True)
    else:
        raise DatasourceError(
            message=(
                f"Unknown datasource: {target}. "
                f"Use --datasource demo or a scheme:// URL "
                f"(sqlite/mysql/clickhouse/duckdb)."
            ),
            datasource=target,
        )


async def create_app_components(
    args,
    config: AgentConfig,
    checkpointer=None,
) -> dict:
    """Create and wire together all application components.

    Args:
        args: Parsed command-line arguments.
        config: Loaded AgentConfig.
        checkpointer: Optional LangGraph checkpointer (from build_checkpointer).

    Returns:
        Dict with all initialized components.
    """
    # ── Storage ────────────────────────────────────────────
    session_store = SessionStore(home_dir=config.home)

    # ── Auth (central app.db: users/tokens/grants/audit) ────
    from trove.services.auth.service import AuthService
    auth = AuthService(Path(config.home).expanduser() / "app.db")
    bootstrap_admin, bootstrap_password = await auth.ensure_bootstrap_admin(
        os.environ.get("TROVE_ADMIN_PASSWORD")
    )

    # ── LLM Gateway ───────────────────────────────────────
    llm_gateway = LLMGateway(providers=config.providers)

    # ── Datasource ────────────────────────────────────────
    connector_registry = ConnectorRegistry()

    # Set up the requested datasource; on failure fall back to demo
    # so the REPL stays usable (with a clear warning).
    try:
        await setup_datasource(args, connector_registry)
    except Exception as e:
        logger.warning("Datasource setup failed (%s); falling back to demo.", e)
        await setup_demo_datasource(connector_registry)

    # ── Catalog ───────────────────────────────────────────
    catalog_service = CatalogService(connector_registry)

    # ── Knowledge base (optional: .trove/kb/) ─────────────
    from trove.services.kb.service import KbService
    kb = KbService(Path.cwd())

    # ── Data lineage (optional: .trove/lineage/) — definitions.yml lazy
    # sync + executed-query capture; never blocks agent startup.
    from trove.services.lineage.service import LineageService
    lineage = LineageService(Path.cwd())

    # ── Semantic layer (optional: config.semantic_layer_path) ──
    # OSSIE YAML 每查询实时读取(mtime 缓存),坏文件回退 last-known-good,
    # 任何初始化失败都不阻断问题流程。
    semantic_layer = None
    if config.semantic_layer_path:
        try:
            from trove.services.semantic_layer.provider import (
                SemanticLayerProvider,
            )
            adapter = await connector_registry.get()
            ds_name = connector_registry.default_name or "default"
            schema = await adapter.get_schema()
            known_tables = {t.name.lower() for t in schema.tables}
            semantic_layer = SemanticLayerProvider(
                directory=Path.cwd() / config.semantic_layer_path / ds_name,
                datasource=ds_name,
                dialect=adapter.dialect(),
                table_exists=lambda t: t.lower() in known_tables,
            )
            logger.info(
                "Semantic layer enabled: %s", semantic_layer.directory)
        except Exception as e:
            logger.warning(
                "Semantic layer init failed (%s); continuing without it.", e)
            semantic_layer = None

    # ── Graphs ────────────────────────────────────────────
    services = GraphServices(
        llm=llm_gateway,
        catalog=catalog_service,
        connectors=connector_registry,
        config=config,
        kb=kb,
        semantic_layer=semantic_layer,
        lineage=lineage,
    )
    graphs = build_graphs(services, checkpointer=checkpointer)

    # ── Session Manager ───────────────────────────────────
    from trove.llm.observability import build_callback_handler
    tracing_handler = build_callback_handler()
    session_manager = SessionManager(
        config=config,
        session_store=session_store,
        graphs=graphs,
        llm_gateway=llm_gateway,
        callbacks=[tracing_handler] if tracing_handler else None,
        kb=kb,
        connectors=connector_registry,
    )

    return {
        "config": config,
        "session_store": session_store,
        "auth": auth,
        "bootstrap_admin_password": bootstrap_password,
        "llm_gateway": llm_gateway,
        "connector_registry": connector_registry,
        "catalog_service": catalog_service,
        "kb": kb,
        "lineage": lineage,
        "graphs": graphs,
        "session_manager": session_manager,
    }


def format_print_payload(summary: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the --print JSON payload from the stream summary and events.

    Args:
        summary: Terminal event summary (final state essentials).
        events: All stream events collected during the run.

    Returns:
        JSON-serializable payload.
    """
    printable_events = [
        {k: v for k, v in event.items() if k in ("type", "node", "content", "row_count")}
        for event in events
    ]
    return {
        "session_id": summary.get("session_id", ""),
        "response": summary.get("final_response", ""),
        "sql": summary.get("sql", ""),
        "row_count": summary.get("row_count", -1),
        "verdict": summary.get("verdict", ""),
        "error": summary.get("error", ""),
        "kb_hits": summary.get("kb_hits", []),
        "semantics": summary.get("semantics", ""),
        "insights": summary.get("insights", []),
        "hitl_status": summary.get("hitl_status", ""),
        "events": printable_events,
    }


# ── Entry Points ──────────────────────────────────────────


async def _load_config(args) -> AgentConfig:
    """Load .env and config with CLI overrides applied."""
    from dotenv import load_dotenv

    # Explicitly anchor on cwd (load_dotenv() defaults to searching from
    # this file's location, which is wrong for a CLI invoked from a project).
    load_dotenv(Path.cwd() / ".env")
    config = ConfigLoader.load_agent_config(args.config)
    from trove.llm.tracing import configure_tracing
    configure_tracing(config.tracing)
    from trove.tracing.local import configure_trace_store
    configure_trace_store(config.home)
    if args.model:
        config.target = args.model
    return config


async def async_main_repl():
    """Async main for REPL mode."""
    args = parse_args()

    if args.version:
        from trove import __version__
        print(f"Trove v{__version__}")
        return

    config = await _load_config(args)

    async with build_checkpointer(config.home) as checkpointer:
        components = await create_app_components(args, config, checkpointer)

        # Create or load a session
        session_manager = components["session_manager"]
        session = await session_manager.start_session(
            project_cwd=".",
            user_id="local",
        )

        # Launch REPL
        from trove.cli.app import TroveREPL
        repl = TroveREPL(
            session_manager=session_manager,
            config=components["config"],
            catalog_service=components["catalog_service"],
            connector_registry=components["connector_registry"],
            session_store=components["session_store"],
            current_session=session,
            kb_service=components["kb"],
            llm_gateway=components["llm_gateway"],
        )

        try:
            await repl.run()
        finally:
            await repl.cleanup()
            await components["connector_registry"].close_all()


def main_repl():
    """Entry point for 'trove' command (REPL mode)."""
    try:
        asyncio.run(async_main_repl())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)


async def async_main_cli():
    """Async main for CLI (non-interactive) mode."""
    # Subcommands: trove-cli job ...   trove-cli schedule ...
    if len(sys.argv) > 1 and sys.argv[1] in ("job", "schedule"):
        from trove.cli.schedule_cmds import main_job, main_schedule

        handler = main_job if sys.argv[1] == "job" else main_schedule
        await handler(sys.argv[2:])
        return

    args = parse_args()

    if args.print_mode:
        # Non-interactive: read from stdin, print JSON
        config = await _load_config(args)

        async with build_checkpointer(config.home) as checkpointer:
            components = await create_app_components(args, config, checkpointer)
            session_manager = components["session_manager"]
            session = await session_manager.start_session()

            user_input = sys.stdin.read().strip()
            if user_input:
                events = []
                summary = {}
                async for event in session_manager.ask_stream(
                    session=session,
                    question=user_input,
                    workflow_name=args.workflow,
                ):
                    events.append(event)
                    if "summary" in event:
                        summary = event["summary"]

                import json
                print(json.dumps(
                    format_print_payload(summary, events),
                    ensure_ascii=False, indent=2,
                ))
        return

    # Default: launch REPL
    await async_main_repl()


def main_cli():
    """Entry point for 'trove-cli' command."""
    try:
        asyncio.run(async_main_cli())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)


# ── HTTP API (trove serve) ───────────────────────────────


def serve_parser() -> argparse.ArgumentParser:
    """Argument parser for the 'trove serve' REST API subcommand."""
    parser = argparse.ArgumentParser(
        prog="trove serve",
        description="Run the Trove REST API (uvicorn)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default 8000)")
    parser.add_argument("--datasource", "-d", default="demo", help="Datasource to use")
    parser.add_argument("--config", "-f", default=None, help="Path to agent.yml config file")
    parser.add_argument("--model", "-m", default=None, help="LLM model to use (overrides config)")
    parser.add_argument("--workflow", "-w", default="reflection", help="Default workflow")
    return parser


async def async_main_serve(argv: list[str]) -> None:
    """Async main for 'trove serve' (REST API over uvicorn)."""
    import uvicorn

    args = serve_parser().parse_args(argv)
    config = await _load_config(args)
    # HITL interrupt 依赖 checkpointer(REPL/CLI 同样传入);serve 的
    # /resume 端点靠它恢复被打断的图线程,缺失会导致 resume 抛错。
    async with build_checkpointer(config.home) as checkpointer:
        components = await create_app_components(args, config, checkpointer)

        bootstrap_password = components.get("bootstrap_admin_password")
        if bootstrap_password:
            # print + logger 双路:uvicorn 可能吞掉/延迟 stdout 顺序
            print(
                f"\n[!] Bootstrap admin 'admin' created — initial password: "
                f"{bootstrap_password}\n"
                f"    Set TROVE_ADMIN_PASSWORD to control it; change it after login.\n",
                flush=True,
            )
            logger.warning(
                "Bootstrap admin 'admin' created — initial password: %s "
                "(set TROVE_ADMIN_PASSWORD to control it)",
                bootstrap_password,
            )

        from trove.api.app import create_app
        app = create_app(components)
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port))
        try:
            await server.serve()
        finally:
            await components["connector_registry"].close_all()


def main_serve(argv: list[str] | None = None) -> None:
    """Entry point for 'trove serve'."""
    try:
        asyncio.run(async_main_serve(argv if argv is not None else sys.argv[2:]))
    except KeyboardInterrupt:
        sys.exit(0)


def main_repl():
    """Entry point for 'trove' command (REPL / serve / job / admin)."""
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        main_serve()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "admin":
        # run_admin_cmds manages its own event loop (asyncio.run inside)
        from trove.cli.admin_cmds import run_admin_cmds
        run_admin_cmds(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("job", "schedule"):
        async def _run_sub():
            from trove.cli.schedule_cmds import main_job, main_schedule

            handler = main_job if sys.argv[1] == "job" else main_schedule
            await handler(sys.argv[2:])

        try:
            asyncio.run(_run_sub())
        except KeyboardInterrupt:
            sys.exit(0)
        return
    try:
        asyncio.run(async_main_repl())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)
