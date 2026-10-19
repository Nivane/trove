"""生成链归因纯核心的测试:L1 档位 / 事实抽取 / plan 解析 / 责任跳 / 报告。"""
from __future__ import annotations

import json

from trove.eval.chain import Predicate, sql_facts


def _f(sql: str):
    got = sql_facts(sql, "mysql")
    assert got is not None and got.ok, sql
    return got


def test_tables_and_join_edges_direction_insensitive():
    a = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i")
    b = _f("SELECT a.x FROM bb b JOIN aa a ON b.i = a.i")
    assert a.tables == ("aa", "bb") == b.tables
    assert a.join_edges == b.join_edges


def test_cte_names_excluded_from_tables():
    got = _f("WITH t AS (SELECT 1 AS x FROM base) SELECT t.x FROM t")
    assert got.tables == ("base",)


def test_conds_flattened_including_subquery():
    got = _f(
        "SELECT * FROM t WHERE t.a = 1 AND t.b IN (2, 3) "
        "AND t.c = (SELECT MAX(c) FROM t2) AND t.d IS NULL"
    )
    assert Predicate((("t", "a"),), "=", ("1",)) in got.conds
    assert any(p.op == "in" and p.values == ("2", "3") for p in got.conds)
    assert any(p.op == "=" and p.values == ("(subquery)",) for p in got.conds)
    assert any(p.op == "raw" for p in got.conds)  # IS NULL 走原文兜底


def test_in_subquery_marker():
    # IN (SELECT …):sqlglot 把它存在 args["query"],不折成 (empty)——
    # 否则它与真·空 IN 列表同形,且与 `= (SELECT …)` 的标记词汇不一致
    got = _f("SELECT * FROM t WHERE t.a IN (SELECT b FROM t2)")
    assert got.conds[0].values == ("(subquery)",)
    lit = _f("SELECT * FROM t WHERE t.a IN (1, 2, 3)")
    assert lit.conds[0].values == ("1", "2", "3")


def test_join_on_is_not_a_cond():
    got = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i WHERE a.k = 7")
    assert len(got.conds) == 1
    # 列限定词归一到基表名(别名 a → aa;基表只出现一次)
    assert got.conds[0].cols == (("aa", "k"),)


def test_aggs_groups_order_limit_projections():
    got = _f(
        "SELECT s.region, COUNT(DISTINCT s.id) FROM sales s "
        "GROUP BY s.region ORDER BY s.region DESC LIMIT 5"
    )
    # 别名归一:限定词按基表名记账(s → sales),列宽容仍由比对层兜底
    assert got.aggs == (("count_distinct", (("sales", "id"),)),)
    assert got.groups == ((("sales", "region"),),)
    assert got.order == (("region", "desc"),)
    assert got.has_limit is True
    assert len(got.projections) == 2


def test_count_star_is_wildcard_zero_cols():
    got = _f("SELECT COUNT(*) FROM sales")
    assert got.aggs == (("count", ()),)


def test_unparseable_returns_none():
    assert sql_facts("SELECT FROM WHERE ((", "mysql") is None
    assert sql_facts("", "mysql") is None


from trove.eval.chain import ladder


def test_no_sql_on_refused_and_blank():
    gold = _f("SELECT 1 FROM a")
    v = ladder(gold, None, verdict="REFUSED", had_sql=False)
    assert v.bucket == "no_sql"
    v2 = ladder(gold, None, verdict="", had_sql=False)
    assert v2.bucket == "no_sql"


def test_pred_unparseable_is_its_own_bucket():
    v = ladder(_f("SELECT 1 FROM a"), None, verdict="MISMATCH", had_sql=True)
    assert v.bucket == "pred_unparseable"


def test_exec_error_rung():
    v = ladder(_f("SELECT 1 FROM a"), _f("SELECT 1 FROM a"),
               verdict="EXECUTION_ERROR")
    assert v.bucket == "exec_error"


def test_rung_order_first_wins_with_also_differ():
    gold = _f("SELECT a.x FROM aa a WHERE a.k = 'F'")
    pred = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i WHERE a.k = 'M'")
    v = ladder(gold, pred, verdict="MISMATCH")
    assert v.bucket == "table_set"
    assert "join_shape" in v.also_differ and "filter_diff" in v.also_differ


def test_table_set_observations():
    gold = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i")
    pred = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i JOIN cc c ON c.i = a.i")
    v = ladder(gold, pred, verdict="MISMATCH")
    assert v.bucket == "table_set"
    assert "extra_table_read" in v.observations
    v2 = ladder(pred, gold, verdict="MISMATCH")
    assert "needed_table_never_read" in v2.observations


def test_structure_equal():
    sql = "SELECT a.x FROM aa a JOIN bb b ON a.i = b.i WHERE a.k = 1"
    v = ladder(_f(sql), _f(sql), verdict="MISMATCH")
    assert v.bucket == "structure_equal"


def test_filter_diff_case_only_observation():
    gold = _f("SELECT a.x FROM aa a WHERE a.gender = 'F'")
    pred = _f("SELECT a.x FROM aa a WHERE a.gender = 'f'")
    v = ladder(gold, pred, verdict="MISMATCH")
    assert v.bucket == "filter_diff"
    assert "case_only_value_diff" in v.observations


def test_extreme_style_mismatch_observation():
    gold = _f("SELECT a.x FROM aa a ORDER BY a.x DESC LIMIT 1")
    pred = _f("SELECT MAX(a.x) FROM aa a")
    v = ladder(gold, pred, verdict="MISMATCH")
    # 聚合档先于排序档命中(一侧有 MIN/MAX、另一侧没有);观察照发
    assert v.bucket == "agg_diff"
    assert "order_limit_diff" in v.also_differ
    assert "extreme_style_mismatch" in v.observations


def test_gold_unparseable_undecidable():
    v = ladder(None, _f("SELECT 1 FROM a"), verdict="MISMATCH")
    assert v.bucket == "undecidable"


def test_projection_diff_tolerates_qualified_and_count_star():
    gold = _f("SELECT COUNT(*) FROM aa a")
    pred = _f("SELECT COUNT(a.i) FROM aa a")
    assert ladder(gold, pred, verdict="MISMATCH").bucket == "structure_equal"


