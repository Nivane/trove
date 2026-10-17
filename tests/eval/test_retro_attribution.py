"""回顾归因单测(历史失败题 vs 声明层)。

钉住五件事:

1. **路径解析**:表/连接/列名,CTE 名不算表;派生表(生成侧最常见的形态)
   **只认穿透列** —— 聚合/表达式输出列一律不给映射,猜错的映射会把
   「解析不到」变成一条错指控;
2. **连接的声明状态**:直接声明边(含反向、列级匹配)、组合路径(共同父)、
   未声明、表不是数据集 —— 四者互斥;
3. **六个桶的判据与优先级**:假前提·走过 > 假前提·需要走 > 拦路 >
   未声明 > 判不了 > 前提为真;「判不了 ≠ 通过」在归因侧同样成立;
4. **前提只在真用上时才算数**:键前提要求该表被读、键列被引用;
   「父键没声明」(覆盖缺口)与「父键没实测」(判不了)是两件事;
5. **端到端**:真 SQLite 库 + ``verify_declarations`` 的探测结论直接决定
   归因桶 —— 两个模块的接线不是打桩接的。
"""
from __future__ import annotations

import pytest

from trove.core.types import DatasourceConfig
from trove.eval.attribution import (
    BUCKETS,
    FailureRow,
    JoinRef,
    SqlPath,
    attribute_row,
    build_index,
    build_report,
    classify_join,
    classify_path,
    error_signature,
    key_premises,
    load_rows,
    parse_path,
    render_report,
)
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.semantic_layer.ossie import parse_ossie
from trove.services.semantic_layer.verify import (
    ADVISORY,
    UNVERIFIABLE,
    VERIFIED,
    VIOLATED,
    Probe,
    verify_declarations,
)

_MODEL = """
version: "0.1"
semantic_model:
  - name: shop
    datasets:
      - name: account
        source: account
        primary_key: [account_id]
        entity: {display_name: 账户, natural_keys: [account_id]}
        fields:
          - name: account_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: account_id
          - name: district_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: district_id
      - name: district
        source: district
        primary_key: [district_id]
        entity: {display_name: 地区, natural_keys: [district_id]}
        fields:
          - name: district_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: district_id
      - name: trans
        source: trans
        fields:
          - name: account_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: account_id
      - name: disp
        source: disp
        fields:
          - name: account_id
            datatype: Integer
            expression:
              dialects:
                - dialect: ANSI_SQL
                  expression: account_id
    relationships:
      - name: account_to_district
        from: account
        to: district
        from_columns: [district_id]
        to_columns: [district_id]
        cardinality: "1:N"
      - name: trans_to_account
        from: trans
        to: account
        from_columns: [account_id]
        to_columns: [account_id]
        cardinality: "1:N"
      - name: disp_to_account
        from: disp
        to: account
        from_columns: [account_id]
        to_columns: [account_id]
        cardinality: "1:N"
"""


def _model(text: str = _MODEL):
    return parse_ossie(text)


def _index(probes=(), *, live=True, text: str = _MODEL):
    return build_index(_model(text), list(probes), live=live)


def _probe(kind: str, target: str, verdict: str) -> Probe:
    return Probe(kind, target, verdict, "")


#: 「这题的前提全测过且都成立」的最小探测集:gold 读了 district 的
#: district_id(它声明的键),所以键前提也要有实测,否则命题是「判不了」。
_OK_PROBES = (
    _probe("relationships", "account_to_district", VERIFIED),
    _probe("keys", "district.district_id", VERIFIED),
)


# ── 路径解析 ─────────────────────────────────────────────


def test_parse_path_tables_joins_columns():
    path = parse_path(
        "SELECT a.account_id FROM account a "
        "JOIN district d ON a.district_id = d.district_id",
        "mysql")
    assert path.parse_ok
    assert path.tables == ("account", "district")
    assert [(j.left, j.left_col, j.right, j.right_col) for j in path.joins] == [
        ("account", "district_id", "district", "district_id")]
    assert "district_id" in path.columns


def test_cte_name_is_not_a_table():
    path = parse_path(
        "WITH big AS (SELECT account_id FROM account) "
        "SELECT * FROM big JOIN district ON big.account_id = district.district_id",
        "mysql")
    assert "big" not in path.tables
    assert path.tables == ("account", "district")
    # CTE 别名 → 基表未知,但同名列能穿透回 account
    assert [j.label() for j in path.joins] == [
        "account.account_id=district.district_id"]


