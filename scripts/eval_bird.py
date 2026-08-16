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
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")

from trove.agent.session import SessionManager
from trove.cli.app import TroveREPL
from trove.core.config import ConfigLoader
from trove.core.i18n import L
from trove.llm.gateway import LLMGateway
from trove.tracing.local import configure_trace_store
from trove.tracing.runlog import create_tracer
from trove.services.datasource.registry import ConnectorRegistry
from trove.services.datasource.urls import parse_datasource_url
from trove.services.datasource.catalog import CatalogService
from trove.services.kb.service import KbService, resolve_kb_root
from trove.workflow.graphs import GraphServices, build_graphs
from trove.workflow.state import WorkflowState


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-json", default="/Users/zhaolipan/Downloads/minidev/MINIDEV/mini_dev_mysql.json")
    parser.add_argument("--db-id", default="financial")
    parser.add_argument("--datasource", default="mysql://root:root@127.0.0.1:3306/financial")
    parser.add_argument("--kb-dir", default=None,
                        help="KB 根目录(含 <db-id>/ 子目录)或直接指向该数据源的 YAML 目录;"
                             "默认 <cwd>/.trove/kb")
    parser.add_argument("--limit", type=int, default=0, help="Only evaluate N questions (0 = all)")
    parser.add_argument("--start", type=int, default=0,
                        help="Skip the first N questions (applied before --limit)")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Don't append the official evidence hint to the question")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Per-step detail: node inputs/outputs, full LLM prompts/outputs, tool observations")
    return parser.parse_args()


def normalize_rows(rows: list[list]) -> list[tuple]:
    """Set-comparison: sorted, stringified rows (column order-insensitive)."""
    return sorted(tuple(str(v) for v in row) for row in rows)


FAILURES_PATH = Path.cwd() / ".trove" / "eval" / "failures.jsonl"
RESULTS_PATH = Path.cwd() / ".trove" / "eval" / "results.jsonl"


def record_failure(entry: dict) -> None:
    """答错的题追加到本地 JSONL(供 scripts/distill_lessons.py 蒸馏教训)。"""
    try:
        FAILURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with FAILURES_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def classify_pred_error(error: str) -> str:
    """final.error → GENERATION_ERROR | EXECUTION_ERROR(与汇总口径一致)。"""
    return "GENERATION_ERROR" if error and "generation" in error.lower() else "EXECUTION_ERROR"


def record_result(entry: dict, path: Path | None = None) -> None:
    """逐题判定追加到 results.jsonl(含 MATCH,供 A/B 对比与归因)。"""
    try:
        target = path or RESULTS_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _result_entry(
    run_id: str, question: str, evidence: str, gold_sql: str, verdict: str,
    final: WorkflowState | None = None,
) -> dict:
    """逐题判定条目;final 缺失(崩溃)时不带 pred/kb/retries 字段。"""
    entry: dict[str, Any] = {
        "run_id": run_id, "question": question, "evidence": evidence,
        "gold_sql": gold_sql, "verdict": verdict,
    }
    if final is not None:
        entry.update({
            "pred_sql": final.sql or "",
            "kb_hits": final.kb_hits,
            "retries": final.retry_count,
        })
    return entry


def slice_questions(questions: list[dict], limit: int = 0, start: int = 0) -> list[dict]:
    """Limit + offset semantics: skip `start` first, then take `limit` (0 = all).

    `--start 5 --limit 5` evaluates questions 6–10.
    """
    sliced = questions[start:]
    if limit:
        sliced = sliced[:limit]
    return sliced


def _print_step(step: dict[str, Any], lang: str) -> None:
    """终端渲染一步（ℹ 序号 · 节点 · 耗时 · 摘要），与 REPL 同格式。"""
    detail = step.get("detail", {})
    head = f"  ℹ [{step['seq']}] {step['node']}"
    if detail.get("retry"):
        head += L(lang, f" · 重试#{detail['retry']}", f" · retry#{detail['retry']}")
    head += f" · {step['elapsed_ms']}ms"
    summary = TroveREPL._step_summary(step["node"], detail, lang)
    line = head + (f" → {summary}" if summary else "")
    if detail.get("reason"):
        line += f" · {detail['reason'][:120]}"
    print(line, flush=True)

    llm = detail.get("llm")
    if llm:
        print(f"    llm: {llm.get('model', '')} · {llm.get('elapsed_ms', 0)}ms", flush=True)
    if step["node"] == "analyze_error" and detail.get("analysis"):
        print(f"    analysis: {detail['analysis'][:300]}", flush=True)
    if step["node"] == "gen_sql" and detail.get("sql"):
        print(f"    sql: {detail['sql'][:300]}", flush=True)


