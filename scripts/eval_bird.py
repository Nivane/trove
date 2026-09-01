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
import re
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
    parser.add_argument("--semantic-layer", default=None,
                        help="语义层目录(OSSIE YAML)。默认 config.semantic_layer_path,"
                             "再退 .trove/semantic/<db-id>;启用后编译路径在 eval 里生效"
                             "(path: compiled 归因 + compile_meta 分因)")
    parser.add_argument("--limit", type=int, default=0, help="Only evaluate N questions (0 = all)")
    parser.add_argument("--start", type=int, default=0,
                        help="Skip the first N questions (applied before --limit)")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Don't append the official evidence hint to the question")
    parser.add_argument("--oracle", action="store_true",
                        help="Oracle 锚:gold SQL 的 FROM/JOIN 表强制进 schema 匹配集"
                             "首位(半开卷上限对照,eval-only,默认关)")
    parser.add_argument("--scaling", type=int, default=5, choices=[5, 50, 200],
                        help="候选池规模(含 primary):5 = 历史 4 温度子图(默认,"
                             "成本不变);50/200 = 去相关大池测试时缩放 A/B"
                             "(成本随 N 线性上升,谨慎开)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Per-step detail: node inputs/outputs, full LLM prompts/outputs, tool observations")
    return parser.parse_args()


def normalize_rows(rows: list[list]) -> list[tuple]:
    """Set-comparison: sorted, stringified rows (column order-insensitive)."""
    return sorted(tuple(str(v) for v in row) for row in rows)


def extract_tables(sql: str) -> list[str]:
    """gold SQL → 涉及的物理表名(sqlglot 优先,正则兜底,保序去重)。

    纯解析,零网络——评测时把 FROM/JOIN 表喂给 oracle_tables 锚进
    schema 匹配集。解析失败退化为空(该题不应用 oracle 锚,不误伤)。
    """
    names: list[str] = []
    try:
        import sqlglot
        for ast in sqlglot.parse(sql, read="mysql"):
            for node in ast.find_all(sqlglot.exp.Table):
                t = node.name
                if t and t not in names:
                    names.append(t)
    except Exception:
        names = []
    if not names:
        for m in re.finditer(
            r"\b(?:FROM|JOIN)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
            sql, re.IGNORECASE,
        ):
            t = m.group(1)
            if t and t not in names:
                names.append(t)
    return names


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
    """逐题判定条目;final 缺失(崩溃)时不带 pred/kb/retries 字段。

    归因字段(P2-8)随 final 一起记录,让"哪个机制贡献了多少准确率"
    可以从 results.jsonl 直接回答:
      consensus        — select 节点多候选投票是否达成一致
      confidence       — 共识置信度(票数/候选数)
      selection        — 裁决细节(votes/adopted/原因)
      fix_mode         — 修复路径:fixer 实现级 / revisor 语义级 / 空(一次过)
      rollback_target  — 打回目标(gen_sql/query_sketch/schema_linking)
      validation_hits  — 通过前被哪些确定性规则拦过(含 answer-columns 层)
      n_candidates     — 进入执行投票的候选数(1 = 单候选直出)
    """
    entry: dict[str, Any] = {
        "run_id": run_id, "question": question, "evidence": evidence,
        "gold_sql": gold_sql, "verdict": verdict,
    }
    if final is not None:
        entry.update({
            "pred_sql": final.sql or "",
            "path": "compiled" if getattr(final, "compiled", False) else "llm",
            "compile_meta": getattr(final, "compile_meta", {}) or {},
            "kb_hits": final.kb_hits,
            "retries": final.retry_count,
            "consensus": final.consensus,
            "confidence": round(final.confidence, 3),
            "selection": final.selection or {},
            "fix_mode": final.fix_mode or "",
            "rollback_target": final.rollback_target or "",
            "validation_hits": final.validation_hits or [],
            "n_candidates": len(final.candidates or []),
        })
    return entry


