"""eval_bird slicing semantics: skip (offset) first, then limit.

`--start 5 --limit 5` must mean "skip the first 5, evaluate the next 5"
(limit + offset), not "take 5 then skip them" (0 questions).
"""

import json

import pytest

from scripts.eval_bird import (
    attribution_slices,
    classify_pred_error,
    extract_tables,
    record_result,
    slice_questions,
    _result_entry,
)
from trove.services.kb.service import resolve_kb_root


def _qs(n=10):
    return [{"q": i} for i in range(n)]


def test_limit_and_offset():
    """start 5 + limit 5 → 第 6~10 题。"""
    assert slice_questions(_qs(), limit=5, start=5) == [{"q": i} for i in range(5, 10)]


def test_offset_only():
    assert slice_questions(_qs(), start=5) == [{"q": i} for i in range(5, 10)]


def test_limit_only():
    assert slice_questions(_qs(), limit=3) == [{"q": 0}, {"q": 1}, {"q": 2}]


def test_no_bounds():
    assert slice_questions(_qs()) == _qs()


def test_offset_beyond_length():
    assert slice_questions(_qs(), start=99) == []


def test_limit_zero_means_all():
    assert slice_questions(_qs(), limit=0, start=5) == [{"q": i} for i in range(5, 10)]


class TestResolveKbRoot:
    """--kb-dir 解析:接受「含 <db_id>/ 子目录的 KB 根」或「扁平 YAML 目录」。"""

    def test_none_keeps_default(self):
        assert resolve_kb_root(None, "financial") is None

    def test_root_with_datasource_subdir_passes_through(self, tmp_path):
        root = tmp_path / "kb"
        (root / "financial").mkdir(parents=True)
        assert resolve_kb_root(str(root), "financial") == root

    def test_flat_dir_staged_under_datasource_name(self, tmp_path):
        flat = tmp_path / "flat"
        flat.mkdir()
        (flat / "examples.yml").write_text("examples: []\n", encoding="utf-8")

        staged = resolve_kb_root(str(flat), "financial")

        assert staged != flat
        assert (staged / "financial").is_dir()
        assert (staged / "financial" / "examples.yml").exists()

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(ValueError):
            resolve_kb_root(str(tmp_path / "nope"), "financial")

    def test_dir_without_yml_or_subdir_raises(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ValueError):
            resolve_kb_root(str(empty), "financial")


class TestExtractTables:
    """gold SQL → oracle 表提取:sqlglot 优先、正则兜底、保序去重。"""

    def test_sqlglot_joins_and_alias(self):
        sql = (
            "SELECT a.account_id, t.amount "
            "FROM account AS a JOIN trans t ON a.account_id = t.account_id"
        )
        assert extract_tables(sql) == ["account", "trans"]

    def test_union_subqueries_collected(self):
        sql = (
            "SELECT account_id FROM account "
            "UNION SELECT account_id FROM (SELECT account_id FROM loan) s"
        )
        tables = extract_tables(sql)
        assert set(tables) == {"account", "loan"}

    def test_regex_fallback_when_sqlglot_fails(self, monkeypatch):
        """sqlglot 解析异常 → 正则兜底(FROM/JOIN 表名)。"""
        import sqlglot

        def boom(*a, **k):
            raise RuntimeError("parse failed")

        monkeypatch.setattr(sqlglot, "parse", boom)
        sql = "select * from loan where status = 'A'"
        assert extract_tables(sql) == ["loan"]
    """逐题判定落盘 results.jsonl:verdict 分类 + JSONL 追加语义。"""

    def test_classify_generation_error(self):
        assert classify_pred_error("SQL generation failed after 3 attempts: bad syntax") == "GENERATION_ERROR"

    def test_classify_execution_error(self):
        assert classify_pred_error("execution failed: Unknown column 'foo'") == "EXECUTION_ERROR"

    def test_record_result_appends_jsonl(self, tmp_path):
        path = tmp_path / "results.jsonl"
        record_result({"question": "第一题", "verdict": "MATCH"}, path)
        record_result({"question": "第二题", "verdict": "MISMATCH"}, path)

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"question": "第一题", "verdict": "MATCH"}
        # 中文原样保留(非 \u 转义),供人直接阅读
        assert "第一题" in lines[0]
        assert json.loads(lines[1])["verdict"] == "MISMATCH"


