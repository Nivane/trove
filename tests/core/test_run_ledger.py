"""运行账本（``trove/core/run_ledger.py``）：纯派生 + 没测到 ≠ 0。

这些用例钉的是账本的三条纪律，而不是某个字段的实现细节：

1. **闭形**：顶层键与覆盖码由机检冻结（另一处在
   ``tests/architecture/test_run_lifecycle_contracts.py``），这里确保同一份
   输入永远给出同一份输出，且不修改输入。
2. **没测到 ≠ 0**：没有事件 → 计数 ``None``；没有任何 token 事实 →
   ``None`` + ``tokens_unmeasured``；而**测到的零**（finish 报了 0 tokens）
   必须原样是 ``0``，不能被洗成「不知道」。
3. **不复制敏感载荷**：问题原文、SQL 原文、工具 observation 都不进账本。
"""

from __future__ import annotations

import copy
import json

from trove.core.run_ledger import (
    COVERAGE_CODES,
    LEDGER_VERSION,
    build_run_ledger,
)

FINISH_KEYS = {
    "version", "run_id", "status", "outcome", "started_at", "finished_at",
    "elapsed_ms", "llm", "tools", "sql", "cache", "verdict", "coverage",
}


def _record(*, status="done", finish=None, events=None, truncation=None,
            assembly=None):
    header = {
        "run_id": "r1",
        "status": status,
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
    }
    if finish is not None:
        header["finish"] = finish
    if truncation is not None:
        header["truncation"] = truncation
    if assembly is not None:
        header["assembly"] = assembly
    return {"header": header, "events": events or []}


def _llm(model="m1", usage=None, elapsed=12):
    payload = {"node": "gen_generate", "model": model, "elapsed_ms": elapsed}
    if usage is not None:
        payload["usage"] = usage
    return {"seq": 0, "kind": "llm", "payload": payload}


def _tool(name, *, error=None, elapsed=3):
    return {"seq": 0, "kind": "tool", "payload": {
        "name": name, "error": error, "elapsed_ms": elapsed,
        "arguments": {}, "observation": "SECRET-OBS",
    }}


class TestShape:
    def test_top_level_keys_are_the_frozen_shape(self):
        assert set(build_run_ledger(_record())) == FINISH_KEYS
        assert LEDGER_VERSION == 1

    def test_empty_record_does_not_raise(self):
        ledger = build_run_ledger({})
        assert ledger["status"] == ""
        assert ledger["outcome"] is None
        assert ledger["llm"]["calls"] is None
        assert ledger["tools"]["by_name"] is None
        assert ledger["sql"]["probes"] is None
        assert ledger["cache"]["answer_cached"] is None
        assert ledger["coverage"] == sorted(
            {"unknown_status", "no_events", "no_finish", "tokens_unmeasured"}
        )

    def test_coverage_only_uses_the_closed_vocabulary(self):
        ledger = build_run_ledger(_record(finish={"sql": "SELECT 1"}))
        assert set(ledger["coverage"]) <= set(COVERAGE_CODES)
        assert ledger["coverage"] == sorted(set(ledger["coverage"]))

    def test_deterministic_and_non_mutating(self):
        record = _record(
            finish={"sql": "SELECT 1", "total_elapsed_ms": 5},
            events=[_llm(usage={"prompt_tokens": 1, "total_tokens": 2}),
                    _tool("probe_query")],
        )
        before = copy.deepcopy(record)
        first = build_run_ledger(record)
        second = build_run_ledger(record)
        assert first == second
        assert record == before, "账本是读出来的视图,不得修改输入"

    def test_does_not_copy_question_sql_or_observations(self):
        record = _record(
            finish={"question": "SECRET-Q", "sql": "SECRET-SQL",
                    "rows_preview": [["SECRET-ROW"]]},
            events=[_tool("probe_query"),
                    _llm(usage={"prompt_tokens": 1, "total_tokens": 1})],
        )
        blob = json.dumps(build_run_ledger(record), ensure_ascii=False)
        assert "SECRET-Q" not in blob
        assert "SECRET-SQL" not in blob
        assert "SECRET-ROW" not in blob
        assert "SECRET-OBS" not in blob


