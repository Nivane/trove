"""覆盖率体检端点(admin)。

一层薄壳:解析 multipart / 校验查询参数 / 把服务的 ``CoverageError`` 翻成
带稳定 ``code`` 的 HTTP 错误 / 给治理动作留痕。逐题判定、成本口径、草稿
门全在 ``services.question_coverage`` 里 —— 路由不重算任何计数。

三条边界与页面文案同源:

- **只读**:体检不执行 SQL、不读业务行(引擎的构造保证);本路由也不提供
  任何「重新执行」入口。
- **成本必须显式确认**:``POST /runs`` 不带 ``confirm_cost=true`` 一律 400,
  服务端不把「没勾」当默认同意。
- **草稿永不自动确认**:``POST /runs/{id}/drafts`` 只落 pending,确认仍在
  语义工作台由人完成。

审计只记运行元数据(run_id/计数/资产名),**不记问题原文** —— 审计面是
治理记录,不是问题库的第二份拷贝。
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)

from trove.api.deps import require_admin
from trove.api.schemas import (
    CoverageDraftsRequest,
    CoveragePreflightRequest,
    CoverageRunCreateRequest,
)
from trove.services.question_coverage.csv_input import (
    MAX_CSV_BYTES,
    CsvInputError,
    parse_csv,
)
from trove.services.question_coverage.service import (
    CoverageError,
    QuestionCoverageService,
)
from trove.services.question_coverage.store import ACTIVE_STATUSES, TERMINAL_STATUSES

router = APIRouter()

#: 历史筛选的合法状态值(闭集;未知值 400 而不是悄悄返回空列表 ——
#: 空列表与「没有这种状态」在页面上是两件事)。
_RUN_STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES


def _service(request: Request) -> QuestionCoverageService:
    service = getattr(request.app.state, "question_coverage", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "coverage_not_configured",
                "message": "question coverage service is not available in this process",
            },
        )
    return service


async def _call(coro: Any) -> Any:
    """把服务的 ``CoverageError`` 翻成 HTTPException(码 + 人话一起带)。"""
    try:
        return await coro
    except CoverageError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.to_dict()) from None


async def _audit(
    request: Request,
    action: str,
    user: dict,
    *,
    status: int,
    details: dict[str, Any] | None = None,
) -> None:
    """治理动作留痕(照 actions.py 的写法);细节只放元数据,不放问题原文。"""
    await request.app.state.auth.record_audit(
        action, user=user, method=request.method, path=request.url.path,
        status=status, details=details,
    )


def _csv_error(exc: CsvInputError) -> HTTPException:
    status = 413 if exc.code == "file_too_large" else 400
    return HTTPException(
        status_code=status,
        detail={"code": exc.code, "message": exc.detail or exc.code},
    )


# ── 预览与输入解析(零 LLM、不落库) ─────────────────────────────────────

@router.post("/admin/question-coverage/preflight")
async def preflight(
    body: CoveragePreflightRequest,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """确定性前段全跑一遍 → 题数分布、模型、成本估计、blockers。"""
    return await _call(
        _service(request).preflight(
            body.datasource, body.questions, actor=user.get("username", "")))


@router.post("/admin/question-coverage/parse-csv")
async def parse_csv_upload(
    request: Request,
    file: UploadFile = File(...),
    question_column: str = Form(""),
    user: dict = Depends(require_admin),
) -> dict:
    """CSV 字节 → 问题列表 + 数据质量报告(编码/表头/去重/截断)。

    只读一次上传、有界读取(``MAX_CSV_BYTES + 1``),不写任何文件。
    """
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "file_too_large",
                "message": f"file exceeds {MAX_CSV_BYTES} bytes",
            },
        )
    label = (file.filename or "csv").strip() or "csv"
    try:
        parsed = parse_csv(
            raw, question_column=question_column, source_label=label)
    except CsvInputError as exc:
        raise _csv_error(exc) from None
    return parsed.to_dict()


# ── 运行 ────────────────────────────────────────────────────────────────

@router.post("/admin/question-coverage/runs", status_code=202)
async def create_run(
    body: CoverageRunCreateRequest,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """成本确认后建 run 并启动后台体检(202;单数据源单活跑)。"""
    service = _service(request)
    payload = await _call(
        service.create_run(
            body.datasource,
            body.questions,
            source_label=body.source_label,
            confirm_cost=body.confirm_cost,
            actor=user.get("username", ""),
        ))
    await _audit(
        request, "coverage.run.create", user, status=202,
        details={
            "run_id": payload["run_id"],
            "datasource": payload["datasource"],
            "question_count": payload["question_count"],
            "model": payload["model"],
            "hard_call_cap": payload["hard_call_cap"],
            "semantic_digest": payload["semantic_digest"],
        })
    return payload


@router.get("/admin/question-coverage/runs")
async def list_runs(
    request: Request,
    datasource: str = Query(min_length=1),
    status: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_admin),
) -> dict:
    """历史运行(新 → 旧;summary 摘要 + 同源上一轮 done 指针)。"""
    if status and status not in _RUN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_status",
                "message": f"unknown run status: {status}",
                "allowed": list(_RUN_STATUSES),
            },
        )
    return await _call(
        _service(request).list_runs(
            datasource, status=status or None, limit=limit, offset=offset))


@router.get("/admin/question-coverage/runs/{run_id}/status")
async def run_status(
    run_id: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """轻轮询:状态/进度/各结局计数/已用调用/任务阶段。"""
    return await _call(_service(request).status(run_id))


@router.get("/admin/question-coverage/runs/{run_id}")
async def run_report(
    run_id: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """完整报告:run + summary + 全部逐题结果 + 聚合缺口。"""
    return await _call(_service(request).report(run_id))


@router.get("/admin/question-coverage/runs/{run_id}/diff")
async def run_diff(
    run_id: str,
    request: Request,
    base_run_id: str = Query(""),
    user: dict = Depends(require_admin),
) -> dict:
    """两轮对比;缺省 base = 同数据源上一轮 done(没有 → reason 说明)。"""
    return await _call(
        _service(request).diff(run_id, base_run_id=base_run_id))


@router.post("/admin/question-coverage/runs/{run_id}/drafts")
async def request_drafts(
    run_id: str,
    body: CoverageDraftsRequest,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """勾选缺口 → **pending** 语义草稿(服务端用当前模型重算规格)。"""
    payload = await _call(
        _service(request).draft_gaps(
            run_id, seqs=body.seqs, all=body.all,
            actor=user.get("username", ""),
        ))
    await _audit(
        request, "coverage.drafts.request", user, status=200,
        details={
            "run_id": run_id,
            "counts": payload["counts"],
            "assets": [
                f"{row.get('kind')}:{row.get('name')}"
                for row in payload["results"]
                if row.get("status") == "created"
            ][:50],
        })
    return payload


@router.post("/admin/question-coverage/runs/{run_id}/cancel", status_code=202)
async def cancel_run(
    run_id: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """协作取消:置 cancel_requested,循环在下一题前收尾(已完成保留)。"""
    payload = await _call(_service(request).cancel(run_id))
    await _audit(
        request, "coverage.run.cancel", user, status=202,
        details={"run_id": run_id, "status": payload["status"]})
    return payload
