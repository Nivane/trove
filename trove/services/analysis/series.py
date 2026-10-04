"""块序列 —— 把历史窗口切成分位分布(纯 + 一条编译 hop)。

季节基线的正确形态是**分布**而不是点值:决策要回答「这次的变化
超出噪声带了吗」,需要的是历史同粒度块的分布(median/MAD/分位),
不是「上一个窗口是多少」—— 强行把分布压成点值会丢掉它唯一的价值。

因此本模块:生成块窗口(``block_windows``)、把窗口对齐到粒度
(``derive_grain``)、同相位块(``same_phase_blocks``),并由引擎发
**一条** ``time_grain`` 查询取回 ``(bucket, value)`` 序列 —— 编译器
侧 ``time_grain`` 已全链路就绪(四方言 date_trunc),这里只负责把
plan 的键填上(``compile_series_hop``,与 ``engine.compile_hop``
同纪律:软 MISS 骨架不算,必须完整编译)。

刻意**不含被测窗口**:噪声带必须由基线分布构成 —— 把被测点混进
分布会稀释异常(自己把自己拉回带内)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from trove.core.periods import shift_months
from trove.services.analysis.decompose import num

#: 支持的块粒度(与语义编译器 plan.time_grain 的白名单同集,取子集)。
GRAINS = ("day", "week", "month")


@dataclass(frozen=True)
class SeriesSpec:
    """块序列规格(挂在 ``AnalysisRequest.series``)。

    ``grain`` 空 → 由当前窗口推导(``derive_grain``);``lookback``
    只计**历史**块(不含当前窗口);``k`` 是噪声带宽(稳健 z 单位,
    消费在显著性侧,这里只随规格传递)。
    """

    grain: str = ""
    lookback: int = 12
    mode: str = "trailing"     # trailing(近期连续块) | same_phase(同相位)
    k: float = 3.5


def _month_end(d: date) -> date:
    return date(d.year + (d.month == 12), (d.month % 12) + 1, 1) - timedelta(days=1)


def derive_grain(window: tuple[str, str] | None) -> str | None:
    """窗口恰好等于一个整块 → 该粒度;否则 None(不对齐,不静默)。

    月首月尾 → ``month``;7 天且起于周一 → ``week``;单日 → ``day``。
    其余(半月、任意区间)不对齐 —— 调用方决定降级还是报错。
    """
    if not window:
        return None
    try:
        start = date.fromisoformat(str(window[0]))
        end = date.fromisoformat(str(window[1]))
    except (TypeError, ValueError):
        return None
    if end < start:
        return None
    if start == end:
        return "day"
    if start.weekday() == 0 and (end - start).days == 6:
        return "week"
    if start.day == 1 and end == _month_end(start):
        return "month"
    return None


def block_windows(
    window: tuple[str, str] | None, grain: str, count: int,
) -> list[tuple[str, str]]:
    """严格早于 ``window`` 的 ``count`` 个同粒度块(时间升序)。

    月块规范到整月边界(月首..月末),周块 7 天、日块 1 天 —— 与
    ``derive_grain`` 的判定互逆:生成的每个块都能被它认回同一粒度。
    """
    if not window or grain not in GRAINS or int(count) <= 0:
        return []
    try:
        start = date.fromisoformat(str(window[0]))
    except (TypeError, ValueError):
        return []
    out: list[tuple[str, str]] = []
    for i in range(int(count), 0, -1):
        if grain == "day":
            d = start - timedelta(days=i)
            out.append((d.isoformat(), d.isoformat()))
        elif grain == "week":
            s = start - timedelta(days=7 * i)
            out.append((s.isoformat(), (s + timedelta(days=6)).isoformat()))
        else:  # month
            s = shift_months(start, -i).replace(day=1)
            out.append((s.isoformat(), _month_end(s).isoformat()))
    return out


def same_phase_blocks(
    window: tuple[str, str] | None, count: int,
) -> list[tuple[str, str]]:
    """同相位块:窗口整体前移 12 个月 × j(季节对齐,升序)。

    ``count`` 是年数。窗口形状原样保留(不重规范) —— same-phase 的
    语义是「去年同期」,窗口怎么定义就怎么平移,shift_months 负责
    月长钳制(2/29 → 2/28)。
    """
    if not window or int(count) <= 0:
        return []
    try:
        start = date.fromisoformat(str(window[0]))
        end = date.fromisoformat(str(window[1]))
    except (TypeError, ValueError):
        return []
    out: list[tuple[str, str]] = []
    for j in range(int(count), 0, -1):
        s = shift_months(start, -12 * j)
        e = shift_months(end, -12 * j)
        out.append((s.isoformat(), e.isoformat()))
    return out


def compile_series_hop(
    semantic_layer: Any,
    matched: list[str],
    dialect: str,
    metric_name: str,
    conds: list[dict[str, Any]],
    *,
    time_grain: str,
    time_field: str,
) -> str | None:
    """构造并编译块序列查询 → SQL(编译 MISS → None,调用方降级)。

    = ``engine.compile_hop`` + plan ``time_grain`` 键。时间字段不在
    answer_columns,编译器把分桶表达式插在维度列之后、度量之前
    (此处无维度 → 首列即 bucket);输出列 = ``(bucket, 度量)``,
    ``series_from_rows`` 直接消费。
    """
    if time_grain not in GRAINS or not time_field:
        return None
    try:
        from trove.services.semantic_layer.compiler import (
            CompileResult,
            SemanticCompiler,
        )

        model = semantic_layer.model()
        if model is None:
            return None
        compiler = SemanticCompiler(model)
        metric = compiler._metric_by_name(metric_name)
        if metric is None:
            return None
        plan = {
            "tables": list(matched),
            "aggregation": metric_name,
            "answer_columns": [metric_name],
            "conditions": list(conds),
            "time_grain": {"field": time_field, "grain": time_grain},
        }
        result = compiler.compile_detailed(plan, list(matched), force_dialect=dialect)
        # 序列是统计的取数面:软 MISS 骨架不算(带缺组件的序列会撒谎)
        if not isinstance(result, CompileResult):
            return None
        return result.sql
    except Exception:
        return None


def series_from_rows(
    columns: list[str], rows: list[list[Any]],
) -> list[tuple[str, float]]:
    """查询结果 ``(bucket, 度量)`` → 按 bucket 标签排序的序列。

    **按标签排序,不靠返回顺序**(GROUP BY 的行序不承诺)。bucket
    是 date_trunc 输出:取前 10 字符的 ISO 日期段做排序键,兼容
    ``'2024-01-01 00:00:00'`` 一类形态;同 bucket 重复(理论上不该
    有)→ 保后者,去重防重复计数。
    """
    merged: dict[str, float] = {}
    for row in rows or []:
        if len(row) < 2:
            continue
        key = str(row[0])[:10]
        if not key:
            continue
        merged[key] = num(row[-1])
    return sorted(merged.items())
