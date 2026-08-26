"""RAG backend tests — sparse(FTS5/BM25)+ dense(embedding)双通道 RRF。

覆盖:向量镜像同步/删除传播/RRF 融合/硬门保持/降级链/config 持久化/
gateway embedding mock。测试用确定性 hashed n-gram 作为 fake embedder
(零网络),与生产走 LLMGateway.embedding 的路径解耦。
"""

import json

import pytest

from trove.services.kb.backends.dense import (
    SqliteVectorStore,
    cosine,
    _pack_vector,
    _unpack_vector,
    _vector_dsn,
    vector_store_for,
)
from trove.services.kb.backends.rag import RagBackend, rrf_fuse
from trove.services.kb.backends.registry import resolver_from_configs
from trove.services.kb.embeddings import embed as det_embed
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
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: AVG(loan.amount)
"""

EXAMPLES = """
examples:
  - question: 每个地区的平均贷款金额
    sql: SELECT d.A2, AVG(l.amount) FROM loan l JOIN district d GROUP BY d.A2
    tags: [地区, 贷款, 聚合]
  - question: What is the average loan amount?
    sql: SELECT AVG(amount) FROM loan
    tags: [loan, avg]
    aggregate: true
  - question: 各地区的客户数量
    sql: SELECT d.A2, COUNT(c.client_id) FROM client c JOIN district d GROUP BY d.A2
    tags: [地区, 客户, 分组]
"""

LESSONS = """
lessons:
  - pattern: "no such table: loans"
    note: 表名是 loan 不是 loans
    sql_snippet: SELECT * FROM loan
    confirmed: true
