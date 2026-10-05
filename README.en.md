<div align="center">

# Trove

**Ask · Analyze · Decide · Act — in one conversation.**

*An open-source, self-hosted **data decision agent**: natural language in, verified answers out — the same human-approved semantic model bounds what is answerable, backs zero-LLM scheduled verdicts that know their own uncertainty, and measures the effect of the action that followed.*

[English](README.en.md) · [简体中文](README.md)

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.12%2B-blue.svg)]()
[![Tests](https://img.shields.io/badge/tests-7400%2B-brightgreen.svg)]()
[![Powered by LangGraph](https://img.shields.io/badge/powered_by-LangGraph-black.svg)]()
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-ready-336791.svg)]()
[![MCP](https://img.shields.io/badge/MCP-server-7c3aed.svg)]()

<br>

<a href="https://nivane.github.io/trove/"><img src="assets/banner.png" alt="Trove — a data decision agent: natural language in, verified answers out. One question flowing through the query pipeline (intent routing → semantic binding → semantic gate → plan &amp; compile → generate &amp; consensus → pre-execution gates → validate &amp; mask → reflect → deliver), next to a real chat session with its analysis panel" width="880"></a>

<br>

📖 **[Documentation](https://nivane.github.io/trove/)** — 46 pages, every mechanism anchored to the source line · [Illustrated user / admin guides](https://nivane.github.io/trove/user/ui-tour.html) · [Datasource onboarding](https://nivane.github.io/trove/guide/datasource.html)

</div>

---

## What Trove Is

Trove is a **self-learning data decision agent**. Ask questions in your own words; get Markdown answers backed by real SQL — verified before and after execution, refused when the answer would be a guess, and improved by every question you ask. The same semantic model is also evaluated on schedule into evidence-carrying verdicts that know their own uncertainty, delivered the moment they trigger; the action happens in your own processes (Trove keeps read-only access to your data), and its effect is measured back afterwards.

Its promise is not "always right" but **never wrong without a fight**:

- **The semantic layer sets the boundary.** A human-approved semantic model (`semantics.yml`, an Apache OSSIE semantic model) is the *only answerable scope* — the datasets, metrics, fields and relationships the business has declared, nothing else. Queries outside it are refused, with a one-click model-extension path attached; never answered by guessing at raw tables.
- **Deterministic rails close the loop.** Generated SQL runs through a zero-LLM rule chain, an AST firewall, an execution-cost guard and a reflection cycle with SQL-version regression. Wrong answers are diagnosed, rolled back and corrected — and the fix is remembered.
- **Decisions share the query's footing — and know how certain they are.** Threshold rules name metrics, windows and baselines in the semantic model's own vocabulary and are evaluated by a deterministic engine, zero LLM. Every trigger carries the SQL and the raw rows it was judged on, plus statistical honesty: a seasonal noise band, no confirmation from fewer than 8 blocks, and a confidence figure labelled for what it is — a *position score, not a probability*. "Cannot tell" is reported as such, never quietly read as "nothing wrong today".
- **Analysis is statistics first, narrative second.** "Why did it drop?" recurses along the metric's definition; contributions, ratio attribution and residuals are shown as they are, and significance, confidence and seasonal baselines come from a pure-stdlib statistics kit — recomputable and seed-reproducible, so a conclusion can always be re-derived. Every number comes from the deterministic engine; only the narrative uses an LLM.
- **An action is not done until it is measured.** Approval and dispatch are not the end: on the agreed cadence Trove comes back and compares the blocks before and after the action against the *same noise band* the verdict used — "outside the band" and "no identifiable change" are both honest outcomes. Measurement reuses the decision's own parsing and compilation, so nobody quietly swaps the ruler.
- **Learning compounds into an asset.** Every correction distills into a lesson; every confirmed Q&A becomes reference SQL. Auto-learned content lands `pending` until an admin confirms — the knowledge grows, and stays reviewable, git-manageable, yours.

## The Shape

```mermaid
flowchart TB
    subgraph entry["Entry points"]
        UI["Web UI"]
        SRV["HTTP service"]
        CLI["CLI / REPL"]
        MCP["MCP"]
    end

    WF["Orchestration · trove/workflow<br/>LangGraph workflow<br/>semantic gate → plan and compile<br/>generate → execute and validate → reflect"]

    subgraph caps["Capabilities · trove/services"]
        SEM["Semantic model"]
        KB["Knowledge base + retrieval"]
        MEM["Memory · decision rules · Skills"]
        ACT["Actions · proposal / approval / receipt / measured effect"]
    end

    LLM["Models · trove/llm<br/>LLM gateway"]
    DS["Data sources<br/>PostgreSQL · MySQL<br/>ClickHouse · DuckDB<br/>SQLite"]
    STATE["State · trove/storage<br/>PostgreSQL / SQLite<br/>sessions · tasks · checkpoints<br/>query log · lineage"]

    UI --> WF
    SRV --> WF
    CLI --> WF
    MCP --> WF
    WF --> SEM
    WF --> KB
    WF --> MEM
    MEM -->|"on trigger"| ACT
    WF -.-> LLM
    KB --> DS
    MEM --> STATE
```

Two bands (entry and capabilities) with the orchestration between them — a single LangGraph graph, drawn as one node — plus the model and state layers hanging off the side: that is the shape. Data sources sit outside Trove, which is why they are not one of the five layers. **The same goes for executing an action** — the proposals, approvals and receipts a trigger produces are inside; the execution is outside (pushed via webhook / pulled via MCP). Solid lines are "who calls whom", dotted lines are "who uses an LLM"; neither is a data flow. For the detail behind each box: [system architecture](https://nivane.github.io/trove/architecture/overview.html) and [query workflow](https://nivane.github.io/trove/architecture/workflow.html).

### What happens to one question

```mermaid
flowchart TB
    Q(["Question"]) --> ROUTE["route_intent<br/>split by intent"]
    ROUTE --> SL["schema_linking<br/>anchor it to the model"]
    SL --> GATE{"Semantic gate"}
    GATE -->|"out of reach"| REFUSE["refuse<br/>+ draft a model extension"]
    GATE -->|"covered / partial"| FAST["fast_match shortcut<br/>an exact KB hit goes to SQL"]
    FAST -->|"miss"| GEN["Plan → compile → generate<br/>agentic loop, voting"]
    FAST -->|"hit"| EXEC["Three gates pre-execution<br/>auth → HITL → read-only"]
    GEN --> EXEC
    EXEC --> VAL["Execute → rules → masking<br/>rules must pass first"]
    VAL --> RE{"reflect"}
    RE -->|"not passing"| RB["analyze_error<br/>version diff → roll back"]
    RB --> GEN
    RE -->|"passing"| OUT(["Delivery<br/>conclusion · chart<br/>insights · attribution"])
```

The full 28-node version, every branch and the rollback ladder: [query workflow](https://nivane.github.io/trove/architecture/workflow.html).

The pipeline for real — ask a question, watch the analysis steps unfold live on the right, and get an answer with a chart plus KB-hit and verified badges:

<p align="center"><img src="assets/demo.gif" alt="Trove demo: ask a question, watch the pipeline steps unfold live (routing → SQL generation → validation → insights → chart), and get an answer with a chart plus KB-hit and verified badges" width="880"></p>

## Why Not Another NL2SQL

Raw-LLM agents write SQL from raw DDL, and are confidently wrong about business meaning: a column named `A11` or a status code `A` means nothing without the glossary, and "average loan amount" is not derivable from a schema. RAG helps — a glossary, examples and lessons anchor generation — but RAG feeds the *ammo*, it does not draw the *boundary*: retrieval misses still get answered with plausible guesses.

Trove takes the semantic-layer route, with a twist that keeps answers flowing:

- **Covered by the model → compile.** The plan is compiled against declared metrics, fields and relationships — the SQL is authoritative, not suggested.
- **Missing only words, values or definitions → compile half.** Joins, filters and grouping are compiled authoritatively and the generation agent fills the gaps, with a skeleton-fidelity check guarding execution — so the answer still ships instead of stopping dead.
- **Structurally out of reach → refuse.** An undeclared table, an ambiguous join, a fan-out — Trove refuses, and drafts a model extension for you to confirm in one step. The refusal *is* the modeling signal; coverage grows with use.

```mermaid
flowchart TB
    PLAN["Query plan"] --> C["Semantic compiler"]
    C -->|"all declared"| OK["Authoritative SQL<br/>compiler output as-is<br/>nothing to generate<br/>executed directly"]
    C -->|"soft MISS"| PC["words, values or<br/>definitions undeclared<br/>joins, filters, grouping<br/>pinned; gaps filled"]
    C -->|"hard MISS"| MISS["undeclared table,<br/>ambiguous join, fan-out<br/>refuse + one-click<br/>model extension"]
```

One compiler, three outcomes — and the **soft MISS in the middle is the common case**: it turns "the model doesn't cover everything" from a hard stop into an answer that ships anyway.

Everything a human cares about — which definitions count, which joins are legal, what the enums mean, how much is too much — is declared once in YAML and enforced on every question, instead of re-guessed on every question.

| | A raw LLM agent | RAG-only NL2SQL | A bare semantic layer | **Trove** |
|---|:---:|:---:|:---:|:---:|
| Draws an answerable boundary (semantic model) | ❌ | ❌ | ✅ | ✅ |
| Refuses instead of guessing outside coverage | ❌ | ❌ | partial | ✅ + one-click model extension |
| Still answers on partial coverage | ❌ | ✅ (guessy) | ❌ | ✅ compiled skeleton + generated gaps |
| Verifies before execution — zero LLM | ❌ | ❌ | partial | ✅ rule chain + AST firewall + EXPLAIN guard |
| Self-corrects after execution (reflection + regression) | ❌ | ❌ | ❌ | ✅ rollback & retry with version checks |
| Conclusion → threshold judgement (zero LLM) | ❌ | ❌ | partial (alerts can't reach the semantic layer's baselines) | ✅ declared in model vocabulary + evidence kept |
| Judgements carry their own uncertainty (seasonal noise band / no confirmation on thin samples) | ❌ | ❌ | ❌ | ✅ pure-stdlib statistics, confidence honestly labelled |
| Measures the effect after acting (closes the loop) | ❌ | ❌ | ❌ | ✅ the same noise band the verdict used |
| Learns per datasource; auto content gated by humans | ❌ | partial | partial | ✅ KB + memory, `pending` until confirmed |

## Engineering Trade-offs

Each row above rests on specific algorithms and boundaries that get their own deep dive — the same skeleton every time: the problem → principles and algorithms → how to use it → what it gives up → where it wins:

| Topic | The trade-off in one line | Deep dive |
|---|---|---|
| Semantic compilation boundary | Compile what compiles, half-compile the missing vocabulary, refuse what's structurally wrong — and grow the model | [Semantic compilation boundary](https://nivane.github.io/trove/engineering/semantic-compiler.html) |
| Decomposition math & driver tree | Claim an identity only when it holds; multiplication/division chains stay un-split, residuals kept in evidence | [Decomposition math and the driver tree](https://nivane.github.io/trove/engineering/decomposition.html) |
| Statistics & noise band | "How much change is a change" answered with a robust z and an honest tri-state, never a p-value performance | [Statistics and the noise band](https://nivane.github.io/trove/engineering/statistics.html) |
| Decision engine | Thresholds declared in the semantic model's own vocabulary, judged with zero LLM, replayable from evidence | [The decision engine](https://nivane.github.io/trove/engineering/decision-engine.html) |
| Causality & what-if | Decomposition is description; causal claims climb an experimental-design ladder — or say they can't be made | [Causality and what-if](https://nivane.github.io/trove/engineering/causal-whatif.html) |
| Closed loop & contracts | Verdict → action → measurement runs as a full loop; measurement discipline lives in the verifier contract | [The closed loop and its contracts](https://nivane.github.io/trove/engineering/closed-loop.html) |
| Action safety | Read-only posture pinned by signatures and tests; closed-set templating; failures are never silent | [Action safety architecture](https://nivane.github.io/trove/engineering/action-safety.html) |
| Extension & governance | One registration path for every extension; org assets go pending → confirm → versioned → rollback | [Extension and governance](https://nivane.github.io/trove/engineering/extension-governance.html) |

## Quick Start

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/). Pick a path first:

| What you want | Which path | Roughly | Start at |
|---|---|---|---|
| A look at what it does | The built-in BIRD financial demo | 5 minutes | the commands below |
| A full deployment you can log into and administer | Docker Compose (frontend + backend + PostgreSQL) | 15 minutes | [Docker](#docker) |
| Your own database | Register a URL → `/kb init` to model it → ask | half an hour up | [First steps with your own data](#first-steps-with-your-own-data) |

```bash
uv sync                                          # install dependencies
uv run trove --datasource demo                   # interactive REPL against the built-in BIRD financial demo

# one-shot CLI: question via stdin, JSON out
echo "Which region has the highest average loan amount?" | uv run trove-cli --datasource demo --print
```

The demo datasource is built in, but conversations still need LLM credentials (project-root `.env` or an env var such as `DEEPSEEK_API_KEY`). No credentials, no answer — there is no silent "mock answer" fallback.

### First steps with your own data

1. **Connect** — a database URL: `--datasource postgres://user:pass@host:5432/db`.
2. **Model** — run `/kb init`: it drafts the initial semantic model (datasets, fields, metrics) from your live schema. No model, no answers — Trove refuses politely until you do, which is the boundary working.
3. **Ask** — then ask naturally. When Trove refuses, confirm the drafted model extension to extend coverage and re-answer in one step.

Under server deployment the same flow runs in the admin console (datasource registration → KB init → draft review). Step-by-step guide for MySQL / Doris / PostgreSQL / ClickHouse / DuckDB, via console or local REPL: [datasource onboarding](https://nivane.github.io/trove/guide/datasource.html).

### Docker

Frontend (nginx-served SPA + `/v1` reverse proxy) and backend (pure JSON API) are independent images, built and rebuilt independently:

```bash
docker compose up --build        # → http://localhost:8080/ (backend :8000 is debug-only)
docker compose build frontend    # rebuild only the frontend image; build backend for the backend
docker compose down
```

Default local login is admin / `admin123` (production is controlled by `TROVE_ADMIN_PASSWORD`). The compose stack defaults to PostgreSQL with the BIRD demo pre-loaded; real conversations need LLM credentials — see the comment on the `~/.trove/conf` mount in `docker-compose.yml`.

## Capabilities at a Glance

Every row has a deeper version on the docs site, anchored to source:

| Capability | In one line | Read more |
|---|---|---|
| Semantic layer | The answerable boundary is a readable, diffable, git-revertible file | [Semantic layer](https://nivane.github.io/trove/capabilities/semantic.html) |
| Topic areas | An optional narrowing inside the semantic model: queries anchor and converge within the area, out-of-area questions are refused outright; grants narrow users to areas in the console | [Semantic layer · topics](https://nivane.github.io/trove/capabilities/semantic.html#topics) |
| Self-checking loop | Rule chain / AST firewall / cost guard / reflection with version regression | [Query workflow](https://nivane.github.io/trove/architecture/workflow.html) |
| Decision rules | Alerts run on the semantic model too: thresholds, windows and baselines evaluated with zero LLM, every trigger carrying evidence and an adjacent diff; a seasonal noise band decides significance, a conditional ladder estimates net effect, and a rule can be replayed what-if on the numbers it actually judged | [Decision rules](https://nivane.github.io/trove/capabilities/decisions.html) |
| Proactive scan | Scheduled scans hunt anomalies across metrics × dimensions; anything beyond the noise band lands as a *draft* that only becomes a rule once an admin confirms it — the LLM proposes hypotheses, the engine verifies them | [Decision rules · scan](https://nivane.github.io/trove/capabilities/decisions.html#scan) |
| Actions | A trigger freezes into a proposal → human approval → dispatch and receipt → measured effect; backoff retry and dry-run rehearsal are built in, and Trove itself holds no write channel | [Actions & approval](https://nivane.github.io/trove/capabilities/actions.html) |
| Decision quality | Verdict history is scored back per rule revision: trigger rate, and how many actions proved effective / showed no change / could not be measured — no rate on thin samples | [Decision rules · quality](https://nivane.github.io/trove/capabilities/decisions.html#quality) |
| Subscriptions | Scheduled analysis is delivered to a notification channel as "every run / alerts only", failures on record; a job can be scoped to a topic area | [Automation & governance](https://nivane.github.io/trove/admin/automation.html) |
| Knowledge base | `/kb init` drafts it, confirmed Q&A becomes reference SQL, drift raises an alarm | [Knowledge base](https://nivane.github.io/trove/capabilities/kb.html) |
| Hybrid retrieval | Keyword + vector recall, weights tunable and measurable, with zero-LLM eval scripts | [Hybrid retrieval](https://nivane.github.io/trove/capabilities/retrieval.html) |
| Memory | Cross-session episodes, auto-extracted preferences, per user × datasource profiles | [Memory](https://nivane.github.io/trove/capabilities/memory.html) |
| Skills | Org-wide methodology: `required` injected / `available` on demand / `validator` assertions | [Skills](https://nivane.github.io/trove/capabilities/skills.html) |
| Analysis | "Why did it drop?" recurses along the metric's definition into a driver tree, with contribution breakdowns and residuals shown as they are; significance, confidence and seasonal baselines come from a recomputable statistics kit; the numbers come from a deterministic engine, only the narrative uses an LLM | [Agent capabilities](https://nivane.github.io/trove/capabilities/agent.html) · [noise band](https://nivane.github.io/trove/capabilities/decisions.html#significance) |
| Six datasources | SQLite / PostgreSQL / MySQL / Doris / ClickHouse / DuckDB, one pattern | [Data capabilities](https://nivane.github.io/trove/capabilities/data.html) |
| Interfaces and governance | Web UI, REST (`/v1`), MCP, CLI; admin review, audit, observability | [API](https://nivane.github.io/trove/reference/api.html) · [MCP](https://nivane.github.io/trove/reference/mcp.html) · [CLI](https://nivane.github.io/trove/reference/cli.html) |

Drivers install on demand: `uv sync --extra postgres|mysql|doris|clickhouse|duckdb` (SQLite is built in); cloud warehouses use `--extra snowflake|bigquery`. The LLM side goes through a litellm gateway — OpenAI / DeepSeek / Anthropic / any compatible endpoint — with per-node tiers, so a strong model can adjudicate reflection while a cheap one plans and writes insights. See the [configuration reference](https://nivane.github.io/trove/reference/config.html).

The admin console is the other half of the same stack: datasource onboarding, KB review, the semantic model and topic areas, decision rules, action proposals and approvals, subscriptions and audit; which datasources and topic areas a user sees is granted per person. This is the KB review queue — automatically captured lessons and examples land as pending, and only enter retrieval after an admin confirms them:

<img src="docs/assets/shots/admin-kb-pending.png" alt="Trove admin console · knowledge base review queue: every auto-captured lesson and example carries Confirm / Edit-and-confirm / Reject, and enters retrieval only after confirmation" width="880">

## Read-Only Execution: the Boundary Is Code

For a data agent to ship, the security boundary cannot be a request written in a prompt. Everything below is **code, not model behaviour** (the full list is in [security boundaries](https://nivane.github.io/trove/ops/security.html)):

| Boundary | What it is in code |
| --- | --- |
| **Read-only statement firewall** | SQL is parsed to an AST and judged there: non-query statements, any DML/DDL, `SELECT INTO OUTFILE`, dangerous functions and metadata tables are refused — no keyword blacklist |
| **Table allowlist** | Enforced **in the execution path itself** (`execute` and `explain` alike), so SQL arriving via MCP, the semantic query API or any bypass is bound by the same gate |
| **Execution-cost guard** | EXPLAIN estimates the heaviest operator's row count first: over the soft limit (default 50M) the query goes back to generation to add `LIMIT`; over the hard limit it is refused outright, without burning an LLM regeneration round |
| **Field-level redaction** | Declared per field as `partial` / `hash` / `null`, rewritten as the result leaves the database and before any model-facing node. The failure direction is strict: if a field should be redacted but the salt or the semantic model cannot be read, the query is refused rather than let through unredacted |
| **External-data isolation** | Content from database cells (and retrieval/probe results) is scanned for injection patterns and replaced with an isolation marker before it reaches the prompt; human-confirmed KB content is exempt by design |
| **Rate limits, quotas and audit** | Per-user token bucket plus a daily quota, over the limit returns 429; every execution records question / SQL / verdict / row count / duration / error |
| **Actions propose, never execute** | A decision trigger only freezes the action into a proposal (payload final the moment it is created, deduplicated by idempotency key); it is dispatched only after approval. The action layer **takes no datasource connectors at all** — Trove itself has no channel to write back to your database, and approval, dispatch and receipt are each recorded separately |

**The application layer is not a security boundary** — always connect Trove to a dedicated read-only role:

```sql
-- PostgreSQL (covers future objects): CREATE ROLE trove_ro LOGIN PASSWORD '...'; GRANT pg_read_all_data TO trove_ro;
-- MySQL (per-database grants, fixed source IP): CREATE USER 'trove_ro'@'10.0.0.5' IDENTIFIED BY '...'; GRANT SELECT ON app.* TO 'trove_ro'@'10.0.0.5';
```

Also recommended: hide sensitive columns with column-level grants or views; set `statement_timeout` / `lock_timeout` (PG) or `MAX_EXECUTION_TIME` (MySQL); enforce row limits in the database. A semantic-model `row_filter` is a **declarative filter** — it keeps rows that should not be answered out of the SQL, and does not replace the database-side boundaries above.

## When It Fails, It Fails Closed

Every item above invites a follow-up: "and when it breaks?" The answer is uniform in the code — **an undecidable judgement falls to the safe side**, never to "let it through". These directions are chosen deliberately, not defaults:

| Situation | On failure | Result |
|---|---|---|
| Auth components missing | Refuse to start | Better not to boot than to run silently insecure |
| HITL payload unrecognisable | Deny by default | Not executed; handed to a human |
| Salt or semantic model unreadable for masking | Refuse the query | Never delivered unredacted |
| No authorization basis (`None`) | Deny everything | "Forgot to grant" never degrades into access |
| Read-only self-check cannot complete | Recorded "unverified" | Never rendered as "safe" |
| KB drift check cannot read | Non-zero exit code | "Could not check" is not "nothing wrong" |
| Confidence undecidable | The most conservative band | Understating beats overstating |
| Noise band unmeasurable (too few blocks / no history) | Report "cannot tell" as-is | A rule that gates on the band raises an error run rather than quietly passing |

The same bias shows up elsewhere: health distinguishes `unavailable` from `degraded` (rather than one "down"), the rule chain stops at the first failure (rather than emitting a pile of vague hints), and a masking failure clears the result set (rather than leaving a stale one that could leak). The full list: [security boundaries](https://nivane.github.io/trove/ops/security.html) and [observability](https://nivane.github.io/trove/ops/observability.html).

## Docs Map

📖 **[nivane.github.io/trove](https://nivane.github.io/trove/)** — 46 pages, from product concepts to API reference, including illustrated guides with 34 real-UI screenshots

| | |
|---|---|
| **Getting started** | [Quickstart](https://nivane.github.io/trove/guide/quickstart.html) · [Concepts](https://nivane.github.io/trove/guide/concepts.html) · [Deploy](https://nivane.github.io/trove/guide/deploy.html) · [Datasources](https://nivane.github.io/trove/guide/datasource.html) |
| **Illustrated guides** | [User guide](https://nivane.github.io/trove/user/ui-tour.html) · [Admin console](https://nivane.github.io/trove/admin/console.html) · [Deployment & ops](https://nivane.github.io/trove/ops/deployment.html) |
| **Architecture** | [System overview](https://nivane.github.io/trove/architecture/overview.html) · [Query workflow](https://nivane.github.io/trove/architecture/workflow.html) |
| **Capabilities** | [Data](https://nivane.github.io/trove/capabilities/data.html) · [Semantic layer](https://nivane.github.io/trove/capabilities/semantic.html) · [KB](https://nivane.github.io/trove/capabilities/kb.html) · [Retrieval](https://nivane.github.io/trove/capabilities/retrieval.html) · [Decision rules](https://nivane.github.io/trove/capabilities/decisions.html) · [Actions & approval](https://nivane.github.io/trove/capabilities/actions.html) · [Agent](https://nivane.github.io/trove/capabilities/agent.html) · [Memory](https://nivane.github.io/trove/capabilities/memory.html) · [Skills](https://nivane.github.io/trove/capabilities/skills.html) · [LLM gateway](https://nivane.github.io/trove/capabilities/llm-gateway.html) |
| **Operations** | [Security](https://nivane.github.io/trove/ops/security.html) · [Admin](https://nivane.github.io/trove/ops/admin.html) · [Observability](https://nivane.github.io/trove/ops/observability.html) · [Drift](https://nivane.github.io/trove/ops/drift.html) · [Eval and regression gate](https://nivane.github.io/trove/ops/eval.html) |
| **Reference** | [Config](https://nivane.github.io/trove/reference/config.html) · [CLI](https://nivane.github.io/trove/reference/cli.html) · [API](https://nivane.github.io/trove/reference/api.html) · [MCP](https://nivane.github.io/trove/reference/mcp.html) |

What the 28-node main graph is made of, the semantic gate's three exits, the rule chain and rollback ladder, the context budget — all in [query workflow](https://nivane.github.io/trove/architecture/workflow.html). The REST docs for a running `serve` are at `/v1/docs`.

## Evaluation

Reproduce accuracy on your own data, your own semantic model, your own calibers — no off-the-shelf numbers are quoted here. The BIRD financial demo ships with a schema identical to the official export, so the full reflection pipeline can be run directly:

```bash
uv run python scripts/eval_bird.py --db-id financial \
  --dev-json /path/to/mini_dev_mysql.json \
  --datasource mysql://root:root@127.0.0.1:3306/financial [--limit 10]
```

Per-question verdicts (each with its `qid` and token cost) land in `.trove/eval/results.jsonl`. `offline_eval.py record` captures a trace with real credentials, after which `replay` scores it with **zero LLM calls**; `eval_gate.py` is the CI regression gate — any metric getting worse exits 1. See [eval and regression gate](https://nivane.github.io/trove/ops/eval.html).

## Development

```bash
uv run pytest                     # full suite: 7400+ tests, mocked LLM, zero network / zero keys
uv run pytest tests/workflow/     # LangGraph graphs and nodes only
uv run pytest -m "not slow"       # skip slow tests
```

Code layout: `trove/workflow/` (graphs and nodes) · `trove/services/` (datasources, KB, semantic layer, decision rules, memory, skills) · `trove/llm/` (litellm gateway, agent loop) · `trove/storage/` (unified storage: sessions, audit, checkpoints) · `trove/agent/` (session orchestration) · `trove/cli/`. Deeper architecture notes live in `CLAUDE.md`.

## FAQ

**Is this a text-to-SQL framework or a BI tool?** Neither exactly — Trove is a data decision agent. You ask, it plans, compiles, executes and verifies, then answers in Markdown with charts and an auditable trail; the same semantic model is also evaluated on schedule into evidence-carrying verdicts. It does not try to be a dashboard builder.

**Why does it refuse questions?** Because the semantic model is the answerable boundary — that is the product. A refusal means "the model does not cover this", and it arrives with a drafted extension you can confirm to extend coverage and re-answer in one step. Quietly guessing at tables is the failure mode this architecture exists to prevent.

**What role does RAG play?** RAG feeds generation — glossary terms, reference SQL, lessons, episodic memory — retrieved deterministically against the matched datasets. It informs *how* Trove writes SQL within coverage; it never decides *whether* the question is answerable.

**Do I need a vector database?** No. Retrieval runs on a built-in SQLite FTS5 mirror out of the box; under PostgreSQL the same instance hosts pg_bm25 + pgvector for hybrid retrieval, and episodic memory may use an embedder when one is configured, falling back gracefully to lexical matching.

**How does the semantic model stay honest as the schema drifts?** Runtime guards keep behaviour safe; a drift check compares declarations against the live schema, reports `stale` in the admin console where rebuilding via `/kb init` preserves human-reviewed definitions. The important part is that it **never reads "could not check" as "no problem"**: an unreachable datasource or an unreadable KB is recorded as unverified and returns a non-zero exit code. See [drift governance](https://nivane.github.io/trove/ops/drift.html).

## Contributing

Bug reports, feature ideas, docs and pull requests are welcome. Please keep changes focused and make sure the tests pass before submitting; for larger changes, open an issue to discuss first.

## License

Trove is released under the [Apache License 2.0](LICENSE).
