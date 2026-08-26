"""Load the built-in BIRD financial demo schema+data into a PostgreSQL business DB.

Used by docker-compose's one-shot `db-init` service (and locally against a
running Postgres). Idempotent: ``CREATE TABLE IF NOT EXISTS`` + inserts use
``ON CONFLICT DO NOTHING``, so re-running never duplicates rows.

Usage:
    uv run python scripts/init_postgres_demo.py [postgres://user:pass@host:port/db]
    (default: postgres://trove:trove@127.0.0.1:5432/trove)
"""

from __future__ import annotations

import asyncio
import sys

from trove.demo import create_demo_database
from trove.services.datasource.adapters.postgres import PostgresAdapter
from trove.services.datasource.urls import parse_datasource_url


async def main(argv: list[str]) -> None:
    url = argv[0] if argv else "postgres://trove:trove@127.0.0.1:5432/trove"
    cfg = parse_datasource_url(url.replace("postgresql://", "postgres://", 1))

    adapter = PostgresAdapter(name=cfg.name, config=cfg.connection_params)
    await adapter.connect()
    try:
        await create_demo_database(adapter)
    finally:
        await adapter.disconnect()
    print(f"BIRD financial schema loaded into PostgreSQL: {cfg.name}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
