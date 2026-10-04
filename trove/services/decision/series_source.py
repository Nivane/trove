"""块序列取数 —— 判定侧唯一的新 I/O(至多 1 条 SQL)。

B1 的 ``analysis/series.py`` 是纯的(块窗口 / 编译 / 解析),取数动作
在引擎里;判定侧同款取数落在本模块 —— 与引擎的 ``_series_stage``
共用同一套块语义(``block_windows`` / ``same_phase_blocks`` /
``compile_series_hop``),但有三处判定侧特有的收紧:

  1. **按维分组仍是一条 SQL**(``answer_columns`` = 维度… + 度量):
     逐维一条会随维值数量爆炸,而噪声带是「门」,预算纪律优先;
  2. **粒度对齐是硬性的**(``derive_grain(window)`` 必须等于生效粒度):
     引擎侧序列是增强,宽松最多是附录错;判定侧序列是门的判据,
     窗口与块形状不一致时**拒绝确认**(宁可不判,不判错);
  3. **落不进块的桶不静默丢**:计入 ``unmatched`` 记账 —— 桶标签与
     块窗口对不上,说明粒度或时区假设错了,外面要看得见。

执行异常**不在这里吞**:调用方(判定侧显著性阶段)要按
``require`` 语义决定「降级记账」还是「error run」,原因字符串必须
原样上达(吞掉就分不清 SQL 报错与块数不足)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from trove.services.analysis.decompose import num
from trove.services.analysis.engine import time_conds
from trove.services.analysis.series import (
    GRAINS,
    block_windows,
    compile_series_hop,
    derive_grain,
    same_phase_blocks,
)

#: ``runner(sql, datasource) -> (columns, rows)`` —— 与桥同一契约。
Runner = Callable[[str, str], Awaitable[tuple[list[Any], list[Any]]]]


@dataclass(frozen=True)
class BlockSeries:
    """块序列取数产物:一条 SQL + 按维对齐到块的取值。

    ``by_dim[标签]`` 与 ``blocks`` 等长;``None`` = 该块无数据(块本身
    仍占位 —— 丢块会把 n 悄悄缩小,而 n 正是噪声带的全部前提)。
    """

    sql: str
    grain: str
    mode: str
    blocks: list[tuple[str, str]] = field(default_factory=list)
    by_dim: dict[str, list[float | None]] = field(default_factory=dict)
    unmatched: int = 0

    def values(self, dim: str = "") -> list[float | None]:
        """该维的块取值(缺维 → 空列表,调用方按「无历史」处理)。"""
        return list(self.by_dim.get(str(dim or ""), []))

    def span(self) -> tuple[str, str] | None:
        if not self.blocks:
            return None
        return self.blocks[0][0], self.blocks[-1][1]


def plan_blocks(
    window: tuple[str, str] | None, *, grain: str = "",
    mode: str = "trailing", lookback: int = 12,
) -> tuple[str, str, list[tuple[str, str]]] | None:
    """窗口 × 季节性声明 → ``(生效粒度, 模式, 块)``;任何一步算不出 → None。

    错误形态不区分(调用方看到的都是「判不了」),但对外记账时应说明
    是哪一类 —— 调用方用 ``derive_grain`` 自己复核即可。
    """
    if not window:
        return None
    derived = derive_grain(window)
    if derived is None:
        return None
    eff = str(grain or "").strip().lower() or derived
    if eff not in GRAINS or eff != derived:
        return None
    m = str(mode or "trailing").strip().lower() or "trailing"
    try:
        count = max(int(lookback), 0)
    except (TypeError, ValueError):
        return None
    if m == "same_phase":
        blocks = same_phase_blocks(window, count)
    else:
        m = "trailing"
        blocks = block_windows(window, eff, count)
    if not blocks:
        return None
    return eff, m, blocks


def plan_reason(
    window: tuple[str, str] | None, *, grain: str = "",
    mode: str = "trailing", lookback: int = 12,
) -> str:
    """``plan_blocks`` 为什么给不出块 —— ``""`` = 给得出。

    对外记账用:全塌成一句「判不了」的话,运维从证据里分不出是窗口
    形状不对(改规则的 window)还是粒度声明与窗口不一致(改
    seasonal.grain)—— 而这两件事的修法完全不同。

    实现**先问 plan_blocks 本身**:原因判定与主路径共用同一个谓词,
    单独复写一遍判断条件迟早与它漂移。
    """
    if plan_blocks(window, grain=grain, mode=mode, lookback=lookback) is not None:
        return ""
    if not window:
        return "no_window"
    derived = derive_grain(window)
    if derived is None:
        return "grain_unaligned"
    eff = str(grain or "").strip().lower() or derived
    if eff not in GRAINS or eff != derived:
        return "grain_mismatch"
    return "no_blocks"


def _block_index(label: str, blocks: list[tuple[str, str]]) -> int | None:
    """桶标签 → 块下标(前缀安全的包含判定,与引擎 same_phase 过滤同款)。

    sqlite 的月桶标签是 ``'YYYY-MM'``(7 位),与 ``'YYYY-MM-DD'`` 窗口
    边界比较要按标签长度截齐,否则前缀短而全部漏掉。
    """
    for i, (w0, w1) in enumerate(blocks):
        if w0[:len(label)] <= label <= w1[:len(label)]:
            return i
    return None


async def fetch_block_series(
    *,
    semantic_layer: Any,
    runner: Runner,
    datasource: str,
    dialect: str,
    metric_name: str,
    matched: list[str] | None,
    filters: list[dict[str, Any]] | None,
    window: tuple[str, str] | None,
    time_field: str,
    grain: str = "",
    mode: str = "trailing",
    lookback: int = 12,
    dimensions: list[str] | None = None,
) -> BlockSeries | None:
    """历史块序列(至多 1 条 SQL)。

    ``None`` = 不适用或编译 MISS(无窗口 / 粒度不对齐 / 无时间字段 /
    空块集 / 软 MISS)—— 调用方按 require 语义决定降级还是 error。
    ``filters`` = 规则 subject 的过滤条件(历史块必须与当期**同一
    口径**:过滤掉了华北,基线里也不该有华北)。
    """
    if not time_field:
        return None
    planned = plan_blocks(window, grain=grain, mode=mode, lookback=lookback)
    if planned is None:
        return None
    eff, m, blocks = planned
    span = (blocks[0][0], blocks[-1][1])
    conds = [dict(f) for f in (filters or [])]
    conds += time_conds(time_field, span, dialect=dialect)
    dims = [str(d) for d in (dimensions or [])]
    sql = compile_series_hop(
        semantic_layer, list(matched or []), dialect, metric_name, conds,
        time_grain=eff, time_field=time_field, dimensions=dims or None,
    )
    if sql is None:
        return None
    _, rows = await runner(sql, datasource)

    n_dims = len(dims)
    by_dim: dict[str, list[float | None]] = {}
    unmatched = 0
    for row in rows or []:
        if len(row) < n_dims + 2:
            continue
        label = " / ".join(str(row[i]) for i in range(n_dims)) if n_dims else ""
        bucket = str(row[n_dims])[:10]
        if not bucket:
            continue
        idx = _block_index(bucket, blocks)
        if idx is None:
            unmatched += 1
            continue
        vals = by_dim.setdefault(label, [None] * len(blocks))
        vals[idx] = num(row[n_dims + 1])
    return BlockSeries(sql=sql, grain=eff, mode=m,
                       blocks=[(b[0], b[1]) for b in blocks],
                       by_dim=by_dim, unmatched=unmatched)
