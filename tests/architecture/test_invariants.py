"""P0 — executable invariant registry (architecture fitness functions).

Each entry is a claim this repository makes about itself. The claim is either

* **detected** — a pure function over source text that returns offenders, with
  a synthetic violation and a safe equivalent proving it is neither vacuous nor
  over-eager; or
* **evidenced** — a behavior-level red line whose proof is a named pytest node,
  checked to still exist (renaming the proof forces a manifest edit).

The registry is a ratchet: new code paths are red, deleting an invariant is
red, and deleting the test that proves one is red. It is not a sandbox —
dynamic imports, ``getattr`` and runtime-assembled paths have no AST shape.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Callable, Mapping

import pytest

from tests.helpers import arch

Sources = Mapping[str, str]
Detector = Callable[[Sources], list[str]]


# ── detectors ────────────────────────────────────────────────────────────
#
# All detectors take a source mapping and return human-readable offenders.
# Keeping them pure is what makes the synthetic self-tests possible.

_CORE_FORBIDDEN = (
    "trove.llm",
    "trove.services.datasource",
    "trove.services.jobs",
    "trove.services.kb",
    "httpx",
    "aiohttp",
    "socket",
)
# scan's hypothesis layer renders prompts (which imports the pure string
# scrubber ``trove.llm.untrusted``) but must never reach the gateway/loop.
_SCAN_FORBIDDEN = (
    "trove.llm.gateway",
    "trove.llm.agent_loop",
    "trove.services.datasource",
    "trove.services.jobs",
    "trove.services.kb",
    "httpx",
    "aiohttp",
    "socket",
)


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def detect_core_imports(sources: Sources) -> list[str]:
    scopes = {
        "trove/services/decision": _CORE_FORBIDDEN,
        "trove/services/analysis": _CORE_FORBIDDEN,
        "trove/services/scan": _SCAN_FORBIDDEN,
    }
    offenders: list[str] = []
    for rel, source in sources.items():
        for prefix, forbidden in scopes.items():
            if not arch.under(rel, prefix):
                continue
            for module in sorted(arch.resolve_imports(rel, ast.parse(source))):
                if any(_matches(module, bad) for bad in forbidden):
                    offenders.append(f"{rel}: imports {module}")
    return offenders


_HASH_SCOPES = (
    "trove/services/decision",
    "trove/services/analysis",
    "trove/services/scan",
)
_HASH_FILES = ("trove/eval/gate.py", "trove/eval/quality_report.py")


def detect_builtin_hash(sources: Sources) -> list[str]:
    offenders: list[str] = []
    for rel, source in sources.items():
        if not (any(arch.under(rel, p) for p in _HASH_SCOPES) or rel in _HASH_FILES):
            continue
        for node in ast.walk(ast.parse(source)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "hash"
            ):
                offenders.append(f"{rel}:{node.lineno} calls builtin hash()")
    return offenders


#: Pure modules: time is an input, never read from the wall clock.
_CLOCK_PREFIXES = {
    "trove/services/analysis": frozenset(),
    "trove/services/decision": frozenset({"service.py", "outcome.py", "drafts.py"}),
}
_CLOCK_FILES = (
    "trove/services/scan/scanner.py",
    "trove/services/scan/hypotheses.py",
    "trove/eval/gate.py",
    "trove/eval/quality_report.py",
)

#: Persisted-timestamp scope: aware clocks are allowed, naive ones are not.
#: ``trove/storage/`` is the persistence layer wholesale; the rest are the
#: modules migrated onto :mod:`trove.core.clock` in P3 plus the admin app (whose
#: retention cutoffs are compared against persisted rows). ``tests/`` is not
#: scanned — a test may freeze a local clock to prove the legacy read path.
_UTC_PREFIXES = ("trove/storage/",)
_UTC_FILES = (
    "trove/api/app.py",
    "trove/tracing/run_record.py",
    "trove/core/clock.py",
    "trove/mcp/server.py",
    "trove/services/action/guard.py",
    "trove/services/action/propose.py",
    "trove/services/action/service.py",
    "trove/services/action/store.py",
    "trove/services/decision/outcome.py",
    "trove/services/decision/service.py",
    "trove/services/decision/verdict_store.py",
    "trove/services/jobs/runner.py",
    "trove/services/scan/service.py",
    "trove/services/validate/service.py",
)


def _dotted(node: ast.AST, bound: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return bound.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value, bound)
        return f"{head}.{node.attr}" if head else ""
    return ""


def _is_aware(node: ast.Call) -> bool:
    """``now(tz)`` / ``now(tz=...)`` with a non-None argument is timezone-aware."""
    if node.args:
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value is None):
            return True
    for keyword in node.keywords:
        if keyword.arg == "tz" and not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is None
        ):
            return True
    return False


def clock_call(node: ast.AST, rel: str, bound: dict[str, str], *, allow_aware: bool) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        target = bound.get(func.id, func.id)
        if target == "time.time":
            return f"{rel}:{node.lineno} reads time.time()"
        return None
    if not isinstance(func, ast.Attribute):
        return None
    owner = _dotted(func.value, bound)
    attr = func.attr
    if owner in {"datetime", "datetime.datetime"} and attr == "utcnow":
        return f"{rel}:{node.lineno} reads naive datetime.utcnow()"
    if owner in {"datetime", "datetime.datetime"} and attr == "now":
        if allow_aware and _is_aware(node):
            return None
        return f"{rel}:{node.lineno} reads datetime.now() without a timezone"
    if owner in {"datetime.date", "date"} and attr == "today":
        return f"{rel}:{node.lineno} reads date.today()"
    if owner == "time" and attr == "time":
        return f"{rel}:{node.lineno} reads time.time()"
    return None


def _clock_in_tree(rel: str, tree: ast.Module, *, allow_aware: bool) -> list[str]:
    bound = arch.import_bindings(rel, tree)
    out = []
    for node in ast.walk(tree):
        hit = clock_call(node, rel, bound, allow_aware=allow_aware)
        if hit:
            out.append(hit)
    return out


def _clock_scopes(sources: Sources) -> list[tuple[str, ast.Module, str]]:
    scoped: list[tuple[str, ast.Module, str]] = []
    for rel, source in sources.items():
        for prefix, exceptions in _CLOCK_PREFIXES.items():
            if arch.under(rel, prefix) and rel.rsplit("/", 1)[-1] not in exceptions:
                scoped.append((rel, ast.parse(source), source))
        if rel in _CLOCK_FILES:
            scoped.append((rel, ast.parse(source), source))
    return scoped


def detect_wall_clock(sources: Sources) -> list[str]:
    offenders: list[str] = []
    for rel, tree, _ in _clock_scopes(sources):
        offenders += _clock_in_tree(rel, tree, allow_aware=False)
    return offenders


def detect_naive_timestamps(sources: Sources) -> list[str]:
    offenders: list[str] = []
    for rel, source in sources.items():
        if rel in _UTC_FILES or any(arch.under(rel, p) for p in _UTC_PREFIXES):
            offenders += _clock_in_tree(rel, ast.parse(source), allow_aware=True)
    return offenders


# ── registry ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Invariant:
    id: str
    statement: str
    detector: Detector | None = None
    synthetic_rel: str = ""
    synthetic: str = ""
    safe: str = ""
    evidence: tuple[str, ...] = ()


INVARIANTS: tuple[Invariant, ...] = (
    Invariant(
        id="cores-no-llm-or-io",
        statement=(
            "确定性内核（decision/analysis/scan）不得 import LLM 网关、数据源、"
            "调度或网络客户端；它们的 I/O 只经注入的 runner/clock/gateway。"
        ),
        detector=detect_core_imports,
        synthetic_rel="trove/services/decision/rules.py",
        synthetic="from trove.llm import gateway\n",
        safe="from ..semantic_layer import model\n",
    ),
    Invariant(
        id="no-builtin-hash",
        statement=(
            "确定性内核与评测报告不得使用内建 hash()：输出不能随 "
            "PYTHONHASHSEED 漂移，全部哈希走 hashlib.sha256。"
        ),
        detector=detect_builtin_hash,
        synthetic_rel="trove/services/decision/rules.py",
        synthetic="def key(value):\n    return hash(value)\n",
        safe="import hashlib\n\ndef key(value):\n    return hashlib.sha256(value).hexdigest()\n",
    ),
    Invariant(
        id="pure-modules-no-wall-clock",
        statement=(
            "纯函数模块的时间由调用方注入：analysis 全包、decision 的纯模块、"
            "scan 的 scanner/hypotheses、eval 的 gate/quality_report 不读墙钟。"
        ),
        detector=detect_wall_clock,
        synthetic_rel="trove/services/analysis/engine.py",
        synthetic="from datetime import datetime\n\ndef stamp():\n    return datetime.now()\n",
        safe="def stamp(now):\n    return now()\n",
    ),
    Invariant(
        id="persisted-timestamps-utc",
        statement=(
            "落盘时间戳只有一个钟：存储层、运行记录、decision/action/scan/"
            "validate/jobs/mcp 的写入与保留截止一律 aware UTC；本地时区会把"
            "保留窗口与「同一时刻」整体平移。旧 naive 行只经 "
            "trove.core.clock.parse_utc 读，不迁移。"
        ),
        detector=detect_naive_timestamps,
        synthetic_rel="trove/storage/run_record_store.py",
        synthetic="from datetime import datetime\n\ndef stamp():\n    return datetime.now()\n",
        safe=(
            "from datetime import datetime, timezone\n\n"
            "def stamp():\n    return datetime.now(timezone.utc).isoformat()\n"
        ),
    ),
    Invariant(
        id="masking-fail-closed",
        statement="脱敏失败清空 rows/columns：不把原文留给任何未来的下游节点。",
        evidence=(
            "tests/workflow/test_masking_node.py::TestFailClosed::"
            "test_a_refusal_also_clears_the_rows",
        ),
    ),
    Invariant(
        id="auto-drafts-pending-only",
        statement=(
            "扫描/拒绝扩展/历史蒸馏的自动产物只落 pending，确认门之前不进入检索或排名。"
        ),
        evidence=(
            "tests/services/scan/test_scan_service.py::TestDrafts::"
            "test_finding_lands_as_a_pending_draft_with_the_band_edge",
            "tests/workflow/test_refuse.py::TestUncoveredRefusal::"
            "test_refusal_drafts_metric_and_writes_pending",
            "tests/services/kb/test_history_distill.py::TestRunDistill::"
            "test_pending_only_gates_and_semantics_untouched",
        ),
    ),
    Invariant(
        id="failures-are-loud",
        statement=(
            "判不了/已停用不得静默按「未触发」通过：错误必须出现在运行结果上。"
        ),
        evidence=(
            "tests/services/decision/test_decision_service.py::TestFailLoud::"
            "test_unresolvable_window",
            "tests/services/decision/test_decision_service.py::TestOrgExtensionsSwitch::"
            "test_disabled_raises_and_evaluate_folds_to_error",
            "tests/services/analysis/test_engine.py::TestPeriodDegradation::"
            "test_missing_time_context_degrades_loudly",
        ),
    ),
    Invariant(
        id="refusal-state-hygiene",
        statement="拒绝轮清空投递字段，上一轮的结果/错误不得借拒绝卡泄漏。",
        evidence=(
            "tests/workflow/test_refuse.py::TestRefusalStateHygiene::"
            "test_plain_refusal_clears_delivery_fields",
        ),
    ),
    Invariant(
        id="one-measurement-one-outcome",
        statement=(
            "一次测量一个结局：测过（含 error 行）就不再是候选，绝不回填第二份测量。"
        ),
        evidence=(
            "tests/services/action/test_measure_due.py::TestMeasurement::"
            "test_one_shot_no_second_measurement",
            "tests/services/action/test_measure_due.py::TestLoudFailures::"
            "test_error_row_is_also_one_shot",
        ),
    ),
    Invariant(
        id="degradation-is-visible",
        statement=(
            "降级/判不了必须显式可见，不得折叠成成功或「无变化」；三态里的 None "
            "不得被写成 False/0，带降级标记的答复仍是 answered（降级是修饰）。"
        ),
        evidence=(
            "tests/workflow/nodes/test_output_honesty.py::TestDegradedAttributionVisible::"
            "test_degraded_attribution_explains_instead_of_bare_heading",
            "tests/services/analysis/test_effect.py::TestMeasureEffect::"
            "test_zero_scale_is_none_not_false",
            "tests/services/action/test_action_outcomes_store.py::TestRoundTrip::"
            "test_outside_band_three_state",
            "tests/services/decision/test_significance.py::TestGate::"
            "test_zero_scale_refuses_to_confirm",
        ),
    ),
    Invariant(
        id="ledger-never-invents-zero",
        statement=(
            "没测到只能说不知道：账本 token 缺失 → None + 覆盖码、usage 无数据 "
            "→ null、eval baseline 未记账的字段不计入分母；测到的 0 才是 0。"
        ),
        evidence=(
            "tests/core/test_run_ledger.py::TestCounts::"
            "test_no_events_means_none_not_zero",
            "tests/core/test_run_ledger.py::TestTokens::"
            "test_unmeasured_tokens_are_none_with_a_reason",
            "tests/core/test_run_ledger.py::TestTokens::"
            "test_measured_zero_tokens_stays_zero",
            "tests/api/test_admin_run_record.py::TestReadEndpoint::"
            "test_ledger_projects_lifecycle_and_cost",
            "tests/eval/test_gate.py::TestTokenDenominatorVisibility::"
            "test_avg_tokens_value_and_denominator_unchanged",
            "tests/eval/test_replay.py::TestScoreReplay::"
            "test_avg_tokens_coverage_count_exposed",
        ),
    ),
    Invariant(
        id="semantics-single-write-path",
        statement="语义文档只经白名单写者落盘；agent 不得自行直写主线。",
        evidence=(
            "tests/services/semantic_layer/test_semantics_write_posture.py::"
            "test_no_unwhitelisted_semantics_writers",
            "tests/services/semantic_layer/test_semantics_write_posture.py::"
            "test_checker_catches_a_synthetic_violation",
        ),
    ),
)

#: Adding or deleting an invariant is a deliberate manifest edit, both ways.
ID_SET = {
    "cores-no-llm-or-io",
    "no-builtin-hash",
    "pure-modules-no-wall-clock",
    "persisted-timestamps-utc",
    "masking-fail-closed",
    "auto-drafts-pending-only",
    "failures-are-loud",
    "refusal-state-hygiene",
    "one-measurement-one-outcome",
    "degradation-is-visible",
    "ledger-never-invents-zero",
    "semantics-single-write-path",
}

REAL = arch.py_sources()


# ── meta contracts ───────────────────────────────────────────────────────


class TestResolveImports:
    """Relative imports must resolve — the old posture checks only saw absolute ones."""

    def test_one_dot_stays_in_the_package(self):
        tree = ast.parse("from . import sibling\n")
        assert "trove.a.b.sibling" in arch.resolve_imports("trove/a/b/c.py", tree)

    def test_two_dots_go_up_one_package(self):
        tree = ast.parse("from ..llm import gateway\n")
        assert "trove.services.llm.gateway" in arch.resolve_imports(
            "trove/services/decision/rules.py", tree
        )

    def test_three_dots_reach_the_top_package(self):
        tree = ast.parse("from ...llm import gateway\n")
        assert "trove.llm.gateway" in arch.resolve_imports(
            "trove/services/decision/rules.py", tree
        )

    def test_relative_import_inside_init_uses_the_package_itself(self):
        tree = ast.parse("from . import sibling\n")
        assert "trove.a.sibling" in arch.resolve_imports("trove/a/__init__.py", tree)

    def test_aliased_imports_resolve_to_their_definition(self):
        source = (
            "from trove.storage.run_record_store import RunRecordStore as R\n"
            "def build():\n"
            "    return R('root')\n"
        )
        targets = arch.constructor_targets("trove/main.py", ast.parse(source))
        assert "trove.storage.run_record_store.RunRecordStore" in targets

    def test_attribute_constructor_resolves_through_the_alias(self):
        source = (
            "import trove.storage.run_record_store as rr\n"
            "def build():\n"
            "    return rr.RunRecordStore('root')\n"
        )
        targets = arch.constructor_targets("trove/main.py", ast.parse(source))
        assert "trove.storage.run_record_store.RunRecordStore" in targets


def test_node_exists_rejects_a_missing_evidence_name():
    """The evidence checker is a real lookup, not an always-true stub."""
    assert not arch.node_exists(
        "tests/architecture/test_invariants.py", None, "test_not_a_real_name"
    )
    assert arch.node_exists(
        "tests/architecture/test_invariants.py",
        None,
        "test_registry_covers_exactly_the_frozen_ids",
    )


def test_registry_covers_exactly_the_frozen_ids():
    arch.assert_frozen(ID_SET, [item.id for item in INVARIANTS], "invariant id set")


def test_every_invariant_has_a_detector_or_evidence():
    bare = [item.id for item in INVARIANTS if item.detector is None and not item.evidence]
    assert not bare, f"invariant without a detector or evidence: {bare}"


def test_detector_entries_carry_both_self_tests():
    missing = [
        item.id
        for item in INVARIANTS
        if item.detector is not None and not (item.synthetic and item.safe)
    ]
    assert not missing, f"detector without synthetic/safe self-test: {missing}"


@pytest.mark.parametrize(
    "item", [i for i in INVARIANTS if i.detector is not None], ids=lambda i: i.id
)
def test_detectors_pass_on_today(item):
    offenders = item.detector(REAL)
    assert not offenders, f"[{item.id}] {item.statement}\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "item", [i for i in INVARIANTS if i.detector is not None], ids=lambda i: i.id
)
def test_detectors_catch_their_synthetic_violation(item):
    offenders = item.detector({item.synthetic_rel: item.synthetic})
    assert offenders, f"[{item.id}] synthetic violation was not detected"


@pytest.mark.parametrize(
    "item", [i for i in INVARIANTS if i.detector is not None], ids=lambda i: i.id
)
def test_detectors_accept_the_safe_form(item):
    offenders = item.detector({item.synthetic_rel: item.safe})
    assert not offenders, f"[{item.id}] safe form was flagged: {offenders}"


@pytest.mark.parametrize(
    "item", [i for i in INVARIANTS if i.evidence], ids=lambda i: i.id
)
def test_evidence_nodes_exist(item):
    missing = [
        node_id
        for node_id in item.evidence
        if not arch.node_exists(*arch.parse_node_id(node_id))
    ]
    assert not missing, (
        f"[{item.id}] evidence disappeared (renamed test?): {missing}"
    )
