"""Demo database tests (BIRD financial dataset)."""

import pytest

from trove.demo import (
    create_demo_database,
    SAMPLE_ACCOUNTS,
    SAMPLE_CARDS,
    SAMPLE_CLIENTS,
    SAMPLE_DISP,
    SAMPLE_DISTRICTS,
    SAMPLE_LOANS,
    SAMPLE_ORDERS,
    SAMPLE_TRANS,
)
from trove.services.datasource.adapters.sqlite import SQLiteAdapter


@pytest.fixture
async def demo_adapter(tmp_path):
    adapter = SQLiteAdapter(name="demo", config={"path": str(tmp_path / "demo.db")})
    await adapter.connect()
    await create_demo_database(adapter)
    yield adapter
    await adapter.disconnect()


class TestDemoDatabase:
    async def test_tables_created(self, demo_adapter):
        schema = await demo_adapter.get_schema()
        table_names = {t.name for t in schema.tables}
        assert {"district", "client", "account", "disp", "card", "loan", "order", "trans"} <= table_names

    @pytest.mark.parametrize("table, samples", [
        ("district", SAMPLE_DISTRICTS),
        ("client", SAMPLE_CLIENTS),
        ("account", SAMPLE_ACCOUNTS),
        ("disp", SAMPLE_DISP),
        ("card", SAMPLE_CARDS),
        ("loan", SAMPLE_LOANS),
        ('"order"', SAMPLE_ORDERS),
        ("trans", SAMPLE_TRANS),
    ])
    async def test_table_row_counts(self, demo_adapter, table, samples):
        result = await demo_adapter.execute(f"SELECT COUNT(*) FROM {table}")
        assert result.rows[0][0] == len(samples)

    async def test_district_schema_columns(self, demo_adapter):
        schema = await demo_adapter.get_schema()
        district = next(t for t in schema.tables if t.name == "district")
        col_names = {c.name for c in district.columns}
        # Key columns from the BIRD financial district table
        assert "A2" in col_names      # district name
        assert "A3" in col_names      # region
        assert "A11" in col_names     # average salary
        assert "A12" in col_names     # unemployment '95
        assert "A13" in col_names     # unemployment '96

    async def test_trans_schema_columns(self, demo_adapter):
        schema = await demo_adapter.get_schema()
        trans = next(t for t in schema.tables if t.name == "trans")
        col_names = {c.name for c in trans.columns}
        assert {"account_id", "date", "type", "operation", "amount", "balance"} <= col_names

    async def test_join_query(self, demo_adapter):
        """Classic BIRD financial query: average loan amount per district."""
        result = await demo_adapter.execute(
            "SELECT d.A2 AS district_name, AVG(l.amount) AS avg_loan "
            "FROM loan l "
            "JOIN account a ON l.account_id = a.account_id "
            "JOIN district d ON a.district_id = d.district_id "
            "GROUP BY d.A2 "
            "ORDER BY avg_loan DESC"
        )
        assert result.row_count == 3
        # Benesov has the single 240000 loan — the highest average
        assert result.rows[0][0] == "Benesov"
        assert result.rows[0][1] == 240000.0

    async def test_unemployment_rate_change(self, demo_adapter):
        """Classic BIRD financial question: unemployment '96 vs '95 per district."""
        result = await demo_adapter.execute(
            "SELECT A2 AS district_name, (A13 - A12) AS unemploy_diff "
            "FROM district "
            "ORDER BY unemploy_diff DESC"
        )
        # Benesov worsened the most (4.2 - 3.4 = 0.8)
        assert result.rows[0][0] == "Benesov"
        assert result.row_count == 3

    async def test_client_district_join(self, demo_adapter):
        """Clients joined with their district names."""
        result = await demo_adapter.execute(
            "SELECT c.client_id, d.A2 "
            "FROM client c JOIN district d ON c.district_id = d.district_id "
            "WHERE d.A2 = 'Hl.m. Praha'"
        )
        # Clients 2 and 3 live in Prague
        assert result.row_count == 2
        assert {row[0] for row in result.rows} == {2, 3}
