"""P3 — run lifecycle + cost contracts（生命周期与代价的机检面）。

三条契约在这里冻结：

* **账本形状** —— ``trove/core/run_ledger.py`` 的顶层键、覆盖码闭集与
  版本号；账本必须保持纯（零 I/O / 零墙钟 / 只依赖 ``run_outcome``）。
  账本是审计面，形状漂移不能靠读代码发现。
* **生命周期漏斗** —— 状态与 ``finished_at`` 只经 ``_transition`` 写；
  store 的 upsert 在写之前校验闭集。任何新的 capture 直写都会红。
* **记录接线清单** —— ``configure_run_records`` 的调用点冻结为「已接」；
  刻意不接的入口逐条登记理由，并断言函数仍存在 —— 改名/删除会让清单
  腐烂报警，而不是永远绿。

状态词汇注册表（``STATUS_TRACKS``）只登记九条轨道，绝不合并/重命名：
各域的状态机是各自的域内事实（jobs 的 ``alert`` 不是运行结局的降级）。
"""

from __future__ import annotations

import ast
import importlib

import pytest

from tests.helpers import arch

from trove.core import run_ledger

# ── 账本词汇（冻结） ─────────────────────────────────────────────────────

LEDGER_TOP_LEVEL_KEYS = {
    "version", "run_id", "status", "outcome", "started_at", "finished_at",
    "elapsed_ms", "llm", "tools", "sql", "cache", "verdict", "coverage",
}

LEDGER_COVERAGE_CODES = (
    "no_events",
    "events_dropped",
    "finish_truncated",
    "unfinished",
    "no_finish",
    "unknown_status",
    "tokens_unmeasured",
    "tokens_from_events",
    "budget_not_recorded",
    "context_not_recorded",
    "connector_cache_not_recorded",
)

#: 账本允许 import 的模块前缀（含 ``from … import name`` 解析出的绑定名）。
LEDGER_ALLOWED_IMPORTS = ("__future__", "typing", "trove.core.run_outcome")

# ── 记录接线（冻结） ─────────────────────────────────────────────────────

#: 已接运行记录的调用点：文件 → ``configure_run_records`` 调用数。
#: ``main.py`` 两处是同一接线点的开/关分支（store 或 None）；新增接线
#: 必须先登记，并回答「这一条为什么需要记录」。
WIRED_RECORDER_SITES = {
    "trove/main.py": 2,
    "scripts/offline_eval.py": 1,
}

#: 刻意不接运行记录的入口 + 理由。每条都断言函数仍存在 —— 入口改名/删除
#: 时这里红，提醒复核「新的入口该不该接」，而不是让清单留着一串死名字。
DELIBERATELY_NOT_WIRED: tuple[tuple[str, str | None, str, str], ...] = (
    (
        "trove/eval/graph_runner.py", None, "run_question",
        "离线评测图执行：每问的成本本就落 eval 产物（results/replay），"
        "运行记录会再抄一份评测成本，且评测进程不跑服务端存储。",
    ),
    (
        "trove/eval/evalset_run.py", None, "run_evalset",
        "evalset 试跑入口：产物是 results/replay 文件并自带 token 记账；"
        "运行记录不是它的账本。",
    ),
    (
        "scripts/eval_bird.py", None, "main",
        "成本敏感的评测主跑：一次性 CLI 进程，不接服务端记录面。",
    ),
    (
        "scripts/lang_ab.py", None, "main",
        "语言 A/B 观察实验：一次性脚本，产物直接打印/写文件。",
    ),
    (
        "trove/services/jobs/runner.py", "SchedulerRunner", "_run_decision",
        "定时判定走 DecisionService：判定本身就是记录（verdict + "
        "rule_rev），不进 NL 管线也就不进运行记录。",
    ),
    (
        "trove/services/jobs/runner.py", "SchedulerRunner", "_run_scan",
        "定时扫描同理：产物是 scan 报告 + pending 草稿。",
    ),
)

# ── 状态词汇注册表（只登记，不合并） ─────────────────────────────────────