class TestLifecycle:
    def test_running_is_unfinished_not_a_failure(self):
        ledger = build_run_ledger(_record(status="running"))
        assert ledger["outcome"] is None
        assert "unfinished" in ledger["coverage"]
        assert "no_finish" not in ledger["coverage"]

    def test_terminal_without_finish_says_no_finish(self):
        ledger = build_run_ledger(_record(status="error"))
        assert ledger["outcome"] == "failed"
        assert "no_finish" in ledger["coverage"]
        assert "unfinished" not in ledger["coverage"]

    def test_unknown_status_degrades_instead_of_raising(self):
        ledger = build_run_ledger(_record(
            status="weird",
            finish={"outcome": "answered"},
        ))
        assert ledger["status"] == "weird"
        assert ledger["outcome"] is None
        assert "unknown_status" in ledger["coverage"]

    def test_outcome_reuses_the_p2_classifier(self):
        ledger = build_run_ledger(_record(finish={"outcome": "refused"}))
        assert ledger["outcome"] == "refused"
        pending = build_run_ledger(_record(status="pending_hitl"))
        assert pending["outcome"] == "pending_approval"


class TestCounts:
    def test_no_events_means_none_not_zero(self):
        ledger = build_run_ledger(_record())
        assert ledger["llm"]["calls"] is None
        assert ledger["llm"]["errors"] is None
        assert ledger["tools"]["calls"] is None
        assert ledger["sql"]["probes"] is None
        assert ledger["sql"]["checks"] is None
        assert ledger["sql"]["validations"] is None

    def test_llm_and_tool_counts(self):
        events = [
            _llm(model="m1", usage={"prompt_tokens": 1, "total_tokens": 1}),
            _llm(model="m2", elapsed=20),
            {"seq": 0, "kind": "llm_error", "payload": {"model": "m2"}},
            _tool("probe_query"),
            _tool("check_result", error="bad sql"),
            _tool("validate_sql"),
        ]
        ledger = build_run_ledger(_record(events=events))
        assert ledger["llm"]["calls"] == 2
        assert ledger["llm"]["errors"] == 1
        assert ledger["llm"]["models"] == ["m1", "m2"]
        assert ledger["llm"]["elapsed_ms"] == 32
        assert ledger["tools"]["calls"] == 3
        assert ledger["tools"]["errors"] == 1
        assert ledger["tools"]["by_name"] == {
            "check_result": 1, "probe_query": 1, "validate_sql": 1,
        }
        assert ledger["sql"]["probes"] == 1
        assert ledger["sql"]["checks"] == 1
        assert ledger["sql"]["validations"] == 1

    def test_sql_counts_are_zero_when_events_exist_but_none_ran(self):
        ledger = build_run_ledger(_record(events=[_llm(usage={})]))
        assert ledger["sql"]["probes"] == 0
        assert ledger["sql"]["checks"] == 0
        assert ledger["sql"]["validations"] == 0


class TestTokens:
    def test_finish_token_usage_is_the_measurement(self):
        ledger = build_run_ledger(_record(
            finish={"token_usage": {
                "prompt": 10, "completion": 5, "total": 15,
                "cached_tokens": 4,
            }},
            events=[_llm(usage={"prompt_tokens": 999, "total_tokens": 999})],
        ))
        assert ledger["llm"]["tokens_source"] == "finish"
        assert ledger["llm"]["tokens"] == {
            "prompt": 10, "completion": 5, "total": 15, "cached": 4,
        }
        assert "tokens_from_events" not in ledger["coverage"]
        assert "tokens_unmeasured" not in ledger["coverage"]

    def test_measured_zero_tokens_stays_zero(self):
        ledger = build_run_ledger(_record(
            finish={"token_usage": {"prompt": 0, "completion": 0, "total": 0}},
        ))
        assert ledger["llm"]["tokens_source"] == "finish"
        assert ledger["llm"]["tokens"]["total"] == 0
        assert "tokens_unmeasured" not in ledger["coverage"]

    def test_falls_back_to_event_usage_with_a_coverage_note(self):
        events = [
            _llm(usage={"prompt_tokens": 3, "completion_tokens": 2,
                        "total_tokens": 5}),
            _llm(usage={"prompt_tokens": 7, "total_tokens": 9,
                        "cache_read_input_tokens": 1}),
        ]
        ledger = build_run_ledger(_record(events=events))
        assert ledger["llm"]["tokens_source"] == "events"
        assert ledger["llm"]["tokens"] == {
            "prompt": 10, "completion": 2, "total": 14, "cached": 1,
        }
        assert "tokens_from_events" in ledger["coverage"]

    def test_cached_tokens_prefers_one_spelling_never_sums(self):
        ledger = build_run_ledger(_record(
            finish={"token_usage": {
                "prompt": 1, "total": 1,
                "cached_tokens": 4, "cache_read_input_tokens": 9,
            }},
        ))
        assert ledger["llm"]["tokens"]["cached"] == 4

    def test_unmeasured_tokens_are_none_with_a_reason(self):
        ledger = build_run_ledger(_record(
            events=[_llm(usage=None), _tool("probe_query")],
        ))
        assert ledger["llm"]["tokens"] == {
            "prompt": None, "completion": None, "total": None, "cached": None,
        }
        assert ledger["llm"]["tokens_source"] is None
        assert "tokens_unmeasured" in ledger["coverage"]


