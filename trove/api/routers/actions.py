"""Admin endpoints for the action pillar (templates, proposals, dispatch).

Two surfaces, both admin-only in v1:

- **Templates** (``.trove/actions/<name>/action.yml``): org assets with the
  same draft → confirm gate as skills. ``confirm`` returns ``injection_hits``
  — reported, never blocking (see ``ActionTemplateService``).
- **Proposals**: what a fired rule asked a human to do. Read the list, read
  one with its full audit trail (approvals + delivery receipts), then act:
  ``approve`` / ``reject`` / ``cancel`` / ``dispatch`` / ``retry`` / ``ack``.

Every transition is a closed-set verb validated here and audited through
``auth.record_audit`` (``action.*``), so "who approved what, and when" has an
answer outside the proposal store too. Failures surface as 400 with the
service's own message — ``ProposalError`` texts are written for a human
("proposal p-… is 'dispatched' — only an approved (or a failed, to retry)…").
"""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import require_admin
from trove.api.schemas import ActionBatchRequest, ActionDecision, ActionTemplateCreate
from trove.services.action.propose import ProposalError
from trove.services.action.service import BATCH_DECISIONS
from trove.services.action.template_render import TEMPLATE_VARIABLES

router = APIRouter()

#: The closed set of proposal transitions the API accepts. Each maps to a
#: service method; the verb in the path is validated against this, so the
#: route table cannot grow a verb the service does not implement.
#: ``dry_run`` 是预演:只走前置判定与回执行,不发 POST、不动提案状态。
_DECISIONS = ("approve", "reject", "cancel", "dispatch", "retry", "dry_run", "ack")

#: 批量端点一次最多接受的 id 数。与提案列表单页上限(200)同值 —— 管理台
#: 只能选中已加载的一页,更大的数字只可能来自脚本。超限**整体 400、不截断**
#: (截断会让调用方以为全都处理了 —— 少批的和没批的一样安静)。
#: 允许动词直接用 service 的 ``BATCH_DECISIONS``(单一事实源,400 文案与
#: service 校验不可能漂移;为什么只有这两个见那里的注释)。
_BATCH_MAX_IDS = 200


def _actions(request: Request):
    service = getattr(request.app.state, "actions", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="action layer is not available in this process")
    return service


def _templates(request: Request):
    return request.app.state.action_templates


def _proposal_dict(p) -> dict[str, Any]:
    return dataclasses.asdict(p)


async def _audit(request: Request, action: str, user: dict, *, status: int,
                 details: dict[str, Any] | None = None) -> None:
    """治理动作留痕(照 skills.py 的写法):``action.<动作>``。"""
    await request.app.state.auth.record_audit(
        action, user=user, method=request.method, path=request.url.path,
        status=status, details=details,
    )


# ── templates ────────────────────────────────────────────

@router.get("/admin/actions/templates")
async def list_action_templates(
    request: Request,
    confirmed_only: bool = False,
    user: dict = Depends(require_admin),
) -> dict:
    """Templates + the layer's state (enabled / configured channel names).

    ``enabled`` and ``channels`` ride along because the console needs them
    beside the list: a template that names a channel this deployment has not
    configured is a broken response waiting for a fire, and the only place
    that is visible before then is here.
    """
    service = _actions(request)
    return {
        "templates": _templates(request).list_templates(
            confirmed_only=confirmed_only),
        "enabled": bool(service.enabled),
        "channels": service.dispatcher.channel_names(),
        "sample_variables": sorted(TEMPLATE_VARIABLES),
    }


@router.post("/admin/actions/templates", status_code=201)
async def create_action_template(
    body: ActionTemplateCreate,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Create a template as a *pending* draft for admin confirmation."""
    try:
        entry = _templates(request).create(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "action.template.create", user, status=201, details={
        "name": entry.get("name"),
        "injection_hits": entry.get("injection_hits") or [],
    })
    return {"name": entry.get("name"), "status": entry.get("status"),
            "injection_hits": entry.get("injection_hits") or [],
            "template": entry}


@router.post("/admin/actions/templates/{name}/confirm")
async def confirm_action_template(
    name: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Confirm a pending template — rules may now reference it."""
    try:
        entry = _templates(request).confirm(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"template not found: {name}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "action.template.confirm", user, status=200, details={
        "name": name, "injection_hits": entry.get("injection_hits") or [],
    })
    return {"name": name, "status": entry.get("status"),
            "injection_hits": entry.get("injection_hits") or []}


