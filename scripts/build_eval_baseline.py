#!/usr/bin/env python3
"""可复现基线产物构建器 — 从 BIRD dev.json 确定性生成固定问题集。

评测回归门(eval_gate)需要锚点:问题集固定、基线结果与问题一一对账。
本脚本一次生成 eval/baseline/ 下的两个可复现产物:

  eval/baseline/questions.jsonl  固定问题集(每行 {qid, db_id, question,
                                  evidence, gold_sql}),顺序 = dev.json 原始
                                  顺序,qid = {db_id}-{index:04d} 稳定可复现
  eval/baseline/results.jsonl    基线判定条目(可选,由历史 results.jsonl
                                  迁移:按问题文本对账出 qid;缺失 qid 的
                                  条目丢弃并报告)

同一 dev.json + db-id 重建结果逐字节一致,基线可比。

用法:
  uv run python scripts/build_eval_baseline.py \
      --dev-json /path/to/mini_dev_mysql.json --db-id financial
  uv run python scripts/build_eval_baseline.py \
      --dev-json /path/to/mini_dev_mysql.json --db-id financial \
      --results .trove/eval/results.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trove.eval.baseline import (
    check_integrity,
    coverage_check,
    load_jsonl,
    match_qid,
)


def parse_args() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dev-json", required=True, help="BIRD dev.json 路径")
    p.add_argument("--db-id", default="financial")
    p.add_argument("--out", default="eval/baseline", help="产物目录(默认 eval/baseline)")
    p.add_argument("--results", default=None,
                   help="迁移历史 results.jsonl → 基线(按问题文本对账出 qid;"
                        "重复条目取每个 qid 的最后一条)")
    p.add_argument("--gold-key", default="SQL", help="dev.json 的 gold SQL 字段名")
    return p.parse_args()


def _write_questions(questions: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for q in questions:
            f.write(json.dumps(q, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    args = parse_args()
    dev_path = Path(args.dev_json)
    if not dev_path.exists():
        print(f"error: dev.json 不存在: {dev_path}", file=sys.stderr)
        return 2
    dev = json.loads(dev_path.read_text(encoding="utf-8"))

    from trove.eval.baseline import build_questions

    questions = build_questions(dev, args.db_id, gold_key=args.gold_key)
    if not questions:
        print(f"error: dev.json 中没有 db_id={args.db_id} 的问题", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    q_path = out_dir / "questions.jsonl"
    _write_questions(questions, q_path)
    print(f"问题集 → {q_path} ({len(questions)} 题, qid 稳定)")

    if args.results:
        rows = load_jsonl(args.results)
        by_qid: dict[str, dict] = {}
        unmatched = 0
        for e in rows:
            qid = e.get("qid") or match_qid(str(e.get("question", "")), questions)
            if not qid or qid not in {q["qid"] for q in questions}:
                unmatched += 1
                continue
            by_qid[qid] = {**e, "qid": qid, "run_id": f"baseline-{qid}"}
        r_path = out_dir / "results.jsonl"
        with r_path.open("w", encoding="utf-8") as f:
            for q in questions:
                if q["qid"] in by_qid:
                    f.write(json.dumps(by_qid[q["qid"]], ensure_ascii=False) + "\n")
        cov = coverage_check(questions, by_qid.values())
        print(f"基线结果 → {r_path} ({len(by_qid)}/{len(questions)} 题有判定, "
              f"丢弃 {unmatched} 条无对账条目)")
        if cov["missing_qids"]:
            print(f"  未覆盖 {len(cov['missing_qids'])} 题: "
                  f"{cov['missing_qids'][:8]}{'...' if len(cov['missing_qids']) > 8 else ''}")

    integrity = check_integrity(q_path, out_dir / "results.jsonl")
    print(f"完整性: {'✓ 健康' if integrity['ok'] else '✗ 存在硬问题'} "
          f"(覆盖 {integrity['coverage']:.0%})")
    for w in integrity["warnings"]:
        print(f"  ! {w}")
    for p in integrity["problems"]:
        print(f"  ✗ {p}")
    return 0 if integrity["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
