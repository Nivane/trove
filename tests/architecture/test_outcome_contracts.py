"""P2 — run-outcome contracts (one vocabulary, every exit classified).

一次运行「怎么结束」只有一个权威:``trove/core/run_outcome.py`` 的六值闭集。
这里把权威本身和它的接线钉成机检:

* 词汇冻结 —— 增删结局都要改清单;
* 运行记录的生命周期状态必须逐条在映射里表态(新状态 = 红);
* ``final_response`` 的写者集合冻结 —— 新出口必须先登记,再想清楚怎么归类;
* 分类器是纯模块 —— 零 ``trove.*`` import / 零墙钟 / 零 I/O;
* 接线点的调用次数冻结 —— 删一处 = 某个出口静默失去结局;
* finish 投影里结局不可裁剪(header.status 对拒绝/澄清/答复都是 ``done``);
* 前端类型与后端闭集逐字一致 —— 跨语言漂移不会自己暴露。

与覆盖率轴(``trove/services/question_coverage/taxonomy.py`` 的 10 结局)无关:
那条轴描述批量回放的编译/执行覆盖程度,不进这里。
"""
from __future__ import annotations

import ast
import re

import pytest

from tests.helpers import arch

from trove.core.run_outcome import RECORD_STATUS_OUTCOME, RUN_OUTCOMES
from trove.workflow.state import WorkflowState

TYPES_TS = "frontend/src/api/types.ts"

#: 唯一允许写 ``final_response`` 的源文件。新增出口 = 改这份清单,
#: 并在同一次评审里回答「这一轮怎么归类」。
FINAL_RESPONSE_WRITERS = {
    "trove/agent/session.py": 1,
    "trove/workflow/nodes/output.py": 4,
}

#: ``classify_run`` 的接线点:文件 → 函数 → 调用次数。
CLASSIFIER_WIRING = {
    "trove/agent/session.py": {
        "_stream_graph_run": 2,   # 批量子任务 pending summary + 协调器错误 summary
        "_state_summary": 1,      # SSE done / 历史回放 / run record finish 共用
        "_maybe_cache_exchange": 1,  # 缓存写门 = 结局门(只收 answered)
    },
    "trove/tracing/run_record.py": {"_summary_from_final": 1},
    "trove/mcp/server.py": {"ask_data": 1},
}


# ── detectors ────────────────────────────────────────────────────────────