@router.post("/admin/actions/templates/{name}/reject")
async def reject_action_template(
    name: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Reject a draft — deletes the template directory."""
    try:
        result = _templates(request).reject(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"template not found: {name}")
    await _audit(request, "action.template.reject", user, status=200,
                 details={"name": name})
    return {"name": name, "status": result.get("status")}


# ── proposals ────────────────────────────────────────────

@router.get("/admin/actions/proposals")
async def list_action_proposals(
    request: Request,
    status: str = "",
    datasource: str = "",
    limit: int = 50,
    user: dict = Depends(require_admin),
) -> dict:
    """Proposal list (newest first) + per-status counts for the badges."""
    service = _actions(request)
    proposals = await service.list_proposals(
        status=status or None, datasource=datasource or None,
        limit=max(1, min(int(limit or 50), 200)),
    )
    return {
        "proposals": [_proposal_dict(p) for p in proposals],
        "counts": await service.status_counts(),
        "enabled": bool(service.enabled),
    }


@router.get("/admin/actions/proposals/{proposal_id}")
async def get_action_proposal(
    proposal_id: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """One proposal with its audit trail: approvals, receipts, outcomes."""
    detail = await _actions(request).get(proposal_id)
    if detail is None:
        raise HTTPException(
            status_code=404, detail=f"proposal not found: {proposal_id}")
    return {
        "proposal": _proposal_dict(detail["proposal"]),
        "approvals": [dataclasses.asdict(a) for a in detail["approvals"]],
        "deliveries": [dataclasses.asdict(d) for d in detail["deliveries"]],
        "outcomes": [dataclasses.asdict(o) for o in detail.get("outcomes", [])],
        "stale": detail["stale"],
    }


@router.get("/admin/actions/proposals/{proposal_id}/outcomes")
async def get_action_outcomes(
    proposal_id: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """该提案的效果测量(闭环验收 B7;至多一行 —— 一次测量一个结局)。

    空列表 ≠ 失败:``outcome_after_days`` 没配时测量从不产生,配了而规则的
    测量期还没滚过行动日时它还没到 —— 两种都由 ``measured`` 与提案自己的
    ``dispatched_at`` 一起读出来,调用方不必猜。
    """
    detail = await _actions(request).get(proposal_id)
    if detail is None:
        raise HTTPException(
            status_code=404, detail=f"proposal not found: {proposal_id}")
    outcomes = list(detail.get("outcomes", []))
    return {
        "proposal_id": proposal_id,
        "outcomes": [dataclasses.asdict(o) for o in outcomes],
        "measured": bool(outcomes),
    }


@router.post("/admin/actions/proposals/batch")
async def batch_decide_action_proposals(
    body: ActionBatchRequest,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """批量审批:逐条独立(一条失败不影响其余),逐条审计。

    替代前端串行 for 循环 —— 串行时前一条失败会让后面的提案状态停在
    「不知道跑没跑」;这里每条的成败都在 ``results`` 里显式给出。逐条审计
    的明细带 ``batch: True``,与单条端点(同名 action)的痕迹区分开。

    两处整批前置校验(400,绝不逐条半执行):动词非法 —— dispatch 是唯一有
    外部副作用的动词(批量外送 = 一键群发),cancel/retry 是纠错,都不该
    成批;id 数超限 —— 不截断(截断后前 N 条已改状态、调用方却以为全处理
    了,比整批拒绝难查得多)。
    """
    if body.decision not in BATCH_DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=f"decision {body.decision!r} cannot be applied in a batch: "
                   f"must be one of {', '.join(BATCH_DECISIONS)}")
    if len(body.ids) > _BATCH_MAX_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"too many ids: {len(body.ids)} > {_BATCH_MAX_IDS} "
                   "(nothing was applied)")
    service = _actions(request)
    actor = str(user.get("username") or user.get("id") or "")
    comment = str(body.comment or "")
    outcome = await service.decide_batch(
        body.ids, body.decision, actor, comment)
    for item in outcome["results"]:
        details: dict[str, Any] = {
            "proposal_id": item["id"], "batch": True, "comment": comment[:200],
        }
        if item["ok"]:
            details["status"] = item.get("status") or ""
            await _audit(request, f"action.proposal.{body.decision}", user,
                         status=200, details=details)
        else:
            # 失败也审计:批量结果集里失败是**数据**不是异常 —— 不落审计的
            # 话,这批的失败在审计面上只剩"谁调过批量端点"一条,逐条原因
            # 蒸发(单条端点的失败在抛 400 前不落审计,这里刻意补齐)。
            details["error"] = item["error"]
            await _audit(request, f"action.proposal.{body.decision}", user,
                         status=400, details=details)
    return {
        "results": outcome["results"],
        "applied": outcome["applied"],
        "failed": outcome["failed"],
    }


@router.post("/admin/actions/proposals/{proposal_id}/{decision}")
async def decide_action_proposal(
    proposal_id: str,
    decision: str,
    request: Request,
    body: ActionDecision | None = None,
    user: dict = Depends(require_admin),
) -> dict:
    """Apply one lifecycle verb to a proposal (closed set — see ``_DECISIONS``).

    The body is optional on purpose: ``approve`` with nothing to add (and
    ``dispatch``/``ack``) must be callable as a plain ``POST`` — requiring an
    empty ``{}`` is friction the caller has to remember, not a safety property
    (``comment`` is the only field, and it defaults to empty).
    """
    if decision not in _DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown decision {decision!r}: must be one of "
                   f"{', '.join(_DECISIONS)}")
    service = _actions(request)
    # 审批人记**用户名**:users.username 唯一(store 里的 UNIQUE 约束),既
    # 稳定又可读 —— 审批轨迹进了 approvals 表之后不再回auth 库做 join,
    # 一个裸 id 在抽屉里是读不出「谁批的」的。
    actor = str(user.get("username") or user.get("id") or "")
    comment = body.comment if body is not None else ""
    try:
        method = getattr(service, decision)
        proposal = await method(proposal_id, actor, comment)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"proposal not found: {proposal_id}")
    except ProposalError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, f"action.proposal.{decision}", user, status=200,
                 details={"proposal_id": proposal_id,
                          "status": proposal.status,
                          "comment": comment[:200]})
    return {"proposal": _proposal_dict(proposal)}
