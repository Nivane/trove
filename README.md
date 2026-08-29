# Trove

**Talk to your data. It answers — and learns with every question.**

[English](README.md) · [简体中文](README.zh-CN.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-2600%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()

---

Trove is a **self-learning conversational data agent**. Ask questions in natural language — Trove handles schema matching, SQL generation with self-validation, execution, and reflective adjudication, and returns a Markdown answer. When an answer is wrong, it diagnoses the root cause, rolls back, and corrects itself.

Every question and every correction grows a per-datasource knowledge base (notes, terms, reference SQL, rules, and lessons), so **accuracy improves the more you use it**.

**Semantic-first by design**: a semantic model (`semantics.yml`) defines exactly what can be answered. Queries it cannot cover are refused with a proposal to extend the model — no hallucinated guesses. Datasources without an initialized semantic model are refused up front and pointed to `/kb init`.

## Why Trove?

- **Built on LangGraph** — a `reflection` workflow with intent routing, planning, agentic SQL generation, deterministic validation, execution, and a self-correction loop. Three workflows available: `reflection` (default), `fixed` (direct pass), `empty` (debug).
- **Agentic SQL generation** — a ReAct loop where the model validates its own work via tools (`validate_sql`, `probe_query`, `check_result`, `finish`) and decides when it is done. Degrades gracefully to the classic generate→validate subgraph.
- **Deterministic safety rails** — SQL goes through an AST firewall (read-only whitelist, DML interception, dangerous-function and metadata-table blocking) and a rule chain (shape, filters, values, ordering) with zero-LLM verification.
- **Self-correction** — on failure, an LLM diagnoses the root cause and re-runs along a rollback ladder (`gen_sql → planner → schema_linking`) with loop protection and a SQL version chain for deterministic regression feedback.
- **A knowledge base that learns** — `/kb init` drafts schema notes, `semantics.yml` (OSSIE semantic model), terms, and templates; `/kb learn` converts confirmed Q&A into reference SQL. Lessons (Hint Bank) are distilled from corrections and eval failures. Retrieval is anchored to schema-linking matches.
- **Multi-candidate consensus** — alternative candidates generated at higher temperature go through consensus voting (`select`) for tougher questions.
- **Human-in-the-loop** — optional confirmation before execution (approve/deny per task; three options for batches).
- **MCP server** — expose NL→SQL as tools (`ask_data`, `list_datasources`, `kb_status`) and resources (`trove://datasources`, `trove://<ds>/schema`, `trove://<ds>/semantics`) over stdio / SSE / streamable-http for Claude Code and other MCP clients.
- **Web UI + REST API** — a Vue SPA chat interface with charts, analysis traces, and HITL dialogs, plus a clean JSON API under `/v1`.
- **Multi-datasource** — SQLite, PostgreSQL, MySQL, ClickHouse, DuckDB with per-datasource knowledge bases and retrieval backends (`builtin` / `hybrid` / `rag`).
- **Multi-model & multi-language** — litellm gateway (OpenAI / DeepSeek / Anthropic / any compatible provider), unified `zh` / `en` interaction language.
- **Observability** — every run produces a local span-tree trace (`/trace`, zero external deps) and can emit full Langfuse traces with per-node, per-call breakdowns.
- **Efficient by default** — deterministic time parsing, complexity-tiered token budgets, template fast-paths, exact-result caching (0 LLM calls on repeated questions), and result-cache hits skip HITL.

## Quick Start

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
# Install dependencies
uv sync

# Run the interactive REPL against the built-in BIRD financial demo
uv run trove --datasource demo

# One-shot CLI (JSON output, question via stdin)
echo "Which region has the highest average loan amount?" | uv run trove-cli --datasource demo --print
```

### REPL commands

| Group | Commands |
|---|---|
| Session | `/help` `/exit` `/clear` `/compact` (compress history) `/tasks` (alias `/todo`) |
| Metadata | `/tables` `/schemas` `/table_schema <table>` `/databases` `/kb …` `/trace` |
| System | `/model [model]` `/datasource [name]` `/init` `/facts` (user memory: list / add <text> / del <id>) |

### CLI options

`--datasource/-d` (demo or `scheme://` URL) · `--config/-f` · `--model/-m` · `--print/-p` (JSON output) · `--workflow/-w` (reflection/fixed/empty) · `--version/-v`

## Interfaces

### Web UI (front-end / back-end split)

The backend `trove serve` is a pure JSON API (everything under `/v1`, including `/v1/chat` SSE streaming); the front end is a separately built SPA (Vue + Vite).

```bash
# Backend (API only)
uv run trove serve --datasource demo

# Frontend (local dev: Vite dev server on :5173, HMR + /v1 proxy → 127.0.0.1:8000)
cd frontend && npm run dev   # open http://localhost:5173/

# Frontend (production build → frontend/dist/)
cd frontend && npm run build
```

Features: single-page chat (Vue 3 + Element Plus), charts (line/bar/pie) with themed rendering, an "analysis trace" sidebar showing plan / SQL / validation steps, HITL confirmation dialogs, server-side session persistence, and file upload.

### MCP server

```bash
uv run trove mcp                                             # stdio (default, local)
uv run trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 --token <secret>  # HTTP + bearer auth
```

Tools: `ask_data` · `list_datasources` · `kb_status`. Resources (read-only): `trove://datasources` · `trove://<datasource>/schema` · `trove://<datasource>/semantics`.

### Docker deployment

Frontend (nginx-served SPA + `/v1` reverse proxy) and backend (pure JSON API) are independent images that build and restart independently.

```bash
docker compose up --build        # build & start (backend :8000 for debugging, frontend :8080)
docker compose build frontend    # rebuild only the frontend image
docker compose restart backend   # restart only the backend
docker compose down
```

Open `http://localhost:8080/` (default login: admin / `admin123` — local demo only; production uses the `TROVE_ADMIN_PASSWORD` environment variable). The default business stack is **PostgreSQL** (ParadeDB image — pgvector + pg_bm25, shared instance) with a `db-init` service that loads the BIRD financial demo data. Real conversations need LLM credentials: uncomment the read-only `~/.trove/conf` mount in `docker-compose.yml`, or provide an API key inside the container.

## Data Sources

| Datasource | Connection | Install |
|---|---|---|
| SQLite | `--datasource demo` / `sqlite:///path/to.db` / `sqlite://:memory:` | built-in |
| PostgreSQL | `postgres://user:pass@host:5432/database` | `uv sync --extra postgres` |
| MySQL | `mysql://user:pass@host:3306/database` | `uv sync --extra mysql` |
| ClickHouse | `clickhouse://user:pass@host:8123/database` | `uv sync --extra clickhouse` |
| DuckDB | `duckdb:///path/to.duckdb` / `duckdb://:memory:` | `uv sync --extra duckdb` |

The datasource name is the database name; each database evolves its own knowledge base under `.trove/kb/<database>/`. Drivers load lazily on demand. For a new datasource, implement the six `DatabaseAdapter` methods and register it in `registry.py`.

## Configuration

Configuration precedence (model selection): CLI `--model` > `conf/agent.yml` / `~/.trove/conf/agent.yml`.

```yaml
# conf/agent.yml (example)
agent:
  target: deepseek/deepseek-reasoner      # litellm model string (reasoning models supported)
  model_fast: deepseek/deepseek-chat      # fast tier for simple/standard questions
  node_models:                            # per-node model overrides (beat complexity tiering)
    planner: deepseek/deepseek-chat
    reflect: deepseek/deepseek-reasoner
  language: zh                            # interaction language: zh / en (default zh)
  semantic_first: true                    # semantic model is the only answerable boundary
  hitl: false                             # human-in-the-loop confirmation before execution
  insights: true                          # LLM insights from the result after execution
  conclusion: true                        # one-line conclusion summary placed at the top
  result_cache: true                      # exact-result cache: repeated question → 0 LLM calls
  fast_path: true                         # deterministic template fast path
  reflect_skip: standard                  # skip LLM adjudication when rules pass
  providers:
    - name: openai                        # custom OpenAI-compatible endpoints
      litellm_params:
        api_key: ${OPENAI_API_KEY}
        api_base: https://your-endpoint/v1
```

API keys: put them in a project-root `.env` (auto-loaded, gitignored) or export environment variables such as `DEEPSEEK_API_KEY`. `providers[].litellm_params` are passed through to litellm by model prefix.

**Observability (Langfuse)**: enable `agent.observability.tracing.enabled: true` and provide `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` in `.env`. Each question becomes a full trace tree (one span per node, one generation per LLM call including reasoning), grouped by `session_id`, `node`, and `question`. The local `/trace` view always works without external services.

## Security (read-only execution)

Trove applies defense-in-depth at the application layer — an AST firewall (`trove/services/sql/guard.py`) with a read-only statement whitelist, data-modifying CTE interception, dangerous-function and metadata-table blocking, and an optional table-name allowlist. **The application layer is not a security boundary.** Always connect Trove to a dedicated **read-only role** at the database level:

```sql
-- PostgreSQL (PG14+, covers future objects)
CREATE ROLE trove_ro LOGIN PASSWORD '...';
GRANT pg_read_all_data TO trove_ro;

-- MySQL (per-database grants, fixed source IP)
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

Recommendations:

- Hide sensitive columns with column-level grants or views — at the authorization layer, not app-level masking.
- Set connection timeouts: `statement_timeout` / `lock_timeout` (PG), `MAX_EXECUTION_TIME` (MySQL session variable).
- Enforce row limits in the database with `LIMIT` / `LEAST()`; app-level limits can be bypassed.
- Optional **EXPLAIN row-count guard** (`explain_row_guard: true`): estimates the heaviest operator's rows before execution and sends oversized scans back to be narrowed — fail-open when the dialect can't be parsed.
- Multi-tenant: prefer one database + one read-only role per tenant; use RLS or procedural CTE pre-filtering when sharing a database.
- All read-only execution tools (`probe_query`, `check_result`, `explain_plan`, `search_values`) pass through the AST firewall first, and every call and result lands in the audit log (`sql_audit`).

## Knowledge Base (how it learns)

> Under platform deployment (`serve`), initialization runs via the admin API `kb/init`; the flow below is the REPL local path.

1. `/kb init` — LLM drafts table/column notes (chunked for large schemas), with optional `--docs <dir>` to import official column descriptions, plus deterministic terms and reference templates.
2. Edit the YAML under `.trove/kb/<datasource>/` to add metrics, terminology, and term→SQL mappings.
3. `/kb reload` — apply changes immediately.
4. Ask normally; when a Q&A is good, `/kb learn` → review the draft → `/kb learn --yes` to commit it as reference SQL.
5. `/kb list` — inspect knowledge counts per datasource.

**Hint Bank (lessons)**: successful corrections automatically distill into pending lessons; `/kb lessons` to review and `/kb lessons --yes` to confirm. Batch-distill from eval failures with `scripts/distill_lessons.py`.

Knowledge bases are isolated per datasource. YAML is the single source of truth (git-manageable); the SQLite mirror is runtime retrieval only.

## Evaluation

BIRD dev-set execution accuracy (EX) against real datasources with the full reflection pipeline:

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

Failed questions land in `.trove/eval/failures.jsonl` for lesson distillation. Helper scripts include `import_golden_examples.py`, `import_bird_descriptions.py`, `probe_enums.py`, `import_sqlite_to_mysql.py`, `eval_hybrid_retrieval.py`, `tune_rrf.py`, and `offline_eval.py`.

## Development

```bash
uv run pytest                     # full suite (~2600 tests, mocked LLM, zero network/keys)
uv run pytest tests/workflow/     # LangGraph graphs and nodes
uv run pytest tests/services/kb/  # knowledge base
uv run pytest -k kb               # all KB-related tests
```

Code layout: `trove/workflow/` (graphs, nodes, intent routing, rules) · `trove/services/` (datasources, KB, SQL) · `trove/agent/` (session orchestration) · `trove/llm/` (litellm gateway, agent loop, observability) · `trove/storage/` (sessions & checkpoints) · `trove/tracing/` (local traces) · `trove/cli/` (REPL and commands).

## Documentation

- `CLAUDE.md` — architecture overview, workflow layers, and hard project constraints
- `conf/agent.yml` — commented reference configuration
- `/v1/docs` — REST API documentation (when `serve` is running)

## Contributing

Contributions are welcome — bug reports, feature ideas, documentation, and pull requests. Please follow the repository's conventions, keep changes focused, and ensure tests pass before submitting.

## License

Trove is released under the [Apache License 2.0](LICENSE).
