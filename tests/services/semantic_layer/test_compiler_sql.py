"""SemanticCompiler tests: constrained-selection SQL compilation.

plan 的 metric/group_by/filters 全部落到已声明模型条目时才编译;任一项
不在「逻辑宇宙」内 → 严格 MISS(None)。聚合项必须命中声明 metric。
"""
import pytest

from trove.services.semantic_layer.compiler import SemanticCompiler
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
    SemanticRelationship,
)


def _field(name, datatype=None):
    return SemanticField(name=name, expression=name, datatype=datatype)


def _demo_model():
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"), _field("amount"),
                _field("status"), _field("date", "Date"),
            ]),
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("district_id"), _field("frequency"),
            ]),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A3"),
            ]),
        ],
        relationships=[
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=["account_id"], to_columns=["account_id"],
                                 cardinality="1:N"),
            SemanticRelationship("account_to_district", "account", "district",
                                 from_columns=["district_id"], to_columns=["district_id"],
                                 cardinality="1:N"),
        ],
        metrics=[
            SemanticMetric("number of loan records", "COUNT(loan.loan_id)", datasets=["loan"]),
            SemanticMetric("total_loan_amount", "SUM(loan.amount)", datasets=["loan"]),
            SemanticMetric("avg_loan_amount", "AVG(loan.amount)", datasets=["loan"]),
        ],
    )


def _compile(plan, matched, model=None):
    return SemanticCompiler(model or _demo_model()).compile_from_plan(plan, matched)


def test_aggregate_by_dimension_with_filter():
    plan = {
        "tables": ["loan", "account", "district"],
        "aggregation": "avg(loan.amount)",
        "answer_columns": ["district.A3", "avg(loan.amount)"],
        "conditions": [{"field": "district.A3", "op": "=", "value": "Prague"}],
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    sql = result.sql
    assert sql.startswith("SELECT district.A3, AVG(loan.amount)")
    assert "FROM loan" in sql
    assert "JOIN account ON loan.account_id = account.account_id" in sql
    assert "JOIN district ON account.district_id = district.district_id" in sql
    assert "WHERE district.A3 = 'Prague'" in sql
    assert "GROUP BY district.A3" in sql
    assert "authoritative" in result.block


def test_simple_count_single_table():
    plan = {"aggregation": "count(loan.loan_id)", "answer_columns": ["count(loan.loan_id)"]}
    result = _compile(plan, ["loan"])
    assert result is not None
    assert result.sql == "SELECT COUNT(loan.loan_id)\nFROM loan"


def test_count_wildcard_matches_declared_metric():
    # COUNT(*) 与声明 COUNT(loan.loan_id) 签名兼容(空列集通配)
    plan = {"aggregation": "count(*)", "answer_columns": ["count(*)"]}
    result = _compile(plan, ["loan"])
    assert result is not None
    assert "COUNT(loan.loan_id)" in result.sql


def test_metric_filter_merged_into_where():
    """metric 内建 filter(status='A')编译期并入 WHERE,列被表限定。"""
    model = _demo_model()
    model.metrics.append(SemanticMetric(
        "active_loan_count", "COUNT(loan.loan_id)",
        datasets=["loan"], filter="status = 'A'",
    ))
    plan = {
        "aggregation": "active_loan_count",
        "answer_columns": ["active_loan_count"],
    }
    result = _compile(plan, ["loan"], model=model)
    assert result is not None
    assert "WHERE (loan.status = 'A')" in result.sql


def test_metric_filter_respects_declared_fields():
    """filter 列必须是声明字段;未声明的列 → 严格 MISS 不猜。"""
    model = _demo_model()
    model.metrics.append(SemanticMetric(
        "bad_filter_count", "COUNT(loan.loan_id)",
        datasets=["loan"], filter="ghost_col = 'x'",
    ))
    plan = {
        "aggregation": "bad_filter_count",
        "answer_columns": ["bad_filter_count"],
    }
    assert _compile(plan, ["loan"], model=model) is None


# ── 枚举值归一(值留在字段层,编译期归一成 code)────────────────


def _client_model():
    """带 enum 字段 gender 的模型(F/M + 可读词)。"""
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"),
                SemanticField(name="gender", expression="gender",
                              datatype="String", semantic_role="enum",
                              enum_display={"F": "female", "M": "male"}),
            ]),
        ],
        metrics=[
            SemanticMetric("number of clients", "COUNT(client.client_id)",
                           datasets=["client"]),
        ],
    )


def test_enum_value_normalized_to_code():
    """plan 写可读词 male → 编译归一成 code 'M'。"""
    model = _client_model()
    plan = {
        "tables": ["client"],
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "male"}],
    }
    result = _compile(plan, ["client"], model=model)
    assert result is not None
    assert "WHERE client.gender = 'M'" in result.sql