def test_groups_tolerate_qualified_columns():
    # 设计 §2.1 对 groups 行同样要求列宽容:GROUP BY s.region ≡ GROUP BY region
    gold = _f("SELECT s.region FROM sales s GROUP BY s.region")
    pred = _f("SELECT s.region FROM sales s GROUP BY region")
    assert ladder(gold, pred, verdict="MISMATCH").bucket == "structure_equal"


def test_conds_tolerate_qualified_columns():
    # 过滤档同样只认列身份、不认限定写法(与 agg/groups/projection 同一把尺)
    gold = _f("SELECT COUNT(*) FROM loans WHERE loans.status = 'active'")
    pred = _f("SELECT COUNT(*) FROM loans WHERE status = 'active'")
    assert ladder(gold, pred, verdict="MISMATCH").bucket == "structure_equal"
    # 反向:列真的不同(别名指向不同表)仍然进 filter_diff
    other = _f("SELECT COUNT(*) FROM loans l JOIN accounts ac ON l.aid = ac.id"
               " WHERE ac.status = 'active'")
    base = _f("SELECT COUNT(*) FROM loans l JOIN accounts ac ON l.aid = ac.id"
              " WHERE l.status = 'active'")
    assert ladder(base, other, verdict="MISMATCH").bucket == "filter_diff"


def test_wildcard_partner_prefers_exact_column_match():
    # gold 的 COUNT(*) 是通配;匹配时先让列集对得上的配成一对,通配留给剩下的
    gold = _f("SELECT COUNT(*), COUNT(a.x) FROM aa a")
    pred = _f("SELECT COUNT(a.x), COUNT(a.y) FROM aa a")
    assert ladder(gold, pred, verdict="MISMATCH").bucket == "structure_equal"


def test_conds_empty_col_set_is_not_a_wildcard():
    # 谓词的空列集 = 无列谓词(1=1),不是 COUNT(*) 那种通配:不得与真列谓词等价
    gold = _f("SELECT COUNT(*) FROM loans WHERE 1 = 1")
    pred = _f("SELECT COUNT(*) FROM loans WHERE loans.status = 1")
    assert ladder(gold, pred, verdict="MISMATCH").bucket == "filter_diff"
    # 两侧都无列时照常相等(别把门收得比事实还紧)
    same = _f("SELECT COUNT(*) FROM loans WHERE 1 = 1")
    assert ladder(gold, same, verdict="MISMATCH").bucket == "structure_equal"


def test_conds_exact_duplicate_is_not_a_divergence():
    # 合取谓词幂等:同一条件写两遍不改变结果集 —— 不算 filter 分歧(旧 `set()` 语义)
    gold = _f("SELECT COUNT(*) FROM loans "
              "WHERE loans.status = 'A' AND loans.status = 'A'")
    pred = _f("SELECT COUNT(*) FROM loans WHERE loans.status = 'A'")
    assert ladder(gold, pred, verdict="MISMATCH").bucket == "structure_equal"
    # 投影相反:重复的输出列改变结果形状,照旧进投影档(谓词幂等 ≠ 条目幂等)
    g2 = _f("SELECT a.x, a.x FROM aa a")
    p2 = _f("SELECT a.x FROM aa a")
    assert ladder(g2, p2, verdict="MISMATCH").bucket == "projection_diff"


from trove.eval.chain import plan_facts_from_json, plan_facts_text

PLAN_TEXT = (
    "Tables: client, disp, account, district\n"
    "Joins: client.client_id = disp.client_id AND disp.account_id = account.account_id\n"
    "Conditions:\n"
    "  - district.A11 = (SELECT MAX(A11) FROM district) (最高平均工资)\n"
    "  - client.gender = F\n"
    "Aggregation: count distinct client.client_id\n"
    "Extreme: max(client.birth_date) · scope: after all filters\n"
    "Answer columns: account_id"
)


def test_plan_text_sections_and_depth():
    pf = plan_facts_text(PLAN_TEXT)
    assert pf is not None
    assert pf.facts.tables == ("account", "client", "disp", "district")
    assert pf.facts.join_edges == (
        (("account", "account_id"), ("disp", "account_id")),
        (("client", "client_id"), ("disp", "client_id")),
    )  # 边内、边间都排序(升序)
    assert ("filter_diff" in pf.comparable) and ("projection_diff" in pf.comparable)
    # 排序不是计划侧可比档(R22):渲染器确实会写 `Ordering:` 行(_render_plan),
    # 本解析器把它当 parked 节整节收下(不入事实、不进可比集)
    assert "order_limit_diff" not in pf.comparable
    assert pf.facts.aggs == (("count_distinct", (("client", "client_id"),)),)
    assert pf.facts.projections == ((None, (("", "account_id"),)),)
    assert any(p.values == ("(subquery)",) for p in pf.facts.conds)
    assert any(p.values == ("F",) for p in pf.facts.conds)


def test_plan_text_truncated_drops_last_section():
    cut = PLAN_TEXT[:PLAN_TEXT.index("Answer columns:")] + "Answer col"
    pf = plan_facts_text(cut, truncated=True)
    assert "projection_diff" not in pf.comparable
    assert "agg_diff" in pf.comparable


def test_plan_text_none_variants():
    pf = plan_facts_text("Tables: none\nJoins: none\nConditions: none\n"
                         "Aggregation: none\nExtreme: none() · scope: none")
    assert pf.facts.tables == () and pf.facts.aggs == () and pf.facts.conds == ()
    assert "projection_diff" not in pf.comparable  # 无 Answer columns 行


def test_plan_json_full_depth():
    pf = plan_facts_from_json({
        "tables": ["loan", "account"],
        "joins": "loan.account_id = account.account_id",
        "conditions": [{"field": "loan.status", "op": "=", "value": "A",
                        "note": "finished"}],
        "aggregation": "sum(loan.amount)",
        "extreme": {"func": "max", "column": "loan.date", "scope": "global"},
        "ordering": "",
        "answer_columns": ["account_id"],
    })
    assert pf.comparable == frozenset(
        {"table_set", "join_shape", "filter_diff", "agg_diff",
         "projection_diff"})  # 计划侧五档(R22):排序/取值不可比
    assert pf.facts.aggs == (("sum", (("loan", "amount"),)),)
    assert pf.facts.conds[0].values == ("A",)
    assert pf.facts.projections == ((None, (("", "account_id"),)),)
    assert pf.facts.groups_known is False  # plan 不渲染分组


