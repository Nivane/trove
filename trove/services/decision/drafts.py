"""决策规则草稿 —— ``decisions.yml`` 前的等候室(pending → confirm / reject)。

**为什么草稿另存一份文件**,而不是「decisions.yml 里给规则加 pending 标记、
消费端过滤」:``decisions.yml`` 是决策执行面的唯一读源(``KbService.load_decisions``
→ ``DecisionService`` / 调度任务 / 管理端规则列表 / jobs 的规则引用检查)。
标记法要求**每一个**消费点都记得过滤,漏一处就是一条静默生效的规则 ——
而"静默生效"正是这一层最不可接受的结局(一条没人审过的规则按点触发、
发通知、进治理待办)。另存 ``decision_drafts.yml`` 让「未生效」成为**结构性
事实**:草稿根本不在 decisions.yml 里,执行面没有任何一条路径能读到它。

形态与语义层审批流同构(``semantic_drafts.yml``:pending → confirm 应用到
semantics.yml → applied;reject → rejected),字段命名也照抄 —— 管理端两处
队列读起来是同一套状态机。confirm 走 ``KbService.save_decisions`` 这个**既有
写入门**(lint 不过拒写 + git 自动提交),不复刻它的校验。

草稿文件不进检索镜像(它不是可检索资产),写路径只做 git 版本化。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trove.core.logging import get_logger
from trove.services.decision.rules import (
    DecisionRule,
    RuleError,
    lint_rule,
    parse_rule,
    rule_to_dict,
)

logger = get_logger(__name__)

#: 草稿文件名 —— 与 ``semantics_drafts.yml`` 同一命名法,在 KB 数据源目录下。
DRAFTS_FILE = "decision_drafts.yml"

#: 与语义层草稿同一套状态词。
STATUSES = ("pending", "applied", "rejected")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DecisionDraft:
    """一条规则草稿。``rule`` 是**规则本体**(``rule_to_dict`` 的往返形状),
    所以草稿读回来能直接喂 ``parse_rule`` / ``rule_to_dict``,不需要第二套
    模式。"""

    id: str
    rule: dict[str, Any]
    status: str = "pending"
    source: str = ""
    note: str = ""
    created_at: str = field(default_factory=_now)
    applied_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "rule": dict(self.rule),
            "status": self.status,
            "source": self.source,
            "note": self.note,
            "created_at": self.created_at,
        }
        if self.applied_at:
            out["applied_at"] = self.applied_at
        return out

    @property
    def rule_id(self) -> str:
        return str(self.rule.get("id") or "")


class DecisionDraftStore:
    """``.trove/kb/<datasource>/decision_drafts.yml`` 的读写。

    构造只吃 ``KbService``(拿 kb_dir / decisions 路径 / 既有写门),不自己
    new 一个 git 层 —— 版本化与 KB 同源才能保证守卫(只暂存点名文件、失败
    降级 no-op)只有一份实现。
    """

    def __init__(self, kb: Any) -> None:
        self.kb = kb

    # ── paths / IO ───────────────────────────────────────

    def path(self, datasource: str) -> Path:
        """草稿文件与 ``decisions.yml`` 同目录(KB 数据源目录内,受 git 版本化)。"""
        return self.kb.decisions_path(datasource).parent / DRAFTS_FILE

    def load(self, datasource: str) -> list[dict[str, Any]]:
        """全部草稿记录(原样 dict)。文件缺失/读不通 → ``[]``。

        读路径宽容与 KB 其余资产一致:一份暂时读不通的草稿文件不该让
        「列规则」也跟着坏掉。**写路径**不宽容(见 ``_write``)。
        """
        path = self.path(datasource)
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            logger.warning("decision_drafts.yml 读取失败: %s", path, exc_info=True)
            return []
        entries = data.get("drafts") if isinstance(data, dict) else None
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    def grouped(self, datasource: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {s: [] for s in STATUSES}
        for e in self.load(datasource):
            out.setdefault(str(e.get("status") or "pending"), []).append(e)
        return out

    def find(self, datasource: str, draft_id: str) -> dict[str, Any] | None:
        return next((e for e in self.load(datasource) if e.get("id") == draft_id), None)

    def find_rule(self, datasource: str, rule_id: str,
                  status: str = "pending") -> dict[str, Any] | None:
        return next(
            (e for e in self.load(datasource)
             if str((e.get("rule") or {}).get("id") or "") == rule_id
             and (not status or e.get("status") == status)),
            None,
        )

    def _write(self, datasource: str, entries: list[dict[str, Any]]) -> None:
        """落盘 —— **严格**:读不通就不写(与 ``KbService._read_examples``
        的写侧姿态同源)。草稿文件读不通时照写,就是把一份坏文件覆盖成空文档;
        这里宁可抛,也不拿默认值当事实。"""
        path = self.path(datasource)
        if path.exists():
            try:
                yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise RuleError(
                    f"{DRAFTS_FILE} 无法解析,拒绝覆盖: {exc}",
                ) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump({"drafts": entries}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ── 写路径 ───────────────────────────────────────────

    def add(self, datasource: str, rule: DecisionRule, *,
            source: str = "", note: str = "") -> dict[str, Any]:
        """登记一条 pending 草稿。**写入前 lint**(结构不过 = 永远确认不了)。

        返回 ``{"status": "created"|"exists"|"present", "draft": {...}}``:
        ``exists`` = 同 id 的 pending 草稿已在(调用方是幂等重放),
        ``present`` = 该 id 已经在 ``decisions.yml`` 里(草稿没有意义)。
        两者都不是错误 —— 套用一份 preset 两次不该报错,但也不该产生第二份
        同 id 草稿(重名草稿确认时会撞车)。
        """
        issues = lint_rule(rule)
        if issues:
            raise RuleError("; ".join(issues))
        try:
            doc = self.kb.load_decisions(datasource)
        except RuleError as exc:
            # 现有 decisions.yml 读不通 → 无法判定"是否重复"。不猜,拒写。
            raise RuleError(
                f"decisions.yml 读取失败,无法判定草稿是否重复: {exc}") from exc
        if any(r.id == rule.id for r in doc.rules):
            return {"status": "present", "draft": None}
        existing = self.find_rule(datasource, rule.id, status="pending")
        if existing is not None:
            return {"status": "exists", "draft": existing}

        draft = DecisionDraft(
            id=uuid.uuid4().hex[:12],
            rule=rule_to_dict(rule),
            status="pending",
            source=source,
            note=note,
        ).to_dict()
        entries = self.load(datasource)
        entries.append(draft)
        self._write(datasource, entries)
        return {"status": "created", "draft": draft}

    async def confirm(self, datasource: str, draft_id: str, *,
                      actor: str = "", message: str = "") -> dict[str, Any]:
        """审批通过:规则**经既有写门**进 ``decisions.yml`` → 草稿标记 applied。

        ``KbService.save_decisions`` 是唯一写入口(写盘前 lint + git 提交),
        这里不绕过也不复刻:一条能落盘却跑不起来的规则,只会变成一条永远
        不触发的定时任务。
        """
        draft = self.find(datasource, draft_id)
        if draft is None:
            raise KeyError(f"决策草稿不存在: {draft_id}")
        if draft.get("status") != "pending":
            raise RuleError(f"草稿 {draft_id} 已 {draft.get('status')}")
        rule = parse_rule(draft.get("rule") or {})
        doc = self.kb.load_decisions(datasource)
        if any(r.id == rule.id for r in doc.rules):
            raise RuleError(
                f"规则 {rule.id!r} 已在 decisions.yml 中(是否已手动写入?)")

        doc.rules.append(rule)
        trailers = {"Generator": "decisions.draft.confirm", "Approved-by": actor} \
            if actor else {"Generator": "decisions.draft.confirm"}
        await self.kb.save_decisions(
            datasource, doc,
            message=message or f"kb: confirm decision draft {draft_id} ({rule.id})",
            trailers=trailers,
        )
        draft["status"] = "applied"
        draft["applied_at"] = _now()
        self._write(datasource, self._replaced(datasource, draft))
        await self._commit(datasource, f"decisions: confirm draft {draft_id} "
                                       f"({rule.id})", actor=actor)
        return dict(draft)

    async def reject(self, datasource: str, draft_id: str, *,
                     actor: str = "") -> dict[str, Any]:
        """驳回:仅标记 rejected,``decisions.yml`` 一个字节都不动。"""
        draft = self.find(datasource, draft_id)
        if draft is None:
            raise KeyError(f"决策草稿不存在: {draft_id}")
        if draft.get("status") != "pending":
            raise RuleError(f"草稿 {draft_id} 已 {draft.get('status')}")
        draft["status"] = "rejected"
        self._write(datasource, self._replaced(datasource, draft))
        await self._commit(datasource, f"decisions: reject draft {draft_id}",
                           actor=actor)
        return dict(draft)

    def _replaced(self, datasource: str, updated: dict[str, Any]) -> list[dict[str, Any]]:
        return [updated if e.get("id") == updated.get("id") else e
                for e in self.load(datasource)]

    async def _commit(self, datasource: str, message: str, *, actor: str = "") -> dict:
        """草稿文件的 git 版本化(与 KB 同源,失败降级 no-op)。

        只暂存 ``decision_drafts.yml`` —— decisions.yml 的那次提交由
        ``save_decisions`` 自己出(两次提交 = 两步可分别回滚,比一次混合
        提交更容易读懂)。
        """
        trailers = {"Generator": "decisions.draft", "Approved-by": actor} \
            if actor else {"Generator": "decisions.draft"}
        return await self.kb.git_commit(
            datasource, message, files=[DRAFTS_FILE], trailers=trailers)