def test_enum_value_code_passthrough():
    """plan 直接写 code 'm'(大小写不敏感)→ 保持库里存的写法 'M'。"""
    model = _client_model()
    plan = {
        "tables": ["client"],
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "m"}],
    }
    result = _compile(plan, ["client"], model=model)
    assert result is not None
    assert "WHERE client.gender = 'M'" in result.sql


def test_enum_value_list_normalized():
    """IN 列表逐元素归一。"""
    model = _client_model()
    plan = {
        "tables": ["client"],
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "conditions": [{"field": "client.gender", "op": "in",
                        "value": "('female', 'M')"}],
    }
    result = _compile(plan, ["client"], model=model)
    assert result is not None
    assert "client.gender IN ('F', 'M')" in result.sql


def test_enum_value_unresolved_is_strict_miss():
    """值不在声明词表 → 保守 MISS(绝不静默产出 0 行 SQL)。"""
    model = _client_model()
    plan = {
        "tables": ["client"],
        "aggregation": "number of clients",
        "answer_columns": ["number of clients"],
        "conditions": [{"field": "client.gender", "op": "=", "value": "x"}],
    }
    res = SemanticCompiler(model).compile_detailed(plan, ["client"])
    from trove.services.semantic_layer.compiler import CompileMiss
    assert isinstance(res, CompileMiss)
    assert res.reason == "enum_value_unresolved"


def test_enum_value_ignored_when_no_enum_display():
    """未声明 enum_display 的字段 → 值原样透传(旧行为不变)。"""
    plan = {
        "tables": ["loan"],
        "aggregation": "number of loan records",
        "answer_columns": ["number of loan records"],
        "conditions": [{"field": "loan.status", "op": "=", "value": "A"}],
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert "WHERE loan.status = 'A'" in result.sql


# ── 权威联表路径(plan.joins, MetricFlow 显式路径)────────────────


def _diamond_model():
    """共享维度菱形:client/account 同连 district(第二条路由)。

    client↔account 有两条简单路径:client—disp—account 与
    client—district—account。BFS 先到先得不可审计 → 无 joins 时严格 MISS;
    显式 joins 选边后可编译。
    """
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="client", primary_key=["client_id"], fields=[
                _field("client_id"), _field("district_id"),
            ]),
            SemanticDataset(name="account", primary_key=["account_id"], fields=[
                _field("account_id"), _field("district_id"),
            ]),
            SemanticDataset(name="disp", primary_key=["disp_id"], fields=[
                _field("disp_id"), _field("client_id"), _field("account_id"),
                _field("type"),
            ]),
            SemanticDataset(name="district", primary_key=["district_id"], fields=[
                _field("district_id"), _field("A3"),
            ]),
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                _field("loan_id"), _field("account_id"),
            ]),
        ],
        relationships=[
            SemanticRelationship("disp_to_client", "disp", "client",
                                 from_columns=["client_id"], to_columns=["client_id"],
                                 cardinality="1:N"),
            SemanticRelationship("disp_to_account", "disp", "account",
                                 from_columns=["account_id"], to_columns=["account_id"],
                                 cardinality="1:N"),
            SemanticRelationship("client_to_district", "client", "district",
                                 from_columns=["district_id"], to_columns=["district_id"],
                                 cardinality="1:N"),
            SemanticRelationship("account_to_district", "account", "district",
                                 from_columns=["district_id"], to_columns=["district_id"],
                                 cardinality="1:N"),
            SemanticRelationship("loan_to_account", "loan", "account",
                                 from_columns=["account_id"], to_columns=["account_id"],
                                 cardinality="1:N"),
        ],
        metrics=[
            SemanticMetric("number of clients", "COUNT(client.client_id)",
                           datasets=["client"]),
            SemanticMetric("number of loans", "COUNT(loan.loan_id)",
                           datasets=["loan"]),
        ],
    )


def test_no_joins_ignores_spurious_shared_dimension_route():
    """无显式 joins 时,绕经**查询未涉及**表的虚假路由不计入二义。

    查询引用 client/loan(plan tables 含 account,但 district 未引用);BFS
    树本身正确(client→disp→account→loan);共享维度 district 的第二路由是
    虚假的 → 应编译而非 ambiguous_join_path。
    """
    plan = {
        "tables": ["client", "account", "loan", "district"],
        "answer_columns": ["client.client_id", "loan.loan_id"],
    }
    result = _compile(plan, ["client", "loan"], model=_diamond_model())
    assert result is not None
    assert "FROM client" in result.sql
    assert "JOIN account" in result.sql
    assert "JOIN loan ON loan.account_id = account.account_id" in result.sql


