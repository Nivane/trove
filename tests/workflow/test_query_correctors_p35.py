"""P3.5 机制针:算式比率规范化(②)/ 人计数路径简化(③a)/ 角色补齐(③c)。

plan fixture 逐字取自 P3 运行台账 ``.trove/eval/results.jsonl``(各题注释标注
run id;台账在 worktree 外,这里只落**小计划字典**);模型取真实 KB
``.trove/kb/financial/semantics.yml``(``parse_ossie``,与
``tests/services/semantic_layer/test_sem_mech_integration.py`` 同一模式,KB
缺席则 skip)。零 LLM / 零网络:全部是纯函数/计划层变换。

- ② ``canonical_ratio_text``:0479/0493/0482/0495 的规范文本逐字断言;
  0478/0481(已是规范形态)/0498(无比率形状)/垃圾串 → None(零文本扰动)。
  纠正器 ``canonicalize_ratio_answer_columns``:0479 计划列改写 + marker;
  0478 列 → None;aggregation 形态(0495)同样改写。
- ③a ``reanchor_entity_count_plan`` 路径简化分支:0483 第 2 轮实录(已数
  count(distinct client.client_id)、桥上有 disp.type='OWNER')→ 直连
  client→district、桥上条件丢弃;反向针 0476(存在语义桥、桥上无条件)与
  0492 → None;幂等(产物再跑一次 → None)。
- ③c ``ensure_owner_role_on_person_path``:0493 实录 → 追加
  ``disp.type='OWNER'``(模型声明的 owner 取值);反向针
  0492/0495/0476/0498 → None。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from trove.services.semantic_layer.ossie import parse_ossie
from trove.workflow.nodes.query_sketch import (
    canonicalize_ratio_answer_columns,
    ensure_owner_role_on_person_path,
    reanchor_entity_count_plan,
)

_KB = (
    Path(__file__).resolve().parents[2]
    / ".trove" / "kb" / "financial" / "semantics.yml"
)


@pytest.fixture(scope="module")
def model():
    if not _KB.exists():
        pytest.skip(f"financial KB not present: {_KB}")
    return parse_ossie(_KB.read_text(encoding="utf-8"), preferred_dialect="mysql")


# --------------------------------------------------------------- plan fixtures
# 0476 · eval-2-1790992871(存在语义桥:桥上无条件,client 侧条件全在)
QUESTION_0476 = (
    "Among the account opened, how many female customers who were born "
    "before 1950 and stayed in Sokolov?"
)
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

# 0479 · eval-6-1791008975 —— 软 MISS 的算式列(子查询),软缺口原文交 gen
EXPR_0479_DIVFIRST = (
    "((SELECT trans.balance FROM trans WHERE trans.account_id = account.account_id "
    "AND trans.date = '1998-12-27') - (SELECT trans.balance FROM trans WHERE "
    "trans.account_id = account.account_id AND trans.date = '1993-03-22')) / "
    "(SELECT trans.balance FROM trans WHERE trans.account_id = account.account_id "
    "AND trans.date = '1993-03-22') * 100"
)
EXPR_0479_CANON = (
    "(CAST(((SELECT trans.balance FROM trans WHERE trans.account_id = "
    "account.account_id AND trans.date = '1998-12-27') - (SELECT trans.balance "
    "FROM trans WHERE trans.account_id = account.account_id AND trans.date = "
    "'1993-03-22')) AS DOUBLE) * 100 / (SELECT trans.balance FROM trans WHERE "
    "trans.account_id = account.account_id AND trans.date = '1993-03-22'))"
)
QUESTION_0479 = (
    "For the client whose loan was approved first in 1993/7/5, what is the "
    "increase rate of his/her account balance from 1993/3/22 to 1998/12/27?"
)
PLAN_0479 = {
    "tables": ["loan", "account", "disp", "client", "trans"],
    "joins": (
        "loan.account_id = account.account_id AND "
        "disp.account_id = account.account_id AND "
        "disp.client_id = client.client_id AND "
        "trans.account_id = account.account_id"
    ),
    "conditions": [
        {"field": "loan.date", "op": "=", "value": "'1993-07-05'",
         "note": "loan approved on 1993-07-05"},
        {"field": "disp.type", "op": "=", "value": "'OWNER'",
         "note": "client is the owner of the account"},
    ],
    "aggregation": "无",
    "extreme": {"func": "min", "column": "loan.date", "rank": 1,
                "scope": "全部条件过滤后的集合"},
    "ordering": [],
    "answer_columns": [EXPR_0479_DIVFIRST],
    "having": [],
    "plan_field": "",
}

# 0483 · eval-8-1791009102 第 2 轮 —— 已按 skill 纪律数 count(distinct
# client.client_id),但连接仍绕 account/disp 且角色条件挂在桥上;gold 直连
# client.district_id 且无角色约束(MySQL 复验逐字节相等)
QUESTION_0483 = (
    "List the top nine districts, by descending order, from the highest to "
    "the lowest, the number of female account holders."
)
PLAN_0483 = {
    "tables": ["account", "district", "disp", "client"],
    "joins": (
        "account.district_id = district.district_id; "
        "disp.account_id = account.account_id; "
        "disp.client_id = client.client_id"
    ),
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'F'", "note": "女性客户"},
        {"field": "disp.type", "op": "=", "value": "'OWNER'", "note": "账户持有人"},
    ],
    "aggregation": "count(distinct client.client_id)",
    "ordering": [{"column": "count(distinct client.client_id)", "direction": "desc"}],
    "answer_columns": ["district.A2", "count(distinct client.client_id)"],
    "having": [],
    "limit": 9,
    "plan_field": "",
}

# 0492 · eval-4-1791008901 —— client 侧占比(量化元素引用人表 → ③c 必须排除)
QUESTION_0492 = (
    "What percentage of clients who opened their accounts in the district "
    "with an average salary of over 10000 are women?"
)
PLAN_0492 = {
    "tables": ["account", "client", "district", "disp"],
    "joins": (
        "account.district_id = district.district_id AND "
        "disp.account_id = account.account_id AND "
        "disp.client_id = client.client_id"
    ),
    "conditions": [
        {"field": "district.A11", "op": ">", "value": "10000",
         "note": "开户地区平均工资超过 10000"},
    ],
    "aggregation": "share of female clients",
    "ordering": [],
    "answer_columns": [
        "CAST(SUM(CASE WHEN client.gender = 'F' THEN 1 ELSE 0 END) AS DOUBLE) "
        "* 100.0 / COUNT(client.client_id)"
    ],
    "having": [],
    "plan_field": "repair_plan_joins",
}

# 0493 · eval-9-1791009137 —— joins 已被 repair_plan_joins 换成所有权链,但
# 没有 disp.type='OWNER'(DISPONENT 也算进来 → 25.362… 对 gold
# 25.300191222790616;加 OWNER + 乘先规范化 = gold 逐字节)
QUESTION_0493 = (
    "What was the growth rate of the total amount of loans across all accounts "
    "for a male client between 1996 and 1997?"
)
EXPR_0493_DIVFIRST = (
    "(SUM(CASE WHEN YEAR(loan.date) = 1997 THEN loan.amount ELSE 0 END) - "
    "SUM(CASE WHEN YEAR(loan.date) = 1996 THEN loan.amount ELSE 0 END)) / "
    "SUM(CASE WHEN YEAR(loan.date) = 1996 THEN loan.amount ELSE 0 END) * 100"
)
EXPR_0493_CANON = (
    "(CAST((SUM(CASE WHEN YEAR(loan.date) = 1997 THEN loan.amount ELSE 0 END) - "
    "SUM(CASE WHEN YEAR(loan.date) = 1996 THEN loan.amount ELSE 0 END)) AS "
    "DOUBLE) * 100 / SUM(CASE WHEN YEAR(loan.date) = 1996 THEN loan.amount "
    "ELSE 0 END))"
)
PLAN_0493 = {
    "tables": ["loan", "account", "client", "disp"],
    "joins": (
        "loan.account_id = account.account_id AND "
        "disp.account_id = account.account_id AND "
        "disp.client_id = client.client_id"
    ),
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'M'", "note": "male client"},
        {"field": "loan.date", "op": ">=", "value": "'1996-01-01'", "note": "loans from 1996"},
        {"field": "loan.date", "op": "<=", "value": "'1997-12-31'", "note": "loans up to 1997"},
    ],
    "aggregation": "sum",
    "ordering": [],
    "answer_columns": [EXPR_0493_DIVFIRST],
    "time_grain": {"field": "loan.date", "grain": "year"},
    "having": [],
    "plan_field": "repair_plan_joins",
}

# 0495 · eval-9-1790993182 —— 占比算式落在 aggregation 与 answer_columns 两处
# (client 侧份额 → ③c 反向针)
QUESTION_0495 = (
    "What percentage of male clients request for weekly statements to be issued?"
)
PLAN_0495 = {
    "tables": ["client", "disp", "account"],
    "joins": "disp.client_id = client.client_id AND disp.account_id = account.account_id",
    "conditions": [
        {"field": "client.gender", "op": "=", "value": "'M'", "note": "male clients"},
        {"field": "account.frequency", "op": "=", "value": "'POPLATEK TYDNE'",
         "note": "weekly statement issuance"},
    ],
    "aggregation": "SUM(CASE WHEN client.gender = 'M' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)",
    "ordering": [],
    "answer_columns": [
        "SUM(CASE WHEN client.gender = 'M' THEN 1 ELSE 0 END) * 100.0 / COUNT(*)"
    ],
    "having": [],
    "plan_field": "",
}

# 0498 · eval-5-1791008949 —— 已有 OWNER 角色条件 + 无比率形状(③c 反向针)
QUESTION_0498 = (
    "Provide the IDs and age of the client with high level credit card, "
    "which is eligible for loans."
)
PLAN_0498 = {
    "tables": ["card", "disp", "client"],
    "joins": "card.disp_id = disp.disp_id AND disp.client_id = client.client_id",
    "conditions": [
        {"field": "card.type", "op": "=", "value": "'gold'", "note": "high-level credit card"},
        {"field": "disp.type", "op": "=", "value": "'OWNER'",
         "note": "eligible for loans (owner)"},
    ],
    "aggregation": "无",
    "ordering": [],
    "answer_columns": [
        "client.client_id",
        "CAST(YEAR(CURRENT_TIMESTAMP()) - YEAR(client.birth_date) AS DOUBLE)",
    ],
    "having": [],
    "plan_field": "",
}

# 0482/0478/0481 的规范形态对照(② 的幂等/改写分界;0482 的计划算式逐字取自
# 台账 financial-0482,0478/0481 取自各自实录的 CAST 形态)
EXPR_0482_DIVFIRST = "((district.A13 - district.A12) / district.A12) * 100"
EXPR_0482_CANON = "(CAST((district.A13 - district.A12) AS DOUBLE) * 100 / district.A12)"
EXPR_0478_CANON = (
    "CAST(SUM(CASE WHEN client.gender = 'M' THEN 1 ELSE 0 END) AS DOUBLE) "
    "* 100.0 / COUNT(client.client_id)"
)
EXPR_0481_CANON = (
    "CAST(SUM(CASE WHEN loan.status = 'C' THEN 1 ELSE 0 END) AS DOUBLE) "
    "* 100.0 / COUNT(*)"
)
EXPR_0495_CANON = (
    "(CAST(SUM(CASE WHEN client.gender = 'M' THEN 1 ELSE 0 END) AS DOUBLE) "
    "* 100.0 / COUNT(*))"
)
EXPR_0498_NOSCALE = (
    "CAST(YEAR(CURRENT_TIMESTAMP()) - YEAR(client.birth_date) AS DOUBLE)"
)


class TestCanonicalRatioText:
    """② ``canonical_ratio_text``:除先乘后 → 先乘后除 + CAST DOUBLE(零 LLM)。"""

    @staticmethod
    def _f():
        from trove.services.semantic_layer.compiler import canonical_ratio_text
        return canonical_ratio_text

    @pytest.mark.parametrize("expr,expected", [
        (EXPR_0479_DIVFIRST, EXPR_0479_CANON),   # 子查询算式(软 MISS 列)
        (EXPR_0493_DIVFIRST, EXPR_0493_CANON),   # 聚合算式(软 MISS 列)
        (EXPR_0482_DIVFIRST, EXPR_0482_CANON),   # 标量算式(与闭集同规范)
        (PLAN_0495["answer_columns"][0], EXPR_0495_CANON),  # 无 CAST 的乘先式
    ])
    def test_divide_first_rewritten(self, expr, expected):
        assert self._f()(expr) == expected

    @pytest.mark.parametrize("expr", [
        EXPR_0478_CANON,   # 已是规范形态(Div(Mul(Cast, K), D))→ 零扰动
        EXPR_0481_CANON,   # 同上
        EXPR_0498_NOSCALE,  # 无比率形状
        "not sql at all ((",
        "COUNT(*)",
    ])
    def test_noop_or_unparseable(self, expr):
        assert self._f()(expr) is None

    def test_idempotent_on_canonical(self):
        f = self._f()
        canon = f(PLAN_0493["answer_columns"][0])
        assert canon is not None and f(canon) is None


class TestCanonicalizeRatioAnswerColumns:
    """② 纠正器:answer_columns 每列 + aggregation(仅当含 "/")。"""

    Q = QUESTION_0479

    def test_0479_recorded_plan_columns_rewritten(self):
        f = canonicalize_ratio_answer_columns
        fixed = f(dict(PLAN_0479))
        assert fixed is not None
        assert fixed["answer_columns"] == [EXPR_0479_CANON]
        assert fixed["plan_field"] == "canonicalize_ratio"
        # 非算数组件逐字保留(extreme/conditions/aggregation 无 "/")
        assert fixed["extreme"] == PLAN_0479["extreme"]
        assert fixed["conditions"] == PLAN_0479["conditions"]
        assert fixed["aggregation"] == "无"
        # 幂等:规范式再跑 → 无改动
        assert f(fixed) is None
        # 非就地
        assert PLAN_0479["answer_columns"] == [EXPR_0479_DIVFIRST]

    def test_0495_aggregation_form_rewritten(self):
        """算式也会落在 aggregation 字段(0495 形态)→ 两处同步规范化。"""
        f = canonicalize_ratio_answer_columns
        fixed = f(dict(PLAN_0495))
        assert fixed is not None
        assert fixed["aggregation"] == EXPR_0495_CANON
        assert fixed["answer_columns"] == [EXPR_0495_CANON]

    @pytest.mark.parametrize("plan", [
        # 已规范(0478/0481 形态)· 无比率形状(0498)· 无 answer_columns
        {"answer_columns": [EXPR_0478_CANON]},
        {"answer_columns": [EXPR_0481_CANON], "aggregation": "count"},
        {"answer_columns": PLAN_0498["answer_columns"], "aggregation": "无"},
        {"answer_columns": []},
        {},
        None,
    ])
    def test_no_change_untouched(self, plan):
        assert canonicalize_ratio_answer_columns(plan) is None


class TestSimplifyPersonCountPath:
    """③a 路径简化分支:数的是人表本身 → 直连人的声明边(0483 第 2 轮)。"""

    def test_0483_recorded_plan_simplified(self, model):
        fixed = reanchor_entity_count_plan(
            dict(PLAN_0483), QUESTION_0483, "en", model)
        assert fixed is not None
        assert fixed["plan_field"] == "simplify_person_count_path"
        assert fixed["tables"] == ["client", "district"]
        assert fixed["joins"] == "client.district_id = district.district_id"
        assert fixed["conditions"] == [PLAN_0483["conditions"][0]]  # 桥上 OWNER 丢弃
        # 计数/投影/排序引用的是 E 自身 → 原样
        assert fixed["aggregation"] == "count(distinct client.client_id)"
        assert fixed["answer_columns"] == [
            "district.A2", "count(distinct client.client_id)"]
        assert fixed["ordering"] == PLAN_0483["ordering"]
        assert fixed["limit"] == 9
        # 非就地 + 幂等(改写后桥上无条件 → 再次运行自然 bail)
        assert PLAN_0483["tables"] == ["account", "district", "disp", "client"]
        assert reanchor_entity_count_plan(fixed, QUESTION_0483, "en", model) is None

    def test_0476_existence_bridge_untouched(self, model):
        """存在语义桥(桥上无条件)→ 少一条连接就换答案,绝不简化。"""
        assert reanchor_entity_count_plan(
            dict(PLAN_0476), QUESTION_0476, "en", model) is None

    def test_client_side_share_untouched(self, model):
        """client 侧占比(0492):非计数列是算式 → dims 形状不确定,不动作。"""
        assert reanchor_entity_count_plan(
            dict(PLAN_0492), QUESTION_0492, "en", model) is None

    def test_own_word_question_untouched(self, model):
        """问句显式点名"持有"→ 角色约束是题面要求,不简化。"""
        q = "How many female clients own an account?"
        assert reanchor_entity_count_plan(
            dict(PLAN_0483), q, "en", model) is None

    def test_bridge_condition_not_enum_untouched(self, model):
        """桥上是普通列条件(account.district_id)→ 不可弃,不动作。"""
        plan = {**PLAN_0483, "conditions": [
            *PLAN_0483["conditions"],
            {"field": "account.district_id", "op": "=", "value": "1"},
        ]}
        assert reanchor_entity_count_plan(plan, QUESTION_0483, "en", model) is None

    def test_bridge_condition_non_owner_role_untouched(self, model):
        """桥表角色限定到非 owner 取值(DISPONENT)→ 语义相反,不弃。"""
        plan = {**PLAN_0483, "conditions": [
            PLAN_0483["conditions"][0],
            {"field": "disp.type", "op": "=", "value": "'DISPONENT'"},
        ]}
        assert reanchor_entity_count_plan(plan, QUESTION_0483, "en", model) is None

    def test_dim_without_declared_edge_untouched(self, model):
        """维度表与人表无声明边(account)→ 不简化(也不造边)。"""
        plan = {**PLAN_0483, "answer_columns": [
            "account.account_id", "count(distinct client.client_id)"],
            "ordering": []}
        assert reanchor_entity_count_plan(plan, QUESTION_0483, "en", model) is None

    def test_still_reanchors_when_counting_detail_table(self, model):
        """数的是明细表(旧分支)行为不变:0483 第 1 轮形状照常换锚。"""
        old = {**PLAN_0483,
               "conditions": [PLAN_0483["conditions"][0]],
               "aggregation": "count",
               "ordering": [{"column": "count(account.account_id)", "direction": "desc"}],
               "answer_columns": ["district.A2", "count(account.account_id)"]}
        fixed = reanchor_entity_count_plan(old, QUESTION_0483, "en", model)
        assert fixed is not None
        assert fixed["plan_field"] == "reanchor_entity_count_plan"
        assert fixed["aggregation"] == "count(distinct client.client_id)"


class TestEnsureOwnerRoleOnPersonPath:
    """③c:记录量化 + 人属性过滤且经角色桥表 → 补 owner 角色限定(0493)。"""

    def test_0493_recorded_plan_owner_appended(self, model):
        fixed = ensure_owner_role_on_person_path(
            dict(PLAN_0493), QUESTION_0493, model)
        assert fixed is not None
        assert fixed["plan_field"] == "ensure_owner_role_on_person_path"
        assert fixed["conditions"][:-1] == PLAN_0493["conditions"]
        added = fixed["conditions"][-1]
        assert added["field"] == "disp.type"
        assert added["op"] == "="
        assert added["value"] == "'OWNER'"
        assert added["note"]
        # 非就地
        assert len(PLAN_0493["conditions"]) == 3
        # 幂等:角色列已受约束 → 再次运行不再追加
        assert ensure_owner_role_on_person_path(fixed, QUESTION_0493, model) is None

    @pytest.mark.parametrize("plan,question", [
        (PLAN_0492, QUESTION_0492),   # client 侧占比(量化元素引用人表)
        (PLAN_0495, QUESTION_0495),   # 同上;且人表无别的记录侧量化
        (PLAN_0476, QUESTION_0476),   # 人数计数(量化对象是人本身)
        (PLAN_0498, QUESTION_0498),   # 已有 OWNER 角色条件
    ])
    def test_reverse_pins_untouched(self, plan, question, model):
        assert ensure_owner_role_on_person_path(dict(plan), question, model) is None

    def test_question_naming_role_untouched(self, model):
        """问句显式点名 owner → 题面自带角色语义,不替它决定。"""
        q = ("What was the growth rate of the total amount of loans across all "
             "accounts for the account owner between 1996 and 1997?")
        assert ensure_owner_role_on_person_path(dict(PLAN_0493), q, model) is None

    def test_role_field_already_constrained_untouched(self, model):
        plan = {**PLAN_0493, "conditions": [
            *PLAN_0493["conditions"],
            {"field": "disp.type", "op": "=", "value": "'OWNER'"},
        ]}
        assert ensure_owner_role_on_person_path(plan, QUESTION_0493, model) is None

    def test_analysis_extreme_untouched(self, model):
        assert ensure_owner_role_on_person_path(
            {**PLAN_0493, "analysis": {"type": "pct_change"}},
            QUESTION_0493, model) is None
        assert ensure_owner_role_on_person_path(
            {**PLAN_0493, "extreme": {"func": "max", "column": "loan.amount"}},
            QUESTION_0493, model) is None

    def test_person_table_without_condition_untouched(self, model):
        """人表不承载条件(0492 形态)→ 无人属性证据,不补。"""
        plan = {**PLAN_0493, "conditions": PLAN_0493["conditions"][1:]}
        assert ensure_owner_role_on_person_path(plan, QUESTION_0493, model) is None

    def test_no_model_or_non_dict_untouched(self):
        assert ensure_owner_role_on_person_path(dict(PLAN_0493), QUESTION_0493, None) is None
        assert ensure_owner_role_on_person_path(None, QUESTION_0493, None) is None


class TestP35Wiring:
    """节点级接线:② 在比率投影收敛之后、③c 在 joins 修复之后(链位固定)。"""

    @staticmethod
    def _provider(model):
        class FakeProvider:
            enabled = True

            def __init__(self, m):
                self._m = m

            def model(self):
                return self._m

        return FakeProvider(model)

    @staticmethod
    def _state(question, matched):
        from trove.workflow.state import WorkflowState

        return WorkflowState(session_id="s1", question=question,
                             matched_tables=matched)

    async def _run(self, payload, question, matched, model):
        import json

        from trove.core.config import AgentConfig
        from trove.workflow.nodes.query_sketch import make_query_sketch

        responses = [json.dumps(payload)]  # 自修正若触发,脚本耗尽即抛错(不入此径)

        class LLM:
            async def chat(self, model, messages, **kwargs):
                return responses.pop(0)

        node = make_query_sketch(LLM(), AgentConfig(target="mock/model"),
                                 semantic_layer=self._provider(model))
        return await node(self._state(question, matched))

    async def test_canonicalize_ratio_wired_after_projection(self, model):
        update = await self._run(
            {**PLAN_0479}, QUESTION_0479,
            ["loan", "account", "disp", "client", "trans"], model)
        assert update["plan_json"]["plan_field"] == "canonicalize_ratio"
        assert update["plan_json"]["answer_columns"] == [EXPR_0479_CANON]

    async def test_owner_role_wired_last(self, model):
        update = await self._run(
            {**PLAN_0493}, QUESTION_0493,
            ["loan", "account", "client", "disp"], model)
        pj = update["plan_json"]
        assert pj["plan_field"] == "ensure_owner_role_on_person_path"
        assert [c["field"] for c in pj["conditions"]][-1] == "disp.type"

    async def test_simplify_branch_wired(self, model):
        update = await self._run(
            {**PLAN_0483}, QUESTION_0483,
            ["account", "district", "order", "client", "trans", "disp"], model)
        pj = update["plan_json"]
        assert pj["plan_field"] == "simplify_person_count_path"
        assert pj["tables"] == ["client", "district"]
