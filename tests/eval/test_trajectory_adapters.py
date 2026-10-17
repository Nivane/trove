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
