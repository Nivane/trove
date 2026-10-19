"""覆盖率体检的编排层 —— preflight、成本确认、后台运行、取消、草稿。

**一次运行的生命周期**(状态机与 ``store.RUN_STATUSES`` 同一闭集):

``queued → running → done | error | cancelled | interrupted``

三条纪律:

- **成本前置且可拒**:``confirm_cost`` 必须是显式的 ``True``;开跑前
  ``preflight`` 用**同一批生产前段节点**逐题跑一遍确定性部分(零 LLM),
  给出会花多少调用(typical / 硬上限)与解析出的模型。硬上限在服务层
  二次拦截:每题 ``CountingLLM`` 封顶,运行级再按
  ``needs_plan × 6 + 10`` 记账 —— 触顶是**运行级失败**,保留已完成结果,
  绝不在用户不知道的情况下多花。
- **中断不伪造完成**:取消/超时/连续失败/进程崩溃都会把未处理的题补成
  ``skipped`` 行(带原因),``done`` 只属于「真跑完了」。
- **草稿永不自动生效**:``draft_gaps`` 只写 pending ``semantic_drafts.yml``
  (复用 :func:`candidates.create_specs` 的唯一写路径),从不调用
  ``confirm_draft``;且规格用**当前**语义模型重算,不信任 run 时的预览。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from trove.core.logging import get_logger
from trove.services.kb.init_tasks import InitTaskStore
from trove.services.question_coverage import taxonomy
from trove.services.question_coverage.csv_input import (
    MAX_QUESTION_CHARS,
    MAX_QUESTIONS,
    normalize_question,
)
from trove.services.question_coverage.engine import (
    QUERY_INTENTS,
    CoverageBudgetExceeded,
    CoverageEngine,
    Prepared,
    schema_map_from,
)
from trove.services.question_coverage.gaps import aggregate_gaps
from trove.services.question_coverage.store import (
    ACTIVE_STATUSES,
    MAX_RUNS_PER_DATASOURCE,
    STALE_AFTER_S,
    TERMINAL_STATUSES,
    CoverageRunActive,
    now_iso,
)
from trove.services.semantic_layer.candidates import candidate_specs, create_specs
from trove.services.semantic_layer.factory import build_provider

logger = get_logger(__name__)

#: 输入上限(与 csv_input 同一组数;API 直连时也按同一口径校验)。
MAX_CSV_BYTES = 1_048_576
__all__ = [
    "MAX_CSV_BYTES",
    "MAX_DRAFTS_PER_REQUEST",
    "MAX_QUESTIONS",
    "MAX_QUESTION_CHARS",
    "MAX_RUNS_PER_DATASOURCE",
    "QUESTION_TIMEOUT_S",
    "RUN_TIMEOUT_S",
    "STALE_AFTER_S",
    "CoverageError",
    "QuestionCoverageService",
]

#: 单题超时:超过即记一行 ``error:timeout``,不拖垮整轮。
QUESTION_TIMEOUT_S = 60
#: 整轮 deadline:到点保留已完成、剩余补 skipped(异步任务不能无限期占着闸)。
RUN_TIMEOUT_S = 1800
#: 单题模型调用硬上限(CountingLLM):JSON 模式回退 + 重规划都算在内。
PER_QUESTION_CALL_CAP = 6
#: 运行级上限的松弛量(覆盖每题固定开销之外的意外重试)。
BUDGET_SLACK = 10
#: 连续多少题 error → 视为运行环境坏了,整轮提前失败。
CONSECUTIVE_ERROR_LIMIT = 3
#: 单次「生成草稿」最多落多少条资产(防一次点击刷屏收件箱)。
MAX_DRAFTS_PER_REQUEST = 50

_CHANGE_STATUSES = ("improved", "regressed", "changed", "same", "not_comparable")


class CoverageError(ValueError):
    """带稳定错误码的业务错误(路由层按 ``status`` 与 ``code`` 映射 HTTP)。"""

    def __init__(self, code: str, message: str = "", status: int = 400,
                 **extra: Any) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = int(status)
        self.extra = dict(extra)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


class QuestionCoverageService:
    """一个 serve 实例一个服务;后台运行挂在私有任务注册表里。

    任务注册表**刻意私有**:KB init 的状态端点不过滤 kind,若共用模块单例,
    体检任务会被 KbView 误报成「KB init 进行中」。私有实例让两者彻底隔离。
    """

    def __init__(
        self,
        store: Any,
        *,
        kb: Any,
        connectors: Any,
        llm: Any,
        config: Any,
        skills: Any = None,
        engine_factory: Callable[..., Any] | None = None,
        root: str | Path | None = None,
        tasks: InitTaskStore | None = None,
        question_timeout_s: float = QUESTION_TIMEOUT_S,
        run_timeout_s: float = RUN_TIMEOUT_S,
        consecutive_error_limit: int = CONSECUTIVE_ERROR_LIMIT,
    ) -> None:
        self.store = store
        self.kb = kb
        self.connectors = connectors
        self.llm = llm
        self.config = config
        self.skills = skills
        self.root = root
        self.tasks = tasks if tasks is not None else InitTaskStore()
        self._engine_factory = engine_factory or self._default_engine
        self._background: dict[str, asyncio.Task] = {}
        self._run_limits: dict[str, dict[str, int]] = {}
        self.question_timeout_s = float(question_timeout_s)
        self.run_timeout_s = float(run_timeout_s)
        self.consecutive_error_limit = max(1, int(consecutive_error_limit))

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """启动时恢复崩溃留下的 running 行(→ interrupted + skipped)。"""
        await self.store.reconcile_stale()

    async def dispose(self) -> None:
        """先收掉后台运行,再释放存储(aiosqlite 线程不关会挂住退出)。"""
        pending = list(self._background.values())
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — 关闭路径尽力而为
                pass
        self._background.clear()
        await self.store.dispose()

    async def wait(self, run_id: str) -> None:
        """等待一次后台运行结束(测试/运维用;路由不调用)。"""
        task = self._background.get(run_id)
        if task is not None:
            await task

    # ── preflight(零 LLM) ──────────────────────────────────────────────

    async def preflight(self, datasource: str, questions: list[Any], *,
                        actor: str = "") -> dict[str, Any]:
        """确定性前段全跑一遍:给成本、模型、blockers 与当前 active run。

        这里**不消费任何模型调用**:``intent → parse_date → schema_linking →
        fast_match`` 全是确定性节点;计划抽取留到真正开跑。
        """
        await self.store.reconcile_stale()
        ds = self._require_datasource(datasource)
        qs = self._validate_questions(questions)

        adapter = await self._adapter(ds)
        try:
            schema = await adapter.get_schema()
        except Exception as exc:  # noqa: BLE001 — 连接/权限失败 = 不可达
            raise CoverageError(
                "datasource_unreachable",
                f"{ds}: {type(exc).__name__}: {exc}"[:300],
            ) from None
        provider = await build_provider(
            self.kb, self.config, ds, adapter, schema=schema, root=self.root)
        engine = self._engine_factory(
            datasource=ds, run_id="", user_id=actor or "")
        engine.configure(Prepared(
            adapter=adapter, schema=schema, provider=provider,
            schema_map=schema_map_from(schema),
        ))

        not_query = certified = unmatched = needs_plan = 0
        llm_questions = 0
        failed = 0
        for question in qs:
            try:
                det = await engine.deterministic(question)
            except Exception:  # noqa: BLE001 — 单题确定性失败:如实计数,不 500
                failed += 1
                continue
            if det.intent not in QUERY_INTENTS:
                not_query += 1
                continue
            llm_questions += 1
            if det.certified:
                certified += 1
            elif det.unmatched or det.no_model:
                unmatched += 1
            else:
                needs_plan += 1

        blockers: list[dict[str, Any]] = []
        if provider is None or self._provider_model(provider) is None:
            blockers.append({
                "code": "no_semantic_model",
                "message": "semantic model is not initialized for this datasource",
                "href": f"/admin/kb?ds={_quote(ds)}",
            })
        warnings: list[str] = []
        if failed:
            warnings.append("preflight_errors")

        active = await self.store.active_run(ds)
        hard_cap = needs_plan * PER_QUESTION_CALL_CAP + BUDGET_SLACK
        return {
            "datasource": ds,
            "question_count": len(qs),
            "llm_question_count": llm_questions,
            "not_query": not_query,
            "certified": certified,
            "unmatched": unmatched,
            "needs_plan": needs_plan,
            "failed": failed,
            "model": engine.model if hasattr(engine, "model") else "",
            "model_source": (
                engine.model_source if hasattr(engine, "model_source") else ""),
            "estimated_calls": {"typical": needs_plan, "cap": hard_cap},
            "hard_call_cap": hard_cap,
            "semantic_digest": self._semantic_digest(ds),
            "blockers": blockers,
            "warnings": warnings,
            "active_run_id": active["run_id"] if active else "",
        }

    # ── 运行 ────────────────────────────────────────────────────────────

    async def create_run(
        self,
        datasource: str,
        questions: list[Any],
        *,
        source_label: str = "",
        confirm_cost: Any = False,
        actor: str = "",
    ) -> dict[str, Any]:
        """成本确认后建 run 并启动后台任务(202 的载荷)。"""
        if confirm_cost is not True:
            raise CoverageError(
                "confirm_cost_required",
                "cost estimate must be explicitly confirmed before starting",
            )
        preview = await self.preflight(datasource, questions, actor=actor)
        ds = preview["datasource"]
        for blocker in preview["blockers"]:
            if blocker["code"] == "no_semantic_model":
                raise CoverageError(
                    "no_semantic_model", blocker["message"], 400,
                    href=blocker.get("href", ""))
        if preview["active_run_id"]:
            raise CoverageError(
                "coverage_run_active",
                "a coverage run is already active for this datasource",
                409, run_id=preview["active_run_id"],
            )

        qs = self._validate_questions(questions)
        run_id = uuid.uuid4().hex[:16]
        hard_cap = int(preview["hard_call_cap"])
        task = self.tasks.create(ds, kind="coverage")
        if task is None:
            active = await self.store.active_run(ds)
            raise CoverageError(
                "coverage_run_active",
                "a coverage run is already active for this datasource",
                409, run_id=active["run_id"] if active else "",
            )
        try:
            await self.store.create_run({
                "run_id": run_id,
                "datasource": ds,
                "status": "queued",
                "created_by": actor,
                "created_at": now_iso(),
                "question_count": len(qs),
                "model": preview["model"],
                "semantic_digest": preview["semantic_digest"],
                "taxonomy_version": taxonomy.TAXONOMY_VERSION,
                "questions": qs,
                "summary": {
                    "source_label": str(source_label or ""),
                    "limits": {
                        "typical_calls": int(preview["estimated_calls"]["typical"]),
                        "hard_call_cap": hard_cap,
                    },
                },
                "task_id": task["id"],
            })
        except CoverageRunActive as exc:
            self.tasks.fail(task["id"], "another coverage run became active")
            raise CoverageError(
                "coverage_run_active",
                "a coverage run is already active for this datasource",
                409, run_id=exc.run_id,
            ) from None
        except Exception:
            self.tasks.fail(task["id"], "failed to persist coverage run")
            raise

        self._run_limits[run_id] = {
            "hard_call_cap": hard_cap,
            "typical_calls": int(preview["estimated_calls"]["typical"]),
        }
        background = asyncio.create_task(self._run(run_id))
        self._background[run_id] = background
        self.tasks.bind_background(task["id"], background)
        background.add_done_callback(
            lambda _t, rid=run_id: self._background.pop(rid, None))
        return {
            "run_id": run_id,
            "task_id": task["id"],
            "status": "queued",
            "datasource": ds,
            "question_count": len(qs),
            "model": preview["model"],
            "estimated_calls": dict(preview["estimated_calls"]),
            "hard_call_cap": hard_cap,
            "semantic_digest": preview["semantic_digest"],
        }

    async def _run(self, run_id: str) -> None:
        run = await self.store.get_run(run_id)
        if run is None:
            return
        questions = [str(q) for q in (run.get("questions") or [])]
        ds = str(run["datasource"])
        task_id = str(run.get("task_id") or "")
        limits = self._run_limits.pop(run_id, None) or {
            "hard_call_cap": len(questions) * PER_QUESTION_CALL_CAP + BUDGET_SLACK,
            "typical_calls": 0,
        }
        hard_cap = int(limits["hard_call_cap"])
        engine = self._engine_factory(
            datasource=ds, run_id=run_id, user_id=str(run.get("created_by") or ""))
        started = time.monotonic()
        total_calls = 0
        error_streak = 0

        status = "done"
        skipped_reason = ""
        run_error = ""
        try:
            await self.store.mark_started(run_id)
            seq = 0
            while seq < len(questions):
                current = await self.store.get_run(run_id)
                if current is None:  # 被删了:无事可做
                    return
                if current.get("cancel_requested"):
                    status, skipped_reason = "cancelled", "cancelled"
                    break
                if time.monotonic() - started > self.run_timeout_s:
                    status, skipped_reason = "error", "run_timeout"
                    run_error = f"run timeout after {int(self.run_timeout_s)}s"
                    break
                try:
                    result = await asyncio.wait_for(
                        engine.check(questions[seq], seq=seq),
                        timeout=self.question_timeout_s,
                    )
                except asyncio.TimeoutError:
                    result = _blank_result(
                        seq, questions[seq], "timeout",
                        {"error": f"question timeout after "
                                  f"{int(self.question_timeout_s)}s",
                         "timeout_s": int(self.question_timeout_s)},
                    )
                except CoverageBudgetExceeded as exc:
                    status, skipped_reason = "error", "llm_budget_exceeded"
                    run_error = f"LLM call cap reached ({exc.cap})"
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — 单题失败折成一行 error
                    result = _blank_result(
                        seq, questions[seq], f"node_error:{type(exc).__name__}",
                        {"error": f"{type(exc).__name__}: {exc}"[:500]},
                    )
                await self.store.add_result(
                    run_id, result, processed_count=seq + 1)
                total_calls += int(result.get("llm_calls") or 0)
                self._progress_task(
                    task_id, seq + 1, len(questions), result)

                if total_calls > hard_cap:
                    status, skipped_reason = "error", "llm_budget_exceeded"
                    run_error = f"run LLM call cap exceeded ({hard_cap})"
                    break
                if result.get("outcome") == "error":
                    error_streak += 1
                    if error_streak >= self.consecutive_error_limit:
                        status, skipped_reason = "error", "run_error"
                        run_error = (
                            f"{error_streak} consecutive questions failed: "
                            f"{result.get('outcome_reason') or 'error'}")
                        break
                else:
                    error_streak = 0
                seq += 1
        except asyncio.CancelledError:
            await self._safe_finalize(
                run_id, questions, status="interrupted",
                skipped_reason="interrupted",
                error="interrupted: task cancelled", task_id=task_id,
                base_summary=run.get("summary"))
            raise
        except Exception as exc:  # noqa: BLE001 — 运行级异常必须落成终态
            logger.exception("coverage run %s failed", run_id)
            await self._safe_finalize(
                run_id, questions, status="error", skipped_reason="run_error",
                error=f"{type(exc).__name__}: {exc}"[:500], task_id=task_id,
                base_summary=run.get("summary"))
            return

        await self._safe_finalize(
            run_id, questions, status=status,
            skipped_reason=skipped_reason, error=run_error, task_id=task_id,
            base_summary=run.get("summary"),
        )

    # ── 取消 / 读取 ─────────────────────────────────────────────────────

    async def cancel(self, run_id: str) -> dict[str, Any]:
        await self.store.reconcile_stale()
        run = await self.store.get_run(run_id)
        if run is None:
            raise CoverageError("run_not_found", f"coverage run not found: {run_id}", 404)
        if run["status"] not in ACTIVE_STATUSES:
            raise CoverageError(
                "run_not_active", f"run is {run['status']}, not active", 409)
        if not await self.store.request_cancel(run_id):
            raise CoverageError(
                "run_not_active", "run finished before cancel took effect", 409)
        return {"run_id": run_id, "status": "cancelling"}

    async def status(self, run_id: str) -> dict[str, Any]:
        """轻轮询:进度、各结局计数、已用调用、心跳、任务阶段。"""
        await self.store.reconcile_stale()
        run = await self.store.get_run(run_id)
        if run is None:
            raise CoverageError("run_not_found", f"coverage run not found: {run_id}", 404)
        counts = await self.store.outcome_counts(run_id)
        task = self.tasks.get(run["task_id"]) if run.get("task_id") else None
        payload: dict[str, Any] = {
            "run_id": run["run_id"],
            "datasource": run["datasource"],
            "status": run["status"],
            "created_by": run["created_by"],
            "created_at": run["created_at"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "heartbeat_at": run["heartbeat_at"],
            "cancel_requested": run["cancel_requested"],
            "question_count": run["question_count"],
            "processed_count": run["processed_count"],
            "model": run["model"],
            "semantic_digest": run["semantic_digest"],
            "taxonomy_version": run["taxonomy_version"],
            "error": run["error"],
            "counts": {o: int(counts.get(o, 0)) for o in taxonomy.OUTCOMES},
            "llm_calls_total": await self.store.llm_calls_total(run_id),
            "task": {
                "status": task.get("status"),
                "stage": task.get("stage"),
                "progress": task.get("progress"),
                "detail": task.get("detail"),
                "error": task.get("error"),
            } if task else None,
        }
        if run["status"] in TERMINAL_STATUSES:
            payload["summary"] = run.get("summary") or {}
        return payload

    async def report(self, run_id: str) -> dict[str, Any]:
        """完整报告:run 元数据 + 汇总 + 全部 results + 聚合缺口。"""
        await self.store.reconcile_stale()
        run = await self.store.get_run(run_id)
        if run is None:
            raise CoverageError("run_not_found", f"coverage run not found: {run_id}", 404)
        results = await self.store.get_results(run_id)
        summary = run.get("summary") or taxonomy.aggregate(results)
        return {
            "run": run,
            "summary": summary,
            "results": results,
            "gap_groups": aggregate_gaps(
                results, model=self._current_model(run["datasource"]),
                datasource=run["datasource"]),
        }

    async def list_runs(self, datasource: str, *, status: str | None = None,
                        limit: int = 20, offset: int = 0) -> dict[str, Any]:
        await self.store.reconcile_stale()
        ds = self._require_datasource(datasource)
        runs = await self.store.list_runs(
            ds, status=status, limit=limit, offset=offset)
        for run in runs:
            run.pop("questions", None)
            run["previous_done_run_id"] = ""
            if run["status"] == "done":
                previous = await self.store.previous_done_run(
                    ds, run["created_at"])
                run["previous_done_run_id"] = (
                    previous["run_id"] if previous else "")
        return {
            "datasource": ds,
            "runs": runs,
            "limit": int(limit),
            "offset": int(offset),
            "has_more": len(runs) >= int(limit),
        }

    async def diff(self, run_id: str, *, base_run_id: str = "") -> dict[str, Any]:
        """两轮对比:summary 差值 + 逐题方向;语义模型变过要点明。"""
        await self.store.reconcile_stale()
        run = await self.store.get_run(run_id)
        if run is None:
            raise CoverageError("run_not_found", f"coverage run not found: {run_id}", 404)
        if run["status"] in ACTIVE_STATUSES:
            raise CoverageError("run_not_finished", "run is still active", 409)

        base: dict[str, Any] | None
        if base_run_id:
            if base_run_id == run_id:
                raise CoverageError("diff_same_run", "base and current are the same run")
            base = await self.store.get_run(base_run_id)
            if base is None:
                raise CoverageError(
                    "base_run_not_found", f"base run not found: {base_run_id}", 404)
            if base["datasource"] != run["datasource"]:
                raise CoverageError(
                    "diff_datasource_mismatch",
                    "base and current runs belong to different datasources")
            if base["status"] in ACTIVE_STATUSES:
                raise CoverageError(
                    "base_run_not_finished", "base run is still active", 409)
        else:
            base = await self.store.previous_done_run(
                run["datasource"], run["created_at"])

        if base is None:
            return {
                "run_id": run_id,
                "base_run_id": "",
                "reason": "no_previous_run",
                "matched": 0,
                "base_only": 0,
                "current_only": 0,
                "changes": {k: 0 for k in _CHANGE_STATUSES},
                "questions": [],
                "summary_delta": {},
                "semantic_changed": False,
                "taxonomy_changed": False,
            }

        run = dict(run)
        base = dict(base)
        run["results"] = await self.store.get_results(run_id)
        base["results"] = await self.store.get_results(base["run_id"])
        body = taxonomy.diff(base, run)
        run_summary = run.get("summary") or taxonomy.aggregate(run["results"])
        base_summary = base.get("summary") or taxonomy.aggregate(base["results"])
        body.update({
            "run_id": run_id,
            "base_run_id": base["run_id"],
            "reason": "",
            "base_created_at": base.get("created_at"),
            "base_semantic_digest": base.get("semantic_digest") or "",
            "semantic_digest": run.get("semantic_digest") or "",
            "semantic_changed": bool(
                base.get("semantic_digest") and run.get("semantic_digest")
                and base["semantic_digest"] != run["semantic_digest"]),
            "base_taxonomy_version": base.get("taxonomy_version"),
            "taxonomy_version": run.get("taxonomy_version"),
            "taxonomy_changed": bool(
                base.get("taxonomy_version") and run.get("taxonomy_version")
                and base["taxonomy_version"] != run["taxonomy_version"]),
            "summary_delta": taxonomy.summary_delta(base_summary, run_summary),
        })
        return body

    # ── 缺口 → pending 草稿 ────────────────────────────────────────────

    async def draft_gaps(
        self, run_id: str, *, seqs: list[int] | None = None,
        all: bool = False, actor: str = "",
    ) -> dict[str, Any]:
        """勾选的缺口 → pending 语义草稿(当前模型重算;从不确认)。"""
        await self.store.reconcile_stale()
        run = await self.store.get_run(run_id)
        if run is None:
            raise CoverageError("run_not_found", f"coverage run not found: {run_id}", 404)
        if run["status"] in ACTIVE_STATUSES:
            raise CoverageError("run_not_finished", "run is still active", 409)
        if all and seqs:
            raise CoverageError("no_selection", "give either seqs or all, not both")
        results = await self.store.get_results(run_id)
        if all:
            selected = results
        elif seqs:
            if len(seqs) > MAX_DRAFTS_PER_REQUEST:
                raise CoverageError(
                    "too_many_seqs",
                    f"at most {MAX_DRAFTS_PER_REQUEST} sequences per request", 422)
            wanted = sorted({int(s) for s in seqs})
            known = {r["seq"] for r in results}
            unknown = [s for s in wanted if s not in known]
            if unknown:
                raise CoverageError(
                    "unknown_seqs", f"unknown sequences: {unknown[:20]}", 400,
                    seqs=unknown[:20])
            chosen = set(wanted)
            selected = [r for r in results if r["seq"] in chosen]
        else:
            raise CoverageError("no_selection", "choose at least one question")

        ds = run["datasource"]
        model = self._current_model(ds)
        dialect = await self._dialect(ds)

        entries: list[dict[str, Any]] = []
        spec_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for result in selected:
            for gap in result.get("gaps") or []:
                if not isinstance(gap, dict):
                    continue
                miss: dict[str, Any] = {
                    "reason": str(gap.get("reason") or ""),
                    "component": str(gap.get("component") or ""),
                }
                if gap.get("value"):
                    miss["value"] = gap["value"]
                specs = candidate_specs([miss], model)
                if not specs:
                    status = "stale" if gap.get("draftable") else "not_draftable"
                    entries.append({"row": _gap_row(result, gap, status)})
                    continue
                for spec in specs:
                    key = (str(spec["kind"]), str(spec["name"]))
                    spec_by_key.setdefault(
                        key, {**spec, "question": result["question"]})
                    entries.append({
                        "key": key,
                        "row": {
                            "seq": result["seq"],
                            "question": result["question"],
                            "kind": key[0],
                            "name": key[1],
                            "reason": str(spec.get("reason") or ""),
                            "label": gap.get("label") or {},
                        },
                    })

        outcomes = await create_specs(
            self.kb, ds, list(spec_by_key.values()),
            actor=actor, note_prefix="coverage",
            max_candidates=MAX_DRAFTS_PER_REQUEST, validate=True,
            dialect=dialect,
        ) if spec_by_key else []
        outcome_by_key = {
            (o["kind"], o["name"]): o for o in outcomes
        }

        resolved: list[dict[str, Any]] = []
        for entry in entries:
            key = entry.get("key")
            if key is None:
                resolved.append(entry["row"])
                continue
            outcome = outcome_by_key.get(key)
            if outcome is None:
                # 超过单次资产上限,未被处理:不伪造状态,顶部 truncated 说明。
                continue
            resolved.append({
                **entry["row"],
                "status": outcome["status"],
                "draft_id": outcome.get("draft_id") or "",
                "errors": list(outcome.get("errors") or []),
            })
        truncated_assets = sum(
            1 for key in spec_by_key if key not in outcome_by_key)
        counts = {
            status: sum(1 for row in resolved if row.get("status") == status)
            for status in ("created", "exists", "invalid", "not_draftable", "stale")
        }
        return {
            "run_id": run_id,
            "datasource": ds,
            "selected_questions": len(selected),
            "requested": len(entries),
            "truncated": truncated_assets > 0,
            "truncated_assets": truncated_assets,
            "counts": counts,
            **counts,
            "results": resolved,
        }

    # ── 内部:终态 / 进度 / 摘要 ────────────────────────────────────────

    async def _safe_finalize(
        self, run_id: str, questions: list[str], *, status: str,
        skipped_reason: str, error: str, task_id: str,
        base_summary: Any,
    ) -> None:
        try:
            await self._finalize(
                run_id, questions, status=status,
                skipped_reason=skipped_reason, error=error,
                task_id=task_id, base_summary=base_summary,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 收尾尽力而为;stale 恢复兜底
            logger.exception("coverage run %s finalize failed", run_id)

    async def _finalize(
        self, run_id: str, questions: list[str], *, status: str,
        skipped_reason: str, error: str, task_id: str,
        base_summary: Any,
    ) -> None:
        if status in ACTIVE_STATUSES:
            raise ValueError(f"not a terminal status: {status!r}")
        run = await self.store.get_run(run_id)
        if run is None:
            return
        done = await self.store.done_seqs(run_id)
        remaining = [s for s in range(len(questions)) if s not in done]
        if remaining and skipped_reason:
            await self.store.append_skipped(
                run_id, remaining, reason=skipped_reason)
        results = await self.store.get_results(run_id)

        summary = taxonomy.aggregate(results)
        base = base_summary if isinstance(base_summary, dict) else {}
        summary["source_label"] = str(base.get("source_label") or "")
        summary["limits"] = dict(base.get("limits") or {})
        summary["llm_calls_total"] = sum(
            int(r.get("llm_calls") or 0) for r in results)

        warnings = list(summary.get("warnings") or [])
        if status == "interrupted":
            warnings.append("interrupted")
        digest_now = self._semantic_digest(run["datasource"])
        if (digest_now and run.get("semantic_digest")
                and digest_now != run["semantic_digest"]):
            warnings.append("semantic_changed_during_run")
        model_now = self._configured_model()
        if (model_now and run.get("model") and model_now != run["model"]):
            warnings.append("model_changed_during_run")
        summary["warnings"] = _dedupe(warnings)

        await self.store.finish_run(
            run_id, status=status, summary=summary, error=error,
            processed_count=len(questions),
        )
        try:
            await self.store.purge_old_runs(run["datasource"])
        except Exception:  # noqa: BLE001 — 清退失败不影响这次结果
            logger.warning("coverage purge failed for %s", run["datasource"])

        if task_id:
            if status == "done":
                coverage = (summary.get("metrics") or {}).get(
                    "deterministic_coverage") or {}
                text = f"{len(questions)} 题完成"
                if coverage.get("den"):
                    text += f"，可确定性覆盖 {coverage['num']}/{coverage['den']}"
                self.tasks.done(task_id, text)
            elif status == "cancelled":
                self.tasks.fail(
                    task_id,
                    f"cancelled after {len(results)}/{len(questions)}")
            else:
                self.tasks.fail(task_id, error or status)

    def _progress_task(self, task_id: str, processed: int, total: int,
                       result: dict[str, Any] | None) -> None:
        if not task_id:
            return
        percent = int(processed * 100 / total) if total else 100
        detail = f"{processed}/{total}"
        if result is not None:
            detail += f" · {result.get('outcome') or ''}"
        self.tasks.update(
            task_id, stage="running" if processed < total else "finishing",
            progress=min(99, percent) if processed < total else 99,
            detail=detail,
        )

    # ── 内部:模型 / 路径 / 校验 ────────────────────────────────────────

    def _default_engine(self, *, datasource: str, run_id: str,
                        user_id: str = "") -> CoverageEngine:
        return CoverageEngine(
            kb=self.kb, connectors=self.connectors, llm=self.llm,
            config=self.config, datasource=datasource, run_id=run_id,
            user_id=user_id, skills=self.skills, root=self.root,
            max_llm_calls=PER_QUESTION_CALL_CAP,
        )

    def _configured_model(self) -> str:
        try:
            from trove.workflow.nodes.query_sketch import resolve_sketch_model
            return str(resolve_sketch_model(self.config)[0] or "")
        except Exception:  # noqa: BLE001 — 配置读失败只影响提示,不影响结果
            return ""

    def _semantic_digest(self, datasource: str) -> str:
        try:
            path = self.kb.semantics_path(datasource)
            if not Path(path).exists():
                return ""
            return "sha256:" + hashlib.sha256(
                Path(path).read_bytes()).hexdigest()
        except Exception:  # noqa: BLE001 — 摘要读不到 = 空串(不冒充「没变」)
            return ""

    def _current_model(self, datasource: str) -> Any:
        try:
            from trove.services.semantic_layer.manage import SemanticManager
            return SemanticManager(self.kb).model(datasource)
        except Exception:  # noqa: BLE001 — 读不到模型 = 不可草稿(诚实降级)
            return None

    async def _dialect(self, datasource: str) -> str | None:
        try:
            adapter = await self.connectors.get(datasource)
            return str(adapter.dialect() or "") or None
        except Exception:  # noqa: BLE001
            return None

    async def _adapter(self, datasource: str) -> Any:
        try:
            return await self.connectors.get(datasource)
        except Exception as exc:  # noqa: BLE001 — 未知/未注册 → 400 专用码
            raise CoverageError(
                "datasource_not_found",
                f"{datasource}: {type(exc).__name__}: {exc}"[:300],
            ) from None

    @staticmethod
    def _provider_model(provider: Any) -> Any:
        try:
            return provider.model()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _require_datasource(datasource: Any) -> str:
        ds = str(datasource or "").strip()
        if not ds:
            raise CoverageError("datasource_required", "datasource is required")
        return ds

    @staticmethod
    def _validate_questions(questions: Any) -> list[str]:
        if isinstance(questions, str) or questions is None:
            raise CoverageError("no_questions", "questions must be a list")
        cleaned: list[str] = []
        for item in questions:
            text = normalize_question(item)
            if not text:
                continue
            if len(text) > MAX_QUESTION_CHARS:
                raise CoverageError(
                    "question_too_long",
                    f"question exceeds {MAX_QUESTION_CHARS} characters", 422)
            cleaned.append(text)
        if not cleaned:
            raise CoverageError("no_questions", "no non-empty questions")
        if len(cleaned) > MAX_QUESTIONS:
            raise CoverageError(
                "too_many_questions",
                f"at most {MAX_QUESTIONS} questions per run", 422)
        return cleaned


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(str(value), safe="")


def _dedupe(values: list[str]) -> list[str]:
    """保序去重(输出形状不随字典/集合迭代顺序漂移)。"""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _blank_result(seq: int, question: str, reason: str,
                  detail: dict[str, Any]) -> dict[str, Any]:
    """运行层单题失败(超时/节点异常)的 result 行 —— 与引擎行同形。"""
    return {
        "seq": int(seq),
        "question": str(question or ""),
        "outcome": "error",
        "outcome_reason": str(reason or "error"),
        "intent": "query",
        "plan_typed": False,
        "miss_class": "",
        "miss_reason": "",
        "miss_component": "",
        "template_name": "",
        "llm_calls": 0,
        "elapsed_ms": 0,
        "gaps": [],
        "detail": dict(detail or {}),
    }


def _gap_row(result: dict[str, Any], gap: dict[str, Any],
             status: str) -> dict[str, Any]:
    return {
        "seq": result.get("seq"),
        "question": result.get("question") or "",
        "kind": str(gap.get("kind") or "other"),
        "name": str(gap.get("component") or gap.get("reason") or ""),
        "reason": str(gap.get("reason") or ""),
        "label": gap.get("label") or {},
        "status": status,
        "draft_id": "",
        "errors": [],
    }
