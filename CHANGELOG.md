# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Cleared the advisories flagged by the CI dependency scan: `filelock`
  (three lock-file GHSAs, raised to ≥4.2.1 through a uv constraint),
  `fastmcp`/`fastmcp-slim` 3.4.10 (kept on the 3.x line), and
  `langgraph-checkpoint-sqlite` 3.1.2 / `langgraph-checkpoint-postgres` 3.1.3.
  The `filelock` floor forces `snowflake-connector-python` 5.x (4.x pins
  `filelock<4`); the Snowflake adapter now runs queries through the 5.x public
  async lifecycle (`execute_async` → `get_results_from_sfqid` → `fetchall`)
  and aborts with `cursor.abort_query(query_id)`, since `conn._cancel_query`
  no longer exists.

### Fixed

- `trove-cli --print` (and the REPL / `trove serve` shutdown) now release every
  store connection before returning. aiosqlite worker threads are non-daemon,
  so one undisposed store kept the process alive after its output was printed.
- F2-a validation no longer rejects correct per-gender grouping SQL: for
  questions like "how many clients are there for each gender", `GROUP BY
  gender` is the requested dimension rather than a missing `WHERE gender = …`
  filter. Grouping by any other dimension still requires the gender filter.
- Attribution no longer skips silently when the query plan's `target_metric`
  uses a shorthand for a declared metric (e.g. "loan amount" when the model
  declares "average Loan amount"). Unique normalized / expression matches
  resolve to the declared name; ambiguous or unknown names now render an
  explicit, reason-specific "attribution could not be completed" line instead
  of returning an empty result. The query-sketch attribution prompt now
  requires copying declared metric and field names verbatim.

## [0.1.0] - 2026-10-23

First release. Trove is an NL→SQL conversational data agent whose answers are
bounded by a human-confirmed semantic model, with analysis, deterministic
decision rules, and action follow-through on top.

### Added

- **Semantic-first query pipeline.** Intent routing, semantic matching, SQL
  generation with self-validation, execution, and reflection adjudication. The
  semantic model (`semantics.yml`, Apache OSSIE shape) is the generation's
  primary view and the answerability boundary; structural misses refuse with a
  one-click path to extend the model, soft misses compile what they can and hand
  the rest to generation.
- **Per-datasource knowledge base.** Schema notes, semantic model, examples,
  rules, and a two-tier (pending/confirmed) lesson bank in `.trove/kb/<ds>/`,
  YAML as the single source of truth, hybrid keyword + dense retrieval.
- **Existing semantic-layer import.** `trove import semantic` maps dbt
  `semantic_models`/metrics YAML (optionally resolving `ref()`/`source()` via
  `manifest.json`) and Cube's compiled meta JSON. Three landing modes:
  `preview` (zero writes, default), `bootstrap` (no model yet), and `stage`
  (opens a semantic change for admin review; `semantics.yml` untouched).
  Unmappable constructs are reported item by item.
- **Analysis engine.** Deterministic decomposition ("why did it change"), robust
  noise-band statistics, effect measurement, all replayable from recorded
  evidence queries.
- **Decision rules.** Threshold rules declared in the semantic model's own
  vocabulary, evaluated deterministically (zero LLM) with evidence, a
  significance gate over historical noise bands, and a causal ladder that
  degrades honestly instead of overclaiming.
- **Actions and closed-loop verification.** Proposals with frozen payloads,
  human approval, templated delivery over IM channels, and post-hoc effect
  verification.
- **Memory.** Episodic recall across sessions, auto-learned preferences, and
  promotion of recurring corrections — automatic content always lands as
  `pending` and never bypasses admin confirmation.
- **Governance.** Admin console, row/column grants, OIDC SSO, org skills,
  scheduled jobs, and audit surfaces including a refusal report.
- **Datasources.** SQLite, MySQL, PostgreSQL, ClickHouse, DuckDB, Doris,
  Snowflake, and BigQuery adapters; Docker Compose deployment with PostgreSQL +
  pgvector as the default stack.
- **Evaluation tooling.** `scripts/eval_gate.py` (offline regression gate, zero
  LLM/network), per-datasource evalsets with a trust tiering model, and the BIRD
  financial demo datasource.

[Unreleased]: https://github.com/Nivane/trove/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Nivane/trove/releases/tag/v0.1.0
