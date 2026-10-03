"""Report-subscription endpoints — 「定时分析 + 订阅」的订阅面.

一次定时运行产出一份报告（NL 路径 = 答案 markdown + 主因 + SQL；决策路径
= 规则消息 + 计数），``SubscriptionService`` 按订阅把它投递给订阅者。本
router 只做管理面：订阅的增删改查 + 投递记录。投递本身发生在 runner 里
（best-effort，见 ``services/jobs/subscribe.py``），不在 HTTP 路径上。

管理面（require_admin）:
  GET    /v1/admin/subscriptions                 — 全部订阅（可按 job/subscriber 过滤）
  POST   /v1/admin/jobs/{job_id}/subscriptions   — 给任务加一个订阅者
  PATCH  /v1/admin/subscriptions/{id}            — 改通道/模式/启停
  DELETE /v1/admin/subscriptions/{id}            — 删订阅
  GET    /v1/admin/deliveries                    — 投递记录（可按任务/订阅者过滤）

用户面（登录即可，严格只看自己的）:
  GET    /v1/subscriptions                       — 我的订阅
  PATCH  /v1/subscriptions/{id}                  — 改自己的（通道/模式/启停）
  DELETE /v1/subscriptions/{id}                  — 退订
  GET    /v1/subscriptions/{id}/deliveries       — 我的投递记录

用户面的 own-only 检查一律以「不存在」应答（404），不用 403 —— 403 会
把「这个 id 属于别人」变成可探测的存在性预言机。订阅者身份记
**username**（与 action 审批人同口径：users.username 有 UNIQUE 约束）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from trove.api.deps import get_current_user, require_admin
from trove.api.schemas import SubscriptionCreate, SubscriptionPatch

router = APIRouter()


def _subs(request: Request):
    svc = getattr(request.app.state, "subscriptions", None)
    if svc is None:
        raise HTTPException(
            status_code=503,
            detail="subscription layer is not available in this process",
        )
    return svc


def _jobs(request: Request):
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is None:
        raise HTTPException(
            status_code=503, detail="jobs layer is not available in this process"
        )
    return jobs


def _actor(user: dict) -> str:
    # 与 actions 审批人同口径：记用户名（唯一），退化才用 id。
    return str(user.get("username") or user.get("id") or "")


def _channel_error(channel: str) -> str | None:
    """Validate a subscription channel string (empty = inherit the job's)."""
    from trove.services.jobs.notify import build_notifier

    channel = (channel or "").strip()
    if not channel:
        return None
    if build_notifier(channel) is None:
        return f"unsupported channel: {channel} (allowed: console | webhook:<url>)"
    return None


async def _subscriber_error(request: Request, subscriber: str) -> str | None:
    """400 message for an unknown subscriber; None when fine.

    A subscription whose subscriber is not a real user is a silent black
    hole — nobody can log in to see the deliveries and admin lists show a
    name that matches no account. Validated at write time. Skipped when no
    user directory is available to check against (embedded/test auth).
    """
    auth = getattr(request.app.state, "auth", None)
    if auth is None or not hasattr(auth, "list_users"):
        return None
    users = await auth.list_users()
    names = {str(u.get("username") or "") for u in users}
    if subscriber not in names:
        return f"unknown user: {subscriber}"
    return None


async def _job_names(request: Request) -> dict[str, str]:
    """id → name map for list enrichment; best-effort (never fails a list)."""
    jobs = getattr(request.app.state, "jobs", None)
    if jobs is None:
        return {}
    try:
        return {j.id: j.name for j in await jobs.list_jobs()}
    except Exception:
        return {}


def _serialize(sub, names: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "id": sub.id,
        "job_id": sub.job_id,
        "job_name": (names or {}).get(sub.job_id, ""),
        "subscriber": sub.subscriber,
        "channel": sub.channel,
        "mode": sub.mode,
        "enabled": bool(sub.enabled),
        "created_by": sub.created_by,
        "created_at": sub.created_at,
        "updated_at": sub.updated_at,
    }


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict | None = None) -> None:
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


# ── admin ────────────────────────────────────────────────


@router.get("/admin/subscriptions")
async def list_subscriptions(
    request: Request,
    job_id: str | None = None,
    subscriber: str | None = None,
    admin: dict = Depends(require_admin),
) -> dict:
    subs = await _subs(request).list_subs(job_id=job_id, subscriber=subscriber)
    names = await _job_names(request)
    return {"subscriptions": [_serialize(s, names) for s in subs],
            "total": len(subs)}


