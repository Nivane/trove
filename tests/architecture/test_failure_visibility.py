"""Failure-visibility ratchet: broad exception handlers must not be silent.

The repo has hundreds of ``except Exception`` blocks by design — a data agent
degrades instead of crashing. The problem is that the machine cannot tell a
deliberate best-effort swallow from a failure that merely got lost: both look
like an empty handler.

This test freezes **today's silent handlers** and makes any new one a red
test. A broad handler counts as *visible* when its own body

* raises (``raise`` / ``raise ... from ...``), or
* calls a log/output function (``logger.*``, ``logging.*``, ``print``, ...), or

when the ``except`` line carries ``# noqa: BLE001`` — the repo's existing
explicit marker for "yes, I am deliberately swallowing this". The marker is
inert for ruff today (only ``E,F`` are selected), so the ratchet is what gives
it meaning.

The baseline is keyed by ``(repo-relative path, qualname, ordinal)`` — **not**
by line number: lines move on every edit, and a ratchet that churns on
unrelated edits gets regenerated without being read (the P3 lesson).

Fixing a handler = delete its row. Adding one = red. Keep the baseline
generated, never hand-transcribed: ``_silent_handlers()`` is the detector.
"""
from __future__ import annotations

import ast
from collections import defaultdict

import pytest

from tests.helpers.arch import ROOT, py_sources

#: Calls that make a handler's failure observable. ``debug`` is included on
#: purpose: the run log is a debug-level surface, and a debug line at least
#: proves someone looked. The ratchet's job is "no unmarked silence", not
#: "loud enough for production".
VISIBLE_CALLS = frozenset({
    "critical", "debug", "error", "exception", "info", "log",
    "print", "print_error", "warn", "warning",
})

BROAD_NAMES = frozenset({"Exception", "BaseException"})


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    """Names caught by one handler; bare ``except:`` catches everything."""
    if handler.type is None:  # bare except: BaseException
        return set(BROAD_NAMES)
    nodes = (
        handler.type.elts
        if isinstance(handler.type, ast.Tuple)
        else [handler.type]
    )
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _is_broad(handler: ast.ExceptHandler) -> bool:
    return bool(_caught_names(handler) & BROAD_NAMES)


