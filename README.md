<div align="center">

# Trove

**Talk to your data. It answers — and learns with every question.**

*An open-source, governed conversational data agent: natural language in, verified answers out — where a human-approved semantic layer is the only answerable boundary.*

[English](README.md) · [简体中文](README.zh-CN.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-2700%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-ready-336791.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed.svg)]()

</div>

---

## What Trove Is

Trove is a **self-learning conversational data agent**. Ask questions in your own words; get Markdown answers backed by real SQL — verified before and after execution, refused when the answer would be a guess, and improved by every question you ask.

Its promise is not "always right" but **never wrong without a fight**. Three pillars hold that promise:

- **The semantic layer sets the boundary.** A human-approved semantic model (`semantics.yml`, an Apache OSSIE semantic model) is the *only answerable scope*: datasets, metrics, fields and relationships the business has declared — nothing else. Queries outside the model are refused with a one-click model-extension path, never answered by guessing at raw tables.
- **Deterministic rails close the loop.** Generated SQL runs through a zero-LLM rule chain (shape / filters / values / ordering), an AST firewall, and a reflection cycle with SQL-version regression. Wrong answers are diagnosed, rolled back, corrected, and the fix is remembered.
- **Learning compounds into an asset.** Every correction distills into a lesson, every confirmed Q&A becomes reference SQL, and cross-session memory recalls episodes and preferences. Auto-learned content lands `pending` until an admin confirms — the knowledge grows, and stays reviewable, git-manageable, yours.

## Why Semantic-First?

Raw-LLM agents write SQL from raw DDL — they are confidently wrong about business meaning. A column named `A11` or a status code `A` means nothing without the glossary, and "average loan amount" is not derivable from a schema. RAG helps: a glossary, examples and lessons anchor generation. But RAG feeds the *ammo*, it does not draw the *boundary* — retrieval misses still get answered with plausible guesses.

Trove follows the semantic-layer route taken by Cortex-Analyst-style products, with a twist that keeps answers flowing:

- **Full coverage → deterministic compile.** The plan is compiled against declared metrics, fields and relationships — the SQL is authoritative, not suggested.
- **Partial coverage → compile a skeleton, let the agent fill the gaps.** When only words, values or definitions are missing (a soft miss), joins, filters and grouping are compiled authoritatively, and the generation agent fills the uncovered parts — then a skeleton-fidelity check guards execution.
- **Structural miss → refuse, extend, re-answer.** When the model structurally cannot cover the question (unknown table, ambiguous join, fan-out), Trove refuses — and drafts a model extension for you to confirm in one step. The refusal *is* the modeling signal; coverage grows with use.

Everything a human cares about — which definitions count, which joins are legal, what the enums mean — is declared once in YAML and enforced on every question, instead of re-guessed on every question.

## How Trove Compares

| | A raw LLM agent | RAG-only NL2SQL | A bare semantic layer | **Trove** |
|---|:---:|:---:|:---:|:---:|
| Writes SQL from natural language | ✅ (often wrong) | ✅ | ❌ | ✅ governed |
| Draws an answerable boundary (semantic model) | ❌ | ❌ | ✅ | ✅ |
| Refuses instead of guessing outside coverage | ❌ | ❌ | partial | ✅ + one-click model extension |
| Still answers on partial coverage (soft-miss skeleton) | ❌ | ✅ (guessy) | ❌ | ✅ compiled skeleton + generated gaps |
| Verifies before execution — zero LLM | ❌ | ❌ | partial | ✅ rule chain + AST firewall + EXPLAIN guard |
| Self-corrects after execution (reflection + regression) | ❌ | ❌ | ❌ | ✅ rollback & retry with version checks |
| Learns per datasource; auto content gated by humans | ❌ | partial | partial | ✅ KB + memory, `pending` until confirmed |
| Works where your data is (6+ engines, self-hosted) | ✅ | ✅ | per-vendor | ✅ Apache-2.0 OSS |

## Architecture

### System overview

