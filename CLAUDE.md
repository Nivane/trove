# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Trove is an NL→SQL conversational data agent: natural-language question → LangGraph pipeline (intent routing, semantic matching, SQL generation with self-validation, execution, reflection adjudication, failure rollback & correction) → Markdown answer. **语义优先（Phase B，决策 1-4）**：语义模型（`semantics.yml`）是生成的主视角与可答边界——gen_sql/planner 以语义视角为主，未覆盖查询 = 拒绝 + 反问扩展模型（LLM 草拟 draft → 管理端确认 → 重答），无语义模型的数据源整体拒绝并提示先 `/kb init`。catalog 枚举工具（`search_values` / `lookup_schema` / `explain_plan`）始终注册，agent 运行时可触达物理 schema 用于值确认 / 结构懒加载 / 执行计划（仅作生成辅助，不替代语义边界判定）。产品定位从「开箱即用」改为「**接入即建模、建模即保障**」（conf/agent.yml `semantic_first: true` 默认开）。Questions and corrections accrue into a per-datasource knowledge base (KB); accuracy grows with use. Ships with a built-in BIRD financial demo datasource (`trove/demo.py`, schema identical to the official BIRD export).

## Common Commands

```bash
uv sync                          # install deps (uv, Python >=3.12)
uv run pytest                    # full suite (~2662 tests, mocked LLM, zero network/keys, ~60s)
uv run pytest tests/workflow/    # graphs and nodes
uv run pytest tests/workflow/test_nodes.py -k extract_sql   # single test
uv run pytest tests/services/memory/   # unified memory subsystem (episodes/prefs/promotion)
uv run pytest -m "not slow"      # skip slow tests

uv run trove --datasource demo            # REPL (built-in BIRD financial demo)
echo "哪个地区的平均贷款金额最高?" | uv run trove-cli --datasource demo --print   # one-shot JSON output

uv run python scripts/lint_kb.py --db-id demo   # KB quality check (static; --db-id 默认 financial 需按实际 KB 目录指定)
uv run python scripts/lint_kb.py --db-id financial --datasource mysql://root:root@127.0.0.1:3306/financial   # + live enum probe
```

### Docker 部署（前后端独立容器）

前后端各自独立镜像、独立重建：前端容器（nginx 托管 SPA + 反代 `/v1` → 后端），后端容器（`trove serve`）。访问 `http://localhost:8080/`。

```bash
docker compose up --build        # 构建并启动（后端 :8000 仅供调试，前端 :8080）
docker compose build frontend    # 只重建前端镜像（独立迭代：前端改动不动后端）
docker compose build backend     # 只重建后端镜像
docker compose restart backend   # 只重启后端，前端容器不受影响
docker compose down              # 停止并移除容器
```

- 登录：admin / `admin123`（compose 里 `TROVE_ADMIN_PASSWORD` 仅本地演练；生产由 `TROVE_ADMIN_PASSWORD` 环境变量控制）
- **默认业务栈 = PostgreSQL**：`postgres` 服务用 `pgvector/pgvector:pg16` 镜像（业务表 + pgvector 向量同实例），`db-init` 一次性服务把 BIRD 金融 demo 数据灌入（幂等，`scripts/init_postgres_demo.py`），后端 `--datasource postgres://trove:trove@postgres:5432/trove`；向量后端默认 `pgvector`（`vector_dsn` 留空 = 同实例推导）。换回内置 SQLite demo 可改 `--datasource demo`，生产数据源改由管理端注册（`/admin/datasources`，持久化到 `.trove/datasources.yml`，重启自动恢复）
- 管理端数据源流程：admin 登录 → 注册（内置 demo 或 URL，注册即连接探测，失败 400 报原因）→ 该源 `kb/init`（LLM 起草 schema 注释 + 确定性 terms/templates；无 LLM 凭证时按配置走纯骨架或报凭证错误）→ 用户端下拉/列表才可见（仅显示「已连接且 KB 已初始化」的数据源，非 admin 还需 grants 授权）
- 真实对话需要 LLM 凭证：取消 compose 中 `~/.trove/conf` 只读挂载的注释（`- ${HOME}/.trove/conf:/root/.trove/conf:ro`），或在容器内提供 API key
- 后端镜像是不带页面的纯 JSON API（所有路由在 `/v1` 下）；前端由独立容器发布（`frontend/Dockerfile` 多阶段构建：`npm run build` → `frontend/dist` → nginx 运行时）。本机开发：后端 `uv run trove serve`（:8000）+ 前端 `cd frontend && npm run dev`（:5173，HMR，反代 `/v1` → 后端）。

