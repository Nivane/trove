"""fast_match 纯匹配函数 + 节点 gate 测试(零 LLM、零网络)。"""


from trove.core.config import AgentConfig
from trove.services.kb.service import ExampleHit
from trove.services.semantic_layer.models import SemanticDataset, SemanticModel
from trove.workflow.nodes.fast_match import (
    FAST_PATH_MAX_QUESTION_LEN,
    make_fast_match,
    match_fast_template,
    template_sql_shape_ok,
)
from trove.workflow.state import WorkflowState


def hit(**kw):
    defaults = dict(question="", sql="", tags=[], template=True)
    defaults.update(kw)
    return ExampleHit(**defaults)


BARE = hit(
    question="How many records are in the students table?",
    sql="SELECT COUNT(*) FROM students",
    tags=["students", "count", "aggregation"],
)
ENUM = hit(
    question="How many students records are male?",
    sql="SELECT COUNT(*) FROM students WHERE gender = 'M'",
    tags=["students", "gender", "filter", "aggregation"],
)
MAX_AMOUNT = hit(
    question="What is the maximum loan amount?",
    sql="SELECT MAX(amount) FROM loans",
    tags=["loans", "amount", "aggregation"],
    aggregate=True,
)
DURATION = hit(
    question="What is the average loan duration?",
    sql="SELECT AVG(duration) FROM loans",
    tags=["loans", "duration", "aggregation"],
    aggregate=True,
)
YEAR97 = hit(
    question="How many loans records have approved_date in 1997?",
    sql="SELECT COUNT(*) FROM loans WHERE substr(approved_date, 1, 4) = '1997'",
    tags=["loans", "approved_date", "filter", "aggregation"],
    date_range=True,
)
BETWEEN = hit(
    question="How many loans records have approved_date between 1995 and 1997?",
    sql="SELECT COUNT(*) FROM loans WHERE approved_date BETWEEN '1995-01-01' AND '1997-12-31'",
    tags=["loans", "approved_date", "filter", "aggregation"],
    date_range=True,
)
BEFORE = hit(
    question="How many loans records have approved_date before 1997?",
    sql="SELECT COUNT(*) FROM loans WHERE substr(approved_date, 1, 4) < '1997'",
    tags=["loans", "approved_date", "filter", "aggregation"],
    date_range=True,
)
PLACEHOLDER = hit(
    question="How many loans records have amount greater than 0?",
    sql="SELECT COUNT(*) FROM loans WHERE amount > 0",
    tags=["loans", "amount", "filter", "aggregation"],
)
GROUPBY = hit(
    question="How many students records are there for each county?",
    sql="SELECT county, COUNT(*) FROM students GROUP BY county",
    tags=["students", "group", "aggregation"],
)
JOINED = hit(
    question="How many students records are there in counties?",
    sql="SELECT COUNT(*) FROM students JOIN counties ON students.county_id = counties.county_id",
    tags=["students", "counties", "join", "aggregation"],
)
SUBQUERY = hit(
    question="How many students records are above average?",
    sql="SELECT COUNT(*) FROM students WHERE grade > (SELECT AVG(grade) FROM students)",
    tags=["students", "grade", "filter", "aggregation"],
)


class TestShapeOk:
    def test_bare_count_ok(self):
        ok, table, agg, has_where = template_sql_shape_ok(BARE.sql)
        assert ok and table == "students" and agg == "COUNT" and not has_where

    def test_max_ok(self):
        ok, table, agg, has_where = template_sql_shape_ok(MAX_AMOUNT.sql)
        assert ok and table == "loans" and agg == "MAX"

    def test_group_by_rejected(self):
        assert template_sql_shape_ok(GROUPBY.sql)[0] is False

    def test_join_rejected(self):
        assert template_sql_shape_ok(JOINED.sql)[0] is False

    def test_subquery_rejected(self):
        assert template_sql_shape_ok(SUBQUERY.sql)[0] is False

    def test_placeholder_compare_rejected(self):
        """`WHERE col > 0` 的 0 是结构占位 → 形状拒。"""
        assert template_sql_shape_ok(PLACEHOLDER.sql)[0] is False

    def test_limit_rejected(self):
        assert template_sql_shape_ok("SELECT COUNT(*) FROM students LIMIT 5")[0] is False

    def test_two_aggregates_rejected(self):
        sql = "SELECT COUNT(*), AVG(grade) FROM students"
        assert template_sql_shape_ok(sql)[0] is False

    def test_parse_error_rejected(self):
        assert template_sql_shape_ok("SELEC * FROM")[0] is False


