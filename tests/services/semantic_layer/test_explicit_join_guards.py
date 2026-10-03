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


def test_explicit_undeclared_edge_repaired_by_table_pair():
    """列名写错但**表对可解析**(A5a):按表对上的声明唯一路径修复,不再一律 MISS。

    原先显式 joins 的列级校验失败即严格 MISS;而计划写错列名(0477 型:
    表对表达了真实关系、列名对不上声明)时,表对表达的骨架是真实且唯一的
    —— 修复走同一套显式 channel(基数守卫/左深树判定不豁免),把「列名笔误」
    与「编造路径」区分开。缺的表(投影引用的 district)按声明路径补齐。
    """
    result = _chain_compile("loan.loan_id = account.account_id")
    assert not isinstance(result, CompileMiss), result
    # 修复出的是**声明列对**(account_id),而不是计划里写错的列名
    assert "JOIN account ON loan.account_id = account.account_id" in result.sql
    assert "JOIN district ON account.district_id = district.district_id" in result.sql
    assert "loan.loan_id = account.account_id" not in result.sql


def test_explicit_undeclared_table_still_rejected():
    """表对里有**未声明数据集** → 修复无路可走,维持严格 MISS。"""
    result = _chain_compile("loan.loan_id = ghost.ghost_id", tables=["loan", "ghost"])
    assert isinstance(result, CompileMiss)
    assert result.reason == "ambiguous_join_path"


def test_explicit_ambiguous_table_pair_still_rejected():
    """同一对表有**多条**声明关系(表对一义、关系二义)→ 不猜,严格 MISS。"""
    model = _chain_model()
    model.relationships.append(SemanticRelationship(
        "loan_to_account_alt", "loan", "account",
        from_columns=["loan_id"], to_columns=["account_id"],
        cardinality="1:N"))
    result = SemanticCompiler(model).compile_detailed(
        _chain_plan("loan.nope = account.nope"), _L3)
    assert isinstance(result, CompileMiss)
    assert result.reason == "ambiguous_join_path"


# ── 子句分隔符:分号(0487 型误拒的解析缺口)────────────────────
#
# query_sketch 会用 ``;`` 分隔多条 join 子句。旧切分正则只认 ``,``/``and``,
# 于是整串进 parse_one 得到一个 Block(内含两条 EQ),撞上「每子句恰一条 EQ」
# 判定 → 已声明路径被当成不可解析 → 严格 MISS(误拒,非模型缺口)。
# 修法仅是补一个分隔符:切分后每条子句仍必须恰一条 EQ 且命中已声明
# relationship,判定强度不变(下面 ② 组钉住不放松)。


def _chain_model():
    """loan ── account ── district(── region) 链,三条已声明 1:N 边。"""
    return SemanticModel(
        name="semicolon_chain",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
            ]),
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("district_id"),
            ]),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("region_id"), _field("A2"),
            ]),
            SemanticDataset(name="region", primary_key=["region_id"], fields=[
                _field("region_id"), _field("name"),
            ]),
        ],
        relationships=[
            SemanticRelationship(
                "loan_to_account", "loan", "account",
                from_columns=["account_id"], to_columns=["account_id"],
                cardinality="1:N"),
            SemanticRelationship(
                "account_to_district", "account", "district",
                from_columns=["district_id"], to_columns=["district_id"],
                cardinality="1:N"),
            SemanticRelationship(
                "district_to_region", "district", "region",
                from_columns=["region_id"], to_columns=["region_id"],
                cardinality="1:N"),
        ],
        metrics=[SemanticMetric("total_loan", "SUM(loan.amount)", datasets=["loan"])],
    )


_L3 = ["loan", "account", "district"]
_A3 = ["district.A2", "total_loan"]
_L4 = ["loan", "account", "district", "region"]
_A4 = ["region.name", "total_loan"]


def _chain_plan(joins, tables=None, answer_columns=None):
    return {
        "tables": tables or _L3,
        "joins": joins,
        "aggregation": "total_loan",
        "answer_columns": answer_columns or _A3,
    }


def _chain_compile(joins, tables=None, answer_columns=None):
    return SemanticCompiler(_chain_model()).compile_detailed(
        _chain_plan(joins, tables, answer_columns), tables or _L3)