#: 轨道 → (模块, 常量, 冻结值)。新增/删除任何轨道的状态值都必须改这里。
STATUS_TRACKS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "run record 状态": (
        "trove.tracing.run_record", "RUN_RECORD_STATUSES",
        ("running", "pending_hitl", "done", "failed", "error", "cancelled"),
    ),
    "run outcome": (
        "trove.core.run_outcome", "RUN_OUTCOMES",
        ("answered", "refused", "clarified", "pending_approval",
         "cancelled", "failed"),
    ),
    "jobs run": (
        "trove.services.jobs.store", "RUN_STATUSES",
        ("running", "ok", "error", "alert"),
    ),
    "jobs delivery": (
        "trove.services.jobs.store", "DELIVERY_STATUSES",
        ("sent", "failed"),
    ),
    "task": (
        "trove.core.types", "TASK_STATUSES",
        ("pending", "in_progress", "done", "failed", "skipped"),
    ),
    "action proposal": (
        "trove.services.action.models", "PROPOSAL_STATUSES",
        ("pending", "approved", "rejected", "expired", "cancelled",
         "dispatched", "delivered", "failed"),
    ),
    "coverage run": (
        "trove.services.question_coverage.store", "RUN_STATUSES",
        ("queued", "running", "done", "error", "cancelled", "interrupted"),
    ),
    "evalset item": (
        "trove.services.evalset.models", "STATUSES",
        ("pending", "confirmed", "rejected", "retired"),
    ),
    "verdict": (
        "trove.services.decision.verdicts", "VERDICT_STATUSES",
        ("ok", "alert", "error"),
    ),
}

# ── detectors ────────────────────────────────────────────────────────────


def _header_field_write(node: ast.AST, field: str) -> bool:
    """``<expr>.header[field] = …``（读访问与参数不算写）。"""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == field
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "header"
    )


def header_field_writes(source: str, field: str) -> list[tuple[str, int]]:
    """``(enclosing function, lineno)`` for every ``*.header[field] = …``."""
    found: list[tuple[str, int]] = []

    def walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = enclosing
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Assign):
                if any(_header_field_write(t, field) for t in child.targets):
                    found.append((name, child.lineno))
            elif isinstance(child, ast.AnnAssign):
                if _header_field_write(child.target, field):
                    found.append((name, child.lineno))
            walk(child, name)

    walk(ast.parse(source), "<module>")
    return sorted(found, key=lambda item: item[1])


def _buffer_initial_header(source: str) -> dict[str, ast.AST]:
    """``_Buffer.__init__`` 里 ``self.header = {…}`` 字面量的键 → 值节点。"""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef) or node.name != "_Buffer":
            continue
        for func in node.body:
            if not isinstance(func, ast.FunctionDef) or func.name != "__init__":
                continue
            for stmt in ast.walk(func):
                if isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                elif isinstance(stmt, ast.AnnAssign):
                    targets = [stmt.target]
                else:
                    continue
                if not isinstance(stmt.value, ast.Dict):
                    continue
                if not any(
                    isinstance(t, ast.Attribute) and t.attr == "header"
                    for t in targets
                ):
                    continue
                return {
                    k.value: v for k, v in zip(stmt.value.keys, stmt.value.values)
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    return {}


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def recorder_wiring_calls(sources: arch.Sources) -> dict[str, int]:
    """``configure_run_records(...)`` call counts per file."""
    counts: dict[str, int] = {}
    for rel, source in sources.items():
        n = sum(
            1 for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and _call_name(node) == "configure_run_records"
        )
        if n:
            counts[rel] = n
    return counts


def _write_method(tree: ast.Module) -> ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "write"
            and node.args.args
            and getattr(node.args.args[0], "arg", "") == "self"
        ):
            return node
    return None


