"""资产模型:tier 派生、状态机、去重、duplicate 清除。"""
from __future__ import annotations

import hashlib

import pytest
import yaml

from trove.services.evalset.lock import EvalsetBusy, EvalsetLock
from trove.services.evalset.models import (
    EvalsetDoc, EvalsetError, EvalsetItem, check_transition, content_hash,
    dump_evalset, new_qid, parse_evalset, resolve_duplicates, tier_of,
)
from trove.services.evalset.rev import evalset_rev
from trove.services.evalset.store import evalset_path, load_evalset, save_evalset


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


# ── 存储 / evalset_rev / 锁 ───────────────────────────────


def _doc(items):
    return EvalsetDoc(datasource="demo", items=items)


def test_roundtrip_is_lossless_and_atomic(tmp_path):
    doc = _doc([item(qid="e-a", extra={"custom": 1})])
    path = save_evalset(tmp_path, doc)
    assert path == tmp_path / ".trove" / "evalsets" / "demo" / "evalset.yml"
    assert not path.with_suffix(".yml.tmp").exists()
    loaded, issues, exists = load_evalset(tmp_path, "demo")
    assert issues == [] and exists is True
    assert loaded.items[0].extra == {"custom": 1}
    assert loaded.updated_at


def test_missing_file_is_not_an_error(tmp_path):
    doc, issues, exists = load_evalset(tmp_path, "demo")
    assert doc.items == [] and issues == [] and exists is False


def test_evalset_rev_only_tracks_confirmed(tmp_path):
    base = [item(qid="e-a", status="confirmed")]
    rev = evalset_rev(_doc(base))
    assert rev == evalset_rev(_doc([item(qid="e-a", status="confirmed")]))
    assert rev != evalset_rev(_doc([item(qid="e-a", status="confirmed",
                                         reference_sql="SELECT 2")]))
    for changed in ("pending", "rejected", "retired"):
        assert evalset_rev(_doc([item(qid="e-a", status=changed)])) == evalset_rev(_doc([]))

    # 规范 JSON:按 qid 排序(输入顺序不参与),且 projection 只有四个评分字段
    # —— 改 note/tags/extra 不移动 rev。
    a = item(qid="e-a", status="confirmed", note="first")
    b = item(qid="e-b", status="confirmed", reference_sql="SELECT 3")
    assert evalset_rev(_doc([a, b])) == evalset_rev(_doc([b, a]))
    annotated = item(qid="e-a", status="confirmed", note="second",
                     tags={"metric": ["loan_amount"]}, extra={"custom": 1})
    assert evalset_rev(_doc([annotated, b])) == evalset_rev(_doc([a, b]))


def test_lock_is_exclusive_and_non_blocking(tmp_path):
    lock = EvalsetLock(tmp_path / ".trove" / "evalsets")
    with lock.acquire("demo"):
        with pytest.raises(EvalsetBusy):
            with lock.acquire("demo"):
                pass


def test_invalid_datasource_is_rejected_on_every_path(tmp_path):
    # 数据源名会拼进路径:读、写、锁都必须用同一把尺子拦下路径穿越。
    for call in (
        lambda: load_evalset(tmp_path, "../escape"),
        lambda: save_evalset(tmp_path, EvalsetDoc(datasource="bad name")),
    ):
        with pytest.raises(EvalsetError) as e:
            call()
        assert e.value.code == "evalset.invalid_datasource"
    with pytest.raises(EvalsetError):
        with EvalsetLock(tmp_path / ".trove" / "evalsets").acquire("a/b"):
            pass


def test_datasource_mismatch_is_an_issue(tmp_path):
    # 目录名是数据源身份:文件自称别的数据源 → issue(文档按解析结果原样返回,
    # 不静默改写)。下游「有 issue 拒写」闸因此能挡住写进别的数据源目录。
    path = evalset_path(tmp_path, "demo")
    path.parent.mkdir(parents=True)
    path.write_text(
        "version: 1\ndatasource: other\nitems:\n"
        "  - qid: e-a\n    question: q\n",
        encoding="utf-8",
    )
    doc, issues, exists = load_evalset(tmp_path, "demo")
    assert exists is True
    assert [i.code for i in issues] == ["evalset.datasource_mismatch"]
    assert doc.datasource == "other"
    assert [it.qid for it in doc.items] == ["e-a"]


def test_unreadable_asset_is_an_issue_not_an_empty_document(tmp_path):
    path = evalset_path(tmp_path, "demo")
    path.parent.mkdir(parents=True)
    path.write_text("\t- broken: [", encoding="utf-8")
    doc, issues, exists = load_evalset(tmp_path, "demo")
    assert exists is True
    assert [i.code for i in issues] == ["evalset.unreadable"]
    assert doc.items == []
