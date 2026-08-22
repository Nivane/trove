# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Trove is an NL→SQL conversational data agent: natural-language question → LangGraph pipeline (intent routing, schema matching, SQL generation with self-validation, execution, reflection adjudication, failure rollback & correction) → Markdown answer. Questions and corrections accrue into a per-datasource knowledge base (KB); cold start is covered by deterministic rules, and accuracy grows with use. Ships with a built-in BIRD financial demo datasource (`trove/demo.py`, schema identical to the official BIRD export).

## Common Commands

```bash
uv sync                          # install deps (uv, Python >=3.12)
uv run pytest                    # full suite (~1057 tests, mocked LLM, zero network/keys, ~20s)
uv run pytest tests/workflow/    # graphs and nodes
uv run pytest tests/workflow/test_nodes.py -k extract_sql   # single test
uv run pytest -m "not slow"      # skip slow tests

uv run trove --datasource demo            # REPL (built-in BIRD financial demo)
echo "哪个地区的平均贷款金额最高?" | uv run trove-cli --datasource demo --print   # one-shot JSON output

uv run python scripts/lint_kb.py --datasource financial   # KB quality check
```

### Docker 部署（前后端独立容器）

前后端各自独立镜像、独立重建：前端容器（nginx 托管 SPA + 反代 `/v1` → 后端），后端容器（`trove serve`）。访问 `http://localhost:8080/ui/`。

```bash
docker compose up --build        # 构建并启动（后端 :8000 仅供调试，前端 :8080）
docker compose build frontend    # 只重建前端镜像（独立迭代：前端改动不动后端）
docker compose build backend     # 只重建后端镜像
docker compose restart backend   # 只重启后端，前端容器不受影响
docker compose down              # 停止并移除容器
```

- 登录：admin / `admin123`（compose 里 `TROVE_ADMIN_PASSWORD` 仅本地演练；生产由 `TROVE_ADMIN_PASSWORD` 环境变量控制）
- 真实对话需要 LLM 凭证：取消 compose 中 `~/.trove/conf` 只读挂载的注释（`- ${HOME}/.trove/conf:/root/.trove/conf:ro`），或在容器内提供 API key
- 后端镜像单独跑仍可访问 `/ui`（pip package-data 携带的 committed bundle）；本地单体开发流程不变：`uv run trove serve` + `cd frontend && npm run build`

Datasource extras (install on demand): `uv sync --extra mysql|clickhouse|duckdb|postgres`. Real-service integration tests are env-gated with `-m integration` (auto-skipped when variables are unset); see README.

Eval script `scripts/eval_bird.py` (real MySQL + full reflection pipeline): **cost-sensitive — do not launch eval runs without explicit user instruction**. Helper scripts: `distill_lessons.py` (distill Hint Bank lessons from eval failures), `import_golden_examples.py`, `import_bird_descriptions.py`, `probe_enums.py`, `import_sqlite_to_mysql.py`.

## Architecture

### Workflow layer `trove/workflow/` (core)

- **`graphs.py`** — three workflows: `reflection` (default, with self-correction loop) / `fixed` (direct pass) / `empty` (debug pass-through). Main pipeline: `route_intent → parse_date → schema_linking → planner → gen_sql → execute_sql → select → validate → reflect → (failure → analyze_error rollback) → output`. Intent routing splits query and metadata paths (the latter runs `answer_metadata → metadata_check` self-validation loop).
- **Node pattern**: every node is a `make_<name>(services...)` factory returning `async def node(state) -> dict` (returns a partial state update; passes through untouched when `state.error` is set). Services are bound into closures at graph build time (`GraphServices`).
- **`state.py`** — `WorkflowState` / `GenSQLState` (pydantic).
- **`rules.py`** — deterministic result/SQL verification rule chain (zero LLM): named `Rule`s execute in registration order, **the first failing rule wins — registration order is priority** (most specific/cheapest first). Families: F1 shape (single-value questions must return one row/column), F4 ordering, F2 filters (entities named in the question must appear as SQL conditions), F3 values (dtype/uniqueness/ranges).
- **`versions.py`** — SQL version chain + regression hard-checks: records each failed round (SQL + result signature), compares against the previous version to produce deterministic feedback (invalid fix / no progress / problem shift). Pure code, zero LLM.
- **`context_budget.py`** — optional gen-prompt blocks (examples/rules/terms/lessons/plan/history) packed by priority into a 2500-token budget.

### gen_sql (agentic by default) `trove/workflow/nodes/gen_sql.py`

