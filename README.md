# Trove

**Talk to your data. It answers — and learns with every question.**

[English](README.md) · [简体中文](README.zh-CN.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-2600%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()

---

Trove is a **self-learning conversational data agent**: ask questions in natural language, get Markdown answers backed by real SQL. It handles schema matching, SQL generation with self-validation, execution, and reflective adjudication; wrong answers are diagnosed, rolled back, and corrected.

Every question and correction grows a per-datasource knowledge base (notes, terms, reference SQL, rules, lessons) plus cross-session memory — **accuracy improves the more you use it**.

**Semantic-first**: a semantic model (`semantics.yml`) defines exactly what can be answered. Queries it cannot cover are refused with a suggestion to extend the model — no hallucinated guesses.

## Highlights

- **LangGraph reflection workflow** — intent routing → planning → agentic SQL generation (ReAct loop with self-validation tools) → execution → reflective adjudication → self-correction loop
- **Deterministic safety rails** — an AST firewall (read-only statement whitelist, DML interception, dangerous-function blocking) plus a zero-LLM rule chain (shape / filters / values / ordering)
- **A knowledge base that learns** — `/kb init` drafts schema notes and a semantic model; `/kb learn` turns confirmed Q&A into reference SQL; corrections distill into a Hint Bank of lessons
- **Unified memory** — cross-session episodic recall, auto-extracted user preferences, per user×datasource profiles; auto content always lands `pending` until an admin confirms
- **MCP server** — expose NL→SQL as tools and resources over stdio / SSE / streamable-http for Claude Code and other MCP clients
- **Web UI + REST API** — Vue SPA chat with charts, analysis traces, and HITL dialogs, plus a JSON API under `/v1`
- **Multi-datasource** — SQLite / PostgreSQL / MySQL / ClickHouse / DuckDB, each with its own knowledge base
- **Multi-model & multi-language** — litellm gateway (OpenAI / DeepSeek / Anthropic / any compatible provider), unified `zh` / `en` interaction language

## Quick Start

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies
uv sync

# Interactive REPL against the built-in BIRD financial demo
uv run trove --datasource demo

# One-shot CLI (JSON output, question via stdin)
echo "Which region has the highest average loan amount?" | uv run trove-cli --datasource demo --print
```

### REPL commands

| Group | Commands |
|---|---|
| Session | `/help` `/exit` `/clear` `/compact` `/tasks` |
| Metadata | `/tables` `/schemas` `/table_schema <table>` `/databases` `/kb …` `/trace` |
| System | `/model [model]` `/datasource [name]` `/init` `/facts` |

### CLI options

`--datasource/-d` · `--config/-f` · `--model/-m` · `--print/-p` · `--workflow/-w` · `--version/-v`

## Interfaces

### Web UI (front-end / back-end split)

The backend is a pure JSON API (everything under `/v1`, including SSE streaming chat); the frontend is a separately built SPA (Vue + Vite).

```bash
uv run trove serve --datasource demo      # backend (API only)
cd frontend && npm run dev                # local dev → http://localhost:5173/
cd frontend && npm run build              # production build → frontend/dist/
```

Ops endpoints (no auth, no sensitive data):

- `GET /v1/health` — real dependency checks: pings internal storage and every connected datasource (`SELECT 1`), reports LLM config presence (no billed probe). `200` + `"status": "ok" | "degraded"` when the process serves; `503 "unavailable"` when internal storage is unreachable. Errors carry only exception type names, never driver text.
- `GET /v1/metrics` — Prometheus text exposition: HTTP requests by route/status, durations, in-flight gauge, LLM attempts/tokens by provider/model, SQL executions by datasource (success/error/cancelled), result-cache hits.
- Every response carries `X-Request-ID` (echoed when a caller supplies one, else a generated uuid4); the same id appears in every stderr log line of that request via `[req=<id>]`.

Client abort (`Stop` in the UI / SSE disconnect) cancels the in-flight query end-to-end: the graph task is cancelled and each adapter's driver-level interrupt fires (sqlite3 interrupt / psycopg cancel / MySQL `KILL QUERY` / duckdb interrupt), so the datasource stops working, not just the awaiting coroutine.

### MCP server

```bash
uv run trove mcp                                                            # stdio (default)
uv run trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 --token <secret>
```

Tools: `ask_data` · `list_datasources` · `kb_status`. Resources (read-only): `trove://datasources` · `trove://<datasource>/schema` · `trove://<datasource>/semantics`.

### Docker

Frontend (nginx-served SPA + `/v1` reverse proxy) and backend (pure JSON API) are independent images, built and restarted independently.

```bash
docker compose up --build        # build & start (frontend :8080, backend :8000)
docker compose build frontend    # rebuild only the frontend image
docker compose restart backend   # restart only the backend
docker compose down
```

Open `http://localhost:8080/` (default login: admin / `admin123` — local demo only; production uses the `TROVE_ADMIN_PASSWORD` environment variable). The compose stack defaults to PostgreSQL (ParadeDB image) with the BIRD demo data pre-loaded. Real conversations need LLM credentials: uncomment the read-only `~/.trove/conf` mount in `docker-compose.yml`, or provide an API key inside the container.

## Data Sources

| Datasource | Connection | Install |
|---|---|---|
| SQLite | `--datasource demo` / `sqlite:///path/to.db` / `sqlite://:memory:` | built-in |
| PostgreSQL | `postgres://user:pass@host:5432/database` | `uv sync --extra postgres` |
| MySQL | `mysql://user:pass@host:3306/database` | `uv sync --extra mysql` |
| ClickHouse | `clickhouse://user:pass@host:8123/database` | `uv sync --extra clickhouse` |
| DuckDB | `duckdb:///path/to.duckdb` / `duckdb://:memory:` | `uv sync --extra duckdb` |

The datasource name is the database name; each database evolves its own knowledge base under `.trove/kb/<database>/`. To add a datasource, implement the `DatabaseAdapter` methods and register it in `registry.py`.

## Configuration

Precedence: CLI `--model` > `conf/agent.yml` > `~/.trove/conf/agent.yml`. The full set of options is documented in the commented reference config — `conf/agent.yml`:

```yaml
agent:
  target: deepseek/deepseek-reasoner   # litellm model string
  language: zh                         # interaction language: zh / en
  semantic_first: true                 # semantic model is the only answerable boundary
  memory:
    enabled: true                      # unified memory subsystem
```

Put API keys in a project-root `.env` (auto-loaded, gitignored) or export environment variables such as `DEEPSEEK_API_KEY`. Custom OpenAI-compatible endpoints go under `agent.providers`; optional Langfuse tracing via `agent.observability.tracing.enabled: true` + `LANGFUSE_*` keys.

## Security (read-only execution)

Trove ships an AST firewall (read-only statement whitelist, DML interception, dangerous-function and metadata-table blocking) plus an optional EXPLAIN row-count guard. **The application layer is not a security boundary** — always connect Trove to a dedicated read-only role:

```sql
-- PostgreSQL (covers future objects)
CREATE ROLE trove_ro LOGIN PASSWORD '...';
GRANT pg_read_all_data TO trove_ro;

-- MySQL (per-database grants, fixed source IP)
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

Also recommended: hide sensitive columns with column-level grants or views, set `statement_timeout` / `lock_timeout` (PG) or `MAX_EXECUTION_TIME` (MySQL), and enforce row limits in the database with `LIMIT` / `LEAST()`. Every executed tool call lands in the audit log (`sql_audit`).

## How It Learns

> Under platform deployment (`serve`), initialization runs via the admin API; the flow below is the REPL local path.

1. `/kb init` — LLM drafts table/column notes and a semantic model (optional `--docs <dir>` for official column descriptions), plus deterministic terms and templates.
2. Edit the YAML under `.trove/kb/<datasource>/` to add metrics, terminology, and term→SQL mappings.
3. `/kb reload` — apply changes immediately.
4. Ask normally; when a Q&A is good, `/kb learn` → review the draft → `/kb learn --yes` to commit it as reference SQL.
5. `/kb list` — inspect knowledge counts per datasource.

Corrections automatically distill into pending Hint Bank lessons (`/kb lessons` to review, `/kb lessons --yes` to confirm). Beyond the datasource KB, a unified memory subsystem remembers per-user across sessions (episodic recall, auto preferences, profiles) — auto content always lands `pending` until an admin confirms. YAML is the single source of truth (git-manageable); the SQLite mirror is runtime retrieval only.

## Development

```bash
uv run pytest                     # full suite (~2660 tests, mocked LLM, zero network/keys)
uv run pytest tests/workflow/     # LangGraph graphs and nodes
uv run pytest -k kb               # all KB-related tests
```

Code layout: `trove/workflow/` (graphs, nodes, rules) · `trove/services/` (datasources, KB, SQL, memory) · `trove/agent/` (session orchestration) · `trove/llm/` (litellm gateway, agent loop) · `trove/storage/` (sessions & checkpoints) · `trove/cli/` (REPL and commands). Deeper architecture notes live in `CLAUDE.md`; REST API docs at `/v1/docs` when `serve` is running.

## Evaluation

BIRD dev-set execution accuracy (EX) with the full reflection pipeline:

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

Failures land in `.trove/eval/failures.jsonl`; batch-distill them into lessons with `scripts/distill_lessons.py`.

## Contributing

Bug reports, feature ideas, documentation, and pull requests are welcome. Keep changes focused and make sure tests pass before submitting.

## License

Trove is released under the [Apache License 2.0](LICENSE).
