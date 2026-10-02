"""``eval_understanding.py`` 测试 —— 四个切片 + 三态退出码。

为什么要单独钉:
- **保守性**是这脚本唯一的正确性主张("宁可 unknown,不许虚报")。没有测试
  钉住,一次"顺手多报点"的改动就会把不存在的缺口放进重建清单里 —— 而那
  正是这份报告存在的意义(它是验收表,不是装饰)。
- **归因与写法无关**:别名/CTE 包装只改写法不改物理读表,若被归成 tables
  分叉(实测 0472 就是这个错),读者会去修一个不存在的问题。
- **三态退出码**是 CI 唯一读得懂的信号,"没查成"混进 0 与 check_drift.py
  当年那条路径同型(连不上库的时候,门禁最绿)。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_script():
    """按路径加载 scripts/eval_understanding.py(它不是包的一部分)。"""
    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_understanding.py"
    spec = importlib.util.spec_from_file_location("eval_understanding_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def eu():
    return _load_script()


SEMANTICS = """
semantic_model:
  - name: demo
    datasets:
      - name: account
        source: financial.account
        primary_key:
          - account_id
        fields:
          - name: account_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: account.account_id
            datatype: Integer
            semantic_role: identifier
          - name: district_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: account.district_id
            datatype: Integer
          - name: frequency
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: account.frequency
            datatype: String
            semantic_role: enum
            enum_display:
              POPLATEK MESICNE: monthly issuance
              POPLATEK TYDNE: weekly issuance
      - name: district
        source: financial.district
        fields:
          - name: district_id
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: district.district_id
            datatype: Integer
          - name: A3
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: district.A3
            datatype: String
            ai_context:
              value_aliases:
                north Bohemia:
                  - severni Cechy
    relationships:
      - name: account_to_district
        from: account
        to: district
        from_columns:
          - district_id
        to_columns:
          - district_id
    metrics:
      - name: account count
        description: number of accounts
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(account.account_id)
        datasets:
          - account