```mermaid
flowchart TB
    subgraph clients["Clients"]
        UI["Web UI<br/>(Vue SPA)"]
        REPL["CLI / REPL"]
        MCP["MCP server<br/>tools + resources"]
        API["REST API /v1<br/>SSE streaming"]
    end

    subgraph core["Trove core"]
        wf["LangGraph pipeline<br/>intent → linking → sketch → gen_sql<br/>→ execute → reflect"]
        semantic["Semantic layer<br/>semantics.yml · compiler · guardrails"]
        kb["Knowledge base<br/>terms · examples · rules · lessons"]
        memory["Memory<br/>episodes · preferences · profiles"]
        llm["LLM gateway<br/>litellm · any provider"]
        admin["Admin & governance<br/>kb init · draft review · drift"]
    end

    subgraph store["Storage"]
        yaml_src["YAML source of truth<br/>(git-manageable)"]
        rt["Runtime retrieval<br/>FTS5 / pg_bm25 + pgvector"]
        state_store["Internal state<br/>sessions · audit · checkpoints<br/>PostgreSQL prod · SQLite fallback"]
    end

    subgraph sources["Data sources"]
        src_pg["PostgreSQL"]
        src_my["MySQL / Doris"]
        src_ch["ClickHouse"]
        src_dk["DuckDB / SQLite"]
    end

    UI & REPL & MCP & API --> wf
    wf <--> llm
    wf --> semantic & kb & memory
    semantic --> yaml_src
    kb --> yaml_src
    yaml_src --> rt
    rt --> kb
    wf -->|"read-only adapters"| sources
    semantic -->|"compile over"| sources
    admin --> yaml_src
    state_store --> wf
```

### How a question becomes an answer

```mermaid
flowchart TB
    Q["Question"] --> INTENT["route_intent"]
    INTENT -->|"query"| PD["parse_date<br/>time context"]
    INTENT -->|"metadata"| MD["answer_metadata<br/>+ metadata self-check"]
    PD --> LINK["schema_linking<br/>dataset anchoring<br/>renders semantic_context"]

    LINK --> GATE{"Semantic gate"}
    GATE -->|"no model"| REFUSE["refuse<br/>run /kb init first"]
    GATE -->|"zero match"| REFUSE
    GATE -->|"covered"| FM{"fast_match<br/>KB exact hit?"}

    FM -->|"hit"| SQL["deterministic template SQL"]
    FM -->|"miss"| SKETCH["query_sketch<br/>LLM plan → typed IR"]

    SKETCH --> COMP{"SemanticCompiler"}
    COMP -->|"full coverage"| SQL
    COMP -->|"soft miss (word / value / definition)"| SKEL["PartialCompile skeleton<br/>joins · filters · grouping authoritative"]
    COMP -->|"hard miss (structural)"| REFUSE

    SQL --> GEN["gen_sql agent<br/>validate_sql · probe · check_result"]
    SKEL --> GEN
    REFUSE -->|"draft confirmed → re-enter"| PD

    GEN --> EXEC["execute_sql<br/>AST firewall · read-only<br/>skeleton-fidelity check"]
    EXEC --> CHK{"Zero-LLM rule chain<br/>shape · filters · values · ordering"}
    CHK -->|"violation"| FIX["analyze_error<br/>diagnose & rollback"]
    FIX --> SKETCH
    CHK -->|"pass"| REFLECT["reflect adjudication<br/>+ SQL version regression"]
    REFLECT --> OUT["answer + charts + insights"]
```

### How it learns (and stays governed)

```mermaid
flowchart LR
    RUN["Every question round<br/>question · SQL · verdict · corrections"] --> OBS["observe"]
    OBS --> EP["episodes<br/>factual recall (cross-session)"]
    OBS --> DR["draft reference example<br/>(pending)"]
    OBS --> LS["draft lesson<br/>(pending)"]
    REFUSE["refuse / missed coverage"] --> XT["semantic extension draft<br/>metric · field · relationship"]
    OBS --> PF["preference hints<br/>(pending)"]

    DR & LS & XT & PF --> GATE{"Admin confirm"}
    GATE -->|"confirm"| KB["Knowledge base<br/>examples.yml · lessons.yml<br/>semantics.yml · rules.yml"]
    GATE -->|"reject"| DISCARD["discarded"]
    EP --> MEM["memory store<br/>episodes · user facts"]
    KB --> RETR["deterministic retrieval<br/>anchored to matched datasets"]
    MEM --> RETR
    RETR --> GEN["next generation"]

    SCHEMA["live schema"] --> DRIFT{"vs declared semantics"}
    DRIFT -->|"stale"| STALE["drift report → rebuild / re-init<br/>preserves reviewed assets"]
    STALE --> KB
```

## What You Get