def _guards_closed_set(method: ast.AST) -> bool:
    """``if status not in RECORD_STATUSES: raise ValueError`` inside ``write``."""
    for node in ast.walk(method):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if not (isinstance(test.left, ast.Name) and test.left.id == "status"):
            continue
        if not any(isinstance(op, ast.NotIn) for op in test.ops):
            continue
        if not any(
            isinstance(c, ast.Name) and c.id == "RECORD_STATUSES"
            for c in test.comparators
        ):
            continue
        if any(
            isinstance(stmt, ast.Raise)
            and isinstance(stmt.exc, ast.Call)
            and isinstance(stmt.exc.func, ast.Name)
            and stmt.exc.func.id == "ValueError"
            for stmt in node.body
        ):
            return True
    return False


def _module_imports(rel: str, source: str) -> set[str]:
    return arch.resolve_imports(rel, ast.parse(source))


def _wall_clock_calls(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"now", "utcnow", "today"}:
            names.append(func.attr)
        elif isinstance(func, ast.Name) and func.id in {"open", "print", "input"}:
            names.append(func.id)
    return names


# ── 账本形状与纯净性 ─────────────────────────────────────────────────────


class TestLedgerVocabulary:
    def test_top_level_keys_are_frozen(self):
        built = run_ledger.build_run_ledger({})
        assert set(built) == LEDGER_TOP_LEVEL_KEYS
        assert run_ledger.LEDGER_VERSION == 1

    def test_coverage_codes_are_frozen(self):
        assert tuple(run_ledger.COVERAGE_CODES) == LEDGER_COVERAGE_CODES

    def test_ledger_is_pure(self):
        """零 I/O、零墙钟、只依赖 ``run_outcome``（P3 的纯净性本体）。"""
        rel = "trove/core/run_ledger.py"
        source = arch.py_sources(rel)[rel]
        tree = ast.parse(source)
        for module in _module_imports(rel, source):
            assert module.startswith(LEDGER_ALLOWED_IMPORTS), (
                f"run_ledger 只允许 import {LEDGER_ALLOWED_IMPORTS},看到 {module!r}"
            )
        assert _wall_clock_calls(tree) == [], (
            "账本不得读墙钟/写文件/打印:同一份输入必须永远给同一份输出"
        )

    def test_ledger_import_scan_catches_a_synthetic_violation(self):
        synthetic = (
            "import json\n"
            "from trove.llm import gateway\n"
            "from trove.core.run_outcome import record_outcome\n"
        )
        imports = _module_imports("trove/core/run_ledger.py", synthetic)
        offenders = {
            m for m in imports if not m.startswith(LEDGER_ALLOWED_IMPORTS)
        }
        assert {"json", "trove.llm.gateway"} <= offenders
        assert "trove.core.run_outcome.record_outcome" not in offenders


# ── 生命周期漏斗 ─────────────────────────────────────────────────────────


class TestLifecycleFunnel:
    def test_only_the_transition_writes_status_and_finished_at(self):
        rel = "trove/tracing/run_record.py"
        source = arch.py_sources(rel)[rel]
        for field in ("status", "finished_at"):
            writers = {name for name, _ in header_field_writes(source, field)}
            assert writers == {"_transition"}, (
                f"{field} 的写者只能是 _transition,"
                f"现在还有 {sorted(writers - {'_transition'})}"
            )

    def test_the_initial_state_is_the_running_literal(self):
        """``_Buffer.__init__`` 的初值是字典字面量，不是一次转移。"""
        rel = "trove/tracing/run_record.py"
        source = arch.py_sources(rel)[rel]
        initial = _buffer_initial_header(source)
        assert isinstance(initial.get("status"), ast.Name)
        assert initial["status"].id == "STATUS_RUNNING"
        assert isinstance(initial.get("finished_at"), ast.Constant)
        assert initial["finished_at"].value is None

    def test_write_site_detector_catches_a_synthetic_violation(self):
        synthetic = (
            "class _Buffer:\n"
            "    def capture_finish(self, buf):\n"
            "        buf.header['status'] = 'done'\n"
            "        buf.header['finished_at'] = 'now'\n"
        )
        assert header_field_writes(synthetic, "status") == [
            ("capture_finish", 3)]
        assert header_field_writes(synthetic, "finished_at") == [
            ("capture_finish", 4)]

    def test_store_rejects_unknown_status_before_writing(self):
        rel = "trove/storage/run_record_store.py"
        source = arch.py_sources(rel)[rel]
        tree = ast.parse(source)
        assert "trove.core.run_outcome.RECORD_STATUSES" in _module_imports(
            rel, source), "持久化侧的闭集必须与 run_outcome 同源"
        method = _write_method(tree)
        assert method is not None, "RunRecordStore.write 是持久化入口"
        assert _guards_closed_set(method), (
            "write() 必须在写之前拒绝未知 status"
        )

    def test_closed_set_guard_detector_catches_a_synthetic_violation(self):
        method = ast.parse(
            "async def write(self, header, events):\n"
            "    status = header['status']\n"
            "    if status not in RECORD_STATUSES:\n"
            "        raise ValueError(status)\n"
        ).body[0]
        assert _guards_closed_set(method)
        unguarded = ast.parse(
            "async def write(self, header, events):\n"
            "    status = header['status']\n"
        ).body[0]
        assert not _guards_closed_set(unguarded)


