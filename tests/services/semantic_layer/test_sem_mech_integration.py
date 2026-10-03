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
    repair_plan_joins,
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
    # B1 渲染规范化:比率形态归为"先乘后除 + CAST DOUBLE"(与 gold 同浮点路径)
    assert "(cast((district.a13 - district.a12) as double) * 100 / district.a12)" in sql


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


# ----------------------------------- 跨车道机制针(0498/0485,合并后补)

# 0498 手写:age = 年份差算式列(复数形态)。真实 plan 只投原列
# (client.birth_date),没有表达式;gold 该列是 DOUBLE(DATE_FORMAT 字符串
# 相减 → 74.0),零容差 str(v) 下整数 74 ≠ '74.0'。A② 通道**忠实保留**
# 计划里的 CAST(绝不自动补)——类型口径由 planner 纪律产出。
PLAN_0498_HAND = {
    "tables": ["disp", "card", "client"],
    "joins": "card.disp_id = disp.disp_id AND disp.client_id = client.client_id",
    "conditions": [
        {"field": "card.type", "op": "=", "value": "'gold'"},
        {"field": "disp.type", "op": "=", "value": "'OWNER'"},
    ],
    "answer_columns": [
        "client.client_id",
        "CAST(YEAR(CURRENT_TIMESTAMP()) - YEAR(client.birth_date) AS DOUBLE)",
    ],
    "ordering": [],
    "having": [],
    "plan_field": "",
}

# 0485 手写:"running contracts" → loan.status IN ('C','D')(官方标签
# C/D = running contract);district 1 → account.district_id = 1。
PLAN_0485_HAND = {
    "tables": ["account", "loan"],
    "joins": "loan.account_id = account.account_id",
    "conditions": [
        {"field": "account.district_id", "op": "=", "value": 1,
         "note": "Branch location 1"},
        {"field": "loan.status", "op": "in", "value": "('C', 'D')",
         "note": "running contracts"},
    ],
    "aggregation": "count",
    "answer_columns": ["count(account.account_id)"],
    "ordering": [],
    "having": [],
    "plan_field": "",
}


def test_0498_hand_year_diff_scalar_real_valued(model):
    """0498 手写:年份差算式列经 A② 通道重建,CAST 类型原样保留(DOUBLE)。
    算式列不再被静默跳过;类型口径是计划声明的一部分。"""
    res = _compile_with(model, PLAN_0498_HAND, ["disp", "card", "client"])
    assert not isinstance(res, CompileMiss), f"age scalar column must compile: {res}"
    sql = _low(res)
    assert "client.client_id" in sql
    assert "cast((year(current_timestamp()) - year(client.birth_date)) as double)" in sql


def test_0485_hand_enum_in_predicate(model):
    """0485 手写:枚举 IN 谓词('C','D')按 code 落到 loan.status,district_id=1
    行级过滤保留,两个连接都在。"""
    res = _compile_with(model, PLAN_0485_HAND, ["account", "loan"])
    assert not isinstance(res, CompileMiss), f"enum IN must compile: {res}"
    sql = _low(res)
    assert "loan.status in ('c', 'd')" in sql
    assert "account.district_id = 1" in sql
    assert "join loan" in sql


def test_0485_real_kb_running_contract_labels_anchor_loan(model):
    """B(官方文档 → enum_display)× C(标签 token 命中)在真实 KB 上合流。

    0485 问句不含 'loan' 词元 → 2.5 只能来自 loan.status 的官方标签
    ("running contract, OK so far" / "…client in debt");标签不可达时
    loan 进不了 matched_datasets,条件整条消失(实跑日志口径)。
    """
    from trove.workflow.nodes.schema_linking import (
        _semantic_dataset_score,
        _word_tokens,
    )
    q = "How many accounts have running contracts in Branch location 1?"
    loan = next(d for d in model.datasets if d.name == "loan")
    assert _semantic_dataset_score(loan, q, _word_tokens(q)) == 2.5


# --------------------------------- P3 跨车道针(0482/0483/0493,纠正器+编译器)

# 0482 实录(results.jsonl financial-0482,path=compiled):双列投影(A2 + 比率
# 式),比率式先除后乘;B3 比率投影收敛剪掉 A2(问题要的是增幅本身,gold 单列),
# A1 渲染规范化把比率归到"先乘后除 + CAST DOUBLE"——先除后乘实测 23/45 行
# 末位差 1 ulp,零容差下必须落在 gold 同一浮点路径。
PLAN_0482_REAL = {
    "tables": ["loan", "account", "district"],
    "joins": (
        "loan.account_id = account.account_id; "
        "account.district_id = district.district_id"
    ),
    "conditions": [
        {"field": "loan.status", "op": "=", "value": "'D'",
         "note": "running contract, client in debt"},
    ],
    "aggregation": "无",
    "ordering": [],
    "answer_columns": [
        "district.A2",
        "((district.A13 - district.A12) / district.A12) * 100",
    ],
    "having": [],
    "plan_field": "",
}