def test_no_joins_genuine_ambiguity_still_miss():
    """真二义:查询组件**同时引用** client+account+district+disp(四条路由表
    全在 needed 内)→ BFS 无法判定走 client 的 district 还是 account 的
    district → 仍严格 MISS,防静默错答。

    若路由表未被组件引用(如 district 只是 query_sketch 误列),绕经它们的虚假
    路由不计入二义 → 编译(见 test_no_joins_ignores_spurious_shared_dimension_route)。
    """
    plan = {
        "tables": ["client", "account", "district", "disp"],
        "answer_columns": ["client.client_id", "account.account_id", "district.A3"],
        "conditions": [{"field": "disp.type", "op": "=", "value": "OWNER"}],
    }
    res = SemanticCompiler(_diamond_model()).compile_detailed(
        plan, ["client", "account", "district", "disp"], force_dialect="mysql")
    from trove.services.semantic_layer.compiler import CompileMiss
    assert isinstance(res, CompileMiss)
    assert res.reason == "ambiguous_join_path"


def test_explicit_joins_resolve_diamond():
    """显式 joins 选边(client→disp→account→loan)→ 编译通过,不删关系。"""
    plan = {
        "tables": ["client", "disp", "account", "loan"],
        "joins": ("disp.client_id = client.client_id, "
                  "disp.account_id = account.account_id, "
                  "loan.account_id = account.account_id"),
        "answer_columns": ["client.client_id"],
    }
    result = _compile(plan, ["client", "loan"], model=_diamond_model())
    assert result is not None
    assert "FROM client" in result.sql
    assert "JOIN disp ON disp.client_id = client.client_id" in result.sql
    assert "JOIN account ON disp.account_id = account.account_id" in result.sql
    assert "JOIN loan ON loan.account_id = account.account_id" in result.sql


def test_explicit_joins_commas_and_and_parsed():
    """joins 支持逗号与 AND 两种分隔(query_sketch 输出形态)。"""
    plan = {
        "tables": ["client", "disp", "account", "loan"],
        "joins": ("disp.client_id = client.client_id AND "
                  "disp.account_id = account.account_id, "
                  "loan.account_id = account.account_id"),
        "answer_columns": ["client.client_id"],
    }
    result = _compile(plan, ["client", "loan"], model=_diamond_model())
    assert result is not None
    assert "FROM client" in result.sql


def test_explicit_joins_undeclared_edge_strict_miss():
    """显式 joins 引用未声明边 → 严格 MISS,不静默忽略后 BFS 改道。"""
    plan = {
        "tables": ["client", "loan"],
        "joins": "client.district_id = loan.loan_id",
        "answer_columns": ["client.client_id"],
    }
    res = SemanticCompiler(_diamond_model()).compile_detailed(
        plan, ["client", "loan"], force_dialect="mysql")
    from trove.services.semantic_layer.compiler import CompileMiss
    assert isinstance(res, CompileMiss)
    assert res.reason == "ambiguous_join_path"


def test_explicit_joins_placeholder_falls_back():
    """占位/空 joins 视为未声明 → 回退 BFS(虚假共享维度路由不计入 → 编译)。"""
    for placeholder in ("", "(empty if none)", "none"):
        plan = {
            "tables": ["client", "account", "loan"],
            "joins": placeholder,
            "answer_columns": ["client.client_id"],
        }
        result = _compile(plan, ["client", "loan"], model=_diamond_model())
        assert result is not None
        assert "FROM client" in result.sql


def test_explicit_joins_disconnected_tree_miss():
    """joins 成两棵断树(anchor 连不上全部)→ 严格 MISS。"""
    plan = {
        "tables": ["client", "disp", "loan", "account"],
        "joins": ("disp.client_id = client.client_id, "
                  "loan.account_id = account.account_id"),
        "answer_columns": ["client.client_id"],
    }
    res = SemanticCompiler(_diamond_model()).compile_detailed(
        plan, ["client", "loan"], force_dialect="mysql")
    from trove.services.semantic_layer.compiler import CompileMiss
    assert isinstance(res, CompileMiss)
    assert res.reason == "ambiguous_join_path"


def test_metric_filter_rejects_subquery():
    model = _demo_model()
    model.metrics.append(SemanticMetric(
        "weird_count", "COUNT(loan.loan_id)",
        datasets=["loan"], filter="account_id IN (SELECT account_id FROM account)",
    ))
    plan = {
        "aggregation": "weird_count",
        "answer_columns": ["weird_count"],
    }
    assert _compile(plan, ["loan"], model=model) is None


def test_agg_time_dimension_preferred_in_resolve_time_field():
    """agg_time_dimension 声明优先:多个时间字段也能判定,不再要求唯一。"""
    from trove.services.semantic_layer.compiler import resolve_time_field

    model = _demo_model()
    model.datasets[0].fields.append(_field("updated_at", "DateTime"))
    resolved = resolve_time_field(model, ["loan"], preferred="loan.date")
    assert resolved is not None
    assert resolved[0] == "loan"
    assert resolved[1].name == "date"
    # 无 preferred 时多个时间字段仍无法判定(不猜)
    assert resolve_time_field(model, ["loan"]) is None


