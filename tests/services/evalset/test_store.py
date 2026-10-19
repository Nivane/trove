"""资产模型:tier 派生、状态机、去重、duplicate 清除。"""
from __future__ import annotations

import hashlib

import pytest
import yaml

from trove.services.evalset.models import (
    EvalsetDoc, EvalsetError, EvalsetItem, check_transition, content_hash,
    dump_evalset, new_qid, parse_evalset, resolve_duplicates, tier_of,
)


def item(**kw):
    base = dict(qid="e-a", question="按地区统计贷款总额", reference_sql="SELECT 1",
                source="semantic", reference_origin="semantic_compiler",
                status="pending")
    base.update(kw)
    return EvalsetItem(**base)


@pytest.mark.parametrize("source,origin,want", [
    ("production", "pipeline", "verified"),
    ("manual", "human", "verified"),
    ("semantic", "human", "verified"),
    ("synthetic", "human", "verified"),
    ("semantic", "semantic_compiler", "semantic"),
    ("synthetic", "llm", "synthetic"),
    ("benchmark", "human", "benchmark"),
    ("manual", "llm", "verified"),      # source=manual 优先
    ("semantic", "", "unknown"),
])
def test_tier_derivation(source, origin, want):
    assert tier_of(item(source=source, reference_origin=origin)) == want


@pytest.mark.parametrize("status,action,ok", [
    ("pending", "confirm", True), ("confirmed", "confirm", False),
    ("rejected", "confirm", False), ("retired", "confirm", False),
    ("pending", "reject", True), ("confirmed", "reject", False),
    ("pending", "edit", True), ("confirmed", "edit", True),
    ("rejected", "edit", False), ("retired", "edit", False),
    ("confirmed", "vouch", True), ("pending", "vouch", False),
    ("confirmed", "retire", True), ("pending", "retire", False),
])
def test_state_machine(status, action, ok):
    it = item(status=status)
    if ok:
        check_transition(it, action)
    else:
        with pytest.raises(EvalsetError) as e:
            check_transition(it, action)
        assert e.value.code == "evalset.invalid_transition"


def test_unknown_action_is_an_invalid_transition():
    with pytest.raises(EvalsetError) as e:
        check_transition(item(), "explode")
    assert e.value.code == "evalset.invalid_transition"


def test_content_hash_uses_ledger_ruler_and_ignores_whitespace_case():
    a = content_hash("Loan_Amt  by region", "SELECT  1")
    b = content_hash("loan_amt by region", "select 1")
    assert a == b
    assert len(a) == 16


def test_content_hash_without_sql_hashes_the_question_alone():
    # 无参考 SQL 时只哈希问题(不加换行):账本归一化 + sha256 前 16 位。
    # 注意账本尺子保留 "_"(与评测对账尺子刻意不同)。
    want = hashlib.sha256(b"loan_amt by region").hexdigest()[:16]
    assert content_hash("Loan_Amt  by region", "") == want
    assert content_hash("q", "") != content_hash("q", "SELECT 1")


def test_parse_tolerates_and_reports_issues():
    doc, issues = parse_evalset({
        "version": 1, "datasource": "demo",
        "items": [
            {"qid": "e-a", "question": "q", "source": "manual",
             "reference_origin": "human", "status": "pending",
             "custom_key": {"keep": True}},
            {"question": "no qid"},
            {"qid": "e-a", "question": "dup"},
        ],
    })
    codes = [i.code for i in issues]
    assert "evalset.missing_qid" in codes
    assert "evalset.duplicate_qid" in codes
    assert [it.qid for it in doc.items] == ["e-a"]
    assert doc.items[0].extra["custom_key"] == {"keep": True}


def test_parse_none_and_empty_are_valid_empty_documents():
    doc, issues = parse_evalset(None, datasource="demo")
    assert doc.datasource == "demo" and doc.items == [] and issues == []
    doc, issues = parse_evalset({"version": 1, "items": []})
    assert doc.items == [] and issues == []


def test_parse_reports_structural_issues_and_keeps_items():
    doc, issues = parse_evalset([1, 2])
    assert [i.code for i in issues] == ["evalset.invalid_root"]
    assert doc.items == []

    doc, issues = parse_evalset({"items": "nope"})
    assert [i.code for i in issues] == ["evalset.invalid_items"]

    doc, issues = parse_evalset({"items": ["nope"]})
    assert [i.code for i in issues] == ["evalset.invalid_item"]

    doc, issues = parse_evalset({"items": [{
        "qid": "e-a", "question": "q", "reference_sql": "SELECT 1",
        "source": "moon", "reference_origin": "robot",
        "status": "limbo", "match": "fuzzy",
    }]})
    assert {i.code for i in issues} == {
        "evalset.unknown_source", "evalset.unknown_origin",
        "evalset.unknown_status", "evalset.unknown_match",
    }
    # 未知枚举值条目保留(响亮但不容错成丢弃)。
    assert len(doc.items) == 1
    assert doc.items[0].source == "moon"


def test_parse_keeps_top_level_and_item_extras():
    doc, issues = parse_evalset({
        "version": 1, "datasource": "demo", "owner": "ops",
        "items": [{"qid": "e-a", "question": "q", "custom": 1}],
    })
    assert issues == []
    assert doc.extra == {"owner": "ops"}
    assert doc.items[0].extra == {"custom": 1}


def test_dump_round_trips_known_fields_and_extras():
    doc, issues = parse_evalset({
        "version": 1, "datasource": "demo", "updated_at": "2026-10-19T10:00:00+00:00",
        "owner": "ops",
        "items": [{
            "qid": "e-a", "question": "q", "reference_sql": "SELECT 1",
            "source": "manual", "reference_origin": "human", "status": "pending",
            "tags": {"metric": ["loan_amount"]}, "needs_reference": False,
            "custom_key": {"keep": True},
        }],
    })
    assert issues == []
    text = dump_evalset(doc)
    assert "custom_key" in text and "owner: ops" in text
    back, back_issues = parse_evalset(yaml.safe_load(text))
    assert back_issues == []
    assert back.to_dict() == doc.to_dict()


@pytest.mark.parametrize("status", ["confirmed", "rejected", "retired"])
def test_resolve_duplicates_clears_pointer_on_terminal(status):
    doc = EvalsetDoc(datasource="demo", items=[
        item(qid="e-a", status=status),
        item(qid="e-b", duplicate_of="e-a", note="same question"),
    ])
    assert resolve_duplicates(doc, "e-a") == ["e-b"]
    assert doc.items[1].duplicate_of == ""
    assert "duplicate resolved: e-a " + status in doc.items[1].note


def test_resolve_duplicates_noop_for_pending_or_unknown_qid():
    doc = EvalsetDoc(datasource="demo", items=[
        item(qid="e-a", status="pending"),
        item(qid="e-b", duplicate_of="e-a"),
    ])
    assert resolve_duplicates(doc, "e-a") == []
    assert doc.items[1].duplicate_of == "e-a"
    assert resolve_duplicates(doc, "e-missing") == []


def test_new_qid_is_prefixed_and_unique():
    assert new_qid().startswith("e-")
    assert new_qid() != new_qid()
