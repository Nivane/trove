"""Admin endpoints for preset packs (datasource onboarding templates).

A preset names the cross-source methodology skeleton an org wants in place
when onboarding a datasource: skill skeletons/references, decision-rule
templates/references, topic-domain skeletons/references, plus caliber and
presentation hints. Two sources merge at read time — built-in
(``trove/presets/<name>/preset.yml``, shipped with the code) and org
(``.trove/presets/<name>/preset.yml``, admin-managed) — with the **org copy
shadowing the built-in** of the same name (deterministic; ``trove validate``
reports it).

``apply`` is the point of the whole surface: it turns a preset into **pending
drafts only** — a skill draft through ``SkillService.create``'s existing gate,
a rule draft through ``decision_drafts.yml`` (never ``decisions.yml``, so the
execution path structurally cannot see it), a topic draft through the
semantic layer's own approval queue. Nothing a preset produces takes effect
before an admin confirms it, and the per-item report says exactly which
reference did not resolve.

The same pack is versioned through the same ``GitVersioning`` the KB and org
skills use, so ``/history`` + ``/rollback`` come free (a rollback is a new
commit; the preset's ``version`` revision counter moves forward).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from trove.api.deps import require_admin
from trove.api.schemas import PresetApplyRequest, PresetBody, PresetRollbackRequest
from trove.services.presets.models import PresetError

router = APIRouter()


def _presets(request: Request):
    svc = getattr(request.app.state, "presets", None)
    if svc is None:
        raise HTTPException(status_code=409, detail="preset service not configured")
    return svc


def _actor(user: dict) -> str:
    return str(user.get("username", ""))


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


@router.get("/admin/presets")
async def list_presets(
    request: Request, user: dict = Depends(require_admin),
) -> dict:
    """Merged built-in + org preset list.

    ``shadowed: true`` on a built-in row means an org preset of the same name
    is what actually loads — the built-in's updates stop applying, which is
    worth seeing rather than inferring.
    """
    return {"presets": _presets(request).merged()}


@router.get("/admin/presets/{name}")
async def get_preset(
    name: str, request: Request, user: dict = Depends(require_admin),
) -> dict:
    """One preset: parsed contract summary + the raw YAML (editor text)."""
    svc = _presets(request)
    try:
        preset = svc.load(name)
        raw = svc.read_raw(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"preset not found: {name}")
    except PresetError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "name": preset.name,
        "version": preset.version,
        "description": preset.description,
        "author": preset.author,
        "source": preset.source,
        "path": preset.path,
        "counts": preset.counts,
        "text": raw,
    }


@router.post("/admin/presets/{name}/apply")
async def apply_preset(
    name: str, body: PresetApplyRequest, request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """套用 preset 到数据源 —— **只落 pending 草稿**,逐条确认后才生效。

    报告逐条 ``{section, item, status, reason}``:``drafted``(落了草稿)/
    ``skipped``(无需动作)/ ``unresolved``(引用解析不到,什么都没落)。
    """
    svc = _presets(request)
    try:
        report = await svc.apply(name, body.datasource, actor=_actor(user))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"preset not found: {name}")
    except PresetError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _audit(request, "preset.apply", user, 200, {
        "preset": name, "datasource": body.datasource, **report.counts,
    })
    return report.to_dict()


@router.put("/admin/presets/{name}")
async def put_preset(
    name: str, body: PresetBody, request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """写入/覆盖一份**组织** preset(契约先过,后落盘,再自动提交)。"""
    if (body.preset is None) == (body.text is None):
        raise HTTPException(
            status_code=400,
            detail="send exactly one of 'preset' (structured) or 'text' (raw YAML)",
        )
    data: Any = body.preset
    if body.text is not None:
        import yaml

        try:
            data = yaml.safe_load(body.text)
        except yaml.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"invalid YAML: {e}")
        if data is not None and not isinstance(data, dict):
            raise HTTPException(
                status_code=400,
                detail=f"preset.yml must be a mapping, got {type(data).__name__}",
            )
    try:
        entry = await asyncio.to_thread(
            _presets(request).save, name, data or {}, actor=_actor(user))
    except PresetError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _audit(request, "preset.save", user, 200,
                 {"preset": name, "version": entry.get("version")})
    return entry


@router.get("/admin/presets/{name}/history")
async def preset_history(
    name: str, request: Request, limit: int = 50,
    user: dict = Depends(require_admin),
) -> dict:
    """该 org preset 的提交历史(非 git 环境返回空表,不报错)。"""
    try:
        history = await asyncio.to_thread(
            _presets(request).history, name, max(1, min(200, int(limit))))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"preset not found: {name}")
    return {"name": name, "history": history}


@router.post("/admin/presets/{name}/rollback")
async def rollback_preset(
    name: str, body: PresetRollbackRequest, request: Request,
    user: dict = Depends(require_admin),
) -> dict:
    """回滚 org preset 到指定 commit(新建提交,``version`` 继续前进)。"""
    actor = _actor(user)
    try:
        result = await asyncio.to_thread(
            _presets(request).rollback, name, body.sha,
            actor=actor, message=body.message)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"preset not found: {name}")
    if not result.get("rolled_back"):
        raise HTTPException(status_code=400, detail=result)
    await _audit(request, "preset.rollback", user, 200,
                 {"preset": name, "sha": body.sha})
    return {"rolled_back": True, "name": name, "sha": body.sha}
