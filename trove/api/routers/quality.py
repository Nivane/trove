"""``GET /v1/admin/quality/overview`` —— 质量运营台(admin,只读聚合)。

设计稿:``~/Downloads/trove-page-p4-ops.html`` §4.1(冻结契约)+ 2026-10-02
五项裁决。本端点只读文件/表/YAML,**零 LLM 零网络**:评测产物是文件、
运行记录是 ``audit_log``、词条评价是 KB 的 lessons.yml/examples.yml。

与 P3 ``overview.py`` 同一条纪律(设计稿 §4.6):
- 每条腿独立超时(``_SOURCE_TIMEOUT_S`` 1.5s),失败 → ``degraded`` 条目
  ``{block, source, error, at}``(error 只报异常类型名),受影响字段为
  ``null`` —— 整页绝不因一条腿失败而 500(本端点唯一不 503 的是它自己:
  质量域没有"存储"单点,文件缺失就是 ``null`` + degraded,这是合法状态)。
- 三个值各有各的意思,渲染端不得混用:``null`` = 没测到/不可用,
  ``0`` = 测到了且为零,``[]`` = 结果为空。

判定的**唯一权威是 ``trove/eval/gate.py`` 的纯函数**(``score_from_file`` /
``compare_metrics``):端点里不重写任何指标口径 —— 各写一遍必然漂移
(CLAUDE.md 记录过一次同文件算出两个 ex 的实例),``scripts/eval_gate.py
--json`` 与本端点在同组产物上必须逐项一致。

裁决落地(与设计稿正文冲突处以裁决为准):
- ① 评价票数(up/down/by_datasource)在本端点的 ``feedback`` 块;
- ② 失败清单**不按 qid 去重**,与 eval gate CLI 逐字节一致;
  重复 qid 通过 ``coverage.duplicate_qids`` 显式暴露,不静默吞掉;
- ④ 没有新的"运行终态表":运行读数 = ``audit_log`` + 消息投影(见 usage.py)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from trove.api.deps import require_admin
from trove.api.routers.overview import (
    _leg,
    _now_iso,
    _project_root,
)
from trove.eval.gate import compare_metrics, load_entries, score_from_file

router = APIRouter()

#: 当前评测产物(相对项目根)。eval_bird 每次运行 append 到这里(裁决⑤/§7:
#: 本轮不做 batch 概念,文件就是"最近一次运行"的账本)。
_CURRENT_RELPATH = Path(".trove") / "eval" / "results.jsonl"
#: 基线缺省值(与 ``EvalConfig.baseline_path`` 的默认一致;配置在时以配置为准)。
_DEFAULT_BASELINE_RELPATH = Path("eval") / "baseline" / "results.jsonl"

_FAILURES_LIMIT_DEFAULT = 50
_FAILURES_LIMIT_MAX = 200

#: 明确不测的口径 —— 不是"忘了",是清单本身(渲染端据此显示灰条而非 0)。
#: - failures.by_error_class:归因切片在 eval 侧已有,但审计写入时没有该列,
#:   方案见 usage.py 顶部注释(读侧投影,下轮);本轮不造假数据。
#: - feedback.trend:评价的时间序列需要按天聚合 updated_at,当前 KB 只有
#:   最后修改时间(覆盖一次就丢历史),没有可诚实渲染的序列。
_NOT_MEASURED = ["failures.by_error_class", "feedback.trend"]

#: 晋升的净赞成票门槛(promotion.py ``maybe_promote`` 的口径,此处只读展示)。
_PROMOTION_NET_UPVOTES = 3


# ── 小工具 ────────────────────────────────────────────────────


def _resolve(root: Path, rel: str) -> Path:
    """相对路径挂到项目根;绝对路径原样(与 CLI ``--baseline`` 的语义一致)。"""
    p = Path(rel)
    return p if p.is_absolute() else root / p


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


async def _read_artifact(path: Path) -> tuple[list[dict], dict]:
    """读一份产物:条目 + 指标(与门禁同一把尺子,双份都进线程池)。

    文件不存在 → ``FileNotFoundError``;存在但一条都解析不出 → ``ValueError``
    —— 对质量台而言"空文件"与"缺失"一样是不可判定的证据(不能拿它算 0)。
    """
    def _sync() -> tuple[list[dict], dict]:
        entries = load_entries(path)
        if not entries:
            if not path.exists():
                raise FileNotFoundError(str(path))
            raise ValueError(f"no parseable entries in {path}")
        return entries, score_from_file(path)

    return await asyncio.to_thread(_sync)


def _kind_of(entries: list[dict]) -> str:
    """产物类型:scorecard / eval_bird / replay(按写入方字段特征判定)。

    判据从两个写入方抄来而非猜的:scorecard 是 ``{"metrics": ...}`` 单条
    (``tune_rrf.py`` / ``eval_hybrid_retrieval.py``);eval_bird 条目带
    ``evidence``/``compile_meta``(``scripts/eval_bird.py::_result_entry``);
    replay 条目带 ``tokens``/``n_candidates``/``consensus``(``eval/replay.py``)。
    """
    first = entries[0] if entries else {}
    if isinstance(first, dict) and "metrics" in first:
        return "scorecard"
    if any("evidence" in e or "compile_meta" in e for e in entries):
        return "eval_bird"
    if any("tokens" in e or "n_candidates" in e or "consensus" in e for e in entries):
        return "replay"
    return "unknown"


def _batch_at(entries: list[dict]) -> str | None:
    """批次时刻 = 条目 run_id 尾段的 Unix 秒最大值(eval 运行号形如
    ``eval-3-1759...``);解析不出 → None(不拿 mtime 冒充:两者常差很远)。"""
    stamps: list[int] = []
    for e in entries:
        tail = str(e.get("run_id") or "").rsplit("-", 1)[-1]
        try:
            stamps.append(int(tail))
        except ValueError:
            continue
    if not stamps:
        return None
    try:
        return datetime.fromtimestamp(max(stamps), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _coverage(baseline_entries: list[dict], current_entries: list[dict]) -> dict | None:
    """当前产物对基线的 qid 覆盖 + 重复 qid 暴露(裁决②配套)。

    重复 qid **不去重**:评测可能对同一题跑了两次,清单按实际条目呈现;
    这里把重复的 qid 单列出来,让"看着少了一题"与"真的跑了两遍"可区分。
    任一侧无 qid 字段 → None(不可计算 ≠ 0)。
    """
    base_qids = {str(e.get("qid")) for e in baseline_entries if e.get("qid")}
    cur_qids = [str(e.get("qid")) for e in current_entries if e.get("qid")]
    if not base_qids or not cur_qids:
        return None
    seen: set[str] = set()
    dupes: set[str] = set()
    for q in cur_qids:
        if q in seen:
            dupes.add(q)
        seen.add(q)
    covered = len(base_qids & seen)
    return {
        "baseline_qids": len(base_qids),
        "covered": covered,
        "ratio": round(covered / len(base_qids), 4),
        "duplicate_qids": sorted(dupes),
    }


def _artifact_block(path: Path, entries: list[dict], metrics: dict) -> dict:
    n = metrics.get("n")
    n_judged = metrics.get("n_judged")
    return {
        "path": str(path),
        "kind": _kind_of(entries),
        "n": int(n) if n is not None else len(entries),
        "n_judged": int(n_judged) if n_judged is not None else None,
        "mtime": _mtime_iso(path),
        "batch_at": _batch_at(entries),
        "metrics": metrics or None,
    }


def _gate_not_concluded(reason: str, min_n: int, tolerances: dict) -> dict:
    return {
        "verdict": "not_concluded",
        "reason": reason,
        "min_n": min_n,
        "tolerances": tolerances,
        "metrics": [],
        "unpaired": [],
        "denominator_notes": [],
    }


def _gate_block(
    conf: dict, base_metrics: dict | None, cur_metrics: dict | None,
    *, baseline_ok: bool, current_ok: bool,
) -> dict:
    """判定块:判不了就明说判不了(not_concluded 是合法结论,不是错误)。

    排序与 CLI 一致:先看当前样本量门槛(min_n),再比;比完无任何可比指标
    (双方指标无交集)也归 not_concluded —— 输出一个空的 "pass" 会撒谎。
    """
    min_n = int(conf.get("min_n") or 0)
    tolerances = dict(conf.get("tolerances") or {})
    if not current_ok:
        return _gate_not_concluded("当前评测产物不可读(缺失或为空),无法对比", min_n, tolerances)
    if not baseline_ok:
        return _gate_not_concluded("基线产物不可读(缺失或为空),无法对比", min_n, tolerances)
    n_cur = int((cur_metrics or {}).get("n") or 0)
    if min_n and n_cur < min_n:
        return _gate_not_concluded(
            f"当前样本量 {n_cur} < 门槛 {min_n}(数据不足,不能下结论)", min_n, tolerances,
        )
    if not base_metrics and not cur_metrics:
        return _gate_not_concluded("两侧都没有可解析的指标", min_n, tolerances)
    report = compare_metrics(base_metrics or {}, cur_metrics or {}, tolerances=tolerances)
    if not report.metrics:
        return _gate_not_concluded("无可比指标(两侧指标无交集)", min_n, tolerances)
    metrics = [
        {
            "metric": m.metric,
            "baseline": m.baseline,
            "current": m.current,
            "delta": m.delta,
            "direction": m.direction,
            "tolerance": m.tolerance,
            "ok": m.ok,
            "note": m.note,
        }
        for m in report.metrics
    ]
    return {
        "verdict": "pass" if report.passed else "regress",
        "reason": None,
        "min_n": min_n,
        "tolerances": tolerances,
        "metrics": metrics,
        "unpaired": sorted(set(report.unpaired)),
        "denominator_notes": list(report.denominator_notes),
    }


def _failures_block(entries: list[dict], limit: int) -> dict:
    """失败清单:**逐条**(不按 qid 去重,裁决②);MATCH 之外全是失败项。

    与 CLI 同一判据(``verdict != "MATCH"``):CRASH 条目没有 path,按字面
    呈现为 ``UNKNOWN`` 桶 —— 归因口径在 eval 侧,这里不越权重判。
    """
    items: list[dict] = []
    by_verdict: dict[str, int] = {}
    by_path: dict[str, int] = {}
    total = 0
    for e in entries:
        verdict = str(e.get("verdict") or "").strip() or "UNKNOWN"
        if verdict == "MATCH":
            continue
        total += 1
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        path = str(e.get("path") or "").strip()
        by_path[path or "UNKNOWN"] = by_path.get(path or "UNKNOWN", 0) + 1
        if len(items) < limit:
            items.append({
                "qid": e.get("qid") or "",
                "question": e.get("question") or "",
                "verdict": verdict,
                "path": path or None,
                "error": str(e.get("error") or ""),
                "retries": int(e.get("retries") or e.get("retry_count") or 0),
                "pred_sql": e.get("pred_sql") or "",
                "gold_sql": e.get("gold_sql") or "",
                "run_id": e.get("run_id") or "",
            })
    return {
        "total": total,
        "by_verdict": by_verdict,
        "by_path": by_path,
        "items": items,
        "truncated": total > len(items),
    }


def _feedback_names(request: Request, kb: Any) -> list[str]:
    """反馈域的源清单:KB 目录(含只有 YAML 的源)+ 已注册源,去重排序。

    KB 目录优先 —— 评价数据只存在于那里;注册表是补集(新注册还没 init
    的源不该被漏掉,虽然它通常也没有词条)。
    """
    names: set[str] = set()
    try:
        names.update(d.name for d in kb._datasource_dirs())  # noqa: SLF001 — 与 /admin/datasources 同一手法
    except Exception:
        pass
    registry = getattr(request.app.state, "connector_registry", None)
    if registry is not None:
        try:
            names.update(info["name"] for info in registry.list_info())
        except Exception:
            pass
    return sorted(n for n in names if n)


async def _feedback_block(request: Request) -> dict:
    """KB 词条评价聚合(裁决①:票数在此块)。

    计数口径:lessons 的 upvotes/downvotes 直接求和(rate_lesson 每次 +/-1);
    ``last_rated_at`` = 有票词条的 ``updated_at`` 最大值 —— 是"最后一次
    修改"不是"最后一次点赞"(KB 不存分事件时间,不冒充)。
    """
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        raise RuntimeError("kb service not configured")
    await kb.ensure_synced(None)

    up = down = pending = confirmed = pending_examples = 0
    by_datasource: list[dict] = []
    last_rated_at: str | None = None
    for name in _feedback_names(request, kb):
        lessons = await kb.list_lessons(name, confirmed_only=False)
        ds_up = sum(int(ln.get("upvotes") or 0) for ln in lessons)
        ds_down = sum(int(ln.get("downvotes") or 0) for ln in lessons)
        up += ds_up
        down += ds_down
        pending += sum(1 for ln in lessons if not ln.get("confirmed"))
        confirmed += sum(1 for ln in lessons if ln.get("confirmed"))
        for ln in lessons:
            if int(ln.get("upvotes") or 0) or int(ln.get("downvotes") or 0):
                ts = str(ln.get("updated_at") or "")
                if ts and (last_rated_at is None or ts > last_rated_at):
                    last_rated_at = ts
        pending_examples += len(await kb.list_pending_examples(name))
        if ds_up or ds_down:
            by_datasource.append({"datasource": name, "up": ds_up, "down": ds_down})

    cfg = getattr(request.app.state, "config", None)
    mem = getattr(cfg, "memory", None)
    promotion_enabled = bool(getattr(mem, "promotion_enabled", False))
    threshold = getattr(mem, "promotion_threshold", None)
    return {
        "up": up,
        "down": down,
        "by_datasource": by_datasource,
        "pending_lessons": pending,
        "confirmed_lessons": confirmed,
        "pending_examples": pending_examples,
        "promotion_enabled": promotion_enabled,
        "promotion_threshold": float(threshold) if threshold is not None else None,
        "promotion_net_upvotes_min": _PROMOTION_NET_UPVOTES,
        "last_rated_at": last_rated_at,
    }


def _eval_conf(request: Request) -> dict:
    cfg = getattr(request.app.state, "config", None)
    ev = getattr(cfg, "eval", None)
    if ev is None:
        return {"baseline_path": str(_DEFAULT_BASELINE_RELPATH), "min_n": 0, "tolerances": {}}
    return {
        "baseline_path": str(getattr(ev, "baseline_path", "") or _DEFAULT_BASELINE_RELPATH),
        "min_n": int(getattr(ev, "min_n", 0) or 0),
        "tolerances": {str(k): str(v) for k, v in dict(getattr(ev, "tolerances", {}) or {}).items()},
    }


# ── 端点 ──────────────────────────────────────────────────────


@router.get("/admin/quality/overview")
async def admin_quality_overview(
    request: Request,
    failures_limit: int = Query(_FAILURES_LIMIT_DEFAULT, ge=1, le=_FAILURES_LIMIT_MAX),
    _admin: dict = Depends(require_admin),
) -> dict:
    """质量总览:评测产物 / 门禁判定 / 失败清单 / KB 反馈,一次取齐。"""
    generated_at = _now_iso()
    degraded: list[dict] = []
    root = _project_root(request)
    conf = _eval_conf(request)
    current_path = root / _CURRENT_RELPATH
    baseline_path = _resolve(root, conf["baseline_path"])

    cur = await _leg(
        "eval_artifacts", str(current_path), degraded,
        lambda: _read_artifact(current_path),
    )
    base = await _leg(
        "eval_artifacts", str(baseline_path), degraded,
        lambda: _read_artifact(baseline_path),
    )

    current = _artifact_block(current_path, cur[0], cur[1]) if cur else None
    baseline = _artifact_block(baseline_path, base[0], base[1]) if base else None
    if current is not None:
        # 覆盖率只对"当前 vs 基线"有意义;基线侧同形状但恒为 null。
        current["coverage"] = _coverage(base[0], cur[0]) if base else None
    if baseline is not None:
        baseline["coverage"] = None

    gate = _gate_block(
        conf,
        base[1] if base else None,
        cur[1] if cur else None,
        baseline_ok=base is not None,
        current_ok=cur is not None,
    )
    failures = _failures_block(cur[0], failures_limit) if cur else None
    feedback = await _leg("feedback", "kb", degraded, lambda: _feedback_block(request))

    return {
        "available": current is not None or baseline is not None,
        "generated_at": generated_at,
        "current": current,
        "baseline": baseline,
        "gate": gate,
        "failures": failures,
        "feedback": feedback,
        "not_measured": list(_NOT_MEASURED),
        "degraded": degraded,
    }
