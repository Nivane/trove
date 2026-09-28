<div align="center">

# Trove

**Talk to your data. It answers — and learns with every question.**

*An open-source, self-hosted conversational data agent: natural language in, verified answers out — where a human-approved semantic model is the only answerable boundary.*

[English](README.en.md) · [简体中文](README.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-3700%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-ready-336791.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed.svg)]()

<br>

<img src="docs/assets/hero-chat.png" width="880" alt="Trove's chat UI: a plain-language question answered with a summary, a bar chart, and the 12-step analysis trail on the right">

<sub><i>One question, end to end — verified SQL, a chart, and every step of the reasoning, visible.</i></sub>

<br>
<br>

📖 **[Capability map](https://nivane.github.io/trove/)** — six groups of data-side capability and twelve agent-side ones, each mechanism anchored to the source line that implements it · [MySQL / Doris onboarding](docs/mysql-doris-quickstart.md)

</div>

---

## What Trove Is

Trove is a **self-learning conversational data agent**. Ask questions in your own words; get Markdown answers backed by real SQL — verified before and after execution, refused when the answer would be a guess, and improved by every question you ask.

Its promise is not "always right" but **never wrong without a fight**. Four pillars hold that promise:

- **The semantic layer sets the boundary.** A human-approved semantic model (`semantics.yml`, an Apache OSSIE semantic model) is the *only answerable scope*: datasets, metrics, fields and relationships the business has declared — nothing else. Queries outside the model are refused with a one-click model-extension path, never answered by guessing at raw tables.
- **Deterministic rails close the loop.** Generated SQL runs through a zero-LLM rule chain (shape / filters / values / ordering), an AST firewall, an execution-cost guard, and a reflection cycle with SQL-version regression. Wrong answers are diagnosed, rolled back, corrected, and the fix is remembered.
- **Decisions share the query's footing.** Query → analysis → decision is one chain: threshold rules name metrics, windows and baselines in the semantic model's own vocabulary and are evaluated by a deterministic engine, zero LLM. Every trigger carries the SQL and the raw rows it was judged on.
- **Learning compounds into an asset.** Every correction distills into a lesson, every confirmed Q&A becomes reference SQL, and cross-session memory recalls episodes and preferences. Auto-learned content lands `pending` until an admin confirms — the knowledge grows, and stays reviewable, git-manageable, yours.

## Why Semantic-First?

Raw-LLM agents write SQL from raw DDL — they are confidently wrong about business meaning. A column named `A11` or a status code `A` means nothing without the glossary, and "average loan amount" is not derivable from a schema. RAG helps: a glossary, examples and lessons anchor generation. But RAG feeds the *ammo*, it does not draw the *boundary* — retrieval misses still get answered with plausible guesses.

Trove follows the semantic-layer route taken by Cortex-Analyst-style products, with a twist that keeps answers flowing:

- **Full coverage → compile.** The plan is compiled against declared metrics, fields and relationships — the SQL is authoritative, not suggested.
- **Partial coverage → compile a skeleton, let the agent fill the gaps.** When only words, values or definitions are missing (a soft miss), joins, filters and grouping are compiled authoritatively, and the generation agent fills the uncovered parts — then a skeleton-fidelity check guards execution.
- **Structural miss → refuse, extend, re-answer.** When the model structurally cannot cover the question (unknown table, ambiguous join, fan-out), Trove refuses — and drafts a model extension for you to confirm in one step. The refusal *is* the modeling signal; coverage grows with use.

Everything a human cares about — which definitions count, which joins are legal, what the enums mean, how much is too much — is declared once in YAML and enforced on every question, instead of re-guessed on every question.

## How Trove Compares

| | A raw LLM agent | RAG-only NL2SQL | A bare semantic layer | **Trove** |
|---|:---:|:---:|:---:|:---:|
| Writes SQL from natural language | ✅ (often wrong) | ✅ | ❌ | ✅ governed |
| Draws an answerable boundary (semantic model) | ❌ | ❌ | ✅ | ✅ |
| Refuses instead of guessing outside coverage | ❌ | ❌ | partial | ✅ + one-click model extension |
| Still answers on partial coverage (soft-miss skeleton) | ❌ | ✅ (guessy) | ❌ | ✅ compiled skeleton + generated gaps |
| Verifies before execution — zero LLM | ❌ | ❌ | partial | ✅ rule chain + AST firewall + EXPLAIN guard |
| Self-corrects after execution (reflection + regression) | ❌ | ❌ | ❌ | ✅ rollback & retry with version checks |
| Conclusion → threshold judgement (decision rules, zero LLM) | ❌ | ❌ | partial (alerts can't reach the semantic layer's baselines) | ✅ declared in model vocabulary + evidence kept |
| Learns per datasource; auto content gated by humans | ❌ | partial | partial | ✅ KB + memory, `pending` until confirmed |
| Works where your data is (6 engines, self-hosted) | ✅ | ✅ | per-vendor | ✅ Apache-2.0 OSS |

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
        wf["LangGraph pipeline<br/>intent → linking → sketch → gen<br/>→ execute → reflect → attribution"]
        semantic["Semantic layer<br/>semantics.yml · compiler · row_filter"]
        kb["Knowledge base<br/>terms · examples · rules · lessons"]
        decision["Decision layer<br/>threshold rules in model vocabulary"]
        memory["Memory<br/>episodes · preferences · profiles"]
        skills["Skills<br/>org methodology · two tiers"]
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
    wf --> semantic & kb & memory & skills
    semantic --> yaml_src
    kb --> yaml_src
    decision --> yaml_src
    yaml_src --> rt
    rt --> kb
    wf -->|"read-only adapters"| sources
    semantic -->|"compile over"| sources
    decision -->|"read-only evaluation"| sources
    admin --> yaml_src
    state_store --> wf
```

### How a question becomes an answer

The main graph is **27 nodes** on LangGraph (default configuration; `clarify` — dashed below — is an optional branch that only attaches when you build with `build_graphs(clarify=True)`, so it is not counted in the 27).

```mermaid
flowchart TB
    Q["Question"] --> INTENT["route_intent"]
    INTENT -->|"data / why"| PD["parse_date<br/>relative time"]
    INTENT -->|"metadata"| MD["answer_metadata<br/>+ metadata_check self-check"]
    INTENT -->|"chitchat / correction / write"| ANS["answer_chitchat<br/>answer_correction · answer_reject"]
    INTENT -.->|"draft to confirm"| CD["confirm_draft"]

    PD --> LINK["schema_linking<br/>dataset anchoring<br/>renders semantic_context"]
    LINK --> GATE{"Semantic gate"}
    GATE -->|"no model / zero match"| REFUSE["refuse<br/>run /kb init first"]
    GATE -->|"covered"| FM{"fast_match<br/>KB exact hit?"}

    FM -->|"hit"| SQL["deterministic template SQL"]
    FM -->|"miss"| SKETCH["query_sketch<br/>LLM plan → typed IR"]

    SKETCH --> COMP{"SemanticCompiler"}
    COMP -->|"full coverage"| SQL
    COMP -->|"soft miss (word / value / definition)"| SKEL["PartialCompile skeleton<br/>joins · filters · grouping authoritative"]
    COMP -->|"hard miss (structural)"| REFUSE
    REFUSE -.->|"draft confirmed → re-enter"| CD
    CD -.-> PD

    SQL --> GEN["gen_retrieve → gen_assemble → gen_generate<br/>retrieve · assemble · generate (ReAct tool loop)"]
    SKEL --> GEN

    GEN --> SEM["semantics<br/>definition re-check"]
    SEM --> HITL{"hitl<br/>pre-execution human check (optional)"}
    HITL --> EXEC["execute_sql<br/>AST firewall · EXPLAIN row guard<br/>skeleton fidelity · row-filter injection"]
    EXEC --> SEL["select<br/>multi-candidate consensus vote"]

    SEL --> CHK{"validate<br/>zero-LLM rule chain<br/>shape · filters · values · ordering"}
    CHK -->|"violation"| FIX["analyze_error<br/>diagnose & rollback"]
    FIX --> SKETCH
    CHK -->|"pass"| REFLECT["reflect adjudication<br/>+ SQL version regression"]
    REFLECT -->|"not accepted"| FIX
    REFLECT --> ATTR{"why / root-cause?"}
    ATTR -->|"yes"| DRILL["attribution<br/>multi-hop drill-down<br/>contribution · waterfall"]
    ATTR -->|"no"| OUT["insights → chart → conclusion → output"]
    DRILL --> OUT
```

### From query to decision (zero LLM)

Query and analysis are thick; the decision step does not have to be a request buried in a prompt. A rule names its subject — metric, dimension, window, baseline — in the **semantic model's own vocabulary**; the engine resolves window and baseline deterministically, compiles SQL, and hands the numbers to a condition language. No LLM sits anywhere in that path.

```mermaid
flowchart LR
    R["decisions.yml<br/>declared in model vocabulary"] --> LINT{"lint before disk"}
    LINT -->|"fails"| REJ["refused"]
    LINT -->|"passes"| GIT["git commit<br/>reviewable · diffable · revertible"]
    GIT --> JOB["scheduled job bound to decision_rule"]
    JOB --> EVAL["DecisionService<br/>window/baseline → SQL → condition AST"]
    EVAL --> JUDGE{"condition judged<br/>three-valued logic"}
    JUDGE -->|"triggered"| ALERT["alert + evidence<br/>rule_digest · model_version<br/>SQL · raw rows"]
    JUDGE -->|"not triggered"| OK["ok record (also persisted)"]
```

- **Rules are written in business vocabulary.** `subject` references metrics and dimensions from the semantic model; the baseline can be period-over-period, year-over-year, a literal, or none; severity is `info` / `warning` / `critical`; judgement can be aggregate or per-dimension, with `any` / `all` / `top_k` emission.
- **The condition language is closed.** A hand-written tokenizer and recursive-descent parser (no `eval` — rules are untrusted input), a fixed variable set (`current` / `baseline` / `delta` / `delta_pct` / `contribution` / `row_count` / `dim`) and exactly four functions (`abs` / `min` / `max` / `pct_change`). A mistyped name is a **parse error**, not a silent no-op.
- **Three-valued logic, so no false alarms.** With no baseline, `not (x > y)` evaluates to Unknown rather than true — a missing baseline never buys you a fake alert.
- **Failure is loud.** A rule that cannot be evaluated (unresolvable window, undeclared metric, unreachable datasource) is an `error` run; it never degrades into a quiet "all fine".
- **The evidence is the audit trail.** A decision run has no LangGraph trace, so its evidence *is* the record: which semantic model (`model_version`), which version of the rule (`rule_digest`), both SQL statements, and the raw rows it judged (up to 200).
- **Rules live with the knowledge.** They sit in `.trove/kb/<datasource>/decisions.yml` — the same directory and the same git audit as terms, examples and lessons; the admin console offers a rule page and a raw YAML editor, and shows which scheduled jobs reference each rule.

### How it learns (and stays governed)

```mermaid
flowchart LR
    RUN["Every question round<br/>question · SQL · verdict · corrections"] --> OBS["observe"]
    OBS --> EP["episodes<br/>factual recall (cross-session)"]
    OBS --> DR["draft reference example<br/>(pending)"]
    OBS --> LS["draft lesson<br/>(pending)"]
    OBS --> PF["preference hints<br/>(pending)"]
    REFUSE["refuse / missed coverage"] --> XT["semantic extension draft<br/>metric · field · relationship"]

    DR & LS & XT & PF --> GATE{"Admin confirm"}
    GATE -->|"confirm"| KB["Knowledge base<br/>examples.yml · lessons.yml<br/>semantics.yml · decisions.yml"]
    GATE -->|"reject"| DISCARD["discarded"]
    EP --> MEM["memory store<br/>episodes · user facts"]
    KB --> RETR["deterministic retrieval<br/>anchored to matched datasets"]
    MEM --> RETR
    RETR --> GEN["next generation"]

    SCHEMA["live schema"] --> DRIFT{"vs declared semantics"}
    DRIFT -->|"mismatch"| STALE["drift report → rebuild / re-init<br/>preserves reviewed assets"]
    STALE --> KB
    DRIFT -->|"could not check"| UNKNOWN["recorded as unverified<br/>never as 'no problem'"]

    SKILLS["Org methodology<br/>draft → confirm"] --> GEN
```

<p align="center">
<img src="docs/assets/hero-semantic.png" width="880" alt="Trove's semantic-layer admin: 44 metrics, 8 datasets and 56 fields declared in the model, with a 10-item pending-approval queue">
</p>

<sub><i>The governance surface — what the model declares, and what is still waiting for a human to confirm.</i></sub>

## What You Get

- **Semantic-first boundary with graded answers** — full compile when covered; a compiled skeleton with agent-filled gaps on soft misses; refuse + one-click model extension on structural misses.
- **Deterministic self-validation** — a zero-LLM rule chain (shape / filters / values / ordering), AST firewall (read-only whitelist, DML interception), optional EXPLAIN row guard, and a reflection cycle with SQL version regression across retries.
- **Fast path and agentic path** — simple questions take a deterministic KB-template fast path; complex ones upgrade to an agentic ReAct loop (`validate_sql` / `probe_query` / `check_result` tools, decides when it is done); ambiguous ones generate multiple candidates and vote.
- **Decision rule layer** — threshold rules written in the semantic model's vocabulary (metric / dimension / window / baseline / severity / emission), evaluated with zero LLM; bind them to scheduled jobs and every trigger ships with evidence; rules are linted before they reach disk, and live in the KB directory under git audit. See "From query to decision" above.
- **Root-cause attribution on why-questions** — "why did revenue drop?" gets a multi-hop drill-down: current vs prior period, dimension breakdown, then into the top contributor. Contribution math is deterministic (zero LLM), the primary split dimension is chosen **by the data** (largest Σ|Δ|, not LLM order), and ratio metrics decompose by shift-share into within-group / mix / interaction effects; only the narrative is generated, and it may cite the contribution table and nothing else.
- **A knowledge base that learns per datasource** — `/kb init` drafts schema notes + a semantic model + deterministic terms and templates; confirmed Q&A becomes reference SQL; corrections distill into a Hint Bank; drift detection reports when the model falls behind the schema.
- **Semantics as code** — every KB write auto-commits, so `git log` is the audit history and `git diff` / `git revert` are review and rollback; commits can carry `Generator` / `Approved-by` trailers. Bad semantics are refused before they reach disk — and the commit gate catches them too.
- **Declarative row filters** — a dataset can declare a `row_filter` (a boolean predicate) in the semantic model, injected at compile time into the top-level WHERE of every query touching that dataset. **The fast path and the compile path share one injection implementation**, so "the fast path forgot the filter" cannot happen. It constrains what a dataset may answer — it is not a substitute for a read-only role on the database side.
- **Unified cross-session memory** — episodic recall, auto-extracted user preferences, per user × datasource profiles; automatic content always lands `pending` until an admin confirms.
- **Org-level methodology skills** — cross-datasource methodology (how to plan, how to diagnose, org conventions) managed as SKILLs with two injection tiers: `required` bodies go into the matching node's system prompt, `available` ones are merely advertised and loaded on demand through `load_skill`; drafts still need admin confirmation. Datasource facts belong in the KB; methodology belongs in skills.
- **Governance as a feature** — YAML is the single source of truth (git-reviewable, diff-able); every executed tool call is audited; HITL approval optional; admin console for KB init, draft review, drift reports, decision rules and skills.
- **Analysis you can audit** — the web UI shows the reasoning trail: schema-linking matches, compile decisions, rule-chain results, agent tool calls, and fix reasons. Generation is itself three checkpointed stages — retrieval → context assembly → generation — separately observable and testable, merged back into a single `gen_sql` step for the UI / CLI / streaming contract.
- **Errors that explain themselves** — a failed run returns a plain-language card (what happened, what to try, whether retrying is worth it) instead of leaking internal node names and error slugs; raw diagnostics fold away and are shown to admins only.
- **Web UI + REST API** — Vue SPA chat with streaming, tables, charts and HITL dialogs; everything under `/v1`, including a declarative semantic query endpoint (`/v1/semantic/query`) that compiles plans straight against the semantic model.
- **MCP server** — expose NL→SQL as tools and resources over stdio / SSE / streamable-http for Claude Code and other MCP clients.
- **Multi-engine, one pattern** — SQLite, PostgreSQL, MySQL, Doris, ClickHouse, DuckDB adapters with a per-datasource KB; unified internal storage on PostgreSQL in production (SQLite fallback in tests), with an admin checkpoint timeline to resume any run from any node.
- **LLM-agnostic and bilingual** — litellm gateway (OpenAI / DeepSeek / Anthropic / any compatible endpoint), per-node model tiers (cheap model for sketches, strong model for reflection), unified `zh` / `en` interaction.
- **Observable and interruptible** — real-dependency health checks, Prometheus metrics, request-id + run-id log correlation, per-run token cost persisted into session history (still readable after a crash or restart), optional Langfuse; cancelling a query stops the datasource driver itself, not just the awaiting coroutine.

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

To see the whole picture first: the [capability map](https://nivane.github.io/trove/) anchors every capability to the source line, so you can read and verify side by side.

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
- `POST /v1/semantic/query` — declarative semantic query API: compile a structured plan directly against the semantic model, no conversation pipeline.
- Every response carries `X-Request-ID`, echoed into every log line of that request.
- Client abort cancels end-to-end: the graph task is cancelled and the adapter's driver-level interrupt fires (sqlite3 / psycopg / MySQL `KILL QUERY` / duckdb).

Admin endpoints (`/v1/admin/*`, require the admin role or a token scope):

- **Datasources & KB** — registration (probed on the spot; failures report the reason), `kb/init`, draft review, drift reports.
- **Decision rules** — `GET /v1/admin/decisions` (list with lint issues and "which jobs reference it"), `PUT /v1/admin/decisions` (structured rules or the whole raw YAML — always through the lint gate), `GET /v1/admin/decisions/raw`.
- **Skills** — `GET/POST /v1/admin/skills` plus `draft` / `llm-draft` / `confirm` / `reject` / `tier` / `body`: draft, confirm and tier org methodology.
- **Scheduled jobs** — `POST /v1/admin/jobs` creates a scheduled question (cron / interval) with an optional threshold alert (`row_count >= 5` / `value > 1000` / `col:<name> <op> <n>` / `no_rows` / `verdict == <x>`) or a bound decision rule (`decision_rule`), channel `console` or `webhook:<url>`, cooldown minutes. Plus `GET/PATCH/DELETE /v1/admin/jobs/{id}`, `POST /v1/admin/jobs/{id}/run`, `GET /v1/admin/jobs/{id}/runs`.
- **Memory, users, audit** — `GET /v1/admin/memory/profile`, preference confirm/reject, users and datasource grants, the audit log (`GET /v1/admin/audit`), and the session checkpoint timeline (replay from any node with `replay_from`).

Scheduling: **`serve` runs an embedded scheduler tick** — due jobs execute automatically (poll interval `agent.scheduler_poll_seconds`, default 30s). Do not run `trove-cli schedule --daemon` alongside `serve` (double execution). A job bound to a decision rule runs the deterministic engine and **never enters the NL pipeline** — decision runs must be replayable and must not depend on the model.

### MCP server

```bash
uv run trove mcp                                                            # stdio (default)
uv run trove mcp --transport streamable-http --host 0.0.0.0 --port 8001 --token <secret>
```

Tools: `ask_data` · `list_datasources` · `kb_status`. Resources (read-only): `trove://datasources` · `trove://<datasource>/schema` · `trove://<datasource>/semantics`.

Binding to a non-loopback address **without** `--token` makes the server refuse to start; with a token, every HTTP request must carry `Authorization: Bearer`, and visible datasources are trimmed to that token's user's grants.

### REPL commands

| Group | Commands |
|---|---|
| Session | `/help` `/exit` `/clear` `/compact` `/tasks` |
| Knowledge | `/tables` `/table_schema` `/schemas` `/databases` `/kb …` `/init` `/facts` |
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

Each database evolves its own knowledge base under `.trove/kb/<database>/` (schema notes / semantic model / examples / rules / lessons). To add a datasource, implement the `DatabaseAdapter` methods and register it in `registry.py`; drivers are imported lazily. Step-by-step MySQL / Doris onboarding (admin console and local REPL paths): [`docs/mysql-doris-quickstart.md`](docs/mysql-doris-quickstart.md) (Chinese).

## Security (read-only execution)

For a data agent to ship, the security boundary cannot be a request written in a prompt. Everything below is **code, not model behaviour**:

| Mechanism | What it does |
|---|---|
| Read-only statement firewall | Parses SQL to an AST and judges it there: non-query statements, any DML/DDL (including data-modifying CTEs), `SELECT INTO OUTFILE`, dangerous functions (`SLEEP` / `LOAD_FILE` / `PG_READ_FILE` …) and metadata tables (`sqlite_master` / `information_schema` / `pg_catalog` …) are all refused — no keyword blacklist |
| Per-datasource table allowlist | Restricts which tables may be touched, enforced **in the execution path itself** (`execute` and `explain` alike) — so SQL arriving via MCP, the semantic query API or any bypass is bound by the same gate; metadata tables stay denied regardless |
| Execution-cost guard | EXPLAIN estimates the heaviest operator's row count before execution: over the soft limit (default 50M) the query goes back to generation to add `LIMIT` / narrow filters; over the hard limit (default 1B) it is refused outright, without burning an LLM regeneration round |
| Tool permissions | Tools are trimmed by role before the model sees them; a call to an invisible tool is folded into "unknown tool" at runtime — the model sees that the tool does not exist, not a wall it can probe |
| External-data isolation | Content coming from database cells (and retrieval/probe results) is scanned for injection patterns and replaced with an isolation marker before it reaches the prompt — human-confirmed KB content is exempt by design |
| Pre-execution human check | Optional node: review the SQL before it runs; if the payload cannot be understood, the default is to refuse, not to let it through |
| Model budget guard | The ReAct loop is bounded by rounds, wall-clock time and cumulative tokens; on hitting a limit it degrades to the classic subgraph instead of burning on |
| Credentials encrypted at rest | Passwords / tokens in datasource config are stored Fernet-encrypted (`enc:v1:…`); the key comes from `TROVE_SECRET_KEY`, or a `0600` `secret.key` is generated next to the config |
| Rate limiting and quotas | In-process per-user token bucket (default 30/min) and a daily quota (default 300); over the limit returns 429 + `Retry-After`, hot-updatable from the admin console |
| Token scopes and default-deny | A token can be scoped to `query` alone (ask questions, cannot administer); with no auth component configured the API refuses to start rather than quietly allowing everything |
| Query audit | Every execution records question / SQL / verdict / row count / duration / error, readable from the audit endpoint |

**The application layer is not a security boundary** — always connect Trove to a dedicated read-only role:

```sql
-- PostgreSQL (covers future objects)
CREATE ROLE trove_ro LOGIN PASSWORD '...';
GRANT pg_read_all_data TO trove_ro;

-- MySQL (per-database grants, fixed source IP)
CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

Also recommended: hide sensitive columns with column-level grants or views, set `statement_timeout` / `lock_timeout` (PG) or `MAX_EXECUTION_TIME` (MySQL), and enforce row limits in the database. A semantic-model `row_filter` is a **declarative filter** — it keeps rows that should not be answered out of the SQL, and does not replace the database-side boundaries above.

## Configuration

Precedence: CLI `--model` > `conf/agent.yml` > `~/.trove/conf/agent.yml`:

```yaml
agent:
  target: deepseek/deepseek-reasoner   # litellm model string
  model_fast: deepseek/deepseek-chat   # cheap tier: sketches, semantics, insights
  language: zh                         # interaction language: zh / en
  semantic_first: true                 # semantic model is the only answerable boundary
  # explain_row_guard: true            # EXPLAIN row guard (on by default)
  # explain_max_rows: 50000000         # soft limit → back to generation
  # explain_hard_max_rows: 1000000000  # hard limit → refuse
  # api_rate_per_minute: 30            # per-user rate limit (0 = off)
  # api_daily_quota: 300               # daily quota (0 = off)
  git_kb: true                         # auto-commit KB writes
  # attribution:                       # why-question root-cause drill-down (on by default)
  #   max_hops: 2                      # 1 = dimension breakdown only, 2 = + top-contributor drill
  # node_models:                       # per-node overrides (query_sketch / reflect / ...)
  #   query_sketch: deepseek/deepseek-chat
  memory:
    enabled: true                      # unified memory subsystem
    # promotion: false                 # auto-promote lessons (off by default)
```

Put API keys in a project-root `.env` (auto-loaded, gitignored) or export variables such as `DEEPSEEK_API_KEY`. Custom OpenAI-compatible endpoints go under `agent.providers`; optional Langfuse tracing via `agent.observability.tracing.enabled`; point `TROVE_STORAGE_URL` at PostgreSQL for internal state (falls back to local SQLite when unset).

## Evaluation

Reproduce accuracy on your own data and model — Trove ships a built-in BIRD financial demo (schema identical to the official BIRD export) and an evaluation script for the full reflection pipeline:

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial \
  [--limit 10] [--verbose]
```

Per-question verdicts — each with its `qid` and the tokens it cost — land in `.trove/eval/results.jsonl`, failures in `failures.jsonl`; batch-distill failures into lessons with `scripts/distill_lessons.py`. No canned numbers here — measure against your own questions, your own semantic model, your own schema.

### Offline replay (record once, score for free)

`scripts/offline_eval.py record` runs a question set with real credentials and writes the trajectory; `replay` scores it with **zero LLM calls** — completion, correctness, token cost, failure recovery — so prompt and rule changes can be iterated without paying per attempt:

```bash
uv run python scripts/offline_eval.py record --questions qs.txt --output .trove/eval/replay.jsonl
uv run python scripts/offline_eval.py replay --input .trove/eval/replay.jsonl
```

### Regression gate (zero LLM, opt-in)

`scripts/eval_gate.py` compares a baseline result file with the current one and **exits 1 on a regression**: EX, compile-hit rate, completion, recovery, gold exact match and token cost — each with its own direction and tolerance (`--tol ex=0.02`, or relative `--tol ex=0.10-r`), plus `--min-n` against under-sized samples and `--json` for CI. It consumes three artifact kinds: `results.jsonl` (eval_bird), `replay.jsonl` (offline replay), and the `--scorecard` JSON the retrieval / RRF evals emit.

A reproducible baseline ships in `eval/baseline/` — a fixed question set plus results, reconciled by stable `qid` — with `scripts/build_eval_baseline.py` to (re)build it and `scripts/eval_baseline.py check` to verify it is intact and fully covered. The gate is **off by default** (`eval.gate_enabled: false`), and so is its CI workflow: `.github/workflows/eval-gate.yml` runs only on `workflow_dispatch` or when `TROVE_RUN_EVAL_GATE=1`, and skips itself unless the config switch is on.

### Retrieval evals

Retrieval quality has its own zero-LLM scripts: `eval_retrieval.py` (recall, substructure coverage and budget pressure — lexical vs gold-table-anchored), `eval_hybrid_retrieval.py` (per-channel ablation), `eval_bird_retrieval.py` (table-level schema recall on BIRD) and `tune_rrf.py` (RRF weight grid) — the latter three can emit a scorecard for the gate.

## Development

```bash
uv run pytest                     # full suite (~3700 tests, mocked LLM, zero network/keys)
uv run pytest tests/workflow/     # LangGraph graphs and nodes
uv run pytest tests/services/kb/  # knowledge base
uv run pytest -m "not slow"       # skip slow tests
```

CI (`.github/workflows/backend.yml`) runs the same suite (non-integration, zero network, plus a PG integration subset) together with `ruff check` and a `pip-audit` dependency scan; the frontend has its own workflow (`npm run ci` plus build verification for both images).

Zero-LLM ops scripts: `scripts/lint_kb.py` (KB quality check, optional live enum probe), `scripts/check_drift.py` (declared semantics vs live schema; exit codes 0/1/2, and **"could not check" is 2, not 0** — green while the database is unreachable is a false green), `scripts/check_kb_anti_cheat.py` (fails if a KB template copies gold SQL), `scripts/check_page_anchors.py` (verifies the [capability map](https://nivane.github.io/trove/)'s source anchors still point at the same code).

Code layout: `trove/workflow/` (LangGraph graphs, nodes, deterministic rule chain) · `trove/services/` (datasource adapters, KB, semantic layer, decision rules, memory, skills, SQL) · `trove/llm/` (litellm gateway, agent loop) · `trove/storage/` (unified backend: sessions, audit, checkpoints) · `trove/agent/` (session orchestration) · `trove/cli/` (REPL and commands).

Deeper architecture notes live in `CLAUDE.md`; REST API docs at `/v1/docs` when `serve` is running.

## FAQ

**Is this a text-to-SQL framework or a BI tool?** Neither exactly — Trove is a conversational data agent. You ask, it plans, compiles, executes and verifies, then answers in Markdown with charts and an auditable trail. It does not try to be a dashboard builder.

**Why does it refuse questions?** Because the semantic model is the answerable boundary — that is the product. A refusal means "the model does not cover this", and it arrives with a drafted extension you can confirm to extend coverage and re-answer in one step. Quietly guessing at tables is the failure mode this architecture exists to prevent.

**How are decision rules different from ordinary alerts?** An ordinary alert can only say "column X > some number" — it cannot reach the baselines, calibers or ratios in the semantic layer. Trove's rules name their subject with the semantic model's own metrics and dimensions, resolve window and baseline (period-over-period, year-over-year, literal) deterministically, and evaluate conditions in a closed language (a mistyped name is a parse error, not a silent no-op). Every trigger keeps the SQL and the raw rows it judged, and the rules live beside the knowledge base under the same git audit.

**What role does RAG play?** RAG feeds generation — glossary terms, reference SQL, lessons, episodic memory — retrieved deterministically against the matched datasets. It informs *how* Trove writes SQL within coverage; it never decides *whether* the question is answerable.

**Do I need a vector database?** No. Retrieval runs on a built-in SQLite FTS5 mirror out of the box; under PostgreSQL the same storage hosts pg_bm25 + pgvector for hybrid retrieval, and episodic memory may use an embedder when one is configured. Everything degrades gracefully to lexical matching.

**Which LLM do I need?** Any litellm-compatible model. Quality guidance: strong models for reflection/adjudication, cheap ones for planning and insights (per-node tiers in `conf/agent.yml`). Note that the demo REPL answers only if LLM credentials are present — there is no silent "mock answer" fallback.

**How does the semantic model stay honest as the schema drifts?** Runtime guards (compile guardrails, table-existence checks, declarative row filters) keep behaviour safe; a drift check compares declarations against the live schema, reports `stale` in the admin console where rebuilding via `/kb init` preserves human-reviewed definitions, and runs headless as `scripts/check_drift.py` (zero LLM, exit codes 0/1/2) for cron or CI. The important part is that it **never reads "could not check" as "no problem"**: an unreachable datasource or an unreadable KB is recorded as unverified and returns a non-zero code — the gate is not greenest exactly when the database is down.

## Roadmap

Directions and their acceptance criteria live in [the capability map's roadmap section](https://nivane.github.io/trove/#road) — each one states what "done" means rather than a date. Two have already landed: drift governance (structure and reference levels, distinguishing "no drift" from "could not check") and row filters (dataset-level `row_filter` injected at compile time, one implementation shared by the fast and compile paths). Still ahead: narrowing the boundary to *who* can see what (identity and field level), verified query assets, semantic branch review, cost attribution, and an untrusted-input boundary.

## Contributing

Bug reports, feature ideas, documentation, and pull requests are welcome. Keep changes focused and make sure tests pass before submitting. Architecture and design docs for active development live with the team — open an issue to discuss larger changes first.

## License

Trove is released under the [Apache License 2.0](LICENSE).