class TestBareCount:
    def test_hit(self):
        m = match_fast_template("How many students are there?", [BARE], ["students"])
        assert m and m["sql"] == "SELECT COUNT(*) FROM students"

    def test_plural_table_matches(self):
        assert match_fast_template("how many student are there?", [BARE], ["students"])

    def test_leftover_token_rejects(self):
        assert match_fast_template("How many students are male?", [BARE], ["students"]) is None

    def test_year_rejects_bare(self):
        assert match_fast_template("How many students are there in 1997?", [BARE], ["students"]) is None

    def test_table_not_matched(self):
        """matched 单表且不含模板表 → miss(问题提及通道仅限 FK 邻居多表场景)。"""
        assert match_fast_template("How many students are there?", [BARE], ["teachers"]) is None

    def test_mention_matters_only_with_multiple_matched(self):
        """多表(邻居)场景:问题明确提及模板表名 → 放行。"""
        m = match_fast_template("How many students are there?", [BARE], ["teachers", "students"])
        assert m and m["sql"] == BARE.sql

    def test_fk_neighbor_matched(self):
        """schema_linking 的 FK 邻居扩展:模板表在 matched 里即可命中。"""
        m = match_fast_template("How many students are there?", [BARE], ["students", "counties"])
        assert m and m["sql"] == "SELECT COUNT(*) FROM students"

    def test_overlong_question_misses(self):
        q = "how many students are there? " * 30
        assert match_fast_template(q, [BARE], ["students"]) is None
        assert len(q) > FAST_PATH_MAX_QUESTION_LEN

    def test_empty_matched_misses(self):
        assert match_fast_template("How many students are there?", [BARE], []) is None


class TestEnumFilter:
    def test_hit_with_label(self):
        m = match_fast_template("How many students are male?", [BARE, ENUM], ["students"])
        assert m and m["sql"] == "SELECT COUNT(*) FROM students WHERE gender = 'M'"

    def test_missing_label_misses(self):
        assert match_fast_template("How many students are there?", [BARE, ENUM], ["students"])["sql"] == BARE.sql

    def test_label_alone_not_enough(self):
        """枚举过滤需要 label 强制出现;只提表名命中裸 COUNT 而非枚举。"""
        assert match_fast_template("How many students are there?", [ENUM], ["students"]) is None


class TestResidualConditionGuard:
    """模板 WHERE 只固定它自己那一个条件 —— 问题多出来的条件词必须 miss。

    2026-10-02 P1 评测实证:32 题里快径命中 4 次,**4 次全是部分命中**
    (0469/0471 枚举模板丢地区 → F2-c 拦下后回滚重放耗尽;0476 丢出生年 +
    城市 → 无规则可拦,错 SQL 直接交付,基线 MATCH → MISMATCH;0493 年份
    SUM 模板抢答增长率问题)。单向子集检查只保证「模板说的在问题里」,
    反向的残条件必须另查(``_residual_covered``)。
    """

    def test_enum_extra_region_rejects(self):
        assert match_fast_template(
            "How many students are male and staying in East Bohemia?",
            [ENUM], ["students"],
        ) is None

    def test_enum_extra_year_rejects(self):
        assert match_fast_template(
            "How many students are male who were born before 1950?",
            [ENUM], ["students"],
        ) is None

    def test_enum_plain_still_hits(self):
        m = match_fast_template("How many students are male?", [ENUM], ["students"])
        assert m and m["sql"] == ENUM.sql

    def test_aggregate_with_where_extra_condition_rejects(self):
        """带 WHERE 的年份 SUM 模板不得抢答增长率问题(0493 形状)。"""
        t = hit(
            question="What is the total amount of loans granted in 1997?",
            sql="SELECT SUM(amount) FROM loans WHERE year(date) = '1997'",
            tags=["loans", "date-range filter", "aggregation"],
            aggregate=True,
        )
        assert match_fast_template(
            "What was the growth rate of the total amount of loans between 1996 and 1997?",
            [t], ["loans"],
        ) is None

    def test_aggregate_with_where_faithful_still_hits(self):
        t = hit(
            question="What is the total amount of loans granted in 1997?",
            sql="SELECT SUM(amount) FROM loans WHERE year(date) = '1997'",
            tags=["loans", "date-range filter", "aggregation"],
            aggregate=True,
        )
        m = match_fast_template("What is the total loan amount granted in 1997?", [t], ["loans"])
        assert m and m["sql"] == t.sql


