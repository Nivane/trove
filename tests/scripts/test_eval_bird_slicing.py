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
    stamp_elapsed,
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
        rollback_target/validation_hits/n_candidates;链路归因字段
        plan/matched_tables 供 #1 离线复算。"""
        final = _DummyState()
        entry = _result_entry("r1", "q", "", "g", "MATCH", final)
        assert entry["consensus"] is True
        assert entry["confidence"] == 0.6
        assert entry["selection"] == {"votes": {"s1": 3}, "adopted": True}
        assert entry["fix_mode"] == "fixer"
        assert entry["rollback_target"] == "gen_sql"
        assert entry["validation_hits"] == [{"rule": "answer-columns"}]
        assert entry["n_candidates"] == 2
        assert entry["plan"] == {"answer_columns": ["count"]}
        assert entry["matched_tables"] == ["account"]

    def test_result_entry_omits_empty_trace_fields(self):
        """plan/matched_tables 空(或 stub final 没有该属性)不写键 ——
        下游按"键缺失"判未录制,空值冒充"录制过但没有"是另一种谎。"""

        class _Bare:
            sql = "SELECT 1"
            kb_hits = []
            retry_count = 0
            consensus = None
            confidence = 0.0
            selection = {}
            fix_mode = ""
            rollback_target = ""
            validation_hits = []
            candidates = []

        entry = _result_entry("r1", "q", "", "g", "MATCH", _Bare())
        assert "plan" not in entry
        assert "matched_tables" not in entry

    def test_result_entry_path_grades_compile_channels(self):
        """path 三档区分编译通道:compiled(全量编译)/ partial(软 MISS
        骨架)/ llm(裸生成)—— 「机制贡献了多少准确率」靠这一列切。"""

        class _Compiled:
            sql = "SELECT 1"
            compiled = True
            compile_partial = False
            kb_hits = []
            retry_count = 0
            consensus = True
            confidence = 0.9
            selection = {}
            fix_mode = ""
            rollback_target = ""
            validation_hits = []
            candidates = ["SELECT 1"]

        class _Partial(_Compiled):
            compiled = False
            compile_partial = True

        class _Llm(_Compiled):
            compiled = False

        assert _result_entry("r", "q", "", "g", "MATCH", _Compiled())["path"] == "compiled"
        assert _result_entry("r", "q", "", "g", "MATCH", _Partial())["path"] == "partial"
        assert _result_entry("r", "q", "", "g", "MATCH", _Llm())["path"] == "llm"

    def test_result_entry_path_refused_wins_over_stale_compile_flags(self):
        """refused 必须最先判:拒绝轮的 compiled*/compile_partial 可能是
        上一轮残留(终态卫生清的是交付字段,归因字段仍可能带着旧值)——
        只要 refusal 在,path 就是 refused,拒绝题没有 SQL 可言。"""

        class _Refused:
            sql = ""
            refusal = {"reason": "uncovered"}
            compiled = True          # 残留值:不得冒充 compiled
            compile_partial = True   # 残留值:不得冒充 partial
            kb_hits = []
            retry_count = 2
            consensus = True
            confidence = 0.6
            selection = {}
            fix_mode = ""
            rollback_target = "query_sketch"
            validation_hits = []
            candidates = []

        entry = _result_entry("r1", "q", "", "g", "REFUSED", _Refused())
        assert entry["path"] == "refused"
        assert entry["pred_sql"] == ""
        assert entry["retries"] == 2

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


class TestStampElapsed:
    def test_sets_measured_wall_clock(self):
        import time

        e = {}
        stamp_elapsed(e, time.monotonic() - 1.5)
        assert e["elapsed_ms"] >= 1400

    def test_explicit_value_not_overwritten(self):
        import time

        e = {"elapsed_ms": 7}
        stamp_elapsed(e, time.monotonic())
        assert e["elapsed_ms"] == 7


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
    plan_json = {"answer_columns": ["count"]}
    matched_tables = ["account"]


class TestLoadQuestions:
    """问题来源选择:基线问题集(带 qid/gold_sql)vs BIRD dev.json(带 SQL)。

    这层存在的理由是「跑分与回归门读同一份文件」。弄错来源不会报错,只会
    安静地评另一批题 —— 所以来源判定要单独测。
    """

    @staticmethod
    def _args(**kw):
        import argparse

        base = dict(questions=None, dev_json=None, db_id="financial", qids=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_questions_jsonl_is_used_verbatim(self, tmp_path):
        from scripts.eval_bird import load_questions

        p = tmp_path / "q.jsonl"
        p.write_text(
            json.dumps({"qid": "financial-0001", "db_id": "financial",
                        "question": "q1", "gold_sql": "SELECT 1"}) + "\n",
            encoding="utf-8",
        )
        rows = load_questions(self._args(questions=str(p)))
        assert [r["qid"] for r in rows] == ["financial-0001"]
        assert rows[0]["gold_sql"] == "SELECT 1"

    def test_dev_json_sql_key_is_normalized_to_gold_sql(self, tmp_path):
        """dev.json 的字段叫 SQL,基线叫 gold_sql —— 在这里抹平。"""
        from scripts.eval_bird import load_questions

        p = tmp_path / "dev.json"
        p.write_text(json.dumps([{"db_id": "financial", "question": "q1",
                                  "SQL": "SELECT 42"}]), encoding="utf-8")
        rows = load_questions(self._args(dev_json=str(p)))
        assert rows[0]["gold_sql"] == "SELECT 42"

    def test_db_id_filters_both_sources(self, tmp_path):
        from scripts.eval_bird import load_questions

        p = tmp_path / "dev.json"
        p.write_text(json.dumps([
            {"db_id": "financial", "question": "a", "SQL": "SELECT 1"},
            {"db_id": "other", "question": "b", "SQL": "SELECT 2"},
        ]), encoding="utf-8")
        assert len(load_questions(self._args(dev_json=str(p)))) == 1

    def test_qids_selects_exact_questions(self, tmp_path):
        """点名比位置切片稳:问题集重排不会静默换题。"""
        from scripts.eval_bird import load_questions

        p = tmp_path / "q.jsonl"
        p.write_text("\n".join(
            json.dumps({"qid": f"financial-{i:04d}", "db_id": "financial",
                        "question": f"q{i}", "gold_sql": "SELECT 1"})
            for i in (10, 11, 12)
        ), encoding="utf-8")
        rows = load_questions(self._args(questions=str(p),
                                         qids="financial-0012,financial-0010"))
        assert [r["qid"] for r in rows] == ["financial-0012", "financial-0010"]

    def test_unknown_qid_is_an_error_not_a_silent_skip(self, tmp_path):
        """点名了却不跑 → 一轮"成功"的评测 + 一份没补上的基线,退出码 0。"""
        from scripts.eval_bird import load_questions

        p = tmp_path / "q.jsonl"
        p.write_text(json.dumps({"qid": "financial-0001", "db_id": "financial",
                                 "question": "q", "gold_sql": "SELECT 1"}) + "\n",
                     encoding="utf-8")
        with pytest.raises(SystemExit) as e:
            load_questions(self._args(questions=str(p), qids="financial-9999"))
        assert e.value.code == 2

    def test_missing_dev_json_points_at_questions_flag(self, tmp_path):
        """BIRD 数据集不在仓库,默认路径在裸机上必然不存在 —— 裸
        FileNotFoundError 会让人以为是自己路径写错。"""
        from scripts.eval_bird import load_questions

        with pytest.raises(SystemExit) as e:
            load_questions(self._args(dev_json=str(tmp_path / "nope.json")))
        assert e.value.code == 2
