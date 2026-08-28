"""离线 KB 级 typed 检索探针:metrics/entities 自召回评估。零 LLM 成本。

用法:
  uv run python scripts/probe_typed_retrieval.py [--kb-dir DIR] [--db-id demo]

对 KB 里每个 metric / entity(带同义词/枚举)用其名称/别名/枚举标签构造探针
问题,检查 typed 检索能否在 top-k 召回自身——自一致性召回(检索应能找到
自己声明过的语义),是索引层/门/打分的离线回归信号。不跑计费 eval_bird。

输出按 kind 汇总 recall@1/3/5,以及无成本基线快照(供后续对比)。
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from trove.services.kb.service import KbService

TOP_K = (1, 3, 5)


def _recall_at(rank: int, hits: list, target: str, key: str) -> bool:
    for h in hits[:rank]:
        if str(getattr(h, key, "")) == target:
            return True
    return False


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-dir", default=None,
                        help="KB 根目录;默认 <cwd>/.trove/kb")
    parser.add_argument("--db-id", default="demo", help="要评估的数据源")
    args = parser.parse_args()

    kb = KbService(Path.cwd(), kb_dir=args.kb_dir)
    await kb.ensure_synced(args.db_id)
    items = (await kb.list_items()).get(args.db_id, {})
    print(f"kb={args.kb_dir or '<cwd>/.trove/kb'} db={args.db_id} "
          f"kinds={items}")

    # ── metric 自召回:名称 + 每个别名作为探针 ────────────────
    m_total = m_hits = 0
    m_recall = {k: 0 for k in TOP_K}
    rows = await kb._rows(
        "SELECT payload FROM kb_items WHERE kind='metric' AND datasource=?",
        (args.db_id,))
    for row in rows:
        p = __import__("json").loads(row["payload"])
        name = str(p.get("name") or "")
        if not name:
            continue
        for probe in [name, *[str(a) for a in p.get("aliases", [])]]:
            probe = probe.strip()
            if not probe:
                continue
            m_total += 1
            hits = await kb.search_metrics(probe, args.db_id, limit=5)
            best = next((k for k in TOP_K if _recall_at(k, hits, name, "name")), None)
            if best:
                m_hits += 1
                for k in TOP_K:
                    if k >= best:
                        m_recall[k] += 1

    # ── entity 自召回:字段名 / 同义词 / 枚举标签作为探针 ─────
    e_total = e_hits = 0
    e_recall = {k: 0 for k in TOP_K}
    rows = await kb._rows(
        "SELECT payload FROM kb_items WHERE kind='entity' AND datasource=?",
        (args.db_id,))
    for row in rows:
        p = __import__("json").loads(row["payload"])
        field = str(p.get("field") or "")
        if not field:
            continue
        probes = [field, *[str(s) for s in p.get("synonyms", [])],
                  *[str(l) for l in p.get("enum_labels", [])]]
        for probe in [x.strip() for x in probes if x and x.strip()]:
            e_total += 1
            hits = await kb.search_entities(probe, args.db_id, limit=5)
            best = next((k for k in TOP_K if _recall_at(k, hits, field, "field")), None)
            if best:
                e_hits += 1
                for k in TOP_K:
                    if k >= best:
                        e_recall[k] += 1

    def fmt(rec: dict, total: int) -> str:
        if total == 0:
            return "n=0"
        return "n=%d recall@1/3/5 = %s" % (
            total,
            " ".join(f"{rec[k] / total:.0%}" for k in TOP_K),
        )

    print("metrics:", fmt(m_recall, m_total))
    print("entities:", fmt(e_recall, e_total))


if __name__ == "__main__":
    asyncio.run(main())
