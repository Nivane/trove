"""A③ 显式 joins 的 needed 感知剪枝。

BFS 通道一直按 ``needed``(组件真正引用的表)剪枝;显式通道此前完全不看
needed —— 计划把共享维度/多余叶子写进 joins 就一律联进去(0492 型:account
只是 district 的另一个叶,联上纯属多余,还会白白触发基数判定)。

剪枝规则刻意保守,两条不变量(见 ``_prune_explicit_to_needed``):

1. **只删叶子**:端点不在 keep(needed ∪ 锚表)里且在当前边表里度数为 1 的边
   才可删,迭代到不动点 —— 删叶子保连通,两棵子树之间的桥(度数 ≥ 2 的中间
   点)照常保留;
2. **锚表的声明连边一律不剪**:锚表是 FROM 根,它的连边定义查询的粒度与行集
   —— 剪掉会破坏"FROM 锚表 + 树序 JOIN"的骨架不变量。这条也保证剪枝结果恒
   包含锚表:不存在剪空后静默回退 BFS 的路径。

断树(显式边集本身不连通)整体跳过剪枝:"哪棵多余"无从判断,剪掉一整棵
等于替计划猜它想联哪个组件 —— 维持今日 ``ambiguous_join_path`` 硬 MISS。
"""
from __future__ import annotations

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


def _field(name):
    return SemanticField(name=name, expression=name)


def _model(*, district_card: str = "1:N"):
    """account ── client ── district ── region 链(全部 1:N,district 可换 M:N)。"""
    return SemanticModel(
        name="prune",
        datasets=[
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("client_id"), _field("frequency"),
            ]),
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("district_id"), _field("gender"),
            ]),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("region_id"), _field("A2"),
            ]),
            SemanticDataset(name="region", primary_key=["region_id"], fields=[
                _field("region_id"), _field("name"),
            ]),
        ],
        relationships=[
            SemanticRelationship("account_to_client", "account", "client",
                                 from_columns=["client_id"], to_columns=["client_id"],
                                 cardinality="1:N"),
            SemanticRelationship("client_to_district", "client", "district",
                                 from_columns=["district_id"], to_columns=["district_id"],
                                 cardinality=district_card),
            SemanticRelationship("district_to_region", "district", "region",
                                 from_columns=["region_id"], to_columns=["region_id"],
                                 cardinality="1:N"),
        ],
        metrics=[
            SemanticMetric("number of accounts", "COUNT(account.account_id)",
                           datasets=["account"]),
        ],
    )


_CHAIN_JOINS = ("account.client_id = client.client_id AND "
                "client.district_id = district.district_id")


def _compile(plan, matched, model=None):
    return SemanticCompiler(model or _model()).compile_detailed(
        plan, list(matched), force_dialect="mysql")


def _sql(res) -> str:
    assert not isinstance(res, CompileMiss), f"unexpected MISS: {res}"
    return res.sql


# ══ 多余叶子被剪:显式通道与 BFS 通道对齐 ═══════════════════


def test_unneeded_leaf_pruned_and_compiles():
    """显式声明的 district 叶子无人引用 → 剪掉,计划照常编译。"""
    plan = {
        "tables": ["account", "client", "district"],
        "joins": _CHAIN_JOINS,
        "answer_columns": ["account.account_id", "client.client_id"],
    }
    res = _compile(plan, ["account", "client", "district"])
    assert _sql(res) == (
        "SELECT account.account_id, client.client_id\n"
        "FROM account\n"
        "JOIN client ON account.client_id = client.client_id"
    )


def test_bfs_control_same_shape_byte_identical():
    """BFS 对照:同一计划(decomposed joins)走 BFS 通道剪枝 → 产物字节一致。

    显式通道剪枝后的产物与"没有显式 joins 时 BFS 本来会产出什么"对齐 ——
    这正是 A③ 的语义:显式声明不该让计划**多联** BFS 不会联的表。
    """
    explicit = _compile({
        "tables": ["account", "client", "district"],
        "joins": _CHAIN_JOINS,
        "answer_columns": ["account.account_id", "client.client_id"],
    }, ["account", "client", "district"])
    bfs = _compile({
        "tables": ["account", "client", "district"],
        "joins": "",
        "answer_columns": ["account.account_id", "client.client_id"],
    }, ["account", "client", "district"])
    assert _sql(explicit) == _sql(bfs)


