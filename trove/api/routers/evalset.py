"""验收集(evalset)管理端点 —— 生成 / 确认门 / 就绪视图 / 试跑 / 预览。

全部挂在 ``/v1/admin/datasources/{name}/evalset`` 下,全部 ``require_admin``
(验收集决定「什么算对」,是治理动作而非查询动作)。本模块只做 **HTTP 形状
与后台任务编排** —— 确认门、终态裁决、脱敏与就绪三态都在
``services/evalset/service.py``,这里不复制一份会漂移的判定:

- 生成与试跑是**进程内**后台任务,复用 ``services/kb/init_tasks`` 的注册表
  (与 ``/kb init`` 同一套 202 + 轮询),状态端点按 ``kind`` 过滤 —— 刚跑完
  的 init 任务不能被读成本类任务的完成态;
- 审计:服务层的写操作自持 audit 回调(create/edit/confirm/reject/vouch/
  retire);只有 ``evalset.generate`` / ``evalset.run`` 这两个**服务不审计**
  的动作在任务收尾时补一条;
- 读不到 run 存储 → ``latest_verdict`` 为 ``None`` + 一条去重的 ``degraded``,
  绝不洗成 0 / 空对象(「读不到」不是「没有结论」);
- **声明顺序是承重的**:静态路由(``/readiness`` / ``/runs`` / ``/items`` /
  ``/generate`` / ``/run`` / ``/confirm``)与 status 端点全部声明在
  ``/{qid}`` 之前 —— Starlette 按声明顺序匹配,``/{qid}`` 会吃掉一切
  同方法的单段路径;
- ``EvalsetError.code`` 决定 HTTP 状态(404 / 409 / 403 / 400),响应体始终
  是 ``{code, message, issues}``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import get_principal, require_admin
from trove.api.routers.admin import _ds_id_or_404
from trove.api.schemas import (
    EvalsetConfirmBatch,
    EvalsetConfirmRequest,
    EvalsetGenerateRequest,
    EvalsetItemCreate,
    EvalsetItemPatch,
    EvalsetReasonBody,
    EvalsetRunRequest,
    EvalsetVouchBody,
)
from trove.eval.evalset_run import (
    RunOptions,
    RunStoreUnreadable,
    read_latest_entries,
    read_latest_summary,
    run_evalset,
)
from trove.services.evalset.generate import (
    GENERATE_SOURCES,
    GenerateRequest,
    generate_evalset,
)
from trove.services.evalset.lock import EvalsetBusy
from trove.services.evalset.service import EvalsetError, EvalsetService
from trove.services.kb.init_tasks import init_tasks

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/datasources/{name}/evalset", tags=["evalset"],
)

#: 打分集合的三个 tier(与 ``services.evalset.models.tier_of`` 同源;
#: benchmark 题只作对照,从不进入本数据源自己的验收集)。
_TIERS = ("verified", "semantic", "synthetic")

#: 历史 run 列表一次最多返回多少条(按 ``at`` 降序)。
_RUNS_LIMIT = 20


# ── helpers ──────────────────────────────────────────────


def _root(request: Request) -> Path:
    """项目根 = ``<root>/.trove/kb`` 的上两级(服务层约定的同一推导)。"""
    return Path(request.app.state.kb.kb_dir).parent.parent


def _actor(user: dict[str, Any]) -> str:
    return str(user.get("username") or user.get("id") or "admin")


async def _audit(
    request: Request, action: str, user: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> None:
    """审计写入(best-effort:审计面读不成不能把已完成的任务报成失败)。

    R18:不复用 ``kb.py::_audit`` —— 那只接受 ``status`` 位置参数且没有
    上抛保护;本模块的调用点在**后台任务收尾**,一次审计故障不应改写
    任务结局。
    """
    try:
        await request.app.state.auth.record_audit(
            action, user=user, method=request.method,
            path=request.url.path, status=None, details=details,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort,只记日志
        logger.warning("evalset api: audit failed for %s: %s", action, exc)


def _status_for(code: str) -> int:
    """``EvalsetError.code`` → HTTP 状态(按短名匹配,前缀是命名空间)。"""
    short = str(code or "").rsplit(".", 1)[-1]
    if short == "not_found":
        return 404
    if short in ("conflict", "busy") or short.startswith("duplicate_"):
        return 409
    if short == "masking_refused":
        return 403
    return 400


def _http(exc: EvalsetError | EvalsetBusy) -> HTTPException:
    """两种服务异常 → 同一错误形状。

    ``EvalsetBusy`` **不是** ``EvalsetError``(锁竞争在写锁模块里,不依赖
    服务层的错误类型):真实锁竞争必须落成 409 ``evalset.busy``,而不是
    逃逸成 500。两者在这里汇合,``_status_for`` 的 busy 分支因此是活代码。
    """
    if isinstance(exc, EvalsetBusy):
        code, message, issues = "evalset.busy", str(exc), []
    else:
        code, message, issues = exc.code, exc.message, exc.issues
    return HTTPException(
        status_code=_status_for(code),
        detail={"code": code, "message": message, "issues": issues},
    )


def _service(request: Request, name: str, user: dict[str, Any]) -> EvalsetService:
    """按请求装配服务;``name`` 对活注册表/持久化配置解析,不存在即 404。"""
    _ds_id_or_404(request, name)

    async def _audit_cb(action: str, actor: str, details: dict) -> None:
        await _audit(
            request, action, user, {**(details or {}), "actor": actor},
        )

    return EvalsetService(
        _root(request),
        kb=request.app.state.kb,
        registry=getattr(request.app.state, "connector_registry", None),
        semantic_layer=getattr(request.app.state, "semantic_layer", None),
        config=getattr(request.app.state, "config", None),
        audit=_audit_cb,
    )


def _latest_verdicts(
    root: Path, datasource: str,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    """{(qid, tier) → 最近一次判定摘要} + 读不出时的降级条目(去重)。

    每个 tier 各读一次 ``latest.jsonl`` / ``latest.summary.json``;任一
    读不出 → **该 tier 整体当没有**(不允许半份判定),并记一条
    ``run_store_unreadable``。缺文件是 ``None`` 的正常态,不进 degraded。

    键必须带 tier:同一 qid 在两层都可能有历史判定(题被背书升级后,
    旧层的判定仍然留在那个目录里),按固定 tier 顺序合并会让条目显示
    另一层的结论。条目只取它**当前所属层**的那份(设计 §9.1 line 450)。
    """
    verdicts: dict[tuple[str, str], dict[str, Any]] = {}
    degraded: list[dict[str, Any]] = []
    unreadable_tiers: list[str] = []
    for tier in _TIERS:
        try:
            entries = read_latest_entries(root, datasource, tier)
            summary = read_latest_summary(root, datasource, tier)
        except RunStoreUnreadable as exc:
            # 只记**真正读失败**的 tier —— 把健康的 tier 也列进降级条目,
            # 会让按 tier 过滤的读端误报「结果缺失」。
            unreadable_tiers.append(tier)
            logger.warning(
                "evalset api: unreadable run store for %s/%s: %s",
                datasource, tier, exc,
            )
            continue
        if not entries:
            continue
        run_id = str((summary or {}).get("run_id") or "")
        at = str((summary or {}).get("at") or "")
        for entry in entries:
            qid = str((entry or {}).get("qid") or "")
            if not qid:
                continue
            verdict: dict[str, Any] = {
                "run_id": str(entry.get("run_id") or run_id),
                "tier": str(entry.get("tier") or tier),
                "verdict": entry.get("verdict"),
                "at": str(entry.get("at") or at),
            }
            if entry.get("reference_error") is not None:
                verdict["reference_error"] = entry["reference_error"]
            # 入口写的是目录所属 tier(条目本身不带 tier 字段)。
            verdicts[(qid, tier)] = verdict
    if unreadable_tiers:
        degraded.append({
            "code": "run_store_unreadable",
            "message": (
                "latest run results exist but could not be read; "
                "latest_verdict is null for the affected tiers"
            ),
            "tiers": unreadable_tiers,
        })
    return verdicts, degraded


def _task_report(task: dict[str, Any]) -> dict[str, Any] | None:
    """解析 ``init_tasks`` summary 里落的 JSON(生成报告 / 试跑结局)。

    坏 JSON、空 summary、非对象 → ``None``:读端由此知道「结局未知」,
    而不是把解析失败当成一个成功的空对象。
    """
    summary = str(task.get("summary") or "")
    if not summary:
        return None
    try:
        parsed = json.loads(summary)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _task_status(datasource: str, kind: str) -> dict[str, Any]:
    """状态端点的公共形状:同类任务在跑/跑完 → 任务字典,否则 idle。"""
    task = init_tasks.by_datasource(datasource)
    if task is None or task.get("kind") != kind:
        return {"status": "idle"}
    return task


# ── 读:列表 / 就绪 / 历史 run ───────────────────────────


@router.get("")
async def list_items(
    name: str,
    request: Request,
    status: str = "",
    tier: str = "",
    source: str = "",
    q: str = "",
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """当前验收集(默认全量,含未确认条目)+ 每条的内嵌最近判定。

    ``latest_verdict`` 是**读时投影**,不落盘:它回答「这道题最近一次
    跑出来是什么」,不改变条目的任何状态字段。
    """
    service = _service(request, name, admin)
    try:
        data = await service.list_items(
            name, status=status, tier=tier, source=source, q=q,
        )
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc
    verdicts, degraded = _latest_verdicts(_root(request), name)
    data["items"] = [
        {**item, "latest_verdict": verdicts.get((
            str(item.get("qid") or ""), str(item.get("tier") or ""),
        ))}
        for item in (data.get("items") or [])
    ]
    data["degraded"] = list(data.get("degraded") or []) + degraded
    return data


@router.get("/readiness")
async def get_readiness(
    name: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """就绪视图:三个 tier 各有几道已确认题 + 最近一次 run 覆盖。

    原样透传服务载荷:资产读不出时 ``available=false`` 且
    ``counts/by_tier/coverage/evalset_rev`` 全是 ``None`` —— 路由不把它
    们补成 0(那会把「没读到」说成「测到了且为零」)。
    """
    service = _service(request, name, admin)
    try:
        return await service.readiness(name)
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc


@router.get("/runs")
async def list_runs(
    name: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """最近的 run 摘要(按 ``at`` 降序,至多 ``_RUNS_LIMIT`` 条)。

    跳过 ``latest.summary.json``(R17:它是原子副本,不是一次 run);坏
    文件跳过并记一条降级,不让整个列表 500;目录缺失 = 合法空列表。
    """
    _ds_id_or_404(request, name)
    base = _root(request) / ".trove" / "eval" / "evalsets" / name
    runs: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    for tier in _TIERS:
        tier_dir = base / tier
        if not tier_dir.is_dir():
            continue
        for path in sorted(tier_dir.glob("*.summary.json")):
            if path.name == "latest.summary.json":
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    "evalset api: unreadable summary %s: %s", path, exc,
                )
                degraded.append({
                    "code": "run_store_unreadable",
                    "message": f"skipped unreadable summary: {path.name}",
                    "tier": tier,
                })
                continue
            if not isinstance(payload, dict):
                degraded.append({
                    "code": "run_store_unreadable",
                    "message": f"skipped non-object summary: {path.name}",
                    "tier": tier,
                })
                continue
            runs.append({"tier": tier, **payload})
    runs.sort(key=lambda run: str(run.get("at") or ""), reverse=True)
    return {
        "datasource": name,
        "runs": runs[:_RUNS_LIMIT],
        "degraded": degraded,
    }


# ── 写:条目(gate 由服务层守) ──────────────────────────


@router.post("/items")
async def create_item(
    name: str,
    body: EvalsetItemCreate,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """新增条目 —— 永远落 ``pending``(确认门在服务层,路由不提供旁路)。"""
    service = _service(request, name, admin)
    try:
        return await service.create(
            name,
            question=body.question,
            reference_sql=body.reference_sql,
            tags=body.tags,
            note=body.note,
            actor=_actor(admin),
            force=body.force,
        )
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc


@router.post("/confirm")
async def confirm_batch(
    name: str,
    body: EvalsetConfirmBatch,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """批量确认 —— 逐条独立(一条失败不连坐其余),结果照单返回。"""
    service = _service(request, name, admin)
    try:
        return await service.confirm_batch(
            name, body.qids, actor=_actor(admin), force=body.force,
        )
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc


# ── 生成(后台任务) ─────────────────────────────────────


@router.post("/generate", status_code=202)
async def generate(
    name: str,
    request: Request,
    body: EvalsetGenerateRequest | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """发起后台生成;同源已有任务在跑 → 409(注册表互斥,不排队)。"""
    _ds_id, ds_id = _ds_id_or_404(request, name)
    payload = body or EvalsetGenerateRequest()
    sources = [
        str(source).strip() for source in (payload.sources or [])
        if str(source).strip()
    ]
    invalid = [source for source in sources if source not in GENERATE_SOURCES]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "evalset.invalid_source",
                "message": (
                    f"unknown generation source(s): {', '.join(invalid)}; "
                    f"expected one of {', '.join(GENERATE_SOURCES)}"
                ),
                "issues": [],
            },
        )
    task = init_tasks.create(name, ds_id, kind="evalset")
    if task is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "evalset.busy",
                "message": (
                    "another task (evalset generate/run or KB init) is "
                    f"running for datasource {name}; wait for it to finish"
                ),
                "issues": [],
            },
        )

    root = _root(request)
    kb = request.app.state.kb
    registry = getattr(request.app.state, "connector_registry", None)
    semantic_layer = getattr(request.app.state, "semantic_layer", None)
    config = getattr(request.app.state, "config", None)
    llm = getattr(request.app.state, "llm_gateway", None)
    auth_store = getattr(request.app.state, "auth", None)
    # 不猜一个新的 home:装配层给了就用,没给就让服务层走它自己的默认。
    home_dir = getattr(request.app.state, "home_dir", None)
    model_for_draft = getattr(config, "model_for_draft", None)
    draft_model = (
        str(model_for_draft("evalset", "simple") or "")
        if callable(model_for_draft) else ""
    )
    task_id = str(task["id"])

    async def _run() -> None:
        try:
            init_tasks.update(task_id, stage="generate", progress=5)
            report = await generate_evalset(
                root,
                GenerateRequest(
                    datasource=name,
                    sources=sources,
                    limit=payload.limit,
                    lang=payload.lang,
                    since=payload.since,
                    rewrite=payload.rewrite,
                ),
                kb=kb,
                registry=registry,
                llm=llm,
                draft_model=draft_model,
                semantic_layer=semantic_layer,
                auth_store=auth_store,
                home_dir=home_dir,
            )
            # summary 是**完成报告**:busy / 无语义模型等结局都在报告里
            # (R20),status 端点原样解析给调用方,不改写成「没东西可生成」。
            init_tasks.done(
                task_id, json.dumps(asdict(report), ensure_ascii=False),
            )
            await _audit(request, "evalset.generate", admin, {
                "datasource": name,
                "sources": sources,
                "status": report.status,
                "added": report.added,
            })
        except Exception as exc:  # noqa: BLE001 — 后台任务失败必须落地
            init_tasks.fail(task_id, str(exc))
        finally:
            init_tasks.release_background(task_id)

    background = asyncio.create_task(_run())
    init_tasks.bind_background(task_id, background)
    return {"task_id": task_id, "status": "running"}


@router.get("/generate/status")
async def generate_status(
    name: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """轮询生成任务;``kind`` 不符(如刚跑完的 init)→ ``idle``。

    完成态附带 ``report``(解析后的 ``GenerateReport``),坏 JSON → None。
    """
    _ds_id_or_404(request, name)
    task = _task_status(name, "evalset")
    if task.get("status") == "idle":
        return task
    return {**task, "report": _task_report(task)}


# ── 试跑(后台任务 + 成本确认门) ────────────────────────


@router.post("/run", status_code=202)
async def run(
    name: str,
    request: Request,
    body: EvalsetRunRequest | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """发起后台试跑。

    正式运行会逐题调用 LLM 判题(成本面),所以**没有 ``confirm=true``
    且非 ``compile_only`` 时直接 400**,detail 带 ``n_items``(该 tier 的
    已确认题数)、``tier`` 与将使用的 ``model`` —— 花多少要能先看见。
    """
    _ds_id, ds_id = _ds_id_or_404(request, name)
    payload = body or EvalsetRunRequest()
    tier = str(payload.tier or "verified")
    if tier not in _TIERS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "evalset.invalid_tier",
                "message": (
                    f"unknown tier: {tier}; expected one of "
                    f"{', '.join(_TIERS)}"
                ),
                "issues": [],
            },
        )

    service = _service(request, name, admin)
    config = getattr(request.app.state, "config", None)
    model_for = getattr(config, "model_for", None)
    model = str(model_for("complex") or "") if callable(model_for) else ""

    try:
        listed = await service.list_items(name, tier=tier, status="confirmed")
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc
    asset_issues = listed.get("issues") or []
    if asset_issues:
        # 读不出的资产不能跑,也不能在成本门里被读成「0 道题」(R24):
        # 这道门先于确认分支,probe / confirm:true / compile_only 一律拒绝。
        raise _http(EvalsetError(
            "evalset.invalid_asset",
            "evalset asset has parse issues; refusing to run it",
            issues=asset_issues,
        ))

    if not payload.compile_only and not payload.confirm:
        # 与 runner ``_select`` 同口径:confirmed + exact 才是会真正跑的题;
        # 非 exact 条目被 runner 跳过,不该出现在成本提示的数字里。
        n_items = sum(
            1 for item in (listed.get("items") or [])
            if str(item.get("match") or "") == "exact"
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "confirmation_required",
                "message": (
                    f"a real evalset run calls the LLM ({model or 'default'}) "
                    f"for about {n_items} confirmed {tier} item(s); pass "
                    "confirm=true to proceed, or compile_only=true for a "
                    "zero-LLM check"
                ),
                "tier": tier,
                "n_items": n_items,
                "model": model,
            },
        )

    task = init_tasks.create(name, ds_id, kind="evalset_run")
    if task is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "evalset.busy",
                "message": (
                    "another task (evalset generate/run or KB init) is "
                    f"running for datasource {name}; wait for it to finish"
                ),
                "issues": [],
            },
        )

    root = _root(request)
    kb = request.app.state.kb
    registry = getattr(request.app.state, "connector_registry", None)
    semantic_layer = getattr(request.app.state, "semantic_layer", None)
    task_id = str(task["id"])

    async def _run() -> None:
        try:
            init_tasks.update(task_id, stage="run", progress=5)
            result = await run_evalset(RunOptions(
                project_root=root,
                datasource=name,
                tier=tier,
                limit=payload.limit,
                sample=payload.sample,
                compile_only=payload.compile_only,
                yes=True,  # 确认门已在本端点前方守住
                kb=kb,
                registry=registry,
                config=config,
                semantic_layer=semantic_layer,
            ))
            # 摘要携带**结局**,不只是「跑完了」:exit 1/2 与「有 run 文件
            # 的完成」必须能被前端区分(否则失败被报成成功)。JSON 形状与
            # 生成报告同一套解析(``_task_report``)。
            init_tasks.done(task_id, json.dumps({
                "exit_code": result.exit_code,
                "run_id": result.run_id,
                "tier": result.tier,
                "message": result.message,
                "skipped": result.skipped,
            }, ensure_ascii=False))
            await _audit(request, "evalset.run", admin, {
                "datasource": name,
                "tier": tier,
                "compile_only": bool(payload.compile_only),
                "exit_code": result.exit_code,
            })
        except Exception as exc:  # noqa: BLE001 — 后台任务失败必须落地
            init_tasks.fail(task_id, str(exc))
        finally:
            init_tasks.release_background(task_id)

    background = asyncio.create_task(_run())
    init_tasks.bind_background(task_id, background)
    return {"task_id": task_id, "status": "running"}


@router.get("/run/status")
async def run_status(
    name: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """轮询试跑任务;``kind`` 不符 → ``idle``。

    完成态附带解析后的 ``result``(exit_code / run_id / tier / message /
    skipped),坏或缺失的摘要 → ``result: null``。只有 ``status`` 而看不到
    结局,会让 exit 2 的任务在 UI 上和成功一模一样。
    """
    _ds_id_or_404(request, name)
    task = _task_status(name, "evalset_run")
    if task.get("status") == "idle":
        return task
    return {**task, "result": _task_report(task)}


# ── 读:预览 / 单条 ─────────────────────────────────────


@router.get("/{qid}/preview")
async def preview_item(
    name: str,
    qid: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """参考 SQL 的有界预览(20 行 / 5s),经脱敏后才返回。

    R30:只留 GET —— 预览是纯只读动作,没有消费方需要 POST;把一次只读
    预览广告成写语义只会扩大 CSRF 面。声明在 ``/{qid}`` 之前(顺序纪律)。
    """
    service = _service(request, name, admin)
    principal = await get_principal(request, admin)
    try:
        return await service.preview(name, qid, principal=principal)
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc


@router.get("/{qid}")
async def get_item(
    name: str,
    qid: str,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """单条详情 —— 从同一份列表投影取,避免第二套字段拼装漂移。"""
    service = _service(request, name, admin)
    try:
        data = await service.list_items(name)
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc
    item = next(
        (row for row in (data.get("items") or [])
         if str(row.get("qid") or "") == qid),
        None,
    )
    if item is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "evalset.not_found",
                "message": f"no evalset item {qid!r} in datasource {name}",
                "issues": [],
            },
        )
    return item


@router.patch("/{qid}")
async def patch_item(
    name: str,
    qid: str,
    body: EvalsetItemPatch,
    request: Request,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    service = _service(request, name, admin)
    try:
        return await service.edit(
            name,
            qid,
            actor=_actor(admin),
            question=body.question,
            reference_sql=body.reference_sql,
            tags=body.tags,
            note=body.note,
            force=body.force,
        )
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc


@router.post("/{qid}/confirm")
async def confirm_item(
    name: str,
    qid: str,
    request: Request,
    body: EvalsetConfirmRequest | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    service = _service(request, name, admin)
    try:
        return await service.confirm(
            name, qid, actor=_actor(admin),
            force=bool(body.force) if body else False,
        )
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc


@router.post("/{qid}/reject")
async def reject_item(
    name: str,
    qid: str,
    request: Request,
    body: EvalsetReasonBody | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    service = _service(request, name, admin)
    try:
        return await service.reject(
            name, qid, actor=_actor(admin),
            reason=body.reason if body else "",
        )
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc


@router.post("/{qid}/vouch")
async def vouch_item(
    name: str,
    qid: str,
    request: Request,
    body: EvalsetVouchBody | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    """人工担保 —— ``note`` 必填,空 note 由服务层报 400(不是路由猜)。"""
    service = _service(request, name, admin)
    try:
        return await service.vouch(
            name, qid, actor=_actor(admin), note=body.note if body else "",
        )
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc


@router.post("/{qid}/retire")
async def retire_item(
    name: str,
    qid: str,
    request: Request,
    body: EvalsetReasonBody | None = None,
    admin: dict[str, Any] = Depends(require_admin),
) -> dict:
    service = _service(request, name, admin)
    try:
        return await service.retire(
            name, qid, actor=_actor(admin),
            reason=body.reason if body else "",
        )
    except (EvalsetError, EvalsetBusy) as exc:
        raise _http(exc) from exc
