"""双跑诊断的**口径层**测试 —— 零 LLM、零网络、零 KB 文件。

这组测试钉住的是「不一致率」这个数字本身。诊断脚本只有一件事会被
人引用:那个百分比。口径漂了,指标就从信号变成噪声源。所以这里不测
「脚本能跑」(那是 IO 层,薄到不值得测),只回答三问:

1. 什么算**一致**?——措辞不同但等价(大小写/空白/别名/裸列 vs 限定列/
   ``COUNT(*)`` vs ``COUNT(col)``)都算一致。判据复用生产守卫
   (``compiled_sql_matches``)的签名比较,不是字符串相等。
2. 什么算**不一致**?——改聚合列/改过滤值/丢条件/换表/多出 ORDER BY。
   其中 ORDER BY / GROUP BY 列 / LIMIT / DISTINCT 是生产签名**刻意不看**
   的维度,本诊断补了尾项守卫,否则「编译路径多排个序」会静默算一致。
3. 一条路径缺失时算什么?——**既不算一致也不算不一致**:不进分母,
   单独计数。快径 miss 是常态(它只服务单表全局聚合的平凡题),
   把这堆算成「一致」会让指标看起来漂亮而毫无意义。
"""

from __future__ import annotations

from scripts.dual_run_diagnose import (
    AGREE,
    DISAGREE,
    FAST_MISSED,
    UNDECIDABLE,
    Row,
    aggregate_intent,
    declared_conditions,
    diagnose_row,
    equivalent_sql,
    resolve_metric,
    summarize,
    tables_in,
    where_columns,
)
from trove.services.kb.service import ExampleHit
from trove.services.semantic_layer.models import (
    SemanticDataset,
    SemanticField,
    SemanticMetric,
    SemanticModel,
)

# ── 夹具 ────────────────────────────────────────────────


def _loan_model() -> SemanticModel:
    """最小模型:一张 loan 表 + 三种聚合指标 + 一个枚举字段。

    枚举的 ``enum_display`` 刻意让 code 与 label 不同形(``B``/``bad``),
    这样「label 出现在问题里 → 编译出 code」这条链路才真的被走到。
    """
    return SemanticModel(
        name="fin",
        datasets=[
            SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
                SemanticField(name="loan_id", expression="loan_id",
                              semantic_role="identifier"),
                SemanticField(name="amount", expression="amount",
                              semantic_role="measure"),
                SemanticField(name="duration", expression="duration",
                              semantic_role="measure"),
                # payments 必须在:它是 "maximum Monthly payment amount" 那条
                # 假不一致的触发条件(financial 的 loan 表真有这一列)。
                # 少了它,守卫测试就变成在测一个不可能发生的输入。
                SemanticField(name="payments", expression="payments",
                              semantic_role="measure"),
                SemanticField(name="status", expression="status",
                              semantic_role="enum",
                              enum_display={"A": "good", "B": "bad"}),
            ]),
        ],
        metrics=[
            SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                           datasets=["loan"]),
            SemanticMetric("total Loan amount in monetary units.",
                           "SUM(loan.amount)", datasets=["loan"]),
            SemanticMetric("max_loan_amount", "MAX(loan.amount)", datasets=["loan"]),
        ],
    )


def _hit(question: str, sql: str, table: str = "loan") -> ExampleHit:
    return ExampleHit(question=question, sql=sql, tags=[table], template=True)


# ── 1. 什么算「一致」 ────────────────────────────────────


def test_identical_sql_is_equivalent():
    v = equivalent_sql("SELECT COUNT(*) FROM loan", "SELECT COUNT(*) FROM loan")
    assert v.status == AGREE


def test_case_whitespace_and_column_qualification_are_not_disagreements():
    """同一件事的两种写法:大小写、换行、裸列 vs 限定列、COUNT(*) vs COUNT(col)。

    ``COUNT(*)`` 与 ``COUNT(col)`` 互认是生产签名里唯一有意的放宽(空列集
    通配)——这里必须继承,否则快径的 ``COUNT(*)`` 模板会把每一条
    ``COUNT(table.pk)`` 的编译产物都打成不一致,指标直接爆掉。
    """
    fast = "select count(*)  from loan   where status='B'"
    compiled = (
        "SELECT COUNT(loan.loan_id) FROM loan AS loan\n"
        "WHERE loan.status = 'B'"
    )
    assert equivalent_sql(fast, compiled).status == AGREE


# ── 2. 什么算「不一致」 ─────────────────────────────────


def test_changed_aggregate_column_is_a_disagreement():
    v = equivalent_sql("SELECT MAX(amount) FROM loan",
                       "SELECT MAX(duration) FROM loan")
    assert v.status == DISAGREE