def _visible_body(body: list[ast.stmt]) -> bool:
    """True when the handler's own body raises or logs.

    Deliberately does **not** descend into nested functions/classes (their
    exceptions are their own) or into nested ``except`` bodies (a log in the
    inner handler says nothing about the outer one).
    """
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in VISIBLE_CALLS:
                return True
            if isinstance(func, ast.Attribute) and func.attr in VISIBLE_CALLS:
                return True
        if isinstance(node, (ast.ExceptHandler, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return False


def _has_noqa_marker(handler: ast.ExceptHandler, lines: list[str]) -> bool:
    """``# noqa: BLE001`` on the ``except`` header (comments live in source)."""
    stop = handler.body[0].lineno if handler.body else handler.lineno
    header = "\n".join(lines[handler.lineno - 1:max(stop, handler.lineno)])
    return "noqa" in header and "BLE001" in header


class _Collector(ast.NodeVisitor):
    def __init__(self, rel: str, lines: list[str]) -> None:
        self.rel = rel
        self.lines = lines
        self.scope: list[str] = []
        self.ordinals: dict[str, int] = defaultdict(int)
        self.silent: set[tuple[str, str, int]] = set()

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if _is_broad(node):
            qualname = ".".join(self.scope) or "<module>"
            ordinal = self.ordinals[qualname]
            self.ordinals[qualname] += 1
            if not (_visible_body(node.body) or _has_noqa_marker(node, self.lines)):
                self.silent.add((self.rel, qualname, ordinal))
        self.generic_visit(node)


def _silent_handlers_in_source(rel: str, source: str) -> set[tuple[str, str, int]]:
    collector = _Collector(rel, source.splitlines())
    collector.visit(ast.parse(source))
    return collector.silent


def _silent_handlers() -> set[tuple[str, str, int]]:
    found: set[tuple[str, str, int]] = set()
    for rel, source in py_sources("trove").items():
        found |= _silent_handlers_in_source(rel, source)
    return found


#: Frozen debt: broad handlers that neither log, raise, nor carry an explicit
#: deliberate-silence marker. Generated by ``_silent_handlers()`` and reviewed
#: by hand; this list may only shrink.
SILENT_HANDLERS: frozenset[tuple[str, str, int]] = frozenset({
    # trove/agent/session.py
    ('trove/agent/session.py', 'SessionManager._cache_key', 0),
    ('trove/agent/session.py', 'SessionManager._run_stats', 0),
    ('trove/agent/session.py', 'SessionManager._state_summary', 0),
    ('trove/agent/session.py', 'SessionManager._stream_graph_run', 1),
    ('trove/agent/session.py', 'SessionManager._stream_graph_run._produce', 0),
    ('trove/agent/session.py', 'SessionManager._trace_run_finish', 0),
    ('trove/agent/session.py', 'SessionManager._trace_run_finish', 1),
    ('trove/agent/session.py', 'SessionManager._trace_run_start', 0),
    ('trove/agent/session.py', 'SessionManager._trace_run_start', 1),
    ('trove/agent/session.py', 'SessionManager._trace_step', 0),
    ('trove/agent/session.py', 'SessionManager.dispose', 0),
    # trove/api/app.py
    ('trove/api/app.py', 'create_app.health', 0),
    ('trove/api/app.py', 'create_app.health._ping_one', 0),
    # trove/api/routers/admin.py
    ('trove/api/routers/admin.py', '_kb_init_status_sync', 0),
    ('trove/api/routers/admin.py', 'get_run_record', 0),
    # trove/api/routers/decisions.py
    ('trove/api/routers/decisions.py', '_audit', 0),
    ('trove/api/routers/decisions.py', 'list_decisions', 0),
    # trove/api/routers/governance.py
    ('trove/api/routers/governance.py', '_leg_ds', 0),
    # trove/api/routers/jobs.py
    ('trove/api/routers/jobs.py', '_audit', 0),
    # trove/api/routers/kb.py
    ('trove/api/routers/kb.py', 'rate_lesson', 0),
    # trove/api/routers/overview.py
    ('trove/api/routers/overview.py', '_datasource_pings._ping_one', 0),
    ('trove/api/routers/overview.py', '_dialect_of', 0),
    ('trove/api/routers/overview.py', '_domain_of', 0),
    ('trove/api/routers/overview.py', '_leg', 0),
    ('trove/api/routers/overview.py', '_storage_ping', 0),
    ('trove/api/routers/overview.py', '_storage_ping', 1),
    # trove/api/routers/presets.py
    ('trove/api/routers/presets.py', '_audit', 0),
    # trove/api/routers/quality.py
    ('trove/api/routers/quality.py', '_feedback_names', 0),
    ('trove/api/routers/quality.py', '_feedback_names', 1),
    # trove/api/routers/semantic.py
    ('trove/api/routers/semantic.py', '_dialect', 0),
    ('trove/api/routers/semantic.py', '_drift_view', 0),
    ('trove/api/routers/semantic.py', '_semantic_drift', 0),
    ('trove/api/routers/semantic.py', 'batch_semantic_drafts', 0),
    # trove/api/routers/semantic_query.py
    ('trove/api/routers/semantic_query.py', '_audit_deny', 0),
    ('trove/api/routers/semantic_query.py', 'semantic_query', 0),
    ('trove/api/routers/semantic_query.py', 'semantic_query', 1),
    ('trove/api/routers/semantic_query.py', 'semantic_topics', 0),
    # trove/api/routers/subscriptions.py
    ('trove/api/routers/subscriptions.py', '_audit', 0),
    ('trove/api/routers/subscriptions.py', '_job_names', 0),
    # trove/api/routers/usage.py
    ('trove/api/routers/usage.py', '_cost_block._fetch', 0),
    ('trove/api/routers/usage.py', '_counter_series', 0),
    ('trove/api/routers/usage.py', '_counter_total', 0),
    ('trove/api/routers/usage.py', '_latency_block._fetch', 0),
    # trove/cli/commands/kb_cmds.py
    ('trove/cli/commands/kb_cmds.py', 'register_kb_commands._cmd_learn', 1),
    # trove/cli/commands/metadata_cmds.py
    ('trove/cli/commands/metadata_cmds.py', 'register_metadata_commands.cmd_table_schema', 0),
    ('trove/cli/commands/metadata_cmds.py', 'register_metadata_commands.cmd_tables', 0),
    # trove/cli/commands/session_cmds.py
    ('trove/cli/commands/session_cmds.py', 'register_session_commands.cmd_compact', 0),
    # trove/eval/replay.py
    ('trove/eval/replay.py', 'normalize_sql', 0),
    # trove/llm/agent_loop.py
    ('trove/llm/agent_loop.py', '_trace_observer', 0),
    ('trove/llm/agent_loop.py', '_trace_observer', 1),
    ('trove/llm/agent_loop.py', 'run_agent_loop', 0),
    ('trove/llm/agent_loop.py', 'run_agent_loop', 1),
    ('trove/llm/agent_loop.py', 'run_agent_loop', 2),
    ('trove/llm/agent_loop.py', 'run_agent_loop', 3),
    ('trove/llm/agent_loop.py', 'run_agent_loop._run_one', 0),
    # trove/llm/gateway.py
    ('trove/llm/gateway.py', 'LLMGateway._egress_config', 0),
    ('trove/llm/gateway.py', '_extract_retry_after', 0),
    ('trove/llm/gateway.py', '_record_failed_generation', 0),
    ('trove/llm/gateway.py', '_record_failed_generation', 1),
    ('trove/llm/gateway.py', '_record_local_call', 0),
    ('trove/llm/gateway.py', '_record_local_call', 1),
    ('trove/llm/gateway.py', '_record_local_call', 2),
    ('trove/llm/gateway.py', '_record_local_call', 3),
    ('trove/llm/gateway.py', '_usage_dict', 0),
    # trove/main.py
    ('trove/main.py', 'create_app_components._audit_egress_denied._write', 0),
    # trove/mcp/http_auth.py
    ('trove/mcp/http_auth.py', '_parse_bearer', 0),
    # trove/mcp/server.py
    ('trove/mcp/server.py', 'build_mcp_server._datasources_visible', 0),
    ('trove/mcp/server.py', 'build_mcp_server._kb_status', 0),
    ('trove/mcp/server.py', 'build_mcp_server._kb_status', 1),
    ('trove/mcp/server.py', 'build_mcp_server._read_kb_file', 0),
    ('trove/mcp/server.py', 'build_mcp_server.ack_proposal', 0),
    ('trove/mcp/server.py', 'build_mcp_server.ask_data', 0),
    # trove/services/action/dispatcher.py
    ('trove/services/action/dispatcher.py', '_retry_after_of', 0),
    # trove/services/action/store.py
    ('trove/services/action/store.py', 'ActionStore.dispose', 0),
    # trove/services/analysis/engine.py
    ('trove/services/analysis/engine.py', 'AnalysisEngine._build_tree', 0),
    ('trove/services/analysis/engine.py', 'AnalysisEngine._build_tree', 1),
    ('trove/services/analysis/engine.py', 'AnalysisEngine._build_tree', 2),
    ('trove/services/analysis/engine.py', 'AnalysisEngine._build_tree', 3),
    ('trove/services/analysis/engine.py', 'AnalysisEngine._build_tree._compile_one', 0),
    ('trove/services/analysis/engine.py', 'AnalysisEngine.run_components', 0),
    ('trove/services/analysis/engine.py', 'AnalysisEngine.run_components', 1),
    ('trove/services/analysis/engine.py', '_annotate_displays', 0),
    ('trove/services/analysis/engine.py', '_hierarchy_ranks', 0),
    ('trove/services/analysis/engine.py', '_hierarchy_ranks', 1),
    ('trove/services/analysis/engine.py', '_ref_dim_label', 0),
    ('trove/services/analysis/engine.py', '_ref_field_display', 0),
    ('trove/services/analysis/engine.py', '_ref_next_level', 0),
    ('trove/services/analysis/engine.py', '_related_dim_refs', 0),
    ('trove/services/analysis/engine.py', 'compile_ratio_hop', 0),
    ('trove/services/analysis/engine.py', 'resolve_dim_ref', 0),
    ('trove/services/analysis/engine.py', 'resolve_metric', 0),
    ('trove/services/analysis/engine.py', 'resolve_metric_name', 0),
    ('trove/services/analysis/engine.py', 'resolve_time_field', 0),
    # trove/services/analysis/expr_tree.py
    ('trove/services/analysis/expr_tree.py', 'metric_components.walk', 0),
    ('trove/services/analysis/expr_tree.py', 'metric_ratio_parts', 0),
    # trove/services/analysis/series.py
    ('trove/services/analysis/series.py', 'compile_series_hop', 0),
    # trove/services/authz/egress.py
    ('trove/services/authz/egress.py', 'EgressRedactor._agent_config', 0),
    # trove/services/authz/enforcer.py
    ('trove/services/authz/enforcer.py', 'referenced_tables', 0),
    # trove/services/checkpoint_view.py
    ('trove/services/checkpoint_view.py', 'get_checkpoint', 0),
    ('trove/services/checkpoint_view.py', 'list_thread', 0),
    # trove/services/datasource/adapters/mysql.py
    ('trove/services/datasource/adapters/mysql.py', 'MySQLAdapter._kill_query', 0),
    # trove/services/datasource/readonly.py
    ('trove/services/datasource/readonly.py', '_adapter', 0),
    ('trove/services/datasource/readonly.py', 'probe', 0),
    # trove/services/datasource/secrets.py
    ('trove/services/datasource/secrets.py', '_coerce_env_key', 0),
    # trove/services/decision/bridge.py
    ('trove/services/decision/bridge.py', '_dimension_components', 0),
    # trove/services/decision/service.py
    ('trove/services/decision/service.py', 'DecisionService._causal_stage', 0),
    ('trove/services/decision/service.py', 'DecisionService._causal_stage', 1),
    ('trove/services/decision/service.py', 'DecisionService._dialect', 1),
    ('trove/services/decision/service.py', 'DecisionService._provider_for', 0),
    ('trove/services/decision/service.py', 'DecisionService._significance_stage', 0),
    ('trove/services/decision/service.py', 'label_display', 0),
    ('trove/services/decision/service.py', 'label_display', 1),
    ('trove/services/decision/service.py', 'label_display', 2),
    ('trove/services/decision/service.py', 'resolve_dim_pairs', 0),
    ('trove/services/decision/service.py', 'resolve_dim_pairs', 1),
    # trove/services/decision/verdict_store.py
    ('trove/services/decision/verdict_store.py', 'VerdictStore.dispose', 0),
    # trove/services/drift/store.py
    ('trove/services/drift/store.py', 'DriftStore.dispose', 0),
    # trove/services/jobs/alerts.py
    ('trove/services/jobs/alerts.py', 'evaluate_alert', 0),
    # trove/services/jobs/cron.py
    ('trove/services/jobs/cron.py', 'cron_next', 0),
    # trove/services/jobs/runner.py
    ('trove/services/jobs/runner.py', 'SchedulerRunner._run_decision', 1),
    ('trove/services/jobs/runner.py', 'SchedulerRunner._run_scan', 1),
    ('trove/services/jobs/runner.py', 'SchedulerRunner.run_job', 2),
    # trove/services/jobs/store.py
    ('trove/services/jobs/store.py', 'JobStore.dispose', 0),
    # trove/services/jobs/subscribe.py
    ('trove/services/jobs/subscribe.py', 'SubscriptionService.deliver_for_run', 0),
    # trove/services/kb/backends/pg_hybrid.py
    ('trove/services/kb/backends/pg_hybrid.py', 'PgHybridKbBackend._expand_keyword', 0),
    # trove/services/kb/enum_probe.py
    ('trove/services/kb/enum_probe.py', 'probe_date_ranges', 0),
    ('trove/services/kb/enum_probe.py', 'probe_date_ranges', 1),
    ('trove/services/kb/enum_probe.py', 'probe_enums', 0),
    ('trove/services/kb/enum_probe.py', 'probe_values', 0),
    # trove/services/kb/history_distill.py
    ('trove/services/kb/history_distill.py', 'run_distill', 2),
    ('trove/services/kb/history_distill.py', 'run_lesson_distill', 0),
    ('trove/services/kb/history_distill.py', 'run_lesson_distill', 1),
    ('trove/services/kb/history_distill.py', 'sql_candidate_specs', 0),
    # trove/services/kb/init_pipeline.py
    ('trove/services/kb/init_pipeline.py', '_draft_init_chunk', 0),
    ('trove/services/kb/init_pipeline.py', '_draft_init_chunk.parse_try', 0),
    ('trove/services/kb/init_pipeline.py', '_recover_draft', 0),
    ('trove/services/kb/init_pipeline.py', '_recover_init_tables', 0),
    ('trove/services/kb/init_pipeline.py', 'init_kb', 0),
    ('trove/services/kb/init_pipeline.py', 'init_kb', 1),
    ('trove/services/kb/init_pipeline.py', 'init_kb', 4),
    ('trove/services/kb/init_pipeline.py', 'init_kb._report', 0),
    # trove/services/kb/ledger.py
    ('trove/services/kb/ledger.py', 'AssetLedger.purge_events', 1),
    ('trove/services/kb/ledger.py', 'AssetLedger.record', 1),
    ('trove/services/kb/ledger.py', 'AssetLedger.usage', 1),
    # trove/services/kb/lint.py
    ('trove/services/kb/lint.py', '_parse', 0),
    # trove/services/kb/live_lint.py
    ('trove/services/kb/live_lint.py', 'check_enums', 0),
    ('trove/services/kb/live_lint.py', 'check_undocumented_columns', 0),
    # trove/services/kb/ossie_format.py
    ('trove/services/kb/ossie_format.py', 'qualify_mapping', 0),
    ('trove/services/kb/ossie_format.py', 'qualify_mapping', 1),
    # trove/services/kb/profiling.py
    ('trove/services/kb/profiling.py', '_probe_column', 0),
    ('trove/services/kb/profiling.py', '_probe_sample', 0),
    ('trove/services/kb/profiling.py', '_probe_shape', 0),
    ('trove/services/kb/profiling.py', '_probe_top_values', 0),
    ('trove/services/kb/profiling.py', 'probe_stats', 0),
    # trove/services/kb/provenance.py
    ('trove/services/kb/provenance.py', '_trove_version', 0),
    # trove/services/kb/semantic_draft.py
    ('trove/services/kb/semantic_draft.py', '_parse_refusal_draft', 0),
    ('trove/services/kb/semantic_draft.py', '_recover_draft', 0),
    # trove/services/kb/service.py
    ('trove/services/kb/service.py', 'KbService._backend_for', 0),
    ('trove/services/kb/service.py', 'KbService._backfill_sync_digests', 0),
    ('trove/services/kb/service.py', 'KbService._disk_asset_refusal', 0),
    ('trove/services/kb/service.py', 'KbService.asset_report', 0),
    ('trove/services/kb/service.py', 'KbService.decisions_lint._lint', 0),
    ('trove/services/kb/service.py', 'KbService.iter_items', 0),
    ('trove/services/kb/service.py', 'KbService.merge_blockers', 0),
    ('trove/services/kb/service.py', 'KbService.rate_lesson', 0),
    ('trove/services/kb/service.py', 'KbService.semantics_lint._lint', 0),
    ('trove/services/kb/service.py', '_recency_factor', 0),
    # trove/services/kb/sql_guard.py
    ('trove/services/kb/sql_guard.py', 'sqlglot_ok', 0),
    ('trove/services/kb/sql_guard.py', 'trial_ok', 0),
    # trove/services/kb/template_params.py
    ('trove/services/kb/template_params.py', '_enrich_values', 0),
    ('trove/services/kb/template_params.py', '_resolve_dimension_columns', 0),
    # trove/services/lineage/parse.py
    ('trove/services/lineage/parse.py', 'analyze_query', 0),
    # trove/services/memory/episode.py
    ('trove/services/memory/episode.py', 'EpisodeStore.search._decay', 0),
    # trove/services/memory/preferences.py
    ('trove/services/memory/preferences.py', 'extract_and_store', 1),
    # trove/services/memory/profile.py
    ('trove/services/memory/profile.py', 'build_profile', 0),
    ('trove/services/memory/profile.py', 'build_profile', 1),
    # trove/services/memory/schema_drift.py
    ('trove/services/memory/schema_drift.py', 'detect_drift', 0),
    ('trove/services/memory/schema_drift.py', 'detect_drift', 1),
    ('trove/services/memory/schema_drift.py', 'detect_drift', 2),
    # trove/services/memory/service.py
    ('trove/services/memory/service.py', 'MemoryService._datasource_names', 0),
    ('trove/services/memory/service.py', 'MemoryService._datasource_names', 1),
    ('trove/services/memory/service.py', 'MemoryService._purge_retrieval_log', 0),
    ('trove/services/memory/service.py', 'MemoryService.touch', 0),
    # trove/services/question_coverage/engine.py
    ('trove/services/question_coverage/engine.py', 'CoverageEngine._gaps_for', 0),
    ('trove/services/question_coverage/engine.py', 'CoverageEngine.deterministic', 0),
    # trove/services/question_coverage/store.py
    ('trove/services/question_coverage/store.py', 'CoverageStore.dispose', 0),
    # trove/services/retrieval/rerank.py
    ('trove/services/retrieval/rerank.py', '_deterministic_score', 0),
    # trove/services/scan/service.py
    ('trove/services/scan/service.py', 'ScanService._provider_for', 0),
    # trove/services/semantic_layer/candidates.py
    ('trove/services/semantic_layer/candidates.py', '_column_ref', 0),
    ('trove/services/semantic_layer/candidates.py', '_metric_spec', 0),
    ('trove/services/semantic_layer/candidates.py', 'create_specs', 1),
    ('trove/services/semantic_layer/candidates.py', 'create_specs', 2),
    # trove/services/semantic_layer/changes.py
    ('trove/services/semantic_layer/changes.py', 'ChangeService._head_commit', 0),
    ('trove/services/semantic_layer/changes.py', '_load_yaml_str', 0),
    # trove/services/semantic_layer/compiler.py
    ('trove/services/semantic_layer/compiler.py', 'SemanticCompiler._apply_analysis', 0),
    ('trove/services/semantic_layer/compiler.py', 'SemanticCompiler._apply_spine', 0),
    ('trove/services/semantic_layer/compiler.py', 'SemanticCompiler._inline_metric', 0),
    ('trove/services/semantic_layer/compiler.py', 'SemanticCompiler._promote_fanout_counts', 0),
    ('trove/services/semantic_layer/compiler.py', 'SemanticCompiler._referenced_tables', 0),
    ('trove/services/semantic_layer/compiler.py', 'SemanticCompiler._render_metric_filter', 0),
    ('trove/services/semantic_layer/compiler.py', 'SemanticCompiler._set_field_anchor', 0),
    ('trove/services/semantic_layer/compiler.py', 'SemanticCompiler._single_agg_column', 0),
    ('trove/services/semantic_layer/compiler.py', '_agg_signature', 0),
    ('trove/services/semantic_layer/compiler.py', '_clause_refs', 0),
    ('trove/services/semantic_layer/compiler.py', '_explicit_join_edges', 0),
    ('trove/services/semantic_layer/compiler.py', '_generated_signature', 0),
    ('trove/services/semantic_layer/compiler.py', '_join_pair_keys', 0),
    ('trove/services/semantic_layer/compiler.py', '_looks_like_formula', 0),
    ('trove/services/semantic_layer/compiler.py', '_predicate_conds', 0),
    ('trove/services/semantic_layer/compiler.py', '_scalar_expr_analyze', 0),
    ('trove/services/semantic_layer/compiler.py', '_share_shape', 0),
    ('trove/services/semantic_layer/compiler.py', '_table_pairs_from_joins', 0),
    ('trove/services/semantic_layer/compiler.py', 'canonical_ratio_text', 0),
    ('trove/services/semantic_layer/compiler.py', 'natural_key_literals_grounded', 0),
    ('trove/services/semantic_layer/compiler.py', 'skeleton_preserved', 0),
    ('trove/services/semantic_layer/compiler.py', 'validate_compiled_sql', 0),
    # trove/services/semantic_layer/diff.py
    ('trove/services/semantic_layer/diff.py', '_parse', 0),
    # trove/services/semantic_layer/importers/base.py
    ('trove/services/semantic_layer/importers/base.py', 'dataset_refs', 0),
    ('trove/services/semantic_layer/importers/base.py', 'qualify_expression', 0),
    ('trove/services/semantic_layer/importers/base.py', 'validate_expression', 0),
    # trove/services/semantic_layer/importers/landing.py
    ('trove/services/semantic_layer/importers/landing.py', 'load_document', 0),
    # trove/services/semantic_layer/issues.py
    ('trove/services/semantic_layer/issues.py', '_normalized_expression', 0),
    ('trove/services/semantic_layer/issues.py', '_normalized_expression', 1),
    # trove/services/semantic_layer/manage.py
    ('trove/services/semantic_layer/manage.py', '_load_yaml', 0),
    # trove/services/semantic_layer/ossie.py
    ('trove/services/semantic_layer/ossie.py', '_dataset_refs', 0),
    # trove/services/semantic_layer/plan.py
    ('trove/services/semantic_layer/plan.py', 'parse_plan_query', 0),
    # trove/services/semantic_layer/provider.py
    ('trove/services/semantic_layer/provider.py', 'SemanticLayerProvider.__init__', 0),
    # trove/services/semantic_layer/rls.py
    ('trove/services/semantic_layer/rls.py', 'render_predicate', 0),
    # trove/services/semantic_layer/sandbox.py
    ('trove/services/semantic_layer/sandbox.py', 'run_replay', 0),
    ('trove/services/semantic_layer/sandbox.py', 'run_replay', 1),
    # trove/services/sql/budget.py
    ('trove/services/sql/budget.py', 'force_limit', 0),
    ('trove/services/sql/budget.py', 'force_limit', 1),
    # trove/services/sql/guard.py
    ('trove/services/sql/guard.py', '_parse', 0),
    ('trove/services/sql/guard.py', '_parse', 1),
    # trove/services/sql/row_guard.py
    ('trove/services/sql/row_guard.py', 'estimate_max_rows', 0),
    # trove/services/sql/terminate.py
    ('trove/services/sql/terminate.py', 'QueryTerminator.terminate', 0),
    # trove/services/sql/validator.py
    ('trove/services/sql/validator.py', 'SQLValidator.validate', 0),
    # trove/services/user_facts/service.py
    ('trove/services/user_facts/service.py', '_is_expired', 0),
    ('trove/services/user_facts/service.py', '_recency_factor', 0),
    # trove/storage/run_record_store.py
    ('trove/storage/run_record_store.py', 'RunRecordStore.dispose', 0),
    ('trove/storage/run_record_store.py', 'RunRecordStore.has_data', 0),
    # trove/tracing/assembly.py
    ('trove/tracing/assembly.py', 'tools_from_registry', 0),
    # trove/tracing/run_record.py
    ('trove/tracing/run_record.py', 'RunRecorder.enabled', 0),
    ('trove/tracing/run_record.py', '_snapshot', 0),
    # trove/workflow/context_budget.py
    ('trove/workflow/context_budget.py', '<module>', 0),
    ('trove/workflow/context_budget.py', 'count_tokens', 0),
    # trove/workflow/graphs.py
    ('trove/workflow/graphs.py', '_run_candidate_subagent', 0),
    ('trove/workflow/graphs.py', 'make_gen_retrieve.gen_retrieve', 0),
    ('trove/workflow/graphs.py', 'make_gen_retrieve.gen_retrieve', 1),
    ('trove/workflow/graphs.py', 'make_gen_retrieve.gen_retrieve', 2),
    ('trove/workflow/graphs.py', 'make_gen_retrieve.gen_retrieve', 7),
    ('trove/workflow/graphs.py', 'make_gen_retrieve.gen_retrieve._shot', 0),
    ('trove/workflow/graphs.py', 'make_route_intent._classify', 0),
    ('trove/workflow/graphs.py', 'make_route_intent._classify', 1),
    ('trove/workflow/graphs.py', 'make_route_intent._classify', 2),
    ('trove/workflow/graphs.py', 'make_route_intent.route_intent', 0),
    # trove/workflow/nodes/chart.py
    ('trove/workflow/nodes/chart.py', '_semantic_time_hints', 0),
    # trove/workflow/nodes/confirm_draft.py
    ('trove/workflow/nodes/confirm_draft.py', 'make_confirm_draft.confirm_draft', 1),
    # trove/workflow/nodes/execute_sql.py
    ('trove/workflow/nodes/execute_sql.py', 'make_execute_sql.execute_sql', 1),
    # trove/workflow/nodes/fast_match.py
    ('trove/workflow/nodes/fast_match.py', 'make_fast_match.fast_match', 1),
    ('trove/workflow/nodes/fast_match.py', 'template_sql_shape_ok', 0),
    # trove/workflow/nodes/gen_sql.py
    ('trove/workflow/nodes/gen_sql.py', '_append_limit', 0),
    ('trove/workflow/nodes/gen_sql.py', '_has_limit', 0),
    ('trove/workflow/nodes/gen_sql.py', '_parse_error_with_position', 0),
    ('trove/workflow/nodes/gen_sql.py', '_probe_result', 0),
    ('trove/workflow/nodes/gen_sql.py', '_probe_result', 1),
    ('trove/workflow/nodes/gen_sql.py', '_search_one', 0),
    ('trove/workflow/nodes/gen_sql.py', 'build_sql_registry._lazy_schema', 0),
    ('trove/workflow/nodes/gen_sql.py', 'build_sql_registry.explain_tool', 0),
    ('trove/workflow/nodes/gen_sql.py', 'build_sql_registry.lookup_schema_tool', 0),
    ('trove/workflow/nodes/gen_sql.py', 'static_semantic_warnings', 0),
    ('trove/workflow/nodes/gen_sql.py', 'validate_sql', 0),
    # trove/workflow/nodes/query_sketch.py
    ('trove/workflow/nodes/query_sketch.py', '_expr_signature', 0),
    ('trove/workflow/nodes/query_sketch.py', '_ordering_candidates', 0),
    ('trove/workflow/nodes/query_sketch.py', '_replan_feedback', 0),
    ('trove/workflow/nodes/query_sketch.py', '_replan_feedback', 1),
    ('trove/workflow/nodes/query_sketch.py', '_schema_map', 0),
    ('trove/workflow/nodes/query_sketch.py', '_sql_projections', 0),
    ('trove/workflow/nodes/query_sketch.py', 'make_query_sketch.query_sketch', 0),
    ('trove/workflow/nodes/query_sketch.py', 'make_query_sketch.query_sketch', 1),
    ('trove/workflow/nodes/query_sketch.py', 'make_query_sketch.query_sketch.call_query_sketch', 0),
    # trove/workflow/nodes/reflect.py
    ('trove/workflow/nodes/reflect.py', '_projection_width_matches', 0),
    # trove/workflow/nodes/refuse.py
    ('trove/workflow/nodes/refuse.py', '_expr_ok', 0),
    ('trove/workflow/nodes/refuse.py', '_mechanical_metric_ok', 0),
    ('trove/workflow/nodes/refuse.py', '_mechanical_metric_ok', 2),
    ('trove/workflow/nodes/refuse.py', '_mechanical_metric_ok', 3),
    # trove/workflow/nodes/schema_linking.py
    ('trove/workflow/nodes/schema_linking.py', '_render_semantic_context', 0),
    ('trove/workflow/nodes/schema_linking.py', '_semantic_match_datasets', 0),
    ('trove/workflow/nodes/schema_linking.py', '_semantic_match_datasets', 1),
    ('trove/workflow/nodes/schema_linking.py', 'make_schema_linking.schema_linking', 0),
    # trove/workflow/nodes/select.py
    ('trove/workflow/nodes/select.py', 'make_select_consensus.select.run', 0),
    # trove/workflow/rules.py
    ('trove/workflow/rules.py', '_division_uses_integer_aggregates', 0),
    ('trove/workflow/rules.py', '_scope_ambiguity', 0),
    ('trove/workflow/rules.py', 'verify', 0),
})


def test_no_new_silent_broad_exception_handlers() -> None:
    current = _silent_handlers()
    extra, missing = current - SILENT_HANDLERS, SILENT_HANDLERS - current
    hint = (
        "A broad handler was added without making its failure observable. "
        "Log it (logger.warning/debug), re-raise it, or — if silence is truly "
        "deliberate — put `# noqa: BLE001` on the except line. If you *fixed* "
        "a handler, delete its baseline row."
    )
    assert not extra and not missing, (
        f"silent broad handlers drifted — new={sorted(extra)} "
        f"stale_baseline={sorted(missing)}. {hint}"
    )


# ── detector self-tests (synthetic, no repo files) ────────────────────────


def _silent(source: str) -> set[tuple[str, str, int]]:
    return _silent_handlers_in_source("trove/synthetic.py", source)


def test_detector_catches_a_silent_handler() -> None:
    assert _silent(
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except Exception:\n"
        "        pass\n"
    ) == {("trove/synthetic.py", "f", 0)}


@pytest.mark.parametrize("body", [
    "raise\n",
    "raise RuntimeError('x')\n",
    "logger.warning('failed: %s', e)\n",
    "log.debug('failed')\n",
    "print('failed')\n",
])
def test_detector_accepts_visible_handlers(body: str) -> None:
    source = (
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except Exception as e:\n"
        f"        {body}"
    )
    assert _silent(source) == set()


def test_noqa_marker_counts_as_deliberate() -> None:
    assert _silent(
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except Exception:  # noqa: BLE001 — best-effort\n"
        "        pass\n"
    ) == set()


def test_narrow_and_tuple_handlers() -> None:
    # Narrow handlers are not the ratchet's business.
    assert _silent(
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except ValueError:\n"
        "        pass\n"
    ) == set()
    # A tuple containing Exception is broad.
    assert _silent(
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except (ValueError, Exception):\n"
        "        pass\n"
    ) == {("trove/synthetic.py", "f", 0)}
    # Bare except catches BaseException.
    assert _silent(
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except:\n"
        "        pass\n"
    ) == {("trove/synthetic.py", "f", 0)}


def test_qualname_and_ordinals() -> None:
    source = (
        "class C:\n"
        "    async def m(self):\n"
        "        try:\n"
        "            g()\n"
        "        except Exception:\n"
        "            pass\n"
        "        try:\n"
        "            g()\n"
        "        except Exception:\n"
        "            pass\n"
        "        def inner():\n"
        "            try:\n"
        "                g()\n"
        "            except BaseException:\n"
        "                pass\n"
    )
    assert _silent(source) == {
        ("trove/synthetic.py", "C.m", 0),
        ("trove/synthetic.py", "C.m", 1),
        ("trove/synthetic.py", "C.m.inner", 0),
    }


def test_log_in_a_nested_handler_does_not_hide_the_outer_one() -> None:
    assert _silent(
        "def f():\n"
        "    try:\n"
        "        g()\n"
        "    except Exception:\n"
        "        try:\n"
        "            h()\n"
        "        except Exception:\n"
        "            logger.warning('inner')\n"
    ) == {("trove/synthetic.py", "f", 0)}


def test_baseline_was_generated_from_the_detector() -> None:
    """Guard against a hand-written baseline: the detector must at least run.

    The real drift assertion is above; this keeps the baseline honest when
    someone replaces it wholesale (every key must name an existing file).
    """
    assert SILENT_HANDLERS, "baseline regenerated? run _silent_handlers()"
    for rel, _qualname, _ordinal in SILENT_HANDLERS:
        assert (ROOT / rel).is_file(), f"baseline names a missing file: {rel}"
