"""Built-in demo datasource setup (BIRD financial SQLite).

Migrated from trove.main so the services layer owns demo registration;
main.py keeps a module-level re-import for test patching compatibility.
"""

from __future__ import annotations

from pathlib import Path

from trove.core.types import DatasourceConfig
from trove.services.datasource.adapters.sqlite import SQLiteAdapter
from trove.services.datasource.registry import ConnectorRegistry


async def setup_demo_datasource(
    registry: ConnectorRegistry, set_default: bool = True
) -> None:
    """Set up the built-in demo SQLite database with BIRD financial data.

    Args:
        registry: The connector registry to register with.
        set_default: Whether demo becomes the registry default. True for the
            REPL / ``--datasource demo`` path; the admin path decides from
            the registry's current default state and persists the flag so a
            restart restores the same default (see admin.py create_datasource).
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
        default=set_default,
    )
    await registry.register(config, set_default=set_default)
