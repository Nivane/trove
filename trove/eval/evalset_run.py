"""手动 evalset runner —— 图驱动、分 tier 落盘、判定映射与成本护栏。

一次 run 的产物是**原始记录 + 派生摘要**两件:

  ``.trove/eval/evalsets/<ds>/<tier>/<run_id>.jsonl``        逐题判定条目
  ``.trove/eval/evalsets/<ds>/<tier>/<run_id>.summary.json`` 本次 run 的汇总
  ``latest.*`` 是上述两件的原子副本,供管理面只读投影。

纪律:
- **分 tier 绝不合并**:分数跨层合并会把「测保真度」与「测正确性」混成
  一个数字;``tier="all"`` 由 CLI 拆成三次调用。
- **GOLD_ERROR 不进 EX 分母**:参考 SQL 都执行不了时,这道题判的是资产
  而不是系统;``replay.JUDGED_VERDICTS`` 不含它,计数值另列。
- **成本护栏**:没有 ``yes`` 不调用 LLM(dry-run);``compile_only`` 只读
  资产 + SQLGlot/试执行。正式运行缺 ``registry`` → 退出 2,零 LLM ——
  参考 SQL 无法执行,没有任何题可判定。
- **KB 快照**:进入 ``KbInitLock`` 后取一次 ``kb_rev``,结束再取;不一致
  如实记 ``kb_rev_changed_mid_run`` + ``degraded``(不承诺中途不变,只承诺
  变了会被标注)。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trove.eval.gate import metrics_from_entries
from trove.eval.graph_runner import build_eval_graph, run_question
from trove.eval.replay import JUDGED_VERDICTS, format_entry
from trove.eval.text import classify_pred_error, normalize_rows
from trove.services.evalset.models import (
    EvalsetDoc,
    EvalsetError,
    EvalsetItem,
    now_iso,
    tier_of,
)
from trove.services.evalset.rev import evalset_rev
from trove.services.evalset.store import load_evalset
from trove.services.kb.locks import KbInitBusy, KbInitLock
from trove.services.kb.sql_guard import sqlglot_ok, trial_ok

__all__ = [
    "RunOptions",
    "RunResult",
    "RunStoreUnreadable",
    "read_latest_entries",
    "read_latest_summary",
    "run_evalset",
]

#: 数据源/tier 会拼进路径:与 evalset 资产目录同一条路径段纪律。
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: 稳定抽样的命名空间前缀(改它 = 换一批题;版本号进前缀,可并存)。
_SAMPLE_NS = "evalset.sample.v1|"

#: 泄漏二次探测的 KB hit 种类(参考示例/模板出现在命中里 = 疑似背题)。
_LEAK_KINDS = frozenset({"example", "template"})


class RunStoreUnreadable(Exception):
    """run 存储存在但读不出(JSON 坏 / 不可读 / 形状不是对象)。

    与「文件缺失」严格分开:缺失 = 还没跑过(``None``),读不出 = 存储坏了
    (响亮报错)。把后者洗成前者会让就绪视图显示「未测」而掩盖一份坏文件。
    """


@dataclass
class RunOptions:
    """一次手动 run 的全部输入(CLI / API / 脚本共用)。"""

    project_root: Path
    datasource: str
    tier: str = "verified"
    limit: int = 0
    sample: int = 0
    compile_only: bool = False
    yes: bool = False
    strict: bool = False
    lang: str = "zh"
    kb: Any = None
    registry: Any = None
    config: Any = None
    semantic_layer: Any = None
    graph: Any = None


@dataclass
class RunResult:
    """一次 run 的结果(退出码 + 摘要 + 逐题条目)。"""

    exit_code: int = 0
    run_id: str = ""
    tier: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    entries: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    message: str = ""


# ── 路径与 I/O ────────────────────────────────────────────────


def _run_root(project_root: Any, datasource: str, tier: str) -> Path:
    """``<root>/.trove/eval/evalsets/<ds>/<tier>``(两段都校验)。"""
    return (
        Path(project_root) / ".trove" / "eval" / "evalsets"
        / _safe_segment(datasource, "datasource")
        / _safe_segment(tier, "tier")
    )


def _safe_segment(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_SEGMENT.fullmatch(value):
        raise RunStoreUnreadable(f"invalid {label} {value!r}")
    return value


def _read_json(path: Path) -> Any:
    """读 JSON;缺失由调用方先判,这里任何读/解析错误都是 ``unreadable``。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RunStoreUnreadable(
            f"cannot read run store {path}: {exc}"
        ) from exc