def test_plan_json_none_aggregation_and_subquery_value():
    pf = plan_facts_from_json({
        "tables": ["d"], "joins": "none",
        "conditions": [{"field": "d.A11", "op": "=",
                        "value": "(SELECT MAX(A11) FROM d)"}],
        "aggregation": "none", "answer_columns": [],
    })
    assert pf.facts.aggs == ()
    assert pf.facts.conds[0].values == ("(subquery)",)


# ── R21–R28:解析不出 ≠ 计划说了「没有」(修订轮,零 LLM 离线实测)─────────


def test_plan_text_truncation_marker_is_stripped():
    cut = (PLAN_TEXT[:PLAN_TEXT.index("Answer columns:")]
           + "Answer columns: account_id…(627 chars)")
    pf = plan_facts_text(cut, truncated=True)
    assert pf.facts.projections == ((None, (("", "account_id"),)),)  # 标记不是列名
    assert "projection_diff" not in pf.comparable                    # 截断:最后一个节退出可比


def test_plan_text_half_cut_header_still_registers_the_section():
    cut = PLAN_TEXT[:PLAN_TEXT.index("Answer columns:")] + "Answer col…(627 chars)"
    pf = plan_facts_text(cut, truncated=True)
    assert "projection_diff" not in pf.comparable
    assert "agg_diff" in pf.comparable   # 半截表头仍登记该节 →「摘最后出现的节」不摘错


def test_plan_agg_none_with_note_is_not_an_aggregate():
    pf = plan_facts_text("Tables: d\nJoins: none\nConditions: none\n"
                         "Aggregation: none (GROUP BY district to list each once)\n"
                         "Answer columns: district")
    assert pf.facts.aggs == () and "agg_diff" in pf.comparable


def test_plan_agg_prose_is_not_comparable():
    pf = plan_facts_text("Tables: d\nJoins: none\nConditions: none\n"
                         "Aggregation: percentage = count(d.x) / count(*) * 100\n"
                         "Answer columns: pct")
    assert "agg_diff" not in pf.comparable       # 读不出 → 退可比集,不猜


def test_plan_agg_note_columns_are_not_extracted():
    pf = plan_facts_text("Tables: d\nJoins: none\nConditions: none\n"
                         "Aggregation: count (percentage = count(d.gender='M') / count(*))\n"
                         "Answer columns: pct")
    assert pf.facts.aggs == (("count", ()),)     # 注里的 d.gender 不是聚合的列
    assert "agg_diff" in pf.comparable


def test_plan_agg_distinct_word_not_substring():
    pf = plan_facts_text("Tables: d\nJoins: none\nConditions: none\n"
                         "Aggregation: count_distinct d.client_id\n"
                         "Answer columns: n")
    assert pf.facts.aggs == (("count_distinct", (("d", "client_id"),)),)


def test_plan_text_in_list_cond_is_readable():
    pf = plan_facts_text("Tables: aa\nJoins: none\n"
                         "Conditions:\n  - aa.k in ('D','B') (eligible)\n"
                         "Aggregation: none\nAnswer columns: x")
    assert pf.facts.conds == (Predicate((("aa", "k"),), "in", ("B", "D")),)
    assert "filter_diff" in pf.comparable


def test_plan_unreadable_cond_drops_filter_depth():
    pf = plan_facts_text("Tables: aa\nJoins: none\n"
                         "Conditions:\n  - aa.k between 1 and 3\n"
                         "Aggregation: none\nAnswer columns: x")
    assert "filter_diff" not in pf.comparable
    assert any(p.op == "raw" for p in pf.facts.conds)


def test_plan_json_comparable_follows_key_presence():
    pf = plan_facts_from_json({"tables": ["aa"]})
    assert pf.comparable == frozenset({"table_set"})


def test_plan_json_ordering_is_not_comparable():
    pf = plan_facts_from_json({
        "tables": ["loan"], "joins": "none", "conditions": [],
        "aggregation": "none", "answer_columns": ["x"],
        "ordering": [{"column": "loan.amount", "direction": "desc"}],
        "limit": 1,
    })
    assert "order_limit_diff" not in pf.comparable
    assert pf.facts.order == ()


def test_plan_note_strip_keeps_note_less_subquery_value():
    # M2:尾注摘除不得吃掉「值本身是子查询」的括号(无尾注时更不该动它)
    pf = plan_facts_text("Tables: aa\nJoins: none\n"
                         "Conditions:\n  - aa.k = (SELECT MAX(k) FROM aa)\n"
                         "Aggregation: none\nAnswer columns: x")
    assert pf.facts.conds == (Predicate((("aa", "k"),), "=", ("(subquery)",)),)
    assert "filter_diff" in pf.comparable


def test_plan_note_with_inner_parens_is_stripped_whole():
    # 尾注自己带括号(实测 4+2 条 IN 条件):从第一个非子查询的 " (" 摘,
    # 既不把注解切进值里,也不因残缺注解把可读条件降成 raw
    listed = plan_facts_text(
        "Tables: loan\nJoins: none\n"
        "Conditions:\n"
        "  - loan.status IN ('B','D') (not eligible (finished unpaid, or running))\n"
        "Aggregation: none\nAnswer columns: x")
    assert listed.facts.conds == (Predicate((("loan", "status"),), "in", ("B", "D")),)
    assert "filter_diff" in listed.comparable
    polluted = plan_facts_text(
        "Tables: district\nJoins: none\n"
        "Conditions:\n"
        "  - district.A11 > 8000 (average income (average salary) exceeds)\n"
        "Aggregation: none\nAnswer columns: x")
    assert polluted.facts.conds == (Predicate((("district", "a11"),), ">", ("8000",)),)


