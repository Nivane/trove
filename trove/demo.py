"""Demo dataset — BIRD financial (Berka / PKDD'99 Czech bank data).

Provides the built-in financial dataset for zero-config demo mode.
Schema follows the official BIRD-DEV ``financial.sqlite`` (8 tables:
district, client, account, disp, card, loan, order, trans).

The embedded rows are a small, referentially coherent subset of the
real dataset so the demo runs with zero network and zero pre-existing
data. To use the full official database instead, point the demo adapter
at a downloaded ``financial.sqlite`` (e.g. from the BIRD benchmark
distribution) — the schema here matches it exactly.

Date columns use the original Berka ``YYMMDD`` text format
(e.g. ``'960101'``), same as the official BIRD sqlite export.

Usage:
    from trove.demo import create_demo_database
    await create_demo_database(adapter)
"""

from __future__ import annotations

from trove.core.logging import get_logger

logger = get_logger(__name__)

# ── BIRD financial schema (8 tables) ─────────────────────

DISTRICT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS district (
    district_id INTEGER PRIMARY KEY,
    A2 TEXT,   -- district name
    A3 TEXT,   -- region
    A4 INTEGER,  -- no. of inhabitants
    A5 INTEGER,  -- no. of municipalities < 500 inhabitants
    A6 INTEGER,  -- no. of municipalities 500-1999
    A7 INTEGER,  -- no. of municipalities 2000-9999
    A8 INTEGER,  -- no. of municipalities > 10000
    A9 INTEGER,  -- no. of cities
    A10 REAL,    -- ratio of urban inhabitants (%)
    A11 INTEGER, -- average salary
    A12 REAL,    -- unemployment rate '95
    A13 REAL,    -- unemployment rate '96
    A14 INTEGER, -- no. of entrepreneurs per 1000 inhabitants
    A15 INTEGER, -- no. of committed crimes '95
    A16 INTEGER  -- no. of committed crimes '96
)
"""

CLIENT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS client (
    client_id INTEGER PRIMARY KEY,
    gender TEXT,
    birth_date TEXT,
    district_id INTEGER,
    FOREIGN KEY (district_id) REFERENCES district(district_id)
)
"""

ACCOUNT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS account (
    account_id INTEGER PRIMARY KEY,
    district_id INTEGER,
    frequency TEXT,
    date TEXT,
    FOREIGN KEY (district_id) REFERENCES district(district_id)
)
"""

DISP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS disp (
    disp_id INTEGER PRIMARY KEY,
    client_id INTEGER,
    account_id INTEGER,
    type TEXT,
    FOREIGN KEY (client_id) REFERENCES client(client_id),
    FOREIGN KEY (account_id) REFERENCES account(account_id)
)
"""

CARD_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS card (
    card_id INTEGER PRIMARY KEY,
    disp_id INTEGER,
    type TEXT,
    issued TEXT,
    FOREIGN KEY (disp_id) REFERENCES disp(disp_id)
)
"""

LOAN_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS loan (
    loan_id INTEGER PRIMARY KEY,
    account_id INTEGER,
    date TEXT,
    amount INTEGER,
    duration INTEGER,
    payments REAL,
    status TEXT,
    FOREIGN KEY (account_id) REFERENCES account(account_id)
)
"""

ORDER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS "order" (
    order_id INTEGER PRIMARY KEY,
    account_id INTEGER,
    bank_to TEXT,
    account_to INTEGER,
    amount REAL,
    k_symbol TEXT,
    FOREIGN KEY (account_id) REFERENCES account(account_id)
)
"""

TRANS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trans (
    trans_id INTEGER PRIMARY KEY,
    account_id INTEGER,
    date TEXT,
    type TEXT,
    operation TEXT,
    amount REAL,
    balance REAL,
    k_symbol TEXT,
    bank TEXT,
    account TEXT,
    FOREIGN KEY (account_id) REFERENCES account(account_id)
)
"""

TABLE_SQLS = [
    DISTRICT_TABLE_SQL,
    CLIENT_TABLE_SQL,
    ACCOUNT_TABLE_SQL,
    DISP_TABLE_SQL,
    CARD_TABLE_SQL,
    LOAN_TABLE_SQL,
    ORDER_TABLE_SQL,
    TRANS_TABLE_SQL,
]

# ── Sample data (coherent subset of the real dataset) ────

# district: (district_id, A2, A3, A4, A5, A6, A7, A8, A9, A10,
#            A11, A12, A13, A14, A15, A16)
SAMPLE_DISTRICTS = [
    (1, "Hl.m. Praha", "Prague", 1204953, 0, 0, 0, 1, 1, 100.0,
     12541, 0.29, 0.43, 167, 85677, 87407),
    (2, "Benesov", "central Bohemia", 88884, 80, 26, 6, 2, 5, 46.7,
     8507, 3.4, 4.2, 120, 1471, 1429),
    (3, "Brno-mesto", "south Moravia", 387986, 0, 0, 0, 1, 1, 100.0,
     10132, 4.6, 4.9, 132, 25402, 25903),
]

# client: (client_id, gender, birth_date, district_id)
SAMPLE_CLIENTS = [
    (1, "F", "621030", 2),
    (2, "M", "551205", 1),
    (3, "F", "700114", 1),
    (4, "M", "730822", 3),
    (5, "F", "651107", 3),
    (6, "M", "470915", 2),
]

