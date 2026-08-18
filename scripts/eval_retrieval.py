"""离线 KB 检索评估:召回率 + 子结构覆盖 + 预算挤占。零 LLM 成本。

用法:
  uv run python scripts/eval_retrieval.py [--dev-json PATH] [--kb-dir DIR]
      [--db-id db1,db2] [--limit N]

对每题:
  - gold SQL → 规范化指纹 + 结构特征(表集合/列集合,SQLGlot,别名/字面量清洗)
  - KbService.search_examples 检索 top-k:
      口径 A: 无锚定(tables=None,纯词法打分)
      口径 B: gold 表锚定(tables=gold 表集合,模拟 schema_linking 的 matched_tables)
  - 精确命中: gold 指纹出现在 top-k 中(KB 里有可抄的整题模板)
  - 结构命中: top-k 与 gold 的整题结构相似度 >= SIM_THRESHOLD
  - 子结构覆盖: 模板对组合题的贡献是拼接——top-k 示例并集能覆盖
    gold 表集合 / 列集合的最大比例(全表覆盖 = join 骨架可复用)
  - 预算: 命中示例的估计 token(4 chars/token,与 context_budget 同口径)

输出按 db_id 汇总;回答「检索召回差」「模板粒度不匹配」还是「预算装不下」
——不需要跑 LLM eval。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import defaultdict
from pathlib import Path

import sqlglot
from sqlglot import exp

from trove.services.kb.service import KbService

SIM_THRESHOLD = 0.5
CHAR_PER_TOKEN = 4
TOP_K = (1, 3, 5, 10)


def sql_fingerprint(sql: str) -> str:
    """规范化指纹:去反引号/空白,小写——精确/近似重复的判定口径。"""
    return re.sub(r"[`\s]+", "", (sql or "").lower())


def _clean(name: str) -> str | None:
    """清洗 SQLGlot 提取的名字:别名/字面量/空串 → None。"""
    name = name.lower()
    if len(name) <= 1 or name.isdigit():
        return None
    return name


def sql_features(sql: str) -> tuple[set[str], set[str]]:
    """结构特征:表集合 + 列集合(SQLGlot,别名/字面量清洗)。解析失败→空集。"""
    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:
        return set(), set()
    tables = {n for n in {_clean(t.name) for t in ast.walk(exp.Table)} if n}
    cols = {n for n in {_clean(c.name) for c in ast.walk(exp.Column)} if n}
    return tables, cols


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def sql_similarity(gold_sql: str, ex_sql: str) -> float:
    """结构相似度:表集合 Jaccard 与列集合 Jaccard 各半。"""
    gt, gc = sql_features(gold_sql)
    et, ec = sql_features(ex_sql)
    return 0.5 * jaccard(gt, et) + 0.5 * jaccard(gc, ec)


def estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // CHAR_PER_TOKEN)


def example_tokens(hit) -> int:
    payload = hit if isinstance(hit, dict) else hit.model_dump() if hasattr(hit, "model_dump") else hit.__dict__
    text = " ".join([
        str(payload.get("question", "")),
        str(payload.get("sql", "")),
        *[str(t) for t in payload.get("tags", [])],
    ])
    return estimate_tokens(text)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dev-json",
        default="/Users/zhaolipan/Downloads/minidev/MINIDEV/mini_dev_mysql.json",
    )
    parser.add_argument("--kb-dir", default=None,
                        help="KB 根目录(含 <db_id>/ 子目录)或扁平 YAML 目录;默认 <cwd>/.trove/kb")
    parser.add_argument("--db-id", default=None,
                        help="只评指定数据源(逗号分隔);默认全部(无 KB 的 db 自然为空)")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    dev = json.loads(Path(args.dev_json).read_text(encoding="utf-8"))
    selected = set(args.db_id.split(",")) if args.db_id else None
    questions = [q for q in dev if not selected or q["db_id"] in selected]
    print(f"questions: {len(questions)}  (dev={Path(args.dev_json).name}, kb={args.kb_dir or '<cwd>/.trove/kb'})")

    kb = KbService(Path.cwd(), kb_dir=args.kb_dir)
    await kb.ensure_synced()

    # per-db: {anchor: {exact@k, sim@k, cover_tables@k, cover_cols@k, tokens}}
    stats = defaultdict(lambda: {
        "n": 0,
        "A": {"exact": defaultdict(int), "sim": defaultdict(int),
              "ct": defaultdict(list), "cc": defaultdict(list), "tokens": []},
        "B": {"exact": defaultdict(int), "sim": defaultdict(int),
              "ct": defaultdict(list), "cc": defaultdict(list), "tokens": []},
    })

    def union_coverage(top: list, gold_set: set[str]) -> float:
        """top-k 示例并集对 gold 集合的最大覆盖比例(空 gold → 1.0)。"""
        if not gold_set:
            return 1.0
        if not top:
            return 0.0
        covered = set()
        for h in top:
            et, ec = sql_features(h.sql)
            covered |= et | ec
        return len(covered & gold_set) / len(gold_set)

    for q in questions:
        db = q["db_id"]
        gold = q.get("SQL") or ""
        gp = sql_fingerprint(gold)
        gold_tables, gold_cols = sql_features(gold)
        s = stats[db]
        s["n"] += 1

        for anchor_name, anchor in (("A", None), ("B", gold_tables or None)):
            hits = await kb.search_examples(
                q["question"], db, limit=10, tables=anchor,
            )
            for k in TOP_K:
                top = hits[:k]
                if top and any(sql_fingerprint(h.sql) == gp for h in top):
                    s[anchor_name]["exact"][k] += 1
                if top and any(sql_similarity(gold, h.sql) >= SIM_THRESHOLD for h in top):
                    s[anchor_name]["sim"][k] += 1
                s[anchor_name]["ct"][k].append(union_coverage(top, gold_tables))
                s[anchor_name]["cc"][k].append(union_coverage(top, gold_cols))
            if hits:
                s[anchor_name]["tokens"].append(min(example_tokens(h) for h in hits[:3]))

    for db, s in sorted(stats.items()):
        n = s["n"]
        if n == 0:
            continue
        print(f"\n== {db} (n={n}) ==")
        for anchor_name, label in (("A", "A 无锚定"), ("B", "B gold表锚定")):
            exact = {k: s[anchor_name]["exact"][k] / n for k in TOP_K}
            sim = {k: s[anchor_name]["sim"][k] / n for k in TOP_K}
            ct = {k: sum(v) / len(v) if v else 0.0
                  for k, v in s[anchor_name]["ct"].items()}
            cc = {k: sum(v) / len(v) if v else 0.0
                  for k, v in s[anchor_name]["cc"].items()}
            print(f"  {label} exact@1/3/5/10 = "
                  + " ".join(f"{exact[k]:.0%}" for k in TOP_K))
            print(f"  {label} sim>=.5@1/3/5/10 = "
                  + " ".join(f"{sim[k]:.0%}" for k in TOP_K))
            print(f"  {label} 表覆盖@3/5/10 = "
                  + " ".join(f"{ct[k]:.0%}" for k in (3, 5, 10))
                  + f"   列覆盖@3/5/10 = "
                  + " ".join(f"{cc[k]:.0%}" for k in (3, 5, 10)))
        toks = s["A"]["tokens"]
        if toks:
            avg = sum(toks) / len(toks)
            print(f"  top3 命中示例 token: avg={avg:.0f} "
                  f"(2500 预算约装 {max(1, int(2500 / max(avg, 1)))} 个)")


if __name__ == "__main__":
    asyncio.run(main())