def test_plan_json_in_list_and_unparsed_cond_depth():
    pf = plan_facts_from_json({
        "tables": ["aa"], "joins": "none",
        "conditions": [{"field": "aa.k", "op": "in", "value": ["D", "B"]}],
        "aggregation": "none", "answer_columns": ["x"],
    })
    assert pf.facts.conds == (Predicate((("aa", "k"),), "in", ("B", "D")),)
    assert "filter_diff" in pf.comparable
    # 缺 field 的条件读不出 → filter 档退出可比集(缺键 ≠ 计划说「没有条件」)
    bad = plan_facts_from_json({
        "tables": ["aa"], "joins": "none",
        "conditions": [{"op": "=", "value": "1"}],
        "aggregation": "none", "answer_columns": ["x"],
    })
    assert "filter_diff" not in bad.comparable


def test_alias_resolved_to_base_table_when_used_once():
    # 列限定词写别名还是基表名是拼写:基表只出现一次时归一到基表名
    got = sql_facts("SELECT c.gender FROM client c WHERE c.gender = 'F'")
    assert Predicate((("client", "gender"),), "=", ("F",)) in got.conds
    assert got.projections == ((None, (("client", "gender"),)),)


def test_self_join_aliases_keep_their_identity():
    # 自连接:同一基表出现两次,别名承载身份,绝不合并
    got = sql_facts("SELECT a.x FROM aa a JOIN aa b ON a.i = b.i WHERE a.x = 1")
    assert Predicate((("a", "x"),), "=", ("1",)) in got.conds


def test_alias_vs_base_name_is_not_a_filter_divergence():
    # 实测语料里最常见的假分歧:一侧别名、一侧基表名
    gold = _f("SELECT c.gender FROM client c JOIN disp d ON c.client_id = d.client_id"
              " WHERE c.gender = 'F'")
    pred = _f("SELECT client.gender FROM client JOIN disp"
              " ON client.client_id = disp.client_id WHERE client.gender = 'F'")
    assert ladder(gold, pred, verdict="MISMATCH").bucket == "structure_equal"


# ── R34 后续:`none.` 尾点归一(渲染态直通模型原文)─────────────────────────
# `_render_plan`(query_sketch.py)把模型的 `aggregation` 值 `str(agg)` 直通,
# 模型写 "none." 就渲染成 `Aggregation: none.` —— 它**不是历史遗留**,是活路径。


def test_plan_text_none_with_trailing_period_is_not_an_aggregate():
    pf = plan_facts_text(
        "Tables: aa\nJoins: none\nConditions: none\n"
        "Aggregation: none.\nAnswer columns: x")
    assert pf.facts.aggs == ()          # `none.` 是「没有聚合」,不是叫 `none.` 的聚合
    # 「说了没有」是**读出来了** → 该档仍可比(与「读不出」退可比集相反)
    assert "agg_diff" in pf.comparable


def test_plan_text_trailing_period_in_tables_joins_and_projection():
    pf = plan_facts_text(
        "Tables: none.\nJoins: none...\nConditions: none.\n"
        "Aggregation: none.\nAnswer columns: none.")
    assert pf.facts.tables == ()
    assert pf.facts.join_edges == ()
    assert pf.facts.conds == ()
    assert pf.facts.aggs == ()
    assert pf.facts.projections == ()   # 输出列节同一口径(唯一绕过共享函数的站点)
    assert "projection_diff" in pf.comparable


def test_plan_json_none_with_trailing_period_is_not_an_aggregate():
    pf = plan_facts_from_json({
        "tables": ["aa"], "joins": "none.", "conditions": [],
        "aggregation": "none.", "answer_columns": ["x"],
    })
    assert pf.facts.aggs == () and pf.facts.join_edges == ()
    assert "agg_diff" in pf.comparable


# ── R37:条件子弹的 `none` 判定收窄(文本/JSON 两路径必须同判)───────────────
# `_render_plan`(query_sketch.py)把 `c.get("field")` 未校验地插进
# `  - {field} {op} {value}` —— 缺 field 的条件因此渲染成 `  - None = 1`,行首那个
# `None` 是**缺失字段的占位符**,不是计划说「没有条件」。文本侧曾按 `_is_plan_none`
# 的 `none ` 前缀规则把它当空值记号跳过(conds=()、unparsed=0、filter 档**留在**可比集);
# JSON 孪生(缺 field → `unparsed`)对同一内容把 filter 档**移出**可比集 ——
# 同一份计划、两条路径、相反裁决,而文本侧正是 R24 要消灭的「读不出 = 说没有」方向。
# 裁定(R37):只有**恰为 none 记号**(大小写无关、可带尾点)的子弹才算空值记号;
# 以 none 起头但记号之后还有算子/内容的(`None = 1` / `None in (…)` / 渲染器对
# 非 dict 条件原样直通的 `none (…)` 散文)按**读不出的条件**处理 —— 原文留档并
# `unparsed += 1` → filter 档退出可比集,与 JSON 侧同一裁决。
# `_is_plan_none` 本体不动:节级语义(表/连接/聚合/输出列)保持逐字节不变。


def test_plan_fieldless_cond_is_unreadable_on_both_paths():
    for bullet, cond in (
        ("None = 1", {"op": "=", "value": 1}),          # 缺 field 的对象条件
        ("None in ['B', 'D']", {"op": "in", "value": ["B", "D"]}),
        ("none (GROUP BY district)", "none (GROUP BY district)"),  # 非 dict → 原样直通
    ):
        text = plan_facts_text(f"Tables: aa\nConditions:\n  - {bullet}")
        # 原文留档:绝不造出一条**列名叫 `none`** 的谓词(`_plan_condition("none = 1")`
        # 正是 `Predicate(cols=(('', 'none'),), op='=', values=('1',))`)
        assert text.facts.conds == (Predicate((), "raw", (bullet,)),)
        assert "filter_diff" not in text.comparable   # 读不出 ≠ 计划说「没有条件」
        twin = plan_facts_from_json({
            "tables": ["aa"], "joins": "none", "conditions": [cond],
            "aggregation": "none", "answer_columns": [],
        })
        assert "filter_diff" not in twin.comparable   # 两路径同一裁决