class TestAggregate:
    def test_max_hit(self):
        m = match_fast_template("What is the maximum amount?", [MAX_AMOUNT], ["loans"])
        assert m and m["sql"] == "SELECT MAX(amount) FROM loans"

    def test_desc_token_rejects(self):
        assert match_fast_template("What is the maximum loan duration?", [MAX_AMOUNT], ["loans"]) is None

    def test_duration_hit(self):
        m = match_fast_template("What is the average loan duration?", [DURATION], ["loans"])
        assert m and m["sql"] == "SELECT AVG(duration) FROM loans"

    def test_agg_word_mismatch(self):
        assert match_fast_template("What is the maximum loan duration?", [DURATION], ["loans"]) is None

    def test_inflection_prefix_match(self):
        """approval/approved 词形变化 → 前缀回退。"""
        t = hit(
            question="What is the average loan approval amount?",
            sql="SELECT AVG(amount) FROM loans",
            tags=["loans", "amount", "aggregation"],
            aggregate=True,
        )
        assert match_fast_template("What is the average approved amount?", [t], ["loans"])

    def test_year_condition_rejects_whereless_aggregate(self):
        """问题带年份/日期条件但模板无 WHERE → 拒绝快径(防丢过滤)。

        "1996 年发放的贷款平均金额" 命中无 WHERE 的 AVG(amount) 模板会
        丢掉 1996 过滤,返回全量均值——必须 miss,交正常链路带出条件。
        """
        t = hit(
            question="What is the average loan amount?",
            sql="SELECT AVG(amount) FROM loans",
            tags=["loans", "amount", "aggregation"],
            aggregate=True,
        )
        assert match_fast_template(
            "What is the average loan amount for loans issued in 1996?",
            [t], ["loans"],
        ) is None

    def test_date_condition_rejects_whereless_aggregate(self):
        t = hit(
            question="What is the average loan amount?",
            sql="SELECT AVG(amount) FROM loans",
            tags=["loans", "amount", "aggregation"],
            aggregate=True,
        )
        assert match_fast_template(
            "What is the average loan amount approved on 1996-01-15?",
            [t], ["loans"],
        ) is None

    def test_enum_condition_rejects_whereless_aggregate(self):
        """无 WHERE 模板不能吃带属性/枚举条件的聚合问题(如 card type 过滤)。"""
        t = hit(
            question="What is the average loan amount?",
            sql="SELECT AVG(amount) FROM loans",
            tags=["loans", "amount", "aggregation"],
            aggregate=True,
        )
        assert match_fast_template(
            "What is the average loan amount for gold cards?",
            [t], ["loans"],
        ) is None

    def test_region_condition_rejects_whereless_aggregate(self):
        """按区域分组/过滤的聚合问题不得被无 WHERE 模板抢答。"""
        t = hit(
            question="What is the average loan amount?",
            sql="SELECT AVG(amount) FROM loans",
            tags=["loans", "amount", "aggregation"],
            aggregate=True,
        )
        assert match_fast_template(
            "What is the average loan amount by region?",
            [t], ["loans"],
        ) is None

    def test_plain_aggregate_still_hits(self):
        """无日期条件时,无 WHERE 的聚合模板照常命中(回归保护)。"""
        m = match_fast_template("What is the maximum amount?", [MAX_AMOUNT], ["loans"])
        assert m and m["sql"] == "SELECT MAX(amount) FROM loans"


