"""RRF 权重 / k 网格调优:在 gold 评测集上搜索最优融合参数(零 LLM 成本)。

用法:
  uv run python scripts/tune_rrf.py --gold evalsets/retrieval.jsonl \
      --datasource demo [--k 10]
  uv run python scripts/tune_rrf.py --gold evalsets/retrieval.jsonl \
      --datasource demo --axis authority --alphas 0,0.05,0.1,0.2

按 ``keyword / dense`` 两路权重与 RRF k 做小网格搜索(精排关闭,隔离融合
本身的质量),以 MRR@k 为主目标、Recall@k 为辅,输出最优参数 JSON,可直接
写回 ``datasources.yml`` 的 ``rrf_weights`` / ``rrf_k`` 字段。网格偏小以保证
秒级完成;生产可按结果在最优邻域再加密一轮。

``--axis authority`` 是**权威分 α 的单轴消融**(默认开的回归门):固定当前
权重/k,只扫 α(0 = 关)。α 只在相邻名次差距(1/N)以内起作用,小评测集上
无 certified/deprecated 资产时各档应当同分 —— 那正是"默认开无回归"的证据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import itertools
import types
from pathlib import Path

from trove.services.retrieval.factory import build_store
from trove.services.retrieval.metrics import evaluate


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gold", required=True, help="jsonl: {query, doc_ids}[]")
    p.add_argument("--datasource", default="",
                   help="datasource name(默认 = 配置的默认源)")
    p.add_argument("--k", type=int, default=10, help="主目标截断点")
    p.add_argument("--limit", type=int, default=0, help="0 = 全部")
    p.add_argument("--scorecard", default="",
                   help="把最优指标写为 scorecard json(供 eval_gate 对比)")
    p.add_argument("--axis", choices=("rrf", "authority"), default="rrf",
                   help="rrf = 权重/k 网格(默认);authority = α 单轴消融")
    p.add_argument("--alphas", default="0,0.05,0.1,0.2",
                   help="--axis authority 的 α 网格(逗号分隔;0 = 关)")
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


def _metrics_dict(m: dict, k: int) -> dict:
    return {
        **{f"recall@{kk}": round(m["recall@k"][kk], 4) for kk in m["recall@k"]},
        **{f"ndcg@{kk}": round(m["ndcg@k"][kk], 4) for kk in m["ndcg@k"]},
        "mrr": round(m["mrr"], 4),
        "zero_recall": float(m["zero_recall"]),
    }


async def _authority_axis(
    args: argparse.Namespace, store, ds: str, gold: dict, top_k: int,
    rank_all,
) -> None:
    """权威分 α 单轴消融:默认开(0.1)的回归门 —— 与 α=0 逐指标对比。"""
    alphas = [float(x) for x in str(args.alphas).split(",") if x.strip()]
    rows: list[tuple[float, dict]] = []
    for a in alphas:
        store._authority_alpha = a
        ranked = await rank_all()
        rows.append((a, evaluate(gold, ranked, (top_k,))))

    print(f"queries: {len(gold)}  datasource: {ds}  axis: authority α "
          f"(weights={store._rrf_weights or 'equal'} k={store._rrf_k})")
    for a, m in rows:
        print(f"  α={a:<6} mrr@{top_k}={m['mrr']:.3f} "
              f"recall@{top_k}={m['recall@k'][top_k]:.0%} "
              f"ndcg@{top_k}={m['ndcg@k'][top_k]:.3f}")
    best_a, best_m = max(rows, key=lambda r: r[1]["mrr"])
    print(f"best α = {best_a}(mrr@{top_k}={best_m['mrr']:.3f})")

    if args.scorecard:
        scorecard = {
            "source": "tune_rrf_authority",
            "datasource": ds,
            "queries": len(gold),
            "k": top_k,
            "rrf_authority_alpha": best_a,
            "metrics": _metrics_dict(best_m, top_k),
            # 全轴留档:回归门对比的是"当前配置"那档,轴表供人看形状
            "axis": [{"alpha": a, **_metrics_dict(m, top_k)} for a, m in rows],
        }
        Path(args.scorecard).write_text(
            json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nscorecard → {args.scorecard}", flush=True)


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


async def main() -> None:
    args = parse_args()
    gold = _load_gold(args.gold)
    if args.limit:
        gold = dict(list(gold.items())[: args.limit])
    store, ds = await _build_store(args)
    top_k = args.k
    names = ("keyword", "dense")

    # 网格:keyword 主导词面、dense 主导语义,两路互相制衡。
    grid = {
        "keyword": (0.5, 1.0, 1.5),
        "dense": (0.7, 1.0, 1.5),
    }
    ks = (40, 60, 100)

    async def _rank_all() -> dict[str, list[str]]:
        store._reranker = None  # 隔离融合质量,精排留给 eval_hybrid_retrieval
        ranked = {}
        for q in gold:
            hits = await store.recall(q, k=top_k, rerank_k=top_k * 4, datasource=ds)
            ranked[q] = [h.doc_id for h in hits]
        return ranked

    async def _rank(weights: dict[str, float], rrf_k: int) -> dict[str, list[str]]:
        store._rrf_weights = weights
        store._rrf_k = rrf_k
        return await _rank_all()

    if args.axis == "authority":
        await _authority_axis(args, store, ds, gold, top_k, _rank_all)
        return

    best = None
    best_score = -1.0
    results = []
    for combo in itertools.product(*(grid[n] for n in names)):
        weights = dict(zip(names, combo))
        for k in ks:
            ranked = await _rank(weights, k)
            m = evaluate(gold, ranked, (top_k,))
            score = m["mrr"]
            results.append((score, weights, k, m))
            if score > best_score:
                best_score = score
                best = (weights, k, m)

    results.sort(key=lambda r: r[0], reverse=True)
    weights, rrf_k, m = best
    print(f"queries: {len(gold)}  datasource: {ds}  channels: {names}  "
          f"grid: {len(results)} combos")
    print(f"best  mrr@{top_k} = {m['mrr']:.3f}  "
          f"recall@{top_k} = {m['recall@k'][top_k]:.0%}")
    print("config to write into datasources.yml:")
    print(json.dumps({"rrf_weights": weights, "rrf_k": rrf_k}, ensure_ascii=False, indent=2))
    print("\ntop-5:")
    for score, w, k, m in results[:5]:
        print(f"  mrr@{top_k}={score:.3f} recall={m['recall@k'][top_k]:.0%} "
              f"weights={w} k={k}")

    if args.scorecard:
        from trove.eval.gate import DEFAULT_TOLERANCE

        scorecard = {
            "source": "tune_rrf",
            "datasource": ds,
            "queries": len(gold),
            "k": top_k,
            "rrf_weights": weights,
            "rrf_k": rrf_k,
            "metrics": _metrics_dict(m, top_k),
        }
        Path(args.scorecard).write_text(
            json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nscorecard → {args.scorecard}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
