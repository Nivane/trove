"""Session audit sink — the write side of the run-record audit trail.

Extracted from ``SessionManager`` (change axis: compliance/audit fields). The
session lifecycle owns conversation state; this module owns *what an audit row
says*. Five sinks, all best-effort by construction — an audit failure must
never block an answer:

- ``query``     → ``query.execute``
- ``refusal``   → ``query.refused`` (why a turn was refused)
- ``authz``     → ``authz.deny`` / ``authz.table_warn`` / ``masking.*``
- ``egress``    → ``llm.egress`` (field names + counts only, never values)
- ``kb_rev``    → the KB fingerprint stamped onto the two query sinks

The user on an audit row is resolved by an **injected** ``user_lookup``
callback (``SessionManager._audit_user``), not by reaching into the auth
service here: tests drive these sinks with a duck-typed owner whose auth fake
has no ``.store``, and that shape is the contract.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from trove.core.logging import get_logger
from trove.core.types import Session
from trove.workflow.state import WorkflowState

logger = get_logger(__name__)

UserLookup = Callable[[Session, WorkflowState], Awaitable[dict[str, Any] | None]]


class SessionAudit:
    """Writes one session's audit rows through an injected auth service.

    Each sink only touches the collaborators it was constructed with, so a
    partially-wired instance (e.g. only ``kb`` + ``connectors`` for the
    fingerprint) behaves exactly like the old per-method attribute reads.
    """

    def __init__(
        self,
        *,
        auth: Any | None = None,
        kb: Any | None = None,
        connectors: Any | None = None,
        config: Any | None = None,
        user_lookup: UserLookup | None = None,
    ) -> None:
        self._auth = auth
        self._kb = kb
        self._connectors = connectors
        self.config = config
        self._user_lookup = user_lookup

    async def kb_rev(self, final: WorkflowState) -> dict[str, Any]:
        """本 run 的 KB 指纹快照(审计 details 的归因键;best-effort)。

        数据源解析与记忆/候选消费同款(``final.datasource or
        connectors.default_name``);KB 指纹只读镜像(``KbService.kb_rev``),
        取不到 / 无 ds → {} → 审计两键省略,绝不阻断回答。
        """
        if self._kb is None:
            return {}
        datasource = final.datasource or (
            self._connectors.default_name if self._connectors is not None else None
        ) or ""
        if not datasource:
            return {}
        try:
            snap = await self._kb.kb_rev(datasource)
        except Exception as e:  # 指纹失败绝不阻断回答/审计
            logger.debug("kb_rev snapshot skipped (%s): %s", type(e).__name__, e)
            return {}
        if not snap:
            return {}
        return {"kb_rev": snap["rev"], "kb_files": snap["files"]}

    async def query(
        self, session: Session, final: WorkflowState, *,
        kb: dict[str, Any] | None = None,
    ) -> None:
        """查询执行审计 —— ``query.execute`` 写入 audit_log。

        管理端 /admin/audit 可据此回答「谁在什么时候查了什么」。
        best-effort:审计写失败绝不阻断回答;无 auth 服务(CLI/嵌入)跳过。
        """
        if self._auth is None:
            return
        try:
            details: dict[str, Any] = {
                "session_id": final.session_id,
                "run_id": final.run_id,
                "question": (final.question or "")[:2000],
                "sql": (final.sql or "")[:8000],
                "datasource": final.datasource or "",
                "verdict": final.verdict,
                "row_count": final.row_count,
                "execution_time_ms": final.execution_time_ms,
                "error": (final.error or "")[:2000],
            }
            # 重放的**双记**(设计 §5.6):authz.on_behalf_of 记的是「谁发起了
            # 一次重放」,这一条记的是「这条 SQL 是以谁的身份跑的」—— 排障时
            # 先被翻出来的是查询记录,只在另一张表里记一次意味着要拼两张表
            # 才知道当时的身份。
            if (final.principal or {}).get("on_behalf_of"):
                details["on_behalf_of"] = final.principal["on_behalf_of"]
            if kb:  # KB 指纹(设计 2026-10-09 §3.3):这条 SQL 是哪一版 KB 给的
                details.update(kb)
            await self._auth.record_audit(
                "query.execute",
                user=await self._user(session, final),
                details=details,
            )
        except Exception as e:  # 审计失败绝不影响查询链路
            logger.debug("query audit skipped (%s): %s", type(e).__name__, e)

    async def refusal(
        self, session: Session, final: WorkflowState, *,
        kb: dict[str, Any] | None = None,
    ) -> None:
        """拒绝审计 —— ``query.refused`` 写入 audit_log。

        ``query.execute`` 对拒绝轮也会写一行,但它答不了「**为什么**被拒」:
        拒绝轮的卫生清理把 sql/verdict 等清零,那一行看起来与一次空运行无
        异。独立 action 带 reason(与 compile_miss 的 reason/component),
        拒绝频率报表据此聚合「最常问但答不了的问题」;relationship 类
        miss(基数未声明/扇出/多路径/不连通)在报表里单列 —— 它们是
        「关系未声明」的信号,不是普通缺词表。

        best-effort:写失败绝不阻断回答;无 auth 服务(CLI/嵌入)跳过;
        非拒绝轮不写(``final.refusal`` 为空即返回)。
        """
        if self._auth is None:
            return
        refusal = final.refusal
        if not refusal:
            return
        try:
            details: dict[str, Any] = {
                "session_id": final.session_id,
                "run_id": final.run_id,
                "question": (final.question or "")[:2000],
                "datasource": final.datasource or "",
                "reason": str(refusal.get("reason") or ""),
            }
            topic = refusal.get("topic")
            if topic:
                details["topic"] = str(topic)[:200]
            miss = refusal.get("compile_miss")
            if isinstance(miss, dict):
                if miss.get("reason"):
                    details["miss_reason"] = str(miss["reason"])[:120]
                if miss.get("component"):
                    details["miss_component"] = str(miss["component"])[:300]
            if kb:  # KB 指纹(设计 2026-10-09 §3.3):这条 SQL 是哪一版 KB 给的
                details.update(kb)
            await self._auth.record_audit(
                "query.refused",
                user=await self._user(session, final),
                details=details,
            )
        except Exception as e:  # 审计失败绝不影响查询链路
            logger.debug("refusal audit skipped (%s): %s", type(e).__name__, e)

    async def authz(self, session: Session, final: WorkflowState) -> None:
        """授权 / 脱敏审计 —— ``authz.deny`` · ``authz.table_warn`` ·
        ``masking.applied`` · ``masking.bypass``。

        四条动作回答四个不同的问题:「谁被拦了」「哪些表被**放行**了」
        「哪些列被改写了」「谁看了原文」。两两都**不能合并**:

        - ``deny`` 与 ``table_warn`` —— 一次 warn 命中是 ``allowed=True``,
          合并进拒绝会让「谁被拦了」这个答案里混进一半没被拦的人。
        - ``applied`` 与 ``bypass`` —— 一次持 ``pii`` 的 bypass 运行 ``fields``
          是空的(什么都没改),合并之后最该留痕的那次会表现成「这次没脱敏」。

        ``table_warn`` 是 §8.2 观察期的落点:warn 判定只进日志的话,「一周里
        哪些表会被拒」收不齐(一行一条的日志答不了聚合问题)。门看见了越界
        **并放行**,比一次干脆的拒绝更值得留痕。

        **只记字段名与模式,不记值**(设计 §6.3)。审计表长期留存、管理端可列、
        会进备份;把值写进去等于把脱敏要防的那份数据再抄一份明文,而且没有
        TTL。best-effort:写失败绝不阻断回答;无 auth 服务跳过。
        """
        if self._auth is None:
            return
        try:
            user = await self._user(session, final)
            decision = final.authz_decision or {}
            if decision and not decision.get("allowed"):
                await self._auth.record_audit(
                    "authz.deny",
                    user=user,
                    details={
                        "session_id": final.session_id,
                        "run_id": final.run_id,
                        "datasource": decision.get("datasource")
                        or final.datasource or "",
                        "reason": decision.get("reason") or "",
                        "tables": list(decision.get("narrowed_tables") or []),
                    },
                )
            elif decision.get("narrowed_tables"):
                # warn 期(§8.2)的 A3 放行:allowed=True,但命中了未声明的表。
                # 表**名**只在这里落库 —— 计数器上放不下(基数无限),而这里正是
                # 「哪些表会被拒」唯一答得全的地方。
                await self._auth.record_audit(
                    "authz.table_warn",
                    user=user,
                    details={
                        "session_id": final.session_id,
                        "run_id": final.run_id,
                        "datasource": decision.get("datasource")
                        or final.datasource or "",
                        "tables": list(decision.get("narrowed_tables") or []),
                    },
                )
            report = final.masking_applied
            if not report:
                # None = 这一步**没跑**(与「跑了但没改动」两回事);两条都不写。
                return
            if report.get("bypass"):
                await self._auth.record_audit(
                    "masking.bypass",
                    user=user,
                    details={
                        "session_id": final.session_id,
                        "run_id": final.run_id,
                        "datasource": final.datasource or "",
                    },
                )
                return
            fields = dict(report.get("fields") or {})
            if fields:
                await self._auth.record_audit(
                    "masking.applied",
                    user=user,
                    details={
                        "session_id": final.session_id,
                        "run_id": final.run_id,
                        "datasource": final.datasource or "",
                        "fields": fields,
                    },
                )
        except Exception as e:  # 审计失败绝不影响查询链路
            logger.debug("authz audit skipped (%s): %s", type(e).__name__, e)

    async def egress(self, session: Session, final: WorkflowState) -> None:
        """数据出境治理审计 —— ``llm.egress`` 写入 audit_log。

        回答「这一问里有没有 ``mask`` 声明字段的值被发给过模型」。details 只含
        字段名与计数(行数/值数/扣留次数),**不含任何值** —— 与
        ``masking.applied`` 同一条纪律:审计表长期留存,写值等于把要防的数据
        再抄一份明文。

        ``egress.audit`` 关闭 → 不写;``egress_report`` 为空/None → 这一问没有
        发生出境脱敏(未声明 ``mask`` 或没有工具回喂),不写空行。
        best-effort:写失败绝不阻断回答;无 auth 服务(CLI/嵌入)跳过。
        """
        if self._auth is None:
            return
        report = final.egress_report
        if not report:
            return
        egress_cfg = getattr(self.config, "egress", None)
        if egress_cfg is not None and not getattr(egress_cfg, "audit", True):
            return
        try:
            details: dict[str, Any] = {
                "session_id": final.session_id,
                "run_id": final.run_id,
                "datasource": final.datasource or "",
                # 排序输出:同一份报告两次审计逐字节一致(聚合与 diff 都省事)。
                "egress": {
                    tool: {
                        **dict(entry or {}),
                        "fields": dict(
                            sorted((entry or {}).get("fields", {}).items())
                        ),
                    }
                    for tool, entry in sorted(report.items())
                },
            }
            await self._auth.record_audit(
                "llm.egress",
                user=await self._user(session, final),
                details=details,
            )
        except Exception as e:  # 审计失败绝不影响查询链路
            logger.debug("egress audit skipped (%s): %s", type(e).__name__, e)

    async def _user(
        self, session: Session, final: WorkflowState,
    ) -> dict[str, Any] | None:
        """Resolve the audit row's user through the injected callback.

        ``None`` lookup / ``None`` result means "no such user" (CLI/embedded
        runs carry free-text user ids) — never fabricate ``{"id": uid}``.
        """
        if self._user_lookup is None:
            return None
        return await self._user_lookup(session, final)