def read_latest_summary(
    project_root: Any, datasource: str, tier: str,
) -> dict[str, Any] | None:
    """读 ``latest.summary.json``:缺失 → ``None``;读不出 → ``RunStoreUnreadable``。"""
    path = _run_root(project_root, datasource, tier) / "latest.summary.json"
    if not path.is_file():
        return None
    data = _read_json(path)
    if not isinstance(data, dict):
        raise RunStoreUnreadable(
            f"run summary {path} is not a JSON object"
        )
    return data


def read_latest_entries(
    project_root: Any, datasource: str, tier: str,
) -> list[dict[str, Any]] | None:
    """读 ``latest.jsonl``:缺失 → ``None``;任一行读不出 → ``RunStoreUnreadable``。"""
    path = _run_root(project_root, datasource, tier) / "latest.jsonl"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RunStoreUnreadable(
            f"cannot read run entries {path}: {exc}"
        ) from exc
    entries: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise RunStoreUnreadable(
                f"cannot parse run entries {path} line {line_no}: {exc}"
            ) from exc
        if not isinstance(entry, dict):
            raise RunStoreUnreadable(
                f"run entry {path} line {line_no} is not a JSON object"
            )
        entries.append(entry)
    return entries


def _atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + ``os.replace``:读者要么看旧版、要么看新版。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:6]}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ── KB 快照 ──────────────────────────────────────────────────


def _kb_dir(options: RunOptions) -> Path:
    """锁目录的锚点:真实 KB 用 ``kb.kb_dir``;未装配时退回项目默认路径。"""
    kb_dir = getattr(options.kb, "kb_dir", None)
    if kb_dir is None:
        return Path(options.project_root) / ".trove" / "kb"
    return Path(kb_dir)


async def _kb_rev(kb: Any, datasource: str) -> dict[str, Any] | None:
    """KB 指纹;KB 缺席/读取失败 → ``None``(**绝不猜一个 rev**)。"""
    if kb is None:
        return None
    fn = getattr(kb, "kb_rev", None)
    if fn is None:
        return None
    try:
        return await fn(datasource)
    except Exception:  # noqa: BLE001 — 指纹缺失是缺席容忍,不阻断跑题
        return None


async def _ensure_synced(kb: Any, datasource: str) -> None:
    if kb is None:
        return
    fn = getattr(kb, "ensure_synced", None)
    if fn is not None:
        await fn(datasource)


def _rev_of(snapshot: Any) -> str:
    """快照 → rev 字符串;``None``(未启用/无有效行)按空串,不猜。"""
    if not isinstance(snapshot, dict):
        return ""
    return str(snapshot.get("rev") or "")


# ── 选题 ─────────────────────────────────────────────────────


def _select(
    doc: EvalsetDoc, options: RunOptions,
) -> tuple[list[EvalsetItem], list[dict[str, Any]]]:
    """confirmed + tier → (可运行条目, 被跳过的 sidecar 行)。

    ``match != exact`` 的条目**不进判定**:确认门本应拦住它,这里是纵深
    防御。跳过不是失败 —— 它只进 ``skipped`` 计数,不进任何分母。
    """
    runnable: list[EvalsetItem] = []
    skipped: list[dict[str, Any]] = []
    for item in doc.items:
        if item.status != "confirmed" or tier_of(item) != options.tier:
            continue
        if item.match != "exact":
            skipped.append({
                "qid": item.qid,
                "question": item.question,
                "skipped": "unsupported_match",
            })
            continue
        runnable.append(item)

    if options.sample and options.sample > 0:
        # 稳定哈希抽样:同一份资产、同一个 N,任何进程都抽到同一批题。
        runnable.sort(key=lambda it: hashlib.sha256(
            (_SAMPLE_NS + it.qid).encode("utf-8")
        ).hexdigest())
        runnable = runnable[: options.sample]
    if options.limit and options.limit > 0:
        runnable = runnable[: options.limit]
    return runnable, skipped