# ── ① 分号分隔的已声明边 → 权威路径,正常编译 ──────────────────

@pytest.mark.parametrize("joins", [
    "loan.account_id = account.account_id; account.district_id = district.district_id",
    "loan.account_id = account.account_id;account.district_id = district.district_id",
    "loan.account_id = account.account_id; account.district_id = district.district_id;",
])
def test_explicit_joins_semicolon_separated_ok(joins):
    """`;` 分隔(有无空格/尾随分号)与 ``,``/``and`` 同判 → 权威路径编译通过。"""
    result = _chain_compile(joins)
    assert not isinstance(result, CompileMiss), result
    assert "JOIN account ON loan.account_id = account.account_id" in result.sql
    assert "JOIN district ON account.district_id = district.district_id" in result.sql


def test_explicit_joins_mixed_separators_ok():
    """`;` 与 ``,`` 混用 → 逐子句解析,三条已声明边仍成左深树。"""
    result = _chain_compile(
        "loan.account_id = account.account_id, "
        "account.district_id = district.district_id; "
        "district.region_id = region.region_id",
        tables=_L4, answer_columns=_A4)
    assert not isinstance(result, CompileMiss), result
    assert "JOIN region ON district.region_id = region.region_id" in result.sql


def test_explicit_joins_semicolon_split_directly():
    """解析层钉住:``;`` 切分后每条子句各自成边(不再整串落进一个 Block)。"""
    from trove.services.semantic_layer.compiler import _explicit_join_edges

    edges, present = _explicit_join_edges(
        "loan.account_id = account.account_id; "
        "account.district_id = district.district_id",
        _chain_model())
    assert present is True
    assert edges is not None
    assert [(e.from_, e.to, e.from_column, e.to_column) for e in edges] == [
        ("loan", "account", "account_id", "account_id"),
        ("account", "district", "district_id", "district_id"),
    ]


# ── ② 列名笔误 → 表对修复(A5a);语柄坏的/表对不存在的仍严格 MISS ──
#
# 0477 形状:并列的第一条边列名写错(``loan.loan_id = account.account_id``,
# 真实列对是 account_id)。A5a 起按**表对**在声明图上找唯一路径修复 ——
# 表对本身表达了真实骨架,列名笔误不该让整条计划作废;但修复不是放行:
# 表对不存在/同表对多关系/子句形态坏 → 一律维持严格 MISS(下面 ③ 组钉住)。

def test_explicit_joins_undeclared_edge_repaired_by_table_pair():
    """0477 形状(AND 分隔,首条边列名错但表对存在)→ 按表对修复后编译。"""
    result = _chain_compile(
        "loan.loan_id = account.account_id AND "
        "account.district_id = district.district_id")
    assert not isinstance(result, CompileMiss), result
    assert "JOIN account ON loan.account_id = account.account_id" in result.sql
    assert "JOIN district ON account.district_id = district.district_id" in result.sql


def test_explicit_joins_undeclared_first_edge_repaired_by_table_pair():
    """同上,但用 `;` 分隔 → 修复路径与 AND 分隔同判(分隔符不影响表对解析)。"""
    result = _chain_compile(
        "loan.loan_id = account.account_id; "
        "account.district_id = district.district_id")
    assert not isinstance(result, CompileMiss), result
    assert "JOIN account ON loan.account_id = account.account_id" in result.sql


@pytest.mark.parametrize("joins", [
    # 无 EQ 的垃圾子句
    "loan.account_id = account.account_id; NOT_A_CLAUSE",
    # 一条子句里两条 EQ(旧整串行为的形状被逐个挡回)
    "loan.account_id = account.account_id; "
    "account.district_id = district.district_id = region.region_id",
    # 子句不是列对列
    "loan.account_id = account.account_id; 1 = 1",
])
def test_explicit_joins_semicolon_malformed_clause_still_rejected(joins):
    """`;` 切出的子句照样要过「恰一条 EQ + 列对列 + 已声明」三关。"""
    result = _chain_compile(joins, tables=_L4, answer_columns=_A4)
    assert isinstance(result, CompileMiss)
    assert result.reason == "ambiguous_join_path"
