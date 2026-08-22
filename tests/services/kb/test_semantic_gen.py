"""semantic_gen tests: deterministic structure-layer generation.

generate_semantic_document turns a physical schema into the OSSIE
structural skeleton (datasets/fields/primary keys + relationships +
terms-derived metrics), 100% deterministic, no LLM.
"""
import yaml

from trove.core.types import ColumnInfo, SchemaInfo, TableInfo
from trove.services.kb.semantic_gen import (
    generate_semantic_document,
    ossie_datatype,
    relationships_from_schema,
)
from trove.services.semantic_layer.ossie import parse_ossie


def _financial_schema() -> SchemaInfo:
    return SchemaInfo(tables=[
        TableInfo(name="loan", columns=[
            ColumnInfo(name="loan_id", type="INTEGER", primary_key=True),
            ColumnInfo(name="account_id", type="INTEGER"),
            ColumnInfo(name="amount", type="DECIMAL(10,2)"),
            ColumnInfo(name="status", type="VARCHAR(2)"),
        ]),
        TableInfo(name="account", columns=[
            ColumnInfo(name="account_id", type="INTEGER", primary_key=True),
            ColumnInfo(name="district_id", type="INTEGER"),
            ColumnInfo(name="date", type="DATE"),
        ]),
        TableInfo(name="district", columns=[
            ColumnInfo(name="district_id", type="INTEGER", primary_key=True),
            ColumnInfo(name="A3", type="VARCHAR(50)"),
        ]),
        TableInfo(name="client", columns=[
            ColumnInfo(name="client_id", type="INTEGER", primary_key=True),
        ]),
    ])


def test_datasets_have_pk_source_and_fields():
    doc = generate_semantic_document(_financial_schema(), model_name="fin")

    datasets = doc["semantic_model"][0]["datasets"]
    assert [d["name"] for d in datasets] == ["loan", "account", "district", "client"]

    loan = datasets[0]
    assert loan["source"] == "loan"
    assert loan["primary_key"] == ["loan_id"]
    # 所有列都是 field
    assert [f["name"] for f in loan["fields"]] == [
        "loan_id", "account_id", "amount", "status"]


def test_field_datatype_mapping():
    loan = generate_semantic_document(_financial_schema())["semantic_model"][0][
        "datasets"][0]
    by_name = {f["name"]: f for f in loan["fields"]}

    assert by_name["amount"]["datatype"] == "Decimal"
    assert by_name["status"]["datatype"] == "String"
    assert by_name["loan_id"]["datatype"] == "Integer"
    # 数值 ID 非时态:不生成 dimension
    assert "dimension" not in by_name["loan_id"]


def test_is_time_default_from_date_column():
    account = generate_semantic_document(_financial_schema())["semantic_model"][0][
        "datasets"][1]
    date_field = next(f for f in account["fields"] if f["name"] == "date")

    assert date_field["datatype"] == "Date"
    # 文件不显式发出 dimension → 解析端按 datatype 默认 is_time=True(spec 语义)
    assert "dimension" not in date_field


def test_relationships_inferred_from_id_naming():
    rels = relationships_from_schema({t.name: t for t in _financial_schema().tables})
    names = {(r["from"], r["to"]) for r in rels}

    assert ("loan", "account") in names       # loan.account_id → account.account_id
    assert ("account", "district") in names   # account.district_id → district.district_id
    assert len(rels) == 2
    rel = next(r for r in rels if r["from"] == "loan")
    assert rel["from_columns"] == ["account_id"]
    assert rel["to_columns"] == ["account_id"]


def test_relationships_skip_self_and_missing_target():
    schema = SchemaInfo(tables=[
        TableInfo(name="t", columns=[
            ColumnInfo(name="t_id", type="INTEGER"),
            ColumnInfo(name="ghost_id", type="INTEGER"),
            ColumnInfo(name="notanid", type="INTEGER"),
        ]),
    ])
    doc = generate_semantic_document(schema)
    assert "relationships" not in doc["semantic_model"][0]


def test_metrics_embedded_from_terms():
    terms = [
        {"term": "number of loan records", "mapping": "COUNT(loan.loan_id)",
         "tables": ["loan"], "definition": "records in loan"},
    ]
    doc = generate_semantic_document(_financial_schema(), model_name="fin", terms=terms)

    model = doc["semantic_model"][0]
    assert model["metrics"][0]["name"] == "number of loan records"
    assert "COUNT(loan.loan_id)" in str(model["metrics"][0])


def test_round_trip_through_parse_ossie():
    schema = _financial_schema()
    terms = [
        {"term": "number of loan records", "mapping": "COUNT(loan.loan_id)",
         "tables": ["loan"]},
    ]
    doc = generate_semantic_document(schema, model_name="fin", terms=terms)
    text = yaml.safe_dump(doc)

    model = parse_ossie(text, preferred_dialect="sqlite")

    assert model.name == "fin"
    assert [d.name for d in model.datasets] == [
        "loan", "account", "district", "client"]
    loan = next(d for d in model.datasets if d.name == "loan")
    assert loan.primary_key == ["loan_id"]
    # 日期字段 is_time 默认 True
    account = next(d for d in model.datasets if d.name == "account")
    date_field = next(f for f in account.fields if f.name == "date")
    assert date_field.is_time is True
    # 关系声明被完整解析
    rel_names = {(r.from_, r.to) for r in model.relationships}
    assert ("loan", "account") in rel_names
    assert ("account", "district") in rel_names
    # metric 正常(数据集锚定)
    assert [m.name for m in model.metrics] == ["number of loan records"]
    assert model.metrics[0].datasets == ["loan"]


def test_ossie_datatype_unknown_returns_none():
    assert ossie_datatype("AUTO_INCREMENT") is None
    assert ossie_datatype("") is None
    assert ossie_datatype(None) is None
    assert ossie_datatype("VARCHAR(100)") == "String"
    assert ossie_datatype("timestamp") == "DateTime"