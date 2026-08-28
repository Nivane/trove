"""离线混合检索评估:Recall@k / MRR / nDCG@k,含精排前(RRF)后(rerank)对比。

用法:
  uv run python scripts/eval_hybrid_retrieval.py \
      --gold evalsets/retrieval.jsonl --datasource demo [--limit N] [--k 1,3,5,10]

gold 为 jsonl,每行 ``{"query": "...", "doc_ids": ["<store doc_id>", ...]}``
(doc_id = 索引时的 item_key,如 ``schema:financial:card`` / ``kb:...:ex1``)。
按数据源配置构建检索库(``build_store``):先以纯 RRF(临时禁精排)跑基线,
再跑完整(含精排)对比 —— 输出对齐 corvid ``evalsets/RESULTS.md`` 的口径,
回答「三路召回召回率够不够」「精排是否修正排序」两个问题,零 LLM 成本。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import types
from pathlib import Path

from trove.services.retrieval.factory import build_store
from trove.services.retrieval.metrics import evaluate


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gold", required=True, help="jsonl: {query, doc_ids}[]")
    p.add_argument("--datasource", default="",
                   help="datasource name(默认 = 配置的默认源)")
    p.add_argument("--limit", type=int, default=0, help="0 = 全部")
    p.add_argument("--k", default="1,3,5,10", help="评估截断点(逗号分隔)")
    return p.parse_args()


def _load_gold(path: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        out[obj["query"]] = list(obj.get("doc_ids") or [])
    return out


async def _build_store(args: argparse.Namespace):
    from trove.main import _load_config, create_app_components
    from trove.core.config import build_checkpointer

    cfg_args = types.SimpleNamespace(
        datasource=args.datasource or "", config=None, model=None)
    config = await _load_config(cfg_args)
    async with build_checkpointer(config.home) as checkpointer:
        components = await create_app_components(cfg_args, config, checkpointer)
    config_store = components["config_store"]
    registry = components["connector_registry"]
    ds = args.datasource or (registry.default_name or "default")
    cfg = next((c for c in config_store.load_configs() if c.name == ds), None)
    if cfg is None:
        raise SystemExit(f"datasource '{ds}' not configured (.trove/datasources.yml)")
    store = build_store(cfg, components["llm_gateway"], config.home)
    return store, ds


def _fmt(m: dict, ks) -> str:
    rec = " ".join(f"{m['recall@k'][k]:.0%}" for k in ks)
    ndcg = " ".join(f"{m['ndcg@k'][k]:.2f}" for k in ks)
    return (f"recall@{'/'.join(map(str, ks))} = {rec}   "
            f"mrr = {m['mrr']:.3f}   ndcg@{'/'.join(map(str, ks))} = {ndcg}   "
            f"zero_recall = {m['zero_recall']}/{m['n']}")


async def main() -> None:
    args = parse_args()
    ks = tuple(int(x) for x in args.k.split(",") if x.strip())
    gold = _load_gold(args.gold)
    if args.limit:
        gold = dict(list(gold.items())[: args.limit])
    store, ds = await _build_store(args)
    top_k = max(ks)
    rerank_k = top_k * 4

    async def _rank_all(reranker_on: bool) -> dict[str, list[str]]:
        orig = store._reranker
        if not reranker_on:
            store._reranker = None
        try:
            ranked = {}
            for q in gold:
                hits = await store.recall(
                    q, k=top_k, rerank_k=rerank_k, datasource=ds)
                ranked[q] = [h.doc_id for h in hits]
            return ranked
        finally:
            store._reranker = orig

    rrf_ranked = await _rank_all(reranker_on=False)
    rerank_ranked = await _rank_all(reranker_on=True)

    print(f"queries: {len(gold)}  datasource: {ds}  "
          f"reranker: {type(store._reranker).__name__}")
    print(f"  RRF(精排前): {_fmt(evaluate(gold, rrf_ranked, ks), ks)}")
    print(f"  rerank(精排后): {_fmt(evaluate(gold, rerank_ranked, ks), ks)}")
    for q, ranked in list(rerank_ranked.items())[:10]:
        print(f"    - {q[:40]!r} → {ranked[:5]}")


if __name__ == "__main__":
    asyncio.run(main())