- ReAct loop (`trove/llm/agent_loop.py`): the model self-validates via tools and **decides when it is done itself** (max_rounds is only a safety guard, not the stopping rule). Guard/empty-hand → degrade to the classic "generate → validate retry" subgraph (`build_gen_sql_subgraph`).
- Tools come from a registry factory: `build_sql_registry(connectors, question, lang, dialect) -> (ToolRegistry, check_hits)` (legacy `make_sql_tools` kept for tests) — `validate_sql` (SQLGlot syntax, always available), `probe_query` (read-only execution observation, 10 rows/5s), `check_result` (read-only execution then `rules.verify` rule chain), `search_values` (value lookup), plus the explicit `finish(answer)` tool (SQL delivered as its payload). probe/check share the `_probe_result` execution channel; check hits merge into `validation_hits` via the node update for eval attribution.
- KB exact hit (word overlap ≥0.95) uses the standard SQL directly, skipping generation; multi-candidate (higher temperature + few-shot rotation `_rotate_few_shots`) → `select` consensus voting.
- Node factories: `make_generate` / `make_validate`.

### Knowledge base `trove/services/kb/`

- **YAML is the single source of truth** (`.trove/kb/<datasource>/`: `schema_notes.yml`, `semantics.yml`, `examples.yml`, `rules.yml`, `lessons.yml`); the SQLite mirror is for runtime retrieval only. Datasource name = database name.
- `semantics.yml` stores an **Apache OSSIE core spec `semantic_model`** (datasets + metrics with `expression.dialects`, expressions are table-qualified — metric `datasets` anchoring is derived from `dataset.field` refs). The legacy flat `terms:` format is **not read** (zero terms + a migration warning); re-run `/kb init --overwrite` to regenerate.
- `KbService` retrieval is deterministically filtered with schema linking's `matched_tables` as anchor; `/kb init` (LLM-drafted notes + deterministic rules for terms/templates), `/kb learn` (LLM draft → human confirm → commit), lessons are a two-tier (pending/confirmed) Hint Bank.
- **KB content language must match the question language** (BIRD is English; measured: a Chinese KB drops accuracy on English questions from 96.9% to 50%, the gap being few-shot retrieval hits).

### Datasources `trove/services/datasource/`

- `adapters/base.py` defines the `DatabaseAdapter` abstract base (connect/disconnect/execute/get_schema/get_capabilities/dialect); new datasources register in `registry.py`'s `_ADAPTER_REGISTRY`. Drivers are lazily imported on demand. `catalog.py` provides table/column/statistics info (profiling Top-K values, join-hint sample-value verification).

### LLM and tests

- `trove/llm/agent_loop.py` — shared agent-loop harness (`run_agent_loop`): parallel tool dispatch, per-tool timeout/retry, round/time/token budgets, loop-steering feedback, and a `ToolRegistry` (defs + handlers + observer middleware hooks: tracing happens here). The `finish(answer)` protocol lets the model deliver a validated final payload instead of relying on "no tool calls = done"; guard/empty results call back into the caller's degradation chain (gen_sql → classic subgraph, planner → direct generation). `chat_full` returns best-effort `usage` for token budgets.
- `trove/llm/gateway.py` — litellm gateway with `LLMGateway(mock_response=...)` mode for tests; `chat`/`chat_full` (tool calling)/`chat_stream`; model selection CLI `--model` > `conf/agent.yml` > `~/.trove/conf/agent.yml`; `providers[]` supports custom api_base with `${ENV_VAR}` substitution.
- Test constraints (`tests/conftest.py`): zero API keys, zero network, all LLM mocked, all databases in-memory SQLite. Common fixtures: `mock_llm`, `sql_llm`, `sqlite_registry`, `demo_registry`, `tmp_home`; workflow tests use the `ScriptedLLM` (scripted responses + recorded prompts) pattern. `tracing.local` is a process-level global; conftest's autouse fixture ensures every test starts from an unconfigured state.

## Hard project constraints (must respect)

- **KB anti-cheating**: never manually stuff BIRD dev-set gold SQL into `.trove/kb/.../examples.yml` — content that `kb init` cannot generate deterministically is not a real accuracy gain. Fixes may only land in code/prompts (applies globally) and in KB content `kb init` can generate (schema notes, terms, enums, template examples).
- **Never run eval_bird proactively** — cost-sensitive (billed LLM calls); do not launch eval runs without explicit instruction.
- **Debug planner first**: wrong output columns from gen_sql usually root in the planner's `answer_columns` dictating output columns (the "Query plan (follow it...)" line in the user message outranks system rules; gen just obeys). Check the planner node's output in `~/.trove/runs/<run_id>.log` (look up run_id from `.trove/eval/results.jsonl` by question).
- Eval environment: `scripts/eval_bird.py` connects to MySQL `root:root@127.0.0.1:3306/financial`, dev json at `/Users/zhaolipan/Downloads/minidev/MINIDEV/mini_dev_mysql.json`; swap KBs with `--kb-dir` (flat YAML dirs supported); per-question verdicts land in `.trove/eval/results.jsonl`, failures in `failures.jsonl` for `distill_lessons.py`.
- Git habit: if a new feature, checkout a new branch, after test succeess, commit directly, merge to main.