def test_plan_exact_none_cond_bullet_stays_the_none_marker():
    # 方向侧:`- none` 逐字(可带尾点,R35)与行内 `Conditions: none` 都是
    # 「计划说了没有条件」—— 读出来了,故 filter 档**留在**可比集里(与退可比集相反)
    bullet = plan_facts_text("Tables: aa\nJoins: none\nConditions:\n  - none\n"
                             "Aggregation: none\nAnswer columns: x")
    assert bullet.facts.conds == ()
    assert "filter_diff" in bullet.comparable
    dotted = plan_facts_text("Tables: aa\nJoins: none\nConditions:\n  - none.\n"
                             "Aggregation: none\nAnswer columns: x")
    assert dotted.facts.conds == ()                # 尾点同归一(R35)
    assert "filter_diff" in dotted.comparable
    inline = plan_facts_text("Tables: aa\nJoins: none\nConditions: none\n"
                             "Aggregation: none\nAnswer columns: x")
    assert inline.facts.conds == ()
    assert "filter_diff" in inline.comparable


from trove.eval.chain import Trajectory, loop_state, responsibility


def _l1(bucket="table_set"):
    from trove.eval.chain import LadderVerdict
    return LadderVerdict(bucket)


def _traj(**kw):
    base = dict(question="q")
    base.update(kw)
    return Trajectory(**base)


def test_responsibility_no_traj_or_no_fields():
    gold = _f("SELECT a.x FROM aa a")
    assert responsibility(gold=gold, pred=None, l1=_l1(), traj=None)[0] == "undecidable"
    assert responsibility(gold=gold, pred=None, l1=_l1(), traj=_traj())[0] == "undecidable"


def test_linking_missed_table():
    gold = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i")
    pred = _f("SELECT a.x FROM aa a")
    t = _traj(matched_tables=("aa",), matched_tables_state="present",
              available=frozenset({"matched_tables"}))
    bucket, reason = responsibility(gold=gold, pred=pred, l1=_l1(), traj=t)
    assert bucket == "linking_missed_table" and "bb" in reason


def test_linking_empty_state_is_a_fact_not_undecidable():
    gold = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i")
    pred = _f("SELECT a.x FROM aa a")
    t = _traj(matched_tables=(), matched_tables_state="empty",
              available=frozenset({"matched_tables"}))
    assert responsibility(gold=gold, pred=pred, l1=_l1(), traj=t)[0] == "linking_missed_table"


def test_planner_chose_differently():
    gold = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i")
    pred = _f("SELECT a.x FROM aa a JOIN cc c ON c.i = a.i")
    pf = plan_facts_text("Tables: aa, cc\nJoins: cc.i = aa.i\nConditions: none\n"
                         "Aggregation: none\nAnswer columns: x")
    t = _traj(matched_tables=("aa", "bb", "cc"), matched_tables_state="present",
              plan_facts=pf, plan_text_state="present",
              available=frozenset({"matched_tables", "plan"}))
    bucket, reason = responsibility(gold=gold, pred=pred, l1=_l1(), traj=t)
    assert bucket == "planner_chose_differently" and "plan" in reason


def test_gen_ignored_plan():
    gold = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i")
    pred = _f("SELECT a.x FROM aa a")
    pf = plan_facts_text("Tables: aa, bb\nJoins: aa.i = bb.i\nConditions: none\n"
                         "Aggregation: none\nAnswer columns: x")
    t = _traj(matched_tables=("aa", "bb"), matched_tables_state="present",
              plan_facts=pf, plan_text_state="present",
              available=frozenset({"matched_tables", "plan"}))
    assert responsibility(gold=gold, pred=pred, l1=_l1(), traj=t)[0] == "gen_ignored_plan"


def test_depth_insufficient_is_undecidable():
    gold = _f("SELECT a.x, a.y FROM aa a WHERE a.k = 1")
    pred = _f("SELECT a.x FROM aa a WHERE a.k = 1")
    # 计划文本必须"在可比深度内与 gold 无分歧"(R16):gold 带 WHERE a.k = 1,
    # 若写成 Conditions: none,plan 自己先分歧 → 判成 planner_chose_differently。
    pf = plan_facts_text("Tables: aa\nJoins: none\nConditions:\n  - aa.k = 1\n"
                         "Aggregation: none")  # 无 Answer columns 节
    t = _traj(matched_tables=("aa",), matched_tables_state="present",
              plan_facts=pf, plan_text_state="present",
              available=frozenset({"matched_tables", "plan"}))
    v = ladder(gold, pred, verdict="MISMATCH")
    assert v.bucket == "projection_diff"
    bucket, reason = responsibility(gold=gold, pred=pred, l1=v, traj=t)
    assert bucket == "undecidable" and "可比深度" in reason


def test_plan_projection_names_are_not_compared_only_counts():
    """plan 的输出列名是别名、gold 的投影带函数标签:名字不参与比对(R26)。"""
    gold = _f("SELECT sum(a.x) FROM aa a")
    pred = _f("SELECT sum(a.x) FROM aa a WHERE a.k = 1")
    pf = plan_facts_text("Tables: aa\nJoins: none\nConditions: none\n"
                         "Aggregation: sum(aa.x)\nAnswer columns: total")
    t = _traj(matched_tables=("aa",), matched_tables_state="present",
              plan_facts=pf, plan_text_state="present",
              available=frozenset({"matched_tables", "plan"}))
    v = ladder(gold, pred, verdict="MISMATCH")
    assert v.bucket == "filter_diff"
    assert responsibility(gold=gold, pred=pred, l1=v, traj=t)[0] == "gen_ignored_plan"


def test_plan_answer_column_count_mismatch_blames_the_planner():
    """个数分歧才是分歧:plan 写了 2 列、gold 只要 1 列(R26 的正向)。"""
    gold = _f("SELECT a.x FROM aa a")
    pred = _f("SELECT a.x, a.y FROM aa a")
    pf = plan_facts_text("Tables: aa\nJoins: none\nConditions: none\n"
                         "Aggregation: none\nAnswer columns: x, y")
    t = _traj(matched_tables=("aa",), matched_tables_state="present",
              plan_facts=pf, plan_text_state="present",
              available=frozenset({"matched_tables", "plan"}))
    v = ladder(gold, pred, verdict="MISMATCH")
    assert v.bucket == "projection_diff"
    bucket, reason = responsibility(gold=gold, pred=pred, l1=v, traj=t)
    assert bucket == "planner_chose_differently" and "projection_diff" in reason


