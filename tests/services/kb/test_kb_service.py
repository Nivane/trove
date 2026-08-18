"""KbService tests — per-datasource YAML source, SQLite mirror, lazy sync.

Layout: .trove/kb/<datasource>/{schema_notes,semantics,examples}.yml
Legacy flat files (kb root) are auto-migrated into a datasource dir.
"""

import re

import aiosqlite
import pytest

from trove.core.types import SchemaInfo, TableInfo, ColumnInfo
from trove.services.kb.service import (
    KbService,
    _bigrams,
    _score_example,
)

SCHEMA_NOTES = """
tables:
  - name: district
    description: 行政区划表，每地区一条记录
    columns:
      - name: A2
        description: 地区名称
      - name: A11
        description: ""
    metrics:
      - name: avg_loan_by_district
        definition: 按地区分组的平均贷款金额
"""

SEMANTICS = """
terms:
  - term: 平均贷款金额
    aliases: [平均贷款, 贷款均值]
    mapping: AVG(loan.amount)
    tables: [loan, account, district]
    definition: 按问题所需维度分组的贷款金额均值
  - term: 客户数量
    aliases: []
    mapping: COUNT(client.client_id)
    tables: [client]
    definition: 客户数
"""

EXAMPLES = """
examples:
  - question: 哪个地区的平均贷款金额最高？
    sql: SELECT d.A2, AVG(l.amount) AS avg_loan FROM loan l
    tags: [贷款, 地区, 排名]
  - question: 各地区贷款总额是多少
    template: true
    sql: SELECT d.A2, SUM(l.amount) FROM loan l
    tags: [贷款, 地区, 汇总]
  - question: 客户年龄分布
    sql: SELECT age, COUNT(*) FROM client GROUP BY age
    tags: [客户, 年龄]
"""

OTHER_SEMANTICS = """
terms:
  - term: 页面浏览量
    aliases: []
    mapping: COUNT(events.page_view)
    tables: [events]
    definition: 浏览事件数
"""


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


@pytest.fixture
def kb_dir(kb):
    kb.kb_dir.mkdir(parents=True)
    return kb.kb_dir


