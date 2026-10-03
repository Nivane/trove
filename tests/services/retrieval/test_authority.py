"""检索权威分:治理状态派生 → 列存储 → 融合偏置(±α)。

三层各测一段:

1. ``authority_of`` 的值映射(纯函数,治理词汇表在 kb.governance);
2. store 的列往返 + ``score = 归一化 RRF + α·authority`` 的数学 —— 含
   **偏置上限**:不相关的认证条目(低 RRF 位)越不过相关的普通条目;
3. 近位次翻转行为:±α 只在**相邻名次**的差距(1/N)以内起作用,这正是
   设计意图(同分候选认证优先,盖不过一个名次以上的真实相关性差距);
   α=0 完全还原纯 RRF 序,且 ``meta["rrf_ids"]`` 始终是未偏置的基准面。
"""

import math

import aiosqlite
import pytest

from trove.services.kb.governance import CERTIFIED, DEPRECATED
from trove.services.kb.service import KbService
from trove.services.retrieval import RetrievalDoc, SqliteHybridStore
from trove.services.retrieval.authority import AUTHORITY_ALPHA, authority_of
from trove.services.retrieval.indexer import Indexer
from trove.services.retrieval.sqlite_store import (
    RETRIEVAL_MIGRATIONS,
    _pack,
)
from trove.storage.migrations import SQLITE, apply_migrations


