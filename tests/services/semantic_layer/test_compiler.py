"""JoinResolver tests: deterministic join-clause resolution.

Declared OSSIE relationships win over data-verified naming-convention edges;
BFS from the anchor table connects matched tables, routing through
intermediate tables when needed. Output is deterministic.
"""
from trove.services.semantic_layer.compiler import JoinResolution, JoinResolver
from trove.services.semantic_layer.models import SemanticModel, SemanticRelationship


def _rel(name, from_, to, fc, tc):
    return SemanticRelationship(
        name=name, from_=from_, to=to, from_columns=[fc], to_columns=[tc])


def _model(*rels):
    return SemanticModel(name="m", relationships=list(rels))


def test_empty_with_less_than_two_tables():
    resolver = JoinResolver()
    assert resolver.resolve([]).empty
    assert resolver.resolve(["loan"]).empty
    assert resolver.resolve(["loan"]).clauses == []


def test_declared_edge_direct_join():
    model = _model(_rel("loan_to_account", "loan", "account", "account_id", "account_id"))
    res = JoinResolver(model).resolve(["loan", "account"])
    assert res.clauses == ["loan.account_id = account.account_id"]
    assert res.extra_tables == []


def test_intermediate_table_pulled_in():
    model = _model(
        _rel("loan_to_account", "loan", "account", "account_id", "account_id"),
        _rel("account_to_district", "account", "district", "district_id", "district_id"),
    )
    # 问题只点名 loan + district → BFS 经 account 联上
    res = JoinResolver(model).resolve(["loan", "district"])
    assert res.clauses == [
        "account.district_id = district.district_id",
        "loan.account_id = account.account_id",
    ]
    assert res.extra_tables == ["account"]


def test_verified_naming_fallback_without_model():
    verified = {"loan": ["loan.account_id → account.account_id (5/5 match)"]}
    res = JoinResolver().resolve(["loan", "account"], verified)
    assert res.clauses == ["loan.account_id = account.account_id"]
    assert res.extra_tables == []


def test_suffix_proof_naming_parse():
    """已验证 hint 带 '(N/M match)' 后缀也能解析。"""
    verified = {"account": ["account.district_id → district.district_id (2/2 match)"]}
    res = JoinResolver().resolve(["account", "district"], verified)
    assert res.clauses == ["account.district_id = district.district_id"]


def test_declared_preferred_over_naming_for_same_pair():
    model = _model(_rel("loan_to_account", "loan", "account", "account_id", "account_id"))
    verified = {"loan": ["loan.account_id → account.account_id"]}
    res = JoinResolver(model).resolve(["loan", "account"], verified)
    # 声明边与命名边同对 → 只保留声明(1 条)
    assert len(res.clauses) == 1
    assert res.clauses == ["loan.account_id = account.account_id"]


def test_disconnected_table_ignored():
    model = _model(_rel("loan_to_account", "loan", "account", "account_id", "account_id"))
    res = JoinResolver(model).resolve(["loan", "account", "client"])
    assert res.clauses == ["loan.account_id = account.account_id"]
    assert res.extra_tables == []  # client 无路可达,不进树


def test_empty_when_no_edges_for_matched_set():
    model = _model(_rel("loan_to_account", "loan", "account", "account_id", "account_id"))
    res = JoinResolver(model).resolve(["client", "trans"])
    assert res.empty


def test_render_block():
    res = JoinResolution(
        clauses=["loan.account_id = account.account_id"],
        extra_tables=["account"],
    )
    text = JoinResolver.render(res)
    assert "Relationships:" in text
    assert "- loan.account_id = account.account_id" in text
    assert "account" in text

    assert JoinResolver.render(JoinResolution()) == ""


def test_deterministic_output():
    model = _model(
        _rel("loan_to_account", "loan", "account", "account_id", "account_id"),
        _rel("account_to_district", "account", "district", "district_id", "district_id"),
    )
    a = JoinResolver(model).resolve(["loan", "district"]).clauses
    b = JoinResolver(model).resolve(["loan", "district"]).clauses
    assert a == b