class TestCoverage:
    def test_events_dropped_lowers_counts_but_payload_truncation_does_not(self):
        dropped = build_run_ledger(_record(
            events=[_llm(usage={})],
            truncation={"events_dropped": 2, "events_truncated": 5},
        ))
        assert "events_dropped" in dropped["coverage"]
        assert dropped["llm"]["calls"] == 1

        # events_truncated 只裁载荷,事件条数仍完整 —— 不该被当成下界。
        capped = build_run_ledger(_record(
            events=[_llm(usage={})],
            truncation={"events_truncated": 5},
        ))
        assert "events_dropped" not in capped["coverage"]

    def test_finish_truncation_is_reported(self):
        ledger = build_run_ledger(_record(
            finish={"sql": "SELECT 1"},
            truncation={"finish_truncated": 1},
        ))
        assert "finish_truncated" in ledger["coverage"]

    def test_context_not_recorded_when_assembly_is_absent(self):
        ledger = build_run_ledger(_record(finish={"sql": ""}))
        assert "context_not_recorded" in ledger["coverage"]

        assembled = build_run_ledger(_record(
            finish={"sql": ""}, assembly={"blocks": [{"name": "rules"}]},
        ))
        assert "context_not_recorded" not in assembled["coverage"]

    def test_budget_note_only_when_sql_actually_ran(self):
        ran = build_run_ledger(_record(finish={"sql": "SELECT 1"}))
        assert "budget_not_recorded" in ran["coverage"]
        assert "connector_cache_not_recorded" in ran["coverage"]

        refused = build_run_ledger(_record(finish={"sql": "", "refusal": {}}))
        assert "budget_not_recorded" not in refused["coverage"]
        assert "connector_cache_not_recorded" not in refused["coverage"]

    def test_execution_projection_keeps_facts_not_config(self):
        ledger = build_run_ledger(_record(finish={
            "sql": "SELECT 1",
            "execution_evidence": {
                "verdict": "OK", "reason": "fine", "degraded": False,
                "estimated_rows": 10, "estimated_bytes": 2048,
                "limit_applied": 100, "terminated": None, "kill": None,
                "source": "explain", "budget": {"code": "row_cap"},
                "as_of_basis": "none",
            },
        }))
        assert ledger["sql"]["execution"] == {
            "verdict": "OK", "reason": "fine", "degraded": False,
            "estimated_rows": 10, "estimated_bytes": 2048,
            "limit_applied": 100, "terminated": None, "kill": None,
        }
        assert "budget_not_recorded" not in ledger["coverage"]


class TestAnswerCache:
    def test_cached_fact_is_tri_state(self):
        assert build_run_ledger(_record(
            finish={"cached": True}))["cache"]["answer_cached"] is True
        assert build_run_ledger(_record(
            finish={"cached": False}))["cache"]["answer_cached"] is False
        assert build_run_ledger(_record())["cache"]["answer_cached"] is None


class TestReadSideTolerance:
    def test_garbled_header_and_events_do_not_raise(self):
        ledger = build_run_ledger({
            "header": {"status": "done", "truncation": "nope",
                       "finish": "nope"},
            "events": "nope",
        })
        assert ledger["llm"]["calls"] is None
        assert "no_events" in ledger["coverage"]
        assert "no_finish" in ledger["coverage"]

    def test_non_numeric_estimates_are_unknown_not_zero(self):
        ledger = build_run_ledger(_record(finish={
            "row_count": "many",
            "retry_count": None,
            "total_elapsed_ms": "fast",
        }))
        assert ledger["sql"]["row_count"] is None
        assert ledger["sql"]["retry_count"] is None
        assert ledger["elapsed_ms"] is None
