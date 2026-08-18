"""Atomic template composition tests (kb.compose).

组合是确定性字符串拼接(仅自家模板,解析不了就跳过)——组合示例是
结构参考,最终 SQL 由 gen_sql 的 check_result 兜底验证。
"""

from trove.services.kb.compose import (
    compose_candidates,
    compose_pair,
    compose_question,
    compose_triple,
    parse_filter,
    parse_group,
    parse_join,
)

JOIN_SQL = (
    "SELECT COUNT(*) FROM loan JOIN account "
    "ON loan.account_id = account.account_id"
)
FILTER_SQL = "SELECT COUNT(*) FROM loan WHERE status = 'A'"
GROUP_SQL = (
    "SELECT account.frequency, COUNT(*) FROM loan JOIN account "
    "ON loan.account_id = account.account_id GROUP BY account.frequency"
)
JOIN_WHERE_SQL = (
    "SELECT COUNT(*) FROM loan JOIN account "
    "ON loan.account_id = account.account_id "
    "WHERE loan.status = 'A'"
)

# 过滤表在 JOIN 之外(account JOIN district + loan 过滤)→ 3+ 表跨链跳过
OUTSIDE_JOIN_SQL = (
    "SELECT COUNT(*) FROM account JOIN district "
    "ON account.district_id = district.district_id"
)


class TestParse:
    def test_parse_join(self):
        assert parse_join(JOIN_SQL) == (
            "loan", "account", "loan.account_id = account.account_id")

    def test_parse_join_rejects_other_shapes(self):
        assert parse_join("SELECT status, COUNT(*) FROM loan GROUP BY status") is None
        assert parse_join("SELECT * FROM loan") is None
        assert parse_join("") is None

    def test_parse_filter(self):
        assert parse_filter(FILTER_SQL) == ("loan", "status", "A")

    def test_parse_filter_rejects_other_shapes(self):
        assert parse_filter("SELECT COUNT(*) FROM loan WHERE status = A") is None
        assert parse_filter("SELECT COUNT(*) FROM loan") is None


class TestComposePair:
    def test_combined_sql_prefixes_where_column(self):
        sql = compose_pair(JOIN_SQL, FILTER_SQL)
        assert sql == (
            "SELECT COUNT(*) FROM loan JOIN account "
            "ON loan.account_id = account.account_id "
            "WHERE loan.status = 'A'"
        )

    def test_filter_on_fact_table_gets_prefix(self):
        """过滤表是 join 的 fact 表 → 同样加表前缀(防歧义)。"""
        sql = compose_pair(JOIN_SQL, "SELECT COUNT(*) FROM loan WHERE status = 'A'")
        assert sql.endswith("WHERE loan.status = 'A'")

    def test_non_equality_filter_rejected(self):
        """只支持 = 值的本家模板;区间/比较过滤模板不组合。"""
        assert parse_filter("SELECT COUNT(*) FROM loan WHERE amount > 0") is None

    def test_filter_table_outside_join_is_skipped(self):
        """3+ 表跨链不在本层组合 → None。"""
        assert compose_pair(OUTSIDE_JOIN_SQL, FILTER_SQL) is None
        assert compose_pair(JOIN_SQL, "SELECT COUNT(*) FROM card WHERE type = 'junior'") is None

    def test_unparseable_input_returns_none(self):
        assert compose_pair("SELECT * FROM t", FILTER_SQL) is None
        assert compose_pair(JOIN_SQL, "SELECT * FROM loan") is None
        assert compose_pair("", "") is None


class TestComposeQuestion:
    def test_english_joins_with_semicolon(self):
        assert compose_question("A?", "B?") == "A?; B?"

    def test_chinese_joins_with_zh_semicolon(self):
        assert compose_question("甲？", "乙？", lang="zh") == "甲？；乙？"


class TestParseGroup:
    def test_parse_join_group(self):
        assert parse_group(GROUP_SQL) == (
            "loan", "account", "frequency",
            "loan.account_id = account.account_id")

    def test_single_table_group_not_parsed(self):
        """单表 GROUP BY 模板不参与三层组合(表集合不同)。"""
        assert parse_group("SELECT status, COUNT(*) FROM loan GROUP BY status") is None


class TestComposeTriple:
    def test_three_layer_composition(self):
        sql = compose_triple(JOIN_WHERE_SQL, GROUP_SQL)
        assert sql == (
            "SELECT account.frequency, COUNT(*) FROM loan JOIN account "
            "ON loan.account_id = account.account_id "
            "WHERE loan.status = 'A' "
            "GROUP BY account.frequency"
        )

    def test_mismatched_table_pair_skipped(self):
        """GROUP 模板表对与组合不同 → 不组合(粒度不匹配)。"""
        other_group = GROUP_SQL.replace("account", "card", 2)
        assert compose_triple(JOIN_WHERE_SQL, other_group) is None

    def test_unparseable_input_returns_none(self):
        assert compose_triple(JOIN_WHERE_SQL, "SELECT * FROM t") is None
        assert compose_triple("SELECT * FROM t", GROUP_SQL) is None


