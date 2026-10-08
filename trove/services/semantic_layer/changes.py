"""语义变更 —— 草稿之上的「合并单元」（设计 §5.2/§6/§7.1）。

分层：草稿 = 收件箱（intake），变更 = 合并单元（unit of merge）。agent 与
草稿两条路径写 ``semantics.yml`` 唯一经由本模块 ``_write_merge`` 的同步写
区（I1）；包装方法 ``merge_draft`` / ``merge_auto``（Task 6）保持
``manage.py`` 对外签名与提交数不变。

**同步写区纪律（A6）**：digest 双检 → 应用 payload → 写前门禁 → 写盘，这
一段内**不得有 await**（单进程事件循环下即原子区）；唯一的取数 await
（漂移 warn 门）放在写区之前。``get``/``list``/``verify``/``detail`` 因此
是同步方法——同步写区要能直调它们。
"""
from __future__ import annotations

import copy
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from trove.core import metrics
from trove.core.config import ChangesConfig
from trove.services.datasource.naming import is_path_safe
from trove.services.drift.models import ImpactSet, normalize_subject
from trove.services.semantic_layer.diff import build_change_diff
from trove.services.semantic_layer.manage import (
    _ACTIONS, _KINDS, _apply_draft, _dump_yaml, _load_yaml,
    _reject_bad_semantics,
)

logger = logging.getLogger(__name__)

_ORIGINS = ("draft_confirm", "auto_apply", "manual")
_STATUSES = ("open", "approved", "rejected", "merged", "stale")


class ChangeError(ValueError):
    """变更操作的确定性错误（400/409/422 由路由层区分）。"""


class ChangeStale(ChangeError):
    """乐观并发冲突：主线已被他人修改（→ 409 stale_change）。"""


class ChangeInvalid(ChangeError):
    """lint / 写前门禁不过（→ 422 change_invalid）；主线不动（I5）。"""