def test_l1_outside_structural_rungs_is_undecidable_with_honest_reason():
    """结构梯看不见的分歧(L1=structure_equal)不归给任何一跳,理由如实说(R17)。"""
    gold = _f("SELECT a.x FROM aa a")
    pred = _f("SELECT a.x FROM aa a")
    pf = plan_facts_text("Tables: aa\nJoins: none\nConditions: none\n"
                         "Aggregation: none\nAnswer columns: x")
    t = _traj(matched_tables=("aa",), matched_tables_state="present",
              plan_facts=pf, plan_text_state="present",
              available=frozenset({"matched_tables", "plan"}))
    v = ladder(gold, pred, verdict="MISMATCH")
    assert v.bucket == "structure_equal"
    bucket, reason = responsibility(gold=gold, pred=pred, l1=v, traj=t)
    assert bucket == "undecidable"
    assert "结构" in reason and "可比深度" not in reason


def test_gen_side_failures_fall_to_gen_when_plan_clean():
    gold = _f("SELECT a.x FROM aa a JOIN bb b ON a.i = b.i")
    pf = plan_facts_text("Tables: aa, bb\nJoins: aa.i = bb.i\nConditions: none\n"
                         "Aggregation: none\nAnswer columns: x")
    t = _traj(matched_tables=("aa", "bb"), matched_tables_state="present",
              plan_facts=pf, plan_text_state="present",
              available=frozenset({"matched_tables", "plan"}))
    bucket, _ = responsibility(gold=gold, pred=None, l1=_l1("no_sql"), traj=t)
    assert bucket == "gen_ignored_plan"


def test_loop_states():
    assert loop_state(None) == "undecidable"
    assert loop_state(_traj()) == "undecidable"
    assert loop_state(_traj(validation_hits_raw='[{"rule": "x"}]',
                            available=frozenset({"validation_hits"}))) == "detected_but_unfixed"
    assert loop_state(_traj(validation_hits_raw="[]", retry_count=0,
                            available=frozenset({"validation_hits", "retry_count"}))) == "never_detected"
    assert loop_state(_traj(retry_count=2, available=frozenset({"retry_count"}))) == "detected_but_unfixed"
    assert loop_state(_traj(rollback_target="gen_sql",
                            available=frozenset({"rollback_target"}))) == "detected_but_unfixed"


# ── 逐行归因 + 报告组装/渲染(设计 §4 报告键 / §5 文本分节)─────────────
# **行与题双计数**:97 行只有 24 题,行数偏袒重试题 —— 同一题的两行必须同时进
# 行计数(行=样本量),而题计数是独立事件数。只报一个都读错。

from trove.eval.attribution import FailureRow
from trove.eval.chain import attribute_row, build_report, render_report


def _row(q="q1", gold="SELECT a.x FROM aa a", pred="SELECT a.y FROM aa a", verdict="MISMATCH"):
    return FailureRow(question=q, gold_sql=gold, pred_sql=pred, verdict=verdict)


def test_attribute_row_without_traj():
    f = attribute_row(_row(), None)
    assert f.l1.bucket == "projection_diff"
    assert f.l2 == "undecidable" and f.loop == "undecidable"


def test_report_double_counting_and_shape():
    rows = [_row(q="q1"), _row(q="q1"), _row(q="q2", pred="")]
    rep = build_report(rows, {}, dialect="mysql", meta={"failures": "x"})
    assert rep["counts"]["rows"] == 3
    assert rep["counts"]["questions"] == 2
    # 题 q1 有两行(同一题重试),行计数=2、题计数=1 —— 双计数的全部意义(R18)
    assert rep["counts"]["l1"]["projection_diff"] == 2
    assert rep["counts"]["l1"]["no_sql"] == 1
    assert rep["counts"]["l1_by_question"]["projection_diff"] == 1
    assert set(rep) >= {"meta", "counts", "coverage", "observations", "samples"}


def test_report_deterministic():
    rows = [_row(), _row(q="q2")]
    a = build_report(rows, {}, dialect="mysql", meta={"failures": "x"})
    b = build_report(rows, {}, dialect="mysql", meta={"failures": "x"})
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_render_sections():
    rep = build_report([_row()], {}, dialect="mysql", meta={"failures": "x"})
    text = render_report(rep)
    for token in ("L1", "L2", "loop", "覆盖", "观察"):
        assert token in text


def test_empty_rows_report_zeros():
    rep = build_report([], {}, dialect="mysql", meta={})
    assert rep["counts"]["rows"] == 0
    assert render_report(rep)


def test_report_counts_truncated_plans():
    """plan 被截断的行数进 coverage,并在文本里露出来(R20)。"""
    t = Trajectory(question="q1", plan_truncated=True)
    rep = build_report([_row()], {"q1": t}, dialect="mysql", meta={})
    assert rep["coverage"]["plan_truncated_rows"] == 1
    assert "plan 被截断行=1" in render_report(rep)


def test_trajectory_lookup_strips_question_whitespace():
    """失败行的问题串带空白时仍要对得上轨迹 —— 对不上时 plan_truncated 恒 0(R20 的死法)。"""
    t = Trajectory(question="q1", plan_truncated=True)
    rep = build_report([_row(q="  q1  ")], {"q1": t}, dialect="mysql", meta={})
    assert rep["coverage"]["plan_truncated_rows"] == 1


def test_report_bytes_stable_across_hash_seeds():
    """同输入跨进程逐字节一致(PYTHONHASHSEED 三连子进程钉死,与 decompose 同款)。

    报告要被当基线比对,前提就是它**不随集合/字典迭代序漂移**:``observations`` /
    ``samples`` / ``loop_by_l2`` 都是聚合字典,``json.dumps`` 不带 ``sort_keys`` 时
    插入序就是输出序 —— 这里同时钉住 JSON 与渲染文本(渲染自成一套排序,两处都要稳)。
    fixture 覆盖四个观察码与全部计数块,空桶则完全不出现(逐字节比较才对得上)。
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    code = """
import json
from trove.eval.attribution import FailureRow
from trove.eval.chain import (
    Trajectory, build_report, plan_facts_from_json, render_report,
)