def test_list_query_projects_fields():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.status", "op": "=", "value": "A"}],
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert result.sql == "SELECT loan.status\nFROM loan\nWHERE loan.status = 'A'"


def test_aggregation_none_marker_zh_compiles_list_query():
    """query_sketch 中文模式把 aggregation 标为「无」(=none),编译器不得误判为
    真实聚合而 no_metric_match。回归:compiler 的 none 标记集需与
    query_sketch 的 ("none", "无") 对齐。"""
    plan = {
        "tables": ["loan"],
        "aggregation": "无",
        "answer_columns": ["loan.status"],
        "conditions": [],
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert result.sql == "SELECT loan.status\nFROM loan"


def test_two_filters_combined_with_and():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [
            {"field": "loan.status", "op": "=", "value": "A"},
            {"field": "loan.amount", "op": ">=", "value": 10},
        ],
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert "WHERE loan.status = 'A' AND loan.amount >= 10" in result.sql


def test_string_value_escaped():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.status", "op": "=", "value": "O'Brien"}],
    }
    result = _compile(plan, ["loan"])
    assert "loan.status = 'O''Brien'" in result.sql


@pytest.mark.parametrize("op", ["like", "in", "<>", "to", "between"])
def test_unknown_operator_is_strict_miss(op):
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.status", "op": op, "value": "A"}],
    }
    if op in {"like", "in", "<>"}:
        assert _compile(plan, ["loan"]) is not None
    else:
        assert _compile(plan, ["loan"]) is None


def test_metric_not_in_declared_is_strict_miss():
    plan = {"aggregation": "sum(loan.ghost_col)", "answer_columns": ["sum(loan.ghost_col)"]}
    assert _compile(plan, ["loan"]) is None


def test_aggregate_without_declared_metric_is_miss():
    # 声明了聚合(avg over trans.amount)但模型里没有该 metric → 严格 MISS
    plan = {"aggregation": "avg(trans.amount)", "answer_columns": ["avg(trans.amount)"]}
    assert _compile(plan, ["trans", "loan"]) is None


def test_group_by_field_not_declared_is_miss():
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["district.A3", "count(loan.loan_id)"],
    }
    # A3 未声明(dataset 无 A3 field)→ 分组维度不在宇宙内 → MISS
    model = _demo_model()
    model.datasets[2].fields = []  # district 无任何 field
    plan["group_by_dim"] = True
    assert SemanticCompiler(model).compile_from_plan(plan, ["loan", "district", "account"]) is None


def test_filter_field_not_declared_is_miss():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.ghost", "op": "=", "value": "x"}],
    }
    assert _compile(plan, ["loan"]) is None


def test_empty_or_single_table_returns_none():
    assert _compile({}, []) is None
    assert _compile({"answer_columns": ["loan.status"], "conditions": [
        {"field": "loan.amount", "op": ">", "value": 1}], }, None) is None


def test_condition_without_value_is_miss():
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "loan.status", "op": "=", "value": None}],
    }
    assert _compile(plan, ["loan"]) is None


def test_astral_table_anchor_falls_back_to_first_matched():
    # metric 锚定 loan,但贷方不在 matched → 兜底 matched[0] 且不产生无效引用
    plan = {"aggregation": "count(loan.loan_id)", "answer_columns": ["count(loan.loan_id)"]}
    result = _compile(plan, ["district"])
    # loan 不在 matched → 回到 district,仍编译(FROM district)但不引用 loan 会很怪
    # —— strict 语义下这种情况当场 MISS(表不在 matched 就是未覆盖)
    assert result is None


def test_resolve_field_via_synonym_alias():
    """query_sketch 直接用别名写列(district.region)→ 唯一命中同数据集字段 A3。"""
    model = _demo_model()
    district = model.datasets[2]
    a3 = next(f for f in district.fields if f.name == "A3")
    a3.synonyms = ["region", "area"]

    plan = {
        "aggregation": "avg(loan.amount)",
        "answer_columns": ["district.region", "avg(loan.amount)"],
        "conditions": [{"field": "district.region", "op": "=", "value": "Prague"}],
    }
    result = _compile(plan, ["loan", "district", "account"], model=model)
    assert result is not None
    sql = result.sql
    assert "district.A3" in sql
    assert "district.region" not in sql
    assert "WHERE district.A3 = 'Prague'" in sql
    assert "GROUP BY district.A3" in sql


def test_synonym_ambiguous_is_miss():
    """两个字段共享同一 synonym → 歧义,不猜,转 LLM 通道。"""
    model = _demo_model()
    district = model.datasets[2]
    a3 = next(f for f in district.fields if f.name == "A3")
    a3.synonyms = ["region"]
    district.fields.append(_field("A4"))
    district.fields[-1].synonyms = ["region"]

    plan = {
        "aggregation": "avg(loan.amount)",
        "answer_columns": ["district.region", "avg(loan.amount)"],
    }
    assert _compile(plan, ["loan", "district", "account"], model=model) is None

