"""Knowledge base / semantic model endpoints.

Reads go through the SQLite mirror (ensure_synced refreshes it
incrementally from YAML — the single source of truth); appends write
straight into the YAML files.

Reads are open to any authenticated user **whose datasource grants cover
the target datasource** (per-endpoint ``require_datasource``, same rule as
``catalog``/``lineage``: admin any, empty grants = registry default,
non-empty = strict allowlist, no evidence = refuse). ``/kb/status`` has no
datasource parameter and enumerates every KB — it lists only the caller's
visible datasources. KB writes (terms/examples) and the confirm-ALL action
are admin-only; POST /v1/kb/lessons and POST /v1/kb/ratings stay open to
any authenticated user — they are the user feedback channel that produces
*pending* lessons for the admin console to confirm or reject.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from trove.api.deps import (
    get_current_user,
    get_principal,
    require_admin,
    require_datasource,
)
from trove.api.schemas import (
    ExampleCreate,
    LessonConfirmResponse,
    LessonCreate,
    LessonRatingCreate,
    TermCreate,
)
from trove.services.authz.policy import visible_datasources

router = APIRouter()


class LessonKeyBody(BaseModel):
    """逐条 lesson 审批的动作键(管理端「待审批」队列)。

    ``key`` = ``pattern`` 或 ``question`` —— 键两认与
    ``KbService.confirm_lesson`` 同一纪律。走 body 而不是 path:pattern
    里含 ``/`` 时 ``%2F`` 能否匹配 path 参数取决于 ASGI 层,body 传键
    没有这个歧义。``note`` 非 None 时表示「编辑后确认」(先改 note)。
    """

    key: str
    datasource: str | None = None
    note: str | None = None


class ExampleKeyBody(BaseModel):
    """逐条示例审批的定位键(question+sql = pending 草稿的稳定标识)。"""

    question: str
    sql: str
    datasource: str | None = None


def _kb(request: Request):
    return request.app.state.kb


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict | None = None) -> None:
    """审计写入(与 admin.py 的 ``_audit`` 同一形状)。

    单条与批量、本路由与 admin 路由共用同一套事件名 —— 「治理动作的
    可追溯性不能取决于走了哪个按钮」。
    """
    await request.app.state.auth.record_audit(
        action, user=user, method=request.method, path=request.url.path,
        status=status, details=details,
    )


def _datasource(request: Request, datasource: str | None) -> str:
    """Resolve the target datasource: explicit param or registry default.

    **只做解析,不做授权** —— 读端点一律走 ``deps.require_datasource``(解析 +
    判定一体),这里的调用方目前只剩下面这些 POST(用户反馈入口)。新端点
    别再用它:绕过闸门只需要少写一行。
    """
    ds = datasource or request.app.state.connector_registry.default_name
    if not ds:
        raise HTTPException(status_code=400, detail="no active datasource")
    return ds


@router.get("/kb/status")
async def kb_status(
    request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """KB 概览(按数据源的条目计数)。

    没有 ``datasource`` 参数 —— 它一次枚举**全部**数据源,所以可见性在
    这里按 grants 过滤(admin 全量),而不是走 ``require_datasource`` 的单源
    判定。过滤规则复用 ``visible_datasources``(与 catalog 列表页同一份
    实现):任何登录用户能枚举出未授权数据源的名字,本身就是一次信息泄露。
    """
    kb = _kb(request)
    items = await kb.list_items()
    registry = getattr(request.app.state, "connector_registry", None)
    default_name = registry.default_name if registry is not None else None
    principal = await get_principal(request, user)
    visible = set(visible_datasources(principal, items.keys(), default_name))
    return {
        "enabled": kb.enabled,
        "items": {ds: counts for ds, counts in items.items() if ds in visible},
    }


@router.get("/kb/assets")
async def kb_assets(
    request: Request, datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """每份 KB 资产的来源与格式体检(只读;C1)。

    回答"这份文件谁生成的、改没改过、是不是比代码新"。``refused`` 非空的
    资产**没有被镜像采用** —— 镜像里是上一次读懂的样子,所以"KB 看起来正常"
    和"磁盘上的文件被采纳了"是两件事,这个接口是唯一能分开它们的入口。
    """
    kb = _kb(request)
    ds = await require_datasource(request, datasource, user)
    return {
        "datasource": ds,
        "assets": kb.asset_report(ds),
        "refused": {
            rel: reason for rel, reason in kb.refused_assets.items()
            if rel.startswith(f"{ds}/")
        },
    }


@router.get("/kb/rules")
async def list_rules(
    request: Request, user: dict = Depends(get_current_user),
    datasource: str | None = None,
) -> dict:
    kb = _kb(request)
    ds = await require_datasource(request, datasource, user)
    await kb.ensure_synced(ds)
    return {"rules": await kb.list_rules(ds)}


@router.get("/kb/entries")
async def list_semantic_entries(
    request: Request, datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """语义条目全量列表(管理端「术语与指标」表)。

    每条带 ``kind``(metric / entity / table)与镜像 ``item_key``:这三类
    是镜像里本就存在的 kind,此前管理端只展示 ``term`` 一类,把指标、
    维度、枚举值都藏在同一张「术语」表里。``term`` 与 ``metric`` 是同一
    份 payload 的两套投影,这里只出 metric 一套(不重复计数)。
    """
    kb = _kb(request)
    ds = await require_datasource(request, datasource, user)
    await kb.ensure_synced(ds)
    return {"entries": await kb.list_semantic_entries(ds)}


# ── Terms (semantics.yml) ────────────────────────────────


@router.get("/kb/terms")
async def list_terms(
    request: Request,
    q: str | None = None,
    datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    kb = _kb(request)
    ds = await require_datasource(request, datasource, user)
    await kb.ensure_synced(ds)
    if q:
        hits = await kb.search_terms(q, ds)
        return {"terms": [asdict(h) for h in hits]}
    return {"terms": [{"term": name} for name in await kb.list_term_names(ds)]}


@router.post("/kb/terms", status_code=201)
async def create_term(
    body: TermCreate, request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    ds = _datasource(request, datasource)
    await _kb(request).append_term(body.model_dump(), ds)
    return {"status": "ok", "term": body.term, "datasource": ds}


# ── Examples (examples.yml) ──────────────────────────────


@router.get("/kb/examples")
async def list_examples(
    request: Request,
    q: str | None = None,
    datasource: str | None = None,
    limit: int = Query(default=3, ge=1, le=20),
    user: dict = Depends(get_current_user),
) -> dict:
    kb = _kb(request)
    ds = await require_datasource(request, datasource, user)
    await kb.ensure_synced(ds)
    if q:
        hits = await kb.search_examples(q, ds, limit=limit)
        return {"examples": [asdict(h) for h in hits]}
    return {"examples": [{"question": question} for question in await kb.list_example_questions(ds)]}


@router.post("/kb/examples", status_code=201)
async def create_example(
    body: ExampleCreate, request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    ds = _datasource(request, datasource)
    await _kb(request).append_example(body.model_dump(), ds)
    return {"status": "ok", "question": body.question, "datasource": ds}


# ── Pending example drafts (好评闭环:user 好评 → draft → admin 确认) ──


@router.get("/kb/examples/pending")
async def list_pending_examples(
    request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """待确认参考示例(好评问答自动草拟,pending 不参与检索)。"""
    ds = _datasource(request, datasource)
    return {"examples": await _kb(request).list_pending_examples(ds)}


@router.post("/kb/examples/draft", status_code=201)
async def draft_example(
    body: ExampleCreate, request: Request, datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    """User feedback channel: 好评问答 → pending 参考示例,供 admin 确认。"""
    ds = _datasource(request, datasource)
    res = await _kb(request).draft_example(
        body.question, body.sql, ds, tags=body.tags, note="",
        generator="user_feedback",
    )
    if res.get("status") == "invalid":
        raise HTTPException(status_code=400, detail="question and sql are required")
    return {"status": res["status"], "question": body.question, "datasource": ds}


@router.post("/kb/examples/confirm", response_model=LessonConfirmResponse)
async def confirm_pending_examples(
    request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """确认全部 pending 示例(清除 pending 标志,进入检索)。

    必须传 ``actor``:确认是**人的背书**,而 I5 要求 certified 必须有人。
    不传的话资产只清 pending、拿不到认证记录 —— 治理能力在接口上等于不存在,
    而 service 层的用例都是自己传 actor 的,这条路测不出来。
    """
    ds = _datasource(request, datasource)
    actor = str(user.get("username", ""))
    confirmed = await _kb(request).confirm_pending_examples(ds, actor=actor)
    await _audit(request, "kb.example.confirm", user, 200,
                 {"datasource": ds, "all": True, "confirmed": confirmed})
    return {"confirmed": confirmed}


@router.post("/kb/examples/reject", response_model=LessonConfirmResponse)
async def reject_pending_examples(
    request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """拒绝(删除)全部 pending 示例。"""
    ds = _datasource(request, datasource)
    rejected = await _kb(request).reject_pending_examples(ds)
    await _audit(request, "kb.example.reject", user, 200,
                 {"datasource": ds, "all": True, "rejected": rejected})
    return {"confirmed": rejected}


@router.post("/kb/examples/confirm-one")
async def confirm_pending_example(
    body: ExampleKeyBody, request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """确认**单条** pending 示例(question+sql 定位,逐条认证门)。

    认证门逐条判定:坏 SQL 以 409 + 原因回报并原样留在 pending,不拖住
    整批(整批版是一条坏 SQL 全批拒绝)。管理台的批量条据此展示部分失败
    与单项重试。404 = 没有匹配的 pending 草稿(可能已被处理)。
    """
    ds = _datasource(request, body.datasource or datasource)
    res = await _kb(request).confirm_pending_example(
        ds, question=body.question, sql=body.sql,
        actor=str(user.get("username", "")),
    )
    if res["status"] == "not_found":
        raise HTTPException(
            status_code=404, detail=f"pending example not found: {body.question}")
    if res["status"] == "refused":
        raise HTTPException(
            status_code=409,
            detail="认证门拒绝(坏 SQL 进资产库后会被快径直接执行): "
                   f"「{body.question}」: " + " | ".join(res["issues"]))
    await _audit(request, "kb.example.confirm", user, 200,
                 {"datasource": ds, "question": body.question})
    return {"status": "confirmed", "question": body.question,
            "audit": "kb.example.confirm"}


@router.post("/kb/examples/reject-one")
async def reject_pending_example(
    body: ExampleKeyBody, request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """拒绝(删除)**单条** pending 示例;已确认的条目不受影响。"""
    ds = _datasource(request, body.datasource or datasource)
    if not await _kb(request).reject_pending_example(
        ds, question=body.question, sql=body.sql,
    ):
        raise HTTPException(
            status_code=404, detail=f"pending example not found: {body.question}")
    await _audit(request, "kb.example.reject", user, 200,
                 {"datasource": ds, "question": body.question})
    return {"status": "rejected", "question": body.question,
            "audit": "kb.example.reject"}


# ── Lessons (Hint Bank, pending until confirmed) ─────────


@router.get("/kb/lessons")
async def list_lessons(
    request: Request,
    datasource: str | None = None,
    pending: bool = False,
    user: dict = Depends(get_current_user),
) -> dict:
    kb = _kb(request)
    ds = await require_datasource(request, datasource, user)
    await kb.ensure_synced(ds)
    return {"lessons": await kb.list_lessons(ds, confirmed_only=not pending)}


@router.post("/kb/lessons", status_code=201)
async def create_lesson(
    body: LessonCreate, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """User feedback channel: creates a *pending* lesson for admin review."""
    ds = _datasource(request, None)
    entry = body.model_dump()
    entry["confirmed"] = False
    await _kb(request).append_lesson(entry, ds)
    return {"status": "ok", "pattern": body.pattern}


@router.post("/kb/ratings", status_code=201)
async def rate_lesson(
    body: LessonRatingCreate, request: Request, user: dict = Depends(get_current_user)
) -> dict:
    """User up/down vote on a question->answer.

    The rated Q&A is upserted into the lesson Hint Bank keyed by
    `question`, aggregating upvotes/downvotes and landing *pending* for the
    admin console to confirm or reject.

    好评同时作为 ``upvote`` 证据累加置信度(过阈值自动确认)。闸门在
    ``MemoryService`` —— ``promotion`` 默认关,没有 memory 组件(嵌入/测试
    装配)时这一路整个不发生。
    """
    ds = _datasource(request, None)
    lesson = await _kb(request).rate_lesson(body.model_dump(), ds)
    if body.vote == 1:
        memory = getattr(request.app.state, "memory", None)
        if memory is not None:
            await memory.promote_lesson(ds, body.question, evidence_kind="upvote")
    # 反馈闭环:评分回写对应 Langfuse trace(run_id = trace_id)
    if body.run_id:
        try:
            from trove.llm.observability import record_user_score
            record_user_score(body.run_id, body.vote, comment=body.note or "")
        except Exception:
            pass
    return {"status": "ok", "question": body.question, "lesson": lesson}


@router.post("/kb/lessons/confirm", response_model=LessonConfirmResponse)
async def confirm_lessons(
    request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    ds = _datasource(request, datasource)
    confirmed = await _kb(request).confirm_pending_lessons(ds)
    await _audit(request, "kb.lesson.confirm", user, 200,
                 {"datasource": ds, "all": True, "confirmed": confirmed})
    return {"confirmed": confirmed}


@router.post("/kb/lessons/confirm-one")
async def confirm_lesson_one(
    body: LessonKeyBody, request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """确认**单条** lesson(键两认:pattern 或 question)。

    ``note`` 非 None 时先就地改 note 再确认(管理端「编辑后确认」)。
    404 = 键没有匹配条目 —— 键两认落地后,投票产生的 lesson(只有
    question)不再必然 404。
    """
    ds = _datasource(request, body.datasource or datasource)
    if not await _kb(request).confirm_lesson(ds, body.key, note=body.note):
        raise HTTPException(status_code=404, detail=f"lesson not found: {body.key}")
    await _audit(request, "kb.lesson.confirm", user, 200,
                 {"datasource": ds, "key": body.key})
    return {"status": "confirmed", "key": body.key, "audit": "kb.lesson.confirm"}


@router.post("/kb/lessons/reject-one")
async def reject_lesson_one(
    body: LessonKeyBody, request: Request, datasource: str | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """删除**单条** lesson(键两认:pattern 或 question)。"""
    ds = _datasource(request, body.datasource or datasource)
    if not await _kb(request).reject_lesson(ds, body.key):
        raise HTTPException(status_code=404, detail=f"lesson not found: {body.key}")
    await _audit(request, "kb.lesson.reject", user, 200,
                 {"datasource": ds, "key": body.key})
    return {"status": "rejected", "key": body.key, "audit": "kb.lesson.reject"}


# ── Table annotations (schema_notes.yml) ─────────────────


@router.get("/kb/tables/{table_name}/notes")
async def table_notes(
    table_name: str, request: Request, datasource: str | None = None,
    user: dict = Depends(get_current_user),
) -> dict:
    kb = _kb(request)
    ds = await require_datasource(request, datasource, user)
    await kb.ensure_synced(ds)
    notes = await kb.table_notes([table_name], ds)
    if table_name not in notes:
        raise HTTPException(status_code=404, detail=f"no notes for table: {table_name}")
    return asdict(notes[table_name])
