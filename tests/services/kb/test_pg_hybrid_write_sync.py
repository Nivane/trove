"""KB 写入 → 统一检索库(默认后端 pg_hybrid)的索引同步。

`KbService` 的向量同步走鸭子类型钩子(``_index_vectors_for_file`` /
``_delete_vectors`` / ``_clear_vectors``):后端有 ``index_file`` 就调,没有就
静默跳过。``rag`` 实现了三个,``pg_hybrid``(**默认生产后端**)一个都没有 ——
于是 KB 写入对统一检索库永久不可见。

危害不是"检索少几条",而是**静默陈旧**:``pg_hybrid._recall`` 只在结果为空时
回退 builtin,库一旦被索引过就永远返回写入**之前**的文档,那份旧文档恰好把
fresh 的 builtin 兜底压住。用户确认了新例子 / 改了口径,检索仍拿旧的喂模型,
而且没有任何信号说明它旧了 —— 比空结果更糟。删除同理:文件没了,文档还在。

本文件把三条契约固化成断言:写入落库、重写刷新、删除传播。
"""

import sqlite3

import aiosqlite
import pytest

from trove.services.kb.backends.pg_hybrid import PgHybridKbBackend
from trove.services.kb.embeddings import embed as det_embed
from trove.services.kb.service import KbService
from trove.services.retrieval.sqlite_store import SqliteHybridStore

SEMANTICS = """
semantic_model:
  - name: financial
    datasets:
      - name: loan
    metrics:
      - name: 平均贷款金额
        expression:
          dialects:
            - dialect: ANSI_SQL
              expression: AVG(loan.amount)
"""

EXAMPLES = """
examples:
  - question: 各地区的客户数量
    sql: SELECT d.A2, COUNT(c.client_id) FROM client c JOIN district d GROUP BY d.A2
    tags: [地区, 客户, 分组]
  - question: 每个地区的平均贷款金额
    sql: SELECT d.A2, AVG(l.amount) FROM loan l JOIN district d GROUP BY d.A2
    tags: [地区, 贷款, 聚合]
"""

LESSONS = """
lessons:
  - pattern: "no such table: loans"
    note: 表名是 loan 不是 loans
    sql_snippet: SELECT * FROM loan
    confirmed: true
"""


class FakeEmbed:
    """确定性 hashed n-gram embedder(零网络,与生产 embedding 路径解耦)。"""

    async def embed(self, texts):
        return [det_embed(t) for t in texts]


def _write_kb(kb_dir, ds="financial"):
    d = kb_dir / ds
    d.mkdir(parents=True, exist_ok=True)
    (d / "semantics.yml").write_text(SEMANTICS, encoding="utf-8")
    (d / "examples.yml").write_text(EXAMPLES, encoding="utf-8")
    (d / "lessons.yml").write_text(LESSONS, encoding="utf-8")
    return d


async def _hybrid_kb(tmp_path, ds="financial"):
    """KbService + **默认生产后端** pg_hybrid(真 SqliteHybridStore,假 embedder)。

    统一检索库与业务库同栈(生产是 PG,测试用 SQLite 兜底实现同一协议),
    所以这里是仓储级真跑,而不是替身。
    """
    store = SqliteHybridStore(tmp_path / "retrieval.sqlite", FakeEmbed(), None)
    holder: dict = {}
    kb = KbService(
        tmp_path / "proj", kb_dir=tmp_path / "kb",
        backend_resolver=lambda _ds: PgHybridKbBackend(
            holder["kb"], store, embedder=FakeEmbed()),
    )
    holder["kb"] = kb
    _write_kb(kb.kb_dir, ds)
    await kb.ensure_synced(ds)
    return kb, store


def _has(content: str, needle: str) -> bool:
    """文档文本是否含该串(FTS 分词把 CJK 逐字加空格,比对前先去空白)。"""
    return needle in content.replace(" ", "")


async def _store_docs(store, ds="financial") -> list[dict]:
    """用一个**独立连接**读检索库:只有真正落库的文档才可见。"""
    try:
        async with aiosqlite.connect(store._db) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT doc_id, kind, source_file, content FROM documents "
                "WHERE datasource = ? ORDER BY doc_id",
                (ds,),
            )
            return [dict(r) for r in await cur.fetchall()]
    except sqlite3.OperationalError:
        return []   # 表都还没建 —— 从未被索引过