Datasource extras (install on demand): `uv sync --extra mysql|clickhouse|duckdb|postgres` (`postgres` = psycopg,业务 + pgvector 向量驱动). Real-service integration tests are env-gated with `-m integration` (auto-skipped when variables are unset; Postgres uses `PG_TEST_URL`); see README.

Eval script `scripts/eval_bird.py` (real MySQL + full reflection pipeline): **cost-sensitive — do not launch eval runs without explicit user instruction**. Helper scripts: `distill_lessons.py` (distill Hint Bank lessons from eval failures), `import_golden_examples.py`, `import_bird_descriptions.py`, `probe_enums.py`, `import_sqlite_to_mysql.py`, `init_postgres_demo.py` (BIRD demo → PostgreSQL).

## Architecture

### Workflow layer `trove/workflow/` (core)

- **`graphs.py`** — three workflows: `reflection` (default, with self-correction loop) / `fixed` (direct pass) / `empty` (debug pass-through). Main query pipeline: `route_intent → parse_date → schema_linking → (refuse/clarify 语义门禁) → fast_match 确定性快径 → planner(条件节点) → gen_sql[subgraph] → semantics → hitl(可选人工确认) → execute_sql → select → validate → reflect → (失败 → analyze_error 回滚) → insights → chart → conclusion → output`. Intent routing splits query and metadata paths (the latter runs `answer_metadata → metadata_check` self-validation loop); `planner` is a conditional node (skipped when the graph is built without it).
- **Node pattern**: every node is a `make_<name>(services...)` factory returning `async def node(state) -> dict` (returns a partial state update; passes through untouched when `state.error` is set). Services are bound into closures at graph build time (`GraphServices`).
- **`state.py`** — `WorkflowState` / `GenSQLState` (pydantic).
- **`rules.py`** — deterministic result/SQL verification rule chain (zero LLM): named `Rule`s execute in registration order, **the first failing rule wins — registration order is priority** (most specific/cheapest first). Families: F1 shape (single-value questions must return one row/column), F4 ordering, F2 filters (entities named in the question must appear as SQL conditions), F3 values (dtype/uniqueness/ranges).
- **`versions.py`** — SQL version chain + regression hard-checks: records each failed round (SQL + result signature), compares against the previous version to produce deterministic feedback (invalid fix / no progress / problem shift). Pure code, zero LLM.
- **`context_budget.py`** — optional gen-prompt blocks (examples/rules/terms/lessons/plan/history/episodes) packed by priority into a complexity-tiered token budget (simple 1500 / standard 2500 / complex 4000, `COMPLEXITY_BUDGET_TOKENS` in `graphs.py`). Item-level fill (`assemble_context`) keeps a block's most relevant items under budget; block priorities in `graphs.py` (`few_shots:1 > user_facts:2 > rules:3 > term_notes:4 > metrics:5 > entities:6 > lessons:7 > episodes:8 > plan:8 > history:9`).

### gen_sql (agentic by default) `trove/workflow/nodes/gen_sql.py`

- ReAct loop (`trove/llm/agent_loop.py`): the model self-validates via tools and **decides when it is done itself** (max_rounds is only a safety guard, not the stopping rule). Guard/empty-hand → degrade to the classic "generate → validate retry" subgraph (`build_gen_sql_subgraph`).
- Tools come from a registry factory: `build_sql_registry(connectors, question, lang, dialect, *, finish=True, matched_tables=None, datasource="") -> (ToolRegistry, check_hits)` (legacy `make_sql_tools` kept for tests) — `validate_sql` (SQLGlot syntax, always available), `probe_query` (read-only execution observation, 10 rows/5s), `check_result` (read-only execution then `rules.verify` rule chain), plus the explicit `finish(answer)` tool (SQL delivered as its payload), and the catalog tools `search_values` / `lookup_schema` / `explain_plan` (always registered — agent may reach the physical schema for value confirmation / lazy schema load / execution plan as generation aids). probe/check share the `_probe_result` execution channel; check hits merge into `validation_hits` via the node update for eval attribution.
- KB exact hit (word overlap ≥0.95) uses the standard SQL directly, skipping generation; multi-candidate (higher temperature + few-shot rotation `_rotate_few_shots`) → `select` consensus voting.
- Node factories: `make_generate` / `make_validate`.

