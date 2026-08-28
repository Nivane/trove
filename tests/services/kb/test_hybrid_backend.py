"""Hybrid retrieval backend tests — FTS5 + BM25 sparse channel.

覆盖:kb_fts 镜像同步/删除传播/dispatch 读时切换/硬门保持/BM25 排序/
resolver 与 DatasourceConfig 持久化。
"""

import json

import aiosqlite
import pytest

from trove.services.kb.backends.fts import (
    fts_query,
    fts_index_text,
    normalize_bm,
)
from trove.services.kb.backends.hybrid import HybridBackend
from trove.services.kb.backends.registry import resolver_from_configs
from trove.services.kb.service import KbService

SEMANTICS = """
semantic_model:
  - name: demo
    datasets:
      - name: loan
      - name: district
      - name: client
    metrics:
      - name: 平均贷款金额
        description: 贷款金额均值
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: AVG(loan.amount)
      - name: 客户数量
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: COUNT(client.client_id)
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

LESSONS = """
lessons:
  - pattern: "no such table: loans"
    note: 表名是 loan 不是 loans
    sql_snippet: SELECT * FROM loan
    confirmed: true
  - pattern: 平均贷款金额口径
    note: 用 AVG(amount) 而非 SUM
    sql_snippet: SELECT AVG(amount) FROM loan
    confirmed: true