- **Semantic-first boundary with graded answers** — full compile when covered; a compiled skeleton with agent-filled gaps on soft misses; refuse + one-click model extension on structural misses.
- **Deterministic self-validation** — a zero-LLM rule chain (shape / filters / values / ordering), AST firewall (read-only whitelist, DML interception), optional EXPLAIN row guard, and a reflection cycle with SQL version regression across retries.
- **Fast path and agentic path** — simple questions take a deterministic KB-template fast path; complex ones upgrade to an agentic ReAct loop (`validate_sql` / `probe_query` / `check_result` tools, decides when it is done); ambiguous ones generate multiple candidates and vote.
- **A knowledge base that learns per datasource** — `/kb init` drafts schema notes + a semantic model + deterministic terms and templates; confirmed Q&A becomes reference SQL; corrections distill into a Hint Bank; drift detection reports when the model falls behind the schema.
- **Unified cross-session memory** — episodic recall, auto-extracted user preferences, per user × datasource profiles; automatic content always lands `pending` until an admin confirms.
- **Governance as a feature** — YAML is the single source of truth (git-reviewable, diff-able); every executed tool call is audited; HITL approval optional; admin console for KB init, draft review and drift reports.
- **Analysis you can audit** — the web UI shows the reasoning trail: schema-linking matches, compile decisions, rule-chain results, agent tool calls, and fix reasons.
- **Web UI + REST API** — Vue SPA chat with streaming, tables, charts and HITL dialogs; everything under `/v1`, including a declarative semantic query endpoint (`/v1/semantic/query`) that compiles plans straight against the semantic model.
- **MCP server** — expose NL→SQL as tools and resources over stdio / SSE / streamable-http for Claude Code and other MCP clients.
- **Multi-engine, one pattern** — SQLite, PostgreSQL, MySQL, Doris, ClickHouse, DuckDB adapters with a per-datasource KB; unified internal storage on PostgreSQL in production (SQLite fallback in tests), with an admin checkpoint timeline to resume any run from any node.
- **LLM-agnostic and bilingual** — litellm gateway (OpenAI / DeepSeek / Anthropic / any compatible endpoint), per-node model tiers (cheap model for sketches, strong model for reflection), unified `zh` / `en` interaction.
- **Observable and interruptible** — real-dependency health checks, Prometheus metrics, request-id tracing, optional Langfuse; cancelling a query stops the datasource driver itself, not just the awaiting coroutine.

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

### First steps with your own data

1. **Connect** — a database URL (or the built-in demo): `--datasource postgres://user:pass@host:5432/db`.
2. **Model** — run `/kb init`: it drafts the initial semantic model (datasets, fields, metrics) from your live schema. No model, no answers: Trove refuses politely until you do — that is the boundary working.
3. **Ask** — then answer naturally. When Trove refuses, confirm the drafted model extension to extend coverage and re-answer in one step.

Under server deployment, the same flow runs in the admin console (datasource registration → KB init → draft review).

### Docker

Frontend (nginx-served SPA + `/v1` reverse proxy) and backend (pure JSON API) are independent images, built and restarted independently:

```bash
docker compose up --build          # build & start (backend :8000, frontend :8080)
docker compose build frontend      # rebuild only the frontend image
docker compose restart backend     # restart only the backend
docker compose down
```

Open `http://localhost:8080/` (default local login: admin / `admin123` — production uses the `TROVE_ADMIN_PASSWORD` env var). The compose stack defaults to PostgreSQL with the BIRD demo pre-loaded. Real conversations need LLM credentials: uncomment the read-only `~/.trove/conf` mount in `docker-compose.yml`, or provide an API key inside the container.

## Interfaces

### Web UI and API

The backend is a pure JSON API (everything under `/v1`, including SSE streaming chat); the frontend is a separately built Vue SPA.

```bash
uv run trove serve --datasource demo      # backend (API only)
cd frontend && npm run dev                # local dev → http://localhost:5173/
cd frontend && npm run build              # production build → frontend/dist/
```

Ops endpoints (no auth, no sensitive data):

- `GET /v1/health` — real dependency checks: pings internal storage and every connected datasource (`SELECT 1`), reports LLM config presence (no billed probe). `200` + `"status": "ok" | "degraded"` when serving; `503` when internal storage is unreachable.
- `GET /v1/metrics` — Prometheus text exposition: HTTP by route/status, LLM attempts/tokens by provider/model, SQL executions by datasource, in-flight gauge.
- `GET /v1/semantic/query` — declarative semantic query API: compile a structured plan directly against the semantic model, no conversation pipeline.
- Every response carries `X-Request-ID`, echoed into every log line of that request.
- Client abort cancels end-to-end: the graph task is cancelled and the adapter's driver-level interrupt fires (sqlite3 / psycopg / MySQL `KILL QUERY` / duckdb).

### MCP server

```bash
uv run trove mcp                                                            # stdio (default)
uv run trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 --token <secret>
```

Tools: `ask_data` · `list_datasources` · `kb_status`. Resources (read-only): `trove://datasources` · `trove://<datasource>/schema` · `trove://<datasource>/semantics`.

### REPL commands

| Group | Commands |
|---|---|
| Session | `/help` `/exit` `/clear` `/compact` `/tasks` |
| Knowledge | `/tables` `/schemas` `/kb …` `/init` `/facts` |
| System | `/model [model]` `/datasource [name]` `/trace` |

### Data sources