rows = [
    FailureRow(question="q1", gold_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i",
               pred_sql="SELECT a.y FROM aa a", verdict="MISMATCH"),
    FailureRow(question="q1", gold_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i",
               pred_sql="SELECT a.y FROM aa a", verdict="MISMATCH"),
    FailureRow(question="q2", gold_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i",
               pred_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i"
                        " JOIN cc c ON c.i = a.i", verdict="MISMATCH"),
    FailureRow(question="q3", gold_sql="SELECT a.x FROM aa a WHERE a.gender = 'F'",
               pred_sql="SELECT a.x FROM aa a WHERE a.gender = 'f'",
               verdict="MISMATCH"),
    FailureRow(question="q4", gold_sql="SELECT a.x FROM aa a ORDER BY a.x DESC LIMIT 1",
               pred_sql="SELECT MAX(a.x) FROM aa a", verdict="MISMATCH"),
    FailureRow(question="q5", gold_sql="SELECT a.x FROM aa a", pred_sql="",
               verdict="REFUSED"),
]
trajs = {
    "q1": Trajectory(
        question="q1", source="runlog", matched_tables=("aa",),
        matched_tables_state="present",
        plan_facts=plan_facts_from_json({
            "tables": ["aa", "bb"], "joins": "aa.i = bb.i", "conditions": [],
            "aggregation": "none", "answer_columns": ["x"],
        }),
        plan_text_state="present", plan_truncated=True,
        validation_hits_raw='[{"rule": "f1_shape"}]',
        available=frozenset({"matched_tables", "plan", "validation_hits"}),
    ),
}
rep = build_report(rows, trajs, dialect="mysql",
                   meta={"failures": "failures.jsonl", "dialect": "mysql"})
