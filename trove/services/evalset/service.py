"""验收集的确认门 —— 所有写操作唯一经过的一层。

``evalset.yml`` 的资产模型/存储/状态机在 :mod:`models`/:mod:`store` 里,本
模块只负责**把一次管理动作变成一次受控写入**:

- 每个写方法都在数据源级写锁内 ``load → mutate → save → git commit → audit``
  (锁是非阻塞的,拿不到就抛 :class:`EvalsetBusy`,不排队);
- 资产本身有解析 issue 时**拒绝写入**(``evalset.invalid_asset``)——坏资产
  不能被一次「顺手保存」静默洗成好资产;
- 自动内容永远不绕过确认门:本服务的每个入口都要求显式 ``actor``,而且
  只有 ``confirm`` 会把条目送进评分集合(``evalset_rev`` 只哈希 confirmed);
- git 与审计都是 **best-effort**:版本化失败不阻断已经成功的写入,但失败
  会进日志(静默失败会让「谁在什么时候改了什么」变成一份假历史)。

失败方向沿用全仓的纪律:能拒绝的显式拒绝,拒绝不了的(读 readiness 的
运行摘要)如实降级为 ``None`` + ``degraded``,绝不把「读不到」洗成 0。
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any

from trove.core.logging import get_logger
from trove.services.authz.masking import MaskingError, build_masker
from trove.services.authz.policy import principal_to_wire
from trove.services.evalset.leakage import check_leakage, load_example_questions
from trove.services.evalset.lock import EvalsetLock
from trove.services.evalset.models import (
    MAX_BATCH,
    STATUSES,
    EvalsetDoc,
    EvalsetError,
    EvalsetItem,
    check_transition,
    content_hash,
    new_qid,
    now_iso,
    resolve_duplicates,
    tier_of,
)
from trove.services.evalset.rev import evalset_rev
from trove.services.evalset.store import load_evalset, save_evalset
from trove.services.evalset.templates import GRAINS
from trove.services.kb.ledger import normalize_question
from trove.services.kb.sql_guard import sqlglot_ok, trial_ok

logger = get_logger(__name__)

#: 预览执行的行上限(设计 §7.4:``LIMIT 20``)。
PREVIEW_LIMIT = 20

#: 预览执行超时(秒);超时与执行报错一样按 ``trial_failed`` 拒绝。
PREVIEW_TIMEOUT_S = 5.0

#: readiness 的固定评分分层(``benchmark`` P0 不实现,不进覆盖视图)。
_TIERS = ("verified", "semantic", "synthetic")

#: 覆盖视图统计的问法形态闭集。
_PATTERNS = ("group_by", "time_series", "filter", "top_n")

#: 覆盖率统计的 tags 键(条目 tags 缺这四个键即 ``untagged``)。
_TAG_KEYS = ("metric", "dimension", "time_grain", "pattern")

#: 可被 vouch 的参考 SQL 来源(人工/pipeline 已经不需要升级)。
_VOUCHABLE_ORIGINS = ("semantic_compiler", "llm")


class EvalsetService:
    """确认门服务:create/confirm/reject/edit/preview/vouch/retire/readiness。

    Args:
        project_root: 仓库/项目根(``.trove/`` 在其下)。
        kb: KB 服务(需要 ``kb_dir``、可选 ``semantics_path``、可选 ``git``)。
        registry: 连接器注册表(试执行/预览用;``None`` = 只做语法校验)。
        semantic_layer: 显式语义层 provider(缺省按 KB 的 ``semantics.yml`` 建)。
        config: ``AgentConfig``(``build_masker`` 读脱敏开关;``None`` = 默认配置)。
        audit: ``async (action, actor, details)`` 审计回调;失败不影响写入。
    """

    def __init__(
        self,
        project_root: Path | str,
        *,
        kb: Any,
        registry: Any = None,
        semantic_layer: Any = None,
        config: Any = None,
        audit: Any = None,
    ) -> None:
        self.root = Path(project_root)
        self.kb = kb
        self.registry = registry
        self.semantic_layer = semantic_layer
        self.config = config
        self.audit = audit
        self._lock = EvalsetLock(self.root / ".trove" / "evalsets")

    # ── 写操作:create ───────────────────────────────────────────

    async def create(
        self,
        datasource: str,
        *,
        question: str,
        reference_sql: str,
        tags: dict[str, Any] | None = None,
        note: str = "",
        actor: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """手工新建一条 ``pending`` 条目(创建 ≠ 确认,确认门一条都不绕)。

        当场跑语法/试执行护栏与泄漏检查:两者任一不过**不落盘**(泄漏不受
        ``force`` 豁免 —— ``force`` 只裁决「同题不同 SQL」的冲突);通过后
        仍只是 ``pending``,必须再走 :meth:`confirm`。
        """
        question = str(question or "").strip()
        if not question:
            raise EvalsetError("evalset.invalid_asset", "question must not be empty")
        reference_sql = str(reference_sql or "")

        with self._lock.acquire(datasource):
            doc = self._load_or_raise(datasource)
            await self._validate_reference(datasource, reference_sql)
            hit = self._leakage_hit(datasource, question)
            if hit is not None:
                raise EvalsetError(
                    "evalset.leakage",
                    f"question matches a KB example ({hit.note()}); "
                    f"rewrite it before creating an eval item",
                )

            digest = content_hash(question, reference_sql)
            duplicate_of = ""
            existing = self._duplicate_by_hash(doc, digest)
            if existing is not None:
                raise EvalsetError(
                    "evalset.duplicate_question",
                    f"the same question+SQL already exists as {existing.qid!r}",
                    issues=[{
                        "qid": existing.qid,
                        "question": existing.question,
                        "status": existing.status,
                    }],
                )
            conflict = self._conflict_by_question(doc, question)
            if conflict is not None:
                if not force:
                    raise EvalsetError(
                        "evalset.duplicate_conflict",
                        f"the same question exists with different SQL as "
                        f"{conflict.qid!r}; pass force=true to keep both",
                        issues=[{
                            "qid": conflict.qid,
                            "question": conflict.question,
                            "reference_sql": conflict.reference_sql,
                            "status": conflict.status,
                        }],
                    )
                duplicate_of = conflict.qid

            item = EvalsetItem(
                qid=new_qid(),
                question=question,
                reference_sql=reference_sql,
                source="manual",
                reference_origin="human",
                status="pending",
                match="exact",
                tags=dict(tags or {}),
                content_hash=digest,
                duplicate_of=duplicate_of,
                note=str(note or ""),
                created_at=now_iso(),
                created_by=actor,
                generator="manual",
            )
            doc.items.append(item)
            path = save_evalset(self.root, doc)
            self._commit(
                path, f"evalset: create {item.qid} for {datasource}", actor,
            )
            await self._audit_event("evalset.create", actor, {
                "qid": item.qid,
                "question": question,
                "duplicate_of": duplicate_of,
                "force": bool(force),
            })
            return self._result(doc, item)

    # ── 写操作:confirm / confirm_batch ─────────────────────────

    async def confirm(
        self, datasource: str, qid: str, *, actor: str, force: bool = False,
    ) -> dict[str, Any]:
        """单条确认:前置条件全过才进 confirmed(并进入 ``evalset_rev``)。"""
        with self._lock.acquire(datasource):
            doc = self._load_or_raise(datasource)
            item = self._find(doc, qid)
            await self._confirm_locked(
                datasource, doc, item, actor=actor, force=force,
            )
            path = save_evalset(self.root, doc)
            self._commit(
                path, f"evalset: confirm {qid} for {datasource}", actor,
            )
            await self._audit_event("evalset.confirm", actor, {
                "qid": qid,
                "force": bool(force),
            })
            return self._result(doc, item)

    async def confirm_batch(
        self,
        datasource: str,
        qids: list[str],
        *,
        actor: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """批量确认:整批形状先验,锁内逐条隔离失败,有成功才落盘一次。

        批量是「少点几次鼠标」不是「放宽判定」:每条仍走完整的
        :meth:`_confirm_locked`,一条失败不连坐其余;非法/超限整批拒绝,
        绝不截断成半执行。
        """
        checked = self._validate_batch_qids(qids)
        results: list[dict[str, Any]] = []
        applied = 0
        failed = 0

        with self._lock.acquire(datasource):
            doc = self._load_or_raise(datasource)
            for qid in checked:
                try:
                    item = self._find(doc, qid)
                    await self._confirm_locked(
                        datasource, doc, item, actor=actor, force=force,
                    )
                except EvalsetError as exc:
                    failed += 1
                    ok = False
                    results.append({
                        "qid": qid,
                        "ok": False,
                        "error": {"code": exc.code, "message": exc.message},
                    })
                else:
                    applied += 1
                    ok = True
                    results.append({"qid": qid, "ok": True})
                await self._audit_event("evalset.confirm", actor, {
                    "qid": qid,
                    "batch": True,
                    "ok": ok,
                    "force": bool(force),
                })

            if applied:
                path = save_evalset(self.root, doc)
                listed = ", ".join(checked[:5])
                more = f" (+{len(checked) - 5} more)" if len(checked) > 5 else ""
                self._commit(
                    path,
                    f"evalset: confirm {applied} item(s) for {datasource} "
                    f"({listed}{more})",
                    actor,
                )
            return {
                "results": results,
                "applied": applied,
                "failed": failed,
                "evalset_rev": evalset_rev(doc),
            }

    # ── 写操作:reject / edit / vouch / retire ──────────────────

    async def reject(
        self, datasource: str, qid: str, *, actor: str, reason: str = "",
    ) -> dict[str, Any]:
        """拒绝一条待审条目(墓碑:参与生成去重,不参与评分)。"""
        with self._lock.acquire(datasource):
            doc = self._load_or_raise(datasource)
            item = self._find(doc, qid)
            check_transition(item, "reject")
            item.status = "rejected"
            item.rejection_reason = str(reason or "")
            resolve_duplicates(doc, item.qid)
            path = save_evalset(self.root, doc)
            self._commit(
                path, f"evalset: reject {qid} for {datasource}", actor,
            )
            await self._audit_event("evalset.reject", actor, {
                "qid": qid,
                "reason": item.rejection_reason,
            })
            return self._result(doc, item)

    async def edit(
        self,
        datasource: str,
        qid: str,
        *,
        actor: str,
        question: str | None = None,
        reference_sql: str | None = None,
        tags: dict[str, Any] | None = None,
        note: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """编辑 pending/confirmed 条目。

        ``confirmed`` 的编辑必须带 ``note``(并重记 ``confirmed_by``/时间):
        改一道已经在评分集合里的题,却不需要说明为什么,等于让
        ``evalset_rev`` 的变化失去可解释的上下文。问题变 → 重查泄漏并刷新
        标记;SQL 变 → 重跑护栏;``content_hash`` **始终重算**(Task 7 的
        载重约束:不能信任存量哈希)。
        """
        with self._lock.acquire(datasource):
            doc = self._load_or_raise(datasource)
            item = self._find(doc, qid)
            check_transition(item, "edit")
            was_confirmed = item.status == "confirmed"
            note_text = None if note is None else str(note).strip()
            if was_confirmed and not note_text:
                raise EvalsetError(
                    "evalset.note_required",
                    "editing a confirmed item requires a note",
                )

            question_changed = False
            if question is not None:
                new_question = str(question).strip()
                if not new_question:
                    raise EvalsetError(
                        "evalset.invalid_asset", "question must not be empty",
                    )
                if new_question != item.question:
                    question_changed = True
                    item.question = new_question
                    # 编辑问题后**自动重查并更新**泄漏标记(设计 §6.5):
                    # 编辑是改写题的合法路径,确认门才是拒绝的出口。
                    hit = self._leakage_hit(datasource, new_question)
                    item.leakage = hit is not None
                    item.leakage_hit = hit.note() if hit is not None else ""

            reference_sql_changed = False
            if reference_sql is not None:
                # 显式给出的参考 SQL 是**人工提供的 oracle**(采集条目正是
                # 靠它补齐 `needs_reference`):即使文本与存量相同也照常过
                # 护栏 —— 存量可能来自未经护栏的路径,文本相同不代表它合规。
                candidate = str(reference_sql)
                reference_sql_changed = candidate != item.reference_sql
                await self._validate_reference(datasource, candidate)
                item.reference_sql = candidate
                item.needs_reference = False

            if tags is not None:
                item.tags = dict(tags or {})
            if note_text:
                item.note = (
                    f"{item.note}; {note_text}" if item.note else note_text
                )
            if was_confirmed:
                marker = f"edited after confirm by {actor}"
                item.note = f"{item.note}; {marker}" if item.note else marker
                item.confirmed_at = now_iso()
                item.confirmed_by = actor

            item.content_hash = content_hash(item.question, item.reference_sql)
            path = save_evalset(self.root, doc)
            self._commit(
                path, f"evalset: edit {qid} for {datasource}", actor,
            )
            await self._audit_event("evalset.edit", actor, {
                "qid": qid,
                "question_changed": question_changed,
                "reference_sql_changed": reference_sql_changed,
                "force": bool(force),
            })
            return self._result(doc, item)

    async def vouch(
        self, datasource: str, qid: str, *, actor: str, note: str,
    ) -> dict[str, Any]:
        """把 confirmed 的 semantic/synthetic 参考 SQL 升级为人工背书。

        只改 ``reference_origin``(``source`` 不变 —— 问题仍是生成的),
        tier 随即从 ``semantic``/``synthetic`` 变为 ``verified``。这是「我
        为该 SQL 的业务口径背书」,不是「这是一道好题」;审计保留原 origin。
        """
        note_text = str(note or "").strip()
        if not note_text:
            raise EvalsetError("evalset.note_required", "vouch requires a note")
        with self._lock.acquire(datasource):
            doc = self._load_or_raise(datasource)
            item = self._find(doc, qid)
            check_transition(item, "vouch")
            if item.reference_origin not in _VOUCHABLE_ORIGINS:
                raise EvalsetError(
                    "evalset.invalid_transition",
                    f"cannot vouch an item whose reference_origin is "
                    f"{item.reference_origin!r} "
                    f"(vouchable: {', '.join(_VOUCHABLE_ORIGINS)})",
                )
            previous_origin = item.reference_origin
            item.reference_origin = "human"
            item.vouched_at = now_iso()
            item.vouched_by = actor
            marker = f"vouched: {note_text}"
            item.note = f"{item.note}; {marker}" if item.note else marker
            path = save_evalset(self.root, doc)
            self._commit(
                path, f"evalset: vouch {qid} for {datasource}", actor,
            )
            await self._audit_event("evalset.vouch", actor, {
                "qid": qid,
                "previous_origin": previous_origin,
                "note": note_text,
            })
            return self._result(doc, item)

    async def retire(
        self, datasource: str, qid: str, *, actor: str, reason: str = "",
    ) -> dict[str, Any]:
        """确认后的条目退出评分(不硬删除:历史 run 仍按当时条目可解释)。"""
        with self._lock.acquire(datasource):
            doc = self._load_or_raise(datasource)
            item = self._find(doc, qid)
            check_transition(item, "retire")
            item.status = "retired"
            item.retired_at = now_iso()
            reason_text = str(reason or "").strip()
            if reason_text:
                marker = f"retired: {reason_text}"
                item.note = f"{item.note}; {marker}" if item.note else marker
            resolve_duplicates(doc, item.qid)
            path = save_evalset(self.root, doc)
            self._commit(
                path, f"evalset: retire {qid} for {datasource}", actor,
            )
            await self._audit_event("evalset.retire", actor, {
                "qid": qid,
                "reason": reason_text,
            })
            return self._result(doc, item)

    # ── 只读:preview ────────────────────────────────────────────

    async def preview(
        self, datasource: str, qid: str, *, principal: Any = None,
    ) -> dict[str, Any]:
        """执行参考 SQL 的有界预览(20 行,5s),经脱敏后才返回。

        刻意**不拿写锁**:预览是只读的,一场正在进行的生成不该阻塞管理员
        看一条已有 SQL 的执行结果(资产写入用 ``os.replace`` 原子换名,读到
        的永远是一份完整版本)。

        失败方向取严:SQL 解析失败/执行失败都不会把未脱敏的行交出去;
        脱敏开启而模型读不出来 → **执行前**就以 ``evalset.masking_refused``
        拒绝(绝不先取一批自己脱不了敏的行);``apply`` 阶段的
        :class:`MaskingError` 同样拒绝。
        """
        doc = self._load_or_raise(datasource)
        item = self._find(doc, qid)
        sql = item.reference_sql
        if not sqlglot_ok(sql):
            raise EvalsetError(
                "evalset.sql_invalid", "reference SQL does not parse",
            )
        if self.registry is None:
            raise EvalsetError(
                "evalset.trial_failed",
                "no datasource registry is configured for preview",
            )

        # 脱敏依据先于执行取齐:``build_masker`` 只在部署级开启时返回
        # masker,而开启后**没有可读语义模型**时它的 ``apply`` 会原样返回
        # 行 —— 那不是「脱敏过」。模型读不出来就在取数之前拒绝(设计 §7.4)。
        provider = await self._provider(datasource)
        masker = build_masker(semantic_layer=provider, config=self.config)
        masking: Any = "off"
        if masker is not None:
            model = None
            if provider is not None:
                try:
                    model = provider.model()
                except Exception as exc:  # noqa: BLE001 — provider 契约是从不上抛
                    raise EvalsetError(
                        "evalset.masking_refused",
                        "masking is enabled but the semantic model is "
                        f"unavailable: {exc}",
                    ) from exc
            if model is None:
                raise EvalsetError(
                    "evalset.masking_refused",
                    "masking is enabled but the semantic model is unavailable",
                )

        wrapped = f"SELECT * FROM ({sql}) t LIMIT {PREVIEW_LIMIT}"
        try:
            result = await asyncio.wait_for(
                self.registry.execute(wrapped, datasource),
                timeout=PREVIEW_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — 超时/方言/表名错误同一种事实
            raise EvalsetError(
                "evalset.trial_failed", f"reference SQL failed: {exc}",
            ) from exc

        columns = list(getattr(result, "columns", []) or [])
        rows = [list(row) for row in (getattr(result, "rows", []) or [])]
        if masker is not None:
            try:
                rows, report = masker(
                    rows, columns,
                    principal=principal_to_wire(principal),
                    sql=sql,
                )
            except MaskingError as exc:
                # 拒绝 = 响应里没有行;降级放行即泄漏,没有下层兜底。
                raise EvalsetError(
                    "evalset.masking_refused", str(exc),
                ) from exc
            masking = report
        return {"qid": qid, "columns": columns, "rows": rows, "masking": masking}

    # ── 只读:list_items / readiness ─────────────────────────────

    async def list_items(
        self,
        datasource: str,
        *,
        status: str = "",
        tier: str = "",
        source: str = "",
        q: str = "",
    ) -> dict[str, Any]:
        """待审/已确认队列(排序稳定:状态组 → 创建时间降序 → qid 升序)。

        计数与 ``by_tier`` 是**全量投影**(不受过滤器影响):待审队列顶部的
        徽标回答的是「这个数据源总共有多少题」,不是「当前筛选出多少」。
        """
        doc, issues, _exists = load_evalset(self.root, datasource)
        all_items = list(doc.items)

        filtered = all_items
        if status:
            filtered = [it for it in filtered if it.status == status]
        if source:
            filtered = [it for it in filtered if it.source == source]
        if tier:
            filtered = [it for it in filtered if tier_of(it) == tier]
        needle = str(q or "").strip().lower()
        if needle:
            filtered = [
                it for it in filtered if needle in (it.question or "").lower()
            ]

        # 稳定三趟排序:qid 升序 → created_at 降序 → 状态组顺序。
        ordered = sorted(filtered, key=lambda it: it.qid)
        ordered = sorted(
            ordered, key=lambda it: it.created_at or "", reverse=True,
        )
        ordered = sorted(ordered, key=self._status_rank)

        return {
            "datasource": datasource,
            "items": [
                {**it.to_dict(), "tier": tier_of(it)} for it in ordered
            ],
            "counts": self._counts(all_items),
            "by_tier": self._by_tier(all_items),
            "issues": [issue.as_dict() for issue in issues],
            "degraded": [],
        }

    async def readiness(self, datasource: str) -> dict[str, Any]:
        """只读就绪/覆盖视图(零 LLM、零写入)。

        三态必须可区分:``latest_runs.<tier> = null`` = 没跑过;有摘要 =
        那一次的指标;摘要存在但读不出 = 该 tier ``None`` + ``available=
        false`` + ``degraded``(去重一条)。语义模型/连接器不可用只影响
        ``coverage``,不让整份 readiness 报错(R16)。

        资产本身读不出(`evalset.yml` 坏/解析 issue)同样不是「空集」:
        ``available=false`` + 一条 ``asset_unreadable``,计数/覆盖/修订一律
        ``None`` —— 拿空文档算一份全 0 的「健康报告」会静默把坏资产洗成
        「还没建验收集」。
        """
        doc, issues, _exists = load_evalset(self.root, datasource)
        all_items = list(doc.items)
        degraded: list[dict[str, Any]] = []
        available = not issues

        if issues:
            degraded.append({
                "code": "asset_unreadable",
                "message": (
                    "could not read evalset.yml; counts and coverage are "
                    "unavailable: " + "; ".join(
                        f"{issue.code}: {issue.message}" for issue in issues[:3]
                    ) + (" …" if len(issues) > 3 else "")
                ),
            })
            coverage: dict[str, Any] | None = None
            counts: dict[str, int] | None = None
            by_tier: dict[str, dict[str, int]] | None = None
            rev: str | None = None
        else:
            coverage = await self._coverage(datasource, all_items, degraded)
            counts = self._counts(all_items)
            by_tier = self._by_tier(all_items)
            rev = evalset_rev(doc)

        latest_runs: dict[str, Any] = {}
        unreadable_tiers: list[str] = []
        for tier in _TIERS:
            summary, readable = self._read_summary(datasource, tier)
            latest_runs[tier] = summary
            if not readable:
                available = False
                unreadable_tiers.append(tier)
        if unreadable_tiers:
            degraded.append({
                "code": "run_store_unreadable",
                "message": (
                    "could not read latest.summary.json for tier(s): "
                    + ", ".join(unreadable_tiers)
                ),
            })

        return {
            "datasource": datasource,
            "available": available,
            "counts": counts,
            "by_tier": by_tier,
            "coverage": coverage,
            "latest_runs": latest_runs,
            "degraded": degraded,
            "evalset_rev": rev,
        }

    # ── 内部:加载/校验 ─────────────────────────────────────────

    def _load_or_raise(self, datasource: str) -> EvalsetDoc:
        """读资产;有任何解析 issue 即拒绝写入(坏资产不被静默覆盖)。"""
        doc, issues, _exists = load_evalset(self.root, datasource)
        if issues:
            raise EvalsetError(
                "evalset.invalid_asset",
                "evalset asset has parse issues; refusing to modify it",
                issues=[issue.as_dict() for issue in issues],
            )
        return doc

    @staticmethod
    def _find(doc: EvalsetDoc, qid: str) -> EvalsetItem:
        item = next((it for it in doc.items if it.qid == qid), None)
        if item is None:
            raise EvalsetError("evalset.not_found", f"no eval item {qid!r}")
        return item

    async def _confirm_locked(
        self,
        datasource: str,
        doc: EvalsetDoc,
        item: EvalsetItem,
        *,
        actor: str,
        force: bool,
    ) -> None:
        """确认的前置条件链(设计 §7.1),顺序即优先级。

        所有校验都在**任何变更之前**:校验失败时条目保持原样(批量确认的
        失败隔离依赖这一点 —— 一条失败的条目不能被留在半改状态)。
        ``doc``/``item`` 由调用方在锁内读入,本方法只做状态更新与
        ``duplicate_of`` 清理,不落盘。
        """
        check_transition(item, "confirm")
        if item.needs_reference or not item.reference_sql.strip():
            raise EvalsetError(
                "evalset.missing_reference",
                "item has no reference SQL or is marked needs_reference",
            )
        if item.match != "exact":
            raise EvalsetError(
                "evalset.unsupported_match",
                f"match {item.match!r} is not supported for confirmation "
                f"(only 'exact')",
            )
        await self._validate_reference(datasource, item.reference_sql)
        # 确认时**现读现查**:生成之后 examples.yml 可能又新增了示例。
        hit = self._leakage_hit(datasource, item.question)
        if hit is not None:
            raise EvalsetError(
                "evalset.leakage",
                f"question matches a KB example ({hit.note()})",
            )
        # 现读现查通过 → 清掉生成/历史留下的旧标记(与 edit 同一取向:
        # 标记描述的是「最近一次判定的结果」,不是一条永久污点)。
        item.leakage = False
        item.leakage_hit = ""
        if item.duplicate_of:
            if not force:
                raise EvalsetError(
                    "evalset.duplicate_conflict",
                    f"item is marked duplicate_of={item.duplicate_of!r}; "
                    f"pass force=true to confirm it anyway",
                    issues=[{"qid": item.duplicate_of}],
                )
            marker = f"force confirm despite duplicate_of={item.duplicate_of}"
            item.note = f"{item.note}; {marker}" if item.note else marker

        item.status = "confirmed"
        item.confirmed_at = now_iso()
        item.confirmed_by = actor
        # 始终重算:存量哈希可能是别的写入路径留下的,不能拿它当身份。
        item.content_hash = content_hash(item.question, item.reference_sql)
        resolve_duplicates(doc, item.qid)

    async def _validate_reference(self, datasource: str, sql: str) -> None:
        """参考 SQL 的两道确定性护栏:SQLGlot 语法 → 有界试执行。

        ``registry is None`` 时只做语法校验(测试/无连接场景);有连接器时
        试执行失败一律拒绝 —— 确认的必须是一条**能跑**的 SQL。
        """
        if not sqlglot_ok(sql):
            raise EvalsetError(
                "evalset.sql_invalid", "reference SQL does not parse",
            )
        if self.registry is not None and not await trial_ok(
            self.registry, datasource, sql,
        ):
            raise EvalsetError(
                "evalset.trial_failed",
                "reference SQL failed trial execution",
            )

    @staticmethod
    def _validate_batch_qids(qids: Any) -> list[str]:
        """整批形状校验:非空、≤ ``MAX_BATCH``、全为非空字符串、无重复。

        任何一条不满足都**整批**拒绝(``evalset.invalid_batch``),不截断、
        不半执行 —— 一个被悄悄丢掉了 20 条的「批量确认成功」比报错更危险。
        """
        if not isinstance(qids, (list, tuple)) or not qids:
            raise EvalsetError(
                "evalset.invalid_batch", "qids must be a non-empty list",
            )
        if len(qids) > MAX_BATCH:
            raise EvalsetError(
                "evalset.invalid_batch",
                f"at most {MAX_BATCH} qids per batch, got {len(qids)}",
            )
        checked: list[str] = []
        seen: set[str] = set()
        for qid in qids:
            if not isinstance(qid, str) or not qid.strip():
                raise EvalsetError(
                    "evalset.invalid_batch",
                    "every qid must be a non-empty string",
                )
            value = qid.strip()
            if value in seen:
                raise EvalsetError(
                    "evalset.invalid_batch", f"duplicate qid {value!r} in batch",
                )
            seen.add(value)
            checked.append(value)
        return checked

    # ── 内部:去重/泄漏 ─────────────────────────────────────────

    @staticmethod
    def _effective_hash(item: EvalsetItem) -> str:
        return item.content_hash or content_hash(
            item.question, item.reference_sql,
        )

    def _duplicate_by_hash(
        self, doc: EvalsetDoc, digest: str,
    ) -> EvalsetItem | None:
        """同 ``content_hash`` 的已有条目(``retired`` 不参与去重)。"""
        for existing in doc.items:
            if existing.status == "retired":
                continue
            if self._effective_hash(existing) == digest:
                return existing
        return None

    @staticmethod
    def _conflict_by_question(
        doc: EvalsetDoc, question: str,
    ) -> EvalsetItem | None:
        """同 ledger 归一问题、不同 SQL 的已有条目(第一条)。"""
        key = normalize_question(question)
        if not key:
            return None
        for existing in doc.items:
            if existing.status == "retired":
                continue
            if normalize_question(existing.question) == key:
                return existing
        return None

    def _leakage_hit(self, datasource: str, question: str):
        """现读 ``examples.yml`` 再查泄漏(确认/编辑/新建共用)。

        命中说明用 ``hit.note()`` 存/展示;``hit.question`` 是命中的**示例**
        问句,绝不能当成条目自己的问题写回去。
        """
        return check_leakage(
            question, load_example_questions(self._examples_path(datasource)),
        )

    def _examples_path(self, datasource: str) -> Path:
        kb_dir = getattr(self.kb, "kb_dir", None)
        base = Path(kb_dir) if kb_dir is not None else (
            self.root / ".trove" / "kb"
        )
        return base / datasource / "examples.yml"

    # ── 内部:语义层/方言 ───────────────────────────────────────

    async def _provider(self, datasource: str) -> Any:
        """语义层 provider(显式注入优先,否则按 KB 的 ``semantics.yml`` 建)。"""
        if self.semantic_layer is not None:
            return self.semantic_layer
        kb_path: Path | None = None
        semantics_path = getattr(self.kb, "semantics_path", None)
        if callable(semantics_path):
            try:
                kb_path = Path(semantics_path(datasource))
            except Exception as exc:  # noqa: BLE001 — 路径解析失败不是致命
                logger.warning(
                    "evalset service: semantics path lookup failed for %s: %s",
                    datasource, exc,
                )
        try:
            from trove.services.semantic_layer.provider import (
                SemanticLayerProvider,
            )
            return SemanticLayerProvider(
                directory=self.root / ".trove" / "semantic" / datasource,
                datasource=datasource,
                dialect=await self._dialect(datasource),
                kb_semantics_path=kb_path,
            )
        except Exception as exc:  # noqa: BLE001 — R16:readiness 不上抛
            logger.warning(
                "evalset service: semantic provider unavailable for %s: %s",
                datasource, exc,
            )
            return None

    async def _dialect(self, datasource: str) -> str:
        """探测连接器方言;不可得一律回退 ``sqlite``(R13)。

        真实 ``ConnectorRegistry.get`` 是 async,测试 fake 是同步的 —— 两种
        都接受(``inspect.isawaitable``),不因调用形状不同把 readiness 打崩。
        """
        getter = getattr(self.registry, "get", None)
        if not callable(getter):
            return "sqlite"
        try:
            adapter = getter(datasource)
            if inspect.isawaitable(adapter):
                adapter = await adapter
            dialect = getattr(adapter, "dialect", None)
            value = dialect() if callable(dialect) else None
            if value:
                return str(value)
        except Exception:  # noqa: BLE001 — 方言探测失败不阻断只读视图
            logger.warning(
                "evalset service: dialect probe failed for %s; "
                "assuming sqlite", datasource,
            )
        return "sqlite"

    # ── 内部:覆盖/统计 ─────────────────────────────────────────

    async def _coverage(
        self,
        datasource: str,
        items: list[EvalsetItem],
        degraded: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """已确认条目对语义模型声明面的覆盖(只读投影)。

        语义模型不可用 → ``None`` + ``no_semantic_model`` degraded(不抛,
        也不伪造一份全 0 的「覆盖报告」—— 0/0 与「一条都没覆盖」不是同
        一件事)。
        """
        provider = await self._provider(datasource)
        model = None
        if provider is not None:
            try:
                model = provider.model()
            except Exception as exc:  # noqa: BLE001 — provider 契约是从不上抛
                logger.warning(
                    "evalset service: semantic model load failed for %s: %s",
                    datasource, exc,
                )
                model = None
        if model is None:
            degraded.append({
                "code": "no_semantic_model",
                "message": "semantic model is unavailable; coverage not computed",
            })
            return None

        metrics_total = sorted({
            str(metric.name) for metric in (model.metrics or ())
            if getattr(metric, "name", "")
        })
        dimensions_total = sorted({
            str(field.name)
            for dataset in (model.datasets or ())
            for field in (getattr(dataset, "fields", ()) or ())
            if getattr(field, "name", "")
            and getattr(field, "semantic_role", "") in ("dimension", "enum")
            and not getattr(field, "is_time", False)
        })
        grains_total = list(GRAINS[:3])
        patterns_total = list(_PATTERNS)

        covered: dict[str, set[str]] = {key: set() for key in _TAG_KEYS}
        untagged = 0
        for item in items:
            if item.status != "confirmed":
                continue
            tagged = False
            for key in _TAG_KEYS:
                values = self._tag_values(item.tags, key)
                if values:
                    tagged = True
                    covered[key].update(values)
            if not tagged:
                untagged += 1

        def count_block(total: list[str], reached: set[str]) -> dict[str, Any]:
            missing = [name for name in total if name not in reached]
            return {
                "covered": len(total) - len(missing),
                "total": len(total),
                "missing": missing,
            }

        def list_block(total: list[str], reached: set[str]) -> dict[str, Any]:
            return {
                "covered": [name for name in total if name in reached],
                "missing": [name for name in total if name not in reached],
            }

        return {
            "metrics": count_block(metrics_total, covered["metric"]),
            "dimensions": count_block(dimensions_total, covered["dimension"]),
            "time_grains": list_block(grains_total, covered["time_grain"]),
            "patterns": list_block(patterns_total, covered["pattern"]),
            "untagged": untagged,
        }

    @staticmethod
    def _tag_values(tags: Any, key: str) -> list[str]:
        """读一个 tags 键;容忍单值/列表/其他形状(只认字符串值)。"""
        if not isinstance(tags, dict):
            return []
        raw = tags.get(key)
        if raw is None:
            return []
        values = [raw] if isinstance(raw, str) else raw
        if not isinstance(values, (list, tuple, set)):
            return []
        out: list[str] = []
        for value in values:
            text = str(value).strip()
            if text:
                out.append(text)
        return out

    @staticmethod
    def _counts(items: list[EvalsetItem]) -> dict[str, int]:
        """全状态计数 + ``total``(恰为 ``STATUSES`` 每键一个 + ``total``)。

        未知状态(容错解析保留了条目)不计进任何已知桶,但会计入 ``total``:
        两个数对不上时,``issues`` 里的 ``unknown_status`` 就是解释。
        """
        counts = {name: 0 for name in STATUSES}
        for item in items:
            if item.status in counts:
                counts[item.status] += 1
        counts["total"] = len(items)
        return counts

    @staticmethod
    def _by_tier(items: list[EvalsetItem]) -> dict[str, dict[str, int]]:
        """``verified/semantic/synthetic`` 的 confirmed/pending 分布。"""
        out = {tier: {"confirmed": 0, "pending": 0} for tier in _TIERS}
        for item in items:
            tier = tier_of(item)
            if tier in out and item.status in ("confirmed", "pending"):
                out[tier][item.status] += 1
        return out

    @staticmethod
    def _status_rank(item: EvalsetItem) -> int:
        try:
            return STATUSES.index(item.status)
        except ValueError:  # 容错解析保留了未知状态 → 排在已知组之后
            return len(STATUSES)

    # ── 内部:运行摘要 ─────────────────────────────────────────

    def _run_summary_path(self, datasource: str, tier: str) -> Path:
        return (
            self.root / ".trove" / "eval" / "evalsets" / datasource
            / tier / "latest.summary.json"
        )

    def _read_summary(
        self, datasource: str, tier: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """读 ``latest.summary.json`` → ``(summary, readable)``。

        ``(None, True)`` = 没跑过(合法空态);``(None, False)`` = 文件存在但
        读不出/不是 JSON 对象 —— 后者必须让 ``available=False``,不能把
        「读不到」洗成「没跑过」。
        """
        path = self._run_summary_path(datasource, tier)
        if not path.is_file():
            return None, True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — R16:「读不到」绝不上抛
            logger.warning(
                "evalset service: summary unreadable for %s/%s: %s",
                datasource, tier, exc,
            )
            return None, False
        if not isinstance(data, dict):
            logger.warning(
                "evalset service: summary for %s/%s is not a JSON object",
                datasource, tier,
            )
            return None, False
        return data, True

    # ── 内部:提交/审计/结果 ────────────────────────────────────

    #: ``commit_files`` 的常态性不提交原因(不值得每次写操作都报一条告警)。
    _QUIET_COMMIT_REASONS = frozenset({"disabled", "no-repo", "nothing-to-commit"})

    def _commit(self, path: Path, message: str, actor: str) -> None:
        """只暂存这一份 ``evalset.yml`` 的 best-effort 提交。

        ``kb.git`` 缺失是正常形态(未启用版本化);``commit_files`` 返回的
        状态字典不改写操作结果 —— 版本化失败绝不阻断已经成功的写入,但
        非常态的失败(lint/暂存/提交报错)会记日志,不让它烂在静默里。
        """
        git = getattr(self.kb, "git", None)
        if git is None:
            return
        commit_files = getattr(git, "commit_files", None)
        if not callable(commit_files):
            return
        try:
            status = commit_files(
                [path],
                message,
                trailers={"Approved-by": actor, "Generator": "evalset"},
            )
        except Exception as exc:  # noqa: BLE001 — 版本化绝不阻断写入
            logger.warning("evalset service: git commit failed: %s", exc)
            return
        if not isinstance(status, dict) or status.get("committed") is not False:
            return
        reason = str(status.get("reason") or "")
        if reason not in self._QUIET_COMMIT_REASONS:
            logger.warning(
                "evalset service: git commit not applied (%s): %s",
                reason or "unknown", message,
            )

    async def _audit_event(
        self, action: str, actor: str, details: dict[str, Any],
    ) -> None:
        """一次写操作一条审计;回调失败只记日志(不使写入失败)。"""
        if self.audit is None:
            return
        try:
            outcome = self.audit(action, actor, details)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # noqa: BLE001 — 审计 best-effort
            logger.warning("evalset service: audit callback failed: %s", exc)

    @staticmethod
    def _result(doc: EvalsetDoc, item: EvalsetItem) -> dict[str, Any]:
        """单条写操作的统一返回形状(条目 + 冻结后的评分修订 + 派生 tier)。

        ``tier`` 是 R22 的 additive key:管理端 ``POST /items`` 的契约要求
        响应直接带派生分层,而 ``to_dict()`` 刻意不落盘 tier —— 服务层是
        唯一知道「刚刚写入的是哪一条」的地方。
        """
        return {
            **item.to_dict(),
            "evalset_rev": evalset_rev(doc),
            "tier": tier_of(item),
        }


__all__ = ["EvalsetService", "PREVIEW_LIMIT", "PREVIEW_TIMEOUT_S"]
