"""KbService tests — YAML source, SQLite mirror, lazy sync, retrieval."""

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


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


@pytest.fixture
def kb_dir(kb):
    kb.kb_dir.mkdir(parents=True)
    return kb.kb_dir


def write_kb(kb_dir):
    (kb_dir / "schema_notes.yml").write_text(SCHEMA_NOTES, encoding="utf-8")
    (kb_dir / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    (kb_dir / "examples.yml").write_text(EXAMPLES, encoding="utf-8")


class TestEnablement:
    async def test_disabled_when_kb_dir_missing(self, kb):
        assert kb.enabled is False
        assert await kb.search_terms("平均贷款金额") == []
        assert await kb.search_examples("平均贷款金额") == []


class TestSync:
    async def test_sync_imports_all_files(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()
        counts = await kb.list_items()
        assert counts == {"table": 1, "term": 2, "example": 2, "template": 1}

    async def test_mtime_reloads_only_changed_file(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()

        # Add a new example to examples.yml
        examples = kb_dir / "examples.yml"
        examples.write_text(
            EXAMPLES.replace(
                "  - question: 客户年龄分布",
                "  - question: 新问题\n    sql: SELECT 1\n    tags: [新]\n  - question: 客户年龄分布",
            ),
            encoding="utf-8",
        )
        await kb.ensure_synced()

        hits = await kb.search_examples("新问题")
        assert any(h.question == "新问题" for h in hits)
        # semantics unchanged (not reloaded unnecessarily — still searchable)
        terms = await kb.search_terms("平均贷款金额")
        assert len(terms) == 1

    async def test_broken_yaml_keeps_old_mirror(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()

        (kb_dir / "examples.yml").write_text("not: [valid: yaml", encoding="utf-8")
        await kb.ensure_synced()

        # Old mirror still serves queries
        hits = await kb.search_examples("贷款")
        assert len(hits) > 0


class TestSearchTerms:
    async def test_chinese_substring_match(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()

        hits = await kb.search_terms("哪个地区的平均贷款金额最高？")
        assert len(hits) == 1
        assert hits[0].term == "平均贷款金额"
        assert hits[0].mapping == "AVG(loan.amount)"
        assert "district" in hits[0].tables

    async def test_alias_match(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()
        hits = await kb.search_terms("贷款均值怎么算")
        assert [h.term for h in hits] == ["平均贷款金额"]

    async def test_no_match_returns_empty(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()
        assert await kb.search_terms("完全不相关的查询") == []


class TestTableNotes:
    async def test_notes_retrieved_for_tables(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()

        notes = await kb.table_notes(["district"])
        assert "district" in notes
        assert notes["district"].description == "行政区划表，每地区一条记录"
        assert notes["district"].columns["A2"] == "地区名称"
        assert notes["district"].metrics["avg_loan_by_district"] == "按地区分组的平均贷款金额"

    async def test_unknown_table_absent_and_empty_desc_skipped(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()

        notes = await kb.table_notes(["district", "nonexistent"])
        assert "nonexistent" not in notes
        assert "A11" not in notes["district"].columns  # empty description skipped


class TestSearchExamples:
    async def test_scoring_orders_by_relevance(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()

        hits = await kb.search_examples("哪个地区的平均贷款金额最高？", limit=3)
        assert hits[0].question == "哪个地区的平均贷款金额最高？"  # self-match wins
        assert hits[0].score > hits[-1].score
        assert all(h.score > 0 for h in hits)

    async def test_limit_caps_results(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()
        hits = await kb.search_examples("贷款", limit=1)
        assert len(hits) == 1

    async def test_zero_score_excluded(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()
        assert await kb.search_examples("zzz 无关问题") == []


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
        await kb.ensure_synced()

        await kb.append_example({
            "question": "每个客户的账户数量",
            "sql": "SELECT client_id, COUNT(*) FROM account GROUP BY client_id",
            "tags": ["客户", "账户"],
        })

        hits = await kb.search_examples("账户数量")
        assert any(h.question == "每个客户的账户数量" for h in hits)

        # YAML file itself contains the new entry (single source of truth)
        text = (kb_dir / "examples.yml").read_text(encoding="utf-8")
        assert "每个客户的账户数量" in text

    async def test_append_term_rewrites_yaml_and_syncs(self, kb, kb_dir):
        write_kb(kb_dir)
        await kb.ensure_synced()

        await kb.append_term({
            "term": "贷款笔数",
            "aliases": [],
            "mapping": "COUNT(loan.loan_id)",
            "tables": ["loan"],
            "definition": "贷款记录条数",
        })

        hits = await kb.search_terms("贷款笔数")
        assert [h.term for h in hits] == ["贷款笔数"]


class TestInitSchemaNotes:
    def test_generates_skeleton(self, kb, kb_dir):
        schema = SchemaInfo(tables=[
            TableInfo(name="district", columns=[
                ColumnInfo(name="A2", type="varchar"),
                ColumnInfo(name="A11", type="int"),
            ]),
        ])
        assert kb.init_schema_notes(schema) is True
        text = (kb_dir / "schema_notes.yml").read_text(encoding="utf-8")
        assert "district" in text
        assert "A2" in text

    def test_refuses_overwrite(self, kb, kb_dir):
        write_kb(kb_dir)
        schema = SchemaInfo(tables=[])
        assert kb.init_schema_notes(schema) is False
        # Original content untouched
        assert "行政区划表" in (kb_dir / "schema_notes.yml").read_text(encoding="utf-8")
