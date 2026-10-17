"""trajectory 适配器测试:run 日志扫描器 / traces 归组 / 合并优先级。"""
from __future__ import annotations

from trove.eval.trajectory import load_run_logs, parse_run_log, trajectory_from_log

LOG = """════ Run r1 · 2026-08-19 10:58:29
question: q one?
gold_sql: SELECT 1
├─ [1] route_intent
├─ in:
│   plan = 
│   plan_json = None
│   matched_tables = []
│   retry_count = 0
│ ├─ · llm deepseek · 12ms · temp 0.0
│ │   [system]
│ │     You are a planner.
prefix-less prompt text, must not become a value
├─ [4] planner
│ ├─ in:
│   matched_tables = ["aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh", "ii", "jj", "kk", "ll"]
│   plan = Tables: aa, bb
Joins: aa.i = bb.i
Conditions:
  - aa.k = 7
Aggregation: none
Answer columns: x
│   plan_json = {"tables": ["aa", "bb"], "joins": "aa.i = bb.i", "cond
│   last_rollback_target = planner
│   validation_hits = [{"rule": "answer-columns", "reason": "conflict"}]
└─ finish: verdict=OK · retries=0 · rows=1 · error=
"""


def test_parse_multiline_plan_and_lists():
    facts = parse_run_log(LOG)
    assert facts.question == "q one?"
    assert facts.values["plan"].startswith("Tables: aa, bb")
    assert "Answer columns: x" in facts.values["plan"]
    assert "prompt text" not in facts.values["plan"]
    assert facts.values["last_rollback_target"] == "planner"
    assert "planner" in facts.nodes and "route_intent" in facts.nodes


def test_trajectory_from_log_fidelity():
    t = trajectory_from_log(parse_run_log(LOG))
    assert t.matched_tables_state == "present"
    assert len(t.matched_tables) == 12
    assert t.plan_text_state == "present"
    assert t.plan_facts is not None
    assert t.plan_truncated is True           # plan_json 600 静默截断
    assert "validation_hits" in t.available
    assert t.retry_count == 0
    # available 是**字段**词汇,不是日志键名(R19):别名键在这里归并
    assert "rollback_target" in t.available
    assert "last_rollback_target" not in t.available
    assert "plan" in t.available and "plan_json" not in t.available


def test_empty_matched_tables_is_empty_not_absent():
    log = LOG.replace('["aa", "bb", "cc", "dd", "ee", "ff", "gg", "hh", "ii", "jj", "kk", "ll"]', "[]")
    t = trajectory_from_log(parse_run_log(log))
    assert t.matched_tables_state == "empty"


def test_load_run_logs_last_wins(tmp_path):
    (tmp_path / "a.log").write_text(LOG, encoding="utf-8")
    (tmp_path / "b.log").write_text(
        LOG.replace("q one?", "q one?").replace('["aa", ', '["zz", '),
        encoding="utf-8")
    import os
    os.utime(tmp_path / "a.log", (1, 1))
    os.utime(tmp_path / "b.log", (2, 2))
    got = load_run_logs(tmp_path)
    assert got["q one?"].matched_tables[0] == "zz"


def _log(*, plan: str = "", plan_json: str = "None",
         matched: str = '["aa", "bb"]') -> str:
    return f"""════ Run r9 · 2026-08-19 11:00:00
question: q nine?
├─ [2] planner2
│ ├─ in:
│   plan = {plan}
│   plan_json = {plan_json}
│   matched_tables = {matched}
└─ finish: verdict=OK · retries=0 · rows=1 · error=
"""


def test_valid_plan_json_is_the_plan_source():
    """合法 plan_json 是实语料 plan_facts 的主来源(32/50),此前无测试覆盖。"""
    log = _log(
        plan="表: aa, bb",
        plan_json='{"tables": ["aa", "bb"], "joins": "aa.i = bb.i", '
                  '"conditions": [], "aggregation": "none", "answer_columns": ["x"]}',
        matched='["AA", "BB"]')
    facts = parse_run_log(log)
    assert "planner2" in facts.nodes          # 节点名里的数字不被截断
    t = trajectory_from_log(facts)
    assert t.plan_facts is not None
    assert t.plan_facts.facts.tables == ("aa", "bb")
    assert "table_set" in t.plan_facts.comparable
    assert t.plan_truncated is False
    assert t.matched_tables == ("aa", "bb")   # _parse_table_list 大小写归一