class FakeEmbedder:
    dim = 16

    async def embed(self, texts):
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            for ch in t:
                vec[ord(ch) % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


# ── 1. 值映射(纯函数)──────────────────────────────────


def test_authority_of_maps_governance_status():
    assert authority_of("example", {"status": CERTIFIED}) == 1.0
    assert authority_of("template", {"status": CERTIFIED}) == 1.0
    assert authority_of("example", {"status": DEPRECATED}) == -1.0
    assert authority_of("template", {"status": DEPRECATED}) == -1.0
    assert authority_of("example", {"status": "draft"}) == 0.0
    assert authority_of("example", {}) == 0.0
    assert authority_of("example", {"status": None}) == 0.0


def test_authority_only_for_certifiable_kinds():
    """lesson 已有票数乘性加权(_rank_lessons),term/table 无认证层级 ——
    同一信号不在这里打第二遍分。"""
    assert authority_of("lesson", {"status": CERTIFIED}) == 0.0
    assert authority_of("term", {"status": CERTIFIED}) == 0.0
    assert authority_of("table", {"status": CERTIFIED}) == 0.0


# ── 2. 列往返 + α 数学 ──────────────────────────────────


async def test_authority_roundtrips_and_scales_rrf_score(tmp_path):
    store = SqliteHybridStore(tmp_path / "r.sqlite", FakeEmbedder(), None)
    await store.index_many([
        RetrievalDoc(content="贷款 平均 金额", datasource="ds", kind="kb",
                     source_file="a.yml", item_key="plain"),
        RetrievalDoc(content="足球 比赛 直播", datasource="ds", kind="kb",
                     source_file="a.yml", item_key="cert", authority=1.0),
    ])
    hits, meta = await store.recall(
        "贷款 平均 金额", k=5, datasource="ds", return_meta=True)

    by_id = {h.doc_id: h for h in hits}
    # 列往返:authority 随行带回(装载路径读的是一行 REAL 列)
    assert by_id["cert"].authority == 1.0
    assert by_id["plain"].authority == 0.0
    # 数学:score = 排序位 RRF(rank1=1.0, rank2=0.5)+ α·authority
    assert by_id["plain"].score == pytest.approx(1.0)
    assert by_id["cert"].score == pytest.approx(0.5 + AUTHORITY_ALPHA)
    # 偏置上限:不相关的认证条目越不过相关的普通条目
    assert hits[0].doc_id == "plain"
    # rrf_ids 是纯 RRF 基准面,不含偏置
    assert meta["rrf_ids"][:2] == ["plain", "cert"]


# ── 3. 近位次翻转 + 关开关 ──────────────────────────────


async def test_authority_flips_near_rank_margins_and_zero_disables(tmp_path):
    """±α 只在**相邻名次**的差距(1/N)以内起作用 —— 这正是设计意图:
    相关性打平的地方认证资产优先,盖不过一个名次以上的真实差距。

    12 条同文本候选(查询串无词法命中 → 候选全由稠密通道给出;同向量并列
    由稳定排序按插入序展开),第 10 条 deprecated、第 11 条 certified:
    certified(2/12 + 0.1)越过 deprecated(3/12 − 0.1),两者都压在尾部普通
    条目(1/12)之上 —— 一升一沉各恰好一位;α=0 时完全还原 RRF 序。
    """
    store = SqliteHybridStore(tmp_path / "r.sqlite", FakeEmbedder(), None)
    docs = [
        RetrievalDoc(content="并列 候选 文本", datasource="ds", kind="kb",
                     source_file="a.yml", item_key=f"p{i}")
        for i in range(9)
    ]
    docs.append(RetrievalDoc(
        content="并列 候选 文本", datasource="ds", kind="kb",
        source_file="a.yml", item_key="dep", authority=-1.0))
    docs.append(RetrievalDoc(
        content="并列 候选 文本", datasource="ds", kind="kb",
        source_file="a.yml", item_key="cert", authority=1.0))
    docs.append(RetrievalDoc(
        content="并列 候选 文本", datasource="ds", kind="kb",
        source_file="a.yml", item_key="tail"))
    await store.index_many(docs)

    hits, meta = await store.recall(
        "zzzz", k=12, datasource="ds", return_meta=True)
    n = len(hits)
    assert n == 12
    ids = [h.doc_id for h in hits]
    by_id = {h.doc_id: h for h in hits}
    assert ids.index("cert") == 9    # 第 11 位 → 升到第 10 位
    assert ids.index("dep") == 10    # 第 10 位 → 沉到第 11 位(仍在尾部之上)
    assert ids.index("tail") == 11
    assert by_id["cert"].score == pytest.approx(2 / n + AUTHORITY_ALPHA)
    assert by_id["dep"].score == pytest.approx(3 / n - AUTHORITY_ALPHA)
    # 基准面不动:rrf_ids 仍是纯 RRF 序(插入序)
    assert meta["rrf_ids"] == [f"p{i}" for i in range(9)] + ["dep", "cert", "tail"]

    # α=0 → 完全还原
    store._authority_alpha = 0.0
    hits0, meta0 = await store.recall(
        "zzzz", k=12, datasource="ds", return_meta=True)
    assert [h.doc_id for h in hits0] == meta0["rrf_ids"]
    assert hits0[9].doc_id == "dep"
    assert all(h.score <= 1.0 for h in hits0)


# ── 4. 存量库迁移 + 写入路径派生 ────────────────────────


async def test_legacy_v2_db_adopts_authority_column(tmp_path):
    """存量 v2 检索库(无 authority 列)→ 打开即迁移到 v3,旧行读作 0.0。"""
    db_path = tmp_path / "legacy.sqlite"
    async with aiosqlite.connect(db_path) as db:
        await apply_migrations(
            db, "retrieval", RETRIEVAL_MIGRATIONS[:2], dialect=SQLITE)
        emb = (await FakeEmbedder().embed(["贷款 金额"]))[0]
        cur = await db.execute(
            "INSERT INTO documents "
            "(doc_id, datasource, kind, source_file, content, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("e1", "ds", "kb", "a.yml", "贷款 金额", _pack(emb)))
        await db.execute(
            "INSERT INTO doc_fts (rowid, content) VALUES (?, ?)",
            (cur.lastrowid, "贷款 金额"))
        await db.commit()

    store = SqliteHybridStore(db_path, FakeEmbedder(), None)
    hits = await store.recall("贷款 金额", k=5, datasource="ds")
    assert [h.doc_id for h in hits] == ["e1"]
    assert hits[0].authority == 0.0
    assert hits[0].score == pytest.approx(1.0)

    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT version FROM trove_schema WHERE store = ?", ("retrieval",))
        assert (await cur.fetchone())[0] == RETRIEVAL_MIGRATIONS[-1].version


_GOV_EXAMPLES = """
examples:
  - question: 认证问法
    sql: SELECT COUNT(*) FROM client
    governance:
      status: certified
      approved_by: alice
      approved_at: 2026-09-01
  - question: 普通问法
    sql: SELECT COUNT(*) FROM loan
  - question: 退役问法
    sql: SELECT COUNT(*) FROM loan
    governance:
      status: deprecated
"""


async def test_indexer_derives_authority_from_governance(tmp_path):
    """全量索引路径:authority 由 payload 里的治理字段确定性派生
    (certified 需 approved_by + approved_at,governance_of 把关)。"""
    kb = KbService(tmp_path / "proj", kb_dir=tmp_path / "kb")
    d = kb.kb_dir / "demo"
    d.mkdir(parents=True)
    (d / "examples.yml").write_text(_GOV_EXAMPLES, encoding="utf-8")
    await kb.ensure_synced("demo")

    store = SqliteHybridStore(tmp_path / "r.sqlite", FakeEmbedder(), None)
    indexer = Indexer(store, kb, None, tmp_path / "home")
    await indexer.index_kb("demo")

    async with aiosqlite.connect(store._db) as db:
        cur = await db.execute(
            "SELECT doc_id, authority FROM documents ORDER BY doc_id")
        got = dict(await cur.fetchall())
    assert got == {"认证问法": 1.0, "普通问法": 0.0, "退役问法": -1.0}
