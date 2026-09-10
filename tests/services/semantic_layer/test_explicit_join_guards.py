"""plan.joins 显式路径的基数守卫。

query_sketch 显式声明 joins 时走「权威路径」分支(免 BFS 猜),但**基数检查
必须同样生效**:M:N 扇出与「基数未声明的边」都是边的物理性质,不因谁来选这条
路径而消失。曾经该分支只校验「边已声明 + 构成左深树」,LLM 只要自己吐出 joins
就能绕过 fan_out / unknown_cardinality 守卫,静默产出行倍增 SQL。

二义(ambiguous_join_path)不在本组守卫内:显式路径本身就是对二义的可审计
抉择(plan 里写明了走哪条),再拒等于让该分支失去存在意义;但「引用未声明
边」「不成左深树」仍严格 MISS。

模型形状:`loan`(多)──`client`(一),FK 在 loan 侧。
"""
import pytest

from trove.services.semantic_layer.compiler import (
    CompileMiss,
    SemanticCompiler,
)
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)

# 非唯一键列:client.account_id 不是 client 的主键 → 无法推断 many→one
_NON_UNIQUE = "account_id"
# 唯一键列:client.client_id 是 client 的主键 → 未声明基数也可确定 many→one
_UNIQUE = "client_id"


def _field(name):
    return SemanticField(name=name, expression=name)


def _model(*, cardinality: str, fan_out: str = "", unique_edge: bool = False):
    """loan(多)→ client(一) 单边模型。

    unique_edge=True:FKEY 落在 client 主键上(未声明基数时由唯一键推断放行);
    否则落在普通列 account_id 上(未声明基数 → 无从判定,保守 MISS)。
    """
    col = _UNIQUE if unique_edge else _NON_UNIQUE
    return SemanticModel(
        name="explicit_guards",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("client_id"),
                _field("amount"),
            ]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("account_id"), _field("name"),
            ]),
        ],
        relationships=[
            SemanticRelationship(
                "loan_client", "loan", "client",
                from_columns=[col], to_columns=[col],
                cardinality=cardinality, fan_out=fan_out,
            ),
        ],
        metrics=[
            SemanticMetric("total_loan", "SUM(loan.amount)", datasets=["loan"]),
        ],
    )


def _plan(*, unique_edge: bool = False, joins: str | None = None):
    col = _UNIQUE if unique_edge else _NON_UNIQUE
    return {
        "tables": ["loan", "client"],
        "joins": joins if joins is not None else f"loan.{col} = client.{col}",
        "aggregation": "total_loan",
        "answer_columns": ["client.name", "total_loan"],
    }


def _compile(model, plan=None):
    return SemanticCompiler(model).compile_detailed(plan or _plan(), ["loan", "client"])


# ── 守卫必须生效(修复前静默编译通过)────────────────────────────

def test_explicit_mn_join_rejected():
    """显式 joins 声明的 M:N 边 → 与 BFS 分支同判:fan_out 严格 MISS。"""
    result = _compile(_model(cardinality="M:N"))
    assert isinstance(result, CompileMiss), (
        "LLM 显式声明 joins 不得绕过 M:N 行倍增守卫"
    )
    assert result.reason == "fan_out"


@pytest.mark.parametrize("spelling", ["MANY-TO-MANY", "M2M", "many_to_many"])
def test_explicit_mn_join_rejected_all_spellings(spelling):
    """基数拼写归一化后仍拒(格式耦合曾让 MANY-TO-MANY 漏检)。"""
    result = _compile(_model(cardinality=spelling))
    assert isinstance(result, CompileMiss)
    assert result.reason == "fan_out"


def test_explicit_mn_bridge_still_rejected():
    """bridge 豁免未实现 → 显式路径同样保守拒绝。"""
    result = _compile(_model(cardinality="M:N", fan_out="bridge:client_agg"))
    assert isinstance(result, CompileMiss)
    assert result.reason == "fan_out"


def test_explicit_undeclared_cardinality_rejected():
    """显式路径上基数未声明且无可推断唯一键 → unknown_cardinality(不赌)。"""
    result = _compile(_model(cardinality=""))
    assert isinstance(result, CompileMiss)
    assert result.reason == "unknown_cardinality"


# ── 不得过度拒绝(这些在修复前后都必须放行)──────────────────────

def test_explicit_mn_dedup_still_relaxed():
    """fan_out=dedup 的 M:N 走显式路径 → 仍放行,且去重子查询生效。"""
    result = _compile(_model(cardinality="M:N", fan_out="dedup"))
    assert not isinstance(result, CompileMiss), result
    assert "FROM (SELECT DISTINCT * FROM loan) AS loan" in result.sql
    assert "ON loan.account_id = client.account_id" in result.sql


def test_explicit_undeclared_cardinality_unique_key_backed_ok():
    """to 侧构成声明唯一键 → many→one 数学上确定,未声明基数也放行。"""
    result = _compile(
        _model(cardinality="", unique_edge=True), _plan(unique_edge=True))
    assert not isinstance(result, CompileMiss), result


def test_explicit_declared_many_to_one_ok():
    """声明了 many→one 的边走显式路径 → 正常编译(防过度拒绝回归)。"""
    result = _compile(_model(cardinality="M:1"))
    assert not isinstance(result, CompileMiss), result
    assert "JOIN client ON loan.account_id = client.account_id" in result.sql


def test_explicit_undeclared_edge_still_rejected():
    """既有行为不回退:显式 joins 引用未声明边 → 严格 MISS。"""
    result = _compile(
        _model(cardinality="M:1"), _plan(joins="loan.loan_id = client.client_id"))
    assert isinstance(result, CompileMiss)
    assert result.reason == "ambiguous_join_path"