# ── 判定 ─────────────────────────────────────────────────────


def _pop_tokens(run_id: str) -> dict[str, int]:
    """本题 token 用量(进程级记账弹栈);记账失败不阻断判定。"""
    try:
        from trove.llm.token_accounting import pop as _pop_usage

        return _pop_usage(run_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def _path_of(final: Any) -> str:
    """答案来源档位:refused 最先判(拒绝轮没有 SQL,残留标记不算数)。"""
    if final is None:
        return "llm"
    if getattr(final, "refusal", None):
        return "refused"
    if getattr(final, "compile_partial", False):
        return "partial"
    if getattr(final, "compiled", False):
        return "compiled"
    return "llm"


def _leakage_suspect(kb_hits: list[Any]) -> bool:
    """``kb_hits`` 里出现 example/template = 疑似答案泄漏(运行时第二道防线)。"""
    for hit in kb_hits or []:
        kind = hit.get("kind", "") if isinstance(hit, dict) else getattr(
            hit, "kind", "")
        if kind in _LEAK_KINDS:
            return True
    return False


async def _judge_one(
    graph: Any,
    item: EvalsetItem,
    *,
    index: int,
    run_id: str,
    options: RunOptions,
    kb_start: Any,
    evalset_fingerprint: str,
) -> dict[str, Any]:
    """跑一道题并折叠成判定条目。

    判定顺序是承重的:**CRASH → GOLD_ERROR → error 分类 → 拒绝 → 空 SQL →
    预测执行 → 比较**。参考 SQL 先于预测侧的一切分类执行:参考都跑不了时,
    这道题判的是资产,不是系统 —— 记 GOLD_ERROR,不进 EX 分母。
    """
    datasource = options.datasource
    t0 = time.monotonic()
    final: Any = None
    verdict = ""
    error = ""
    reference_error = ""
    ref_rows: list[list] | None = None
    pred_rows: list[list] | None = None

    try:
        final = await run_question(
            graph, item.question, run_id=run_id, index=index,
            lang=options.lang, datasource=datasource,
        )
        # 兼容同步 runner / 测试替身:其返回值本身也可能是 awaitable
        # (真正的 graph_runner 返回 WorkflowState,这里零开销)。
        while inspect.isawaitable(final):
            final = await final
    except Exception as exc:  # noqa: BLE001 — 单题崩溃不拖垮整轮
        verdict = "CRASH"
        error = f"crash: {str(exc)[:200]}"
    else:
        # 参考 SQL:现场执行,失败即 GOLD_ERROR(优先级高于 final.error)
        if not (item.reference_sql or "").strip():
            verdict = "GOLD_ERROR"
            reference_error = "reference_sql missing"
        else:
            try:
                ref_rows = (await options.registry.execute(
                    item.reference_sql, datasource)).rows
            except Exception as exc:  # noqa: BLE001
                verdict = "GOLD_ERROR"
                reference_error = str(exc)[:200]

        if not verdict:
            if final.error:
                verdict = classify_pred_error(final.error)
                error = final.error[:200]
            elif getattr(final, "refusal", None):
                verdict = "REFUSED"
                reason = str((final.refusal or {}).get("reason") or "uncovered")
                error = f"refused: {reason}"[:200]
            elif not getattr(final, "sql", ""):
                verdict = "EMPTY_SQL"
                error = "空 SQL（意图可能误路由）"
            else:
                try:
                    pred_rows = (await options.registry.execute(
                        final.sql, datasource)).rows
                except Exception as exc:  # noqa: BLE001
                    verdict = "EXECUTION_ERROR"
                    error = str(exc)[:200]
                else:
                    if normalize_rows(pred_rows) == normalize_rows(ref_rows or []):
                        verdict = "MATCH"
                    else:
                        verdict = "MISMATCH"
                        error = (
                            f"mismatch (pred {len(pred_rows)} rows, "
                            f"ref {len(ref_rows or [])} rows)"
                        )

    kb_hits = list(getattr(final, "kb_hits", []) or []) if final is not None else []
    if pred_rows is not None:
        row_count: int | None = len(pred_rows)
    else:
        row_count = getattr(final, "row_count", None) if final is not None else None
    snapshot = kb_start if isinstance(kb_start, dict) else {}

    entry = format_entry(
        run_id, item.question,
        pred_sql=(getattr(final, "sql", "") or "") if final is not None else "",
        row_count=row_count,
        verdict=verdict,
        retry_count=int(getattr(final, "retry_count", 0) or 0) if final is not None else 0,
        consensus=getattr(final, "consensus", None) if final is not None else None,
        confidence=float(getattr(final, "confidence", 0.0) or 0.0)
        if final is not None else 0.0,
        validation_hits=list(getattr(final, "validation_hits", []) or [])
        if final is not None else [],
        rollback_target=(getattr(final, "rollback_target", "") or "")
        if final is not None else "",
        fix_mode=(getattr(final, "fix_mode", "") or "")
        if final is not None else "",
        n_candidates=len(getattr(final, "candidates", []) or [])
        if final is not None else 0,
        tokens=_pop_tokens(run_id),
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        gold_sql="",
        kb_hits=kb_hits,
        qid=item.qid,
        kb_rev=snapshot.get("rev", ""),
        kb_files=snapshot.get("files"),
    )
    # 参考 SQL 放 reference_sql,不放 gold_sql:避免 gate 的 gold_match
    # (SQL 文本结构比对)被读成准确率。
    entry.pop("gold_sql", None)
    entry.update({
        "source": item.source,
        "tier": tier_of(item),
        "reference_origin": item.reference_origin,
        "reference_sql": item.reference_sql,
        "evalset_rev": evalset_fingerprint,
        "match": item.match,
        "path": _path_of(final),
        "compile_meta": (getattr(final, "compile_meta", {}) or {})
        if final is not None else {},
        "selection": (getattr(final, "selection", {}) or {})
        if final is not None else {},
        "leakage_suspect": _leakage_suspect(kb_hits),
        "at": now_iso(),
    })
    if reference_error:
        entry["reference_error"] = reference_error
    if error:
        entry["error"] = error
    return entry


# ── 正式运行 ─────────────────────────────────────────────────


def _new_run_id(datasource: str, tier: str) -> str:
    """run 身份:时间戳 + 短随机后缀(排序友好且并发不撞名)。"""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"evalset-{datasource}-{tier}-{stamp}-{uuid.uuid4().hex[:6]}"


def _write_run(
    out_dir: Path, run_id: str, entries: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    """run 原始记录 + 派生摘要 + ``latest.*`` 原子副本。"""
    entries_text = "".join(
        json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries
    )
    summary_text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(out_dir / f"{run_id}.jsonl", entries_text)
    _atomic_write(out_dir / f"{run_id}.summary.json", summary_text)
    _atomic_write(out_dir / "latest.jsonl", entries_text)
    _atomic_write(out_dir / "latest.summary.json", summary_text)


def _build_summary(
    *,
    run_id: str,
    datasource: str,
    tier: str,
    entries: list[dict[str, Any]],
    skipped: int,
    reference_missing: int,
    evalset_fingerprint: str,
    kb_start: Any,
    kb_end: Any,
) -> dict[str, Any]:
    """summary = 设计 §10 的形状 + ``run_id/datasource/tier/at/degraded``。

    比率来自 ``metrics_from_entries``(与回归门同一口径);**计数值直接数
    条目** —— ``metrics_from_entries`` 只给比率,n 与分子分母计数由这里给。
    """
    metrics = metrics_from_entries(entries)
    ex_hit = sum(1 for e in entries if e.get("verdict") == "MATCH")
    ex_judged = sum(1 for e in entries if e.get("verdict") in JUDGED_VERDICTS)
    reference_errors = sum(
        1 for e in entries if e.get("verdict") == "GOLD_ERROR")
    leakage_suspect = sum(
        1 for e in entries if e.get("leakage_suspect"))

    start_rev = _rev_of(kb_start)
    end_rev = _rev_of(kb_end)
    changed = kb_end != kb_start
    degraded: list[dict[str, Any]] = []
    if changed:
        degraded.append({
            "code": "kb_changed_mid_run",
            "message": (
                "KB rev changed during the run "
                f"({start_rev or 'none'} -> {end_rev or 'none'}); "
                "scores are a mixed-version view"
            ),
        })

    return {
        "run_id": run_id,
        "datasource": datasource,
        "tier": tier,
        "at": now_iso(),
        "n": len(entries),
        "ex": metrics.get("ex", 0.0),
        "ex_hit": ex_hit,
        "ex_judged": ex_judged,
        "compile_hit": metrics.get("compile_hit", 0.0),
        "completion": metrics.get("completion", 0.0),
        "avg_tokens": metrics.get("avg_tokens"),
        "reference_errors": reference_errors,
        "leakage_suspect": leakage_suspect,
        "skipped": skipped,
        "reference_missing": reference_missing,
        "evalset_rev": evalset_fingerprint,
        "kb_rev": start_rev,
        "kb_rev_end": end_rev,
        "kb_rev_changed_mid_run": changed,
        "degraded": degraded,
    }


async def _compile_only(
    options: RunOptions,
    runnable: list[EvalsetItem],
    skipped: list[dict[str, Any]],
) -> RunResult:
    """零 LLM 资产自检:SQLGlot + (有 registry 时)试执行。不写 run 文件。"""
    entries: list[dict[str, Any]] = []
    for item in runnable:
        ok = sqlglot_ok(item.reference_sql)
        error = "" if ok else "sql_parse_failed"
        if ok and options.registry is not None:
            ok = await trial_ok(
                options.registry, options.datasource, item.reference_sql)
            if not ok:
                error = "trial_failed"
        row: dict[str, Any] = {"qid": item.qid, "ok": ok}
        if not ok:
            row["error"] = error
        entries.append(row)
    failed = sum(1 for row in entries if not row["ok"])
    checked = len(entries)
    summary = {
        "checked": checked,
        "failed": failed,
        "skipped": len(skipped),
    }
    return RunResult(
        exit_code=0 if failed == 0 else 2,
        tier=options.tier,
        summary=summary,
        entries=entries,
        skipped=len(skipped),
        message=f"compile-only: {checked - failed}/{checked} 通过",
    )


async def _run_formal(
    options: RunOptions,
    doc: EvalsetDoc,
    runnable: list[EvalsetItem],
    skipped: list[dict[str, Any]],
    kb_start: Any,
) -> RunResult:
    """逐题走真实管线并落盘。"""
    datasource = options.datasource
    if options.registry is None:
        # R21:参考 SQL 无法执行 → 没有任何题可判定;不写 run 文件。
        return RunResult(
            exit_code=2,
            tier=options.tier,
            summary={},
            entries=[],
            skipped=len(skipped),
            message=(
                "正式运行需要数据源连接器(registry):参考 SQL 无法执行,"
                "没有任何题可判定。请提供 registry,或使用 --compile-only。"
            ),
        )

    graph = options.graph
    if graph is None:
        if options.config is None or options.kb is None:
            return RunResult(
                exit_code=2,
                tier=options.tier,
                summary={},
                entries=[],
                skipped=len(skipped),
                message="正式运行需要 config 与 kb(或显式传入 graph)。",
            )
        graph = build_eval_graph(
            config=options.config,
            kb=options.kb,
            registry=options.registry,
            semantic_layer=options.semantic_layer,
        )

    run_id = _new_run_id(datasource, options.tier)
    fingerprint = evalset_rev(doc)
    reference_missing = sum(
        1 for item in runnable if not (item.reference_sql or "").strip())

    entries: list[dict[str, Any]] = []
    for index, item in enumerate(runnable):
        entries.append(await _judge_one(
            graph, item, index=index, run_id=run_id, options=options,
            kb_start=kb_start, evalset_fingerprint=fingerprint,
        ))

    kb_end = await _kb_rev(options.kb, datasource)
    summary = _build_summary(
        run_id=run_id,
        datasource=datasource,
        tier=options.tier,
        entries=entries,
        skipped=len(skipped),
        reference_missing=reference_missing,
        evalset_fingerprint=fingerprint,
        kb_start=kb_start,
        kb_end=kb_end,
    )
    out_dir = _run_root(options.project_root, datasource, options.tier)
    _write_run(out_dir, run_id, entries, summary)

    exit_code = 0
    if options.strict and (
        summary["reference_errors"] > 0 or summary["leakage_suspect"] > 0
    ):
        exit_code = 2
    message = (
        f"tier={options.tier}: n={summary['n']}, "
        f"EX {summary['ex_hit']}/{summary['ex_judged']}, "
        f"reference_errors={summary['reference_errors']}, "
        f"leakage_suspect={summary['leakage_suspect']}, "
        f"skipped={summary['skipped']}"
    )
    return RunResult(
        exit_code=exit_code,
        run_id=run_id,
        tier=options.tier,
        summary=summary,
        entries=entries,
        skipped=len(skipped),
        message=message,
    )


async def _run_locked(
    options: RunOptions, doc: EvalsetDoc, kb_start: Any,
) -> RunResult:
    """持有 KB init 锁后的完整流程:选题 → 护栏 → 正式跑。"""
    runnable, skipped = _select(doc, options)
    if not runnable and not skipped:
        # 该 tier 没有已确认的题:退出 2(与「资产解析失败」同档 ——
        # 没有任何可评分的题,一次"成功"的空跑只会让人误以为测过了)。
        return RunResult(
            exit_code=2,
            tier=options.tier,
            summary={},
            entries=[],
            skipped=0,
            message=(
                f"tier={options.tier} 没有已确认的题目;"
                "先确认验收集条目再运行。"
            ),
        )

    if options.compile_only:
        return await _compile_only(options, runnable, skipped)

    if not options.yes:
        model = str(getattr(options.config, "target", "") or "")
        return RunResult(
            exit_code=0,
            tier=options.tier,
            summary={},
            entries=[],
            skipped=len(skipped),
            message=(
                f"dry-run: tier={options.tier} 将运行 {len(runnable)} 题,"
                f"模型 {model or '未配置'};加 --yes 才会真正调用 LLM。"
            ),
        )

    return await _run_formal(options, doc, runnable, skipped, kb_start)


async def _run_evalset(options: RunOptions) -> RunResult:
    project_root = Path(options.project_root)

    if not isinstance(options.tier, str) or not _SAFE_SEGMENT.fullmatch(options.tier):
        return RunResult(
            exit_code=2, tier=str(options.tier),
            message=f"invalid tier {options.tier!r}",
        )

    try:
        doc, issues, exists = load_evalset(project_root, options.datasource)
    except EvalsetError as exc:
        return RunResult(
            exit_code=2, tier=options.tier,
            message=f"evalset asset invalid: {exc}",
        )

    if not exists:
        return RunResult(
            exit_code=2, tier=options.tier,
            message=(
                f"没有数据源 {options.datasource!r} 的验收集资产;"
                "先运行 evalset generate 或手工新建条目。"
            ),
        )
    if issues:
        return RunResult(
            exit_code=2, tier=options.tier,
            summary={"issues": [issue.as_dict() for issue in issues]},
            message=(
                "验收集资产有解析问题,拒绝运行: "
                + "; ".join(f"{i.path}: {i.code}" for i in issues[:5])
            ),
        )

    # KB 快照 + 跨进程互斥:锁内再取一次 start,确保快照与真正跑题的
    # 时段对齐(拿锁前可能刚有 KB 任务收尾)。
    await _ensure_synced(options.kb, options.datasource)
    lock = KbInitLock(_kb_dir(options) / ".locks")
    try:
        with lock.acquire(options.datasource):
            kb_start = await _kb_rev(options.kb, options.datasource)
            return await _run_locked(options, doc, kb_start)
    except KbInitBusy:
        return RunResult(
            exit_code=2,
            tier=options.tier,
            summary={},
            entries=[],
            skipped=0,
            message=(
                "KB 任务进行中(init/reload/distill):"
                "试跑会读到半份 KB,等任务结束后再跑。"
            ),
        )


async def run_evalset(options: RunOptions) -> RunResult:
    """执行一次手动 run(未预期异常 → 退出码 1,不把崩溃洗成判定)。"""
    try:
        return await _run_evalset(options)
    except Exception as exc:  # noqa: BLE001 — 未预期异常 → 1
        return RunResult(
            exit_code=1,
            tier=str(getattr(options, "tier", "")),
            message=f"unexpected error: {exc}",
        )