### Knowledge base `trove/services/kb/`

- **YAML is the single source of truth** (`.trove/kb/<datasource>/`: `schema_notes.yml`, `semantics.yml`, `examples.yml`, `rules.yml`, `lessons.yml`); the SQLite mirror is for runtime retrieval only. Datasource name = database name.
- `semantics.yml` stores an **Apache OSSIE core spec `semantic_model`** (datasets + metrics with `expression.dialects`, expressions are table-qualified — metric `datasets` anchoring is derived from `dataset.field` refs). The legacy flat `terms:` format is **not read** (zero terms + a migration warning); re-run `/kb init --overwrite` to regenerate.
- `KbService` retrieval is deterministically filtered with schema linking's `matched_tables` as anchor; `/kb init` (LLM-drafted notes + deterministic rules for terms/templates), `/kb learn` (LLM draft → human confirm → commit), lessons are a two-tier (pending/confirmed) Hint Bank.
- **KB content language must match the question language** (BIRD is English; measured once: a Chinese KB drops English-question accuracy substantially — the dominant gap is few-shot retrieval hits, not generation). Treat this as a qualitative constraint; re-verify on your own eval before relying on the exact number.

### Memory subsystem `trove/services/memory/` (unified facade)

One front door over all of Trove's memory layers, replacing scattered per-module reads/writes. **Deterministic gate first, auto-writes pending-only** — every channel keeps the repo's "zero lexical hit = don't return" philosophy, and automatic content (episodes/observations/preferences) never bypasses admin confirmation.

- **`models.py`** — `MemoryConfig` (progressive-enablement switches, `conf/agent.yml` → `agent.memory`), `MemoryScope` (datasource × user), `MemoryEntry` (unified item shape consumed by the context budget).
- **`service.py`** — `MemoryService` facade: `observe` (automatic write-back after one run), `store` / `retrieve` (unified read dispatch by kind), `run_lifecycle` (retention purge + schema drift), `profile` (per user×datasource). All calls best-effort — memory failure never blocks a query.
- **`episode.py`** — **episodic memory** (P1): cross-session `question → SQL → verdict → correction_history → matched_tables` recall in `~/.trove/memory/episodes.sqlite`, deduped by `(datasource, user_id, question, sql)`. Read path = `relevance_score` gate (≥0.5) + recency boost, zero LLM/vectors. Injected into gen_sql as the `episodes` context block (success SQL = few-shot anchor; failure + corrections = counter-example).
- **`preferences.py`** — **auto user-preference learning** (P3): at session compaction, LLM extracts durable calibers/preferences (`memory/preference_extract.{en,zh}.j2`); high-confidence candidates commit straight to `user_facts`, low-confidence land as confirmable pending drafts (`~/.trove/memory/preferences.sqlite`). Noise filter rejects chit-chat/one-off requests.
- **`promotion.py`** — **auto-promotion** (P3, opt-in `memory.promotion: true`): confidence accumulator over evidence (repeated-correction pass / upvote / lesson reuse); crossing `promotion_threshold` auto-confirms a pending lesson via `KbService.update_lesson_confidence` (still human-auditable/revertible in `lessons.yml`).
- **`observations`** (in `service.observe`) — success → `draft_example(..., tags=["auto"])` pending example; correction history → pending Hint Bank lesson with `confidence`/`source`; hard failure → LLM-distilled pending lesson (`lesson_distill` reuse, `is_noise_lesson` filter).
- **`lifecycle`** (in `service.run_lifecycle`) — retention purge for episodes/preferences/user-facts (`UserFactsService.purge_expired`, previously dead code — now wired into the periodic sweep) / retrieval query log; plus `schema_drift.py` comparing live catalog vs KB `schema_notes.yml` columns (zero LLM).
- **`profile.py`** — per (user, datasource) correctness rate / failure patterns / committed facts; admin endpoint `GET /admin/memory/profile`, optional generation hint via `memory.profile_boost`.

