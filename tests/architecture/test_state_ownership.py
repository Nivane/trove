"""P1 — state ownership manifest (who owns which state, who may construct it).

Every durable artifact in this repository has exactly one authoritative writer,
a frozen set of construction sites, and a direction it may be imported from.
Those claims are prose in ``CLAUDE.md`` today; this module turns them into a
manifest that fails when a *new* proper path appears.

**These are ratchets, not sandboxes.** Dynamic imports, ``getattr`` and
runtime-assembled paths have no AST shape and slip through; so do helpers that
receive a filename/path as a parameter (the caller is attributed instead). What
they do catch is the ordinary way a second writer or a second constructor
appears: someone writes the obvious ``from ... import RunRecordStore`` and calls
it, or adds ``semantics_path(...).write_text(...)`` in a new function.

Known-bypasses are frozen as today's truth, not blessed as good design:
``API_STORE_IMPORTS`` records five routers that reach a store directly instead
of going through ``app.state``, and ``BYPASSES`` records scripts that write KB
YAML outside the service. Both are P2 material — the point of freezing them is
that a fifth one cannot appear silently.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Mapping

import pytest

from tests.helpers import arch
from trove.workflow.state import GenSQLState, WorkflowState

Sources = Mapping[str, str]
REAL = arch.py_sources()


# ── manifest: store classes ──────────────────────────────────────────────
#
# Classification vocabulary:
#   durable    — authoritative state with a stable on-disk location
#   derived    — rebuildable from KB YAML / another authority
#   protocol   — ABC / interface with no storage of its own
#   in_memory  — process-lifetime registry, lost on restart by design
#   unwired    — implemented but no production constructor (dead asset)


STORE_CLASSES: dict[str, str] = {
    "trove.services.action.store.ActionStore": "durable",
    "trove.services.admin_settings.store.SettingsStore": "durable",
    "trove.services.auth.store.AppDbStore": "durable",
    "trove.services.datasource.config_store.ConfigStore": "durable",
    "trove.services.decision.drafts.DecisionDraftStore": "durable",
    "trove.services.decision.verdict_store.VerdictStore": "durable",
    "trove.services.drift.store.DriftStore": "durable",
    "trove.services.jobs.store.JobStore": "durable",
    "trove.services.kb.backends.dense.PgVectorStore": "derived",
    "trove.services.kb.backends.dense.SqliteVectorStore": "derived",
    "trove.services.kb.backends.dense.VectorStore": "protocol",
    "trove.services.kb.init_tasks.InitTaskStore": "in_memory",
    "trove.services.kb.ledger.AssetLedger": "unwired",
    "trove.services.lineage.service.LineageService": "durable",
    "trove.services.memory.episode.EpisodeStore": "durable",
    "trove.services.memory.preferences.PreferenceStore": "durable",
    "trove.services.question_coverage.store.CoverageStore": "durable",
    "trove.services.retrieval.pg_store.PgHybridStore": "derived",
    "trove.services.retrieval.query_log.QueryLogRecorder": "durable",
    "trove.services.retrieval.sqlite_store.SqliteHybridStore": "derived",
    "trove.services.retrieval.store.HybridStore": "protocol",
    "trove.services.user_facts.store.UserFactsStore": "durable",
    "trove.storage.config_store.ConfigStore": "durable",
    "trove.storage.run_record_store.RunRecordStore": "durable",
    "trove.storage.session_store.SessionStore": "durable",
    "trove.storage.task_store.TaskStore": "durable",
}

#: Not named ``*Store`` but owned state all the same.
_EXTRA_CLASS_NAMES = ("AssetLedger", "QueryLogRecorder", "LineageService")

CLASSIFICATIONS = {"durable", "derived", "protocol", "in_memory", "unwired"}


def store_inventory(sources: Sources) -> set[str]:
    found = set(arch.class_definitions(sources))
    for rel, source in sources.items():
        if not rel.startswith("trove/"):
            continue
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef) and node.name in _EXTRA_CLASS_NAMES:
                found.add(f"{arch.module_name(rel)}.{node.name}")
    return found


# ── manifest: artifacts ──────────────────────────────────────────────────
#
# ``writer`` and ``note`` are human-read documentation; the machine-checked
# fields are id/path/authority/owner (shape) and owner coverage of the durable
# classes. Path templates must name one of the four roots — that is how the
# ``~/.trove/.trove`` double nesting stays visible instead of hiding in code.


@dataclass(frozen=True)
class Artifact:
    id: str
    path: str
    authority: str
    owner: str
    writer: str
    rebuildable: bool
    note: str = ""


ARTIFACTS: tuple[Artifact, ...] = (
    Artifact(
        "sessions", "{home}/sessions.sqlite", "authoritative",
        "trove.storage.session_store.SessionStore",
        "trove/storage/session_store.py", False,
        "sessions + messages + hits; TROVE_STORAGE_URL switches to Postgres",
    ),
    Artifact(
        "tasks", "{home}/sessions.sqlite", "authoritative",
        "trove.storage.task_store.TaskStore",
        "trove/agent/session.py", False,
        "same physical DB as sessions, keyed by project/session",
    ),
    Artifact(
        "checkpoints", "{home}/checkpoints.db", "checkpoint",
        "", "trove/storage/checkpoint_store.py", False,
        "LangGraph resumable state; AsyncPostgresSaver under TROVE_STORAGE_URL",
    ),
    Artifact(
        "run_records", "{home}/run_records.sqlite", "authoritative",
        "trove.storage.run_record_store.RunRecordStore",
        "trove/tracing/run_record.py", False,
        "evidence layer; UTC-only clocks; retention purge",
    ),
    Artifact(
        "project_config", "{project}/.trove/config.yml", "authoritative",
        "trove.storage.config_store.ConfigStore",
        "trove/storage/config_store.py", False,
        "whitelisted project config keys",
    ),
    Artifact(
        "datasources", "{project}/.trove/datasources.yml", "authoritative",
        "trove.services.datasource.config_store.ConfigStore",
        "trove/services/datasource/config_store.py", False,
        "registered datasources; secret key lives beside it",
    ),
    Artifact(
        "settings", "{home}/settings.db", "authoritative",
        "trove.services.admin_settings.store.SettingsStore",
        "trove/services/admin_settings/store.py", False,
        "runtime overrides; agent.yml is never written",
    ),
    Artifact(
        "app_db", "{home}/app.db", "authoritative",
        "trove.services.auth.store.AppDbStore",
        "trove/services/auth/store.py", False,
        "auth, audit log, lineage-independent governance tables",
    ),
    Artifact(
        "episodes", "{home}/memory/episodes.sqlite", "authoritative",
        "trove.services.memory.episode.EpisodeStore",
        "trove/services/memory/episode.py", False,
        "factual question/SQL history; not a pending-gated asset",
    ),
    Artifact(
        "preferences", "{home}/memory/preferences.sqlite", "authoritative",
        "trove.services.memory.preferences.PreferenceStore",
        "trove/services/memory/preferences.py", False,
        "preference candidates; low-confidence rows still need confirmation",
    ),
    Artifact(
        "user_facts", "{home}/user_facts.db", "authoritative",
        "trove.services.user_facts.store.UserFactsStore",
        "trove/services/user_facts/store.py", False,
        "durable user calibers with expiry",
    ),
    Artifact(
        "verdicts", "{home}/.trove/decisions/verdicts.sqlite", "authoritative",
        "trove.services.decision.verdict_store.VerdictStore",
        "trove/services/decision/verdict_store.py", False,
        "double nesting under home: project_root is config.home",
    ),
    Artifact(
        "actions", "{home}/.trove/actions/actions.sqlite", "authoritative",
        "trove.services.action.store.ActionStore",
        "trove/services/action/store.py", False,
        "proposals/deliveries/outcomes; same double nesting as verdicts",
    ),
    Artifact(
        "jobs", "{home}/.trove/jobs/jobs.sqlite", "authoritative",
        "trove.services.jobs.store.JobStore",
        "trove/services/jobs/store.py", False,
        "schedules, runs, subscriptions; CLI schedule uses cwd/.trove instead",
    ),
    Artifact(
        "decision_drafts", "{kb}/<datasource>/decision_drafts.yml", "authoritative",
        "trove.services.decision.drafts.DecisionDraftStore",
        "trove/services/decision/drafts.py", False,
        "pending-only queue; never read by the execution path",
    ),
    Artifact(
        "lineage_runtime", "{lineage}/lineage.sqlite", "derived",
        "trove.services.lineage.service.LineageService",
        "trove/services/lineage/service.py", True,
        "mirror; definitions.yml is the authority",
    ),
    Artifact(
        "lineage_definitions", "{lineage}/<datasource>/definitions.yml", "authoritative",
        "trove.services.lineage.service.LineageService",
        "trove/services/lineage/service.py", False,
        "human-authored table/column lineage",
    ),
    Artifact(
        "query_log", "{home}/retrieval/query_log.sqlite", "authoritative",
        "trove.services.retrieval.query_log.QueryLogRecorder",
        "trove/services/retrieval/query_log.py", False,
        "retrieval observability; retention only when memory.retention_days set",
    ),
    Artifact(
        "kb_mirror", "{kb}/kb.sqlite", "derived",
        "trove.services.kb.service.KbService",
        "trove/services/kb/service.py", True,
        "SQLite FTS5 mirror; Postgres uses pg_hybrid instead",
    ),
    Artifact(
        "retrieval_index", "{kb}/kb.sqlite", "derived",
        "trove.services.retrieval.pg_store.PgHybridStore",
        "trove/services/retrieval/factory.py", True,
        "sqlite backend shares kb.sqlite; pg_hybrid lives in the business PG",
    ),
    Artifact(
        "kb_yaml", "{kb}/<datasource>/*.yml", "authoritative",
        "trove.services.kb.service.KbService",
        "trove/services/kb/service.py + semantic_layer/changes.py", False,
        "git-versioned KB; writer map is machine-checked below",
    ),
    Artifact(
        "drift", "{project}/.trove/drift/drift.sqlite", "derived",
        "trove.services.drift.store.DriftStore",
        "trove/services/drift/store.py", True,
        "schema drift findings; recomputed from live catalog + schema_notes",
    ),
    Artifact(
        "coverage", "{project}/.trove/coverage/coverage.sqlite", "authoritative",
        "trove.services.question_coverage.store.CoverageStore",
        "trove/services/question_coverage/store.py", False,
        "question-coverage runs; interrupted runs are marked, never rewritten",
    ),
    Artifact(
        "evalset", "{project}/.trove/evalsets/<datasource>/evalset.yml", "authoritative",
        "", "trove/services/evalset/store.py", False,
        "git-versioned eval assets; pending entries never score",
    ),
    Artifact(
        "skills", "{project}/.trove/skills/<name>/SKILL.md", "authoritative",
        "", "trove/services/skills/service.py", False,
        "org skills; git-versioned with revision counter",
    ),
    Artifact(
        "presets", "{project}/.trove/presets/<name>/preset.yml", "authoritative",
        "", "trove/services/presets/service.py", False,
        "org presets; apply only lands pending drafts",
    ),
    Artifact(
        "action_templates", "{project}/.trove/actions/<name>/action.yml", "authoritative",
        "", "trove/services/action/templates.py", False,
        "org action templates; confirm gate before rules may reference them",
    ),
    Artifact(
        "asset_ledger", "{home}/assets/asset_ledger.sqlite", "authoritative",
        "trove.services.kb.ledger.AssetLedger",
        "trove/services/kb/ledger.py", False,
        "dead asset: no production constructor (classification: unwired)",
    ),
)

ROOTS = ("{home}", "{project}", "{kb}", "{lineage}")
AUTHORITIES = {"authoritative", "derived", "cache", "checkpoint"}


# ── manifest: constructor sites ──────────────────────────────────────────
#
# Frozen by FILE, not line — re-anchoring line numbers on every edit would be
# noise. A classmethod factory counts: ``QueryLogRecorder.for_home`` is a
# construction site even though the target is an attribute of the class.


CONSTRUCTORS: dict[str, frozenset[str]] = {
    "trove.services.action.store.ActionStore": frozenset({"trove/main.py"}),
    "trove.services.admin_settings.store.SettingsStore": frozenset({"trove/main.py"}),
    "trove.services.auth.store.AppDbStore": frozenset({
        "scripts/distill_history.py",
        "trove/cli/commands/evalset_cmds.py",
        "trove/services/auth/service.py",
    }),
    "trove.services.datasource.config_store.ConfigStore": frozenset({
        "scripts/check_drift.py",
        "trove/cli/commands/evalset_cmds.py",
        "trove/main.py",
        "trove/services/validate/service.py",
    }),
    "trove.services.decision.drafts.DecisionDraftStore": frozenset({
        "trove/api/routers/decisions.py",
        "trove/services/presets/service.py",
        "trove/services/scan/service.py",
        "trove/services/validate/service.py",
    }),
    "trove.services.decision.verdict_store.VerdictStore": frozenset({"trove/main.py"}),
    "trove.services.drift.store.DriftStore": frozenset({
        "trove/api/routers/drift.py",
        "trove/api/routers/overview.py",
        "trove/api/routers/semantic.py",
        "trove/main.py",
        "trove/services/drift/service.py",
        "trove/services/semantic_layer/changes.py",
    }),
    "trove.services.jobs.store.JobStore": frozenset({
        "trove/cli/schedule_cmds.py",
        "trove/main.py",
    }),
    "trove.services.lineage.service.LineageService": frozenset({
        "trove/api/routers/governance.py",
        "trove/main.py",
        "trove/services/kb/history_distill.py",
    }),
    "trove.services.memory.episode.EpisodeStore": frozenset({
        "trove/services/evalset/sources.py",
        "trove/services/extensions/dryrun.py",
        "trove/services/kb/history_distill.py",
        "trove/services/memory/service.py",
    }),
    "trove.services.memory.preferences.PreferenceStore": frozenset({
        "trove/services/memory/service.py",
    }),
    "trove.services.question_coverage.store.CoverageStore": frozenset({"trove/main.py"}),
    "trove.services.retrieval.query_log.QueryLogRecorder": frozenset({
        "trove/services/kb/backends/registry.py",
        "trove/services/memory/service.py",
        "trove/services/retrieval/factory.py",
    }),
    "trove.services.user_facts.store.UserFactsStore": frozenset({
        "trove/services/user_facts/service.py",
    }),
    "trove.storage.config_store.ConfigStore": frozenset({
        "trove/cli/commands/system_cmds.py",
    }),
    "trove.storage.run_record_store.RunRecordStore": frozenset({
        "scripts/offline_eval.py",
        "trove/cli/maintenance_cmds.py",
        "trove/main.py",
    }),
    "trove.storage.session_store.SessionStore": frozenset({
        "trove/cli/maintenance_cmds.py",
        "trove/cli/schedule_cmds.py",
        "trove/main.py",
    }),
    "trove.storage.task_store.TaskStore": frozenset({"trove/agent/session.py"}),
}


def _canonical(
    target: str, defs: Mapping[str, str], sources: Sources, qnames: set[str]
) -> str | None:
    canon = arch.canonical_class(target, defs, sources)
    if canon in qnames:
        return canon
    # classmethod factories: ``...QueryLogRecorder.for_home``
    for qname in qnames:
        if target == qname or target.startswith(qname + "."):
            return qname
    return None


def constructor_sites(sources: Sources, qnames: set[str]) -> dict[str, set[str]]:
    defs = arch.class_definitions(sources, suffix="")
    sites: dict[str, set[str]] = {}
    for rel, source in sources.items():
        for target in arch.constructor_targets(rel, ast.parse(source)):
            canon = _canonical(target, defs, sources, qnames)
            if canon is not None:
                sites.setdefault(canon, set()).add(rel)
    return sites


# ── manifest: import directions ──────────────────────────────────────────


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def detect_evidence_imports(sources: Sources) -> list[str]:
    """workflow / agent may not reach the evidence store directly."""
    offenders: list[str] = []
    for rel, source in sources.items():
        if not (arch.under(rel, "trove/workflow") or rel == "trove/agent/session.py"):
            continue
        for module in sorted(arch.resolve_imports(rel, ast.parse(source))):
            if _matches(module, "trove.storage.run_record_store"):
                offenders.append(f"{rel} imports {module}")
    return offenders


def detect_tracing_upstream_imports(sources: Sources) -> list[str]:
    """tracing is a sink: it may not import workflow / agent / KB."""
    offenders: list[str] = []
    for rel, source in sources.items():
        if not arch.under(rel, "trove/tracing"):
            continue
        for module in sorted(arch.resolve_imports(rel, ast.parse(source))):
            if any(
                _matches(module, bad)
                for bad in ("trove.workflow", "trove.agent", "trove.services.kb")
            ):
                offenders.append(f"{rel} imports {module}")
    return offenders


def detect_api_store_imports(sources: Sources) -> list[str]:
    defs = arch.class_definitions(sources)
    qnames = set(STORE_CLASSES)
    offenders: list[str] = []
    for rel, source in sources.items():
        if not arch.under(rel, "trove/api"):
            continue
        tree = ast.parse(source)
        for local, target in sorted(arch.import_bindings(rel, tree).items()):
            canon = _canonical(target, defs, sources, qnames)
            if canon is not None:
                offenders.append(f"{rel}:{local} -> {canon}")
    return offenders


def detect_api_workflow_imports(
    sources: Sources, allowed: Mapping[str, frozenset[str]]
) -> list[str]:
    """Only the frozen api→workflow modules may be imported."""
    workflow_modules = {
        arch.module_name(rel) for rel in sources if arch.under(rel, "trove/workflow")
    }
    offenders: list[str] = []
    for rel, source in sources.items():
        if not arch.under(rel, "trove/api"):
            continue
        permitted = allowed.get(rel, frozenset())
        for module in sorted(arch.resolve_imports(rel, ast.parse(source))):
            if module in workflow_modules and module not in permitted:
                offenders.append(f"{rel} imports {module}")
    return offenders


#: Today's count is one module (``miss_copy``). Freeze it per file so a second
#: import added to another router is red even if it names the same module.
API_WORKFLOW_MODULES: dict[str, frozenset[str]] = {
    "trove/api/routers/admin.py": frozenset({
        "trove.workflow.miss_copy",
        "trove.workflow.confidence",
    }),
}

#: Known direct store imports in API routers (P2: route through app.state).
API_STORE_IMPORTS = frozenset({
    "trove/api/routers/decisions.py:DecisionDraftStore -> "
    "trove.services.decision.drafts.DecisionDraftStore",
    "trove/api/routers/drift.py:DriftStore -> trove.services.drift.store.DriftStore",
    "trove/api/routers/governance.py:LineageService -> "
    "trove.services.lineage.service.LineageService",
    "trove/api/routers/overview.py:DriftStore -> trove.services.drift.store.DriftStore",
    "trove/api/routers/semantic.py:DriftStore -> trove.services.drift.store.DriftStore",
})


# ── manifest: KB YAML writers ────────────────────────────────────────────
#
# Attribution is by "function names the asset and reaches a write", with:
#   * docstrings excluded (a comment is not a write),
#   * module-level filename constants resolved (``DRAFTS_FILE``),
#   * one fixed-point hop through ``self.<method>`` for path derivations and
#     for private write primitives (``_save`` / ``_save_drafts`` / ``_write``),
#   * literal matching on the BASENAME, so ``base.semantics.yml`` staging
#     snapshots are not mistaken for the authoritative file.


@dataclass(frozen=True)
class YamlAsset:
    filename: str
    path_attrs: tuple[str, ...] = ()
    calls: tuple[tuple[str, str], ...] = ()        # (rel prefix, call attr)
    definers: tuple[tuple[str, str, str], ...] = ()  # (rel prefix, class, method)


YAML_ASSETS: tuple[YamlAsset, ...] = (
    YamlAsset("schema_notes.yml", ("schema_notes_path", "notes_path")),
    YamlAsset(
        "semantics.yml",
        ("semantics_path",),
        (("scripts/regen_kb_generated.py", "_write_pending"),),
    ),
    YamlAsset(
        "examples.yml",
        ("examples_path",),
        (("scripts/regen_kb_generated.py", "_write_pending"),),
    ),
    YamlAsset("lessons.yml", ("lessons_path",)),
    YamlAsset("decisions.yml", ("decisions_path",)),
    YamlAsset(
        "decision_drafts.yml",
        (),
        (("trove/services/decision/drafts.py", "_write"),),
        (("trove/services/decision/drafts.py", "DecisionDraftStore", "_write"),),
    ),
    YamlAsset(
        "semantic_drafts.yml",
        ("semantic_drafts_path", "_drafts_path", "drafts_path"),
        (("trove/services/semantic_layer/", "_save_drafts"),),
        (("trove/services/semantic_layer/", "SemanticManager", "_save_drafts"),),
    ),
    YamlAsset(
        "semantic_changes.yml",
        ("semantic_changes_path", "changes_path"),
        (("trove/services/semantic_layer/changes.py", "_save"),),
        (("trove/services/semantic_layer/changes.py", "ChangeService", "_save"),),
    ),
    YamlAsset("rules.yml", ("rules_path",)),
)

_WRITE_CALLS = frozenset({
    "_dump_yaml",
    "_write_doc",
    "_write_init",
    "_init_write",
    "_init_doc",
    "_init_file",
    "_append_entry",
    "write_text",
    "_save_drafts",
})


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            ids.add(id(first.value))
    return ids


def _call_attr(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _mentions(
    expr: ast.AST,
    filename: str,
    path_attrs: set[str],
    module_consts: Mapping[str, str],
    hop: Mapping[str, bool],
    docs: set[int],
) -> bool:
    for node in ast.walk(expr):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and node.value.rstrip("/").split("/")[-1] == filename
        ):
            return True
        if isinstance(node, ast.Attribute) and node.attr in path_attrs:
            return True
        if isinstance(node, ast.Name) and node.id in module_consts:
            value = module_consts[node.id]
            if value.rstrip("/").split("/")[-1] == filename:
                return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and hop.get(node.func.attr)
        ):
            return True
    return False


def _assigned_names(node: ast.AST) -> list[str]:
    targets = (
        node.targets
        if isinstance(node, ast.Assign)
        else [node.target]
        if isinstance(node, ast.AnnAssign)
        else []
    )
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Tuple | ast.List):
            names += [elt.id for elt in target.elts if isinstance(elt, ast.Name)]
    return names


def _functions(tree: ast.Module) -> list[tuple[str | None, ast.AST]]:
    funcs: list[tuple[str | None, ast.AST]] = [
        (None, node)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    funcs += [
        (node.name, inner)
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for inner in node.body
        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return funcs


def _site_key(class_name: str | None, func: ast.AST) -> str:
    return f"{class_name}.{func.name}" if class_name else func.name


def _writers_in_module(rel: str, tree: ast.Module, asset: YamlAsset) -> set[str]:
    module_consts = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    functions = _functions(tree)
    path_attrs = set(asset.path_attrs)
    call_specs = {attr for prefix, attr in asset.calls if rel.startswith(prefix)}
    define_specs = {
        (cls, method)
        for prefix, cls, method in asset.definers
        if rel.startswith(prefix)
    }
    mentions: dict[str, bool] = {}
    writers: dict[str, bool] = {}

    for _ in range(10):
        changed = False
        for class_name, func in functions:
            key = _site_key(class_name, func)
            if not mentions.get(key):
                if _mentions(
                    func,
                    asset.filename,
                    path_attrs,
                    module_consts,
                    mentions,
                    _docstring_ids(func),
                ):
                    mentions[key] = True
                    changed = True
        for class_name, func in functions:
            key = _site_key(class_name, func)
            if writers.get(key):
                continue
            docs = _docstring_ids(func)
            asset_names: set[str] = set()
            for node in ast.walk(func):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                if node.value is not None and _mentions(
                    node.value, asset.filename, path_attrs, module_consts, mentions, docs
                ):
                    asset_names.update(_assigned_names(node))

            wrote = bool(class_name) and (class_name, func.name) in define_specs
            if not wrote:
                for node in ast.walk(func):
                    if not isinstance(node, ast.Call):
                        continue
                    attr = _call_attr(node)
                    direct = attr in call_specs or (
                        attr in _WRITE_CALLS
                        and (
                            _mentions(
                                node,
                                asset.filename,
                                path_attrs,
                                module_consts,
                                mentions,
                                docs,
                            )
                            or any(
                                isinstance(arg, ast.Name) and arg.id in asset_names
                                for arg in node.args
                            )
                            or any(
                                isinstance(inner, ast.Name) and inner.id in asset_names
                                for inner in ast.walk(node.func)
                            )
                        )
                    )
                    if direct:
                        wrote = True
                        break
            if not wrote:
                wrote = any(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"
                    and writers.get(
                        f"{class_name}.{node.func.attr}" if class_name else node.func.attr
                    )
                    for node in ast.walk(func)
                )
            if wrote:
                writers[key] = True
                changed = True
        if not changed:
            break
    return {f"{rel}::{key}" for key in writers}


def _asset_tokens(asset: YamlAsset) -> tuple[str, ...]:
    """Cheap substrings that must appear before this asset can be written.

    Call attrs are deliberately not tokens: ``_write`` / ``_save`` are generic
    and would defeat the prefilter. The defining class name is specific enough
    for the private write primitives, and cross-class callers name the file or
    a path attribute anyway.
    """
    return (asset.filename, *asset.path_attrs, *(cls for _, cls, _ in asset.definers))


def detect_yaml_writers(sources: Sources) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {asset.filename: set() for asset in YAML_ASSETS}
    for rel, source in sources.items():
        if not rel.startswith(("trove/", "scripts/")):
            continue
        candidates = [
            asset
            for asset in YAML_ASSETS
            if any(token in source for token in _asset_tokens(asset))
        ]
        if not candidates:
            continue
        tree = ast.parse(source)
        for asset in candidates:
            found[asset.filename] |= _writers_in_module(rel, tree, asset)
    return found


KB_YAML_WRITERS: dict[str, frozenset[str]] = {
    "schema_notes.yml": frozenset({
        "scripts/probe_enums.py::main",
        "trove/services/kb/service.py::KbService.init_notes",
        "trove/services/kb/service.py::KbService.init_schema_notes",
    }),
    "semantics.yml": frozenset({
        "scripts/backfill_values.py::_run",
        "scripts/import_official_semantics.py::main",
        "scripts/regen_kb_generated.py::main",
        "trove/services/kb/service.py::KbService.append_term",
        "trove/services/kb/service.py::KbService.init_semantics",
        "trove/services/kb/service.py::KbService.init_terms",
        "trove/services/semantic_layer/changes.py::ChangeService._write_merge",
        "trove/services/semantic_layer/changes.py::ChangeService.merge",
        "trove/services/semantic_layer/changes.py::ChangeService.merge_auto",
        "trove/services/semantic_layer/changes.py::ChangeService.merge_draft",
    }),
    "examples.yml": frozenset({
        "scripts/regen_kb_generated.py::main",
        "trove/services/kb/service.py::KbService.append_example",
        "trove/services/kb/service.py::KbService.confirm_pending_example",
        "trove/services/kb/service.py::KbService.confirm_pending_examples",
        "trove/services/kb/service.py::KbService.draft_example",
        "trove/services/kb/service.py::KbService.init_examples",
        "trove/services/kb/service.py::KbService.rate_lesson",
        "trove/services/kb/service.py::KbService.reject_pending_example",
        "trove/services/kb/service.py::KbService.reject_pending_examples",
    }),
    "lessons.yml": frozenset({
        "trove/services/kb/service.py::KbService.append_lesson",
        "trove/services/kb/service.py::KbService.confirm_lesson",
        "trove/services/kb/service.py::KbService.confirm_pending_lessons",
        "trove/services/kb/service.py::KbService.rate_lesson",
        "trove/services/kb/service.py::KbService.reject_lesson",
        "trove/services/kb/service.py::KbService.update_lesson_confidence",
    }),
    "decisions.yml": frozenset({
        "scripts/seed_guide_instance.py::wire_rule_action",
        "trove/services/kb/service.py::KbService.save_decisions",
    }),
    "decision_drafts.yml": frozenset({
        "trove/services/decision/drafts.py::DecisionDraftStore._write",
        "trove/services/decision/drafts.py::DecisionDraftStore.add",
        "trove/services/decision/drafts.py::DecisionDraftStore.confirm",
        "trove/services/decision/drafts.py::DecisionDraftStore.reject",
    }),
    "semantic_drafts.yml": frozenset({
        "trove/services/semantic_layer/changes.py::ChangeService._write_merge",
        "trove/services/semantic_layer/changes.py::ChangeService.merge",
        "trove/services/semantic_layer/changes.py::ChangeService.merge_auto",
        "trove/services/semantic_layer/changes.py::ChangeService.merge_draft",
        "trove/services/semantic_layer/manage.py::SemanticManager._save_drafts",
        "trove/services/semantic_layer/manage.py::SemanticManager.create_draft",
        "trove/services/semantic_layer/manage.py::SemanticManager.reject_draft",
    }),
    "semantic_changes.yml": frozenset({
        "trove/services/semantic_layer/changes.py::ChangeService._save",
        "trove/services/semantic_layer/changes.py::ChangeService._write_merge",
        "trove/services/semantic_layer/changes.py::ChangeService.merge",
        "trove/services/semantic_layer/changes.py::ChangeService.merge_auto",
        "trove/services/semantic_layer/changes.py::ChangeService.merge_draft",
        "trove/services/semantic_layer/changes.py::ChangeService.open",
        "trove/services/semantic_layer/changes.py::ChangeService.reject",
    }),
    # Human-authored only — a machine writer here is a policy violation, not a
    # ratchet candidate. ``trove/`` + ``scripts/`` have zero.
    "rules.yml": frozenset(),
}

#: Scripts that write KB YAML outside the service write paths. Reasons are
#: required: an exemption without a why is how a bypass becomes policy.
BYPASSES: dict[str, str] = {
    "scripts/probe_enums.py::main": (
        "存量 KB 的枚举回填工具：读 live 库 DISTINCT 值写回 schema_notes.yml，"
        "输出是给 kb init 复算的输入，不是 agent 生成路径"
    ),
    "scripts/backfill_values.py::_run": (
        "字段级 values 键回填：只补语义模型的值域声明，人工核对后运行"
    ),
    "scripts/import_official_semantics.py::main": (
        "官方数据库文档 → semantics.yml 的确定性导入（零 LLM），"
        "与 kb init 的生成器同源但用于存量库"
    ),
    "scripts/regen_kb_generated.py::main": (
        "生成器拥有条目的重刷（metrics/examples）：只改 _meta 声明为生成器的条目，"
        "人改条目原样保留"
    ),
    "scripts/seed_guide_instance.py::wire_rule_action": (
        "演示种子：给 demo decisions.yml 接上 action 块，仅截图/演练环境使用"
    ),
}


# ── projections ──────────────────────────────────────────────────────────

#: GenSQLState fields that are NOT copies of a WorkflowState field. Everything
#: else is a projection: adding one here is a deliberate manifest edit.
GEN_DERIVED_FIELDS = frozenset({
    "attempts",
    "entities",
    "episodes",
    "few_shots",
    "lessons",
    "metrics",
    "previous_sql",
    "profile",
    "reasoning_context",
    "reflect_reason",
    "rules",
    "term_notes",
    "user_facts",
    "validation_errors",
})


# ── machine checks ───────────────────────────────────────────────────────


def test_store_inventory_is_frozen():
    arch.assert_frozen(STORE_CLASSES, store_inventory(REAL), "store class inventory")


def test_store_inventory_catches_a_new_class():
    inventory = store_inventory({"trove/rogue.py": "class BogusStore:\n    pass\n"})
    assert "trove.rogue.BogusStore" in inventory


def test_classifications_are_known():
    unknown = {
        qname: kind for qname, kind in STORE_CLASSES.items() if kind not in CLASSIFICATIONS
    }
    assert not unknown, f"unknown classification: {unknown}"


def test_every_durable_class_declares_an_artifact():
    owners = {artifact.owner for artifact in ARTIFACTS}
    durable = {q for q, kind in STORE_CLASSES.items() if kind == "durable"}
    assert not durable - owners, (
        f"durable store without an Artifact row: {sorted(durable - owners)}"
    )


def test_constructor_sites_are_frozen():
    durable = {q for q, kind in STORE_CLASSES.items() if kind == "durable"}
    actual = constructor_sites(REAL, durable)
    drift: list[str] = []
    for qname in sorted(durable):
        extra, missing = arch.diff(CONSTRUCTORS[qname], actual.get(qname, set()))
        if extra or missing:
            drift.append(f"{qname}: new={extra} gone={missing}")
    assert not drift, "constructor sites drifted:\n" + "\n".join(drift)


def test_constructor_detector_catches_a_new_site():
    fake = {
        "trove/cli/rogue.py": (
            "from trove.storage.run_record_store import RunRecordStore\n\n"
            "def go():\n    return RunRecordStore('/tmp')\n"
        )
    }
    sites = constructor_sites({**REAL, **fake}, {"trove.storage.run_record_store.RunRecordStore"})
    assert "trove/cli/rogue.py" in sites["trove.storage.run_record_store.RunRecordStore"]


def test_constructor_detector_accepts_a_classmethod_factory():
    fake = {
        "trove/cli/rogue.py": (
            "from trove.services.retrieval.query_log import QueryLogRecorder\n\n"
            "def go(home):\n    return QueryLogRecorder.for_home(home)\n"
        )
    }
    qname = "trove.services.retrieval.query_log.QueryLogRecorder"
    sites = constructor_sites({**REAL, **fake}, {qname})
    assert "trove/cli/rogue.py" in sites[qname]


def test_evidence_store_imports_have_one_direction():
    offenders = detect_evidence_imports(REAL)
    assert not offenders, "workflow/agent must reach evidence only via tracing: " + str(offenders)


def test_tracing_is_a_sink():
    offenders = detect_tracing_upstream_imports(REAL)
    assert not offenders, "tracing must not import upstream layers: " + str(offenders)


def test_import_detectors_catch_synthetic_violations():
    assert detect_evidence_imports({
        "trove/workflow/nodes/rogue.py": "from trove.storage.run_record_store import RunRecordStore\n"
    })
    assert detect_tracing_upstream_imports({
        "trove/tracing/rogue.py": "from trove.workflow.state import WorkflowState\n"
    })


def test_api_store_imports_are_frozen():
    arch.assert_frozen(API_STORE_IMPORTS, detect_api_store_imports(REAL), "API store imports")


def test_api_store_import_detector_resolves_reexports():
    fake = {
        "trove/api/routers/rogue.py": (
            "from trove.services.drift import DriftStore\n\n"
            "def get():\n    return DriftStore(project_root='.')\n"
        )
    }
    hits = detect_api_store_imports({**REAL, **fake})
    assert any(hit.startswith("trove/api/routers/rogue.py:") for hit in hits)


def test_api_workflow_imports_are_frozen():
    offenders = detect_api_workflow_imports(REAL, API_WORKFLOW_MODULES)
    assert not offenders, "api→workflow import outside the frozen list: " + str(offenders)


def test_api_workflow_import_detector_catches_a_new_module():
    offenders = detect_api_workflow_imports(
        {
            "trove/api/routers/rogue.py": "from trove.workflow.graphs import build_graphs\n",
            "trove/workflow/graphs.py": "",
        },
        API_WORKFLOW_MODULES,
    )
    assert offenders == ["trove/api/routers/rogue.py imports trove.workflow.graphs"]


def test_yaml_writers_are_frozen():
    actual = detect_yaml_writers(REAL)
    drift: list[str] = []
    for filename, expected in KB_YAML_WRITERS.items():
        extra, missing = arch.diff(expected, actual[filename])
        if extra or missing:
            drift.append(f"{filename}: new={extra} gone={missing}")
    assert not drift, "KB YAML writer map drifted:\n" + "\n".join(drift)


def test_yaml_detector_catches_a_new_writer():
    fake = {
        "trove/services/kb/service.py": (
            "def rogue(kb):\n"
            "    path = kb.semantics_path('demo')\n"
            "    path.write_text('x')\n"
        )
    }
    assert "trove/services/kb/service.py::rogue" in detect_yaml_writers(fake)[
        "semantics.yml"
    ]


def test_yaml_detector_accepts_a_reader():
    fake = {
        "trove/services/kb/service.py": (
            "def read_only(kb):\n"
            "    path = kb.semantics_path('demo')\n"
            "    return path.read_text()\n"
        )
    }
    assert detect_yaml_writers(fake)["semantics.yml"] == set()


def test_script_yaml_writes_carry_a_bypass_reason():
    script_sites = {
        site
        for sites in KB_YAML_WRITERS.values()
        for site in sites
        if site.startswith("scripts/")
    }
    arch.assert_frozen(BYPASSES, script_sites, "script YAML bypasses")
    empty = [site for site, reason in BYPASSES.items() if not reason.strip()]
    assert not empty, f"bypass without a reason: {empty}"


def test_rules_yml_has_no_machine_writer():
    assert KB_YAML_WRITERS["rules.yml"] == frozenset()
    assert detect_yaml_writers(REAL)["rules.yml"] == set()


def test_gensql_state_is_a_projection_of_workflow_state():
    actual = set(GenSQLState.model_fields) - set(WorkflowState.model_fields)
    arch.assert_frozen(GEN_DERIVED_FIELDS, actual, "GenSQLState-only fields")


def test_manifest_shape():
    ids = [artifact.id for artifact in ARTIFACTS]
    duplicates = sorted({artifact_id for artifact_id in ids if ids.count(artifact_id) > 1})
    assert not duplicates, f"duplicate artifact id: {duplicates}"

    bad_roots = [
        artifact.id
        for artifact in ARTIFACTS
        if not artifact.path.startswith(ROOTS)
    ]
    assert not bad_roots, f"artifact path without a declared root: {bad_roots}"

    bad_authority = [
        artifact.id for artifact in ARTIFACTS if artifact.authority not in AUTHORITIES
    ]
    assert not bad_authority, f"unknown authority: {bad_authority}"

    durable = {q for q, kind in STORE_CLASSES.items() if kind == "durable"}
    arch.assert_frozen(durable, CONSTRUCTORS, "durable classes with a constructor map")


@pytest.mark.parametrize("qname", sorted(CONSTRUCTORS), ids=lambda q: q.rsplit(".", 1)[-1])
def test_constructor_files_exist(qname):
    missing = [path for path in CONSTRUCTORS[qname] if path not in REAL]
    assert not missing, f"{qname}: constructor file disappeared: {missing}"