def test_changed_filter_value_is_a_disagreement():
    v = equivalent_sql("SELECT COUNT(*) FROM loan WHERE status = 'A'",
                       "SELECT COUNT(*) FROM loan WHERE status = 'B'")
    assert v.status == DISAGREE


def test_changed_filter_column_is_a_disagreement():
    v = equivalent_sql("SELECT COUNT(*) FROM loan WHERE status = 'A'",
                       "SELECT COUNT(*) FROM loan WHERE duration = 'A'")
    assert v.status == DISAGREE


def test_dropped_filter_is_a_disagreement():
    """丢条件是双跑最该抓的一类:静默多算行。"""
    v = equivalent_sql("SELECT COUNT(*) FROM loan WHERE status = 'A'",
                       "SELECT COUNT(*) FROM loan")
    assert v.status == DISAGREE


def test_extra_order_by_is_a_disagreement():
    """ORDER BY 是生产签名**不看**的维度(LLM 补排序被判合格)。

    快径模板的形状守卫排除了 ORDER BY,所以「编译侧多给一个排序」在
    快径语境下是偏离而不是等价改写 —— 尾项守卫专治这一路静默。
    """
    v = equivalent_sql("SELECT COUNT(*) FROM loan",
                       "SELECT COUNT(*) FROM loan ORDER BY loan.amount DESC")
    assert v.status == DISAGREE


def test_extra_group_by_is_a_disagreement():
    """GROUP BY 生产签名只比**个数**;个数相同、列不同时它放行。

    这里补到列级 —— 「按 duration 分组」与「按 status 分组」是两张表。
    """
    v = equivalent_sql("SELECT duration, COUNT(*) FROM loan GROUP BY duration",
                       "SELECT duration, COUNT(*) FROM loan GROUP BY status")
    assert v.status == DISAGREE


def test_limit_difference_is_a_disagreement():
    v = equivalent_sql("SELECT loan_id FROM loan",
                       "SELECT loan_id FROM loan LIMIT 10")
    assert v.status == DISAGREE


def test_distinct_difference_is_a_disagreement():
    v = equivalent_sql("SELECT COUNT(loan_id) FROM loan",
                       "SELECT COUNT(DISTINCT loan_id) FROM loan")
    assert v.status == DISAGREE


# ── 3. 一条路径缺失 / 判不出来时算什么 ───────────────────


def test_unparseable_sql_is_undecidable_rather_than_agreement():
    """半个 SQL 不能因为「都没解析出来」而被判一致。

    这是整套口径里最容易写错的一处:解析失败时两侧都拿到 None,写
    ``return None is None`` 就把「无法判定」伪装成了「一致」——指标会
    朝着好看的方向错。
    """
    v = equivalent_sql("SELECT COUNT(*", "SELECT COUNT(*) FROM loan")
    assert v.status == UNDECIDABLE
    assert v.reason


def test_two_unparseable_sqls_are_still_undecidable():
    v = equivalent_sql("not sql at all", "not sql at all")
    assert v.status == UNDECIDABLE


def test_fast_path_miss_is_excluded_from_the_rate():
    """快径没命中 → 不跑编译,也不进分母。"""
    model = _loan_model()
    row = diagnose_row(
        question="How many loans are there per region?",
        matched_tables=["loan"], hits=[_hit("How many loan records?", "SELECT COUNT(*) FROM loan")],
        model=model,
    )
    assert row.status == FAST_MISSED
    rep = summarize([row], corpus="x", datasource="fin", dialect="sqlite", command="cmd")
    assert rep.n_comparable == 0
    assert rep.rate is None  # 0/0 不是 0.0%


def test_empty_denominator_reports_no_rate_rather_than_zero():
    rep = summarize([], corpus="x", datasource="fin", dialect="sqlite", command="cmd")
    assert rep.rate is None
    assert "0.0%" not in "\n".join(rep.render())


def test_compile_miss_is_excluded_from_the_rate():
    """快径命中但声明模型编译不出来 → 不可比,不进分母。"""
    model = _loan_model()
    # 快径命中 MAX(payments),但模型里没有度量 payments 的 MAX 指标 → 不猜
    row = diagnose_row(
        question="What is the maximum Monthly payment amount?",
        matched_tables=["loan"],
        hits=[_hit("What is the maximum Monthly payment amount?",
                   "SELECT MAX(payments) FROM loan")],
        model=model,
    )
    assert row.status != AGREE
    rep = summarize([row], corpus="x", datasource="fin", dialect="sqlite", command="cmd")
    assert rep.n_comparable == 0