def test_compile_strict_miss_on_many_to_many_path():
    """P5.2:M:N 边在联路径 → 编译期拒,严格 MISS 回 LLM 通道。"""
    from trove.services.semantic_layer.models import SemanticRelationship

    model = _demo_model()
    model.relationships.append(
        SemanticRelationship("trans_to_card_m2n", "trans", "card",
                             from_columns=["disp_id"], to_columns=["disp_id"],
                             cardinality="M:N"))
    # matched 含 card(仅经 M:N 路径可达)→ fan_out → None
    plan = {
        "aggregation": "count(trans.trans_id)",
        "answer_columns": ["count(trans.trans_id)"],
    }
    assert _compile(plan, ["trans", "card"], model=model) is None


def test_compile_still_ok_when_m2n_unrelated():
    """M:N 边存在但与查询无关(剪枝后不在路径上)→ 正常编译。"""
    from trove.services.semantic_layer.models import SemanticRelationship

    model = _demo_model()
    model.relationships.append(
        SemanticRelationship("order_to_card_m2n", "order", "card",
                             from_columns=["disp_id"], to_columns=["disp_id"],
                             cardinality="M:N"))
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["count(loan.loan_id)"],
    }
    assert _compile(plan, ["loan"], model=model) is not None


# ── P0: 列可达性 / 基数未知 / guardrail 列级校验 ─────────────


def test_projection_on_unconnected_matched_table_is_miss():
    """P0-1:matched 含 district 但模型无到 district 的关系边 → FROM loan
    无 JOIN,投影 district.A3 不可达 → 投影表守卫严格 MISS(不产笛卡尔 SQL)。"""
    model = _demo_model()
    model.relationships = []  # 移除全部关系边
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["district.A3", "count(loan.loan_id)"],
    }
    assert SemanticCompiler(model).compile_from_plan(plan, ["loan", "district"]) is None


def test_filter_on_unconnected_matched_table_is_miss():
    """P0-1:过滤条件引用无关系边的表 → 同样不可达 → 严格 MISS。"""
    model = _demo_model()
    model.relationships = []
    plan = {
        "answer_columns": ["loan.status"],
        "conditions": [{"field": "district.A3", "op": "=", "value": "Prague"}],
    }
    assert SemanticCompiler(model).compile_from_plan(plan, ["loan", "district"]) is None


def test_compile_miss_on_unknown_cardinality():
    """P0-3:联路径上出现未声明基数的关系 → 保守 MISS(不赌 many→one)。"""
    model = _demo_model()
    model.relationships[1] = SemanticRelationship(
        "account_to_district", "account", "district",
        from_columns=["district_id"], to_columns=["district_id"])  # 空基数
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["district.A3", "count(loan.loan_id)"],
    }
    assert SemanticCompiler(model).compile_from_plan(
        plan, ["loan", "district", "account"]) is None


def test_compile_ok_when_cardinality_declared():
    """P0-3:显式声明 1:N → 编译正常(不再默认安全,但声明过就放行)。"""
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": ["district.A3", "count(loan.loan_id)"],
    }
    assert _compile(plan, ["loan", "district", "account"]) is not None


def test_compile_miss_on_ambiguous_join_path():
    """P2:root→matched 双路由,且两路由的表都被组件引用 → 严格 MISS。

    查询同时引用 loan/district/account/client:district 可从 loan→account 与
    loan→client 两条路由到达(BFS 无法判定走哪条)→ 不先到先得地猜。
    若 account/client 未被引用,绕经它们的第二路由是虚假的 → 编译(见
    test_no_joins_ignores_spurious_shared_dimension_route)。
    """
    model = _demo_model()
    model.datasets.append(SemanticDataset(name="client", primary_key=["client_id"], fields=[
        _field("client_id"), _field("district_id"),
    ]))
    model.relationships.append(
        SemanticRelationship("loan_to_client", "loan", "client",
                             from_columns=["account_id"], to_columns=["account_id"],
                             cardinality="1:N"))
    model.relationships.append(
        SemanticRelationship("client_to_district", "client", "district",
                             from_columns=["district_id"], to_columns=["district_id"],
                             cardinality="1:N"))
    plan = {
        "aggregation": "count(loan.loan_id)",
        "answer_columns": [
            "district.A3", "count(loan.loan_id)",
            "account.account_id", "client.client_id",
        ],
    }
    assert _compile(
        plan, ["loan", "district", "account", "client"], model=model) is None


def test_guardrail_flags_columns_not_in_from_join():
    """P0-2:表在 allowed(声明集)但没 JOIN → 列级校验拦截(非笛卡尔/非法)。"""
    from trove.services.semantic_layer.compiler import validate_compiled_sql

    sql = "SELECT district.A3, COUNT(loan.loan_id)\nFROM loan"
    violations = validate_compiled_sql(sql, _demo_model(), ["loan", "district"])
    assert any("not in FROM/JOIN" in v for v in violations)