def test_derived_table_passthrough_column_resolves():
    """派生表的穿透列:`(SELECT account_id FROM account) AS T2` → account。"""
    path = parse_path(
        "SELECT 1 FROM trans T1 JOIN "
        "(SELECT account_id FROM account) AS T2 ON T1.account_id = T2.account_id",
        "mysql")
    assert [j.label() for j in path.joins] == [
        "trans.account_id=account.account_id"]


def test_derived_aggregate_column_is_not_guessed():
    """聚合输出列没有基列可言 —— 猜一个最像的会把未声明报成声明过。"""
    path = parse_path(
        "SELECT 1 FROM trans T1 JOIN "
        "(SELECT account_id, SUM(amount) AS total FROM account GROUP BY account_id) AS T2 "
        "ON T1.account_id = T2.total",
        "mysql")
    assert [j.resolved for j in path.joins] == [False]


def test_using_and_unqualified_joins_are_unresolved():
    path = parse_path(
        "SELECT 1 FROM account JOIN district USING (district_id)", "mysql")
    assert [j.resolved for j in path.joins] == [False]


def test_unparseable_sql_is_not_ok():
    assert parse_path("SELECT FROM WHERE ((", "mysql").parse_ok is False
    assert parse_path("", "mysql").parse_ok is False


# ── 声明索引 ─────────────────────────────────────────────


def test_index_maps_tables_and_aggregates_verdicts():
    idx = _index([
        _probe("relationships", "account_to_district", VERIFIED),
        _probe("relationships", "account_to_district", VIOLATED),
    ])
    assert idx.table_to_dataset == {
        "account": "account", "district": "district",
        "trans": "trans", "disp": "disp"}
    # 混着的时候取最重的一档:一侧证伪 + 一侧通过 ≠ 通过
    assert idx.premise("account_to_district") == VIOLATED


def test_live_off_makes_no_premise_claims():
    idx = _index([_probe("relationships", "account_to_district", VERIFIED)],
                 live=False)
    assert idx.premise("account_to_district") == ""
    assert idx.key_verdict == {}
    assert idx.key_columns["account"] == ("account_id",)


# ── 连接分类 ─────────────────────────────────────────────


def test_declared_edge_matches_both_directions():
    idx = _index([_probe("relationships", "account_to_district", VERIFIED)])
    fwd = classify_join(idx, JoinRef("account", "district_id", "district", "district_id"))
    rev = classify_join(idx, JoinRef("district", "district_id", "account", "district_id"))
    assert fwd[:2] == ("declared", "account_to_district")
    assert rev[:2] == ("declared", "account_to_district")
    assert fwd[2] == VERIFIED


def test_column_mismatch_on_declared_pair_is_undeclared():
    idx = _index()
    kind, name, _ = classify_join(
        idx, JoinRef("account", "account_id", "district", "district_id"))
    assert kind == "undeclared"
    assert name == "account.account_id=district.district_id"


def test_composed_path_through_shared_parent():
    idx = _index()
    kind, name, _ = classify_join(
        idx, JoinRef("trans", "account_id", "disp", "account_id"))
    assert (kind, name) == ("composed", "account")


def test_composed_path_premise_is_the_parent_key():
    idx = _index([_probe("keys", "account.account_id", VIOLATED)])
    kind, name, verdict = classify_join(
        idx, JoinRef("trans", "account_id", "disp", "account_id"))
    assert (kind, name, verdict) == ("composed", "account", VIOLATED)
    verdicts = classify_path(idx, SqlPath(True, ("disp", "trans"), (
        JoinRef("trans", "account_id", "disp", "account_id"),), ("account_id",)))
    assert verdicts.composed == ("account",)
    assert verdicts.violated == ("account",)
    # account 本身没被读 → 键前提不在这里重复出现(组合路径的前提由连接的
    # 分类带出,就是上面这条)—— 键前提只管 SQL 真读了的表
    assert verdicts.violated_keys == ()


