# Trove

Trove — Intelligent Data Agent: natural language to SQL with an evolvable knowledge base.

## Quick start

```bash
# dev environment (installs project + dev tools)
uv sync

# run tests
uv run pytest

# start the REPL (with the built-in demo SQLite datasource)
uv run trove --datasource demo

# one-shot CLI mode
uv run trove-cli --datasource demo "哪个地区的平均贷款金额最高?"
```

## Extras

```bash
uv sync --extra postgres   # PostgreSQL adapter (asyncpg)
uv sync --extra duckdb     # DuckDB adapter
```

See `~/hub/TroveDesign/docs/` for architecture and MVP scope.