async def _run_with_steps(graph, state: WorkflowState, tracer=None) -> WorkflowState:
    """逐节点执行：终端渲染 step + 写本地 trace store（run/step/finish 事件）。

    与 SessionManager.ask_stream 同一事件形态：最终状态由各节点 update
    合并得到，因此 /trace 回放与 REPL 的链路完全一致。

    tracer:活跃 RunTracer 时把其 callback 挂进 graph config——每个节点
    （含 gen_sql 子图与重试）自动开/关 span,llm/tool 事件落到 span 下;
    verbose 模式下叙事由 tracer 实时回显,简洁 step 行不再重复打印。
    """
    lang = state.lang
    SessionManager._trace_run_start(state)
    merged: dict[str, Any] = state.model_dump()
    seq = 0
    last_ts = time.monotonic()
    config: dict[str, Any] = {}
    if tracer is not None:
        config["callbacks"] = [tracer.callback()]
    async for update in graph.astream(state, config=config or None, stream_mode="updates"):
        for node_name, delta in update.items():
            if not delta:
                continue
            now = time.monotonic()
            elapsed_ms = int((now - last_ts) * 1000)
            last_ts = now
            seq += 1
            # 修正上下文：本次节点执行前挂起的反馈与轮次（与 SessionManager 一致）
            reason = merged.get("error_feedback", "")
            retry = merged.get("retry_count", 0)
            merged.update(delta)
            step = SessionManager._step_event(
                seq, node_name, delta, elapsed_ms, reason, retry, lang,
            )
            SessionManager._trace_step(state, step)
            if tracer is None or not tracer.verbose:
                _print_step(step, lang)
    final = WorkflowState.model_validate(merged)
    SessionManager._trace_run_finish(state.run_id, final)
    return final


