"""eval_bird slicing semantics: skip (offset) first, then limit.

`--start 5 --limit 5` must mean "skip the first 5, evaluate the next 5"
(limit + offset), not "take 5 then skip them" (0 questions).
"""

import json

import pytest

from scripts.eval_bird import classify_pred_error, record_result, slice_questions
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


class TestResultRecording:
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