def test_composed_without_declared_parent_key_is_coverage_gap():
    """父键没声明 → 覆盖缺口(另有计数),不是「没实测」的判不了。"""
    text = _MODEL.replace("        primary_key: [account_id]\n", "") \
                 .replace("        entity: {display_name: 账户, natural_keys: [account_id]}\n", "")
    idx = _index(text=text)
    verdicts = classify_path(idx, SqlPath(True, ("disp", "trans"), (
        JoinRef("trans", "account_id", "disp", "account_id"),), ()))
    assert verdicts.composed == ("account",)
    assert verdicts.composed_undeclared == ("account",)
    assert verdicts.unprobed_keys == ()


def test_unknown_table_detected():
    idx = _index()
    verdicts = classify_path(idx, SqlPath(True, ("card", "trans"), (
        JoinRef("trans", "account_id", "card", "account_id"),), ()))
    assert verdicts.unknown_tables == ("card",)
    assert verdicts.declared_only() is False


# ── 键前提只在真用上时算数 ───────────────────────────────


def test_key_premise_requires_the_key_column_to_be_used():
    idx = _index([_probe("keys", "account.account_id", VIOLATED)])
    used = SqlPath(True, ("account",), (), ("account_id",))
    unused = SqlPath(True, ("account",), (), ("district_id",))
    assert key_premises(idx, used) == (["account"], [])
    assert key_premises(idx, unused) == ([], [])


def test_key_premise_unprobed_when_not_live():
    idx = _index(live=False)
    used = SqlPath(True, ("account",), (), ("account_id",))
    assert key_premises(idx, used) == ([], ["account"])


# ── 归因桶 ───────────────────────────────────────────────


def _row(gold: str, pred: str = "", error: str = "mismatch") -> FailureRow:
    return FailureRow(question="q", gold_sql=gold, pred_sql=pred, error=error)


_GOLD_DISTRICT = ("SELECT COUNT(*) FROM account a "
                  "JOIN district d ON a.district_id = d.district_id")
_PRED_DISTRICT = ("SELECT COUNT(*) FROM account a "
                  "JOIN district d ON a.district_id = d.district_id")


def test_bucket_premise_true():
    idx = _index(_OK_PROBES)
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "premise_true"


def test_declared_key_without_probe_is_not_true():
    """键前提声明了但没实测 → 判不了;「没测过」不许读成「为真」。"""
    idx = _index([_probe("relationships", "account_to_district", VERIFIED)])
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "unattributable"
    assert "district(键)[未实测]" in f.reason


def test_bucket_premise_false_walked_beats_needed():
    idx = _index([_probe("relationships", "account_to_district", VIOLATED)])
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "premise_false_walked"
    assert "account_to_district[已被证伪]" in f.reason


def test_bucket_premise_false_needed_when_pred_is_clean():
    idx = _index([_probe("relationships", "account_to_district", VIOLATED)])
    f = attribute_row(_row(_GOLD_DISTRICT, "SELECT COUNT(*) FROM account"), idx)
    assert f.bucket == "premise_false_needed"
    assert "pred_missing_table" in f.flags


_MN_MODEL = _MODEL.replace('        cardinality: "1:N"\n      - name: trans_to_account',
                           '        cardinality: "M:N"\n      - name: trans_to_account')


def test_bucket_path_blocked_for_mn_without_exemption():
    idx = _index(text=_MN_MODEL)
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "path_blocked"
    assert "M:N 未声明 fan_out 豁免" in f.reason


def test_bucket_path_undeclared_for_missing_table():
    idx = _index()
    f = attribute_row(_row(
        "SELECT COUNT(*) FROM card c JOIN disp d ON c.disp_id = d.disp_id"), idx)
    assert f.bucket == "path_undeclared"
    assert "card" in f.reason


def test_bucket_unattributable_when_not_live():
    idx = _index(live=False)
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "unattributable"
    assert "未接库" in f.reason


def test_bucket_unattributable_when_gold_unparseable():
    idx = _index()
    f = attribute_row(_row("SELECT FROM WHERE ((", "SELECT 1"), idx)
    assert f.bucket == "unattributable"
    assert f.gold.parsed is False


def test_unverified_edge_never_reads_as_true():
    """判不了 ≠ 通过:探测失败(unverifiable)的声明前提进「判不了」。"""
    idx = _index([_probe("relationships", "account_to_district", UNVERIFIABLE)])
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "unattributable"
    assert "未能实测" in f.reason


