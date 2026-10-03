"""语义机制批次 · 集成冒烟(真实 KB 模型 + run 日志逐字 plan 形状)。

本文件只测**跨车道交互**,单机制细节由各车道自己的单测覆盖:

- A1 扇出提升(0470)/ PK 签名容忍
- A3c advisory 骨架(0492/0495)
- A4b 值路由重锚(0476)
- A5a 显式 joins 表对修复(0477)
- B3 排序第三档(0483/0487)+ 方向词归一后的形状

上半段 plan fixture 逐字取自 ``~/.trove/runs/<run_id>.log`` 的 ``plan_json``
(每题注释标注 run id);0479 的 plan_json 在日志里被截断,按其上一行未截断的
``[output]`` 块逐字重建。下半段是**手写机制针**(0475/0486/0482/0492/0495):
这几题的机制产物只存在于本批改造之后,run 日志里没有对应形状,故按机制契约
手写并断言编译产物(A① 极值 rank、A② 算式列、A③ 剪枝、A④ 条件所有权)。
gold SQL 只作 env-gated 的 MySQL oracle,绝不入 KB。

零 LLM / 零网络:默认只编译(纯函数)。执行断言需 ``MYSQL_TEST_URL``
(形如 ``mysql://root:root@127.0.0.1:3306/financial``),未设自动跳过。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from trove.services.semantic_layer.compiler import (
    CompileMiss,
    PartialCompile,
    SemanticCompiler,
    is_hard_miss,
)
from trove.services.semantic_layer.ossie import parse_ossie

_KB = (
    Path(__file__).resolve().parents[3]
    / ".trove" / "kb" / "financial" / "semantics.yml"
)

# ---------------------------------------------------------------- plan fixtures

# 0470 · eval-2-1790949108
PLAN_0470 = {
    "tables": ["district", "client"],
    "joins": "client.district_id = district.district_id",
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'F'", "note": "female clients"},
    ],
    "aggregation": "count(distinct district.district_id)",
    "ordering": [],
    "answer_columns": ["count(distinct district.district_id)"],
    "having": [
        {"metric": "avg(district.A11)", "op": ">", "value": 6000},
        {"metric": "avg(district.A11)", "op": "<", "value": 10000},
    ],
    "plan_field": "",
}

# 0476 · eval-3-1790953174 —— 区名 'Sokolov' 被安在 client.district_id(identifier
# 列,值域错配);A4b 应把它重锚到 district.A2(值词表唯一命中)。
PLAN_0476 = {
    "tables": ["account", "client", "disp"],
    "joins": "disp.account_id = account.account_id AND disp.client_id = client.client_id",
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'F'", "note": "female customers"},
        {"field": "client.birth_date", "op": "<", "value": "'1950-01-01'", "note": "born before 1950"},
        {"field": "client.district_id", "op": "=", "value": "'Sokolov'", "note": "stayed in Sokolov"},
    ],
    "aggregation": "count",
    "ordering": [],
    "answer_columns": ["count(client.client_id)"],
    "having": [],
    "plan_field": "",
}

# 0477 · eval-9-1790949282 —— 表对正确(client↔account、account↔district)、列名错;
# A5a 应按声明关系重建路径(client-disp-account-district)。
PLAN_0477 = {
    "tables": ["client", "account", "district"],
    "joins": (
        "client.client_id = account.account_id AND "
        "account.district_id = district.district_id"
    ),
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'F'", "note": "女性客户"},
        {"field": "client.birth_date", "op": "=", "value": "'1976-01-29'", "note": "出生日期 1976/1/29"},
    ],
    "aggregation": "无",
    "ordering": [],
    "answer_columns": ["district.A2"],
    "having": [],
    "plan_field": "",
}

# 0479 · eval-11-1790949291(plan_json 截断,按 [output] 块重建)——唯一投影是
# 三聚合复合式,无声明 metric 命中 → 软缺口(no_metric_match),不是硬拒。
PLAN_0479 = {
    "tables": ["loan", "account", "disp", "client", "trans"],
    "joins": (
        "loan.account_id = account.account_id AND "
        "disp.account_id = account.account_id AND "
        "disp.client_id = client.client_id AND "
        "trans.account_id = account.account_id"
    ),
    "conditions": [
        {"field": "loan.date", "op": "=", "value": "'1993-07-05'", "note": "loan approved on 1993/7/5"},
        {"field": "trans.date", "op": "IN", "value": "('1993-03-22', '1998-12-27')",
         "note": "balances at the two dates"},
    ],
    "aggregation": "无",
    "extreme": {"func": "min", "column": "loan.date", "scope": "全部条件过滤后"},
    "ordering": [],
    "having": [],
    "plan_field": "",
    "answer_columns": [
        "(MAX(CASE WHEN trans.date = '1998-12-27' THEN trans.balance END) "
        "- MAX(CASE WHEN trans.date = '1993-03-22' THEN trans.balance END)) "
        "/ MAX(CASE WHEN trans.date = '1993-03-22' THEN trans.balance END) * 100"
    ],
}

# 0483 · eval-15-1790949366 第 2 轮(第 1 轮列名里还吞着方向词「降序」,
# 由规划侧归一化;此处取归一化后的形状)——排序键是 answer_columns 里的聚合
# 表达式,B3 第三档应内联成 ORDER BY COUNT(account.account_id) DESC。
PLAN_0483 = {
    "tables": ["account", "district", "disp", "client"],
    "joins": (
        "account.district_id = district.district_id AND "
        "disp.account_id = account.account_id AND "
        "disp.client_id = client.client_id"
    ),
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'F'", "note": "female account holders"},
    ],
    "aggregation": "count",
    "ordering": [{"column": "count(account.account_id)", "direction": "desc"}],
    "answer_columns": ["district.A2", "count(account.account_id)"],
    "having": [],
    "limit": 9,
    "plan_field": "",
}

# 0487 · eval-19-1790949426 —— joins 分号分隔;排序列同样吞了方向词(规划侧归一
# 化后为 sum(trans.amount) desc)。现行代码在此报 limit_without_order(排序列
# 解析失败被静默丢弃);排序解析修好后应能编译出 ORDER BY … LIMIT 10。
PLAN_0487 = {
    "tables": ["trans", "account", "district"],
    "joins": (
        "trans.account_id = account.account_id; "
        "account.district_id = district.district_id"
    ),
    "conditions": [
        {"field": "trans.type", "op": "=", "value": "'VYDAJ'", "note": "non-credit card withdrawals"},
        {"field": "trans.date", "op": "LIKE", "value": "'1996-01%'", "note": "January 1996"},
    ],
    "aggregation": "sum",
    "ordering": [{"column": "sum(trans.amount)", "direction": "desc"}],
    "answer_columns": ["district.A2", "sum(trans.amount)"],
    "having": [],
    "limit": 10,
    "plan_field": "",
}

# 0492 · eval-24-1790949553 —— aggregation 是自由文本 + analysis.metric 未声明
# → 软缺口;骨架必须降级为 advisory,否则 gen_sql 被冻结在退化投影上。
PLAN_0492 = {
    "tables": ["account", "client", "district"],
    "joins": (
        "account.district_id = district.district_id AND "
        "client.district_id = district.district_id"
    ),
    "conditions": [
        {"field": "district.A11", "op": ">", "value": "10000", "note": "districts with average salary over 10000"},
    ],
    "aggregation": "share of female clients",
    "ordering": [],
    "answer_columns": ["client.gender"],
    "having": [],
    "analysis": {"type": "share", "metric": "count(client.client_id)", "partition_by": [],
                 "order_by": "", "direction": "asc"},
    "plan_field": "",
}

# 0495 · eval-27-1790949654 —— analysis.metric 未声明 → 软缺口;骨架是 count,
# 且 A1 扇出提升应把它变成 COUNT(DISTINCT client.client_id)(client 在
# client→disp 1:N 边的「1」端)。
PLAN_0495 = {
    "tables": ["client", "disp", "account"],
    "joins": "client.client_id = disp.client_id AND disp.account_id = account.account_id",
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'M'", "note": "男性客户"},
        {"field": "account.frequency", "op": "=", "value": "'POPLATEK TYDNE'", "note": "每周发放对账单"},
    ],
    "aggregation": "count",
    "ordering": [],
    "answer_columns": ["count(client.client_id)"],
    "having": [],
    "analysis": {"type": "share", "metric": "count(client.client_id)", "partition_by": [],
                 "order_by": "", "direction": "asc"},
    "plan_field": "",
}

# --------------------------------------------------------- 机制针(手写形状)
#
# 0475/0486/0482/0492 的真实 plan 产生于机制落地**之前**:run 日志里的形状
# 拿不到"机制能消费"的那一面(0486 的 rank=2、0482 的算式列当时都被静默丢给
# 生成侧,日志里没有成功编译的痕迹)。这里按机制契约**手写**形状,与上面的
# 逐字 fixture 共用同一模型、同一 matched 口径。

# 0475 手写:只有 extreme(min)、没有可编译投影 → 兜底单列 MIN(极值列)
PLAN_0475_HAND = {
    "tables": ["district"],
    "answer_columns": [],
    "extreme": {"func": "min", "column": "district.A11"},
    "conditions": [],
    "ordering": [],
    "having": [],
    "plan_field": "",
}

# 0486 手写:"全区的第二高" → rank=2 行级选择谓词(声明字段 district.A15);
# scope 文本带显式全局标记 → 跨表条件不进子查询(取未过滤集的极值)
PLAN_0486_HAND = {
    "tables": ["district", "client"],
    "joins": "client.district_id = district.district_id",
    "conditions": [{"field": "client.gender", "op": "=", "value": "'F'"}],
    "aggregation": "number of client records",
    "answer_columns": ["number of client records"],
    "extreme": {"func": "max", "column": "district.A15", "rank": 2,
                "scope": "second highest among all districts by A15"},
    "ordering": [],
    "having": [],
    "plan_field": "",
}

# 0482 手写:算式列(失业率 1996 vs 1995 的增幅)——旧投影循环对含 "(" 的
# 无签名列静默跳过;闭语法通道应重建为表限定形态
PLAN_0482_HAND = {
    "tables": ["district"],
    "answer_columns": [
        "district.A2",
        "(district.A13 - district.A12) / district.A12 * 100",
    ],
    "conditions": [],
    "ordering": [],
    "having": [],
    "plan_field": "",
}

# 0492 手写:A③ 剪枝可作用的形状(锚表是 client;account 在显式 joins 里
# 但无人引用 → 应被剪掉);其余组件与逐字 PLAN_0492 同形
PLAN_0492_HAND = {
    "tables": ["client", "district", "account"],
    "joins": (
        "account.district_id = district.district_id AND "
        "client.district_id = district.district_id"
    ),
    "conditions": [
        {"field": "district.A11", "op": ">", "value": "10000",
         "note": "districts with average salary over 10000"},
    ],
    "aggregation": "share of female clients",
    "ordering": [],
    "answer_columns": ["client.gender"],
    "having": [],
    "analysis": {"type": "share", "metric": "count(client.client_id)",
                 "partition_by": [], "order_by": "", "direction": "asc"},
    "plan_field": "",
}

# 0495 手写(A④ 条件所有权):候选命中**声明**的占比度量(自由拼法带
# NULLIF 守卫)→ 其分子谓词的孪生行级条件不得冻结进骨架 WHERE
_SHARE_FEMALE = (
    "SUM(CASE WHEN client.gender = 'F' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)"
)
PLAN_0495_OWN_HAND = {
    "tables": ["client"],
    "conditions": [{"field": "client.gender", "op": "=", "value": "'F'"}],
    "aggregation": (
        "SUM(CASE WHEN client.gender = 'F' THEN 1 ELSE 0 END)"
        " * 100.0 / NULLIF(COUNT(*), 0)"
    ),
    "answer_columns": [_SHARE_FEMALE],
    "ordering": [],
    "having": [],
    "plan_field": "",
}

# 运行时的 matched_tables(取自各 run 日志,原样)。
MATCHED = {
    "0470": ["district", "client", "trans"],
    "0476": ["account", "client", "trans", "disp"],
    "0477": ["account", "client", "district", "trans"],
    "0479": ["loan", "account", "disp", "client", "trans"],
    "0483": ["account", "district", "order", "client", "trans", "disp"],
    "0487": ["card", "district", "trans"],
    "0492": ["account", "client", "district", "trans"],
    "0495": ["client", "account", "card", "order", "disp"],
}


# ------------------------------------------------------------------- helpers

@pytest.fixture(scope="module")
def model():
    if not _KB.exists():
        pytest.skip(f"financial KB not present: {_KB}")
    return parse_ossie(_KB.read_text(encoding="utf-8"), preferred_dialect="mysql")


def _compile(model, qid: str, plan: dict):
    return SemanticCompiler(model).compile_detailed(
        plan, list(MATCHED[qid]), force_dialect="mysql")


def _compile_with(model, plan: dict, matched: list[str]):
    """手写形状:matched 显式给出(不查 MATCHED 表)。"""
    return SemanticCompiler(model).compile_detailed(
        plan, list(matched), force_dialect="mysql")


def _sql(res) -> str:
    return " ".join(res.sql.split())


def _low(res) -> str:
    return _sql(res).lower()


# -------------------------------------------------------------------- tests

def test_0470_count_distinct_fanout_promotion(model):
    """0470:无声明形式的 count(distinct …) 必须编译成功,且联 1:N 边时提升为
    COUNT(DISTINCT)(不提升会按行对数计数:MySQL 实测 2009 vs 正确 69)。"""
    res = _compile(model, "0470", PLAN_0470)
    assert not isinstance(res, CompileMiss), f"soft count-distinct must compile: {res}"
    sql = _low(res)
    assert "count(distinct district.district_id)" in sql
    # 度量 filter(having 里的 avg(district.A11))必须落成 WHERE 谓词
    assert "district.a11" in sql
    assert "6000" in sql and "10000" in sql


def test_0476_value_reroute_to_district_a2(model):
    """0476:字面量 'Sokolov' 唯一命中 district.A2(值词表)且源字段无词表
    → 重锚;并且 join 树必须补上 district(显式 joins 分支不看 needed,
    不补边就会 unreachable_table)。"""
    res = _compile(model, "0476", PLAN_0476)
    assert not isinstance(res, CompileMiss), f"re-anchor must compile: {res}"
    sql = _low(res)
    assert "district.a2 = 'sokolov'" in sql
    assert "client.district_id = 'sokolov'" not in sql
    assert "join district" in sql
    # client 在 client→disp 1:N 边的「1」端 → 计数提升为 DISTINCT
    assert "count(distinct client.client_id)" in sql


def test_0477_explicit_join_table_pair_repair(model):
    """0477:显式 joins 列名错但表对正确 → 按声明关系重建路径;产出必须覆盖
    district 且不再引用不存在的 client.client_id = account.account_id。"""
    res = _compile(model, "0477", PLAN_0477)
    assert not isinstance(res, CompileMiss), f"table-pair repair must compile: {res}"
    sql = _low(res)
    assert "district.a2" in sql
    assert "client.client_id = account.account_id" not in sql
    assert "join district" in sql


def test_0479_soft_only_miss_stays_soft(model):
    """0479:复合表达式无声明 metric 命中 = 软缺口;分类必须保持 soft,
    query_sketch 才能放行给 gen_sql(硬拒只在 is_hard_miss 为真时发)。"""
    res = _compile(model, "0479", PLAN_0479)
    if isinstance(res, CompileMiss):
        assert res.reason == "no_metric_match"
        assert is_hard_miss(res.reason) is False
    else:
        # 若骨架可产出,则必须是 partial(软)而非全量权威
        assert isinstance(res, PartialCompile)


def test_0483_ordering_aggregate_expression_third_tier(model):
    """0483:排序键 = answer_columns 里的聚合表达式 → B3 第三档内联,
    产出 ORDER BY COUNT(...) DESC LIMIT 9,不再是 limit_without_order。"""
    res = _compile(model, "0483", PLAN_0483)
    assert not isinstance(res, CompileMiss), f"ordering must resolve: {res}"
    sql = _low(res)
    assert "order by count(" in sql
    assert " desc" in sql
    assert "limit 9" in sql


def test_0487_ordering_metric_expression(model):
    """0487:排序键是 sum(trans.amount) 聚合表达式 + LIMIT 10 → 排序可解析,
    不再因「排序被丢弃」报 limit_without_order。"""
    res = _compile(model, "0487", PLAN_0487)
    assert not isinstance(res, CompileMiss), f"ordering must resolve: {res}"
    sql = _low(res)
    assert "order by sum(trans.amount)" in sql
    assert "limit 10" in sql


@pytest.mark.parametrize("qid,plan,kept", [
    ("0492", PLAN_0492, "district.a11 > 10000"),
    ("0495", PLAN_0495, "client.gender = 'm'"),
])
def test_share_analysis_partial_is_advisory(model, qid, plan, kept):
    """0492/0495:share 口径未声明 → 软缺口骨架,必须是 partial + advisory
    (where 子集检查让位给 gen_sql 重构视角),但权威部分(join/过滤)仍在。

    A④ 条件所有权**不改这里的 0495 断言**(``client.gender = 'm'`` 照旧冻结):
    0495 的 answer 候选 ``count(client.client_id)`` 命中的是 KB 里的**无条件**
    计数度量 —— 所有权只看候选定义里有没有内部谓词,无条件度量给出空集,
    孪生条件本就不归它。会翻转的是"候选 = 条件/占比形态"的形状,见
    ``test_0495_hand_share_candidate_owns_twin_condition``(手写 fixture)。
    """
    res = _compile(model, qid, plan)
    assert isinstance(res, PartialCompile), f"{qid} should be a partial skeleton: {res}"
    assert getattr(res.contract, "advisory", False) is True
    sql = _low(res)
    assert kept in sql
    assert "join" in sql


# ----------------------------------------------- 机制针(手写形状)编译断言

def test_0475_hand_extreme_only_projection_fallback(model):
    """0475 手写:只有 min 极值、没有可编译投影 → 兜底单列 MIN(极值列),
    不再 nothing_compilable 硬 MISS。"""
    res = _compile_with(model, PLAN_0475_HAND, ["district"])
    assert not isinstance(res, CompileMiss), f"extreme fallback must compile: {res}"
    sql = _low(res)
    assert "min(district.a11)" in sql
    assert "from district" in sql
    assert "limit" not in sql and "order by" not in sql  # 聚合兜底不是"取一行"


def test_0486_hand_second_highest_selection_predicate(model):
    """0486 手写:rank=2 极值 → 行级选择谓词(OFFSET 1);scope 带全局标记 →
    子查询取未过滤集,跨表条件留在外层 WHERE。"""
    res = _compile_with(model, PLAN_0486_HAND, ["district", "client"])
    assert not isinstance(res, CompileMiss), f"extreme rank=2 must compile: {res}"
    sql = _low(res)
    assert (
        "district.a15 = (select district.a15 from district "
        "order by district.a15 desc limit 1 offset 1)"
    ) in sql
    assert "client.gender = 'f'" in sql


def test_0482_hand_scalar_expression_column(model):
    """0482 手写:算式列经闭语法通道**重建**进投影(表限定、括号规范化),
    不再被静默跳过。"""
    res = _compile_with(model, PLAN_0482_HAND, ["district"])
    assert not isinstance(res, CompileMiss), f"expression column must compile: {res}"
    sql = _low(res)
    assert "district.a2" in sql
    assert "(((district.a13 - district.a12) / district.a12) * 100)" in sql


def test_0492_hand_prunes_unreferenced_account_join(model):
    """0492 手写(锚表 client):显式 joins 里的 account 无人引用 → A③ 剪掉;
    对照逐字 PLAN_0492(锚表 account)不剪 —— 剪枝只删"非锚的无关叶子"。"""
    hand = _compile_with(model, PLAN_0492_HAND, ["account", "client", "district"])
    assert isinstance(hand, PartialCompile), hand
    hand_sql = _low(hand)
    assert "district.a11 > 10000" in hand_sql
    assert "join account" not in hand_sql
    assert "join district" in hand_sql

    verbatim = _compile(model, "0492", PLAN_0492)
    assert isinstance(verbatim, PartialCompile), verbatim
    # 逐字形状里 account 是锚表(FROM 根 + keep)→ 它的连边一条不剪
    assert "from account" in _low(verbatim)
    assert "join account" not in _low(verbatim)


def test_0495_hand_share_candidate_owns_twin_condition(model):
    """0495 手写(A④):候选命中**声明**占比度量后,分子谓词的孪生行级条件
    不再冻结进骨架 WHERE(产物 = 声明表达式,无 WHERE)。"""
    res = _compile_with(model, PLAN_0495_OWN_HAND, ["client"])
    assert not isinstance(res, CompileMiss), f"matched share must compile: {res}"
    sql = _low(res)
    assert "case when client.gender = 'f' then 1 else 0 end" in sql
    assert "where" not in sql  # 孪生条件归聚合所有


# ------------------------------------------------- env-gated MySQL execution

def _mysql_conn():
    url = os.environ.get("MYSQL_TEST_URL")
    if not url:
        pytest.skip("MYSQL_TEST_URL not set")
    try:
        import pymysql
    except ImportError:  # pragma: no cover
        pytest.skip("pymysql not installed")
    from urllib.parse import urlparse

    u = urlparse(url)
    return pymysql.connect(
        host=u.hostname or "127.0.0.1", port=u.port or 3306,
        user=u.username or "root", password=u.password or "",
        database=(u.path or "/financial").lstrip("/"))


@pytest.mark.integration
@pytest.mark.parametrize("qid,plan,expected", [
    ("0470", PLAN_0470, 69),     # gold: COUNT(DISTINCT district_id) + A11 BETWEEN
    ("0476", PLAN_0476, 8),      # gold: COUNT(client_id) … district.A2='Sokolov'
])
def test_execution_matches_gold(model, qid, plan, expected):
    res = _compile(model, qid, plan)
    assert not isinstance(res, CompileMiss), f"{qid} must compile: {res}"
    conn = _mysql_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(res.sql)
            got = cur.fetchone()
    finally:
        conn.close()
    assert int(got[0]) == expected, f"{qid}: compiled SQL gives {got[0]}, gold {expected}"