def test_guardrail_ok_when_columns_joined():
    """P0-2:正常 JOIN 覆盖引用列 → guardrail 放行。"""
    from trove.services.semantic_layer.compiler import validate_compiled_sql

    sql = (
        "SELECT district.A3, COUNT(loan.loan_id)\nFROM loan\n"
        "JOIN account ON loan.account_id = account.account_id\n"
        "JOIN district ON account.district_id = district.district_id"
    )
    assert validate_compiled_sql(sql, _demo_model(), ["loan", "district", "account"]) == []


# ── P1-4: 时间维度绑定 ────────────────────────────────────


def test_resolve_time_field_unique_by_datatype():
    """时态 datatype 未显式 is_time → datatype 默认即时间维度(唯一 → 可绑定)。"""
    from trove.services.semantic_layer.compiler import resolve_time_field

    model = _demo_model()
    model.datasets[0].fields = [_field("loan_id"), _field("date", "Date")]  # is_time 未设
    resolved = resolve_time_field(model, ["loan"])
    assert resolved is not None
    ds, field = resolved
    assert ds == "loan" and field.name == "date"


def test_resolve_time_field_ambiguous_is_none():
    """多个时间字段(created_at/updated_at)→ 无法判定 → None,不猜。"""
    from trove.services.semantic_layer.compiler import resolve_time_field

    model = _demo_model()
    model.datasets[0].fields = [
        _field("loan_id"),
        _field("created_at", "Date"),
        _field("updated_at", "Date"),
    ]
    assert resolve_time_field(model, ["loan"]) is None


def test_resolve_time_field_missing_is_none():
    """matched 无时间字段 → None。"""
    from trove.services.semantic_layer.compiler import resolve_time_field

    model = _demo_model()
    model.datasets[0].fields = [_field("loan_id"), _field("amount")]
    assert resolve_time_field(model, ["loan"]) is None


# ── 多度量 / 派生度量 ──────────────────────────────────────