def test_advisory_premise_is_not_a_violation():
    idx = _index(_OK_PROBES + (
        _probe("relationships", "account_to_district", ADVISORY),))
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "premise_true"


# ── 报告 ─────────────────────────────────────────────────


def test_error_signature_collapses_numbers():
    assert error_signature("mismatch (pred 1 rows, gold 4484 rows)") == \
        "mismatch (pred # rows, gold # rows)"
    assert error_signature("") == "(无 error 字段)"


def test_report_counts_dedupes_samples_and_renders():
    idx = _index(_OK_PROBES)
    rows = [_row(_GOLD_DISTRICT, _PRED_DISTRICT, "mismatch (pred 1 rows, gold 2 rows)"),
            _row(_GOLD_DISTRICT, _PRED_DISTRICT, "mismatch (pred 3 rows, gold 9 rows)")]
    report = build_report(rows, idx, meta={"kb_dir": "x"})
    assert report["counts"]["rows"] == 2
    assert report["counts"]["questions"] == 1
    assert report["counts"]["buckets"]["premise_true"] == 2
    # 同一道题的两条记录在样例里只出现一次,签名仍按条数归并
    assert len(report["samples"]["premise_true"]) == 1
    assert report["error_signatures"]["premise_true"] == {
        "mismatch (pred # rows, gold # rows)": 2}
    assert report["coverage"]["gold_fully_declared"] == 2
    text = render_report(report)
    assert "前提为真(声明层解释不了)" in text
    assert "签名 ×2: mismatch (pred # rows, gold # rows)" in text
    assert set(report["counts"]["buckets"]) == set(BUCKETS)


def test_report_join_total_does_not_double_count_unprobed():
    idx = _index(live=False)
    report = build_report([_row(_GOLD_DISTRICT, _PRED_DISTRICT)], idx)
    gj = report["counts"]["joins_gold"]
    assert gj["joins"] == 1
    assert gj["declared"] == 1
    assert gj["unprobed_in_declared"] == 1   # 子集,不另加进总数


def test_load_rows_is_loud_on_bad_json(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"question": "a"}\n\n{"question": "b"}\n', encoding="utf-8")
    assert [r.question for r in load_rows(p)] == ["a", "b"]
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"question": "a"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":2"):
        load_rows(bad)


# ── 端到端:真库探测 → 桶 ────────────────────────────────


@pytest.fixture
async def make_adapter():
    registries = []

    async def _make(ddl: str):
        registry = ConnectorRegistry()
        adapter = await registry.register(DatasourceConfig(
            name="attr_db", type="sqlite",
            connection_params={"path": ":memory:"}))
        for stmt in ddl.split(";"):
            if stmt.strip():
                await adapter.execute(stmt)
        registries.append(registry)
        return adapter

    yield _make
    for r in registries:
        await r.close_all()


async def test_live_probe_violation_drives_the_bucket(make_adapter):
    """「一」侧不唯一的库 → verify 报 violated → gold 需要这条边 → 假前提。"""
    adapter = await make_adapter(
        "CREATE TABLE account (account_id INTEGER, district_id INTEGER);"
        "CREATE TABLE district (district_id INTEGER);"
        "INSERT INTO district VALUES (1), (1);"
        "INSERT INTO account VALUES (10, 1)")
    model = _model()
    probes = await verify_declarations(adapter, model, "sqlite")
    assert [p.verdict for p in probes if p.target == "account_to_district"] == [VIOLATED]
    idx = build_index(model, probes, live=True)
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "premise_false_walked"
    assert "account_to_district[已被证伪]" in f.reason


async def test_live_probe_verified_gives_premise_true(make_adapter):
    adapter = await make_adapter(
        "CREATE TABLE account (account_id INTEGER, district_id INTEGER);"
        "CREATE TABLE district (district_id INTEGER);"
        "INSERT INTO district VALUES (1), (2);"
        "INSERT INTO account VALUES (10, 1)")
    model = _model()
    probes = await verify_declarations(adapter, model, "sqlite")
    idx = build_index(model, probes, live=True)
    f = attribute_row(_row(_GOLD_DISTRICT, _PRED_DISTRICT), idx)
    assert f.bucket == "premise_true"
    assert idx.premise("account_to_district") == VERIFIED
