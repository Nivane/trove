"""`Indexer.index_kb(rebuild=True)` 的重建语义:清理必须真的清掉。

重建路径(CLI `--rebuild` / 管理端重新索引)先删后写。原本删的是
``delete_source(datasource, "kb")`` —— 可 KB 文档落库时 ``source_file`` 是
YAML 文件名(``examples.yml``),``"kb"`` 与谁都对不上:删除空转,旧文档原地
留下,``rebuild`` 永远修不好。

后果与"从不重建"同型:从 KB 移除的条目在检索库里阴魂不散。它映射不回
``kb_items``(``_recall`` 会跳过),但召回名额是有限的 —— 死文档占的位就是
真文档失去的位。而 ``_recall`` 只在结果**为空**时才回退 builtin,库非空时
这条陈旧路径不会自曝。
"""

import pathlib
import sqlite3

import aiosqlite
import pytest

from trove.services.kb.embeddings import embed as det_embed
from trove.services.kb.service import KbService
from trove.services.retrieval.indexer import Indexer
from trove.services.retrieval.sqlite_store import SqliteHybridStore

EXAMPLES = """
examples:
  - question: 各地区的客户数量
    sql: SELECT COUNT(*) FROM client
    tags: [客户]
  - question: 每个地区的平均贷款金额
    sql: SELECT AVG(amount) FROM loan
    tags: [贷款]
"""

_REMOVED = """  - question: 每个地区的平均贷款金额
    sql: SELECT AVG(amount) FROM loan
    tags: [贷款]
"""


class FakeEmbed:
    """确定性 hashed n-gram embedder(零网络)。"""

    async def embed(self, texts):
        return [det_embed(t) for t in texts]

    async def embed_hybrid(self, texts):
        return [(det_embed(t), None) for t in texts]


@pytest.fixture
async def indexed(tmp_path):
    """一份已索引的 KB:两个 example 都在检索库里。"""
    kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb")
    d = kb.kb_dir / "demo"
    d.mkdir(parents=True)
    (d / "examples.yml").write_text(EXAMPLES, encoding="utf-8")
    await kb.ensure_synced("demo")

    store = SqliteHybridStore(tmp_path / "r.sqlite", FakeEmbed(), None)
    indexer = Indexer(store, kb, None, tmp_path / "home")
    await indexer.index_kb("demo")
    return kb, store, indexer, d


async def _doc_ids(store) -> list[str]:
    """独立连接读检索库(表未建 = 从未索引 → 空)。"""
    try:
        async with aiosqlite.connect(store._db) as db:
            cur = await db.execute("SELECT doc_id FROM documents ORDER BY doc_id")
            return [r[0] for r in await cur.fetchall()]
    except sqlite3.OperationalError:
        return []


async def test_rebuild_purges_docs_whose_items_are_gone(indexed):
    """条目已从 KB 移除 → rebuild 之后它的文档必须消失。"""
    kb, store, indexer, d = indexed
    assert len(await _doc_ids(store)) == 2

    (d / "examples.yml").write_text(
        EXAMPLES.replace(_REMOVED, ""), encoding="utf-8")
    await kb.ensure_synced("demo")

    await indexer.index_kb("demo", rebuild=True)

    ids = await _doc_ids(store)
    assert ids == ["各地区的客户数量"]
    assert "每个地区的平均贷款金额" not in ids


async def test_rebuild_keeps_current_items(indexed):
    """重建不得误删仍在 KB 里的条目。"""
    _kb, store, indexer, _d = indexed

    n = await indexer.index_kb("demo", rebuild=True)

    assert n == 2
    assert sorted(await _doc_ids(store)) == ["各地区的客户数量", "每个地区的平均贷款金额"]


async def test_incremental_index_does_not_purge(indexed):
    """非 rebuild 是增量:只 upsert,不清理(清理由写入钩子按文件负责)。"""
    kb, store, indexer, d = indexed

    (d / "examples.yml").write_text(
        EXAMPLES.replace(_REMOVED, ""), encoding="utf-8")
    await kb.ensure_synced("demo")

    await indexer.index_kb("demo")      # 无 rebuild

    assert len(await _doc_ids(store)) == 2