def test_multi_metric_projections_in_answer_order():
    plan = {
        "aggregation": "avg(loan.amount)",
        "answer_columns": ["district.A3", "avg(loan.amount)", "count(loan.loan_id)"],
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    assert result.sql.startswith(
        "SELECT district.A3, AVG(loan.amount), COUNT(loan.loan_id)")
    assert "GROUP BY district.A3" in result.sql


def test_multi_metric_one_unmatched_is_strict_miss():
    plan = {
        "answer_columns": ["district.A3", "avg(loan.amount)", "sum(loan.ghost)"],
    }
    assert _compile(plan, ["loan", "district", "account"]) is None


def test_multi_metric_dedupes_aggregation_and_answer_column():
    # aggregation 与 answer_columns 同度量 → 只投影一次
    plan = {
        "aggregation": "avg(loan.amount)",
        "answer_columns": ["district.A3", "avg(loan.amount)"],
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    assert result.sql.count("AVG(loan.amount)") == 1


def _derived_model():
    model = _demo_model()
    model.metrics.append(
        SemanticMetric("avg_per_loan",
                       "total_loan_amount / COUNT(loan.loan_id)",
                       datasets=["loan"], metric_type="derived"))
    return model


def test_derived_metric_inlined_by_name_match():
    # aggregation 直接写派生度量名(裸名匹配)→ 编译期内联
    plan = {"aggregation": "avg_per_loan", "answer_columns": ["avg_per_loan"]}
    result = _compile(plan, ["loan"], model=_derived_model())
    assert result is not None
    # sqlite 方言除法渲染带 CAST AS REAL(防整数除法截断)——方言正确行为
    assert result.sql == (
        "SELECT CAST(SUM(loan.amount) AS REAL) / COUNT(loan.loan_id)\nFROM loan")


def test_derived_metric_in_answer_columns():
    plan = {"answer_columns": ["district.A3", "avg_per_loan"]}
    result = _compile(plan, ["loan", "district", "account"], model=_derived_model())
    assert result is not None
    assert "SUM(loan.amount)" in result.sql
    assert "/ COUNT(loan.loan_id)" in result.sql
    assert "GROUP BY district.A3" in result.sql


def test_derived_metric_cycle_is_miss():
    model = _demo_model()
    model.metrics.append(
        SemanticMetric("m_a", "m_b + 1", metric_type="derived"))
    model.metrics.append(
        SemanticMetric("m_b", "m_a + 1", metric_type="derived"))
    plan = {"aggregation": "m_a", "answer_columns": ["m_a"]}
    assert _compile(plan, ["loan"], model=model) is None


def test_derived_metric_unresolved_identifier_is_miss():
    model = _demo_model()
    model.metrics.append(
        SemanticMetric("m_x", "ghost_metric + COUNT(loan.loan_id)",
                       metric_type="derived"))
    plan = {"aggregation": "m_x", "answer_columns": ["m_x"]}
    assert _compile(plan, ["loan"], model=model) is None


def test_derived_metric_depth_guard_is_miss():
    model = _demo_model()
    model.metrics.append(
        SemanticMetric("m_d0", "COUNT(loan.loan_id)", metric_type="derived"))
    for i in range(1, 7):
        model.metrics.append(
            SemanticMetric(f"m_d{i}", f"m_d{i - 1} + 0", metric_type="derived"))
    plan = {"aggregation": "m_d6", "answer_columns": ["m_d6"]}
    assert _compile(plan, ["loan"], model=model) is None


def test_derived_metric_referencing_unjoined_table_is_miss():
    # 派生度量内联出 loan 但 matched 只有 district → 投影表守卫 MISS
    model = _derived_model()
    plan = {"aggregation": "avg_per_loan", "answer_columns": ["avg_per_loan"]}
    assert _compile(plan, ["district"], model=model) is None


def test_ratio_type_inlines_like_derived():
    model = _demo_model()
    model.metrics.append(
        SemanticMetric("amount_per_record",
                       "total_loan_amount / COUNT(loan.loan_id)",
                       datasets=["loan"], metric_type="ratio"))
    plan = {"aggregation": "amount_per_record", "answer_columns": ["amount_per_record"]}
    result = _compile(plan, ["loan"], model=model)
    assert result is not None
    assert "SUM(loan.amount)" in result.sql
    assert "/ COUNT(loan.loan_id)" in result.sql


def test_simple_metric_type_keeps_expression_verbatim():
    # 非派生度量(simple/空)表达式原样,不经过 sqlglot 重渲染
    model = _demo_model()
    plan = {"aggregation": "avg(loan.amount)", "answer_columns": ["avg(loan.amount)"]}
    result = _compile(plan, ["loan"], model=model)
    assert result is not None
    assert "AVG(loan.amount)" in result.sql


# ── 编译照抄校验(compiled_sql_matches)───────────────────────


def test_compiled_sql_matches_ignores_formatting():
    """空白/大小写/引号风格差异 → 等价(照抄校验不拦格式级微调)。"""
    from trove.services.semantic_layer.compiler import compiled_sql_matches

    ok, why = compiled_sql_matches(
        "SELECT COUNT(loan.loan_id)\nFROM loan",
        "select count(loan.loan_id) from loan",
        "sqlite",
    )
    assert ok is True
    assert why == ""


def test_compiled_sql_matches_tolerates_count_normalization():
    """COUNT(*) vs COUNT(col) 语义等价(编译器把 count(*) 归一到声明度量)——
    结构签名相同,不误判为偏离。"""
    from trove.services.semantic_layer.compiler import compiled_sql_matches

    ok, _ = compiled_sql_matches(
        "SELECT COUNT(loan.loan_id)\nFROM loan",
        "SELECT COUNT(*) FROM loan",
        "sqlite",
    )
    assert ok is True


def test_compiled_sql_matches_rejects_structural_deviation():
    """改聚合函数 / 改过滤值 / 改投影宽度 / 换表 → 结果形状必然改变 → 打回。"""
    from trove.services.semantic_layer.compiler import compiled_sql_matches

    base = "SELECT COUNT(loan.loan_id) FROM loan"
    assert compiled_sql_matches(base, "SELECT SUM(loan.amount) FROM loan", "sqlite")[0] is False
    assert compiled_sql_matches(base, "SELECT COUNT(loan.loan_id) FROM trans", "sqlite")[0] is False
    assert compiled_sql_matches(
        "SELECT a FROM t WHERE x = 'A'",
        "SELECT a FROM t WHERE x = 'a'", "sqlite")[0] is False
    assert compiled_sql_matches(
        "SELECT a, b FROM t", "SELECT a FROM t", "sqlite")[0] is False


def test_compiled_sql_matches_conservative_passthrough():
    """保守方向:跨方言 / 解析失败 / 空 SQL → 放行(不误伤合法微调)。"""
    from trove.services.semantic_layer.compiler import compiled_sql_matches

    assert compiled_sql_matches(
        "SELECT COUNT(x) FROM t", "SELECT SUM(y) FROM t", "mysql")[0] is True
    assert compiled_sql_matches("SELECT 1", "not a query", "sqlite")[0] is True
    assert compiled_sql_matches("", "", "sqlite")[0] is True
    assert compiled_sql_matches("SELECT COUNT(x) FROM t", "", "sqlite")[0] is True


# ── 时间粒度分桶 ─────────────────────────────────────────

def test_time_grain_month_replaces_projection_and_group_by():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["loan.date", "sum(loan.amount)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert result.sql == (
        "SELECT strftime('%Y-%m', loan.date), SUM(loan.amount)\n"
        "FROM loan\n"
        "GROUP BY strftime('%Y-%m', loan.date)")


def test_time_grain_mysql_dialect_changes_only_bucketed_expr():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["loan.date", "sum(loan.amount)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
    }
    result = SemanticCompiler(_demo_model()).compile_from_plan(
        plan, ["loan"], force_dialect="mysql")
    assert result is not None
    assert "DATE_FORMAT(loan.date, '%Y-%m')" in result.sql
    assert "SUM(loan.amount)" in result.sql


def test_time_grain_field_not_declared_is_miss():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["loan.ghost_date", "sum(loan.amount)"],
        "time_grain": {"field": "loan.ghost_date", "grain": "year"},
    }
    assert _compile(plan, ["loan"]) is None


def test_time_grain_non_temporal_field_is_miss():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["loan.amount", "sum(loan.amount)"],
        "time_grain": {"field": "loan.amount", "grain": "year"},
    }
    assert _compile(plan, ["loan"]) is None