class TestDateRange:
    def test_year_hit(self):
        m = match_fast_template("How many loans were approved in 1997?", [YEAR97], ["loans"])
        assert m and m["sql"] == YEAR97.sql

    def test_year_mismatch(self):
        assert match_fast_template("How many loans were approved in 1996?", [YEAR97], ["loans"]) is None

    def test_in_template_rejects_interval_question(self):
        """用户带区间词时,纯 in 模板不得抢答。"""
        assert match_fast_template(
            "How many loans were approved before 1997?", [YEAR97, BEFORE], ["loans"],
        )["sql"] == BEFORE.sql

    def test_between_needs_both_years(self):
        assert match_fast_template(
            "How many loans were approved between 1996 and 1997?", [YEAR97, BETWEEN], ["loans"],
        ) is None

    def test_between_both_years_hit(self):
        m = match_fast_template(
            "How many loans were approved between 1995 and 1997?", [YEAR97, BETWEEN], ["loans"],
        )
        assert m and m["sql"] == BETWEEN.sql

    def test_no_desc_word_misses(self):
        """列描述词缺失(仅年份)→ miss(保守)。"""
        assert match_fast_template("How many loans in 1997?", [YEAR97], ["loans"]) is None

    def test_before_year_mismatch(self):
        assert match_fast_template("How many loans were approved before 1996?", [BEFORE], ["loans"]) is None


class TestGroupOrderGuard:
    """分组/排序意图守卫:带 per/each/按…分/ordered by 等问题绝不能被
    单表全局聚合模板抢答(丢分组/排序语义)→ 一律 miss 交回正常链路。"""

    def test_total_per_region_not_hijacked_by_plain_sum(self):
        """「total amount per region」不得命中 SELECT SUM(amount) 全局模板。"""
        m = match_fast_template(
            "Show the total amount of loans issued per region, ordered by total descending",
            [MAX_AMOUNT, BARE], ["loans", "students"],
        )
        assert m is None

    def test_per_grouping_misses(self):
        m = match_fast_template(
            "What is the total amount of loans per district?", [MAX_AMOUNT], ["loans"],
        )
        assert m is None

    def test_each_grouping_misses(self):
        m = match_fast_template(
            "How many records are there in each county?", [BARE], ["students"],
        )
        assert m is None

    def test_ordered_by_misses(self):
        m = match_fast_template(
            "What is the total amount ordered by date?", [MAX_AMOUNT], ["loans"],
        )
        assert m is None

    def test_desc_misses(self):
        m = match_fast_template(
            "Show the total amount descending?", [MAX_AMOUNT], ["loans"],
        )
        assert m is None

    def test_plain_aggregate_still_hits(self):
        """无分组/排序信号的平凡聚合题照常快径。"""
        m = match_fast_template("What is the maximum amount?", [MAX_AMOUNT], ["loans"])
        assert m and m["sql"] == "SELECT MAX(amount) FROM loans"

    def test_bare_count_still_hits(self):
        m = match_fast_template("How many students are there?", [BARE], ["students"])
        assert m and m["sql"] == "SELECT COUNT(*) FROM students"

    def test_zh_grouping_misses(self):
        zh_hit = hit(
            question="学生表中各地区的平均工资是多少？",
            sql="SELECT AVG(grade) FROM students",
            tags=["学生", "grade", "聚合"],
            aggregate=True,
        )
        assert match_fast_template("学生表中各地区的平均工资", [zh_hit], ["students"]) is None

    def test_zh_plain_aggregate_still_hits(self):
        zh_hit = hit(
            question="成绩的平均值是多少？",
            sql="SELECT AVG(grade) FROM students",
            tags=["学生", "grade", "聚合"],
            aggregate=True,
        )
        m = match_fast_template("学生的平均成绩是多少？", [zh_hit], ["students"])
        assert m and m["sql"] == "SELECT AVG(grade) FROM students"


