"""Execution-layer table allowlist (ConnectorRegistry).

The generation tools already allowlist tables, but the final execute path
(used by MCP / semantic_query / any direct caller) must enforce the same
boundary — otherwise a caller bypassing gen_sql can reference any table.
"""

import pytest

from trove.core.errors import DatasourceError
from trove.core.types import DatasourceConfig
from trove.services.datasource.registry import ConnectorRegistry


async def test_allowlist_rejects_unlisted_table(sqlite_registry):
    sqlite_registry.set_allowed_tables("test_db", ["other_table"])
    with pytest.raises(DatasourceError, match="not in the allowed tables"):
        await sqlite_registry.execute("SELECT * FROM students")


async def test_allowlist_allows_listed_table(sqlite_registry):
    sqlite_registry.set_allowed_tables("test_db", ["Students"])
    result = await sqlite_registry.execute("SELECT * FROM students")
    assert result.row_count == 5


async def test_clear_allowlist_restores_unrestricted(sqlite_registry):
    sqlite_registry.set_allowed_tables("test_db", ["nope"])
    sqlite_registry.set_allowed_tables("test_db", None)
    result = await sqlite_registry.execute("SELECT * FROM students")
    assert result.row_count == 5


async def test_metadata_table_denied_without_allowlist(sqlite_registry):
    with pytest.raises(DatasourceError, match="metadata/system table"):
        await sqlite_registry.execute("SELECT name FROM sqlite_master")


async def test_explain_enforces_allowlist(sqlite_registry):
    sqlite_registry.set_allowed_tables("test_db", ["other_table"])
    with pytest.raises(DatasourceError, match="not in the allowed tables"):
        await sqlite_registry.explain("SELECT * FROM students")


async def test_register_pins_allowlist_and_unregister_clears(tmp_path):
    registry = ConnectorRegistry()
    cfg = DatasourceConfig(
        name="pinned", type="sqlite",
        connection_params={"path": ":memory:"},
        allowed_tables=["Students"],
    )
    await registry.register(cfg)
    assert registry.allowed_tables("pinned") == {"students"}
    with pytest.raises(DatasourceError, match="not in the allowed tables"):
        await registry.execute("SELECT * FROM other")
    await registry.unregister("pinned")
    assert registry.allowed_tables("pinned") is None
