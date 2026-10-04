"""Admin endpoints for org skill assets (methodology skills).

Skills live in ``.trove/skills/<name>/SKILL.md`` (frontmatter + body) and
follow the same ``draft → admin confirm → active`` gate as KB lessons/examples:
a pending draft is invisible to prompts and the load_skill tool until an
admin confirms it. ``required``-tier skills are injected into the system
prompt of the nodes they trigger; ``available``-tier skills are only
advertised and loaded on demand via ``load_skill``.

Governance (P2): every write path auto-commits through ``GitVersioning``
(same class the KB uses), so ``/history`` + ``/rollback`` come free — the
git log *is* the change audit trail, and a rollback is a new commit (the
skill's ``version`` revision counter moves forward, never backward).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import require_admin
from trove.api.schemas import (
    SkillBodyUpdate,
    SkillCreate,
    SkillLlmDraftRequest,
    SkillRollbackRequest,
    SkillTierUpdate,
)

router = APIRouter()


def _skills(request: Request):
    return request.app.state.skills


def _actor(user: dict) -> str:
    """审计/提交 trailer 里的操作者名。"""
    return str(user.get("username", ""))


async def _audit(request: Request, action: str, user: dict, status: int,
                 details: dict[str, Any] | None = None) -> None:
    """治理动作留痕(照 drift.py:133-138 的写法)。

    技能此前是唯一「改了不留痕」的治理动作(设计稿 P5 §1 缺陷 12 /
    附录 C.1-R5):confirm / reject / tier 三条写路由补上审计,动作名
    ``skill.<动作>``。只在成功后写(与 drift 的 _transition 一致)。
    """
    await request.app.state.auth.record_audit(
        action, user=user, method=request.method, path=request.url.path,
        status=status, details=details,
    )


@router.get("/admin/skills")
async def list_skills(
    request: Request,
    pending: bool = True,
    user: dict = Depends(require_admin),
) -> dict:
    """Merged code + org skill list (admin console view)."""
    return {"skills": _skills(request).list_all()}


@router.get("/admin/skills/org")
async def list_org_skills(
    request: Request,
    confirmed_only: bool = False,
    user: dict = Depends(require_admin),
) -> dict:
    """Org-authored skills only."""
    return {"skills": _skills(request).list_org(confirmed_only=confirmed_only)}


@router.post("/admin/skills/draft", status_code=201)
async def draft_skill(
    body: SkillCreate,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Create a skill as a *pending* draft for admin confirmation."""
    try:
        # 写路径带 git commit:offload 到线程,别让子进程占住事件循环。
        entry = await asyncio.to_thread(
            _skills(request).create, body.model_dump(), actor=_actor(user))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": entry["status"], "name": entry["name"],
            "injection_hits": entry["injection_hits"], "skill": entry}


@router.post("/admin/skills/llm-draft", status_code=201)
async def llm_draft_skill(
    body: SkillLlmDraftRequest,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """LLM drafts the skill body from a spec → pending draft for review."""
    try:
        entry = await _skills(request).draft_with_llm(
            body.name, body.description, body.node, body.purpose, lang=body.lang,
        )
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": entry["status"], "name": entry["name"],
            "injection_hits": entry["injection_hits"], "skill": entry}


@router.get("/admin/skills/{name}/body")
async def skill_body(
    name: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Full body of one skill (preview for review/confirm)."""
    entry = _skills(request).read_skill(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    return entry


@router.post("/admin/skills/{name}/confirm")
async def confirm_skill(
    name: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Confirm a pending skill — it becomes active for prompts/tools.

    ``injection_hits`` 是确认关口的扫描结果(命中模式名,空 = 干净)。
    **报而不拦**:内容是指令性的,只有写它的人能判断那句话是不是有意写的。
    """
    try:
        entry = await asyncio.to_thread(
            _skills(request).confirm, name, actor=_actor(user))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    await _audit(request, "skill.confirm", user, 200, {
        "name": name, "injection_hits": entry.get("injection_hits") or [],
    })
    return {"name": name, "status": entry["status"],
            "injection_hits": entry["injection_hits"]}


@router.post("/admin/skills/{name}/reject")
async def reject_skill(
    name: str,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Reject a draft — deletes the skill directory (deletion is committed too)."""
    try:
        result = await asyncio.to_thread(
            _skills(request).reject, name, actor=_actor(user))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    await _audit(request, "skill.reject", user, 200, {"name": name})
    return {"name": name, "status": result["status"]}


@router.post("/admin/skills/{name}/tier")
async def set_skill_tier(
    name: str,
    body: SkillTierUpdate,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Switch injection tier: required (full prompt) / available (on demand)."""
    try:
        entry = await asyncio.to_thread(
            _skills(request).set_tier, name, body.tier, actor=_actor(user))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "skill.tier", user, 200, {
        "name": name, "tier": entry["tier"],
    })
    return {"name": name, "tier": entry["tier"]}


@router.put("/admin/skills/{name}/body")
async def update_skill_body(
    name: str,
    body: SkillBodyUpdate,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """Rewrite a skill's body (frontmatter untouched, revision +1).

    ``injection_hits`` 与 confirm 关口同一套扫描:先扫、后写,命中只报不改。
    """
    try:
        entry = await asyncio.to_thread(
            _skills(request).update_body, name, body.body, actor=_actor(user))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "skill.body", user, 200, {
        "name": name, "version": entry.get("version"),
        "injection_hits": entry.get("injection_hits") or [],
    })
    return {"name": name, "version": entry.get("version"),
            "injection_hits": entry.get("injection_hits") or []}


@router.get("/admin/skills/{name}/history")
async def skill_history(
    name: str,
    request: Request,
    limit: int = 50,
    user: dict = Depends(require_admin),
) -> dict:
    """该 skill 的提交历史(变更时间线 = 回滚点选择面,按时间倒序)。

    非 git 环境返回空列表,不报错(与语义层 history 同一约定)。
    """
    try:
        history = await asyncio.to_thread(_skills(request).history, name,
                                          limit=max(1, min(200, int(limit))))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    return {"name": name, "history": history}


@router.post("/admin/skills/{name}/rollback")
async def rollback_skill(
    name: str,
    body: SkillRollbackRequest,
    request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """回滚该 skill 到指定 commit(新建提交,历史不改写,修订号 +1)。

    ``sha`` 必须是该 skill 目录的真实历史 commit;坏 sha / git 缺失 → 400。
    """
    actor = _actor(user)
    try:
        result = await asyncio.to_thread(
            _skills(request).rollback, name, body.sha,
            actor=actor, message=body.message)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    if not result.get("rolled_back"):
        raise HTTPException(status_code=400, detail=result)
    await _audit(request, "skill.rollback", user, 200, {
        "name": name, "sha": body.sha,
    })
    entry = result.get("entry") or {}
    return {"rolled_back": True, "name": name, "sha": body.sha,
            "version": entry.get("version")}