async def test_kb_write_indexes_into_hybrid_store(tmp_path):
    """KB 同步必须把条目落进统一检索库(否则该库对写入永久失明)。"""
    _kb, store = await _hybrid_kb(tmp_path)

    docs = await _store_docs(store)
    assert {d["source_file"] for d in docs} == {
        "semantics.yml", "examples.yml", "lessons.yml"}
    # doc_id == kb_items.item_key:pg_hybrid._recall 靠这条映射回 payload
    assert any(_has(d["content"], "客户数量") for d in docs)


async def test_rewrite_refreshes_store_content(tmp_path):
    """改写 YAML → 检索库内容随之刷新,旧文本不得残留。

    这条是"静默陈旧"的直接断言:库里有旧文档时 _recall 不会回退 builtin,
    旧文本会一直喂给模型。
    """
    kb, store = await _hybrid_kb(tmp_path)

    (kb.kb_dir / "financial" / "examples.yml").write_text(
        EXAMPLES.replace("客户数量", "客户总数"), encoding="utf-8")
    await kb.ensure_synced("financial")

    docs = await _store_docs(store)
    texts = [d["content"] for d in docs]
    assert any(_has(t, "客户总数") for t in texts)
    assert not any(_has(t, "客户数量") for t in texts)


async def test_deleted_file_leaves_no_stale_docs(tmp_path):
    """文件从磁盘消失 → 该文件的文档一并清掉,其余文件的留在库里。"""
    kb, store = await _hybrid_kb(tmp_path)

    (kb.kb_dir / "financial" / "lessons.yml").unlink()
    await kb.ensure_synced("financial")

    docs = await _store_docs(store)
    assert {d["source_file"] for d in docs} == {"semantics.yml", "examples.yml"}


async def test_item_removed_from_a_surviving_file_disappears(tmp_path):
    """条目从 YAML 删掉但**文件还在** → 它的文档也不得留在库里。

    这条是 index_file「先删后写」的存在理由:若只按 item_key upsert,被删
    条目的文档会永远留在库里 —— 它映射不回 kb_items,只会白占召回位,把
    真正该召回的名额挤掉。
    """
    kb, store = await _hybrid_kb(tmp_path)
    before = await _store_docs(store)
    assert any(_has(d["content"], "每个地区的平均贷款金额") for d in before)

    (kb.kb_dir / "financial" / "examples.yml").write_text(
        EXAMPLES.replace("""  - question: 每个地区的平均贷款金额
    sql: SELECT d.A2, AVG(l.amount) FROM loan l JOIN district d GROUP BY d.A2
    tags: [地区, 贷款, 聚合]
""", ""), encoding="utf-8")
    await kb.ensure_synced("financial")

    after = await _store_docs(store)
    assert not any(_has(d["content"], "每个地区的平均贷款金额") for d in after)
    assert any(_has(d["content"], "客户数量") for d in after)


async def test_new_item_reaches_an_already_indexed_store(tmp_path):
    """库已索引过之后新增的 KB 条目必须可达 —— 静默陈旧的真正危害。

    库非空(旧条目在)时 ``_recall`` **不会**回退 builtin,所以新增条目若不
    落库就永远召不回来:用户确认的新例子/新 lesson 一次都进不了提示词,而
    检索看起来"正常工作"。这比库为空更隐蔽 —— 库为空时兜底还会接上。
    """
    kb, store = await _hybrid_kb(tmp_path)
    assert await _store_docs(store)          # 先确认库确实非空(兜底已失效)

    (kb.kb_dir / "financial" / "examples.yml").write_text(
        EXAMPLES + """  - question: district 表里有多少个地区
    sql: SELECT COUNT(*) FROM district
    tags: [地区]
""", encoding="utf-8")
    await kb.ensure_synced("financial")

    docs = await _store_docs(store)
    assert any(_has(d["content"], "有多少个地区") for d in docs)
    # doc_id 必须是 kb_items 的 item_key,否则 _recall 映射不回 payload
    keys = {r["item_key"] for r in await kb._rows(
        "SELECT item_key FROM kb_items WHERE datasource = 'financial'")}
    assert {d["doc_id"] for d in docs} <= keys


async def test_delete_kb_clears_store(tmp_path):
    """delete_kb 清空该数据源的全部文档。"""
    kb, store = await _hybrid_kb(tmp_path)
    assert await _store_docs(store)          # 先有东西可清,断言才不空转

    await kb.delete_kb("financial")

    assert await _store_docs(store) == []