Wiring: `main.py` builds `MemoryService` (kb/user_facts/llm/connectors/catalog); `GraphServices.memory` feeds the `episodes` block; `SessionManager._observe_memory` replaces the single-path `_capture_lessons` (which remains as fallback when the facade is absent); `api/app.py` periodic sweep runs `run_lifecycle`. Admin: `/admin/memory/profile`, `/admin/memory/preferences` (+confirm/reject).

### Datasources `trove/services/datasource/`

- `adapters/base.py` defines the `DatabaseAdapter` abstract base (connect/disconnect/execute/get_schema/get_capabilities/dialect); new datasources register in `registry.py`'s `_ADAPTER_REGISTRY`. Drivers are lazily imported on demand. `catalog.py` provides table/column/statistics info (profiling Top-K values, join-hint sample-value verification).

### LLM and tests

- `trove/llm/agent_loop.py` — shared agent-loop harness (`run_agent_loop`): parallel tool dispatch, per-tool timeout/retry, round/time/token budgets, loop-steering feedback, and a `ToolRegistry` (defs + handlers + observer middleware hooks: tracing happens here). The `finish(answer)` protocol lets the model deliver a validated final payload instead of relying on "no tool calls = done"; guard/empty results call back into the caller's degradation chain (gen_sql → classic subgraph, planner → direct generation). `chat_full` returns best-effort `usage` for token budgets.
- `trove/llm/gateway.py` — litellm gateway with `LLMGateway(mock_response=...)` mode for tests; `chat`/`chat_full` (tool calling)/`chat_stream`; model selection CLI `--model` > `conf/agent.yml` > `~/.trove/conf/agent.yml`; `providers[]` supports custom api_base with `${ENV_VAR}` substitution.
- Test constraints (`tests/conftest.py`): zero API keys, zero network, all LLM mocked, all databases in-memory SQLite. Common fixtures: `mock_llm`, `sql_llm`, `sqlite_registry`, `demo_registry`, `tmp_home`; workflow tests use the `ScriptedLLM` (scripted responses + recorded prompts) pattern. `tracing.local` is a process-level global; conftest's autouse fixture ensures every test starts from an unconfigured state.

## Hard project constraints (must respect)

- **KB anti-cheating**: never manually stuff BIRD dev-set gold SQL into `.trove/kb/.../examples.yml` — content that `kb init` cannot generate deterministically is not a real accuracy gain. Fixes may only land in code/prompts (applies globally) and in KB content `kb init` can generate (schema notes, terms, enums, template examples).
- **Auto memory never bypasses confirmation**: the memory subsystem's automatic writes (episodes are the exception — they are factual "what happened" records) land as `pending` drafts and must NOT enter retrieval until an admin confirms (`lessons.yml`/`examples.yml` `confirmed`/`pending` fields are the gate). Do not change this to auto-inject — it protects the human-curated KB from pollution (see `MemoryConfig.promotion` default-off).
- **Never run eval_bird proactively** — cost-sensitive (billed LLM calls); do not launch eval runs without explicit instruction.
- **Debug planner first**: wrong output columns from gen_sql usually root in the planner's `answer_columns` dictating output columns (the "Query plan (follow it...)" line in the user message outranks system rules; gen just obeys). Check the planner node's output in `~/.trove/runs/<run_id>.log` (look up run_id from `.trove/eval/results.jsonl` by question).
- Eval environment: `scripts/eval_bird.py` connects to MySQL `root:root@127.0.0.1:3306/financial`, dev json at `/Users/zhaolipan/Downloads/minidev/MINIDEV/mini_dev_mysql.json`; swap KBs with `--kb-dir` (flat YAML dirs supported); per-question verdicts land in `.trove/eval/results.jsonl`, failures in `failures.jsonl` for `distill_lessons.py`.
- Git habit: for a new feature, checkout a new branch; after tests pass, commit directly, then merge the branch to main — but **do not push** (keep origin/main untouched), and never delete the feature branch. Make sure no design docs or secret configs land in any commit.