QUESTION_0482 = (
    "For loans contracts which are still running where client are in debt, "
    "list the district of the and the state the percentage unemployment rate "
    "increment from year 1995 to 1996."
)

QUESTION_0483 = (
    "List the top nine districts, by descending order, from the highest to "
    "the lowest, the number of female account holders."
)

# 0493 实录(results.jsonl financial-0493,path=llm):joins 借共享维度列把
# client 接到 account(account.district_id = client.district_id,扇出);
# A2 计划层 joins 修复换成所有权链 loan→account→disp→client。编译仍软 MISS
# (no_metric_match),修复的意义在**计划文本层**:gen 逐字照抄的是修好的链。
PLAN_0493_REAL = {
    "tables": ["loan", "account", "client"],
    "joins": (
        "loan.account_id = account.account_id AND "
        "account.district_id = client.district_id"
    ),
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'M'",
         "note": "male client"},
        {"field": "loan.date", "op": ">=", "value": "'1996-01-01'",
         "note": "loans from 1996"},
        {"field": "loan.date", "op": "<=", "value": "'1997-12-31'",
         "note": "loans up to 1997"},
    ],
    "aggregation": "sum",
    "time_grain": {"field": "loan.date", "grain": "year"},
    "ordering": [],
    "answer_columns": [
        "(SUM(CASE WHEN YEAR(loan.date) = 1997 THEN loan.amount ELSE 0 END) "
        "- SUM(CASE WHEN YEAR(loan.date) = 1996 THEN loan.amount ELSE 0 END)) "
        "/ SUM(CASE WHEN YEAR(loan.date) = 1996 THEN loan.amount ELSE 0 END) * 100"
    ],
    "having": [],
    "plan_field": "",
}


def test_0482_real_ratio_only_projection_single_column(model):
    """0482 实录:B3 收敛只留比率列 + A1 归一(先乘后除/CAST DOUBLE)。"""
    from trove.workflow.nodes.query_sketch import ratio_only_projection

    fixed = ratio_only_projection(dict(PLAN_0482_REAL), QUESTION_0482)
    assert fixed is not None and fixed["plan_field"] == "ratio_only_projection"
    assert fixed["answer_columns"] == [
        "((district.A13 - district.A12) / district.A12) * 100"]
    res = _compile_with(
        model, fixed, ["client", "district", "loan", "trans", "account"])
    assert not isinstance(res, CompileMiss), res
    sql = _low(res)
    assert "district.a2" not in sql
    assert "(cast((district.a13 - district.a12) as double) * 100 / district.a12)" in sql


def test_0483_real_reanchor_to_holder_table(model):
    """0483 实录:B2 重锚把计数从 account 换到持有人表 client,计数改
    count(distinct client.client_id) 并三处同步;编译不再出现 account。"""
    from trove.workflow.nodes.query_sketch import reanchor_entity_count_plan

    re = reanchor_entity_count_plan(dict(PLAN_0483), QUESTION_0483, "en", model)
    assert re is not None, "reanchor must fire on the recorded shape"
    assert re["plan_field"] == "reanchor_entity_count_plan"
    assert re["tables"] == ["client", "district"]
    assert re["joins"] == "client.district_id = district.district_id"
    assert re["answer_columns"] == [
        "district.A2", "count(distinct client.client_id)"]
    assert re["ordering"] == [
        {"column": "count(distinct client.client_id)", "direction": "desc"}]
    res = _compile_with(model, re, MATCHED["0483"])
    assert not isinstance(res, CompileMiss), res
    sql = _low(res)
    assert "account" not in sql
    assert "count(client.client_id)" in sql
    assert "order by count(client.client_id) desc" in sql
    assert "limit 9" in sql


def test_0493_real_plan_join_repair_ownership_chain(model):
    """0493 实录:A2 修复把借共享列的扇出连接换成所有权链(经 disp),
    disp 进表集;坏连接逐字消失(计划文本交 gen 的就是这条链)。"""
    rp = repair_plan_joins(dict(PLAN_0493_REAL), model)
    assert rp is not None
    assert "disp" in rp["tables"]
    assert "account.district_id = client.district_id" not in rp["joins"]
    assert "disp.account_id = account.account_id" in rp["joins"]
    assert "disp.client_id = client.client_id" in rp["joins"]


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


_GOLD_ORACLE = (
    Path(__file__).resolve().parents[3] / "eval" / "baseline" / "questions.jsonl"
)


