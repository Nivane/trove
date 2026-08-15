"""Import a BIRD-format SQLite database into MySQL (one-off data utility).

Usage:
    uv run python scripts/import_sqlite_to_mysql.py <sqlite_path> \
        mysql://user:pass@host:3306/database

Creates the database if missing, maps the sqlite schema to MySQL types,
bulk-loads every table, and prints final row counts.
"""

import asyncio
import sqlite3
import sys
from urllib.parse import urlparse, unquote

import aiomysql

BATCH_SIZE = 5000

# BIRD 官方行数（用于最终核对）
EXPECTED_COUNTS = {
    "district": 77, "account": 4500, "client": 5369, "disp": 5369,
    "card": 892, "loan": 682, "order": 6471, "trans": 1056320,
}


def mysql_type(sqlite_decl: str) -> str:
    """Map a sqlite declared column type to a MySQL type."""
    t = (sqlite_decl or "").strip().upper()
    if not t:
        return "VARCHAR(255)"
    if t.startswith("VARCHAR"):
        return t if "(" in t else "VARCHAR(255)"
    if t == "TEXT":
        return "TEXT"
    if "INT" in t:
        return "INT"
    if t.startswith("DECIMAL") or t.startswith("NUMERIC"):
        return t if "(" in t else "DECIMAL(20,6)"
    if t.startswith("DATETIME") or t.startswith("TIMESTAMP"):
        return "DATETIME"
    if t == "DATE":
        return "DATE"
    if t in ("REAL", "FLOAT", "DOUBLE"):
        return "DOUBLE"
    return "VARCHAR(255)"


def read_tables(sqlite_path: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        tables = []
        for (tname,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            cols = conn.execute(f'PRAGMA table_info("{tname}")').fetchall()
            # cols: (cid, name, type, notnull, dflt_value, pk)
            tables.append({
                "name": tname,
                "columns": [
                    {"name": c[1], "type": mysql_type(c[2]), "pk": bool(c[5])}
                    for c in cols
                ],
                "rows": [list(r) for r in conn.execute(f'SELECT * FROM "{tname}"')],
            })
        return tables
    finally:
        conn.close()


async def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    sqlite_path, mysql_url = sys.argv[1], sys.argv[2]

    parsed = urlparse(mysql_url)
    params = {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "db": parsed.path.lstrip("/") or "",
    }

    print(f"读取 SQLite: {sqlite_path}")
    tables = read_tables(sqlite_path)
    print(f"  {len(tables)} 张表")

    # 建库（无 db 连接）
    admin = await aiomysql.connect(
        host=params["host"], port=params["port"],
        user=params["user"], password=params["password"],
        autocommit=True,
    )
    async with admin.cursor() as cur:
        await cur.execute(f"CREATE DATABASE IF NOT EXISTS `{params['db']}`")
    admin.close()

    conn = await aiomysql.connect(**params, autocommit=True)
    try:
        for table in tables:
            cols = ", ".join(
                f"`{c['name']}` {c['type']}" + (" PRIMARY KEY" if c["pk"] else "")
                for c in table["columns"]
            )
            ddl = f"CREATE TABLE IF NOT EXISTS `{table['name']}` ({cols})"
            async with conn.cursor() as cur:
                await cur.execute(f"DROP TABLE IF EXISTS `{table['name']}`")
                await cur.execute(ddl)

            placeholders = ",".join(["%s"] * max(len(table["columns"]), 1))
            insert_sql = (
                f"INSERT INTO `{table['name']}` VALUES ({placeholders})"
            )
            for i in range(0, len(table["rows"]), BATCH_SIZE):
                batch = table["rows"][i : i + BATCH_SIZE]
                async with conn.cursor() as cur:
                    await cur.executemany(insert_sql, batch)
            print(f"  {table['name']}: {len(table['rows'])} 行")

        print("\n核对（BIRD 官方统计）:")
        for name, expected in EXPECTED_COUNTS.items():
            async with conn.cursor() as cur:
                await cur.execute(f"SELECT COUNT(*) FROM `{name}`")
                actual = (await cur.fetchone())[0]
            mark = "✓" if actual == expected else "✗"
            print(f"  {name}: {actual} (期望 {expected}) {mark}")
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
