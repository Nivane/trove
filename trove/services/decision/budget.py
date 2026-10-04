"""决策预算 —— 一次判定的查询上限与优先级让路(纯,零 I/O)。

老路径的查询账在引入本模块前是**不可见**的:判定自己 2 条、显著性
1-2 条、因果 0-4 条、桥 ≤4 条,加总没有上限也没有记账(``max_queries``
只管引擎内部)。本模块把账本放到**规则判定**这一层:

    judge(2) > significance(2) > causal(4) > bridge(4),total = 12

三条口径(与 ``analysis/budget.py::QueryLedger`` 共用实现):

  - **顺序即优先级**:阶段按上面的顺序执行,先到先得 —— judge 是判定
    本身,永远排第一;预算不足时让路的永远靠后阶段,绝不反过来;
  - **让路永远记账**:预算不够 → ``QueryLedger.yield_`` 进 ``yielded``
    (阶段自己再写 ``degraded``),``evidence.budget`` 里看得见 ——
    「过严的护栏会被绕过,那连观测都没有了」;
  - **只有显式声明才花钱**:未声明新能力的规则连账本都不进证据
    (见 service 的写法),老规则的输出保持原样。

``validate`` 只做配置自检(负额度 / total 连 judge 都盖不住),不参与
运行时判定 —— 它是给测试和将来的配置面用的。
"""

from __future__ import annotations

from dataclasses import dataclass

from trove.services.analysis.budget import QueryLedger

#: 阶段执行顺序 = 优先级顺序。
STAGES = ("judge", "significance", "causal", "bridge")


@dataclass(frozen=True)
class DecisionBudget:
    """一次判定的查询预算(默认 12 = 各阶段额度之和,正常情况不下发让路)。"""

    judge: int = 2
    significance: int = 2
    causal: int = 4
    bridge: int = 4
    total: int = 12

    def reserve(self, stage: str) -> int:
        """该阶段的预留额度(未知阶段 → 0,不发明额度)。"""
        if stage not in STAGES:
            return 0
        try:
            return max(int(getattr(self, stage)), 0)
        except (TypeError, ValueError):
            return 0

    def allocate(self) -> QueryLedger:
        """→ 运行时账本(total 是硬上限;阶段额度是软目标,靠 ``can`` 门)."""
        try:
            total = max(int(self.total), 0)
        except (TypeError, ValueError):
            total = 0
        return QueryLedger(total=total)

    def allowance(self) -> dict[str, int]:
        """预算不足时按优先级切分的**每阶段额度**(展示/测试用)。

        ``total`` 盖不满所有预留时,靠后的阶段先被切到 0 —— 与运行时
        「顺序即优先级」是同一件事的静态视图。
        """
        remaining = max(self.total, 0) if isinstance(self.total, int) else 0
        out: dict[str, int] = {}
        for stage in STAGES:
            grant = min(self.reserve(stage), remaining)
            out[stage] = grant
            remaining -= grant
        return out

    def validate(self) -> list[str]:
        """配置自检(空列表 = 合法)。"""
        issues: list[str] = []
        for stage in STAGES:
            value = getattr(self, stage)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                issues.append(f"{stage} reserve must be a non-negative integer "
                              f"(got {value!r})")
        if not isinstance(self.total, int) or isinstance(self.total, bool) \
                or self.total < 0:
            issues.append(f"total must be a non-negative integer (got {self.total!r})")
        elif self.total < self.reserve("judge"):
            issues.append(
                f"total ({self.total}) cannot cover the judge reserve "
                f"({self.reserve('judge')}) — a budget that prevents judging "
                "is a broken run, not a saving")
        return issues
