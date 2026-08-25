"""eval_compile_stats 聚合器测试:合成 8 行 jsonl 断言命中率/分因/路径 EX%。"""
import json

from scripts.eval_compile_stats import load_entries, stats


def _entry(**kw):
    return kw


def _write(tmp_path, entries):
    p = tmp_path / "results.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return p


def _fixture_entries():
    # 8 题:4 compiled(3 MATCH / 1 MISMATCH)、2 miss+llm(分因各一)、
    # 1 miss+refuse(MATCH,但不算 path=compiled)、1 gold 失败(不进分母)
    return [
        _entry(run_id="r1", question="q1", verdict="MATCH", path="compiled",
               compile_meta={"outcome": "compiled", "miss_reason": "", "miss_component": ""}),
        _entry(run_id="r2", question="q2", verdict="MATCH", path="compiled",
               compile_meta={"outcome": "compiled", "miss_reason": "", "miss_component": ""}),
        _entry(run_id="r3", question="q3", verdict="MATCH", path="compiled",
               compile_meta={"outcome": "compiled", "miss_reason": "", "miss_component": ""}),
        _entry(run_id="r4", question="q4", verdict="MISMATCH", path="compiled",
               compile_meta={"outcome": "compiled", "miss_reason": "", "miss_component": ""}),
        _entry(run_id="r5", question="q5", verdict="MATCH", path="llm",
               compile_meta={"outcome": "miss", "miss_reason": "no_metric_match",
                             "miss_component": "sum(loan.ghost)"}),
        _entry(run_id="r6", question="q6", verdict="MISMATCH", path="llm",
               compile_meta={"outcome": "miss", "miss_reason": "fan_out",
                             "miss_component": "trans, card"}),
        _entry(run_id="r7", question="q7", verdict="MATCH", path="llm",
               compile_meta={"outcome": "miss", "miss_reason": "no_metric_match",
                             "miss_component": "avg(loan.ghost2)"}),
        _entry(run_id="r8", question="q8", verdict="GOLD_ERROR", path="llm",
               compile_meta={"outcome": "miss", "miss_reason": "bad_time_grain",
                             "miss_component": "fortnight"}),
    ]


def test_hit_rate_and_miss_reasons(tmp_path):
    p = _write(tmp_path, _fixture_entries())
    entries = load_entries(p)
    lines = stats(entries)
    text = "\n".join(lines)
    # 命中率:4 compiled / 7 有 meta(可判定 7,无 meta 0)
    assert "编译命中率: 4/7 (57.1%)" in text
    # MISS 分因:no_metric_match 2 次、fan_out 1 次
    assert "no_metric_match: 2 (66.7% of MISS)" in text
    assert "fan_out: 1 (33.3% of MISS)" in text
    # 路径 EX%:compiled 3/4、llm 2/3
    assert "compiled 3/4 (75.0%)" in text
    assert "llm 2/3 (66.7%)" in text


def test_question_filter(tmp_path):
    p = _write(tmp_path, _fixture_entries())
    entries = load_entries(p, question_filter="q1")
    assert len(entries) == 1
    assert entries[0]["run_id"] == "r1"


def test_missing_file_yields_no_entries(tmp_path):
    assert load_entries(tmp_path / "nope.jsonl") == []


def test_malformed_lines_tolerated(tmp_path):
    p = tmp_path / "results.jsonl"
    p.write_text('{"run_id": "ok", "verdict": "MATCH"}\nnot json\n\n{"broken"\n')
    entries = load_entries(p)
    assert len(entries) == 1
    assert entries[0]["run_id"] == "ok"


def test_no_meta_reports_gracefully(tmp_path):
    p = _write(tmp_path, [
        _entry(run_id="r1", question="q1", verdict="MATCH", path="llm"),
    ])
    lines = stats(load_entries(p))
    assert any("无 compile_meta 数据" in l for l in lines)
