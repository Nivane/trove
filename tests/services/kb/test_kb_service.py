"""KbService tests — per-datasource YAML source, SQLite mirror, lazy sync.

Layout: .trove/kb/<datasource>/{schema_notes,semantics,examples}.yml
Legacy flat files (kb root) are auto-migrated into a datasource dir.
"""

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


class TestEnablement:
    async def test_disabled_when_kb_dir_missing(self, kb):
        assert kb.enabled is False
        assert await kb.search_terms("平均贷款金额", "demo") == []
        assert await kb.search_examples("平均贷款金额", "demo") == []


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