class TestComposeCandidates:
    def _hit(self, sql, score, question="q", tags=None):
        return {"question": question, "sql": sql, "tags": tags or [], "score": score}

    def test_no_join_or_no_filter_returns_unchanged(self):
        only_join = [self._hit(JOIN_SQL, 5)]
        assert compose_candidates(only_join) == only_join
        only_filter = [self._hit(FILTER_SQL, 5)]
        assert compose_candidates(only_filter) == only_filter

    def test_combines_and_scores_as_max(self):
        hits = [
            self._hit(JOIN_SQL, 8, question="How many accounts in district?"),
            self._hit(FILTER_SQL, 6, question="How many loans with status A?"),
        ]
        out = compose_candidates(hits)
        assert len(out) == 3  # 2 原子 + 1 组合
        combo = [h for h in out if "JOIN" in h["sql"] and "WHERE" in h["sql"]][0]
        assert combo["score"] == 6  # int(max(8, 6) × 0.85) 降权:组合不压过最强原子
        assert combo["template"] is True
        assert "account" in combo["question"] and "status" in combo["question"]

    def test_combined_tags_are_union_sorted(self):
        hits = [
            self._hit(JOIN_SQL, 8, tags=["account", "loan", "join"]),
            self._hit(FILTER_SQL, 6, tags=["loan", "status", "filter"]),
        ]
        combo = [
            h for h in compose_candidates(hits)
            if "JOIN" in h["sql"] and "WHERE" in h["sql"]
        ][0]
        assert combo["tags"] == sorted(
            {"account", "loan", "join", "status", "filter"})

    def test_combo_limit_caps_total(self):
        """join×filter 全组合 > 上限时只保留 max_combos 个最高分组合。"""
        filters = [
            self._hit(f"SELECT COUNT(*) FROM loan WHERE status = '{v}'", s)
            for v, s in (("A", 5), ("B", 4), ("C", 3))
        ]
        out = compose_candidates(
            [self._hit(JOIN_SQL, 9)] + filters, max_combos=2)
        combos = [
            h for h in out if "JOIN" in h["sql"] and "WHERE" in h["sql"]
        ]
        assert len(combos) == 2  # 3 个唯一组合被截到 2 个
        assert combos[0]["score"] == 7 and combos[1]["score"] == 7  # int(9×0.85)

    def test_duplicate_combination_deduped(self):
        """同一 join × 同一 filter 重复出现只产出一个组合。"""
        hits = [
            self._hit(JOIN_SQL, 9), self._hit(JOIN_SQL, 3),
            self._hit(FILTER_SQL, 8), self._hit(FILTER_SQL, 2),
        ]
        combos = [
            h for h in compose_candidates(hits)
            if "JOIN" in h["sql"] and "WHERE" in h["sql"]
        ]
        assert len(combos) == 1

    def test_accepts_duck_typed_objects(self):
        """dataclass 样式对象(dict 以外)也能处理——service 层传入 ExampleHit。"""

        class Hit:
            def __init__(self, sql, score):
                self.sql, self.score, self.question, self.tags = sql, score, "q", []

        hits = [Hit(JOIN_SQL, 5), Hit(FILTER_SQL, 4)]
        out = compose_candidates(hits)
        assert len(out) == 3

    def test_three_layer_combo_generated(self):
        """JOIN×WHERE×GROUP:三层组合进候选池,score = 三者最高分。"""
        hits = [
            self._hit(JOIN_SQL, 9, question="How many loans in account?"),
            self._hit(FILTER_SQL, 7, question="How many loans with status A?"),
            self._hit(GROUP_SQL, 5, question="Loans per frequency?"),
        ]
        out = compose_candidates(hits, max_triples=4)
        triples = [
            h for h in out
            if "JOIN" in h["sql"] and "WHERE" in h["sql"] and "GROUP BY" in h["sql"]
        ]
        assert len(triples) == 1
        assert triples[0]["sql"] == (
            "SELECT account.frequency, COUNT(*) FROM loan JOIN account "
            "ON loan.account_id = account.account_id "
            "WHERE loan.status = 'A' "
            "GROUP BY account.frequency"
        )
        assert triples[0]["score"] == 7  # int(max(9,7,5) × 0.85)

    def test_three_layer_requires_all_three_atoms(self):
        """缺 join/filter/group 任一 → 无三层组合。"""
        no_filter = [self._hit(JOIN_SQL, 9), self._hit(GROUP_SQL, 5)]
        out = compose_candidates(no_filter)
        assert len(out) == 2  # 只有两个原子,无任何组合
