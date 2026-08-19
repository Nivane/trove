"""fast_match 纯匹配函数 + 节点 gate 测试(零 LLM、零网络)。"""

import pytest

from trove.core.config import AgentConfig
from trove.services.kb.service import ExampleHit
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


async def run_node(state, kb=None, connectors=None, config=None):
    node = make_fast_match(
        kb=kb or FakeKB([BARE]),
        connectors=connectors or FakeConnectors(),
        config=config,
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
