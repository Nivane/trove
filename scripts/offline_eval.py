"""离线 eval:录制轨迹 → 回放打分(绕开 eval_bird 的成本约束)。

低成本迭代闭环:真实 LLM 跑一遍问题集(record)→ 之后任意次零 LLM
回放打分(replay)。评分维度对齐 eval 四维:完成率/正确率/token 成本/
失败恢复率。

用法:
  python scripts/offline_eval.py record --questions qs.txt --output .trove/eval/replay.jsonl
  python scripts/offline_eval.py replay --input .trove/eval/replay.jsonl
  python scripts/offline_eval.py replay --input .trove/eval/results.jsonl   # 兼容 eval_bird 产物

record 输入:
  --questions  每行一个问题的 txt(或 {"question": ...} 的 jsonl)
  --gold       可选 jsonl:[{"question": ..., "sql": ...}] 用于 gold 精确匹配
  --datasource 数据源 URL(默认 demo)
  --limit      只跑前 N 题
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

DEFAULT_OUTPUT = Path.cwd() / ".trove" / "eval" / "replay.jsonl"
DEFAULT_RESULTS = Path.cwd() / ".trove" / "eval" / "results.jsonl"


def parse_args() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trove 离线 eval(录制/回放)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="真实 LLM 跑一遍并录制轨迹")
    rec.add_argument("--questions", required=True, help="问题文件(txt 或 jsonl)")
    rec.add_argument("--gold", default=None, help="gold jsonl:[{question, sql}]")
    rec.add_argument("--datasource", default="demo", help="数据源 URL(默认内置 demo)")
    rec.add_argument("--output", default=str(DEFAULT_OUTPUT))
    rec.add_argument("--limit", type=int, default=0)

    rep = sub.add_parser("replay", help="零 LLM 回放打分")
    rep.add_argument("--input", default=str(DEFAULT_OUTPUT))
    return parser


def _load_questions(path: str) -> list[str]:
    p = Path(path)
    if p.suffix == ".jsonl":
        return [
            (json.loads(line).get("question", "") or "").strip()
            for line in p.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _load_gold(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    return {
        (json.loads(line).get("question", "") or "").strip(): (json.loads(line).get("sql", "") or "").strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def cmd_record(args) -> int:
    from trove.core.config import ConfigLoader
    from trove.eval.replay import append_entry, format_entry
    from trove.llm.gateway import LLMGateway
    from trove.llm.token_accounting import pop as pop_tokens
    from trove.services.datasource.catalog import CatalogService
    from trove.services.datasource.registry import ConnectorRegistry
    from trove.services.kb.service import KbService
    from trove.tracing.runlog import create_tracer
    from trove.workflow.graphs import GraphServices, build_graphs
    from trove.workflow.state import WorkflowState

    questions = _load_questions(args.questions)
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print("no questions", file=sys.stderr)
        return 2
    gold = _load_gold(args.gold)
    print(f"录制 {len(questions)} 题 → {args.output}", flush=True)

    config = ConfigLoader.load_agent_config("conf/agent.yml")
    registry = ConnectorRegistry()
    try:
        from trove.services.datasource.urls import parse_datasource_url

        await registry.register(parse_datasource_url(args.datasource), set_default=True)
    except Exception as e:
        print(f"datasource error: {e}", file=sys.stderr)
        return 2
    kb = KbService(Path.cwd())
    services = GraphServices(
        llm=LLMGateway(providers=config.providers),
        catalog=CatalogService(registry),
        connectors=registry,
        config=config,
        kb=kb,
        semantic_layer=None,
    )
    graph = build_graphs(services)["reflection"]

    for i, question in enumerate(questions, 1):
        run_id = f"replay-{i}-{int(time.time())}"
        state = WorkflowState(
            session_id=f"replay-{i}", question=question, run_id=run_id,
            lang=config.language,
        )
        tracer = create_tracer(run_id, verbose=False)
        tracer.start_run({"question": question, "gold_sql": gold.get(question, "")})
        t0 = time.monotonic()
        try:
            final = await graph.ainvoke(state)
        except Exception as e:
            tracer.finish({"verdict": "CRASH", "error": str(e)[:120]})
            append_entry(args.output, format_entry(
                run_id, question, verdict="ERROR", gold_sql=gold.get(question, ""),
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            ))
            print(f"  [{i}/{len(questions)}] CRASH: {e}", flush=True)
            continue
        tracer.finish({"verdict": final.verdict})
        tokens = pop_tokens(run_id) or {}
        entry = format_entry(
            run_id, question,
            pred_sql=final.sql or "",
            row_count=final.row_count,
            verdict=final.verdict,
            retry_count=final.retry_count,
            consensus=final.consensus,
            confidence=final.confidence,
            validation_hits=final.validation_hits or [],
            rollback_target=final.rollback_target or "",
            fix_mode=final.fix_mode or "",
            n_candidates=len(final.candidates or []),
            tokens=tokens,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            gold_sql=gold.get(question, ""),
            kb_hits=final.kb_hits or [],
        )
        append_entry(args.output, entry)
        print(f"  [{i}/{len(questions)}] {entry['verdict']} "
              f"retry={entry['retry_count']} tok={entry['tokens'].get('total', 0)}", flush=True)
    print("录制完成。离线打分: python scripts/offline_eval.py replay", flush=True)
    return 0


def cmd_replay(args) -> int:
    from trove.eval.replay import load_entries, render_scorecard, score_replay

    entries = load_entries(args.input)
    if not entries:
        print(f"no entries in {args.input}", file=sys.stderr)
        return 2
    print(render_scorecard(score_replay(entries)))
    return 0


def main() -> int:
    args = parse_args().parse_args()
    if args.cmd == "replay":
        return cmd_replay(args)
    return asyncio.run(cmd_record(args))


if __name__ == "__main__":
    raise SystemExit(main())