def test_report_counts_every_non_comparable_row_by_reason():
    """分母之外的东西必须可见:只报一个率,读者无法判断它覆盖了多少题。"""
    rows = [
        Row(question="a", status=AGREE),
        Row(question="b", status=DISAGREE, reason="shape"),
        Row(question="c", status=FAST_MISSED),
        Row(question="d", status=FAST_MISSED),
    ]
    rep = summarize(rows, corpus="x", datasource="fin", dialect="sqlite", command="cmd")
    assert (rep.n_agree, rep.n_disagree, rep.n_comparable) == (1, 1, 2)
    assert rep.excluded() == {FAST_MISSED: 2}
    assert rep.rate == 0.5


# ── 4. 计划合成:问题 → 声明度量 / 声明过滤 ──────────────


def test_aggregate_intent_reads_the_question_not_the_sql():
    assert aggregate_intent("How many loan records are there?") == "COUNT"
    assert aggregate_intent("What is the total Loan amount?") == "SUM"
    assert aggregate_intent("What is the maximum Loan duration?") == "MAX"
    assert aggregate_intent("List the loans") == ""


def test_resolve_metric_uses_column_evidence_from_the_question():
    m, why = resolve_metric("What is the total Loan amount in monetary units.?",
                            ["loan"], _loan_model())
    assert why == ""
    assert m is not None
    assert m.name == "total Loan amount in monetary units."


def test_resolve_metric_refuses_a_metric_that_ignores_a_named_field():
    """问题点到已声明字段而候选指标不度量它 → 不猜。

    回归的是原型上真实出现过的一次误判:``maximum Monthly payment amount``
    被解析成 ``max_loan_amount``(唯一 MAX 指标 + 共享 "amount" 词),于是
    诊断报出一条**假不一致** —— 快径的 ``MAX(payments)`` 其实是对的。
    代价是这类问题落进「不可比」桶:少一条数据,好过多一条假数据。
    """
    m, why = resolve_metric("What is the maximum Monthly payment amount?",
                            ["loan"], _loan_model())
    assert m is None
    assert why


def test_ambiguous_metric_is_not_guessed():
    """两个指标同样贴合 → 不猜(猜错就制造一条假不一致)。"""
    model = SemanticModel(
        name="fin",
        datasets=[SemanticDataset(name="loan", primary_key=["loan_id"], fields=[
            SemanticField(name="loan_id", expression="loan_id",
                          semantic_role="identifier"),
            SemanticField(name="amount", expression="amount",
                          semantic_role="measure"),
        ])],
        metrics=[
            SemanticMetric("total loan amount", "SUM(loan.amount)", datasets=["loan"]),
            SemanticMetric("sum of loan amount", "SUM(loan.amount)", datasets=["loan"]),
        ],
    )
    m, why = resolve_metric("What is the total loan amount?", ["loan"], model)
    assert m is None
    assert why


def test_declared_conditions_maps_a_display_label_to_the_declared_code():
    """问题说 ``bad``,库里存 ``B``:桥由**模型声明**提供,不由诊断脚本提供。"""
    conds, why = declared_conditions("How many loan records are bad?", ["loan"],
                                     _loan_model())
    assert why == ""
    assert conds == [{"field": "loan.status", "op": "=", "value": "bad"}]


def test_declared_conditions_gives_nothing_when_the_question_names_no_label():
    conds, why = declared_conditions("How many loan records are there?", ["loan"],
                                     _loan_model())
    assert conds == []
    assert why == ""


def test_ambiguous_label_within_one_field_is_not_guessed():
    """同一字段两个标签同时出现("good or bad")→ 值歧义,不猜。"""
    conds, why = declared_conditions("How many loans are good or bad?", ["loan"],
                                     _loan_model())
    assert conds is None
    assert why


# ── 5. SQL 上的两个小工具 ───────────────────────────────


def test_tables_in_reads_from_and_join_tables():
    assert tables_in("SELECT COUNT(*) FROM loan") == ["loan"]
    assert tables_in("SELECT COUNT(*) FROM LOAN AS l JOIN account AS a "
                     "ON l.account_id = a.account_id") == ["account", "loan"]
    assert tables_in("not sql") == []


def test_where_columns_reads_the_filter_columns():
    assert where_columns("SELECT COUNT(*) FROM loan WHERE status = 'B'") == {"status"}
    assert where_columns("SELECT COUNT(*) FROM loan") == set()


# ── 6. 端到端(仍无 LLM、无 IO):快径 + 编译 + 比对 ──────


def test_diagnose_row_agrees_when_template_and_declared_metric_say_the_same_thing():
    """``COUNT(*)`` 模板 vs ``COUNT(loan.loan_id)`` 指标 —— 口径上一致。"""
    model = _loan_model()
    row = diagnose_row(
        question="How many loan records are bad?",
        matched_tables=["loan"],
        hits=[_hit("How many loan records are bad?",
                   "SELECT COUNT(*) FROM loan WHERE status = 'B'")],
        model=model,
    )
    assert row.status == AGREE, row
    assert row.metric == "number of loan records"
    assert row.fast_sql and row.compiled_sql