def test_time_grain_bad_grain_is_miss():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["loan.date", "sum(loan.amount)"],
        "time_grain": {"field": "loan.date", "grain": "fortnight"},
    }
    assert _compile(plan, ["loan"]) is None


def test_time_grain_without_aggregation_is_miss():
    plan = {
        "answer_columns": ["loan.date"],
        "time_grain": {"field": "loan.date", "grain": "year"},
    }
    assert _compile(plan, ["loan"]) is None


def test_time_grain_field_not_in_answer_columns_inserted_after_dims():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["district.A3", "sum(loan.amount)"],
        "time_grain": {"field": "loan.date", "grain": "year"},
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    assert "SELECT district.A3, strftime('%Y', loan.date), SUM(loan.amount)" in result.sql
    assert "GROUP BY district.A3, strftime('%Y', loan.date)" in result.sql


def test_time_grain_compiled_sql_passes_guardrail():
    from trove.services.semantic_layer.compiler import validate_compiled_sql

    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["loan.date", "sum(loan.amount)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert validate_compiled_sql(result.sql, _demo_model(), ["loan"]) == []


# ── HAVING / ORDER BY ────────────────────────────────────

def test_metric_having_after_group_by():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["district.A3", "sum(loan.amount)"],
        "having": [{"metric": "total_loan_amount", "op": ">", "value": 10000}],
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    assert result.sql == (
        "SELECT district.A3, SUM(loan.amount)\n"
        "FROM loan\n"
        "JOIN account ON loan.account_id = account.account_id\n"
        "JOIN district ON account.district_id = district.district_id\n"
        "GROUP BY district.A3\n"
        "HAVING SUM(loan.amount) > 10000")


def test_having_field_folds_into_where():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["district.A3", "sum(loan.amount)"],
        "having": [{"field": "loan.status", "op": "=", "value": "A"}],
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    assert "WHERE loan.status = 'A'" in result.sql
    assert "HAVING" not in result.sql


def test_unknown_having_metric_is_miss():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["sum(loan.amount)"],
        "having": [{"metric": "ghost_metric", "op": ">", "value": 1}],
    }
    assert _compile(plan, ["loan"]) is None


def test_having_both_field_and_metric_is_miss():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["sum(loan.amount)"],
        "having": [{"field": "loan.status", "metric": "total_loan_amount",
                    "op": ">", "value": 1}],
    }
    assert _compile(plan, ["loan"]) is None


def test_having_on_list_question_is_miss():
    plan = {
        "answer_columns": ["loan.status"],
        "having": [{"metric": "total_loan_amount", "op": ">", "value": 1}],
    }
    assert _compile(plan, ["loan"]) is None


def test_ordering_string_form():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["district.A3", "sum(loan.amount)"],
        "ordering": "district.A3 desc",
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    assert result.sql.endswith("ORDER BY district.A3 DESC")


def test_ordering_by_metric_name():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["district.A3", "sum(loan.amount)"],
        "ordering": "total_loan_amount desc",
    }
    result = _compile(plan, ["loan", "district", "account"])
    assert result is not None
    assert result.sql.endswith("ORDER BY SUM(loan.amount) DESC")


def test_ordering_list_of_dicts_form():
    plan = {
        "answer_columns": ["loan.status"],
        "ordering": [{"column": "loan.status", "direction": "desc"}],
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert result.sql.endswith("ORDER BY loan.status DESC")


def test_ordering_unresolvable_column_dropped():
    plan = {
        "answer_columns": ["loan.status"],
        "ordering": "loan.ghost_col desc",
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert "ORDER BY" not in result.sql


def test_ordering_with_time_grain_uses_bucketed_expr():
    plan = {
        "aggregation": "sum(loan.amount)",
        "answer_columns": ["loan.date", "sum(loan.amount)"],
        "time_grain": {"field": "loan.date", "grain": "month"},
        "ordering": "loan.date desc",
    }
    result = _compile(plan, ["loan"])
    assert result is not None
    assert result.sql.endswith("ORDER BY strftime('%Y-%m', loan.date) DESC")