def attribution_slices(results: list[dict]) -> list[str]:
    """机制归因切片:每个维度按取值统计 EX%(分母 = 该取值内的可判定题数)。

    维度覆盖全部新记录字段:共识与否、置信度档、修复路径(fixer/revisor)、
    打回目标、规则拦截、候选数、evidence A/B。gold 失败/崩溃题不进切片
    (verdict 不在可判定集合,避免污染分子分母)。
    """
    verdicts_ok = {
        "MATCH", "MISMATCH", "GENERATION_ERROR", "EXECUTION_ERROR", "EMPTY_SQL",
    }
    rows = [r for r in results if r.get("verdict") in verdicts_ok]
    if not rows:
        return []

    def rate(rows_sub: list[dict]) -> str:
        m = sum(1 for r in rows_sub if r.get("verdict") == "MATCH")
        return f"{m}/{len(rows_sub)} ({m / len(rows_sub) * 100:.1f}%)"

    _MISSING = object()  # 区分"键不存在"与 False/""(共识 False 也是有效取值)

    def bucket(rows_sub: list[dict], key: str) -> str:
        by_val: dict[Any, list[dict]] = {}
        for r in rows_sub:
            v = r.get(key, _MISSING)
            if v == "" or v is None:  # 空字符串/None 与缺失同义(机制未走)
                v = _MISSING
            by_val.setdefault(v, []).append(r)
        return " | ".join(
            f"{v}: {rate(vr)}" if v is not _MISSING else f"(无): {rate(vr)}"
            for v, vr in sorted(by_val.items(), key=lambda kv: -len(kv[1]))
        )

    # 为数值/布尔/集合类取值预计算桶键(不污染 jsonl 条目本身)
    for r in rows:
        r["_confidence_bucket"] = "high (≥0.5)" if r.get("confidence", 0) >= 0.5 else "low (<0.5)"
        r["_hits_bucket"] = "拦过" if r.get("validation_hits") else "未拦"
        r["_cand_bucket"] = (
            "multi (≥2)" if (r.get("n_candidates") or 0) >= 2 else "single (1)"
        )
        r["_evidence_bucket"] = "with evidence" if r.get("evidence") else "no evidence"
        r["_oracle_bucket"] = "oracle" if r.get("oracle") else "no-oracle"
        r["_scaling_bucket"] = str(r.get("scaling") or 5)
    return [
        "=== 机制归因切片 ===",
        f"路径: {bucket(rows, 'path')}",
        f"共识: {bucket(rows, 'consensus')}",
        f"置信度: {bucket(rows, '_confidence_bucket')}",
        f"修复模式: {bucket(rows, 'fix_mode')}",
        f"回退目标: {bucket(rows, 'rollback_target')}",
        f"规则拦截: {bucket(rows, '_hits_bucket')}",
        f"候选数: {bucket(rows, '_cand_bucket')}",
        f"evidence: {bucket(rows, '_evidence_bucket')}",
        f"oracle: {bucket(rows, '_oracle_bucket')}",
        f"scaling: {bucket(rows, '_scaling_bucket')}",
    ]


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
    kb = KbService(Path.cwd(), kb_dir=kb_root)

    # 语义层接线(仿 main.py):编译路径在 eval 里必须是活的,否则
    # path: compiled 永远为 0。--semantic-layer 显式给目录;默认走
    # config.semantic_layer_path,再退 .trove/semantic/<db-id>。任何
    # 失败静默降级(零语义层 → 全部 path: llm,compile_meta 记
    # no_semantic_layer 供统计脚本诊断)。
    semantic_layer = None
    try:
        from trove.services.semantic_layer.provider import SemanticLayerProvider

        schema = await adapter.get_schema()
        known_tables = {t.name.lower() for t in schema.tables}
        semantic_dir = Path(args.semantic_layer) if args.semantic_layer else (
            Path.cwd() / config.semantic_layer_path / args.db_id
            if config.semantic_layer_path
            else Path.cwd() / ".trove" / "semantic" / args.db_id
        )
        semantic_layer = SemanticLayerProvider(
            directory=semantic_dir,
            datasource=args.db_id,
            dialect=adapter.dialect(),
            table_exists=lambda t: t.lower() in known_tables,
            kb_semantics_path=kb.semantics_path(args.db_id),
        )
        if not semantic_layer.enabled:
            semantic_layer = None
    except Exception as e:
        print(f"semantic layer unavailable in eval: {e}", flush=True)
        semantic_layer = None

    services = GraphServices(
        llm=LLMGateway(providers=config.providers),
        catalog=CatalogService(registry),
        connectors=registry,
        config=config,
        kb=kb,
        semantic_layer=semantic_layer,
    )
    graph = build_graphs(services, scaling=args.scaling)["reflection"]

    matched = 0
    failures = {"generation": 0, "execution": 0, "mismatch": 0, "gold_error": 0, "crash": 0}
    total_retries = 0
    results: list[dict] = []  # 本轮的逐题条目(供归因切片;jsonl 仍全部落盘)

    def done(entry: dict) -> dict:
        """判定条目同时进内存切片池与 results.jsonl。

        oracle/scaling 随条目记录(每个 done 都渲染当前 state),让 oracle
        A/B 与缩放 A/B 可以从 results.jsonl 直接切片。
        """
        entry.setdefault("oracle", bool(state.oracle_tables))
        entry.setdefault("scaling", args.scaling)
        results.append(entry)
        record_result(entry)
        return entry

    def log(msg: str) -> None:
        print(msg, flush=True)

    for i, q in enumerate(questions, 1):
        question = q["question"]
        evidence = q.get("evidence", "") if not args.no_evidence else ""
        gold_sql = q["SQL"]
        # run_id 唯一(含时间戳):traces.jsonl 与 /trace 回放按 run 隔离,
        # 同一题反复评估不会把多轮执行的事件混进一个 run
        run_id = f"eval-{i}-{int(time.time())}"
        oracle_tables = extract_tables(gold_sql) if args.oracle else []
        state = WorkflowState(
            session_id=f"eval-{i}", question=question, evidence=evidence,
            run_id=run_id,
            lang=config.language,
            oracle_tables=oracle_tables,
        )
        # per-run 观测:trace span 树 + runs/{run_id}.log 详尽日志(+verbose 回显)
        tracer = create_tracer(state.run_id, verbose=args.verbose)
        tracer.start_run({
            "question": question,
            "evidence": evidence,
            "gold_sql": gold_sql,
            "oracle": bool(oracle_tables),
            "scaling": args.scaling,
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
            done(_result_entry(
                run_id, question, evidence, gold_sql, "CRASH",
            ) | {"error": f"crash: {str(e)[:200]}"})
            log(f"[{i}/{len(questions)}] ✗ 崩溃: {str(e)[:70]}")
            continue
        total_retries += final.retry_count

        try:
            gold_rows = (await adapter.execute(gold_sql)).rows
        except Exception as e:
            failures["gold_error"] += 1
            done(_result_entry(
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
            done(_result_entry(
                run_id, question, evidence, gold_sql,
                classify_pred_error(final.error), final,
            ) | {"error": final.error[:200]})
            log(f"[{i}/{len(questions)}] ✗ {final.error[:70]}")
            continue

        if not final.sql:
            failures["execution"] += 1
            done(_result_entry(
                run_id, question, evidence, gold_sql, "EMPTY_SQL", final,
            ) | {"error": "空 SQL（意图可能误路由）"})
            log(f"[{i}/{len(questions)}] ✗ 空 SQL（意图可能误路由）")
            continue

        pred_rows = (await adapter.execute(final.sql)).rows
        if normalize_rows(pred_rows) == normalize_rows(gold_rows):
            matched += 1
            done(_result_entry(
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
            done(_result_entry(
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
    for line in attribution_slices(results):
        log(line)

    await registry.close_all()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