# account: (account_id, district_id, frequency, date)
SAMPLE_ACCOUNTS = [
    (1, 1, "POPLATEK MESICNE", "950501"),
    (2, 1, "POPLATEK MESICNE", "960302"),
    (3, 2, "POPLATEK MESICNE", "940615"),
    (4, 2, "POPLATEK MESICNE", "970101"),
    (5, 3, "POPLATEK MESICNE", "950810"),
    (6, 3, "POPLATEK MESICNE", "960402"),
]

# disp: (disp_id, client_id, account_id, type) — owner of each account
SAMPLE_DISP = [
    (1, 1, 3, "OWNER"),
    (2, 2, 1, "OWNER"),
    (3, 3, 2, "OWNER"),
    (4, 4, 5, "OWNER"),
    (5, 5, 6, "OWNER"),
    (6, 6, 4, "OWNER"),
]

# card: (card_id, disp_id, type, issued)
SAMPLE_CARDS = [
    (1, 2, "classic", "951001"),
    (2, 3, "gold", "960402"),
    (3, 4, "junior", "961101"),
]

# loan: (loan_id, account_id, date, amount, duration, payments, status)
# status: A = finished/paid, B = finished/unpaid, C = running/ok, D = running/in debt
SAMPLE_LOANS = [
    (1, 1, "960101", 150000, 48, 3125.0, "A"),
    (2, 3, "970301", 240000, 60, 4000.0, "C"),
    (3, 5, "960701", 80000, 24, 3333.33, "B"),
]

# order: (order_id, account_id, bank_to, account_to, amount, k_symbol)
SAMPLE_ORDERS = [
    (1, 2, "YZ", 123456, 1800.0, "POJISTNE"),
    (2, 6, "AB", 654321, 2500.0, "SIPO"),
]

# trans: (trans_id, account_id, date, type, operation, amount, balance,
#         k_symbol, bank, account)
# type: PRIJEM = credit, VYDAJ = withdrawal
SAMPLE_TRANS = [
    (1, 1, "960101", "PRIJEM", "VKLAD", 50000.0, 50000.0, "", "", ""),
    (2, 1, "960215", "VYDAJ", "VYBER KARTOU", 1200.0, 48800.0, "", "", ""),
    (3, 1, "960301", "PRIJEM", "PREVOD Z UCTU", 20000.0, 68800.0, "", "YZ", 987654),
    (4, 3, "961001", "PRIJEM", "VKLAD", 100000.0, 100000.0, "", "", ""),
    (5, 3, "961201", "VYDAJ", "PREVOD NA UCET", 30000.0, 70000.0, "UVER", "AB", 111222),
    (6, 5, "970101", "PRIJEM", "VKLAD", 75000.0, 75000.0, "", "", ""),
    (7, 5, "970315", "VYDAJ", "VYBER", 5000.0, 70000.0, "", "", ""),
    (8, 2, "970101", "PRIJEM", "VKLAD", 300000.0, 300000.0, "", "", ""),
]


def _literal(value) -> str:
    """Render a Python value as a SQLite literal (strings quoted + escaped)."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


async def _insert(adapter, table: str, columns: list[str], rows: list[tuple]) -> None:
    """Insert sample rows into a table (values inlined — the adapter's
    execute() takes a single SQL string with no bind parameters)."""
    col_sql = ", ".join(f'"{c}"' for c in columns)
    for row in rows:
        values = ", ".join(_literal(v) for v in row)
        await adapter.execute(
            f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({values})"
        )


async def create_demo_database(adapter) -> None:
    """Create and populate the demo BIRD financial database.

    Args:
        adapter: A connected DatabaseAdapter (typically SQLiteAdapter).
    """
    logger.info("Creating demo database...")

    # Create tables
    for table_sql in TABLE_SQLS:
        await adapter.execute(table_sql)

    # Insert sample data
    await _insert(adapter, "district",
                  ["district_id", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9",
                   "A10", "A11", "A12", "A13", "A14", "A15", "A16"],
                  SAMPLE_DISTRICTS)
    await _insert(adapter, "client",
                  ["client_id", "gender", "birth_date", "district_id"],
                  SAMPLE_CLIENTS)
    await _insert(adapter, "account",
                  ["account_id", "district_id", "frequency", "date"],
                  SAMPLE_ACCOUNTS)
    await _insert(adapter, "disp",
                  ["disp_id", "client_id", "account_id", "type"],
                  SAMPLE_DISP)
    await _insert(adapter, "card",
                  ["card_id", "disp_id", "type", "issued"],
                  SAMPLE_CARDS)
    await _insert(adapter, "loan",
                  ["loan_id", "account_id", "date", "amount", "duration", "payments", "status"],
                  SAMPLE_LOANS)
    await _insert(adapter, '"order"',
                  ["order_id", "account_id", "bank_to", "account_to", "amount", "k_symbol"],
                  SAMPLE_ORDERS)
    await _insert(adapter, "trans",
                  ["trans_id", "account_id", "date", "type", "operation", "amount",
                   "balance", "k_symbol", "bank", "account"],
                  SAMPLE_TRANS)

    logger.info(
        "Demo database created: %d districts, %d clients, %d accounts, "
        "%d dispositions, %d cards, %d loans, %d orders, %d transactions",
        len(SAMPLE_DISTRICTS), len(SAMPLE_CLIENTS), len(SAMPLE_ACCOUNTS),
        len(SAMPLE_DISP), len(SAMPLE_CARDS), len(SAMPLE_LOANS),
        len(SAMPLE_ORDERS), len(SAMPLE_TRANS),
    )