class TestZh:
    BARE_ZH = hit(
        question="学生表中有多少条记录？",
        sql="SELECT COUNT(*) FROM students",
        tags=["学生", "行数", "聚合"],
    )
    AVG_ZH = hit(
        question="成绩的平均值是多少？",
        sql="SELECT AVG(grade) FROM students",
        tags=["学生", "grade", "聚合"],
        aggregate=True,
    )

    def test_bare_count_zh(self):
        m = match_fast_template("学生表里有多少学生？", [self.BARE_ZH], ["students"])
        assert m and m["sql"] == "SELECT COUNT(*) FROM students"

    def test_agg_zh(self):
        m = match_fast_template("学生的平均成绩是多少？", [self.AVG_ZH], ["students"])
        assert m and m["sql"] == "SELECT AVG(grade) FROM students"

    def test_agg_zh_label_missing(self):
        assert match_fast_template("平均成绩是多少？", [self.AVG_ZH], ["students"]) is None

    def test_agg_zh_desc_nomatch_does_not_crash(self):
        """回归:模板问句不带"X的平均值"措辞(_ZH_AGG_DESC_RE 不命中)时
        不得崩溃 'NoneType' has no attribute 'group'——应干净地 miss。"""
        plain = hit(
            question="各地区的平均工资",          # 无「值」字后缀,desc 正则不命中
            sql="SELECT A3, AVG(A11) FROM district GROUP BY A3",
            tags=["district", "A11", "聚合"],
            aggregate=True,
        )
        assert match_fast_template("各地区的平均工资", [plain], ["district"]) is None


# ── 节点 gate(修正轮 / 意图 / 配置 kill-switch) ─────────


class FakeKB:
    def __init__(self, hits):
        self._hits = hits

    async def ensure_synced(self, **kwargs):
        pass

    async def list_templates(self, datasource):
        return self._hits


class FakeConnectors:
    default_name = "test_db"

    async def get(self):
        class Adapter:
            def dialect(self):
                return "sqlite"
        return Adapter()


def node_state(**kw):
    defaults = dict(
        session_id="s1",
        question="How many students are there?",
        intent="query",
    )
    defaults.update(kw)
    return WorkflowState(**defaults)


class FakeSemantic:
    """SemanticLayerProvider 的最小替身:只暴露 .model()。

    注意是**方法**不是 property —— 与真实 provider(``provider.py:272``)一致,
    全仓按 ``semantic_layer.model()`` 调用。
    """

    def __init__(self, model=None, raises=None):
        self._model = model
        self._raises = raises

    def model(self):
        if self._raises is not None:
            raise self._raises
        return self._model


def sem_model(*datasets):
    return SemanticModel(name="m", datasets=list(datasets))


async def run_node(state, kb=None, connectors=None, config=None, semantic=None):
    node = make_fast_match(
        kb=kb or FakeKB([BARE]),
        connectors=connectors or FakeConnectors(),
        config=config,
        semantic=semantic,
    )
    return await node(state)


class TestNodeGates:
    async def test_hit_writes_fast_path(self):
        out = await run_node(node_state(matched_tables=["students"]))
        assert out["fast_path"] is True
        assert out["sql"] == "SELECT COUNT(*) FROM students"
        assert out["complexity"] == "simple"
        assert out["kb_hits"][0]["kind"] == "template"
        assert out["dialect"] == "sqlite"

    async def test_correction_round_never_fast_paths(self):
        state = node_state(
            matched_tables=["students"],
            error_feedback="Validation rule: count-shape",
        )
        assert await run_node(state) == {}

    async def test_correction_round_clears_stale_hit(self):
        """回归(2026-10-02 P1 评测 Q1/Q3):回滚到 schema_linking 后重入本节点。

        修正轮守卫故意 miss,但 ``fast_path``/``sql`` 还是**上一轮命中**写下的
        —— 只返回 ``{}`` 不更新,路由器(``fast_path and sql and not error``)
        就按「本轮命中」把刚被 F2-c 拦下的旧 SQL 原样重放,连败后无档可升,
        题降级成 EXECUTION_ERROR(基线同题 MATCH)。miss 必须清掉命中标记;
        ``sql`` 不清(修复链要拿失败的 SQL 当底稿)。
        """
        state = node_state(
            matched_tables=["students"],
            error_feedback="Validation rule: count-shape",
            fast_path=True,
            sql="SELECT COUNT(*) FROM students WHERE gender = 'M'",
        )
        out = await run_node(state)
        assert out.get("fast_path") is False
        assert "sql" not in out

    async def test_router_sends_stale_hit_to_normal_path(self):
        """路由器契约:标记清掉后必须回正常链路,绝不重放旧 SQL。"""
        from trove.workflow.graphs import _make_route_after_fast_match

        state = node_state(
            matched_tables=["students"],
            error_feedback="Validation rule: count-shape",
            fast_path=True,
            sql="SELECT COUNT(*) FROM students WHERE gender = 'M'",
        )
        route = _make_route_after_fast_match("query_sketch")
        assert route(state) == "execute_sql"  # 修复前的坏行为:旧 SQL 直执行
        merged = state.model_copy(update=await run_node(state))
        assert route(merged) == "query_sketch"

    async def test_other_miss_gates_clear_stale_hit_too(self):
        """任何 miss 分支都得清:KB 故障轮次不该留着上一轮的命中标记。"""

        class Boom:
            async def ensure_synced(self, **kwargs):
                raise RuntimeError("db gone")

        state = node_state(
            matched_tables=["students"], fast_path=True, sql="SELECT 1",
        )
        assert (await run_node(state, kb=Boom())) == {"fast_path": False}

    async def test_error_analysis_blocks(self):
        state = node_state(matched_tables=["students"], error_analysis="TARGET: gen_sql")
        assert await run_node(state) == {}

    async def test_non_query_intent_blocks(self):
        state = node_state(matched_tables=["students"], intent="metadata")
        assert await run_node(state) == {}

    async def test_config_off_blocks(self):
        cfg = AgentConfig(fast_path=False)
        state = node_state(matched_tables=["students"])
        assert await run_node(state, config=cfg) == {}

    async def test_no_templates_misses(self):
        assert await run_node(node_state(matched_tables=["students"]), kb=FakeKB([])) == {}

    async def test_kb_failure_is_silent_miss(self):
        class Boom:
            async def ensure_synced(self, **kwargs):
                raise RuntimeError("db gone")

        assert await run_node(node_state(matched_tables=["students"]), kb=Boom()) == {}

    async def test_error_state_passes_through(self):
        state = node_state(error="boom")
        assert await run_node(state) == {}

    async def test_miss_writes_nothing(self):
        out = await run_node(node_state(matched_tables=["students"], question="who is John"))
        assert out == {}