class ChangeNotFound(KeyError):
    """变更不存在（→ 404）。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_yaml_str(text: str) -> dict[str, Any]:
    """文本 → dict（坏文本/非映射 → {}）——快照回读专用,与 _load_yaml 同宽容。"""
    try:
        data = yaml.safe_load(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _subjects_of(payloads: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for p in payloads:
        kind = str(p.get("kind") or "")
        name = str(p.get("name") or "")
        if kind and name:
            out.append({"kind": kind, "name": name})
    return out


@dataclass
class ChangeRecord:
    id: str
    datasource: str
    origin: str = "manual"
    status: str = "open"
    question: str = ""
    note: str = ""
    author: str = ""
    created_at: str = ""
    base_digest: str = ""
    base_commit: str = ""
    subjects: list[dict[str, str]] = field(default_factory=list)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    draft_id: str = ""
    auto: bool = False
    warnings: list[dict[str, Any]] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    dialect: str | None = None
    diff: dict[str, Any] | None = None
    reject_reason: str = ""
    resolved_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChangeRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in dict(d or {}).items() if k in known})


class ChangeService:
    def __init__(self, kb, *, config: ChangesConfig | None = None,
                 drift_store: Any = None, staging=None) -> None:
        from trove.services.semantic_layer.manage import SemanticManager

        self._kb = kb
        self._config = config or ChangesConfig()
        self._drift = drift_store  # 注入(测试/装配);None = 首次用时惰性构造
        # 隔离区按数据源分目录（``<kb>/<ds>/.staging/<id>/``——根 .gitignore 的
        # 条目正是 ``.trove/kb/*/.staging/``,快照必须与它同构）。注入的
        # StagingArea 覆盖全部数据源（测试/装配用）;``self._staging`` 指向上
        # 一次用过的数据源,评审/测试靠它定位快照。
        self._injected_staging = staging
        self._staging = staging
        self._manager = SemanticManager(kb)

    # ── 基础 ─────────────────────────────────────────────

    def _check(self, datasource: str) -> None:
        if not is_path_safe(datasource):
            raise ValueError(f"unsafe KB datasource name {datasource!r}")

    def _area(self, datasource: str):
        """该数据源的隔离区（注入优先）。"""
        from trove.services.semantic_layer.staging import StagingArea

        if self._injected_staging is not None:
            return self._injected_staging
        area = StagingArea(Path(self._kb.kb_dir) / datasource)
        self._staging = area
        return area

    def changes_path(self, datasource: str) -> Path:
        return Path(self._kb.kb_dir) / datasource / "semantic_changes.yml"

    def _load(self, datasource: str) -> list[ChangeRecord]:
        data = _load_yaml(self.changes_path(datasource))
        return [ChangeRecord.from_dict(e) for e in (data.get("changes") or [])]

    def _save(self, datasource: str, records: list[ChangeRecord]) -> None:
        _dump_yaml(self.changes_path(datasource),
                   {"changes": [r.to_dict() for r in records]})

    def get(self, datasource: str, change_id: str) -> ChangeRecord:
        self._check(datasource)
        for r in self._load(datasource):
            if r.id == change_id:
                return r
        raise ChangeNotFound(f"变更不存在: {change_id}")

    def list(self, datasource: str, *, status: str | None = None) -> list[ChangeRecord]:
        self._check(datasource)
        return [r for r in self._load(datasource) if status is None or r.status == status]

    def _head_commit(self, datasource: str) -> str:
        """当前 HEAD（重算锚点）——best-effort,取不到留空。"""
        try:
            from trove.services.kb.git_versioning import GitKb

            entries = GitKb(Path(self._kb.kb_dir), enabled=True).history_files(
                [self._kb.semantics_path(datasource)], limit=1)
            return entries[0]["sha"] if entries else ""
        except Exception:
            return ""

    def _current_text(self, datasource: str) -> str:
        path = self._kb.semantics_path(datasource)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    # ── 漂移 warn 门（I8）────────────────────────────────

    def _drift_store(self):
        if self._drift is None:
            from trove.services.drift.store import DriftStore

            self._drift = DriftStore(Path(self._kb.kb_dir).parent.parent)
        return self._drift

    async def drift_warnings(self, datasource: str,
                             subjects: list[dict[str, str]]) -> tuple[list[dict], list[str]]:
        """open_subjects ∩ touched_subjects → warnings;读不到 → degraded（I8）。

        两侧都经 ``normalize_subject`` 归一后按**裸名字**比较：漂移侧存的是归一
        后的裸主体（``loan.region``），``subjects`` 这边取的也是每条记录的
        ``name`` 字段。**``kind:`` 前缀不被剥离**（``normalize_subject`` 只做
        小写/去引号/压空白/取末两段）——``field:loan.region`` 与 ``loan.region``
        **不会**交集,所以调用方必须传裸名。本文件 ``detail()`` 里带前缀的那种
        形状是交给 ``resolve_impact`` 的,与这里无关。
        """
        try:
            open_subs = await self._drift_store().open_subjects(datasource)
        except Exception as e:
            logger.warning("drift store unavailable for %s: %s", datasource, e)
            return [], ["drift_unavailable"]
        wanted = {normalize_subject(s.get("name") or "") for s in subjects}
        wanted.discard("")
        hit = sorted(o for o in open_subs if normalize_subject(o) in wanted)
        return ([{"code": "open_drift", "subjects": hit}] if hit else []), []

    # ── 开单 ─────────────────────────────────────────────

    def _validate_payloads(self, payloads: list[dict[str, Any]]) -> None:
        if not payloads:
            raise ChangeError("变更至少需要一个 payload")
        for p in payloads:
            # 形状先于内容：非 dict 的条目/载荷在这里挡下。放过去的话,下面
            # 的 ``p.get(...)`` 会抛 AttributeError,干跑的 ``(ValueError,
            # TypeError)`` 兜不住 —— 到 HTTP 层就是一次 500。
            if not isinstance(p, dict):
                raise ChangeError("payload 条目必须是对象(dict)")
            if str(p.get("kind")) not in _KINDS:
                raise ChangeError(f"kind 必须为 {sorted(_KINDS)} 之一")
            if str(p.get("action")) not in _ACTIONS:
                raise ChangeError(f"action 必须为 {sorted(_ACTIONS)} 之一")
            if not str(p.get("name") or ""):
                raise ChangeError("name 必填")
            pl = p.get("payload")
            if pl is not None and not isinstance(pl, dict):
                raise ChangeError("payload 必须是对象(dict)")
            if str(p.get("action")) == "upsert" and not pl:
                raise ChangeError("upsert 载荷需要 payload")

    async def open(self, datasource: str, *, origin: str, payloads: list[dict],
                   question: str = "", note: str = "", author: str = "",
                   dialect: str | None = None) -> dict[str, Any]:
        """显式开单：快照 `.staging/<id>/` + 记录 status=open（两条提交之一）。

        刻意**不查漂移面**：warn 门的取数 await 在 merge 时做（合入前那一
        刻的漂移才是要看的），开单只写记录与快照——这里写的 warnings 会在
        合入时被覆盖,提前查一次只是把一份会过期的答案记进审计。
        """
        self._check(datasource)
        origin = origin if origin in _ORIGINS else "manual"
        self._validate_payloads(payloads)
        area = self._area(datasource)
        # 清理在 open 时顺带做（不依赖调度器）;隔离区按数据源分目录,因此
        # 这里只清当前数据源的快照——别的源有自己的保留期账。
        area.prune(self._config.retain_staging_days)
        base_text = self._current_text(datasource)
        data = _load_yaml(self._kb.semantics_path(datasource))
        after = copy.deepcopy(data) if data else {}
        for p in payloads:
            fake = {"id": "chg-dry", "kind": p.get("kind"), "action": p.get("action"),
                    "name": p.get("name"), "payload": p.get("payload")}
            try:
                _apply_draft(after, fake, dialect)
            except (ValueError, TypeError) as e:
                raise ChangeInvalid(f"开单干跑失败: {e}") from e
        rec = ChangeRecord(
            id=f"chg-{datetime.now(timezone.utc):%Y%m%d}-{uuid4().hex[:4]}",
            datasource=datasource, origin=origin, status="open",
            question=question, note=note, author=author, created_at=_now(),
            base_digest=_digest(base_text), base_commit=self._head_commit(datasource),
            subjects=_subjects_of(payloads), payloads=[dict(p) for p in payloads],
            dialect=dialect,
        )
        area.stage(rec.id, base_text=base_text,
                   after_text=yaml.safe_dump(after, allow_unicode=True,
                                             sort_keys=False))
        records = self._load(datasource)
        records.append(rec)
        self._save(datasource, records)
        trailers = {"Generator": "semantic.change.open"}
        if author:
            trailers["Approved-by"] = author
        await self._kb.force_sync(datasource)
        await self._kb.git_commit(
            datasource, f"semantic: open change {rec.id} ({rec.note or origin})",
            files=["semantic_changes.yml"], trailers=trailers)
        metrics.record_semantic_change("open", origin)
        if origin in (self._config.sandbox_by_origin or []):
            self.verify(datasource, rec.id)
        return rec.to_dict()

    # ── 验证 / 详情 / 驳回 ───────────────────────────────

    def verify(self, datasource: str, change_id: str) -> dict[str, Any]:
        """沙箱编译回放（零 LLM）。快照缺 → 明确报错（半损不静默）。"""
        from trove.services.semantic_layer.sandbox import run_replay

        rec = self.get(datasource, change_id)
        area = self._area(datasource)
        base_text = area.read_base(rec.id)
        after_text = area.read_after(rec.id)
        if base_text is None or after_text is None:
            raise ChangeError(f"变更 {rec.id} 的快照缺失 —— 已不可验证（可重开）")
        out = run_replay(Path(self._kb.kb_dir), datasource,
                         dialect=rec.dialect or "sqlite",
                         base_text=base_text, after_text=after_text)
        area.write_verification(rec.id, out)
        return out

    def detail(self, datasource: str, change_id: str, *,
               dialect: str | None = None) -> dict[str, Any]:
        """详情：记录 + diff + 影响面 + 验证（后三者惰性算,失败只降级）。"""
        rec = self.get(datasource, change_id)
        out = rec.to_dict()
        degraded = list(out.get("degraded") or [])
        out["diff"] = rec.diff
        if out["diff"] is None:
            area = self._area(datasource)
            base_text = area.read_base(rec.id)
            after_text = area.read_after(rec.id)
            if base_text is not None and after_text is not None:
                out["diff"] = build_change_diff(
                    _load_yaml_str(base_text), _load_yaml_str(after_text),
                    rec.payloads, rec.dialect or dialect).to_dict()
            else:
                # 快照半损 → 这份 diff 算不出来。**如实标注**：`diff is None`
                # 与「还没算过」是两件事（同 staging 的 None 语义）。
                degraded.append("snapshot_missing")
        try:
            from trove.services.drift.impact import resolve_impact

            out["impact"] = resolve_impact(
                Path(self._kb.kb_dir), datasource,
                {f"{s['kind']}:{s['name']}" for s in rec.subjects}).to_dict()
        except Exception as e:
            logger.warning("impact resolve failed for %s: %s", rec.id, e)
            out["impact"] = ImpactSet().to_dict()
            degraded.append("impact_unavailable")
        out["verification"] = self._area(datasource).read_verification(rec.id)
        out["degraded"] = degraded
        return out

    async def reject(self, datasource: str, change_id: str, *, by: str,
                     reason: str) -> dict[str, Any]:
        if not str(reason or "").strip():
            raise ChangeError("驳回必须给出 reason —— 无理由的驳回等于删记录")
        rec = self.get(datasource, change_id)
        if rec.status != "open":
            raise ChangeError(f"变更 {change_id} 已 {rec.status}")
        rec.status = "rejected"
        rec.reject_reason = reason
        rec.resolved_by = by
        records = self._load(datasource)
        self._save(datasource, [rec if r.id == rec.id else r for r in records])
        trailers = {"Generator": "semantic.change.reject"}
        if by:
            trailers["Approved-by"] = by
        await self._kb.force_sync(datasource)
        await self._kb.git_commit(
            datasource, f"semantic: reject change {rec.id}",
            files=["semantic_changes.yml"], trailers=trailers)
        metrics.record_semantic_change("rejected", rec.origin)
        return rec.to_dict()

    # ── 合并（★ I1：两条路径唯一写 semantics.yml 的落点）──

    def _write_merge(self, datasource: str, rec: ChangeRecord, *,
                     dialect: str | None, by: str, warnings: list[dict],
                     degraded: list[str], generator: str,
                     extra_trailers: dict[str, str] | None = None,
                     mark_applied: str | None = None,
                     append_entry: dict[str, Any] | None = None,
                     fail_prefix: str = "合并失败") -> dict[str, Any]:
        """同步写区（A6：digest 读 → 写盘之间无 await）。

        顺序是承重的：digest 双检 → 应用 payload → 写前门禁 → **取齐三份
        待写内容（含 drafts/changes 的读与查找）** → 依次写 semantics /
        drafts / changes。所有可能 raise 的读都在第一个字节落盘之前 ——
        写区只可能「什么都没写」或「三份都写完」（I5；`_find_draft` 的
        查找失败、`_load_yaml` 的坏文件都算在内）。

        ``fail_prefix``:payload 应用失败的报错前缀。草稿确认路径传
        ``草稿确认失败``——收窄为委托 **不改对外消息**（管理端 400 detail
        是管理员看得见的文本,兼容安全带的一部分）。
        """
        semantics = self._kb.semantics_path(datasource)
        current_text = semantics.read_text(encoding="utf-8") if semantics.exists() else ""
        current_digest = _digest(current_text)
        if rec.base_digest and rec.base_digest != current_digest:
            metrics.record_semantic_merge_conflict()
            raise ChangeStale("主线已被他人修改,请重开变更(stale_change)")
        rec.base_digest = rec.base_digest or current_digest

        data = _load_yaml(semantics)
        base_doc = copy.deepcopy(data) if data else {}
        working = copy.deepcopy(data) if data else {}
        for p in rec.payloads:
            fake = {"id": rec.id, "kind": p.get("kind"), "action": p.get("action"),
                    "name": p.get("name"), "payload": p.get("payload")}
            try:
                _apply_draft(working, fake, dialect)
            except (ValueError, TypeError) as e:
                raise ChangeInvalid(f"{fail_prefix}: {e}") from e
        if "version" not in working and "semantic_model" in working:
            working["version"] = "0.2.0.dev0"
        try:
            _reject_bad_semantics(working, dialect)
        except ValueError as e:
            raise ChangeInvalid(str(e)) from e

        rec.status = "merged"
        rec.warnings = list(warnings)
        rec.degraded = list(degraded)
        rec.diff = build_change_diff(base_doc, working, rec.payloads, dialect).to_dict()
        rec.resolved_by = by

        # ── 落盘前：取齐三份内容（读/查找都可能 raise —— 放在写盘之前）──
        manager = self._manager
        applied_write: tuple[Path, list[dict[str, Any]]] | None = None
        append_write: tuple[Path, list[dict[str, Any]]] | None = None
        if mark_applied:
            draft, path = manager._find_draft(datasource, mark_applied)
            draft["status"] = "applied"
            applied_write = (path, manager._drafts_with(datasource, draft))
        if append_entry is not None:
            path = manager._drafts_path(datasource)
            drafts_data = _load_yaml(path)
            drafts = (list(drafts_data.get("drafts", []))
                      if isinstance(drafts_data, dict) else [])
            drafts.append(append_entry)
            append_write = (path, drafts)
        records = self._load(datasource)
        if any(r.id == rec.id for r in records):
            records = [rec if r.id == rec.id else r for r in records]
        else:
            records.append(rec)

        # ── 写盘（此后不再抛已知异常）：semantics → drafts → changes ──
        _dump_yaml(semantics, working)
        if applied_write is not None:
            manager._save_drafts(applied_write[0], applied_write[1])
        if append_write is not None:
            manager._save_drafts(append_write[0], append_write[1])
        self._save(datasource, records)

        files = ["semantics.yml", "semantic_changes.yml"]
        if mark_applied or append_entry is not None:
            files.append("semantic_drafts.yml")
        trailers = {"Generator": generator, **({"Approved-by": by} if by else {}),
                    **(extra_trailers or {})}
        rec_dict = rec.to_dict()
        return {"record": rec_dict, "files": files, "trailers": trailers}

    async def _finish_merge(self, datasource: str, out: dict, *, message: str,
                            lint_dialect: str | None) -> None:
        await self._kb.force_sync(datasource)
        await self._kb.git_commit(
            datasource, message, files=out["files"],
            lint=self._kb.semantics_lint(datasource, lint_dialect or "sqlite"),
            trailers=out["trailers"] or None)
        metrics.record_semantic_change("merged", out["record"]["origin"])

    async def merge(self, datasource: str, change_id: str, *, by: str,
                    auto: bool = False) -> dict[str, Any]:
        """校验并翻转记录：唯一写 semantics.yml 的落点（I1/I2/I5）。

        乐观并发：记录的 ``base_digest`` 与主线现文本不符 → ``ChangeStale``
        （409）。任何失败都在写盘之前抛出，主线与隔离区零字节变化。
        """
        rec = self.get(datasource, change_id)
        if rec.status != "open":
            raise ChangeError(f"变更 {change_id} 已 {rec.status}")
        warnings, degraded = await self.drift_warnings(datasource, rec.subjects)
        out = self._write_merge(
            datasource, rec, dialect=rec.dialect, by=by, warnings=warnings,
            degraded=degraded, generator="semantic.change.merge",
            extra_trailers={"Auto-approved-by": "deterministic-gate"} if auto else None)
        await self._finish_merge(
            datasource, out, message=f"semantic: merge change {rec.id}",
            lint_dialect=rec.dialect)
        merged = dict(out["record"])
        merged["change_id"] = rec.id
        return merged

    # ── 包装路径（refuse.py / API 现状调用点零改动）────────

    async def merge_draft(self, datasource: str, draft_id: str,
                          dialect: str | None = None, *,
                          by: str = "", generator: str = "") -> dict[str, Any]:
        """pending 草稿 → 合并：（草稿状态守卫 + 冲突守卫 + 写盘 + 单提交）。

        守卫**不搬家**：仍在合并入口（草稿是提货单，合并才是落库那一跳）。
        返回原 draft dict（status=applied）+ ``change_id`` —— 与
        ``SemanticManager.confirm_draft`` 原返回形状一致。
        """
        # 路径安全闸从被收窄的 ``manage.confirm_draft`` 平移过来：委托不能
        # 顺手把守卫丢了（本模块的其它入口同样先过 ``_check``）。
        self._check(datasource)
        draft, _drafts_path = self._manager._find_draft(datasource, draft_id)
        if draft.get("status") != "pending":
            raise ChangeError(f"草稿 {draft_id} 已 {draft.get('status')}")
        if draft.get("conflict"):
            raise ChangeError(
                "该草稿与现有模型冲突,不能直接确认"
                f"({(draft.get('conflict') or {}).get('message') or '见草稿注解'})"
                "——请以它为蓝本修正后新建")
        rec = ChangeRecord(
            id=f"chg-{datetime.now(timezone.utc):%Y%m%d}-{uuid4().hex[:4]}",
            datasource=datasource, origin="draft_confirm", status="open",
            note=str(draft.get("note") or ""), author=by, created_at=_now(),
            subjects=_subjects_of([draft]), draft_id=draft_id,
            payloads=[{"kind": draft.get("kind"), "action": draft.get("action"),
                       "name": draft.get("name"), "payload": draft.get("payload")}],
            dialect=dialect)
        warnings, degraded = await self.drift_warnings(datasource, rec.subjects)
        out = self._write_merge(
            datasource, rec, dialect=dialect, by=by, warnings=warnings,
            degraded=degraded, generator=generator or "semantic.confirm",
            mark_applied=draft_id, fail_prefix="草稿确认失败")
        await self._finish_merge(
            datasource, out,
            message=(f"semantic: confirm {draft.get('kind')} {draft.get('name')} "
                     f"(draft {draft_id})"),
            lint_dialect=dialect)
        return {**draft, "status": "applied", "change_id": rec.id}

    async def merge_auto(self, datasource: str, kind: str, name: str,
                         payload: dict | None = None,
                         note: str = "refuse-auto-confirm", *,
                         actor: str = "") -> dict[str, Any]:
        """A/B 档快速通道：确定性门已过的声明直接入库（I4）。

        与 ``merge_draft`` 的差别只有两处：入队记录 ``status=applied``（从来
        没 pending 过）与 ``Auto-approved-by: deterministic-gate`` trailer ——
        **actor 为空也要带**（I4：审计要能区分「人点的」与「门自动放行的」）。
        """
        # 同上：``manage.auto_apply`` 原有的路径安全闸随之平移。
        self._check(datasource)
        if kind not in _KINDS:
            raise ValueError(f"kind 必须为 {sorted(_KINDS)} 之一")
        entry: dict[str, Any] = {
            "id": uuid4().hex[:12], "kind": kind, "action": "upsert", "name": name,
            "payload": payload or None, "note": note or "", "status": "applied",
            "created_at": _now(),
        }
        rec = ChangeRecord(
            id=f"chg-{datetime.now(timezone.utc):%Y%m%d}-{uuid4().hex[:4]}",
            datasource=datasource, origin="auto_apply", status="open",
            note=note or "", author=actor, created_at=_now(), auto=True,
            subjects=[{"kind": kind, "name": name}],
            payloads=[{"kind": kind, "action": "upsert", "name": name,
                       "payload": payload}])
        warnings, degraded = await self.drift_warnings(datasource, rec.subjects)
        out = self._write_merge(
            datasource, rec, dialect=None, by=actor, warnings=warnings,
            degraded=degraded, generator="refuse.auto_apply",
            extra_trailers={"Auto-approved-by": "deterministic-gate"},
            append_entry=entry)
        await self._finish_merge(
            datasource, out, message=f"semantic: auto-apply {kind} {name}",
            lint_dialect=None)
        return {**entry, "change_id": rec.id}
