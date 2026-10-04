"""因果梯取数 —— 判定侧因果阶段的 I/O(至多 2 条 SQL)。

块语义与 ``series_source`` 完全共用(``fetch_block_series`` 的
``include_current`` 把当期窗口追加为最后一个块);本模块只加因果特有
的三件事:

  1. **处理组序列** = 规则 subject 口径(过滤原样保留)—— 因果问的是
     「这组数字的变化」,口径必须与判定自己看到的完全一致;
  2. **对照帧**两种形态(与 ``causal.control`` 的两形一一对应):
     - 维度值:按 ``control.dim`` 分组 —— **手术式摘掉 subject.filters
       里钉在该维上的等值过滤**:不摘,分组结果只剩处理组自己一行,
       对照与供体池都不存在;摘掉后对照组 = ``str(control.value)``
       那一行,供体池 = 其余行(**排除被 subject 钉住的处理组标签** ——
       供体受同一干预,合成对照就不成立);
     - filters:窄口径序列(subject.filters + control.filters)——
       供体池结构上不存在,``donor_reason="donors_need_dim_control"``,
       L3 不可用(诚实停在 L2);
  3. **取数窗口放宽** = ``max(seasonal.lookback, placebo_blocks + 1,
     MIN_BLOCKS)`` —— placebo 对数与稳定基线各自需要的最小块数兜底。

异常与 ``series_source`` 同纪律:**不在这里吞** —— 因果阶段按「降级
记账、绝不上抛、绝不改 triggered」处理,原因字符串要原样可辨(SQL
报错与「对照组没数据」是两种不同的修法)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trove.services.analysis.stats import MIN_BLOCKS
from trove.services.decision.rules import Causal, CausalControl, Seasonal
from trove.services.decision.series_source import (
    BlockSeries,
    Runner,
    fetch_block_series,
)

#: 判定侧的等值算子闭集(与 compiler 的过滤语义一致,见 compiler.py)。
_EQUALITY_OPS = ("=", "==", "in")


def causal_lookback(seasonal: Seasonal, causal: Causal) -> int:
    """因果取数窗口(历史块数)= max(seasonal.lookback, placebo+1, MIN_BLOCKS)。

    ``placebo_blocks + 1``:k 对相邻块对需要 k+1 个块。``MIN_BLOCKS``:
    C1 稳定基线是硬门,取数时就按它兜底,免得块真的不够时白跑两条 SQL。
    """
    return max(int(seasonal.lookback or 0),
               int(causal.placebo_blocks or 0) + 1,
               MIN_BLOCKS)


def _field_key(f: dict[str, Any]) -> str:
    """过滤的字段名末段 —— 编译器两种写法都收(``region`` / ``loan.region``,
    见 ``_resolve_field``),而声明侧写的是语义维度名:比较必须跨约定
    成立,否则一颗钉子都识别不到(处理组识别失败、供体池被污染,而
    证据里只会看到 treated_unidentified 的表面症状)。"""
    return str(f.get("field") or "").strip().split(".")[-1]


def _is_eq(f: dict[str, Any]) -> bool:
    """等值过滤(``=`` / ``==`` / ``in``;缺省 op 即 ``=``,与引擎一致)。"""
    return str(f.get("op") or "=").strip().lower() in _EQUALITY_OPS


def _is_eq_pin(f: dict[str, Any], dim: str) -> bool:
    """该过滤是否是钉在 ``dim`` 上的等值过滤。"""
    key = _field_key(f)
    return bool(key) and key == str(dim).strip().split(".")[-1] and _is_eq(f)


def _labels_of(f: dict[str, Any]) -> list[str]:
    v = f.get("value")
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


def donor_plan(
    control: CausalControl, subject_filters: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    """过滤手术:``(供体帧过滤, 处理组标签 | None)``(纯)。

    - 供体帧过滤 = subject 过滤**摘掉钉在 ``control.dim`` 上的等值过滤**
      (非等值过滤保留 —— 它们是口径的一部分,不是处理组的身份);
    - 处理组标签 = 那些被摘掉的钉子所指向的维值集合;
      **一颗钉子都没有 → ``None``** —— 说明 subject 没有把处理组识别到
      维值粒度(处理组 = 整个总体),任何供体都是处理组的子集,合成
      对照被污染 → L3 不可用(判定侧按 ``treated_unidentified`` 记账)。
    """
    filters = [dict(f) for f in (subject_filters or [])]
    pins = [f for f in filters if _is_eq_pin(f, control.dim)]
    treated: list[str] | None = None
    if pins:
        treated = []
        for f in pins:
            for label in _labels_of(f):
                if label not in treated:
                    treated.append(label)
    donors = [f for f in filters if not _is_eq_pin(f, control.dim)]
    return donors, treated


@dataclass(frozen=True)
class ControlFrame:
    """对照帧(一条分组或窄口径 SQL 的产物)。"""

    series: BlockSeries | None = None
    mode: str = ""                       # "dim" | "filters"
    label: str = ""                      # dim 口径:对照组取值标签
    treated_labels: list[str] | None = None
    donors: dict[str, list[float | None]] = field(default_factory=dict)
    donor_reason: str = ""               # 供体池不可用的原因("" = 可用)


async def fetch_treated_series(
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
) -> BlockSeries | None:
    """处理组块序列(subject 口径,含当期块)—— ``None`` = 不适用/编译 MISS。"""
    return await fetch_block_series(
        semantic_layer=semantic_layer, runner=runner, datasource=datasource,
        dialect=dialect, metric_name=metric_name, matched=matched,
        filters=filters, window=window, time_field=time_field,
        grain=grain, mode=mode, lookback=lookback,
        dimensions=None, include_current=True,
    )


async def fetch_control_series(
    *,
    semantic_layer: Any,
    runner: Runner,
    datasource: str,
    dialect: str,
    metric_name: str,
    matched: list[str] | None,
    subject_filters: list[dict[str, Any]] | None,
    control: CausalControl,
    window: tuple[str, str] | None,
    time_field: str,
    grain: str = "",
    mode: str = "trailing",
    lookback: int = 12,
) -> ControlFrame:
    """对照帧(第二条 SQL):dim 口径按维分组 / filters 口径窄序列。

    ``series is None`` = 编译 MISS 或查询无产物,由阶段侧记账为 C2 未过。
    """
    if control.by_dim():
        donor_filters, treated_labels = donor_plan(control, subject_filters)
        series = await fetch_block_series(
            semantic_layer=semantic_layer, runner=runner, datasource=datasource,
            dialect=dialect, metric_name=metric_name, matched=matched,
            filters=donor_filters, window=window, time_field=time_field,
            grain=grain, mode=mode, lookback=lookback,
            dimensions=[control.dim], include_current=True,
        )
        label = str(control.value)
        donors: dict[str, list[float | None]] = {}
        donor_reason = ""
        if series is not None:
            if treated_labels is None:
                donor_reason = "treated_unidentified"
            else:
                for lab in series.by_dim:
                    if lab == label or lab in treated_labels:
                        continue
                    donors[lab] = series.values(lab)
                if not donors:
                    donor_reason = "no_donors"
        return ControlFrame(series=series, mode="dim", label=label,
                            treated_labels=treated_labels, donors=donors,
                            donor_reason=donor_reason)

    series = await fetch_block_series(
        semantic_layer=semantic_layer, runner=runner, datasource=datasource,
        dialect=dialect, metric_name=metric_name, matched=matched,
        filters=override_filters(subject_filters, control.filters),
        window=window, time_field=time_field,
        grain=grain, mode=mode, lookback=lookback,
        dimensions=None, include_current=True,
    )
    return ControlFrame(series=series, mode="filters",
                        donor_reason="donors_need_dim_control")


def override_filters(
    subject_filters: list[dict[str, Any]] | None,
    control_filters: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """filters 口径对照帧的完整过滤:``subject 口径 + control 覆盖``(纯)。

    control 里的等值过滤**覆盖** subject 在同字段(末段名)上的等值钉:
    处理组身份就在 subject 的钉里(``region=华东``),直接拼接会得到
    ``region=华东 AND region=华北`` —— 空集。覆盖而非拼接,是「对照 =
    同口径换一组」的直接表达;control 没碰的字段(如 ``product=车贷``
    这类口径)原样保留。非等值过滤是附加约束,两边都留。
    """
    pinned = {_field_key(f) for f in (control_filters or []) if _is_eq(f)}
    kept = [dict(f) for f in (subject_filters or [])
            if not (_is_eq(f) and _field_key(f) in pinned)]
    return kept + [dict(f) for f in (control_filters or [])]


def split_post(values: list[float | None] | None) -> tuple[
        list[float | None], float | None]:
    """块序列 → ``(历史, 当期)`` —— 与 ``BlockSeries`` 约定一致(末位 = 当期)。"""
    vals = list(values or [])
    if not vals:
        return [], None
    return vals[:-1], vals[-1]
