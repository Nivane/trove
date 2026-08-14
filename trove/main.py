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

    # ── LLM Gateway ───────────────────────────────────────
    llm_gateway = LLMGateway(providers=config.providers)

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

    # ── Graphs ────────────────────────────────────────────
    services = GraphServices(
        llm=llm_gateway,
        catalog=catalog_service,
        connectors=connector_registry,
        config=config,
    )
    graphs = build_graphs(services, checkpointer=checkpointer)

    # ── Session Manager ───────────────────────────────────
    session_manager = SessionManager(
        config=config,
        session_store=session_store,
        graphs=graphs,
        llm_gateway=llm_gateway,
    )

    return {
        "config": config,
        "session_store": session_store,
        "llm_gateway": llm_gateway,
        "connector_registry": connector_registry,
        "catalog_service": catalog_service,
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


# Allow running as python -m trove
if __name__ == "__main__":
    main_repl()
