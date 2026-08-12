"""Main entry point for Trove.

Supports multiple modes:
  - trove (REPL): Interactive terminal UI
  - trove-cli --datasource demo: Command-line mode
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from trove.core.config import ConfigLoader, AgentConfig
from trove.core.logging import get_logger
from trove.core.types import DatasourceConfig
from trove.storage.session_store import SessionStore
from trove.llm.gateway import LLMGateway
from trove.services.datasource.registry import ConnectorRegistry, register_adapter
from trove.services.datasource.adapters.sqlite import SQLiteAdapter
from trove.services.datasource.catalog import CatalogService
from trove.workflow.engine import WorkflowEngine
from trove.workflow.registry import WorkflowRegistry
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


async def create_app_components(args) -> dict:
    """Create and wire together all application components.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Dict with all initialized components.
    """
    # ── Config ────────────────────────────────────────────
    config = ConfigLoader.load_agent_config(args.config)
    if args.model:
        config.target = args.model

    # ── Storage ───────────────────────────────────────────
    session_store = SessionStore(home_dir=config.home)

    # ── LLM Gateway ───────────────────────────────────────
    llm_gateway = LLMGateway()

    # ── Datasource ────────────────────────────────────────
    connector_registry = ConnectorRegistry()

    # Set up the requested datasource
    datasource_name = args.datasource
    if datasource_name == "demo":
        await setup_demo_datasource(connector_registry)
    elif datasource_name.startswith("sqlite://"):
        db_path = datasource_name.replace("sqlite://", "")
        adapter_config = DatasourceConfig(
            name="sqlite",
            type="sqlite",
            connection_params={"path": db_path},
            default=True,
        )
        await connector_registry.register(adapter_config, set_default=True)
    else:
        logger.warning(
            "Unknown datasource: %s. Use --datasource demo for the built-in demo.",
            datasource_name,
        )
        await setup_demo_datasource(connector_registry)

    # ── Catalog ───────────────────────────────────────────
    catalog_service = CatalogService(connector_registry)

    # ── Workflow Engine ───────────────────────────────────
    engine = WorkflowEngine()
    for wf_name in WorkflowRegistry.list_available():
        wf = WorkflowRegistry.create(wf_name)
        engine.register(wf)

    # ── Session Manager ───────────────────────────────────
    session_manager = SessionManager(
        config=config,
        session_store=session_store,
        workflow_engine=engine,
        llm_gateway=llm_gateway,
        catalog_service=catalog_service,
        connector_registry=connector_registry,
    )

    return {
        "config": config,
        "session_store": session_store,
        "llm_gateway": llm_gateway,
        "connector_registry": connector_registry,
        "catalog_service": catalog_service,
        "engine": engine,
        "session_manager": session_manager,
    }


# ── Entry Points ──────────────────────────────────────────


async def async_main_repl():
    """Async main for REPL mode."""
    args = parse_args()

    if args.version:
        from trove import __version__
        print(f"Trove v{__version__}")
        return

    components = await create_app_components(args)

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
    args = parse_args()

    if args.print_mode:
        # Non-interactive: read from stdin, print JSON
        components = await create_app_components(args)
        session_manager = components["session_manager"]
        session = await session_manager.start_session()

        user_input = sys.stdin.read().strip()
        if user_input:
            response, result = await session_manager.ask(
                session=session,
                question=user_input,
                workflow_name=args.workflow,
            )
            import json
            print(json.dumps({
                "response": response,
                "trace_id": result.trace_id,
                "nodes": [
                    {"name": n.node_name, "status": n.status.value, "data": n.data}
                    for n in result.nodes
                ],
            }, ensure_ascii=False, indent=2))
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


# Allow running as python -m trove
if __name__ == "__main__":
    main_repl()