| Datasource | Connection | Install |
|---|---|---|
| SQLite | `--datasource demo` / `sqlite:///path/to.db` | built-in |
| PostgreSQL | `postgres://user:pass@host:5432/database` | `uv sync --extra postgres` |
| MySQL | `mysql://user:pass@host:3306/database` | `uv sync --extra mysql` |
| Doris | `doris://user:pass@host:9030/database` | `uv sync --extra doris` |
| ClickHouse | `clickhouse://user:pass@host:8123/database` | `uv sync --extra clickhouse` |
| DuckDB | `duckdb:///path/to.duckdb` | `uv sync --extra duckdb` |

Each database evolves its own knowledge base under `.trove/kb/<database>/`. To add a datasource, implement the `DatabaseAdapter` methods and register it in `registry.py`.

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

Also recommended: hide sensitive columns with column-level grants or views, set `statement_timeout` / `lock_timeout` (PG) or `MAX_EXECUTION_TIME` (MySQL), and enforce row limits in the database. Every executed tool call lands in the audit log.

## Configuration

Precedence: CLI `--model` > `conf/agent.yml` > `~/.trove/conf/agent.yml`:

```yaml
agent:
  target: deepseek/deepseek-reasoner   # litellm model string
  model_fast: deepseek/deepseek-chat   # cheap tier: sketches, semantics, insights
  language: zh                         # interaction language: zh / en
  semantic_first: true                 # semantic model is the only answerable boundary
  # node_models:                       # per-node overrides (query_sketch / reflect / ...)
  #   query_sketch: deepseek/deepseek-chat
  memory:
    enabled: true                      # unified memory subsystem
```

Put API keys in a project-root `.env` (auto-loaded, gitignored) or export variables such as `DEEPSEEK_API_KEY`. Custom OpenAI-compatible endpoints go under `agent.providers`; optional Langfuse tracing via `agent.observability.tracing.enabled`.

## Evaluation

Reproduce accuracy on your own data and model — Trove ships a built-in BIRD financial demo (schema identical to the official BIRD export) and an evaluation script for the full reflection pipeline:

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

Verdicts land in `.trove/eval/results.jsonl`, failures in `failures.jsonl`; batch-distill failures into lessons with `scripts/distill_lessons.py`. No canned numbers here — measure against your own questions, your own semantic model, your own schema. (Full test suite: ~2700 tests, mocked LLM, zero network and zero API keys.)

## Development

```bash
uv run pytest                     # full suite (~2700 tests, zero network/keys)
uv run pytest tests/workflow/     # LangGraph graphs and nodes
uv run pytest tests/services/kb/  # knowledge base
uv run pytest -m "not slow"       # skip slow tests
```

Code layout: `trove/workflow/` (LangGraph graphs, nodes, deterministic rule chain) · `trove/services/` (datasource adapters, KB, semantic layer, memory, SQL) · `trove/llm/` (litellm gateway, agent loop) · `trove/storage/` (unified backend: sessions, audit, checkpoints) · `trove/agent/` (session orchestration) · `trove/cli/` (REPL and commands).

Deeper architecture notes live in `CLAUDE.md`; REST API docs at `/v1/docs` when `serve` is running.

## FAQ

**Is this a text-to-SQL framework or a BI tool?** Neither exactly — Trove is a conversational data agent. You ask, it plans, compiles, executes and verifies, then answers in Markdown with charts and an auditable trail. It does not try to be a dashboard builder.

**Why does it refuse questions?** Because the semantic model is the answerable boundary — that is the product. A refusal means "the model does not cover this", and it arrives with a drafted extension you can confirm to extend coverage and re-answer in one step. Quietly guessing at tables is the failure mode this architecture exists to prevent.

**What role does RAG play?** RAG feeds generation — glossary terms, reference SQL, lessons, episodic memory — retrieved deterministically against the matched datasets. It informs *how* Trove writes SQL within coverage; it never decides *whether* the question is answerable.

**Do I need a vector database?** No. Retrieval runs on a built-in SQLite FTS5 mirror out of the box; under PostgreSQL the same storage hosts pg_bm25 + pgvector for hybrid retrieval, and episodic memory may use an embedder when one is configured. Everything degrades gracefully to lexical matching.

**Which LLM do I need?** Any litellm-compatible model. Quality guidance: strong models for reflection/adjudication, cheap ones for planning and insights (per-node tiers in `conf/agent.yml`). Note that the demo REPL answers only if LLM credentials are present — there is no silent "mock answer" fallback.

**How does the semantic model stay honest as the schema drifts?** Runtime guards (compile guardrails, table-existence validation) keep behavior safe; an active drift check compares declarations against the live schema and reports `stale` in the admin console, where rebuilding via `/kb init` preserves human-reviewed definitions.

## Contributing

Bug reports, feature ideas, documentation, and pull requests are welcome. Keep changes focused and make sure tests pass before submitting. Architecture and design docs for active development live with the team — open an issue to discuss larger changes first.

## License

Trove is released under the [Apache License 2.0](LICENSE).