@router.post("/admin/jobs/{job_id}/subscriptions", status_code=201)
async def create_subscription(
    job_id: str, body: SubscriptionCreate, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    jobs = _jobs(request)
    job = await jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    channel_err = _channel_error(body.channel)
    if channel_err:
        raise HTTPException(status_code=400, detail=channel_err)
    subscriber = body.subscriber.strip()
    if not subscriber:
        raise HTTPException(status_code=400, detail="subscriber is required")
    subscriber_err = await _subscriber_error(request, subscriber)
    if subscriber_err:
        raise HTTPException(status_code=400, detail=subscriber_err)
    svc = _subs(request)
    if await svc.find(job_id, subscriber) is not None:
        raise HTTPException(
            status_code=400,
            detail=f"{subscriber!r} is already subscribed to job {job_id}",
        )
    sub = await svc.create(
        job_id, subscriber, channel=body.channel, mode=body.mode,
        created_by=_actor(admin),
    )
    if sub is None:
        raise HTTPException(status_code=400, detail="invalid subscription definition")
    await _audit(request, "subscriptions.create", admin, 201,
                 {"id": sub.id, "job_id": job_id, "subscriber": subscriber})
    return {"subscription": _serialize(sub, {job.id: job.name})}


@router.patch("/admin/subscriptions/{sub_id}")
async def update_subscription(
    sub_id: str, body: SubscriptionPatch, request: Request,
    admin: dict = Depends(require_admin),
) -> dict:
    if body.channel is not None:
        channel_err = _channel_error(body.channel)
        if channel_err:
            raise HTTPException(status_code=400, detail=channel_err)
    sub = await _subs(request).update(
        sub_id, channel=body.channel, mode=body.mode, enabled=body.enabled,
    )
    if sub is None:
        raise HTTPException(status_code=404, detail=f"subscription not found: {sub_id}")
    await _audit(request, "subscriptions.update", admin, 200, {"id": sub_id})
    return {"subscription": _serialize(sub, await _job_names(request))}


@router.delete("/admin/subscriptions/{sub_id}", status_code=204)
async def delete_subscription(
    sub_id: str, request: Request, admin: dict = Depends(require_admin),
) -> None:
    if not await _subs(request).delete(sub_id):
        raise HTTPException(status_code=404, detail=f"subscription not found: {sub_id}")
    await _audit(request, "subscriptions.delete", admin, 204, {"id": sub_id})


@router.get("/admin/deliveries")
async def list_deliveries_admin(
    request: Request,
    job_id: str | None = None,
    subscriber: str | None = None,
    subscription_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    admin: dict = Depends(require_admin),
) -> dict:
    rows = await _subs(request).list_deliveries(
        subscription_id=subscription_id, job_id=job_id,
        subscriber=subscriber, limit=limit,
    )
    return {"deliveries": rows, "total": len(rows)}


# ── user (own-only) ──────────────────────────────────────


@router.get("/subscriptions")
async def my_subscriptions(
    request: Request, user: dict = Depends(get_current_user),
) -> dict:
    subs = await _subs(request).list_subs(subscriber=_actor(user))
    names = await _job_names(request)
    return {"subscriptions": [_serialize(s, names) for s in subs],
            "total": len(subs)}


async def _own_or_404(request: Request, sub_id: str, user: dict):
    sub = await _subs(request).get(sub_id)
    if sub is None or sub.subscriber != _actor(user):
        raise HTTPException(
            status_code=404, detail=f"subscription not found: {sub_id}"
        )
    return sub


@router.patch("/subscriptions/{sub_id}")
async def update_my_subscription(
    sub_id: str, body: SubscriptionPatch, request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    await _own_or_404(request, sub_id, user)
    if body.channel is not None:
        channel_err = _channel_error(body.channel)
        if channel_err:
            raise HTTPException(status_code=400, detail=channel_err)
    sub = await _subs(request).update(
        sub_id, channel=body.channel, mode=body.mode, enabled=body.enabled,
    )
    if sub is None:
        raise HTTPException(status_code=404, detail=f"subscription not found: {sub_id}")
    await _audit(request, "subscriptions.self_update", user, 200, {"id": sub_id})
    return {"subscription": _serialize(sub, await _job_names(request))}


@router.delete("/subscriptions/{sub_id}", status_code=204)
async def delete_my_subscription(
    sub_id: str, request: Request, user: dict = Depends(get_current_user),
) -> None:
    await _own_or_404(request, sub_id, user)
    await _subs(request).delete(sub_id)
    await _audit(request, "subscriptions.self_delete", user, 204, {"id": sub_id})


@router.get("/subscriptions/{sub_id}/deliveries")
async def my_deliveries(
    sub_id: str, request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(get_current_user),
) -> dict:
    await _own_or_404(request, sub_id, user)
    rows = await _subs(request).list_deliveries(subscription_id=sub_id, limit=limit)
    return {"deliveries": rows, "total": len(rows)}
