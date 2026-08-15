"""BIRD dev-set evaluation for Trove — Execution Accuracy (EX).

Runs the full reflection pipeline against a real datasource for every
dev question of one database, executes both the predicted and the gold
SQL, and compares result sets.

Usage:
    uv run python scripts/eval_bird.py --limit 10
    uv run python scripts/eval_bird.py --db-id financial \
        --dev-json /path/to/MINIDEV/dev.json \
        --datasource mysql://root:root@127.0.0.1:3306/financial
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from trove.core.config import ConfigLoader
from trove.llm.gateway import LLMGateway
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.datasource.catalog import CatalogService
from trove.services.kb.service import KbService
from trove.workflow.graphs import GraphServices, build_graphs
from trove.workflow.state import WorkflowState


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-json", default="/Users/zhaolipan/Downloads/minidev/MINIDEV/mini_dev_mysql.json")
    parser.add_argument("--db-id", default="financial")
    parser.add_argument("--datasource", default="mysql://root:root@127.0.0.1:3306/financial")
    parser.add_argument("--limit", type=int, default=0, help="Only evaluate the first N questions (0 = all)")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Don't append the official evidence hint to the question")
    return parser.parse_args()


def normalize_rows(rows: list[list]) -> list[tuple]:
    """Set-comparison: sorted, stringified rows (column order-insensitive)."""
    return sorted(tuple(str(v) for v in row) for row in rows)


async def main() -> None:
    args = parse_args()

    dev = json.loads(Path(args.dev_json).read_text(encoding="utf-8"))
    questions = [q for q in dev if q.get("db_id") == args.db_id]
    if not questions:
        print(f"dev.json 中没有 db_id={args.db_id} 的问题")
        sys.exit(1)
    if args.limit:
        questions = questions[: args.limit]
    print(f"评估 {args.db_id}: {len(questions)} 题")

    config = ConfigLoader.load_agent_config("conf/agent.yml")
    registry = ConnectorRegistry()
    adapter = await registry.register(parse_datasource_url(args.datasource), set_default=True)

    services = GraphServices(
        llm=LLMGateway(providers=config.providers),
        catalog=CatalogService(registry),
        connectors=registry,
        config=config,
        kb=KbService(Path.cwd()),
    )
    graph = build_graphs(services)["reflection"]

    matched = 0
    failures = {"generation": 0, "execution": 0, "mismatch": 0, "gold_error": 0}
    total_retries = 0

    for i, q in enumerate(questions, 1):
        question = q["question"]
        if not args.no_evidence and q.get("evidence"):
            question = f"{question}\nEvidence: {q['evidence']}"
        gold_sql = q["SQL"]
        state = WorkflowState(session_id=f"eval-{i}", question=question)
        final = await graph.ainvoke(state)
        total_retries += final["retry_count"]

        try:
            gold_rows = (await adapter.execute(gold_sql)).rows
        except Exception as e:
            failures["gold_error"] += 1
            print(f"[{i}/{len(questions)}] ✗ gold 执行失败: {question[:40]}... ({e})")
            continue

        if final["error"]:
            kind = "generation" if "generation" in final["error"].lower() else "execution"
            failures[kind] += 1
            print(f"[{i}/{len(questions)}] ✗ {final['error'][:70]}")
            continue

        pred_rows = (await adapter.execute(final["sql"])).rows
        if normalize_rows(pred_rows) == normalize_rows(gold_rows):
            matched += 1
            print(f"[{i}/{len(questions)}] ✓ {question[:50]}... (retry {final['retry_count']})")
        else:
            failures["mismatch"] += 1
            print(f"[{i}/{len(questions)}] ✗ 结果不一致: {question[:50]}... "
                  f"(pred {len(pred_rows)} rows, gold {len(gold_rows)} rows, retry {final['retry_count']})")

    total = len(questions)
    evaluated = total - failures["gold_error"]
    print("\n=== 汇总 ===")
    print(f"Execution Accuracy: {matched}/{evaluated} = {matched / evaluated * 100:.1f}%"
          f"（gold 执行失败 {failures['gold_error']} 题未计入）")
    print(f"错误分布: 生成失败 {failures['generation']} | 执行失败 {failures['execution']} | "
          f"结果不一致 {failures['mismatch']}")
    print(f"平均修正轮数: {total_retries / total:.1f}")

    await registry.close_all()


if __name__ == "__main__":
    asyncio.run(main())
