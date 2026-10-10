"""Tests for the SQLite hybrid store (FTS5 + cosine + RRF + rerank).

Exercises the full recall path without PostgreSQL: index a few docs, run a
query, and assert the RRF-fused + reranked candidates come back ranked by the
configured reranker.
"""

import aiosqlite
import pytest

from trove.services.retrieval import (
    DeterministicReranker,
    RetrievalDoc,
    SqliteHybridStore,
)
from trove.services.retrieval.rerank import CrossEncoderReranker


class FakeEmbedder:
    dim = 16

    async def embed(self, texts):
        import math

        out = []
        for t in texts:
            vec = [0.0] * self.dim
            for ch in t:
                vec[ord(ch) % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


@pytest.fixture
def store(tmp_path):
    return SqliteHybridStore(
        tmp_path / "retrieval.sqlite", FakeEmbedder(), DeterministicReranker())


async def test_recall_ranks_relevant_first(store):
    docs = [
        RetrievalDoc(content="贷款 平均 金额 怎么 计算", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="ex1"),
        RetrievalDoc(content="完全 无关 的 足球 比赛 比分", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="ex2"),
        RetrievalDoc(content="地区 分布 与 账户 数量 的 关系", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="ex3"),
    ]
    await store.index_many(docs)

    hits = await store.recall("贷款 平均 金额", k=3, datasource="ds")
    assert hits, "expected at least one hit"
    assert hits[0].content.startswith("贷款")


async def test_recall_scoped_by_datasource(store):
    await store.index_many([
        RetrievalDoc(content="贷款 金额", datasource="ds1", kind="kb",
                     source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="贷款 金额", datasource="ds2", kind="kb",
                     source_file="a.yml", item_key="e2"),
    ])
    ds1 = await store.recall("贷款 金额", k=5, datasource="ds1")
    ds2 = await store.recall("贷款 金额", k=5, datasource="ds2")
    assert {h.doc_id for h in ds1} == {"e1"}
    assert {h.doc_id for h in ds2} == {"e2"}


async def test_delete_source(store):
    await store.index_many([
        RetrievalDoc(content="贷款 金额", datasource="ds", kind="kb",
                     source_file="a.yml", item_key="e1"),
    ])
    await store.delete_source("ds", "a.yml")
    hits = await store.recall("贷款 金额", k=5, datasource="ds")
    assert hits == []


async def test_clear(store):
    await store.index_many([
        RetrievalDoc(content="贷款 金额", datasource="ds", kind="kb",
                     source_file="a.yml", item_key="e1"),
    ])
    await store.clear("ds")
    assert await store.recall("贷款 金额", k=5, datasource="ds") == []


async def test_cross_encoder_reranker_used(store):
    # swap to cross-encoder reranker, ensure recall still returns ranked hits
    store._reranker = CrossEncoderReranker(FakeEmbedder())
    await store.index_many([
        RetrievalDoc(content="贷款 平均 金额 是 多少", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="足球 比赛 直播 时间", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e2"),
    ])
    hits = await store.recall("贷款 平均 金额", k=2, datasource="ds")
    assert hits and hits[0].doc_id == "e1"


async def test_recall_score_is_rank_order_rrf(store):
    """无精排时命中 score = 排序位映射后的 RRF 分,而不是只留下名次。

    保留分数是给下游 ``_fuse_extra_sim``(0.5 权重)一个和检索质量相关的量
    —— 若只留名次,融合就退化成纯 rank 常量。
    """
    store._reranker = None
    await store.index_many([
        RetrievalDoc(content="贷款 平均 金额 怎么 计算", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="地区 分布 与 账户 数量", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e2"),
        RetrievalDoc(content="足球 比赛 直播 时间", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e3"),
    ])
    hits, meta = await store.recall(
        "贷款 平均 金额", k=3, datasource="ds", return_meta=True)
    assert hits[0].doc_id == "e1"
    assert hits[0].score == pytest.approx(1.0)
    scores = [h.score for h in hits]
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores == sorted(scores, reverse=True)
    # 无精排 → 最终序就是 RRF 序
    assert [h.doc_id for h in hits] == meta["rrf_ids"][:len(hits)]
    assert meta["rerank_used"] is False


# ── FTS 通道:写读两侧同一套预分词约定 ───────────────────
#
# fts.py 的约定是**两侧对称**:索引侧 fts_index_text、查询侧 fts_query,
# CJK 统一拆单字符。下面三条钉住 store 也在守这套约定。


async def test_multiword_question_recalls_either_term(store):
    """FTS5 OR 析取:多词问句命中**任一**词的文档都要进 keyword 通道。

    回归钉(2026-10-10):旧 ``_fts_ids`` 先把 MATCH 串 ``.lower()`` ——
    FTS5 的 ``OR`` 是大写关键字,小写后成了对字面词 "or" 的合取,
    任何多词查询恒 0 命中(实测 ``'"loan" or "client"'`` 0 行,大写 214 行)。
    """
    store._reranker = None
    await store.index_many([
        RetrievalDoc(content="loan amount statistics", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="client district list", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e2"),
    ])
    hits, meta = await store.recall(
        "average loan per client", k=10, datasource="ds", return_meta=True)
    # keyword 通道两篇都召回(OR);只命中任一词也必须召回
    assert meta["branch_sizes"][0] == 2
    assert {h.doc_id for h in hits} == {"e1", "e2"}


async def test_question_punctuation_does_not_break_match(store):
    """问句带 FTS5 语法字符(? * ' 括号)不得把 MATCH 弄成 syntax error。

    回归钉:eval 脚本曾把问句原样喂 MATCH,实测在 "?" 上直接
    ``OperationalError: fts5: syntax error near "?"``。归一后每个 token
    都是引号包住的字面量,语法字符进不了 MATCH 语法。
    """
    await store.index_many([
        RetrievalDoc(content="loan amount by region", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
    ])
    hits = await store.recall(
        "What's the loan amount? (per region*)", k=5, datasource="ds")
    assert [h.doc_id for h in hits] == ["e1"]


async def test_cjk_question_matches_unsplit_content(store):
    """中文两侧同一套预分词:unicode61 把连续 CJK 视为**一个** token,
    索引与查询必须都拆成单字符才能字对字命中;只在查询侧拆(或都不拆)
    都会在"内容与问句写法不同"时漏召回。"""
    store._reranker = None
    await store.index_many([
        RetrievalDoc(content="贷款金额最高的地区", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e1"),
        RetrievalDoc(content="足球比赛日程", datasource="ds",
                     kind="kb", source_file="a.yml", item_key="e2"),
    ])
    hits, meta = await store.recall(
        "贷款金额", k=5, datasource="ds", return_meta=True)
    assert meta["branch_sizes"][0] == 1
    assert hits[0].doc_id == "e1"


async def test_reindex_replaces_fts_row_instead_of_orphaning(store):
    """重索引同一 item_key:旧 doc_fts 行必须随旧 documents 行一起删掉。

    回归钉:旧 index_many 只删 documents 旧行 —— doc_fts 旧行按旧 rowid
    留在索引里(join 不回主子行,检索无感,但永久占着 FTS 索引,
    重索引一次翻一倍;实测 376 条 KB 条目重索引一次 → 752 行)。
    """
    await store.index_many([
        RetrievalDoc(content="贷款 金额", datasource="ds", kind="kb",
                     source_file="a.yml", item_key="e1"),
    ])
    await store.index_many([
        RetrievalDoc(content="新的 内容 完全 不同", datasource="ds", kind="kb",
                     source_file="a.yml", item_key="e1"),
    ])
    async with aiosqlite.connect(store._db) as db:
        cur = await db.execute("SELECT count(*) FROM doc_fts")
        assert (await cur.fetchone())[0] == 1
        cur = await db.execute("SELECT count(*) FROM documents")
        assert (await cur.fetchone())[0] == 1
    # 重索引后的文档仍可检索(新内容命中)
    hits = await store.recall("新的 内容", k=5, datasource="ds")
    assert [h.doc_id for h in hits] == ["e1"]
