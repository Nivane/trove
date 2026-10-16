"""生成链归因纯核心的测试:L1 档位 / 事实抽取 / plan 解析 / 责任跳 / 报告。"""
from __future__ import annotations

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