class TestStatusVocabulary:
    def test_record_statuses_and_the_outcome_map_are_one_set(self):
        from trove.core.run_outcome import RECORD_STATUS_OUTCOME, RECORD_STATUSES
        from trove.tracing.run_record import RUN_RECORD_STATUSES

        assert set(RECORD_STATUSES) == set(RECORD_STATUS_OUTCOME)
        assert set(RUN_RECORD_STATUSES) == set(RECORD_STATUSES)

    @pytest.mark.parametrize("track", sorted(STATUS_TRACKS))
    def test_each_track_keeps_its_frozen_values(self, track):
        module_name, constant, frozen = STATUS_TRACKS[track]
        module = importlib.import_module(module_name)
        actual = tuple(getattr(module, constant))
        assert actual == frozen, (
            f"{track}（{module_name}.{constant}）状态值漂移: {actual!r}"
        )

    def test_statement_status_constants_match_the_registry(self):
        import trove.tracing.run_record as record

        declared = {
            value for name, value in vars(record).items()
            if name.startswith("STATUS_") and isinstance(value, str)
        }
        assert declared == set(record.RUN_RECORD_STATUSES)


# ── 记录接线清单 ─────────────────────────────────────────────────────────


class TestRecorderWiring:
    def test_configure_run_records_call_sites_are_frozen(self):
        sources = arch.py_sources()
        assert recorder_wiring_calls(sources) == WIRED_RECORDER_SITES

    def test_wiring_detector_catches_a_synthetic_violation(self):
        synthetic = {
            "trove/new_entry.py": "configure_run_records(store, enabled=True)\n",
        }
        assert recorder_wiring_calls(synthetic) == {"trove/new_entry.py": 1}

    @pytest.mark.parametrize(
        "rel,class_name,func,reason",
        DELIBERATELY_NOT_WIRED,
        ids=[f"{r.rsplit('/', 1)[-1]}::{f}" for r, _, f, _ in DELIBERATELY_NOT_WIRED],
    )
    def test_each_deliberately_not_wired_entry_still_exists(
            self, rel, class_name, func, reason):
        assert reason.strip(), "每条不接的入口都要有理由"
        assert arch.node_exists(rel, class_name, func), (
            f"{rel}::{func} 已不存在 —— 接线清单需要复核:这个入口该不该接运行记录?"
        )

    def test_not_wired_entries_do_not_call_the_recorder(self):
        wired_files = set(WIRED_RECORDER_SITES)
        leftovers = {
            rel for rel, _, _, _ in DELIBERATELY_NOT_WIRED if rel in wired_files
        }
        assert leftovers == set()

    def test_run_record_store_construction_stays_in_the_p1_manifest(self):
        """P1 已冻结构造点；本批的接线清单是它的消费面，不是替代品。"""
        from tests.architecture.test_state_ownership import CONSTRUCTORS

        key = "trove.storage.run_record_store.RunRecordStore"
        assert key in CONSTRUCTORS
        assert CONSTRUCTORS[key], "构造点清单不能为空——空集合不构成冻结"