print(json.dumps(rep, ensure_ascii=False))
print(render_report(rep))
"""
    outs = set()
    for seed in ("0", "1", "999"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=env, cwd=str(root), timeout=120)
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout)
    assert len(outs) == 1, f"跨进程不一致: {outs}"


def _aggregate_rows_and_trajs():
    from trove.eval.chain import plan_facts_from_json
    rows = [
        FailureRow(question="q1",
                   gold_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i",
                   pred_sql="SELECT a.y FROM aa a", verdict="MISMATCH"),
        FailureRow(question="q1",
                   gold_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i",
                   pred_sql="SELECT a.y FROM aa a", verdict="MISMATCH"),
        FailureRow(question="q2",
                   gold_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i",
                   pred_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i"
                            " JOIN cc c ON c.i = a.i", verdict="MISMATCH"),
        FailureRow(question="q3",
                   gold_sql="SELECT a.x FROM aa a WHERE a.gender = 'F'",
                   pred_sql="SELECT a.x FROM aa a WHERE a.gender = 'f'",
                   verdict="MISMATCH"),
        FailureRow(question="q4",
                   gold_sql="SELECT a.x FROM aa a ORDER BY a.x DESC LIMIT 1",
                   pred_sql="SELECT MAX(a.x) FROM aa a", verdict="MISMATCH"),
        FailureRow(question="q5", gold_sql="SELECT a.x FROM aa a", pred_sql="",
                   verdict="REFUSED"),
    ]
    trajs = {"q1": Trajectory(
        question="q1", source="runlog", matched_tables=("aa",),
        matched_tables_state="present",
        plan_facts=plan_facts_from_json({
            "tables": ["aa", "bb"], "joins": "aa.i = bb.i",
            "conditions": [], "aggregation": "none", "answer_columns": ["x"]}),
        plan_text_state="present", plan_truncated=True,
        validation_hits_raw='[{"rule": "f1_shape"}]',
        available=frozenset({"matched_tables", "plan", "validation_hits"}))}
    return rows, trajs


def test_report_aggregate_blocks_positive():
    rows, trajs = _aggregate_rows_and_trajs()
    rep = build_report(rows, trajs, dialect="mysql", meta={"failures": "x"})
    c = rep["counts"]
    assert c["rows"] == 6 and c["questions"] == 5
    assert c["l1"] == {"no_sql": 1, "table_set": 3, "filter_diff": 1, "agg_diff": 1}
    assert c["l1_by_question"] == {"no_sql": 1, "table_set": 2,
                                   "filter_diff": 1, "agg_diff": 1}
    assert c["l2"] == {"linking_missed_table": 2, "undecidable": 4}
    assert c["l2_by_question"] == {"linking_missed_table": 1, "undecidable": 4}
    assert c["loop"] == {"detected_but_unfixed": 2, "undecidable": 4}
    assert c["loop_by_l2"] == {
        "linking_missed_table": {"detected_but_unfixed": 2},
        "undecidable": {"undecidable": 4}}
    cov = rep["coverage"]
    assert cov["l1_judged_rows"] == 6
    assert cov["l2_judged_rows"] == 2
    assert cov["plan_truncated_rows"] == 2
    assert cov["fidelity"] == {
        "matched_tables": {"present": 2, "empty": 0, "absent": 4},
        "plan": {"present": 2, "empty": 0, "absent": 4}}
    assert rep["observations"] == {
        "case_only_value_diff": 1, "extra_table_read": 1,
        "extreme_style_mismatch": 1, "needed_table_never_read": 2}
    l1s, l2s = rep["samples"]["l1"], rep["samples"]["l2"]
    assert set(l1s) == {"table_set", "filter_diff", "agg_diff", "no_sql"}
    assert {s["question"] for s in l1s["table_set"]} == {"q1", "q2"}
    assert len(l1s["table_set"]) >= 2
    assert set(l2s) == {"linking_missed_table", "undecidable"}
    assert all(s["l2_reason"] for s in l2s["linking_missed_table"])
    assert {s["question"] for s in l2s["undecidable"]} == {"q2", "q3", "q4"}
    assert all({"question", "reason", "l2_reason"} <= set(s)
               for bag in (l1s, l2s) for b in bag.values() for s in b)
    text = render_report(rep)
    assert "L1 可判行=6" in text
    assert "L2 可判行=2" in text
    assert "plan 被截断行=2" in text
    assert "needed_table_never_read: 2" in text
    assert "[l2:linking_missed_table]" in text


def test_refused_row_does_not_use_residual_pred_for_link_attribution():
    """REFUSED 行的 pred 是残留:拿它当「已读表」会把 linking_missed_table 压成 gen_ignored_plan。"""
    from trove.eval.chain import plan_facts_from_json
    row = FailureRow(
        question="q1",
        gold_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i",
        pred_sql="SELECT a.x FROM aa a JOIN bb b ON a.i = b.i",  # 上一轮残留
        verdict="REFUSED")
    traj = Trajectory(
        question="q1", source="runlog", matched_tables=("aa",),
        matched_tables_state="present",
        plan_facts=plan_facts_from_json({
            "tables": ["aa", "bb"], "joins": "aa.i = bb.i",
            "conditions": [], "aggregation": "none", "answer_columns": ["x"]}),
        plan_text_state="present")
    f = attribute_row(row, traj)
    assert f.l1.bucket == "no_sql"
    assert f.l2 == "linking_missed_table", f.l2_reason


# ── 末轮复审 I-2 / M-1..M-3:plan 侧归一化与报告口径 ─────────────────


def test_plan_json_quoted_scalar_is_unquoted_before_comparison():
    """I-2:plan prompt 的示例值写成 ``"'b'"``,引号是拼写 —— 与 SQL 侧去引号后比。"""
    from trove.eval.chain import _diff_rungs, plan_facts_from_json
    pf = plan_facts_from_json({
        "tables": ["aa"], "joins": "none",
        "conditions": [{"field": "aa.k", "op": "=", "value": "'b'"}],
        "aggregation": "none", "answer_columns": ["x"]})
    assert pf.facts.conds == (Predicate((("aa", "k"),), "=", ("b",)),)
    gold = _f("SELECT x FROM aa WHERE aa.k = 'b'")
    assert _diff_rungs(gold, pf.facts, frozenset({"filter_diff"})) == []


def test_plan_text_quoted_scalar_is_unquoted():
    from trove.eval.chain import plan_facts_text
    pf = plan_facts_text("Tables: aa\nJoins: none\n"
                         "Conditions:\n  - aa.k = 'b'\n"
                         "Aggregation: none\nAnswer columns: x")
    assert pf.facts.conds == (Predicate((("aa", "k"),), "=", ("b",)),)
    assert "filter_diff" in pf.comparable


def test_plan_json_in_value_with_outer_parens():
    """I-2:JSON 的 in 值可能是带括号的字符串 ``"('B','D')"``,与列表形态同口径。"""
    from trove.eval.chain import plan_facts_from_json
    pf = plan_facts_from_json({
        "tables": ["aa"], "joins": "none",
        "conditions": [{"field": "aa.k", "op": "in", "value": "('B','D')"}],
        "aggregation": "none", "answer_columns": ["x"]})
    assert pf.facts.conds == (Predicate((("aa", "k"),), "in", ("B", "D")),)
    assert "filter_diff" in pf.comparable


def test_plan_neq_spellings_are_the_same_operator():
    """I-2:``<>`` 与 ``!=`` 是同一个 NEQ(SQL 侧 sqlglot 两者都归 NEQ→``!=``)。"""
    from trove.eval.chain import plan_facts_from_json, plan_facts_text
    for op in ("<>", "!="):
        pf = plan_facts_from_json({
            "tables": ["aa"], "joins": "none",
            "conditions": [{"field": "aa.k", "op": op, "value": "1"}],
            "aggregation": "none", "answer_columns": ["x"]})
        assert pf.facts.conds == (Predicate((("aa", "k"),), "!=", ("1",)),), op
    text = plan_facts_text("Tables: aa\nJoins: none\n"
                           "Conditions:\n  - aa.k <> 1\n"
                           "Aggregation: none\nAnswer columns: x")
    assert text.facts.conds == (Predicate((("aa", "k"),), "!=", ("1",)),)


def test_plan_unsafe_ops_drop_filter_depth():
    """I-2:is / not in / between / ilike 在 SQL 侧是 raw,计划侧也绝不当确定谓词。"""
    from trove.eval.chain import plan_facts_from_json
    for op, value in (("is", "null"), ("not in", "('a')"),
                      ("between", "1 AND 2"), ("ilike", "'a%'")):
        pf = plan_facts_from_json({
            "tables": ["aa"], "joins": "none",
            "conditions": [{"field": "aa.k", "op": op, "value": value}],
            "aggregation": "none", "answer_columns": ["x"]})
        assert "filter_diff" not in pf.comparable, op
        assert any(p.op == "raw" for p in pf.facts.conds), op


def test_report_samples_include_pred_sql_and_dedupe_by_stripped_question():
    """M-1/M-2:样本带 pred_sql、同题只占一席;题数/样本用 strip 后的问句。"""
    gold, pred = "SELECT a.x FROM aa a", "SELECT a.y FROM aa a"
    rows = [FailureRow(question=" q1 ", gold_sql=gold, pred_sql=pred,
                       verdict="MISMATCH"),
            FailureRow(question="q1", gold_sql=gold, pred_sql=pred,
                       verdict="MISMATCH"),
            FailureRow(question="q2", gold_sql=gold, pred_sql=pred,
                       verdict="MISMATCH")]
    rep = build_report(rows, {}, dialect="mysql")
    bag = rep["samples"]["l1"]["projection_diff"]
    assert [s["question"] for s in bag] == ["q1", "q2"]
    assert all(s["pred_sql"] == pred for s in bag)
    assert rep["counts"]["questions"] == 2
    assert rep["counts"]["l1_by_question"]["projection_diff"] == 2


def test_render_l2_samples_use_l2_reason_and_show_pred():
    """M-1:L2 样本此前打印的是 L1 原因;现在给责任跳理由并露出 pred SQL。"""
    rows, trajs = _aggregate_rows_and_trajs()
    text = render_report(build_report(rows, trajs, dialect="mysql", meta={}))
    assert "[l2:linking_missed_table]" in text
    assert "gold 需要的表未进 matched_tables" in text
    assert "pred: SELECT" in text


def test_attribute_row_none_pred_sql_is_no_sql_not_crash():
    """M-3:不经 load_rows 的直接构造可以给 None —— 按无 SQL 处理,不许崩。"""
    row = FailureRow(question="q", gold_sql="SELECT a.x FROM aa a",
                     pred_sql=None, verdict="MISMATCH")
    f = attribute_row(row, None)
    assert f.l1.bucket == "no_sql"