def test_empty_plan_json_object_is_not_truncation():
    t = trajectory_from_log(parse_run_log(_log(plan="", plan_json="{}")))
    assert t.plan_facts is None
    assert t.plan_truncated is False


def test_matched_tables_preview_cut_is_absent_not_partial():
    t = trajectory_from_log(parse_run_log(_log(matched='["aa", "bb"] …(12 items)')))
    assert t.matched_tables_state == "absent"
    assert t.matched_tables == ()


def test_load_run_logs_equal_mtime_ties_break_by_name(tmp_path, monkeypatch):
    import os
    import pathlib
    a, b = tmp_path / "a.log", tmp_path / "b.log"
    a.write_text(_log(matched='["aa"]'), encoding="utf-8")
    b.write_text(_log(matched='["zz"]'), encoding="utf-8")
    os.utime(a, (5, 5))
    os.utime(b, (5, 5))
    real_glob = pathlib.Path.glob

    def fake_glob(self, pattern):
        if self == tmp_path:
            return iter([b, a])               # 目录序不代表名字序
        return real_glob(self, pattern)

    monkeypatch.setattr(pathlib.Path, "glob", fake_glob)
    assert load_run_logs(tmp_path)["q nine?"].matched_tables == ("zz",)


import json

from trove.eval.chain import loop_state
from trove.eval.trajectory import (load_structured, load_traces,
                                   merge_trajectories)

TRACES_LINES = [
    {"run_id": "r1", "kind": "run", "question": "q one?", "ts": 1.0},
    {"run_id": "r1", "kind": "step", "node": "schema_linking",
     "detail": {"matched_tables": "['aa', 'bb']"}},
    {"run_id": "r1", "kind": "step", "node": "planner",
     "detail": {"plan": "Tables: aa, bb\nJoins: aa.i = bb.i\nConditions: none"}},
    {"run_id": "r1", "kind": "step", "node": "analyze_error",
     "detail": {"error": "rows differ", "analysis": "need join fix",
                "rollback": "gen_sql"}},
    {"run_id": "r1", "kind": "finish", "summary": {"verdict": "MISMATCH"}},
    {"run_id": "r2", "kind": "run", "question": "q one?", "ts": 2.0},
    {"run_id": "r2", "kind": "step", "node": "schema_linking",
     "detail": {"matched_tables": "['aa']"}},
]


def _write_lines(tmp_path, name, lines):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines),
                 encoding="utf-8")
    return p


def test_load_traces_repr_string_and_last_run_wins(tmp_path):
    got = load_traces(_write_lines(tmp_path, "traces.jsonl", TRACES_LINES))
    t = got["q one?"]
    # 同题多 run:文件序最后一条 run 整条胜出(r2 只有 schema_linking)
    assert t.matched_tables == ("aa",)
    assert t.plan_facts is None
    assert t.error_analysis == ""
    assert t.source == "traces"


def test_load_traces_validate_step_is_the_awareness_evidence(tmp_path):
    """R31:traces 源的 validation_hits 记在 `validate.detail`(设计 §3.1)。

    只有 validate 一步自知的 run 也必须判得出来 —— 否则 loop 三态对它
    只能给 undecidable(守卫与证据意见相反,R19 的同款缺陷换一个源)。
    """
    lines = [
        {"run_id": "r9", "kind": "run", "question": "q nine?", "ts": 1.0},
        {"run_id": "r9", "kind": "step", "node": "schema_linking",
         "detail": {"matched_tables": "['aa']"}},
        {"run_id": "r9", "kind": "step", "node": "validate",
         "detail": {"reason": "SUM over non-numeric column", "retry": 0}},
    ]
    t = load_traces(_write_lines(tmp_path, "traces.jsonl", lines))["q nine?"]
    assert t.validation_hits_raw == "SUM over non-numeric column"
    assert "validation_hits" in t.available
    assert loop_state(t) == "detected_but_unfixed"


