"""ossie_format 桥接层测试:flat term ↔ OSSIE semantic_model 双向转换。

覆盖写方向(terms_to_ossie_document / append_term_to_document)、读方向
(ossie_to_term_payloads,含 legacy/坏文件降级)与 qualify_mapping。
"""
import yaml

from trove.services.kb.ossie_format import (
    append_term_to_document,
    ossie_to_term_payloads,
    qualify_mapping,
    terms_to_ossie_document,
)


def dump(doc: dict) -> str:
    return yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True,
                          sort_keys=False)


def test_round_trip_preserves_payload():
    terms = [
        {"term": "平均贷款金额", "aliases": ["均贷额"], "mapping": "AVG(loan.amount)",
         "tables": ["loan"], "definition": "所有贷款的平均金额"},
        {"term": "客户数量", "aliases": [], "mapping": "COUNT(client.client_id)",
         "tables": ["client"], "definition": ""},
    ]
    doc = terms_to_ossie_document(terms, model_name="demo")
    payloads = ossie_to_term_payloads(dump(doc))
    assert payloads == terms


def test_round_trip_without_optional_fields():
    terms = [{"term": "贷款总数", "aliases": [], "mapping": "COUNT(loan.loan_id)",
              "tables": ["loan"], "definition": ""}]
    doc = terms_to_ossie_document(terms)
    text = dump(doc)
    assert "ai_context" not in text
    assert "description" not in text
    assert ossie_to_term_payloads(text) == terms


def test_datasets_are_sorted_union_of_tables():
    doc = terms_to_ossie_document([
        {"term": "a", "mapping": "COUNT(loan.loan_id)", "tables": ["loan"]},
        {"term": "b", "mapping": "COUNT(client.client_id)", "tables": ["client", "loan"]},
    ])
    model = doc["semantic_model"][0]
    assert [d["name"] for d in model["datasets"]] == ["client", "loan"]


def test_empty_mapping_entries_skipped_with_warning(caplog):
    terms = [
        {"term": "好term", "mapping": "SUM(loan.amount)", "tables": ["loan"]},
        {"term": "坏term", "mapping": "", "tables": ["loan"]},
    ]
    doc = terms_to_ossie_document(terms)
    names = [m["name"] for m in doc["semantic_model"][0]["metrics"]]
    assert names == ["好term"]
    assert any("坏term" in r.message for r in caplog.records)


def test_derived_anchoring_tightens_tables():
    # A 决策:锚定从表达式推导 —— 多表绑定收紧为表达式实际引用的表
    doc = terms_to_ossie_document([
        {"term": "t", "mapping": "AVG(loan.amount)", "tables": ["loan", "account", "district"]},
    ])
    payloads = ossie_to_term_payloads(dump(doc))
    assert payloads[0]["tables"] == ["loan"]


def test_legacy_flat_format_returns_empty_with_hint(caplog):
    text = yaml.safe_dump({"terms": [{"term": "x", "mapping": "SUM(loan.amount)",
                                      "tables": ["loan"]}]}, allow_unicode=True)
    assert ossie_to_term_payloads(text) == []
    assert any("overwrite" in r.message for r in caplog.records)


def test_blank_file_returns_empty_silently(caplog):
    assert ossie_to_term_payloads("") == []
    assert ossie_to_term_payloads("# only a comment\n") == []
    assert not caplog.records


def test_broken_ossie_returns_empty_with_warning(caplog):
    # metric 缺 dialects → parse_ossie 抛 ValueError → 降级为 []
    text = yaml.safe_dump({"semantic_model": [{"metrics": [
        {"name": "x", "expression": {"dialects": []}},
    ]}]}, allow_unicode=True)
    assert ossie_to_term_payloads(text) == []
    assert any("not a valid OSSIE" in r.message for r in caplog.records)


def test_empty_name_metric_dropped(caplog):
    text = yaml.safe_dump({"semantic_model": [{"metrics": [
        {"name": "", "expression": {"dialects": [{"dialect": "ANSI_SQL",
                                                   "expression": "COUNT(*)"}]}},
    ]}]}, allow_unicode=True)
    assert ossie_to_term_payloads(text) == []
    assert any("empty name" in r.message for r in caplog.records)


def test_qualify_single_table_plain():
    assert qualify_mapping("AVG(grade)", ["students"]) == "AVG(students.grade)"


def test_qualify_inside_extract():
    assert (qualify_mapping("AVG(EXTRACT(YEAR FROM date))", ["account"])
            == "AVG(EXTRACT(YEAR FROM account.date))")


def test_qualify_leaves_count_star_untouched():
    assert qualify_mapping("COUNT(*)", ["loan"]) == "COUNT(*)"
    assert qualify_mapping("COUNT(loan.loan_id)", ["loan"]) == "COUNT(loan.loan_id)"


def test_qualify_requires_exactly_one_table():
    assert qualify_mapping("AVG(grade)", []) == "AVG(grade)"
    assert qualify_mapping("AVG(grade)", ["a", "b"]) == "AVG(grade)"


def test_qualify_unparseable_untouched():
    assert qualify_mapping("SUM((", ["loan"]) == "SUM(("


def test_append_merges_metrics_and_datasets():
    doc = terms_to_ossie_document([{"term": "a", "mapping": "COUNT(loan.loan_id)",
                                    "tables": ["loan"]}])
    append_term_to_document(doc, {"term": "b", "mapping": "AVG(client.balance)",
                                  "tables": ["client", "loan"], "aliases": ["均余额"],
                                  "definition": "客户平均余额"})
    model = doc["semantic_model"][0]
    assert [m["name"] for m in model["metrics"]] == ["a", "b"]
    assert sorted(d["name"] for d in model["datasets"]) == ["client", "loan"]
    # 追加内容在 OSSIE 文档里结构正确且可读回
    payloads = ossie_to_term_payloads(dump(doc))
    assert {p["term"] for p in payloads} == {"a", "b"}


def test_append_onto_fresh_doc_creates_model():
    doc: dict = {}
    append_term_to_document(doc, {"term": "x", "mapping": "COUNT(loan.loan_id)",
                                  "tables": ["loan"]})
    assert "semantic_model" in doc
    assert doc["semantic_model"][0]["metrics"][0]["name"] == "x"