def write_kb(kb_dir, ds="demo"):
    d = kb_dir / ds
    d.mkdir(parents=True, exist_ok=True)
    (d / "schema_notes.yml").write_text(SCHEMA_NOTES, encoding="utf-8")
    (d / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    (d / "examples.yml").write_text(EXAMPLES, encoding="utf-8")


class TestTableAnchoredRetrieval:
    """Evidence layer: runtime deterministic filtering by matched tables."""

    ALL_TABLES = ["loan", "account", "district", "client"]

    async def test_terms_filtered_by_matched_tables(self, kb, kb_dir):
        """问题命中两个术语；只匹配 loan 时，绑定 district/client 的术语被过滤。"""
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        hits = await kb.search_terms(
            "平均贷款金额 客户数量", "demo",
            tables=["loan", "account"], all_tables=self.ALL_TABLES,
        )
        assert [h.term for h in hits] == ["平均贷款金额"]

    async def test_table_agnostic_terms_survive(self, kb, kb_dir):
        """不绑定任何表的术语（无 tables 字段）不过滤。"""
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        # 手动补一个无表绑定的术语
        import yaml as _yaml
        sem = kb_dir / "demo" / "semantics.yml"
        doc = _yaml.safe_load(sem.read_text(encoding="utf-8"))
        doc["terms"].append({"term": "金卡", "mapping": "card_type='gold'", "definition": "黄金卡"})
        sem.write_text(_yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        await kb.ensure_synced("demo")

        hits = await kb.search_terms(
            "金卡 客户数量", "demo",
            tables=["loan"], all_tables=self.ALL_TABLES,
        )
        terms = [h.term for h in hits]
        assert "金卡" in terms          # 无表绑定 → 保留
        assert "客户数量" not in terms  # 绑定 client（未匹配）→ 过滤

    async def test_examples_filtered_and_boosted_by_tables(self, kb, kb_dir):
        """提到未匹配表（client）的示例被过滤；提到匹配表（loan）的保留并加分。"""
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        hits = await kb.search_examples(
            "地区 贷款", "demo",
            tables=["loan"], all_tables=self.ALL_TABLES,
        )
        # client 示例被过滤
        assert all("client" not in (h.question + h.sql).lower() for h in hits)
        # loan 示例保留
        assert any("loan" in h.sql.lower() for h in hits)
        # 表锚是加分项：loan 示例得分更高
        loan_hit = next(h for h in hits if "loan" in h.sql.lower())
        assert loan_hit.score >= 3

    async def test_per_table_keeps_one_representative_per_anchor(self, kb, kb_dir):
        """per_table 分组:每张锚定表各保底一个代表模板,高分单表不霸榜。"""
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        # 补一张 district 模板,把 loan 模板做得分普遍更高
        import yaml as _yaml

        examples = kb_dir / "demo" / "examples.yml"
        doc = _yaml.safe_load(examples.read_text(encoding="utf-8"))
        doc["examples"].append({
            "question": "每个地区的客户有多少",
            "template": True,
            "sql": "SELECT d.A2, COUNT(*) FROM client c JOIN district d ON c.district_id = d.A1 GROUP BY d.A2",
            "tags": ["地区", "客户", "分组"],
        })
        examples.write_text(_yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        await kb.ensure_synced("demo")

        # 分组:limit=2 两张表各保底 1 个(不加分组时可能全被 loan 模板占据)
        grouped = await kb.search_examples(
            "客户 地区 贷款", "demo", limit=2,
            tables=["loan", "district"], all_tables=self.ALL_TABLES,
            per_table=True,
        )
        grouped_tables = set()
        for h in grouped:
            grouped_tables |= {"loan", "district"} & set(
                {w for w in re.findall(r"[a-z]+", h.sql.lower())})
        assert grouped_tables == {"loan", "district"}, grouped_tables
        # 单表场景退化为普通行为(分组无副作用)
        single = await kb.search_examples(
            "贷款", "demo", limit=2, tables=["loan"],
            all_tables=self.ALL_TABLES, per_table=True,
        )
        assert single == await kb.search_examples(
            "贷款", "demo", limit=2, tables=["loan"],
            all_tables=self.ALL_TABLES,
        )

    async def test_retrieval_composes_join_and_filter_templates(self, kb, kb_dir):
        """检索时组合膨胀:命中 JOIN 模板 + WHERE 模板 → 组合示例进 top-k。

        组合 SQL = join 骨架 + 过滤列加表前缀;评分 = 两个原子较高分。
        现有中文/别名模板不匹配组合正则,不受影响。
        """
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        import yaml as _yaml

        examples = kb_dir / "demo" / "examples.yml"
        doc = _yaml.safe_load(examples.read_text(encoding="utf-8"))
        doc["examples"].extend([
            {
                "question": "How many loan records are there in account?",
                "template": True,
                "sql": ("SELECT COUNT(*) FROM loan JOIN account "
                        "ON loan.account_id = account.account_id"),
                "tags": ["loan", "account", "join", "aggregation"],
            },
            {
                "question": "How many loan records have status A?",
                "template": True,
                "sql": "SELECT COUNT(*) FROM loan WHERE status = 'A'",
                "tags": ["loan", "status", "filter", "aggregation"],
            },
        ])
        examples.write_text(_yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
        await kb.ensure_synced("demo")

        hits = await kb.search_examples(
            "How many loan records have status A in account?",
            "demo", limit=3,
            tables=["loan", "account"], all_tables=self.ALL_TABLES,
        )
        combined = [h for h in hits if "JOIN" in h.sql and "WHERE" in h.sql]
        assert combined, [h.sql for h in hits]
        assert combined[0].sql == (
            "SELECT COUNT(*) FROM loan JOIN account "
            "ON loan.account_id = account.account_id "
            "WHERE loan.status = 'A'"
        )
        assert combined[0].template is True
        # 组合评分 = 两个原子的较高分 → 排序在原子之前
        assert combined[0].score >= max(h.score for h in hits[:3])


class TestEnablement:
    async def test_disabled_when_kb_dir_missing(self, kb):
        assert kb.enabled is False
        assert await kb.search_terms("平均贷款金额", "demo") == []
        assert await kb.search_examples("平均贷款金额", "demo") == []

    def test_kb_dir_override(self, tmp_path):
        """kb_dir 参数覆盖默认的 <project_root>/.trove/kb 布局(供 eval --kb-dir 使用)。"""
        custom = tmp_path / "custom_kb"
        service = KbService(tmp_path / "proj", kb_dir=custom)
        assert service.kb_dir == custom


class TestSync:
    async def test_sync_imports_all_files(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        counts = await kb.list_items()
        assert counts == {"demo": {"table": 1, "term": 2, "example": 2, "template": 1}}

    async def test_mtime_reloads_only_changed_file(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        examples = kb_dir / "demo" / "examples.yml"
        examples.write_text(
            EXAMPLES.replace(
                "  - question: 客户年龄分布",
                "  - question: 新问题\n    sql: SELECT 1\n    tags: [新]\n  - question: 客户年龄分布",
            ),
            encoding="utf-8",
        )
        await kb.ensure_synced("demo")

        hits = await kb.search_examples("新问题", "demo")
        assert any(h.question == "新问题" for h in hits)
        terms = await kb.search_terms("平均贷款金额", "demo")
        assert len(terms) == 1

    async def test_broken_yaml_keeps_old_mirror(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        (kb_dir / "demo" / "examples.yml").write_text("not: [valid: yaml", encoding="utf-8")
        await kb.ensure_synced("demo")

        hits = await kb.search_examples("贷款", "demo")
        assert len(hits) > 0

    async def test_datasources_are_isolated(self, kb, kb_dir):
        """一个数据源的知识对另一个不可见。"""
        write_kb(kb_dir, ds="demo")
        other = kb_dir / "other"
        other.mkdir()
        (other / "semantics.yml").write_text(OTHER_SEMANTICS, encoding="utf-8")
        await kb.ensure_synced("demo")

        assert [h.term for h in await kb.search_terms("页面浏览量", "demo")] == []
        assert [h.term for h in await kb.search_terms("页面浏览量", "other")] == ["页面浏览量"]
        assert await kb.search_terms("平均贷款金额", "other") == []


class TestLegacyMigration:
    def _legacy_flat(self, kb_dir):
        (kb_dir / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
        (kb_dir / "examples.yml").write_text(EXAMPLES, encoding="utf-8")

    async def test_legacy_files_migrated_to_default_datasource(self, kb, kb_dir):
        self._legacy_flat(kb_dir)
        await kb.ensure_synced(default_datasource="demo")

        # files moved into demo/
        assert not (kb_dir / "semantics.yml").exists()
        assert (kb_dir / "demo" / "semantics.yml").exists()
        hits = await kb.search_terms("平均贷款金额", "demo")
        assert [h.term for h in hits] == ["平均贷款金额"]

    async def test_legacy_files_without_default_go_to_legacy(self, kb, kb_dir):
        self._legacy_flat(kb_dir)
        await kb.ensure_synced()

        assert (kb_dir / "legacy" / "semantics.yml").exists()
        hits = await kb.search_terms("平均贷款金额", "legacy")
        assert [h.term for h in hits] == ["平均贷款金额"]

    async def test_migration_skips_name_collisions(self, kb, kb_dir):
        (kb_dir / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
        write_kb(kb_dir, ds="demo")  # demo/semantics.yml already exists
        await kb.ensure_synced(default_datasource="demo")

        # colliding file left in place, existing demo file untouched
        assert (kb_dir / "semantics.yml").exists()
        terms = await kb.search_terms("客户数量", "demo")
        assert len(terms) == 1


class TestMirrorSchema:
    async def test_mirror_rebuilt_without_datasource_column(self, kb, kb_dir):
        """旧版镜像（无 datasource 列）→ 检测到后重建。"""
        write_kb(kb_dir)
        async with aiosqlite.connect(kb.db_path) as db:
            await db.execute(
                "CREATE TABLE kb_items (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
                "item_key TEXT NOT NULL, payload TEXT NOT NULL, source_file TEXT NOT NULL)"
            )
            await db.execute(
                "CREATE TABLE kb_sync (file_path TEXT PRIMARY KEY, mtime REAL NOT NULL)"
            )
            await db.commit()

        await kb.ensure_synced("demo")
        counts = await kb.list_items()
        assert counts["demo"]["term"] == 2


class TestSearchTerms:
    async def test_chinese_substring_match(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        hits = await kb.search_terms("哪个地区的平均贷款金额最高？", "demo")
        assert len(hits) == 1
        assert hits[0].term == "平均贷款金额"
        assert hits[0].mapping == "AVG(loan.amount)"
        assert "district" in hits[0].tables

    async def test_alias_match(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        hits = await kb.search_terms("贷款均值怎么算", "demo")
        assert [h.term for h in hits] == ["平均贷款金额"]

    async def test_no_match_returns_empty(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        assert await kb.search_terms("完全不相关的查询", "demo") == []


class TestTableNotes:
    async def test_stats_and_row_count_parsed(self, kb, kb_dir):
        """profiling 写入的 stats/row_count 经解析进入 table_notes。"""
        (kb_dir / "schema_notes.yml").write_text(
            """
tables:
  - name: district
    description: 行政区划表
    row_count: 77
    columns:
      - name: A2
        description: 地区名称
        stats:
          null_ratio: 0.0
          distinct: 77
          shape: capital
      - name: A11
        description: ""
        stats:
          null_ratio: 0.93
          distinct: 3
""",
            encoding="utf-8",
        )
        await kb.force_sync("default")
        notes = await kb.table_notes(["district"], "default")
        table = notes["district"]
        assert table.row_count == 77
        assert table.stats["A2"] == {"null_ratio": 0.0, "distinct": 77, "shape": "capital"}
        assert table.stats["A11"]["null_ratio"] == 0.93
        # 空描述列不进 columns,但 stats 照常解析(描述盲区与统计可分离)
        assert "A11" not in table.columns

    async def test_notes_retrieved_for_tables(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        notes = await kb.table_notes(["district"], "demo")
        assert "district" in notes
        assert notes["district"].description == "行政区划表，每地区一条记录"
        assert notes["district"].columns["A2"] == "地区名称"
        assert notes["district"].metrics["avg_loan_by_district"] == "按地区分组的平均贷款金额"

    async def test_unknown_table_absent_and_empty_desc_skipped(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        notes = await kb.table_notes(["district", "nonexistent"], "demo")
        assert "nonexistent" not in notes
        assert "A11" not in notes["district"].columns  # empty description skipped


class TestSearchExamples:
    async def test_scoring_orders_by_relevance(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        hits = await kb.search_examples("哪个地区的平均贷款金额最高？", "demo", limit=3)
        assert hits[0].question == "哪个地区的平均贷款金额最高？"  # self-match wins
        assert hits[0].score > hits[-1].score
        assert all(h.score > 0 for h in hits)

    async def test_limit_caps_results(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        hits = await kb.search_examples("贷款", "demo", limit=1)
        assert len(hits) == 1

    async def test_zero_score_excluded(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        assert await kb.search_examples("zzz 无关问题", "demo") == []


class TestPureHelpers:
    def test_bigrams(self):
        assert _bigrams("abcd") == {"ab", "bc", "cd"}
        assert _bigrams("a") == set()

    def test_score_example_components(self):
        question = "贷款均值是多少"
        matched_terms = {"平均贷款金额"}
        example = {
            "question": "哪个地区的平均贷款金额最高？",
            "tags": ["贷款", "地区"],
        }
        score = _score_example(question, matched_terms, example)
        # term×2 (平均贷款金额 in example) + tag×1 (贷款 in question)
        # + bigram×1 (贷款 shared by both questions)
        assert score == 2 * 1 + 1 + 1

    def test_score_example_english_word_overlap(self):
        """英文问题用词级重叠评分（char-bigram 对英文无效）。"""
        question = "how many accounts have running contracts"
        example = {
            "question": "how many accounts are running",
            "tags": ["loan"],  # 不是问题子串，tag 不命中
        }
        score = _score_example(question, set(), example)
        # 词级重叠: how / many / accounts / running = 4
        assert score == 4

    def test_score_example_english_no_overlap_zero(self):
        """英文问题与无关示例 → 词级无重叠 → 0 分。"""
        score = _score_example(
            "how many accounts are running", set(),
            {"question": "top districts by loan amount", "tags": []},
        )
        assert score == 0


class TestListing:
    async def test_list_term_names(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        names = await kb.list_term_names("demo")
        assert "平均贷款金额" in names
        assert "客户数量" in names
        assert await kb.list_term_names("other") == []

    async def test_list_example_questions(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        questions = await kb.list_example_questions("demo")
        assert any("平均贷款金额" in q for q in questions)


class TestLessons:
    LESSONS = """
lessons:
  - pattern: "no such table: loans"
    note: 表名是 loan 不是 loans
    sql_snippet: SELECT * FROM loan
    confirmed: true
  - pattern: Unknown column salary
    note: 工资列是 A11
    sql_snippet: SELECT A11 FROM district
    confirmed: false
"""

    async def test_lessons_parsed_and_confirmed_filter(self, kb, kb_dir):
        (kb_dir / "demo").mkdir(parents=True)
        (kb_dir / "demo" / "lessons.yml").write_text(self.LESSONS, encoding="utf-8")
        await kb.ensure_synced("demo")
        confirmed = await kb.list_lessons("demo")
        assert len(confirmed) == 1
        assert confirmed[0]["pattern"] == "no such table: loans"

    async def test_append_lesson_writes_pending(self, kb, kb_dir):
        await kb.append_lesson({"pattern": "x", "note": "y", "sql_snippet": "z"}, "demo")
        assert await kb.list_lessons("demo") == []  # pending 不注入
        assert (kb_dir / "demo" / "lessons.yml").exists()

    async def test_confirm_pending_lessons(self, kb, kb_dir):
        (kb_dir / "demo").mkdir(parents=True)
        (kb_dir / "demo" / "lessons.yml").write_text(self.LESSONS, encoding="utf-8")
        await kb.ensure_synced("demo")
        await kb.confirm_pending_lessons("demo")
        confirmed = await kb.list_lessons("demo")
        assert len(confirmed) == 2


class TestRules:
    async def test_rules_parsed(self, kb, kb_dir):
        (kb_dir / "demo").mkdir(parents=True)
        (kb_dir / "demo" / "rules.yml").write_text(
            "rules:\n"
            "  - rule: '年龄 = 1998 - YEAR(birth_date)'\n"
            "  - rule: '金额保留两位小数'\n",
            encoding="utf-8",
        )
        await kb.ensure_synced("demo")
        rules = await kb.list_rules("demo")
        assert "年龄 = 1998 - YEAR(birth_date)" in rules
        assert len(rules) == 2
        assert await kb.list_rules("other") == []


class TestEvolution:
    async def test_append_example_rewrites_yaml_and_syncs(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        await kb.append_example({
            "question": "每个客户的账户数量",
            "sql": "SELECT client_id, COUNT(*) FROM account GROUP BY client_id",
            "tags": ["客户", "账户"],
        }, "demo")

        hits = await kb.search_examples("账户数量", "demo")
        assert any(h.question == "每个客户的账户数量" for h in hits)

        text = (kb_dir / "demo" / "examples.yml").read_text(encoding="utf-8")
        assert "每个客户的账户数量" in text

    async def test_append_example_isolated_per_datasource(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        await kb.append_example({
            "question": "other 库的问题",
            "sql": "SELECT 1",
            "tags": ["x"],
        }, "other")

        assert await kb.search_examples("other 库", "demo") == []
        hits = await kb.search_examples("other 库", "other")
        assert len(hits) == 1

    async def test_append_term_rewrites_yaml_and_syncs(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")

        await kb.append_term({
            "term": "贷款笔数",
            "aliases": [],
            "mapping": "COUNT(loan.loan_id)",
            "tables": ["loan"],
            "definition": "贷款记录条数",
        }, "demo")

        hits = await kb.search_terms("贷款笔数", "demo")
        assert [h.term for h in hits] == ["贷款笔数"]


class TestPurgeDeletedFiles:
    """Deleting a YAML from disk must purge its mirror items.

    Keyed by (datasource, source_file) — a same-named file in another
    datasource must not keep stale rows alive (the old basename-only
    purge let demo/lessons.yml shield a deleted financial/lessons.yml).
    """

    LESSONS_A = """
lessons:
  - pattern: "pattern one"
    note: 教训一
    confirmed: true
"""

    LESSONS_B = """
lessons:
  - pattern: "pattern two"
    note: 教训二
    confirmed: true
"""

    async def test_ensure_synced_purges_items_of_deleted_file(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        assert (await kb.list_items())["demo"]["term"] == 2

        (kb_dir / "demo" / "semantics.yml").unlink()
        await kb.ensure_synced("demo")

        counts = (await kb.list_items())["demo"]
        assert "term" not in counts
        assert counts["example"] == 2  # 其他文件的条目保留
        assert counts["table"] == 1

    async def test_ensure_synced_purges_stale_sync_row(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced("demo")
        (kb_dir / "demo" / "semantics.yml").unlink()
        await kb.ensure_synced("demo")

        async with aiosqlite.connect(kb.db_path) as db:
            rows = await (await db.execute(
                "SELECT file_path FROM kb_sync"
            )).fetchall()
        assert [r[0] for r in rows] == ["demo/examples.yml", "demo/schema_notes.yml"]

    async def test_force_sync_purges_by_datasource_not_basename(self, kb, kb_dir):
        (kb_dir / "demo").mkdir(parents=True)
        (kb_dir / "financial").mkdir(parents=True)
        (kb_dir / "demo" / "lessons.yml").write_text(self.LESSONS_A, encoding="utf-8")
        (kb_dir / "financial" / "lessons.yml").write_text(self.LESSONS_B, encoding="utf-8")
        await kb.ensure_synced("demo")
        await kb.ensure_synced("financial")
        assert len(await kb.list_lessons("financial")) == 1

        (kb_dir / "financial" / "lessons.yml").unlink()
        await kb.force_sync()

        assert len(await kb.list_lessons("financial")) == 0
        assert len(await kb.list_lessons("demo")) == 1


class TestInitSchemaNotes:
    def test_generates_skeleton(self, kb, kb_dir):
        schema = SchemaInfo(tables=[
            TableInfo(name="district", columns=[
                ColumnInfo(name="A2", type="varchar"),
                ColumnInfo(name="A11", type="int"),
            ]),
        ])
        assert kb.init_schema_notes(schema, "demo") is True
        text = (kb_dir / "demo" / "schema_notes.yml").read_text(encoding="utf-8")
        assert "district" in text
        assert "A2" in text

    def test_refuses_overwrite(self, kb, kb_dir):
        write_kb(kb_dir)
        schema = SchemaInfo(tables=[])
        assert kb.init_schema_notes(schema, "demo") is False
        assert "行政区划表" in (kb_dir / "demo" / "schema_notes.yml").read_text(encoding="utf-8")


class TestInitThreeWay:
    """LLM-assisted initialization writes: notes + terms + examples."""

    def test_init_notes_writes_annotated_tables(self, kb, kb_dir):
        tables = [{
            "name": "students",
            "description": "学生表",
            "columns": [{"name": "grade", "description": "成绩", "enums": []}],
            "metrics": [],
        }]
        assert kb.init_notes(tables, "demo") is True
        text = (kb_dir / "demo" / "schema_notes.yml").read_text(encoding="utf-8")
        assert "学生表" in text

    def test_init_terms_writes_terms(self, kb, kb_dir):
        terms = [{
            "term": "学生数",
            "aliases": [],
            "mapping": "COUNT(students.id)",
            "tables": ["students"],
            "definition": "学生记录数",
        }]
        assert kb.init_terms(terms, "demo") is True
        text = (kb_dir / "demo" / "semantics.yml").read_text(encoding="utf-8")
        assert "学生数" in text

    def test_init_examples_writes_templates(self, kb, kb_dir):
        examples = [{
            "template": True,
            "question": "学生总数是多少",
            "sql": "SELECT COUNT(*) FROM students",
            "tags": ["学生"],
        }]
        assert kb.init_examples(examples, "demo") is True
        text = (kb_dir / "demo" / "examples.yml").read_text(encoding="utf-8")
        assert "学生总数是多少" in text

    def test_init_three_way_refuses_overwrite(self, kb, kb_dir):
        write_kb(kb_dir)
        assert kb.init_notes([], "demo") is False
        assert kb.init_terms([], "demo") is False
        assert kb.init_examples([], "demo") is False

    def test_init_exists_lists_existing_files(self, kb, kb_dir):
        assert kb.init_exists("demo") == []
        (kb_dir / "demo").mkdir(parents=True)
        (kb_dir / "demo" / "semantics.yml").write_text("terms: []\n", encoding="utf-8")
        assert kb.init_exists("demo") == ["semantics.yml"]