def test_load_structured_fidelity_and_skip(tmp_path):
    lines = [
        {"question": "q one?", "plan": {"tables": ["aa"], "joins": "none",
                                        "conditions": [], "aggregation": "none",
                                        "answer_columns": ["x"]},
         "matched_tables": ["aa"], "validation_hits": [], "retries": 1,
         "rollback_target": "gen_sql"},
        {"question": "q two?", "gold_sql": "SELECT 1"},   # 无轨迹字段 → 跳过
    ]
    got = load_structured(_write_lines(tmp_path, "results.jsonl", lines))
    assert set(got) == {"q one?"}
    t = got["q one?"]
    assert t.matched_tables_state == "present"
    assert t.plan_facts is not None
    assert t.retry_count == 1
    assert t.rollback_target == "gen_sql"


def test_merge_field_level_priority(tmp_path):
    traces = load_traces(_write_lines(tmp_path, "traces.jsonl", TRACES_LINES[:5]))
    structured = load_structured(_write_lines(tmp_path, "results.jsonl", [
        {"question": "q one?", "matched_tables": ["zz"],
         "plan": {"tables": ["zz"], "joins": "none", "conditions": [],
                  "aggregation": "none", "answer_columns": ["x"]}}]))
    merged = merge_trajectories(structured, traces)
    assert merged["q one?"].matched_tables == ("zz",)     # structured 优先
    assert merged["q one?"].error_analysis == "need join fix"  # traces 补位


def test_merge_preserves_plan_truncated_state():
    """R38:单源合并必须原样保留 plan 的截断/空态(R20 的 plan_truncated 不能因合并丢失)。"""
    from trove.eval.trajectory import parse_run_log, trajectory_from_log

    log = ("question: q trunc?\n"
           "│   matched_tables = [\"aa\"]\n"
           "│   plan = \n"
           "│   plan_json = {\"tables\": [\"aa\", \"bb\"], \"joins\": \"aa.i = bb.i\", \"cond\n")
    t = trajectory_from_log(parse_run_log(log))
    assert t.plan_text_state == "empty" and t.plan_truncated is True
    m = merge_trajectories({"q trunc?": t})["q trunc?"]
    assert m.plan_text_state == "empty"
    assert m.plan_truncated is True
    assert m.plan_facts is None


def test_load_traces_analyze_error_without_analysis(tmp_path):
    """R39:analyze_error 只带 error 时,链路依然自知过 —— 不能判成 never_detected。"""
    lines = [
        {"run_id": "r10", "kind": "run", "question": "q ten?", "ts": 1.0},
        {"run_id": "r10", "kind": "step", "node": "schema_linking",
         "detail": {"matched_tables": "['aa']"}},
        {"run_id": "r10", "kind": "step", "node": "analyze_error",
         "detail": {"error": "boom"}},
    ]
    t = load_traces(_write_lines(tmp_path, "traces.jsonl", lines))["q ten?"]
    assert t.error_analysis == "boom"
    assert loop_state(t) == "detected_but_unfixed"


def test_load_structured_matched_tables_repr_string(tmp_path):
    """R40:结构化记录的 matched_tables 若被序列化成字符串,按列表解析,绝不逐字符展开。"""
    lines = [{"question": "q repr?", "matched_tables": "['aa', 'bb']"}]
    t = load_structured(_write_lines(tmp_path, "results.jsonl", lines))["q repr?"]
    assert t.matched_tables == ("aa", "bb")
    assert t.matched_tables_state == "present"


def test_load_traces_non_dict_detail_is_ignored(tmp_path):
    """R41:detail 不是对象时按坏值跳过,不让旁路记录把整份 traces 读取打断。"""
    lines = [
        {"run_id": "r11", "kind": "run", "question": "q eleven?", "ts": 1.0},
        {"run_id": "r11", "kind": "step", "node": "schema_linking",
         "detail": "matched_tables"},
    ]
    t = load_traces(_write_lines(tmp_path, "traces.jsonl", lines))["q eleven?"]
    assert t.matched_tables_state == "absent"