def test_diagnose_row_flags_a_template_that_filters_on_undeclared_vocabulary():
    """快径 SQL 过滤在一个声明词表里没有的列上 → 不可比,而不是不一致。

    这条是**假阳性的主要防线**:问题 "are B?" 里的 ``B`` 在声明的
    ``enum_display`` 里查不到(声明只有 label ``bad``),诊断侧无法把它
    合成成条件。此时编译产物必然少一个 WHERE,若不拦就报成假不一致。
    """
    model = _loan_model()
    row = diagnose_row(
        question="How many loan records are B?",
        matched_tables=["loan"],
        hits=[_hit("How many loan records are B?",
                   "SELECT COUNT(*) FROM loan WHERE status = 'B'")],
        model=model,
    )
    assert row.status != DISAGREE
    assert row.status != AGREE
    assert row.reason


def test_diagnose_row_reports_a_real_disagreement_end_to_end():
    """模板算错的列 vs 声明指标的列 —— 这条必须被抓住。"""
    model = _loan_model()
    row = diagnose_row(
        question="What is the total Loan amount in monetary units.?",
        matched_tables=["loan"],
        hits=[_hit("What is the total Loan amount in monetary units.?",
                   "SELECT SUM(duration) FROM loan")],
        model=model,
    )
    assert row.status == DISAGREE, row
    assert "SUM" in row.fast_sql and "SUM" in row.compiled_sql


def test_declared_row_filter_is_applied_to_both_sides_not_just_the_compiled_one():
    """声明了 ``row_filter`` 的模型:快径侧必须同样注入,否则全是假不一致。

    线上快径不过编译器,行级谓词由 ``fast_match`` 自己注入。诊断里少做这
    一步,就会拿「无过滤的快径」比「有过滤的编译」—— 每一条都报不一致,
    而且报得理直气壮。这条测试钉住这次复现。
    """
    model = SemanticModel(
        name="fin",
        datasets=[SemanticDataset(name="loan", primary_key=["loan_id"],
                                  row_filter="status = 'A'", fields=[
            SemanticField(name="loan_id", expression="loan_id",
                          semantic_role="identifier"),
            SemanticField(name="status", expression="status", semantic_role="enum",
                          enum_display={"A": "good", "B": "bad"}),
        ])],
        metrics=[SemanticMetric("number of loan records", "COUNT(loan.loan_id)",
                                datasets=["loan"])],
    )
    row = diagnose_row(
        question="How many loan records are there?",
        matched_tables=["loan"],
        hits=[_hit("How many loan records are there?", "SELECT COUNT(*) FROM loan")],
        model=model,
    )
    assert row.status == AGREE, row
    assert "status = 'A'" in row.fast_sql
    assert "status = 'A'" in row.compiled_sql


# ── 7. 可复现:率 + 明细 + 命令行 ────────────────────────


def test_render_carries_the_command_line_and_both_sqls_of_every_disagreement():
    """一个光秃秃的百分比不可复核。明细必须带两侧 SQL 与跑法。"""
    rows = [
        Row(question="q1", status=AGREE, fast_sql="SELECT 1", compiled_sql="SELECT 1"),
        Row(question="q2", status=AGREE, fast_sql="SELECT 2", compiled_sql="SELECT 2"),
        Row(question="q3", status=DISAGREE, reason="shape",
            fast_sql="SELECT MAX(duration) FROM loan",
            compiled_sql="SELECT MAX(loan.amount) FROM loan", metric="max_loan_amount"),
    ]
    rep = summarize(rows, corpus="corpus.yml", datasource="fin", dialect="sqlite",
                    command="uv run python scripts/dual_run_diagnose.py --datasource fin")
    text = "\n".join(rep.render())
    assert "uv run python scripts/dual_run_diagnose.py --datasource fin" in text
    assert "corpus.yml" in text
    assert "SELECT MAX(duration) FROM loan" in text
    assert "SELECT MAX(loan.amount) FROM loan" in text
    assert "max_loan_amount" in text
    assert "不一致率 1/3" in text  # 率带分母,不是光秃秃的百分比


def test_render_can_hide_the_agreements():
    rows = [
        Row(question="q1", status=AGREE, fast_sql="SELECT 1", compiled_sql="SELECT 1"),
        Row(question="q2", status=DISAGREE, reason="shape",
            fast_sql="SELECT MAX(duration) FROM loan",
            compiled_sql="SELECT MAX(loan.amount) FROM loan"),
    ]
    rep = summarize(rows, corpus="c", datasource="fin", dialect="sqlite", command="cmd")
    assert "q2" in "\n".join(rep.render(only_disagree=True))
    assert "q1" not in "\n".join(rep.render(only_disagree=True))