def _gold_sql(qid: str) -> str:
    """题面 oracle 取仓库内的 eval/baseline/questions.jsonl(与 anti-cheat
    脚本同一份)。gold 只在本测试内作执行比对,绝不进 KB。"""
    import json

    with _GOLD_ORACLE.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("qid") == f"financial-{qid}":
                return row["gold_sql"]
    raise KeyError(f"qid {qid} not in oracle")


@pytest.mark.integration
@pytest.mark.parametrize("qid,plan,matched", [
    # 0485: 官方标签语义(running contract = C/D)→ 枚举 IN 谓词,计数与 gold 同
    ("0485", PLAN_0485_HAND, ["account", "loan"]),
    # 0498: 派生量算式列(年份差 CAST DOUBLE)逐行 = gold(DATE_FORMAT 相减同为
    # double;str() 零容差下 74 与 '74.0' 不同,这正是 CAST 存在的意义)
    ("0498", PLAN_0498_HAND, ["disp", "card", "client"]),
])
def test_execution_matches_gold_rows(model, qid, plan, matched):
    from scripts.eval_bird import normalize_rows  # harness 同款比较口径

    res = _compile_with(model, plan, matched)
    assert not isinstance(res, CompileMiss), f"{qid} must compile: {res}"
    conn = _mysql_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(res.sql)
            got = cur.fetchall()
            cur.execute(_gold_sql(qid))
            gold = cur.fetchall()
    finally:
        conn.close()
    assert normalize_rows(got) == normalize_rows(gold), (
        f"{qid}: compiled rows != gold rows\n got={got!r}\ngold={gold!r}")


def _assert_rows_match_gold(qid: str, sql: str):
    from scripts.eval_bird import normalize_rows  # harness 同款比较口径

    conn = _mysql_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            got = cur.fetchall()
            cur.execute(_gold_sql(qid))
            gold = cur.fetchall()
    finally:
        conn.close()
    assert normalize_rows(got) == normalize_rows(gold), (
        f"{qid}: rows != gold rows\n got={got!r}\ngold={gold!r}")


@pytest.mark.integration
def test_0482_real_projection_rows_match_gold(model):
    """B3+A1 收敛后的单列比率式,45 行逐字符 = gold(含 1 ulp 浮点路径)。"""
    from trove.workflow.nodes.query_sketch import ratio_only_projection

    fixed = ratio_only_projection(dict(PLAN_0482_REAL), QUESTION_0482)
    res = _compile_with(
        model, fixed, ["client", "district", "loan", "trans", "account"])
    assert not isinstance(res, CompileMiss), res
    _assert_rows_match_gold("0482", res.sql)


@pytest.mark.integration
def test_0483_real_reanchor_rows_match_gold(model):
    """B2 重锚后的计划(gold 的计数形态 COUNT(client_id),无 DISTINCT 因
    client_id 是主键),9 行 = gold。"""
    from trove.workflow.nodes.query_sketch import reanchor_entity_count_plan

    re = reanchor_entity_count_plan(dict(PLAN_0483), QUESTION_0483, "en", model)
    assert re is not None
    res = _compile_with(model, re, MATCHED["0483"])
    assert not isinstance(res, CompileMiss), res
    _assert_rows_match_gold("0483", res.sql)


@pytest.mark.integration
def test_0493_repaired_ownership_path_rows_match_gold(model):
    """A2 所有权链 + 角色限定 + 占比 DOUBLE 形态三者齐备才与 gold 同行。

    连接段必须与 repair 输出逐子句一致(它修的就是这一环);角色限定
    (disp.type='OWNER',问题「for a male client」的持有人语义)与占比 CANON
    形态是规划/生成侧纪律的既定渲染(实跑 pred 即为此形态),在此按纪律手写
    执行载体——不加限定实测 25.362,加限定 25.300191222790616 = gold。"""
    rp = repair_plan_joins(dict(PLAN_0493_REAL), model)
    assert rp is not None
    n97 = "SUM(CASE WHEN YEAR(loan.date) = 1997 THEN loan.amount ELSE 0 END)"
    n96 = "SUM(CASE WHEN YEAR(loan.date) = 1996 THEN loan.amount ELSE 0 END)"
    sql = (
        f"SELECT (CAST(({n97} - {n96}) AS DOUBLE) * 100 / {n96}) "
        "FROM loan "
        "JOIN account ON loan.account_id = account.account_id "
        "JOIN disp ON disp.account_id = account.account_id "
        "JOIN client ON disp.client_id = client.client_id "
        "WHERE client.gender = 'M' AND disp.type = 'OWNER'"
    )
    for clause in rp["joins"].split(" AND "):
        assert clause in sql, f"repair clause not in pin SQL: {clause}"
    _assert_rows_match_gold("0493", sql)