# ── RLS:快径不过编译器,声明层行级安全必须在此补注入 ─────────


class TestFastPathRLS:
    """回归:``row_filter`` 曾在快径上失效 —— 模板命中直接产出 SQL,不过编译
    器,于是声明了行级安全的数据集被统计**全表**。(BARE 模板 = 全域
    ``SELECT COUNT(*) FROM students``;声明 RLS 后必须带谓词。)"""

    async def test_row_filter_injected_on_fast_path(self):
        sem = FakeSemantic(sem_model(
            SemanticDataset(name="students", row_filter="county = 'A'"),
        ))
        out = await run_node(node_state(matched_tables=["students"]), semantic=sem)
        assert out["fast_path"] is True
        assert "county = 'A'" in out["sql"]
        # kb_hits 里回显的 SQL 必须与执行用的一致,否则前端展示与实跑不符
        assert out["kb_hits"][0]["sql"] == out["sql"]

    async def test_no_rls_model_leaves_sql_untouched(self):
        sem = FakeSemantic(sem_model(SemanticDataset(name="students")))
        out = await run_node(node_state(matched_tables=["students"]), semantic=sem)
        assert out["sql"] == "SELECT COUNT(*) FROM students"

    async def test_no_semantic_provider_keeps_legacy_behaviour(self):
        out = await run_node(node_state(matched_tables=["students"]), semantic=None)
        assert out["sql"] == "SELECT COUNT(*) FROM students"

    async def test_injection_failure_falls_back_to_slow_path(self):
        """无法确认 RLS 生效时放弃快径 —— 快径是优化,授权不是。"""
        sem = FakeSemantic(raises=RuntimeError("provider down"))
        assert await run_node(node_state(matched_tables=["students"]), semantic=sem) == {}

    async def test_unparseable_template_sql_falls_back(self):
        """坏模板 SQL:注入器拒绝 → miss → 交回编译路径(那里正常注入)。"""
        bad = hit(
            question="How many records are in the students table?",
            sql="SELECT COUNT(*) FROM students WHERE ((",
            tags=["students", "count", "aggregation"],
        )
        sem = FakeSemantic(sem_model(
            SemanticDataset(name="students", row_filter="county = 'A'"),
        ))
        out = await run_node(
            node_state(matched_tables=["students"]), kb=FakeKB([bad]), semantic=sem,
        )
        assert out == {}
