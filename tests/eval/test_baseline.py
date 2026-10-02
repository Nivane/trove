"""可复现基线产物测试:问题集构建 / qid 对账 / 覆盖检查 / 完整性(纯函数,零 IO)。"""

from __future__ import annotations

import json

import pytest

from trove.eval.baseline import (
    build_questions,
    check_integrity,
    coverage_check,
    field_coverage,
    load_jsonl,
    match_qid,
    qid_for,
)

DEV_ROWS = [
    {"question_id": 1, "db_id": "financial", "question": "How many accounts in East Bohemia?",
     "evidence": "A3 = region", "SQL": "SELECT COUNT(*) FROM account"},
    {"question_id": 2, "db_id": "financial", "question": "List districts by salary?",
     "evidence": "", "SQL": "SELECT * FROM district"},
    {"question_id": 3, "db_id": "other", "question": "ignored", "SQL": ""},
]


def _questions():
    return build_questions(DEV_ROWS, "financial")


def test_qid_for_stable():
    assert qid_for("financial", 1) == "financial-0001"
    assert qid_for("financial", 32) == "financial-0032"


def test_build_questions_filters_and_keeps_order():
    qs = _questions()
    assert [q["qid"] for q in qs] == ["financial-0001", "financial-0002"]
    assert qs[0]["gold_sql"] == "SELECT COUNT(*) FROM account"
    assert qs[0]["evidence"] == "A3 = region"
    # 与 db_id 不符的条目被过滤
    assert all(q["db_id"] == "financial" for q in qs)


def test_build_questions_deterministic():
    assert build_questions(DEV_ROWS, "financial") == build_questions(DEV_ROWS, "financial")


def test_match_qid_exact_and_normalized():
    qs = _questions()
    assert match_qid("How many accounts in East Bohemia?", qs) == "financial-0001"
    # 归一化:标点/大小写/空白不敏感
    assert match_qid("  How many  accounts  in east bohemia?  ", qs) == "financial-0001"
    assert match_qid("no such question", qs) is None


def test_coverage_check_counts():
    qs = _questions()
    cov = coverage_check(qs, [
        {"qid": "financial-0001", "question": "How many accounts in East Bohemia?", "verdict": "MATCH"},
        {"qid": "financial-0001", "verdict": "MISMATCH"},  # 重复判定
        {"question": "Some unrelated question?", "verdict": "MATCH"},  # 文本对账失败 → 无 qid
        {"qid": "other-0009", "verdict": "MATCH"},  # 问题集外
    ])
    assert cov["missing_qids"] == ["financial-0002"]
    assert cov["extra_qids"] == ["other-0009"]
    assert cov["duplicates"] == ["financial-0001"]
    assert cov["covered_qids"] == ["financial-0001", "other-0009"]


def test_check_integrity_partial_coverage_is_warning(tmp_path):
    qp = tmp_path / "questions.jsonl"
    rp = tmp_path / "results.jsonl"
    qp.write_text("\n".join(json.dumps(q) for q in _questions()), encoding="utf-8")
    rp.write_text(json.dumps({"qid": "financial-0001", "verdict": "MATCH"}) + "\n",
                  encoding="utf-8")
    report = check_integrity(qp, rp)
    assert report["ok"] is True  # 缺题是软缺口,默认不算硬问题
    assert report["warnings"] and "缺" in report["warnings"][0]
    assert report["coverage"] == pytest.approx(0.5)


def test_check_integrity_require_full_promotes_missing(tmp_path):
    qp = tmp_path / "questions.jsonl"
    rp = tmp_path / "results.jsonl"
    qp.write_text("\n".join(json.dumps(q) for q in _questions()), encoding="utf-8")
    rp.write_text(json.dumps({"qid": "financial-0001", "verdict": "MATCH"}) + "\n",
                  encoding="utf-8")
    report = check_integrity(qp, rp, require_full=True)
    assert report["ok"] is False
    assert any("缺" in p for p in report["problems"])


def test_check_integrity_hard_problems(tmp_path):
    qp = tmp_path / "questions.jsonl"
    rp = tmp_path / "results.jsonl"
    qp.write_text("\n".join(json.dumps(q) for q in _questions()), encoding="utf-8")
    # 冗余 qid + 空结果文件之外的硬问题:results 里含问题集外 qid 不算 hard?
    # extra/duplicate 才是硬问题。
    rp.write_text(json.dumps({"qid": "outside-1", "verdict": "MATCH"}) + "\n",
                  encoding="utf-8")
    report = check_integrity(qp, rp)
    assert report["ok"] is False
    assert any("问题集外" in p for p in report["problems"])


def test_load_jsonl_tolerates_bad_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\nnot-json\n{"b": 2}\n', encoding="utf-8")
    assert load_jsonl(p) == [{"a": 1}, {"b": 2}]


def test_field_coverage_counts_nonempty_only():
    """0/空串/空 dict 与缺失同义:elapsed_ms=0 是默认值,不是"耗时为零"。"""
    rows = [
        {"tokens": {"total": 120}, "elapsed_ms": 900,
         "compile_meta": {"outcome": "compiled"}, "path": "compiled"},
        {"tokens": {}, "elapsed_ms": 0, "compile_meta": {}, "path": ""},
    ]
    cov = field_coverage(rows)
    assert cov["tokens"] == 0.5
    assert cov["elapsed_ms"] == 0.5
    assert cov["compile_meta"] == 0.5
    assert cov["path"] == 0.5
    assert cov["plan"] == 0.0
    assert cov["matched_tables"] == 0.0


def test_field_coverage_empty_results():
    cov = field_coverage([])
    assert set(cov) == {
        "tokens", "elapsed_ms", "compile_meta", "path", "plan", "matched_tables",
    }
    assert all(v == 0.0 for v in cov.values())


def _write_baseline(tmp_path, result_extra=None):
    """一题问题集 × 一条结果(部分覆盖:缺题只算 warning)。"""
    qp = tmp_path / "questions.jsonl"
    rp = tmp_path / "results.jsonl"
    qp.write_text("\n".join(json.dumps(q) for q in _questions()), encoding="utf-8")
    entry = {"qid": "financial-0001", "verdict": "MATCH"}
    entry.update(result_extra or {})
    rp.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return qp, rp


def test_check_integrity_reports_field_coverage(tmp_path):
    qp, rp = _write_baseline(tmp_path)
    report = check_integrity(qp, rp)
    assert report["ok"] is True  # 只报告,不拦截(冻结基线补不齐历史条目)
    assert report["field_coverage"]["tokens"] == 0.0
    assert report["field_coverage"]["path"] == 0.0


def test_check_integrity_require_fields_met(tmp_path):
    qp, rp = _write_baseline(tmp_path, {
        "tokens": {"total": 100}, "elapsed_ms": 1200, "path": "llm",
    })
    report = check_integrity(qp, rp, require_fields=["tokens", "elapsed_ms", "path"])
    assert report["ok"] is True


def test_check_integrity_require_fields_incomplete_is_hard(tmp_path):
    qp, rp = _write_baseline(tmp_path, {"path": "llm"})
    report = check_integrity(qp, rp, require_fields=["tokens"])
    assert report["ok"] is False
    assert any("字段覆盖不足" in p and "tokens" in p for p in report["problems"])


def test_check_integrity_require_fields_unknown_name(tmp_path):
    qp, rp = _write_baseline(tmp_path)
    report = check_integrity(qp, rp, require_fields=["nope"])
    assert report["ok"] is False
    assert any("未知" in p for p in report["problems"])