def test_needed_covers_all_explicit_tables_nothing_pruned():
    """needed 覆盖全部显式表 → 一条不剪(剪枝只删无引用的叶子)。"""
    plan = {
        "tables": ["account", "client", "district"],
        "joins": _CHAIN_JOINS,
        "answer_columns": ["account.account_id", "client.client_id", "district.A2"],
    }
    res = _compile(plan, ["account", "client", "district"])
    assert _sql(res) == (
        "SELECT account.account_id, client.client_id, district.A2\n"
        "FROM account\n"
        "JOIN client ON account.client_id = client.client_id\n"
        "JOIN district ON client.district_id = district.district_id"
    )


def test_degree_two_bridge_dimension_not_pruned():
    """度数 ≥ 2 的维度是**桥**(连接两棵子树)→ 不剪(剪它会断开树)。"""
    plan = {
        "tables": ["account", "client", "district", "region"],
        "joins": _CHAIN_JOINS + " AND district.region_id = region.region_id",
        "answer_columns": ["account.account_id", "region.name"],
    }
    res = _compile(plan, ["account", "client", "district", "region"])
    sql = _sql(res)
    # district 夹在 client 与 region 之间(度数 2)→ 保留;region 被引用 → 保留
    assert "JOIN district ON client.district_id = district.district_id" in sql
    assert "JOIN region ON district.region_id = region.region_id" in sql


# ══ 剪枝发生在基数守卫**之前**(多余 M:N 叶子不再误报行倍增)════


def test_unneeded_mn_leaf_no_longer_fan_out():
    """多余的 M:N 叶子被剪掉后不再进基数守卫 → 不再误报 fan_out。"""
    plan = {
        "tables": ["account", "client", "district"],
        "joins": _CHAIN_JOINS,
        "answer_columns": ["account.account_id", "client.client_id"],
    }
    res = _compile(plan, ["account", "client", "district"],
                   model=_model(district_card="M:N"))
    assert _sql(res) == (
        "SELECT account.account_id, client.client_id\n"
        "FROM account\n"
        "JOIN client ON account.client_id = client.client_id"
    )


def test_needed_mn_edge_still_fan_out():
    """反向:M:N 边被组件引用(needed)→ 照常进守卫,fan_out 硬 MISS 不豁免。"""
    plan = {
        "tables": ["account", "client", "district"],
        "joins": _CHAIN_JOINS,
        "answer_columns": ["account.account_id", "client.client_id", "district.A2"],
    }
    res = _compile(plan, ["account", "client", "district"],
                   model=_model(district_card="M:N"))
    assert isinstance(res, CompileMiss), res
    assert res.reason == "fan_out"


# ══ 保守边界:锚表连边不剪 / 断树不剪 ═══════════════════════


def test_anchor_incident_edges_never_pruned():
    """锚表(FROM 根)的声明连边一律保留 —— 即使该表无人引用。

    锚表的连边定义查询的粒度与行集(内连接会筛行),而且剪掉它会剪空边表;
    那条路径**不回退 BFS**(规格要求保持今日硬 MISS 语义),所以规则上直接
    保护锚边,让"FROM 锚表 + 树序 JOIN"的骨架不变量恒成立。
    """
    plan = {
        "tables": ["account", "client"],
        "joins": "account.client_id = client.client_id",
        "answer_columns": ["account.frequency"],
    }
    res = _compile(plan, ["account", "client"])
    assert _sql(res) == (
        "SELECT account.frequency\n"
        "FROM account\n"
        "JOIN client ON account.client_id = client.client_id"
    )


def test_disconnected_edges_not_pruned_hard_miss():
    """显式边集不连通(两棵互不相连的树)→ 整体跳过剪枝 → 维持硬 MISS。

    第二棵(district-region)虽然没人引用,但"哪棵多余"无从判断:剪掉一整棵
    等于替计划猜它想联哪个组件。计划声明的路由整体不成树 = 计划坏了,
    ``ambiguous_join_path`` 是它的正确归宿(与 BFS 通道的严格性一致)。
    """
    plan = {
        "tables": ["account", "client", "district", "region"],
        "joins": ("account.client_id = client.client_id, "
                  "district.region_id = region.region_id"),
        "answer_columns": ["account.account_id", "client.client_id"],
    }
    res = _compile(plan, ["account", "client", "district", "region"])
    assert isinstance(res, CompileMiss), res
    assert res.reason == "ambiguous_join_path"


def test_needed_table_in_pruned_component_still_hard_miss():
    """断树里被引用的表同样不救:组件不可达 → 硬 MISS(不猜、不静默丢引用)。"""
    plan = {
        "tables": ["account", "client", "district", "region"],
        "joins": ("account.client_id = client.client_id, "
                  "district.region_id = region.region_id"),
        "answer_columns": ["account.account_id", "region.name"],
    }
    res = _compile(plan, ["account", "client", "district", "region"])
    assert isinstance(res, CompileMiss), res
    assert res.reason == "ambiguous_join_path"
