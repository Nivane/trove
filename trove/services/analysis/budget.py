"""查询预算账本 —— 显式记账 + 优先级让路(纯,零 I/O)。

老路径的查询不加约束(``max_queries`` 只管新阶段),这个洞曾经是
**不可见**的;账本把它变成可观测:每次执行记 stage,每次因预算
让路记 yielded。设计口径(与方案 B2 的 DecisionBudget 共用本实现):

  - ``total=None`` → 无上限,行为与记账之前**逐字节相同**(兼容);
  - 上限只在显式设置时生效(决策桥 / 新阶段),且让路**永远记账、
    永不静默** —— 「过严的护栏会被绕过,那连观测都没有了」;
  - ``frame`` 只回答三件事:能不能再花 N 次(``can``)、花了归谁
    (``record``)、谁被让掉(``yield_``);优先级怎么排是上层的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryLedger:
    """一次运行的查询账本(实例即一次运行,顺序即账目顺序)。"""

    total: int | None = None
    used: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)
    yielded: list[dict[str, Any]] = field(default_factory=list)

    def can(self, n: int = 1) -> bool:
        """还能再花 n 次吗(无上限 → 永远 True)。"""
        if self.total is None:
            return True
        return self.used + max(int(n), 0) <= int(self.total)

    def record(self, stage: str, n: int = 1) -> None:
        """执行后记账(每次真实执行都应过这里)。"""
        step = max(int(n), 0)
        key = str(stage or "unknown")
        self.used += step
        self.by_stage[key] = self.by_stage.get(key, 0) + step

    def yield_(self, stage: str, *, needed: int, reason: str = "query_budget_exceeded") -> dict[str, Any]:
        """让路记账 → 返回入账条目(调用方把它同时写进 degraded)。"""
        entry: dict[str, Any] = {
            "stage": str(stage or "unknown"),
            "needed": int(needed),
            "reason": str(reason),
            "remaining": self.remaining(),
        }
        self.yielded.append(entry)
        return entry

    def remaining(self) -> int | None:
        if self.total is None:
            return None
        return max(int(self.total) - self.used, 0)

    def snapshot(self) -> dict[str, Any]:
        """确定性快照(键序固定;供 evidence 记账)。"""
        return {
            "limit": self.total,
            "used": self.used,
            "by_stage": dict(self.by_stage),
            "yielded": [dict(y) for y in self.yielded],
        }