async def main() -> None:
    args = parse_args()

    dev = json.loads(Path(args.dev_json).read_text(encoding="utf-8"))
    questions = [q for q in dev if q.get("db_id") == args.db_id]
    if not questions:
        print(f"dev.json 中没有 db_id={args.db_id} 的问题")
        sys.exit(1)
    questions = slice_questions(questions, limit=args.limit, start=args.start)
    print(f"评估 {args.db_id}: {len(questions)} 题", flush=True)

    config = ConfigLoader.load_agent_config("conf/agent.yml")
    try:
        kb_root = resolve_kb_root(args.kb_dir, args.db_id)
    except ValueError as e:
        # parser.error 会在协程内抛 SystemExit,把 asyncio.run 的清理流程
        # 搅出 "Event loop is closed" 噪声;打印一行错误并返回码 2 更干净。
        print(f"error: {e}", file=sys.stderr)
        return 2

    # LLM 调用记录到本地 trace store（诊断 reflect/gen loop 实际行为）
    configure_trace_store(Path.home() / ".trove")
    registry = ConnectorRegistry()
    adapter = await registry.register(parse_datasource_url(args.datasource), set_default=True)

    services = GraphServices(
        llm=LLMGateway(providers=config.providers),
        catalog=CatalogService(registry),
        connectors=registry,
        config=config,
        kb=KbService(Path.cwd(), kb_dir=kb_root),
    )
    graph = build_graphs(services)["reflection"]

    matched = 0
    failures = {"generation": 0, "execution": 0, "mismatch": 0, "gold_error": 0, "crash": 0}
    total_retries = 0

    def log(msg: str) -> None:
        print(msg, flush=True)

    for i, q in enumerate(questions, 1):
        question = q["question"]
        evidence = q.get("evidence", "") if not args.no_evidence else ""
        gold_sql = q["SQL"]
        # run_id 唯一(含时间戳):traces.jsonl 与 /trace 回放按 run 隔离,
        # 同一题反复评估不会把多轮执行的事件混进一个 run
        run_id = f"eval-{i}-{int(time.time())}"
        state = WorkflowState(
            session_id=f"eval-{i}", question=question, evidence=evidence,
            run_id=run_id,
            lang=config.language,
        )
        # per-run 观测:trace span 树 + runs/{run_id}.log 详尽日志(+verbose 回显)
        tracer = create_tracer(state.run_id, verbose=args.verbose)
        tracer.start_run({
            "question": question,
            "evidence": evidence,
            "gold_sql": gold_sql,
            "model": config.target,
            "lang": config.language,
        })
        log(f"── [{i}/{len(questions)}] {question[:60]}")
        try:
            final = await _run_with_steps(graph, state, tracer)
        except Exception as e:
            # 单题崩溃不拖垮整轮评估（如 LLM 中途 500）
            failures["crash"] += 1
            tracer.finish({"verdict": "CRASH", "error": str(e)[:120]})
            record_failure({
                "question": question, "evidence": evidence, "gold_sql": gold_sql,
                "pred_sql": "", "error": f"crash: {str(e)[:200]}",
            })
            record_result(_result_entry(
                run_id, question, evidence, gold_sql, "CRASH",
            ) | {"error": f"crash: {str(e)[:200]}"})
            log(f"[{i}/{len(questions)}] ✗ 崩溃: {str(e)[:70]}")
            continue
        total_retries += final.retry_count

        try:
            gold_rows = (await adapter.execute(gold_sql)).rows
        except Exception as e:
            failures["gold_error"] += 1
            record_result(_result_entry(
                run_id, question, evidence, gold_sql, "GOLD_ERROR", final,
            ) | {"error": str(e)[:200]})
            log(f"[{i}/{len(questions)}] ✗ gold 执行失败: {question[:40]}... ({e})")
            continue

        if final.error:
            kind = "generation" if "generation" in final.error.lower() else "execution"
            failures[kind] += 1
            record_failure({
                "question": question, "evidence": evidence, "gold_sql": gold_sql,
                "pred_sql": final.sql or "", "error": final.error[:200],
            })
            record_result(_result_entry(
                run_id, question, evidence, gold_sql,
                classify_pred_error(final.error), final,
            ) | {"error": final.error[:200]})
            log(f"[{i}/{len(questions)}] ✗ {final.error[:70]}")
            continue

        if not final.sql:
            failures["execution"] += 1
            record_result(_result_entry(
                run_id, question, evidence, gold_sql, "EMPTY_SQL", final,
            ) | {"error": "空 SQL（意图可能误路由）"})
            log(f"[{i}/{len(questions)}] ✗ 空 SQL（意图可能误路由）")
            continue

        pred_rows = (await adapter.execute(final.sql)).rows
        if normalize_rows(pred_rows) == normalize_rows(gold_rows):
            matched += 1
            record_result(_result_entry(
                run_id, question, evidence, gold_sql, "MATCH", final,
            ))
            log(f"[{i}/{len(questions)}] ✓ {question[:50]}... (retry {final.retry_count})")
        else:
            failures["mismatch"] += 1
            record_failure({
                "question": question, "evidence": evidence, "gold_sql": gold_sql,
                "pred_sql": final.sql or "",
                "error": f"mismatch (pred {len(pred_rows)} rows, gold {len(gold_rows)} rows)",
            })
            record_result(_result_entry(
                run_id, question, evidence, gold_sql, "MISMATCH", final,
            ) | {"error": f"mismatch (pred {len(pred_rows)} rows, gold {len(gold_rows)} rows)"})
            log(f"[{i}/{len(questions)}] ✗ 结果不一致: {question[:50]}... "
                f"(pred {len(pred_rows)} rows, gold {len(gold_rows)} rows, retry {final.retry_count})")

    total = len(questions)
    evaluated = total - failures["gold_error"] - failures["crash"]
    log("\n=== 汇总 ===")
    log(f"Execution Accuracy: {matched}/{evaluated} = {matched / evaluated * 100:.1f}%"
        f"（gold 失败 {failures['gold_error']} + 崩溃 {failures['crash']} 题未计入）")
    log(f"错误分布: 生成失败 {failures['generation']} | 执行失败 {failures['execution']} | "
        f"结果不一致 {failures['mismatch']} | 崩溃 {failures['crash']}")
    log(f"平均修正轮数: {total_retries / total:.1f}")

    await registry.close_all()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