"""

ALL_TABLES = ["loan", "district", "client"]


def _write_kb(kb_dir, ds="financial"):
    d = kb_dir / ds
    d.mkdir(parents=True, exist_ok=True)
    (d / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    (d / "examples.yml").write_text(EXAMPLES, encoding="utf-8")
    (d / "lessons.yml").write_text(LESSONS, encoding="utf-8")


def _cfg(name, backend="hybrid"):
    class C:
        def __init__(self, n, b):
            self.name = n
            self.retrieval_backend = b
    return C(name, backend)


async def _hybrid_kb(tmp_path, ds="financial"):
    """KbService + resolver:该数据源配 hybrid。"""
    resolve, bind = resolver_from_configs([_cfg(ds)])
    kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb", backend_resolver=resolve)
    bind(kb)
    _write_kb(kb.kb_dir, ds)
    await kb.ensure_synced(ds)
    return kb


@pytest.fixture
def kb(tmp_path):
    return KbService(tmp_path / "proj")


@pytest.fixture
def kb_dir(kb):
    kb.kb_dir.mkdir(parents=True)
    return kb.kb_dir


class TestFtsMirror:
    async def test_sync_builds_kb_fts(self, kb, kb_dir):
        """ensure_synced 后 kb_fts 与 kb_items 同事务镜像;term/rule 不索引。"""
        _write_kb(kb_dir)
        await kb.ensure_synced("financial")
        rows = await kb._rows(
            "SELECT kind, source_file FROM kb_fts ORDER BY kind")
        kinds = [r["kind"] for r in rows]
        assert kinds.count("example") + kinds.count("template") == 3
        assert kinds.count("lesson") == 2
        assert "term" not in kinds  # term 是子串语义,不入稀疏索引

    async def test_rewrite_file_updates_index(self, kb, kb_dir):
        _write_kb(kb_dir)
        await kb.ensure_synced("financial")
        examples = kb_dir / "financial" / "examples.yml"
        examples.write_text(
            EXAMPLES.replace("客户年龄分布", "客户收入分布"), encoding="utf-8")
        await kb.ensure_synced("financial")
        rows = await kb._rows(
            "SELECT payload FROM kb_items WHERE kind = 'example' "
            "AND datasource = 'financial'")
        texts = [json.loads(r["payload"])["question"] for r in rows]
        assert "客户收入分布" in texts
        assert "客户年龄分布" not in texts

    async def test_delete_propagation_purges_kb_fts(self, kb, kb_dir):
        """YAML 删除 → kb_items 与 kb_fts 同步清除(删除传播)。"""
        _write_kb(kb_dir)
        await kb.ensure_synced("financial")
        (kb_dir / "financial" / "lessons.yml").unlink()
        await kb.ensure_synced("financial")
        rows = await kb._rows(
            "SELECT rowid FROM kb_fts WHERE kind = 'lesson' "
            "AND datasource = 'financial'")
        assert rows == []

    async def test_delete_kb_clears_fts(self, kb, kb_dir):
        _write_kb(kb_dir)
        await kb.ensure_synced("financial")
        await kb.delete_kb("financial")
        rows = await kb._rows("SELECT rowid FROM kb_fts")
        assert rows == []

    async def test_mirror_rebuild_keeps_fts_consistent(self, kb, kb_dir):
        """缺 datasource 列的旧镜像重建时 kb_fts 一并重建。"""
        _write_kb(kb_dir)
        await kb.ensure_synced("financial")
        async with aiosqlite.connect(kb.db_path) as db:
            await db.execute(
                "CREATE TABLE old_items (id INTEGER PRIMARY KEY, "
                "kind TEXT, item_key TEXT, payload TEXT, source_file TEXT)")
            await db.execute(
                "INSERT INTO old_items (id, kind, item_key, payload, source_file) "
                "SELECT id, kind, item_key, payload, source_file FROM kb_items")
            await db.execute("DROP TABLE kb_items")
            await db.execute("ALTER TABLE old_items RENAME TO kb_items")
            await db.commit()
        await kb.ensure_synced("financial")
        rows = await kb._rows(
            "SELECT rowid FROM kb_fts WHERE datasource = 'financial'")
        assert len(rows) == 7  # 3 example + 2 lesson + 2 metric(分型导入)


class TestDispatch:
    async def test_builtin_config_resolves_none(self, tmp_path):
        resolve, bind = resolver_from_configs([_cfg("financial", "builtin")])
        kb = KbService(tmp_path / "proj", backend_resolver=resolve)
        bind(kb)
        assert kb._backend_for("financial") is None

    async def test_unknown_backend_falls_back_builtin(self, tmp_path):
        resolve, bind = resolver_from_configs([_cfg("financial", "nope")])
        kb = KbService(tmp_path / "proj", backend_resolver=resolve)
        bind(kb)
        assert kb._backend_for("financial") is None

    async def test_hybrid_config_resolves_backend(self, tmp_path):
        resolve, bind = resolver_from_configs([_cfg("financial")])
        kb = KbService(tmp_path / "proj", backend_resolver=resolve)
        bind(kb)
        backend = kb._backend_for("financial")
        assert isinstance(backend, HybridBackend)
        assert kb._backend_for("other") is None  # 未配置 → builtin

    async def test_resolver_error_degrades_builtin(self, tmp_path):
        def boom(ds):
            raise RuntimeError("resolver broke")
        kb = KbService(tmp_path / "proj", backend_resolver=boom)
        assert kb._backend_for("financial") is None


class TestHybridSearch:
    async def test_hybrid_examples_via_dispatch(self, tmp_path):
        kb = await _hybrid_kb(tmp_path)
        hits = await kb.search_examples("每个地区的平均贷款金额", "financial", limit=2)
        assert len(hits) == 2
        assert any("loan" in h.sql.lower() for h in hits)

    async def test_hard_gate_irrelevant_returns_empty(self, tmp_path):
        kb = await _hybrid_kb(tmp_path)
        hits = await kb.search_examples("zzz 无关问题 xyz", "financial", limit=2)
        assert hits == []
        lessons = await kb.search_lessons("zzz 无关问题 xyz", "financial", limit=2)
        assert lessons == []

    async def test_terms_delegate_to_builtin(self, tmp_path):
        kb = await _hybrid_kb(tmp_path)
        hits = await kb.search_terms("平均贷款金额", "financial")
        assert any(h.term == "平均贷款金额" for h in hits)

    async def test_lessons_ranked_with_bm25(self, tmp_path):
        kb = await _hybrid_kb(tmp_path)
        lessons = await kb.search_lessons("表名写错 loans 表不存在", "financial", limit=2)
        assert lessons[0]["pattern"] == "no such table: loans"

    async def test_table_anchor_filters_in_hybrid(self, tmp_path):
        kb = await _hybrid_kb(tmp_path)
        hits = await kb.search_examples(
            "贷款", "financial", limit=3,
            tables=["loan"], all_tables=ALL_TABLES,
        )
        assert all("client" not in (h.question + h.sql).lower() for h in hits)

    async def test_no_sync_degrades_empty(self, tmp_path):
        """kb_fts 未建/未同步 → 空,不抛(降级链)。"""
        kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb",
                       backend_resolver=lambda ds: HybridBackend(KbService(tmp_path / "proj")))
        hits = await kb.search_examples("贷款", "financial", limit=2)
        assert hits == []


class TestFtsHelpers:
    def test_fts_query_strips_stopwords_and_ors(self):
        q = fts_query("what is the average loan amount")
        assert '"average"' in q and '"loan"' in q and '"amount"' in q
        assert "the" not in q

    def test_fts_query_empty_for_stopwords_only(self):
        assert fts_query("the and or") == ""

    def test_fts_index_text_cjk_splits_chars(self):
        text = fts_index_text("每个地区的平均贷款金额", ["贷款"], "SELECT AVG(amount) FROM loan")
        for ch in "平均贷款":
            assert ch in text.split()
        assert "loan" in text.split()

    def test_normalize_bm(self):
        scores = {1: -5.0, 2: -1.0, 3: -3.0}
        norm = normalize_bm(scores)
        assert norm[1] == 1.0 and norm[2] == 0.0  # 最相关 → 1,最不相关 → 0
        assert 0.0 < norm[3] < 1.0

    def test_normalize_bm_flat(self):
        assert normalize_bm({1: -2.0, 2: -2.0}) == {1: 1.0, 2: 1.0}
        assert normalize_bm({}) == {}


class TestConfigStorePersistence:
    def test_default_backend_builtin(self):
        from trove.core.types import DatasourceConfig
        assert DatasourceConfig(name="x", type="sqlite").retrieval_backend == "builtin"

    async def test_roundtrip_persists_backend(self, tmp_path):
        from trove.services.datasource.config_store import ConfigStore
        from trove.core.types import DatasourceConfig
        store = ConfigStore(tmp_path / ".trove" / "datasources.yml")
        cfg = DatasourceConfig(
            name="financial", type="mysql",
            connection_params={"host": "h"}, credentials={},
            ds_id="abc123", retrieval_backend="hybrid",
        )
        store.save_configs([cfg])
        loaded = store.load_configs()
        assert loaded[0].retrieval_backend == "hybrid"

    async def test_old_config_without_field_defaults_builtin(self, tmp_path):
        """旧 datasources.yml 无该字段 → 读取默认 builtin(向后兼容)。"""
        from trove.services.datasource.config_store import ConfigStore
        path = tmp_path / ".trove" / "datasources.yml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "datasources:\n"
            "  - id: d1\n    name: financial\n    type: mysql\n"
            "    connection: {}\n    credentials: {}\n    default: false\n",
            encoding="utf-8",
        )
        loaded = ConfigStore(path).load_configs()
        assert loaded[0].retrieval_backend == "builtin"
