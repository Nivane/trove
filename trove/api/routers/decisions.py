"""Decision-rule endpoints (admin) — read and edit ``decisions.yml``.

Rules are the declarative half of the decision layer: a rule names a metric
in the semantic model's vocabulary plus the condition under which it counts
as a problem, and a scheduled job evaluates it with zero LLM involvement.
They live inside the KB tree (``.trove/kb/<datasource>/decisions.yml``), so
every edit is a git commit and the digest returned here is what a run's
evidence records as "which version of the rule judged this".

This surface is deliberately read-heavy: the UI lists rules with their lint
issues and editor text, and PUT replaces the whole document at once. Partial
per-rule PATCH would need a merge story for concurrent edits to the same
file; replacing the document makes the write atomic and the git commit an
exact record of it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import require_admin, require_admin_or_analyst
from trove.core.logging import get_logger
from trove.api.schemas import DecisionDocBody, DecisionSimulateBody
from trove.services.decision.rules import (
    RuleError,
    lint_advisories,
    lint_document,
    lint_document_assets,
    parse_document,
    rule_to_dict,
)

router = APIRouter()
logger = get_logger(__name__)


def _kb(request: Request):
    kb = getattr(request.app.state, "kb", None)
    if kb is None:
        raise HTTPException(status_code=409, detail="knowledge base not configured")
    return kb


def _jobs(request: Request):
    return getattr(request.app.state, "jobs", None)


def _verdicts(request: Request):
    """The verdict store, or ``None`` when this process has none.

    ``None`` is not an error for the *list* view (rules still exist without a
    history — a fresh install, or ``trove serve`` wired without the store), so
    callers degrade to a null ``latest_verdict``. The two history endpoints
    need the store to answer at all, so they turn it into a 409.
    """
    return getattr(request.app.state, "verdicts", None)


def _verdict_brief(rec, diff: dict | None = None) -> dict[str, Any]:
    """A verdict without its evidence blob — what a history list renders.

    The evidence (SQL + raw rows) is the *detail* view's payload; inlining it
    per row would make one drawer-open cost a few hundred KB of JSON that the
    list never shows.
    """
    return {
        "id": rec.id,
        "datasource": rec.datasource,
        "rule_id": rec.rule_id,
        "rule_digest": rec.rule_digest,
        "run_id": rec.run_id,
        "job_id": rec.job_id,
        "status": rec.status,
        "triggered": rec.triggered,
        "severity": rec.severity,
        "priority": rec.priority,
        "message": rec.message,
        "error": rec.error,
        "row_count": rec.row_count,
        "evidence_truncated": rec.evidence_truncated,
        "anchor_date": rec.anchor_date,
        "evaluated_at": rec.evaluated_at,
        "created_at": rec.created_at,
        "diff": diff,
    }


def _asset_issues(request: Request, doc) -> list[str]:
    """Template-existence lint, i.e. the same closure ``save`` enforces.

    ``KbService`` injects the registry as a duck-typed object; here it is the
    app component. Same three-way semantics as ``KbService._asset_lint``:
    registry absent → unchecked (a process without the action layer must not
    report every rule as dangling); registry present → its two name sets. The
    list view is where an admin sees "this rule points at a template that was
    rejected/deleted" *before* the next fire, so it must not be quieter here
    than at save time.
    """
    templates = getattr(request.app.state, "action_templates", None)
    if templates is None:
        return []
    try:
        return lint_document_assets(
            doc, templates=set(templates.names()),
            confirmed=set(templates.confirmed_names()),
        )
    except Exception:  # 读模板目录失败:体检腿坏掉不该把规则列表也带走
        logger.warning("decision asset lint failed", exc_info=True)
        return []


async def _referencing_jobs(request: Request, datasource: str, rule_id: str) -> list[str]:
    """Job ids (name: id) that schedule this rule — deleting a rule that a job
    still points at would leave a job failing on every tick."""
    jobs = _jobs(request)
    if jobs is None:
        return []
    out = []
    for job in await jobs.list_jobs():
        if job.decision_rule == rule_id and job.datasource == datasource:
            out.append(f"{job.name} ({job.id})")
    return out


@router.get("/admin/decisions")
async def list_decisions(
    request: Request, datasource: str, admin: dict = Depends(require_admin_or_analyst),
) -> dict:
    """All rules for one datasource, with lint issues and job references."""
    kb = _kb(request)
    try:
        doc = kb.load_decisions(datasource)
    except RuleError as e:
        # A file that will not parse is not "no rules" — say so, or the UI
        # would offer to create a first rule and silently overwrite it.
        raise HTTPException(status_code=422, detail=str(e))
    issues = lint_document(doc) + _asset_issues(request, doc)
    if not doc.rules:
        # Display-only: an empty document is writable (it is also the only way
        # to delete the last rule), but a reader should know it means "nothing
        # is being watched", not "everything is fine".
        issues.insert(0, f"no decision rules declared for {datasource!r}")
    # One lookup for the whole page, and a failure here must not cost the
    # reader the rule list — "the history is unavailable" and "there is no
    # history" are different facts, so the former degrades to null per rule
    # and the list still renders.
    latest: dict[str, Any] = {}
    store = _verdicts(request)
    if store is not None and doc.rules:
        try:
            latest = await store.latest_for_rules(
                datasource, [r.id for r in doc.rules])
        except Exception:
            latest = {}
    rules = []
    for rule in doc.rules:
        last = latest.get(rule.id)
        entry = {
            "id": rule.id,
            "name": rule.name,
            "enabled": rule.enabled,
            "severity": rule.severity,
            "priority": rule.priority,
            "recommendation": rule.recommendation,
            "owner_role": rule.owner_role,
            "window": rule.window,
            "scope": rule.scope,
            "emit": rule.emit,
            "conditions": list(rule.conditions),
            "condition_mode": rule.condition_mode,
            "action": (
                {"template": rule.action.template,
                 "autonomy": rule.action.autonomy,
                 "params": dict(rule.action.params)}
                if rule.action is not None else None
            ),
            "referenced_by": await _referencing_jobs(request, datasource, rule.id),
            "latest_verdict": _verdict_brief(last) if last is not None else None,
        }
        # Schema v3/v4:与 ``rule_to_dict`` 同一序列化源 —— 未声明的规则
        # 响应与历史逐字节一致(前端「拿不到不渲染」),声明了才多出对应块。
        serialized = rule_to_dict(rule)
        for key in ("seasonal", "significance", "causal"):
            if key in serialized:
                entry[key] = serialized[key]
        rules.append(entry)
    return {
        "datasource": datasource,
        "version": doc.version,
        "digest": doc.digest,
        "rules": rules,
        # 草稿队列一并给出:规则列表在**生效面**,草稿在等候室 —— 两者
        # 分开呈现,读者才知道"这条规则现在到底跑不跑"。
        "pending_drafts": [
            {"id": d.get("id"), "rule_id": (d.get("rule") or {}).get("id"),
             "source": d.get("source"), "created_at": d.get("created_at"),
             "enabled": (d.get("rule") or {}).get("enabled", True)}
            for d in _draft_store(request).grouped(datasource)["pending"]
        ],
        "issues": issues,
        # 提示级与拦截级**分开出**:advisory 从不拦保存,混进 issues 会让
        # 读者以为规则存不下去(与 save 的 422 判定不是同一张表)。
        "advisories": lint_advisories(doc),
    }


@router.get("/admin/decisions/raw")
async def get_decisions_raw(
    request: Request, datasource: str, admin: dict = Depends(require_admin),
) -> dict:
    """``decisions.yml`` as text, for the editor.

    Declared **before** ``/{rule_id}`` — Starlette matches routes in
    registration order, so the other way round this would be swallowed by the
    rule-id pattern and answer 404. Returns "" for a datasource with no file
    yet: an empty editor is the right starting point, and ``save`` creates it.
    """
    path = _kb(request).decisions_path(datasource)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"datasource": datasource, "text": text}


# ── 规则草稿(pending → confirm / reject)────────────────────
#
# 草稿**不在** decisions.yml 里(``decision_drafts.yml``,见
# ``services/decision/drafts.py``)—— 所以执行面(调度/DecisionService)结构上
# 读不到未确认的规则,「apply 后立刻跑任务,规则不生效」是结构事实而不是约定。
#
# 路由顺序:这三条必须声明在 ``/{rule_id}`` 之前 —— Starlette 按注册顺序
# 匹配,``/admin/decisions/drafts`` 与 ``/admin/decisions/{rule_id}`` 同为
# 三段,反过来就会被规则 id 吞掉(与 /raw 同一个陷阱)。


def _draft_store(request: Request):
    from trove.services.decision.drafts import DecisionDraftStore

    return DecisionDraftStore(_kb(request))


@router.get("/admin/decisions/drafts")
async def list_decision_drafts(
    request: Request, datasource: str,
    admin: dict = Depends(require_admin),
) -> dict:
    """该数据源的规则草稿队列(按状态分组;前端待审列表读这里)。"""
    store = _draft_store(request)
    return {"datasource": datasource, "drafts": store.grouped(datasource)}


@router.post("/admin/decisions/drafts/{draft_id}/confirm")
async def confirm_decision_draft(
    draft_id: str, request: Request, datasource: str,
    admin: dict = Depends(require_admin),
) -> dict:
    """确认草稿:规则**经既有写门**(``save_decisions``)进 decisions.yml。"""
    store = _draft_store(request)
    actor = str((admin or {}).get("username", ""))
    try:
        draft = await store.confirm(datasource, draft_id, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"draft not found: {draft_id}")
    except RuleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _audit(request, "decisions.draft.confirm", admin, 200, {
        "datasource": datasource, "draft_id": draft_id,
        "rule_id": (draft.get("rule") or {}).get("id"),
    })
    return {"datasource": datasource, "draft_id": draft_id,
            "rule_id": (draft.get("rule") or {}).get("id"),
            "status": draft.get("status")}


@router.post("/admin/decisions/drafts/{draft_id}/reject")
async def reject_decision_draft(
    draft_id: str, request: Request, datasource: str,
    admin: dict = Depends(require_admin),
) -> dict:
    """驳回草稿:只标记 rejected,decisions.yml 一个字节不动。"""
    store = _draft_store(request)
    actor = str((admin or {}).get("username", ""))
    try:
        draft = await store.reject(datasource, draft_id, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"draft not found: {draft_id}")
    except RuleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _audit(request, "decisions.draft.reject", admin, 200, {
        "datasource": datasource, "draft_id": draft_id,
    })
    return {"datasource": datasource, "draft_id": draft_id,
            "status": draft.get("status")}


@router.get("/admin/decisions/verdicts/{verdict_id}")
async def get_verdict(
    verdict_id: int, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """One verdict with its full evidence, plus its diff against the previous.

    Declared **before** ``/{rule_id}`` (same Starlette ordering trap as
    ``/raw``): the paths do not actually overlap today — ``/{rule_id}`` is one
    segment — but a future ``/verdicts/...`` sibling declared below it would
    be shadowed, and the ordering is cheap insurance.

    The evidence carries raw business rows and is admin-only by design; a
    user-facing decision surface (later milestone) must re-mask on read
    rather than reuse this payload.
    """
    store = _verdicts(request)
    if store is None:
        raise HTTPException(
            status_code=409, detail="decision verdict store not configured")
    rec = await store.get(int(verdict_id))
    if rec is None:
        raise HTTPException(status_code=404,
                            detail=f"verdict not found: {verdict_id}")
    from dataclasses import asdict

    from trove.services.decision.verdicts import diff_verdicts

    prev = await store.previous_for(rec.datasource, rec.rule_id, rec)
    return {
        "verdict": asdict(rec),
        "diff": diff_verdicts(prev, rec) if prev is not None else None,
    }


@router.get("/admin/decisions/{rule_id}/verdicts")
async def list_verdicts(
    rule_id: str, request: Request, datasource: str,
    limit: int = Query(20, ge=1, le=200),
    since: str | None = Query(None),
    admin: dict = Depends(require_admin),
) -> dict:
    """Newest-first verdict history for one rule, each row diffed against the
    one before it (inline — the window is bounded, so no N+1).

    The oldest row of the returned window has ``diff: null``: its predecessor
    is outside the page. That is a boundary, not "nothing changed" — raise
    ``limit``/``since`` to reach further back.

    A rule with no history answers 200 with an empty list (it may simply have
    never been scheduled); only a missing *store* is a 409.
    """
    store = _verdicts(request)
    if store is None:
        raise HTTPException(
            status_code=409, detail="decision verdict store not configured")
    records = await store.list_for_rule(datasource, rule_id, limit=limit,
                                        since=since)
    from trove.services.decision.verdicts import diff_verdicts

    out = []
    for i, rec in enumerate(records):
        prev = records[i + 1] if i + 1 < len(records) else None
        out.append(_verdict_brief(
            rec, diff_verdicts(prev, rec) if prev is not None else None))
    return {
        "datasource": datasource,
        "rule_id": rule_id,
        "count": len(out),
        "verdicts": out,
    }


def _replay_maps(evidence: dict[str, Any]) -> tuple[
        dict[str, float | None], dict[str, float | None],
        dict[str, float | None] | None, dict[str, bool] | None, int, str]:
    """verdict 证据 → 重放 maps。

    行卡是判定当时对每组的完整记账(``current``/``baseline``/``confidence``/
    ``gated``),从它重建输入就是**逐字节重放**那次判定的输入方向:重建
    出来的 maps 喂回同一个 ``judge``,原样判定必须复现行卡里的
    ``triggered``(测试钉这一点)。

    第六个返回值是 ``row_count`` 的来源:``"evidence"`` = 判定当时记的
    业务行数;老证据缺这个数时退到行卡条数并标 ``"cards"`` —— 两者**不是
    同一个量**(行卡按组建账),所以退让必须看得见:引用 ``row_count``
    条件的规则在这种重放里,判的就是这个替身。
    """
    rows = [r for r in (evidence.get("rows") or []) if isinstance(r, dict)]
    cur_map = {str(r.get("dim") or ""): r.get("current") for r in rows}
    base_map = {str(r.get("dim") or ""): r.get("baseline") for r in rows}
    conf = {str(r["dim"] or ""): r.get("confidence") for r in rows
            } if any("confidence" in r for r in rows) else None
    gated = {str(r["dim"] or ""): bool(r.get("gated")) for r in rows
             } if any("gated" in r for r in rows) else None
    raw = (evidence.get("evidence") or {}).get("row_count")
    if isinstance(raw, int):
        return cur_map, base_map, conf, gated, int(raw), "evidence"
    return cur_map, base_map, conf, gated, len(rows), "cards"


def _caller_maps(rule, body) -> tuple[dict[str, float | None],
                                      dict[str, float | None]]:
    """调用方 maps 的形状校验 —— 组键错形状会静默判到 None 上,必须拒绝。

    聚合规则唯一的组键是 ``""``(``judge`` 对非 per_dimension 规则只看
    这一个键),给了别的键就是「数字没被用上而模拟看起来跑过了」——
    正是模拟面最不能有的失败模式。
    """
    cur = dict(body.current or {})
    base = dict(body.baseline or {})
    if not cur:
        raise HTTPException(
            status_code=400,
            detail="source='caller' requires a non-empty 'current' map "
                   "(there is nothing to judge otherwise)")
    if rule.scope == "per_dimension":
        if "" in cur or "" in base:
            raise HTTPException(
                status_code=400,
                detail=f"rule {rule.id!r} is per_dimension: group keys are "
                       "dimension values, '' is not one of them")
    else:
        extra = sorted({k for k in (*cur, *base) if k != ""})
        if extra:
            raise HTTPException(
                status_code=400,
                detail=f"rule {rule.id!r} is aggregate: its only group key "
                       f"is '' — got {extra}")
    return cur, base


@router.post("/admin/decisions/{rule_id}/simulate")
async def simulate_decision(
    rule_id: str, body: DecisionSimulateBody, request: Request,
    datasource: str, admin: dict = Depends(require_admin),
) -> dict:
    """what-if:同一条判定内核在假想数字上重判(**零业务库查询**)。

    Declared **before** ``/{rule_id}``(与 ``/raw``、``/drafts`` 同一条
    Starlette 顺序纪律,注册顺序即匹配顺序)。

    数字来源只有两条,都不碰业务库:``source="verdict"``(默认)重放
    最近一条 verdict 的 ``evidence.rows`` —— 模拟的价值在于判的仍是
    「当时那份数字」;``source="caller"`` 由调用方直接给 maps。第三条路
    「再查一遍库」是被**刻意**排除的:那样第二次取的数与判定当时的数
    可能已经不是同一份,「模拟」就变成了「另一条规则」。

    判定与模拟共用 ``service.judge``(B4 提取的模块级纯函数),这是两边
    结论不漂移的全部保证;显著带只重放不重算(见 ``whatif`` 的
    ``degraded``)。规则在 verdict 之后被编辑过时,响应里
    ``source.stale=true`` 并进 ``degraded`` —— 数字是旧规则的判定现场,
    条件是新规则的,这一点必须看得见。
    """
    from trove.services.decision.expr import DecisionExprError
    from trove.services.decision.rules import rule_rev
    from trove.services.decision.whatif import (
        WhatIfError,
        impact_summary,
        parse_scenario,
        simulate_rule,
        simulate_tree,
    )

    kb = _kb(request)
    try:
        doc = kb.load_decisions(datasource)
    except RuleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    rule = next((r for r in doc.rules if r.id == rule_id), None)
    if rule is None:
        raise HTTPException(
            status_code=404,
            detail=f"decision rule not found: {rule_id} (datasource {datasource!r})",
        )

    degraded: list[dict[str, Any]] = []
    tree_evidence: dict[str, Any] | None = None
    if body.source == "verdict":
        if body.current is not None or body.baseline is not None \
                or body.row_count is not None:
            # 给了 maps 却重放 verdict = 用错了数据源而结果看起来是对的。
            raise HTTPException(
                status_code=400,
                detail="source='verdict' replays the recorded numbers and "
                       "ignores caller maps — send source='caller' to judge "
                       "maps you supply",
            )
        store = _verdicts(request)
        if store is None:
            raise HTTPException(
                status_code=409, detail="decision verdict store not configured")
        recs = await store.list_for_rule(datasource, rule_id, limit=1)
        if not recs:
            raise HTTPException(
                status_code=404,
                detail=f"no verdict to replay for rule {rule_id!r} — run it "
                       "once, or send source='caller' with explicit maps",
            )
        rec = recs[0]
        cur_map, base_map, conf, gated, row_count, rc_source = _replay_maps(
            rec.evidence)
        tree_evidence = (rec.evidence.get("analysis") or {}).get("tree")
        source_info: dict[str, Any] = {
            "kind": "verdict", "verdict_id": rec.id,
            "evaluated_at": rec.evaluated_at,
            "rule_digest": rec.rule_digest,
            "stale": rec.rule_digest != doc.digest,
            "row_count_source": rc_source,
        }
        if source_info["stale"]:
            degraded.append({"stage": "replay",
                             "reason": "rule_edited_since_verdict"})
    else:
        cur_map, base_map = _caller_maps(rule, body)
        conf = gated = None
        row_count = int(body.row_count or 0)
        source_info = {"kind": "caller"}

    try:
        adjustments = parse_scenario(body.scenario)
    except WhatIfError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        sim = simulate_rule(rule, cur_map, base_map, row_count, adjustments,
                            confidence_by_dim=conf, gated_by_dim=gated)
    except DecisionExprError as e:
        raise HTTPException(status_code=422, detail=str(e))

    tree = None
    if body.impacts is not None:
        if tree_evidence:
            tree = simulate_tree(tree_evidence, body.impacts)
        else:
            tree = {"total": None, "assumed_unchanged": 0,
                    "not_modeled": [{"node": "", "reason": "no_tree_evidence",
                                     "detail": "verdict 证据里没有驱动器树"}]}
    sim["degraded"] = list(sim.get("degraded") or []) + degraded
    return {
        "datasource": datasource,
        "rule_id": rule_id,
        "rule_rev": rule_rev(rule),
        "source": source_info,
        "scenario": body.scenario,
        "before": sim["before"],
        "after": sim["after"],
        "flip": sim["flip"],
        "applied": sim["applied"],
        "unapplied": sim["unapplied"],
        "degraded": sim["degraded"],
        "tree": tree,
        "summary": impact_summary(sim, tree=tree),
    }


@router.get("/admin/decisions/{rule_id}")
async def get_decision(
    rule_id: str, request: Request, datasource: str,
    admin: dict = Depends(require_admin),
) -> dict:
    kb = _kb(request)
    try:
        doc = kb.load_decisions(datasource)
    except RuleError as e:
        raise HTTPException(status_code=422, detail=str(e))
    rule = next((r for r in doc.rules if r.id == rule_id), None)
    if rule is None:
        raise HTTPException(
            status_code=404,
            detail=f"decision rule not found: {rule_id} (datasource {datasource!r})",
        )
    return {
        "datasource": datasource,
        "digest": doc.digest,
        "rule": rule_to_dict(rule),
        "issues": [i for i in lint_document(doc) if rule_id in i],
        "referenced_by": await _referencing_jobs(request, datasource, rule_id),
    }


@router.put("/admin/decisions")
async def put_decisions(
    body: DecisionDocBody, request: Request, admin: dict = Depends(require_admin),
) -> dict:
    """Replace the whole ``decisions.yml`` for a datasource.

    ``KbService.save_decisions`` is the only write gate and it lints
    unconditionally — a rule that would never fire is refused rather than
    persisted as a job that silently does nothing. There is deliberately no
    bypass here: adding one would only move the enforcement, not remove it.
    """
    kb = _kb(request)
    if (body.rules is None) == (body.text is None):
        raise HTTPException(
            status_code=400,
            detail="send exactly one of 'rules' (structured) or 'text' (raw YAML)",
        )
    data: dict[str, Any] = body.model_dump()
    if body.text is not None:
        import yaml

        try:
            parsed = yaml.safe_load(body.text)
        except yaml.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"invalid YAML: {e}")
        if parsed is not None and not isinstance(parsed, dict):
            # ``dict.update`` would raise TypeError on a list and surface as a
            # 500 — the editor is a free-text box, so this is a typo away.
            raise HTTPException(
                status_code=400,
                detail=f"decisions.yml must be a mapping, got {type(parsed).__name__}",
            )
        data.update(parsed or {})
    try:
        doc = parse_document(data)
    except RuleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    actor = str((admin or {}).get("username", ""))
    try:
        result = await kb.save_decisions(
            body.datasource, doc,
            message=body.message or "",
            trailers={"Generator": "decisions.save", "Approved-by": actor}
            if actor else None,
        )
    except RuleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "decisions.save", admin, 200, {
        "datasource": body.datasource, "rules": len(doc.rules),
    })
    return {
        "datasource": body.datasource,
        "rules": len(doc.rules),
        "git": result if isinstance(result, dict) else {},
    }


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict[str, Any] | None = None) -> None:
    auth = getattr(request.app.state, "auth", None)
    if auth is None or not hasattr(auth, "record_audit"):
        return
    try:
        await auth.record_audit(
            action, user=user, method=request.method, path=request.url.path,
            status=status, details=details,
        )
    except Exception:
        pass
