"""BIRD financial 检索评测:表级 schema 召回 + 分路消融。

用法:
  uv run python scripts/eval_bird_retrieval.py \
      --bird ~/Downloads/minidev/MINIDEV/mini_dev_mysql.json \
      --datasource mysql_fin [--limit N] [--rerank]

**金标从哪来**:每道 BIRD 题的 gold SQL 里反引号引用的表名,限定在该数据源的
schema_notes 表集合内。金标文档 = ``kind='table'`` 的 KB item(item_key = 表名,
即 schema_notes.yml 的表级说明,含 description + 逐列描述 + 枚举)。

**这测的是什么**:给定一句真实的业务提问(不是 KB 里的模板问法),混合检索能否把
**正确表的 schema 说明**召回进 top-k —— 即 schema linking 的召回环节。

**为什么要做分路消融**:``eval_hybrid_retrieval.py`` 只对比精排前后;回答「为什么
要混合而不是单路」需要**同金标、同查询集**下对比 keyword-only / dense-only / RRF。
这里直接调用 store 的通道原语再自行融合,而不是用 ``rrf_weights=0`` 关通道——
后者仍会在 ``rrf_fuse`` 里为 0 权重通道建立 score=0 的条目,污染排序。

learned-sparse 第三路已退役(与 dense 同属一个 backbone 的 head,不构成独立
信号),消融因此只剩 keyword / dense 两路。

零 LLM 成本、零网络(embedder 走本地 bge-m3)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import yaml

from trove.core.types import DatasourceConfig
from trove.services.kb.service import KbService
from trove.services.retrieval.factory import build_store
from trove.services.retrieval.indexer import Indexer
from trove.services.retrieval.metrics import evaluate
from trove.services.retrieval.store import normalize_scores, rrf_scores


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bird", required=True, help="BIRD mini_dev_mysql.json 路径")
    p.add_argument("--datasource", default="mysql_fin")
    p.add_argument("--home", default=".trove", help="trove home(默认 .trove)")
    p.add_argument("--db-id", default="financial", help="BIRD db_id")
    p.add_argument("--limit", type=int, default=0, help="0 = 全部")
    p.add_argument("--k", default="1,3,5,10")
    p.add_argument("--rerank", action="store_true",
                   help="额外跑一遍带精排(默认只跑 RRF 序,快)")
    return p.parse_args()


def known_tables(kb_dir: Path) -> set[str]:
    """从 schema_notes.yml 读该数据源声明的表名集合。"""
    path = kb_dir / "schema_notes.yml"
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(t["name"]) for t in data.get("tables", []) if t.get("name")}


_TABLE_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def gold_from_sql(sql: str, known: set[str]) -> list[str]:
    """gold SQL → 引用的表名(限定在 known 内)。

    BIRD MySQL 方言里表名和列名都用反引号(`` `T2`.`account_id` ``),所以只
    靠反引号不够——必须用 known 表集合过滤掉列名与别名。
    """
    hit = {t for t in _TABLE_RE.findall(sql or "") if t.lower() in {k.lower() for k in known}}
    # 保序:按 known 里声明的顺序,输出确定性
    return [t for t in known if t in hit]


def load_bird(path: str, db_id: str, known: set[str], limit: int) -> dict[str, list[str]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    gold: dict[str, list[str]] = {}
    n_skipped = 0
    for item in raw:
        if item.get("db_id") != db_id:
            continue
        q = str(item.get("question") or "").strip()
        tables = gold_from_sql(str(item.get("SQL") or ""), known)
        if not q or not tables:
            n_skipped += 1
            continue
        # 同一问题文本可能重复(不同 var 变体)→ 合并金标
        gold.setdefault(q, [])
        for t in tables:
            if t not in gold[q]:
                gold[q].append(t)
    if limit:
        gold = dict(list(gold.items())[:limit])
    if n_skipped:
        print(f"[gold] 跳过 {n_skipped} 条(无问题文本或解析不出表名)")
    return gold


async def main() -> None:
    args = parse_args()
    ks = tuple(int(x) for x in args.k.split(",") if x.strip())
    home = Path(args.home).resolve()
    kb_dir = home / "kb" / args.datasource

    known = known_tables(kb_dir)
    if not known:
        raise SystemExit(f"读不到表清单:{kb_dir / 'schema_notes.yml'}")
    print(f"[corpus] {args.datasource} 声明表 {len(known)} 张: {sorted(known)}")

    gold = load_bird(args.bird, args.db_id, known, args.limit)
    print(f"[gold] BIRD {args.db_id} 题数 {len(gold)}")

    cfg = DatasourceConfig(
        name=args.datasource,
        type="mysql",
        embedder_backend="bge-m3",
        embedding_dims=1024,
        fts_tokenizer="en_stem",
        retrieval_dsn="",          # 空 → SqliteHybridStore(无需起 PG)
        rerank_backend="deterministic",
    )
    store = build_store(cfg, None, home)
    print(f"[store] {type(store).__name__}")

    kb = KbService(Path.cwd(), kb_dir=kb_dir)
    await kb.ensure_synced(default_datasource=args.datasource)
    indexer = Indexer(store, kb, None, home)
    n = await indexer.index_kb(args.datasource)
    print(f"[index] 索引 {n} 条 KB item")

    store._ds = args.datasource
    top_k = max(ks)
    rerank_k = top_k * 4

    async def ranked_for(query: str, channels: tuple[str, ...]) -> list[str]:
        """复刻 HybridStore.recall 的通道逻辑,但可挑选通道子集。"""
        kw = query.strip() or query
        vector = await store._embed(query)
        lists: list[list[str]] = []
        if "keyword" in channels:
            lists.append(await store._fts_ids(kw, rerank_k))
        if "dense" in channels:
            lists.append(await store._ann_ids(vector, rerank_k))
        scores = rrf_scores(lists, k=store._rrf_k)
        fused = sorted(scores, key=lambda d: scores[d], reverse=True)
        hits = await store._load(fused, normalize_scores(scores))
        if args.rerank and store._reranker is not None and hits:
            hits = await store._reranker.rerank(query, hits, rerank_k)
        return [h.doc_id for h in hits[:top_k]]

    async def run(name: str, channels: tuple[str, ...]) -> dict:
        ranked = {}
        for q in gold:
            ranked[q] = await ranked_for(q, channels)
        m = evaluate(gold, ranked, ks)
        rec = " ".join(f"{m['recall@k'][k]:.0%}" for k in ks)
        ndcg = " ".join(f"{m['ndcg@k'][k]:.2f}" for k in ks)
        print(f"  {name:<22} recall@{'/'.join(map(str, ks))} = {rec}   "
              f"mrr = {m['mrr']:.3f}   ndcg = {ndcg}   "
              f"zero_recall = {m['zero_recall']}/{m['n']}")
        return m

    print(f"\n=== 分路消融(N={len(gold)} 题,top_k={top_k},rerank_k={rerank_k}) ===")
    await run("keyword-only", ("keyword",))
    await run("dense-only", ("dense",))
    await run("keyword+dense", ("keyword", "dense"))
    await run("双路 RRF(生产)", ("keyword", "dense"))

    if args.rerank:
        print("\n=== 精排前后(生产三路) ===")
        await run("双路 RRF(精排前)", ("keyword", "dense"))


if __name__ == "__main__":
    asyncio.run(main())