def _is_final_response(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == "final_response"


def final_response_writes(source: str) -> list[int]:
    """``return {"final_response": …}`` 与 ``summary["final_response"] = …``。

    只认这两种「显式写出口」的形状:读访问(``final.final_response``)、
    元组常量(``_FINISH_KEEP``)与函数参数不算写。盲区是动态组装
    (``**payload`` / ``setattr``),与 P0 的其余探测器同款。
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            if any(_is_final_response(k) for k in node.value.keys):
                found.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript) and _is_final_response(
                    target.slice
                ):
                    found.append(node.lineno)
    return sorted(found)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def classifier_calls(source: str) -> dict[str, int]:
    """Count ``classify_run(...)`` calls per enclosing function (nested included)."""
    counts: dict[str, int] = {}

    def walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = enclosing
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call) and _call_name(child) == "classify_run":
                counts[name] = counts.get(name, 0) + 1
            walk(child, name)

    walk(ast.parse(source), "<module>")
    return counts


def module_imports(source: str, rel: str = "trove/core/run_outcome.py") -> set[str]:
    return arch.resolve_imports(rel, ast.parse(source))


def status_constants(source: str) -> set[str]:
    values: set[str] = set()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id.startswith("STATUS_")
            for t in node.targets
        ):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            values.add(node.value.value)
    return values


def literal_tuple(source: str, name: str) -> tuple[str, ...]:
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, (ast.Tuple, ast.List))
        ):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found as a literal tuple")


# ── vocabulary ───────────────────────────────────────────────────────────


class TestVocabulary:
    def test_the_six_outcomes_are_frozen_both_ways(self):
        arch.assert_frozen(
            {"answered", "refused", "clarified", "pending_approval",
             "cancelled", "failed"},
            list(RUN_OUTCOMES),
            "run outcome vocabulary",
        )

    def test_degradation_is_not_an_outcome(self):
        # 降级是修饰:带降级标记的答复仍是 answered,标记留在各自载荷里。
        assert "degraded" not in RUN_OUTCOMES

    def test_lifecycle_statuses_must_take_a_position(self):
        source = arch.py_sources("trove/tracing/run_record.py")[
            "trove/tracing/run_record.py"
        ]
        arch.assert_frozen(
            status_constants(source),
            list(RECORD_STATUS_OUTCOME),
            "run record status → outcome mapping",
        )

    def test_mapped_values_stay_inside_the_closed_set(self):
        allowed = set(RUN_OUTCOMES) | {None}
        assert set(RECORD_STATUS_OUTCOME.values()) <= allowed

    def test_workflow_state_carries_inputs_not_the_label(self):
        # 结局是派生物:state 里有四个信号,没有 outcome 字段。
        # 加字段会让两条真相(信号与标签)有可能打架。
        fields = WorkflowState.model_fields
        assert "outcome" not in fields
        for name in ("error", "refusal", "clarification_question", "hitl_status"):
            assert name in fields, f"classifier input {name!r} disappeared"


# ── finish projection ────────────────────────────────────────────────────


class TestFinishProjection:
    def _source(self) -> str:
        return arch.py_sources("trove/tracing/run_record.py")[
            "trove/tracing/run_record.py"
        ]

    def test_outcome_is_kept_and_never_dropped_under_size_pressure(self):
        keep = literal_tuple(self._source(), "_FINISH_KEEP")
        drop = literal_tuple(self._source(), "_FINISH_DROP_ORDER")
        assert "outcome" in keep
        assert "outcome" not in drop

    def test_record_projection_exposes_the_classified_outcome(self):
        # 管理端读记录时不必自己解析 finish_json(done 的三分别只在里面)。
        source = arch.py_sources("trove/api/routers/admin.py")[
            "trove/api/routers/admin.py"
        ]
        assert "record_outcome" in source
        calls = {
            _call_name(n)
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Call)
        }
        assert "record_outcome" in calls


# ── funnel ratchet ───────────────────────────────────────────────────────


class TestFinalResponseFunnel:
    def test_only_the_frozen_writers_touch_final_response(self):
        found = {
            rel: len(final_response_writes(source))
            for rel, source in arch.py_sources("trove", "scripts").items()
            if final_response_writes(source)
        }
        arch.assert_frozen(FINAL_RESPONSE_WRITERS, found, "final_response writers")

    def test_detector_catches_a_new_returning_exit(self):
        source = (
            'async def terminal(state):\n'
            '    return {"final_response": "custom"}\n'
        )
        assert final_response_writes(source) == [2]

    def test_detector_catches_a_summary_assignment(self):
        source = 'def build(summary):\n    summary["final_response"] = "x"\n'
        assert final_response_writes(source) == [2]

    def test_detector_ignores_reads_and_constants(self):
        source = (
            "FINISH_KEEP = ('final_response',)\n"
            "def render(final):\n"
            "    content = final.final_response\n"
            '    return {"answer": content}\n'
        )
        assert final_response_writes(source) == []


# ── wiring ───────────────────────────────────────────────────────────────


class TestClassifierWiring:
    @pytest.mark.parametrize(
        ("rel", "function", "expected"),
        [
            (rel, function, count)
            for rel, per_function in CLASSIFIER_WIRING.items()
            for function, count in per_function.items()
        ],
    )
    def test_each_exit_classifies(self, rel, function, expected):
        source = arch.py_sources(rel)[rel]
        assert classifier_calls(source).get(function, 0) == expected, (
            f"{rel}:{function} must call classify_run {expected}× — "
            "删一处会让一个出口静默失去结局"
        )

    def test_wiring_detector_counts_nested_functions(self):
        source = (
            "def outer():\n"
            "    def inner():\n"
            "        return classify_run(error='x')\n"
            "    return classify_run()\n"
        )
        assert classifier_calls(source) == {"inner": 1, "outer": 1}

    def test_cli_print_payload_carries_the_outcome(self):
        # --print 是一条对外出口:结局留在 summary 里但被白名单挡掉,
        # 机器消费方就又一次分不清「答了」与「拒了」。
        source = arch.py_sources("trove/main.py")["trove/main.py"]
        function = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef)
            and node.name == "format_print_payload"
        )
        keys = {
            key.value
            for node in ast.walk(function)
            if isinstance(node, ast.Dict)
            for key in node.keys
            if isinstance(key, ast.Constant)
        }
        assert "outcome" in keys


# ── core purity ──────────────────────────────────────────────────────────


class TestCorePurity:
    def _source(self) -> str:
        return arch.py_sources("trove/core/run_outcome.py")[
            "trove/core/run_outcome.py"
        ]

    def test_no_trove_imports_no_io_no_clock(self):
        # tracing / MCP 直接用它,所以它不能顺着 import 把图连回去
        # (P1 的导入方向纪律),也不能在被调用时读墙钟或落盘。
        source = self._source()
        imports = module_imports(source)
        assert not [m for m in imports if m == "trove" or m.startswith("trove.")]
        tree = ast.parse(source)
        called = {
            _call_name(n)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert not called & {"open", "now", "utcnow", "time", "today"}
        assert "datetime" not in imports and "time" not in imports

    def test_classifier_returns_only_closed_values(self):
        from trove.core.run_outcome import classify_run

        for error in ("", "boom"):
            for refusal in (None, {"reason": "no_model"}):
                for clarification in ("", "which year?"):
                    for hitl in ("", "pending", "approved", "rejected"):
                        assert classify_run(
                            error=error,
                            refusal=refusal,
                            clarification_question=clarification,
                            hitl_status=hitl,
                        ) in RUN_OUTCOMES


# ── frontend mirror ──────────────────────────────────────────────────────


def _ts_block(source: str, header: str) -> str:
    start = source.index(header)
    end = source.index("\n}\n", start) if header.startswith("export interface") else start
    return source[start:end]


class TestFrontendMirror:
    def _source(self) -> str:
        return (arch.ROOT / TYPES_TS).read_text(encoding="utf-8")

    def test_run_outcome_union_matches_the_backend(self):
        source = self._source()
        block = source[source.index("export type RunOutcome ="):]
        block = block[: block.index("\n\n")]
        values = re.findall(r"'([a-z_]+)'", block)
        arch.assert_frozen(set(RUN_OUTCOMES), values, "frontend RunOutcome union")

    def test_done_summary_carries_an_optional_outcome(self):
        source = self._source()
        block = _ts_block(source, "export interface DoneSummary {")
        assert "outcome?: RunOutcome" in block, (
            "DoneSummary.outcome 必须可选:缺席 = 旧后端/旧记录,"
            "不要按 'answered' 兜底"
        )