"""


@pytest.fixture()
def kb(tmp_path):
    """最小可解析 KB:2 数据集 + 1 关系 + 1 度量 + 1 枚举域 + 1 别名域。"""
    d = tmp_path / ".trove" / "kb" / "demo"
    d.mkdir(parents=True)
    (d / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    return tmp_path / ".trove" / "kb"


def _model(eu, kb):
    model = eu.load_semantic_model(kb, "demo", "sqlite")
    assert model is not None
    return model


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    return path


# ── Slice A:语义表达上限 ────────────────────────────────────────────


def test_expressibility_fully_covered(eu, kb):
    gold = (
        "SELECT district.A3 FROM account "
        "JOIN district ON account.district_id = district.district_id "
        "WHERE account.frequency = 'POPLATEK MESICNE'"
    )
    row = eu.check_expressibility("q1", gold, _model(eu, kb), "sqlite")
    assert row["status"] == "ok"
    assert row["gaps"] == [], row["gaps"]
    assert row["levels"]["tables"]["total"] == 2
    assert row["levels"]["joins"] == {"total": 1, "covered": 1, "gaps": 0, "unknown": 0}
    assert row["levels"]["values"]["covered"] == 1


def test_expressibility_reports_gaps_per_level(eu, kb):
    """缺表 / 缺列 / 缺值三类缺口各就其位 —— 不混进 unknown。"""
    model = _model(eu, kb)
    missing_table = eu.check_expressibility(
        "q1", "SELECT loan.amount FROM loan", model, "sqlite")
    assert missing_table["status"] == "ok"
    assert [g["detail"] for g in missing_table["gaps"]] == ["缺表 loan"]

    missing_col = eu.check_expressibility(
        "q2", "SELECT account.iban FROM account", model, "sqlite")
    assert [g["detail"] for g in missing_col["gaps"]] == ["缺列 account.iban"]

    missing_value = eu.check_expressibility(
        "q3", "SELECT account.account_id FROM account "
              "WHERE account.frequency = 'WEEKLY'", model, "sqlite")
    assert [g["detail"] for g in missing_value["gaps"]] == [
        "缺值 account.frequency=WEEKLY"
    ]


def test_expressibility_unparseable_gold_is_unknown(eu, kb):
    """解析不了 → unknown:**不计入分母**,也不进缺口清单。"""
    model = _model(eu, kb)
    bad = eu.check_expressibility("q1", "NOT SQL AT ALL", model, "sqlite")
    assert bad["status"] == "unknown"
    assert bad["total"] == 0 and bad["gaps"] == []

    ok = eu.check_expressibility(
        "q2", "SELECT account.account_id FROM account", model, "sqlite")
    summary = eu.expressibility_slice(
        [{"qid": "q1", "gold_sql": "NOT SQL AT ALL"},
         {"qid": "q2", "gold_sql": "SELECT account.account_id FROM account"}],
        model, "sqlite",
    )
    assert summary["n_questions"] == 2
    assert summary["n_measurable"] == 1
    assert summary["n_unknown"] == 1
    # 分母只算可测那题:单独跑 q2 的 total 必须与两题合跑相同
    only = eu.expressibility_slice(
        [{"qid": "q2", "gold_sql": "SELECT account.account_id FROM account"}],
        model, "sqlite",
    )
    assert summary["total"] == only["total"]
    assert summary["coverage"] == 1.0
    assert ok["status"] == "ok"


def test_derived_table_qualified_column_is_unknown_not_dropped(eu, kb):
    """CTE/派生表限定的列:**记 unknown**,既不算缺口也不静默消失。

    静默丢掉一项,分母就与"模型覆盖多少"脱钩 —— 报告会显得比实际干净。
    """
    gold = ("SELECT T2.total FROM account AS T1 "
            "JOIN (SELECT district_id, COUNT(*) AS total FROM district "
            "GROUP BY district_id) AS T2 ON T1.district_id = T2.district_id")
    row = eu.check_expressibility("q1", gold, _model(eu, kb), "sqlite")
    assert row["gaps"] == []
    assert any("t2.total" in u for u in row["unknown"]), row["unknown"]
    assert row["levels"]["columns"]["unknown"] == 2  # t2.total + t2.district_id
    # 只数得清的两项进分母(account/district 的物理列),派生表限定的一律不进
    assert row["levels"]["columns"]["total"] == row["levels"]["columns"]["covered"]


def test_undeclared_value_domain_is_counted_not_gapped(eu, kb):
    """模型没声明值域 ≠ 模型表达不了:只计数,不算缺口。"""
    row = eu.check_expressibility(
        "q1", "SELECT account.account_id FROM account "
              "WHERE account.district_id = 5", _model(eu, kb), "sqlite")
    assert row["gaps"] == []
    assert row["undeclared_values"] == 1


# ── Slice B:分叉归因 ────────────────────────────────────────────────


def test_divergence_tables(eu):
    gold = ("SELECT account.account_id FROM account "
            "JOIN district ON account.district_id = district.district_id")
    pred = "SELECT account.account_id FROM account"
    row = eu.attribute_divergence("q1", pred, gold, "sqlite")
    assert row["bucket"] == "tables"
    assert "account+district" in row["detail"]


def test_divergence_cte_wrapping_is_not_a_table_divergence(eu):
    """回归:CTE 包装只改写法(实测 0472 曾被误归成 tables)。

    pred 把 district 包进 CTE,物理读表与 gold 相同 —— 分叉应在外层可见的
    过滤(进了 CTE 体里),不该报成"读表不同"。
    """
    gold = ("SELECT account.account_id FROM account "
            "JOIN district ON account.district_id = district.district_id "
            "WHERE district.A3 = 'North Bohemia'")
    pred = ("WITH d AS (SELECT * FROM district WHERE A3 = 'North Bohemia') "
            "SELECT account.account_id FROM account "
            "JOIN d ON account.district_id = d.district_id")
    row = eu.attribute_divergence("q1", pred, gold, "sqlite")
    assert row["bucket"] == "conds", row
    assert "1 → 0" in row["detail"]


def test_divergence_alias_spelling_normalized(eu):
    """别名写法差异 + 其余全同 → none(写法不是结构分叉)。"""
    gold = ("SELECT t1.account_id FROM account AS t1 "
            "JOIN district AS t2 ON t1.district_id = t2.district_id")
    pred = ("SELECT account.account_id FROM account "
            "JOIN district ON account.district_id = district.district_id")
    row = eu.attribute_divergence("q1", pred, gold, "sqlite")
    assert row["bucket"] == "none"


def test_divergence_signature_equal_reports_none(eu):
    sql = "SELECT account.account_id FROM account"
    row = eu.attribute_divergence("q1", sql, sql, "sqlite")
    assert row["bucket"] == "none"
    assert "签名等价" in row["detail"]


def test_divergence_unparseable_side_is_unknown(eu):
    row = eu.attribute_divergence(
        "q1", "NOT SQL", "SELECT account.account_id FROM account", "sqlite")
    assert row["bucket"] == "unknown"


def test_divergence_cond_value_detail(eu):
    gold = "SELECT account.account_id FROM account WHERE account.frequency = 'A'"
    pred = "SELECT account.account_id FROM account WHERE account.frequency = 'B'"
    row = eu.attribute_divergence("q1", pred, gold, "sqlite")
    assert row["bucket"] == "conds"
    assert "'A'" in row["detail"] and "'B'" in row["detail"]


def test_divergence_slice_counts_buckets(eu):
    entries = [
        {"qid": "q1", "verdict": "MISMATCH", "gold_sql": "SELECT account.account_id FROM account",
         "pred_sql": "SELECT account.account_id FROM account JOIN district ON account.district_id = district.district_id"},
        {"qid": "q2", "verdict": "MATCH", "gold_sql": "SELECT 1", "pred_sql": "SELECT 1"},
        {"qid": "q3", "verdict": "MISMATCH", "gold_sql": "SELECT 1", "pred_sql": ""},
    ]
    out = eu.divergence_slice(entries, "sqlite")
    assert out["n_mismatch"] == 2
    assert out["n_attributable"] == 1  # 空 pred 不进归因
    assert out["buckets"] == {"tables": 1}


# ── Slice C:链路归因 ────────────────────────────────────────────────

_GOLD_CHAIN = ("SELECT account.account_id FROM account "
               "JOIN district ON account.district_id = district.district_id")


def test_chain_unrecorded_without_plan(eu, kb):
    out = eu.chain_slice(
        [{"qid": "q1", "verdict": "MISMATCH", "gold_sql": _GOLD_CHAIN}], "sqlite",
    )
    assert out["status"] == "unrecorded"
    assert out["n_recorded"] == 0
    assert out["entries"][0]["bucket"] == "unrecorded"


def test_chain_schema_linking_miss(eu, kb):
    entry = {
        "qid": "q1", "verdict": "MISMATCH", "gold_sql": _GOLD_CHAIN,
        "matched_tables": ["account"],
        "plan": {"tables": ["account", "district"]},
        "pred_sql": _GOLD_CHAIN,
    }
    row = eu.attribute_chain(entry, "sqlite")
    assert row["bucket"] == "schema_linking"
    assert "district" in row["detail"]


def test_chain_query_sketch_miss(eu, kb):
    entry = {
        "qid": "q1", "verdict": "MISMATCH", "gold_sql": _GOLD_CHAIN,
        "matched_tables": ["account", "district"],
        "plan": {"tables": ["account"]},
        "pred_sql": _GOLD_CHAIN,
    }
    row = eu.attribute_chain(entry, "sqlite")
    assert row["bucket"] == "query_sketch"
    assert "district" in row["detail"]


def test_chain_query_sketch_drops_conditions(eu, kb):
    entry = {
        "qid": "q1", "verdict": "MISMATCH",
        "gold_sql": "SELECT account.account_id FROM account WHERE account.frequency = 'A'",
        "matched_tables": ["account"],
        "plan": {"tables": ["account"], "conditions": []},
        "pred_sql": "SELECT account.account_id FROM account",
    }
    row = eu.attribute_chain(entry, "sqlite")
    assert row["bucket"] == "query_sketch"
    assert "无条件" in row["detail"]


def test_chain_compiler_not_compiled(eu, kb):
    entry = {
        "qid": "q1", "verdict": "MISMATCH", "gold_sql": _GOLD_CHAIN,
        "matched_tables": ["account", "district"],
        "plan": {"tables": ["account", "district"], "conditions": []},
        "compile_meta": {"outcome": "partial", "miss_component": "projections"},
        "pred_sql": _GOLD_CHAIN,
    }
    row = eu.attribute_chain(entry, "sqlite")
    assert row["bucket"] == "compiler"
    assert "projections" in row["detail"]


def test_chain_gen_structure_drift(eu, kb):
    entry = {
        "qid": "q1", "verdict": "MISMATCH", "gold_sql": _GOLD_CHAIN,
        "matched_tables": ["account", "district"],
        "plan": {"tables": ["account", "district"], "conditions": []},
        "compile_meta": {"outcome": "compiled"},
        "pred_sql": "SELECT account.account_id FROM account",
    }
    row = eu.attribute_chain(entry, "sqlite")
    assert row["bucket"] == "gen"


def test_chain_match_is_ok(eu, kb):
    entry = {
        "qid": "q1", "verdict": "MATCH", "gold_sql": _GOLD_CHAIN,
        "matched_tables": ["account", "district"], "plan": {"tables": ["account", "district"]},
        "pred_sql": _GOLD_CHAIN,
    }
    out = eu.chain_slice([entry], "sqlite")
    assert out["status"] == "ok"
    assert out["buckets"] == {"ok": 1}


def test_chain_partial_recording_status(eu, kb):
    entries = [
        {"qid": "q1", "verdict": "MATCH", "gold_sql": _GOLD_CHAIN,
         "matched_tables": ["account"], "plan": {"tables": ["account"]}, "pred_sql": _GOLD_CHAIN},
        {"qid": "q2", "verdict": "MISMATCH", "gold_sql": _GOLD_CHAIN,
         "pred_sql": _GOLD_CHAIN},
    ]
    out = eu.chain_slice(entries, "sqlite")
    assert out["status"] == "partial"
    assert out["n_recorded"] == 1


# ── Slice D:一次通过率 ──────────────────────────────────────────────


def test_first_pass_slice_from_replay(eu):
    entries = [
        {"verdict": "MATCH", "pred_sql": "SELECT 1", "path": "compiled"},
        {"verdict": "MATCH", "pred_sql": "SELECT 1", "path": "compiled",
         "retry_count": 1},
        {"verdict": "MISMATCH", "pred_sql": "SELECT 2", "path": "llm"},
    ]
    out = eu.first_pass_slice(entries)
    assert out["n"] == 3
    assert out["first_pass"] == pytest.approx(0.3333)
    assert out["path_coverage"] == 1.0
    assert out["ex_by_path"] == {"compiled": 1.0, "llm": 0.0}


def test_first_pass_slice_reports_partial_path_coverage(eu):
    entries = [
        {"verdict": "MATCH", "pred_sql": "SELECT 1", "path": "compiled"},
        {"verdict": "MATCH", "pred_sql": "SELECT 1"},
    ]
    out = eu.first_pass_slice(entries)
    assert out["path_coverage"] == 0.5
    assert out["ex_by_path"] is None  # 半份 path 不发分档(缺数据≠llm 档)


# ── main:三态退出码 ─────────────────────────────────────────────────


def _main(eu, questions: Path, results: Path, kb: Path, *extra: str) -> int:
    return eu.main([
        "--questions", str(questions), "--results", str(results),
        "--kb-dir", str(kb), "--db-id", "demo", "--dialect", "sqlite", *extra,
    ])


def test_main_happy_path_exit_0_and_json_shape(eu, kb, tmp_path, capsys):
    qs = _write_jsonl(tmp_path / "q.jsonl", [
        {"qid": "q1", "question": "how many accounts",
         "gold_sql": "SELECT account.account_id FROM account"},
    ])
    rs = _write_jsonl(tmp_path / "r.jsonl", [
        {"qid": "q1", "verdict": "MATCH", "pred_sql": "SELECT account.account_id FROM account",
         "path": "compiled", "matched_tables": ["account"],
         "plan": {"tables": ["account"], "conditions": []}},
    ])
    assert _main(eu, qs, rs, kb, "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {
        "db_id", "dialect", "questions_path", "results_path",
        "expressibility", "divergence", "links", "first_pass",
    }
    assert report["links"]["status"] == "ok"
    assert report["expressibility"]["coverage"] == 1.0


def test_main_missing_kb_exits_2(eu, tmp_path):
    qs = _write_jsonl(tmp_path / "q.jsonl", [{"qid": "q1", "gold_sql": "SELECT 1"}])
    rs = _write_jsonl(tmp_path / "r.jsonl", [{"qid": "q1", "verdict": "MATCH"}])
    assert _main(eu, qs, rs, tmp_path / "no-kb") == 2


def test_main_missing_questions_exits_2(eu, kb, tmp_path):
    rs = _write_jsonl(tmp_path / "r.jsonl", [{"qid": "q1", "verdict": "MATCH"}])
    assert _main(eu, tmp_path / "nope.jsonl", rs, kb) == 2


def test_main_empty_questions_exits_2(eu, kb, tmp_path):
    qs = _write_jsonl(tmp_path / "q.jsonl", [])
    rs = _write_jsonl(tmp_path / "r.jsonl", [{"qid": "q1", "verdict": "MATCH"}])
    assert _main(eu, qs, rs, kb) == 2


def test_main_degenerate_measurement_exits_1(eu, kb, tmp_path):
    """条目为空 → 空洞的 100%/0 结构 = 没查成,不许混进 0。"""
    qs = _write_jsonl(tmp_path / "q.jsonl", [
        {"qid": "q1", "gold_sql": "SELECT account.account_id FROM account"},
    ])
    rs = _write_jsonl(tmp_path / "r.jsonl", [])
    assert _main(eu, qs, rs, kb) == 1


def test_main_all_gold_unparseable_exits_1(eu, kb, tmp_path):
    qs = _write_jsonl(tmp_path / "q.jsonl", [{"qid": "q1", "gold_sql": "NOT SQL"}])
    rs = _write_jsonl(tmp_path / "r.jsonl", [{"qid": "q1", "verdict": "MATCH"}])
    assert _main(eu, qs, rs, kb) == 1


def test_main_text_report_renders(eu, kb, tmp_path, capsys):
    qs = _write_jsonl(tmp_path / "q.jsonl", [
        {"qid": "q1", "gold_sql": "SELECT loan.amount FROM loan"},
    ])
    rs = _write_jsonl(tmp_path / "r.jsonl", [{"qid": "q1", "verdict": "MATCH"}])
    assert _main(eu, qs, rs, kb) == 0
    out = capsys.readouterr().out
    assert "理解层切片" in out
    assert "缺表 loan" in out
    assert "链路归因: 未录制" in out