class TestAttributionSlices:
    """P2-8 机制归因切片:逐题归因字段 + 按维度切 EX%。"""

    def _entry(self, verdict="MATCH", **attrs):
        e = {"run_id": "r1", "question": "q", "evidence": "", "gold_sql": "",
             "verdict": verdict}
        e.update(attrs)
        return e

    def test_result_entry_carries_attribution_fields(self):
        """final 存在时记录机制路径:consensus/confidence/selection/fix_mode/
        rollback_target/validation_hits/n_candidates。"""
        final = _DummyState()
        entry = _result_entry("r1", "q", "", "g", "MATCH", final)
        assert entry["consensus"] is True
        assert entry["confidence"] == 0.6
        assert entry["selection"] == {"votes": {"s1": 3}, "adopted": True}
        assert entry["fix_mode"] == "fixer"
        assert entry["rollback_target"] == "gen_sql"
        assert entry["validation_hits"] == [{"rule": "answer-columns"}]
        assert entry["n_candidates"] == 2

    def test_slices_rate_per_dimension_value(self):
        results = [
            self._entry("MATCH", consensus=True, confidence=0.8, fix_mode="",
                        rollback_target="", validation_hits=[],
                        n_candidates=5, evidence="hint"),
            self._entry("MATCH", consensus=True, confidence=0.8, fix_mode="",
                        rollback_target="", validation_hits=[],
                        n_candidates=5, evidence="hint"),
            self._entry("MISMATCH", consensus=False, confidence=0.25, fix_mode="",
                        rollback_target="gen_sql", validation_hits=[{"rule": "f3"}],
                        n_candidates=1, evidence=""),
        ]
        lines = attribution_slices(results)
        text = "\n".join(lines)
        assert "共识: True: 2/2 (100.0%) | False: 0/1 (0.0%)" in text
        assert "high (≥0.5): 2/2 (100.0%)" in text
        assert "回退目标: (无): 2/2 (100.0%) | gen_sql: 0/1 (0.0%)" in text
        assert "拦过: 0/1 (0.0%)" in text
        assert "multi (≥2): 2/2 (100.0%)" in text
        assert "with evidence: 2/2 (100.0%)" in text

    def test_oracle_and_scaling_slice_buckets(self):
        """oracle A/B 与缩放 A/B 的归因切片:从 results 直接切 EX%。"""
        results = [
            self._entry("MATCH", oracle=True, scaling=50),
            self._entry("MISMATCH", oracle=True, scaling=50),
            self._entry("MATCH", oracle=False, scaling=5),
        ]
        lines = attribution_slices(results)
        text = "\n".join(lines)
        assert "oracle: oracle: 1/2 (50.0%)" in text
        assert "no-oracle: 1/1 (100.0%)" in text
        assert "scaling: 50: 1/2 (50.0%)" in text

    def test_gold_error_and_crash_excluded_from_slices(self):
        results = [
            self._entry("MATCH", consensus=True, confidence=1.0),
            self._entry("GOLD_ERROR", consensus=True, confidence=1.0),
            self._entry("CRASH", consensus=True, confidence=1.0),
        ]
        lines = attribution_slices(results)
        text = "\n".join(lines)
        assert "True: 1/1 (100.0%)" in text  # 只有 MATCH 进分母

    def test_empty_results_yield_no_lines(self):
        assert attribution_slices([]) == []


class _DummyState:
    """_result_entry 的 final 形状(只用到归因字段,构造真实 WorkflowState
    需要 lang/session 等环境)。"""

    sql = "SELECT 1"
    kb_hits = []
    retry_count = 1
    consensus = True
    confidence = 0.6
    selection = {"votes": {"s1": 3}, "adopted": True}
    fix_mode = "fixer"
    rollback_target = "gen_sql"
    validation_hits = [{"rule": "answer-columns"}]
    candidates = ["SELECT 1", "SELECT 2"]