"""


class FakeEmbed:
    """确定性 hashed n-gram embedder(模拟 dense 通道,零网络)。"""

    async def embed(self, texts):
        return [det_embed(t) for t in texts]


class _Cfg:
    name = "financial"
    retrieval_backend = "rag"
    embedding_model = "test-embed"
    vector_backend = "sqlite"
    vector_dsn = ""


def _write_kb(kb_dir, ds="financial"):
    d = kb_dir / ds
    d.mkdir(parents=True, exist_ok=True)
    (d / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    (d / "examples.yml").write_text(EXAMPLES, encoding="utf-8")
    (d / "lessons.yml").write_text(LESSONS, encoding="utf-8")


async def _rag_kb(tmp_path, embedder=None, ds="financial"):
    """KbService + rag resolver(fake embedder)。"""
    resolve, bind = resolver_from_configs(
        [_Cfg()], embedder_factory=lambda cfg: embedder or FakeEmbed())
    kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb",
                   backend_resolver=resolve)
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


class TestRrfFuse:
    def test_both_channels(self):
        out = rrf_fuse({1: 0.9, 2: 0.5}, {2: 0.8, 3: 0.6})
        assert out[2] > out[1] > 0      # 双通道命中 > 单通道 rank1
        assert out[1] == pytest.approx(0.5)  # 单通道 rank1 → 0.5
        assert max(out.values()) <= 1.0 and min(out.values()) > 0

    def test_sparse_only(self):
        out = rrf_fuse({1: 0.9, 2: 0.1}, {})
        assert out[1] == pytest.approx(0.5)
        assert out[2] < out[1]

    def test_empty(self):
        assert rrf_fuse({}, {}) == {}


class TestVectorStore:
    async def test_pack_unpack_roundtrip(self):
        vec = [0.5, -1.0, 2.25]
        assert _unpack_vector(_pack_vector(vec)) == pytest.approx(vec)

    async def test_cosine(self):
        assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
        assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
        assert cosine([1, 0], []) == 0.0

    async def test_replace_query_delete(self, tmp_path):
        kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb")
        kb.kb_dir.mkdir(parents=True)
        store = SqliteVectorStore(kb)
        await store.replace("ds", "a.yml", [(1, "example", [1.0, 0.0])])
        await store.replace("ds", "b.yml", [(2, "example", [0.0, 1.0])])
        hits = await store.query("ds", [1.0, 0.0], ("example",), 5)
        assert hits[0] == (1, pytest.approx(1.0))
        assert hits[1][0] == 2

        await store.delete_file("ds", "a.yml")
        hits = await store.query("ds", [1.0, 0.0], ("example",), 5)
        assert [i for i, _ in hits] == [2]

        await store.replace("ds", "a.yml", [(1, "example", [1.0, 0.0])])
        await store.clear("ds")
        assert await store.query("ds", [1.0, 0.0], ("example",), 5) == []

    async def test_query_filters_kind(self, tmp_path):
        kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb")
        kb.kb_dir.mkdir(parents=True)
        store = SqliteVectorStore(kb)
        await store.replace("ds", "a.yml", [
            (1, "example", [1.0, 0.0]), (2, "lesson", [1.0, 0.0]),
        ])
        hits = await store.query("ds", [1.0, 0.0], ("example",), 5)
        assert [i for i, _ in hits] == [1]


class TestRagBackend:
    async def test_sync_builds_vectors(self, tmp_path):
        kb = await _rag_kb(tmp_path)
        rows = await kb._rows(
            "SELECT id, kind FROM kb_vectors ORDER BY kind")
        kinds = [r["kind"] for r in rows]
        assert kinds.count("example") == 3
        assert kinds.count("lesson") == 1
        assert "term" not in kinds  # term 是子串语义,不建向量

    async def test_retrieval_ranks_relevant_first(self, tmp_path):
        kb = await _rag_kb(tmp_path)
        hits = await kb.search_examples("每个地区的平均贷款金额", "financial", limit=2)
        assert any("loan" in h.sql.lower() for h in hits)

    async def test_hard_gate_irrelevant_empty(self, tmp_path):
        kb = await _rag_kb(tmp_path)
        assert await kb.search_examples("zzz 无关问题 xyz", "financial") == []
        assert await kb.search_lessons("zzz 无关问题 xyz", "financial") == []

    async def test_lessons_via_dense(self, tmp_path):
        kb = await _rag_kb(tmp_path)
        lessons = await kb.search_lessons("loans 表不存在", "financial", limit=2)
        assert lessons[0]["pattern"] == "no such table: loans"

    async def test_dense_failure_degrades_to_sparse(self, tmp_path):
        kb = await _rag_kb(tmp_path)

        class BrokenEmbed:
            async def embed(self, texts):
                raise RuntimeError("embed api down")

        kb2 = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb")
        rag = RagBackend(kb2, embedder=BrokenEmbed(),
                         vector_store=SqliteVectorStore(kb2))
        hits = await rag.search_examples("每个地区的平均贷款金额", "financial", limit=2)
        assert len(hits) >= 1  # 稀疏兜底

    async def test_no_embedder_degrades_to_sparse(self, tmp_path):
        kb = await _rag_kb(tmp_path, embedder=None)
        hits = await kb.search_examples("每个地区的平均贷款金额", "financial", limit=2)
        assert len(hits) >= 1

    async def test_table_anchor_filters_in_rag(self, tmp_path):
        kb = await _rag_kb(tmp_path)
        hits = await kb.search_examples(
            "贷款", "financial", limit=3,
            tables=["loan"], all_tables=["loan", "district", "client"],
        )
        assert all("client" not in (h.question + h.sql).lower() for h in hits)

    async def test_delete_propagation_clears_vectors(self, tmp_path):
        kb = await _rag_kb(tmp_path)
        (kb.kb_dir / "financial" / "lessons.yml").unlink()
        await kb.ensure_synced("financial")
        rows = await kb._rows("SELECT id FROM kb_vectors WHERE kind='lesson'")
        assert rows == []

    async def test_rewrite_reindexes(self, tmp_path):
        kb = await _rag_kb(tmp_path)
        (kb.kb_dir / "financial" / "examples.yml").write_text(
            EXAMPLES.replace("客户数量", "客户总数"), encoding="utf-8")
        await kb.ensure_synced("financial")
        rows = await kb._rows(
            "SELECT payload FROM kb_items WHERE kind='example'")
        texts = [json.loads(r["payload"])["question"] for r in rows]
        assert any("客户总数" in q for q in texts)
        assert not any("客户数量" in q for q in texts)
        vec_rows = await kb._rows("SELECT id FROM kb_vectors")
        assert len(vec_rows) == 4  # 3 example + 1 lesson,索引跟随重建

    async def test_delete_kb_clears_vectors(self, tmp_path):
        kb = await _rag_kb(tmp_path)
        await kb.delete_kb("financial")
        rows = await kb._rows("SELECT id FROM kb_vectors")
        assert rows == []


class TestGatewayEmbedding:
    async def test_mock_embedding_cycles(self):
        from trove.llm.gateway import LLMGateway
        g = LLMGateway(mock_embedding=[[1.0, 0.0], [0.0, 1.0]])
        out = await g.embedding("m", ["a", "b", "c"])
        assert len(out) == 3
        assert out[0] == [1.0, 0.0]
        assert out[2] == [1.0, 0.0]  # cycle

    async def test_mock_response_does_not_leak_into_embedding(self):
        """mock_response 只管 chat;embedding 无 mock_embedding 走真实路径
        (无网络 → LLMError,证明 string mock 不会误当向量返回)。"""
        from trove.core.errors import LLMError
        from trove.llm.gateway import LLMGateway
        g = LLMGateway(mock_response="SELECT 1")
        with pytest.raises(LLMError):
            await g.embedding("m", ["a"])


class TestRagConfig:
    async def test_roundtrip_persists_rag_fields(self, tmp_path):
        from trove.core.types import DatasourceConfig
        from trove.services.datasource.config_store import ConfigStore
        cfg = DatasourceConfig(
            name="financial", type="mysql",
            ds_id="abc", retrieval_backend="rag",
            embedding_model="bge-m3", vector_backend="pgvector",
            vector_dsn="postgresql://v@h:5432/vec",
        )
        store = ConfigStore(tmp_path / ".trove" / "datasources.yml")
        store.save_configs([cfg])
        loaded = store.load_configs()[0]
        assert loaded.retrieval_backend == "rag"
        assert loaded.embedding_model == "bge-m3"
        assert loaded.vector_backend == "pgvector"
        assert loaded.vector_dsn == "postgresql://v@h:5432/vec"

    async def test_resolver_builds_rag_backend(self, tmp_path):
        resolve, bind = resolver_from_configs(
            [_Cfg()], embedder_factory=lambda cfg: FakeEmbed())
        kb = KbService(tmp_path / "proj", backend_resolver=resolve)
        bind(kb)
        backend = kb._backend_for("financial")
        assert isinstance(backend, RagBackend)

    async def test_resolver_falls_back_builtin_for_other(self, tmp_path):
        resolve, bind = resolver_from_configs([_Cfg()])
        kb = KbService(tmp_path / "proj", backend_resolver=resolve)
        bind(kb)
        assert kb._backend_for("other") is None


class TestAggregateGate:
    async def test_aggregate_example_zero_score_excluded(self, kb, kb_dir):
        """回归:聚合/日期模板的降权 max(1,...) 不得把 0 分抬成 1。

        无关问题 + aggregate 示例 → det 必须为 0(硬门保持),而非 1。
        """
        (kb_dir / "demo").mkdir(parents=True)
        (kb_dir / "demo" / "examples.yml").write_text(
            "examples:\n"
            "  - question: What is the average loan amount?\n"
            "    sql: SELECT AVG(amount) FROM loan\n"
            "    tags: [loan, avg]\n"
            "    aggregate: true\n"
            "  - question: 客户年龄分布\n"
            "    sql: SELECT age, COUNT(*) FROM client GROUP BY age\n"
            "    tags: [客户]\n",
            encoding="utf-8",
        )
        await kb.ensure_synced("demo")
        assert await kb.search_examples("zzz 无关问题", "demo") == []


class TestVectorStoreDefault:
    """默认向量后端:postgres 业务库 → pgvector(同实例推导 dsn),其它 → sqlite。"""

    def test_postgres_cfg_derives_same_instance_dsn(self):
        from trove.core.types import DatasourceConfig

        cfg = DatasourceConfig(
            name="trove", type="postgres",
            connection_params={"host": "pg", "port": 5432,
                               "user": "trove", "password": "p",
                               "database": "trove"},
            vector_backend="pgvector",
        )
        assert _vector_dsn(cfg) == "postgresql://trove:p@pg:5432/trove"

    def test_explicit_vector_dsn_wins(self):
        from trove.core.types import DatasourceConfig

        cfg = DatasourceConfig(
            name="trove", type="postgres",
            connection_params={"host": "pg", "database": "trove"},
            vector_backend="pgvector", vector_dsn="postgresql://v@h:5432/vec",
        )
        assert _vector_dsn(cfg) == "postgresql://v@h:5432/vec"

    def test_non_postgres_cfg_has_no_derived_dsn(self):
        from trove.core.types import DatasourceConfig

        cfg = DatasourceConfig(name="demo", type="sqlite",
                               connection_params={"path": "x.db"})
        assert _vector_dsn(cfg) == ""

    def test_postgres_cfg_builds_pgvector_store(self, tmp_path):
        from trove.core.types import DatasourceConfig
        from trove.services.kb.backends.dense import PgVectorStore
        from trove.services.kb.service import KbService

        cfg = DatasourceConfig(
            name="trove", type="postgres",
            connection_params={"host": "pg", "database": "trove",
                               "user": "u", "password": "p"},
            vector_backend="pgvector",
        )
        kb = KbService(tmp_path / "proj")
        store = vector_store_for(kb, cfg)
        assert isinstance(store, PgVectorStore)
        assert store._dsn == "postgresql://u:p@pg:5432/trove"

    def test_non_postgres_cfg_builds_sqlite_store(self, tmp_path):
        from trove.core.types import DatasourceConfig
        from trove.services.kb.service import KbService

        cfg = DatasourceConfig(name="demo", type="sqlite",
                               connection_params={"path": "x.db"})
        kb = KbService(tmp_path / "proj")
        assert isinstance(vector_store_for(kb, cfg), SqliteVectorStore)
