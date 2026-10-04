"""扫描/假设的模型 —— **全闭集**。

四个 dataclass 分别对应扫描链上的四段:

  - ``ScanSpec`` —— 扫描**规格**(定时任务 ``Job.scan_spec`` 的 JSON 形状,
    也可由 API/交互侧构造):扫什么(metric × dim)、窗口、块模式与噪声带宽;
  - ``Finding`` —— 一个扫描单元的**发现**:``anomaly``(超带)或
    ``unverifiable``(算不出/预算让路 —— 记账,绝不静默丢弃);
  - ``Hypothesis`` —— LLM 起草的**结构化**假设(引用在解析期就验证:
    引用不过 = 零查询,绝不把一条解析不了的假设送进执行面);
  - ``VerifiedHypothesis`` —— 裁决结果,四态闭集,每条恰一行。

闭集是纪律不是风格:``mode``/``kind``/``status``/``direction`` 任一取值
落到集合外都必须是**解析错误**(响亮),而不是被下游当默认值吞掉 ——
扫描的产出会变成草稿、通知与治理待办,一个静默走偏的枚举值最后会变成
一条「看起来在跑、其实永远不触发」的规则。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

#: 块模式(与 ``analysis.series`` 同集):trailing = 近期连续块;same_phase = 同相位(去年同窗)。
SCAN_MODES = ("trailing", "same_phase")

#: 发现类型:超带 = anomaly;算不出/预算耗尽 = unverifiable(不丢弃)。
FINDING_KINDS = ("anomaly", "unverifiable")

#: 假设裁决四态(每条恰一行)。
HYPOTHESIS_STATUSES = ("supported", "refuted", "unverifiable", "no_data")

#: 方向闭集。
DIRECTIONS = ("up", "down")

#: 块粒度白名单(与 analysis.series.GRAINS 同集,写成常量避免 import 环)。
GRAINS = ("day", "week", "month")

#: 非「词字符」折成 '-'。``\w`` 在 str 模式默认 Unicode —— 汉字/假名是
#: 词字符,原样保留:把「华东」折成空串会让 region 的两个取值撞成同一个
#: 草稿 id(第二条发现被 ``exists`` 静默顶掉)。
_SLUG_RE = re.compile(r"[^\w]+")


class ScanError(ValueError):
    """A scan spec / hypothesis that must not be executed."""


def slug(text: str, *, limit: int = 48) -> str:
    """稳定 slug(草稿/规则 id 用):小写、非词字符折成 '-'、有界。

    确定性优先于可读:同一个 (metric, dim, value) 在任何进程/任何次扫描
    里都必须得到同一个 id,否则 ``drafts.add`` 的幂等判定(同 id 已存在
    → exists)会失效,同一个发现每跑一次就多一份同义草稿。
    """
    out = _SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")
    return (out[:limit].strip("-")) or "x"


@dataclass(frozen=True)
class ScanSpec:
    """一条扫描规格(metric × dimension 的笛卡尔积 = 扫描单元)。

    ``dimensions`` 允许为空 —— 那是对「整度量」的扫描(聚合视角)。
    ``window`` 走与决策规则**同一个**自然语言时间入口解析(同一句
    「本月」在规则、扫描、问答三处必须是同一个跨度);``grain`` 空 = 由
    窗口推导(不对齐即 unverifiable,不猜)。
    """

    metrics: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    window: str = ""
    grain: str = ""
    mode: str = "trailing"
    lookback: int = 12
    k: float = 3.5
    top_k: int = 5
    #: 定时扫描是否跑一轮假设(默认关:后台/成本;交互侧默认开,见 ScanConfig)。
    hypotheses: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "metrics": list(self.metrics),
            "dimensions": list(self.dimensions),
            "window": self.window,
            "grain": self.grain,
            "mode": self.mode,
            "lookback": int(self.lookback),
            "k": float(self.k),
            "top_k": int(self.top_k),
            "hypotheses": bool(self.hypotheses),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ScanSpec:
        """松 dict → 规格(结构错**抛** ``ScanError``,绝不静默取默认)。

        写侧(API/CLI)与运行侧(定时任务)共用这一个入口:一条挂空
        metric 名或坏 mode 的规格必须在**写入时**被拒,而不是每个 tick
        都以同样的方式静默失败。
        """
        if not isinstance(raw, dict):
            raise ScanError(f"scan_spec must be a mapping, got {type(raw).__name__}")

        def _names(key: str) -> tuple[str, ...]:
            vals = raw.get(key) or []
            if not isinstance(vals, (list, tuple)):
                raise ScanError(f"scan_spec.{key} must be a list")
            out = tuple(str(v).strip() for v in vals if str(v or "").strip())
            return out

        metrics = _names("metrics")
        if not metrics:
            raise ScanError("scan_spec.metrics must name at least one metric")
        mode = str(raw.get("mode") or "trailing").strip().lower()
        if mode not in SCAN_MODES:
            raise ScanError(
                f"scan_spec.mode must be one of {', '.join(SCAN_MODES)} (got {mode!r})")
        grain = str(raw.get("grain") or "").strip().lower()
        if grain and grain not in GRAINS:
            raise ScanError(
                f"scan_spec.grain must be empty or one of {', '.join(GRAINS)} "
                f"(got {grain!r})")
        try:
            lookback = int(raw.get("lookback", 12))
            top_k = int(raw.get("top_k", 5))
            k = float(raw.get("k", 3.5))
        except (TypeError, ValueError) as e:
            raise ScanError(f"scan_spec has a non-numeric field: {e}") from e
        if lookback < 1:
            raise ScanError(f"scan_spec.lookback must be >= 1 (got {lookback})")
        if top_k < 1:
            raise ScanError(f"scan_spec.top_k must be >= 1 (got {top_k})")
        if not math.isfinite(k) or k <= 0:
            raise ScanError(f"scan_spec.k must be a positive number (got {k})")
        return cls(
            metrics=metrics,
            dimensions=_names("dimensions"),
            window=str(raw.get("window") or "").strip(),
            grain=grain,
            mode=mode,
            lookback=lookback,
            k=k,
            top_k=top_k,
            hypotheses=bool(raw.get("hypotheses", False)),
        )

    @property
    def units(self) -> list[tuple[str, str]]:
        """扫描单元 = metric × (每个维度 + 聚合视角)。顺序 = 声明的顺序。"""
        pairs: list[tuple[str, str]] = []
        for metric in self.metrics:
            for dim in self.dimensions:
                pairs.append((metric, dim))
            if not self.dimensions:
                pairs.append((metric, ""))
        return pairs


@dataclass
class Finding:
    """一个扫描单元的发现 —— 恰好是「一条结论 + 它的证据」。

    ``kind=anomaly``:该组的当期块**落在历史噪声带之外**(``outside`` 为
    True)。``kind=unverifiable``:算不出(未声明度量/维度、无时间字段、
    窗口与粒度不对齐、空序列、编译 MISS)或预算让路 —— 仍是一行,带
    ``reason``;「判不了」与「判了没问题」在产物里必须能分开。
    """

    metric: str
    dimension: str = ""
    value: str = ""
    kind: str = "anomaly"
    reason: str = ""
    window: str = ""
    period: list[str] = field(default_factory=list)
    current: float | None = None
    band: dict[str, Any] = field(default_factory=dict)
    z: float | None = None
    outside: bool | None = None
    n: int = 0
    low_n: bool = False
    grain: str = ""
    mode: str = "trailing"
    method: str = "robust"
    sql: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "dimension": self.dimension,
            "value": self.value,
            "kind": self.kind,
            "reason": self.reason,
            "window": self.window,
            "period": list(self.period),
            "current": self.current,
            "band": dict(self.band),
            "z": self.z,
            "outside": self.outside,
            "n": int(self.n),
            "low_n": bool(self.low_n),
            "grain": self.grain,
            "mode": self.mode,
            "method": self.method,
            "sql": self.sql,
        }

    @property
    def label(self) -> str:
        return f"{self.metric} / {self.dimension}={self.value}" \
            if self.dimension else self.metric


@dataclass
class Hypothesis:
    """LLM 起草的**结构化**假设(引用在解析期验证,不过 = 不入列)。

    判断口径是全闭集的:方向(up/down)+ 至少动多少(``min_pct``,比例)。
    这样裁决可以纯确定性完成 —— 模型提出可证伪的陈述,数据来判;模型
    不参与对自己的裁决。
    """

    claim: str
    metric: str
    dimension: str = ""
    value: str = ""
    direction: str = "up"
    min_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "metric": self.metric,
            "dimension": self.dimension,
            "value": self.value,
            "direction": self.direction,
            "min_pct": self.min_pct,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Hypothesis:
        """松 dict → 假设(结构错抛 ``ScanError``)。

        **只做形状判定**:metric/dim 是否在语义模型里声明由
        ``hypotheses.validate`` 验证(那里才有模型)—— 分两层是因为形状
        错误是模型输出问题(该重试/丢弃),引用错误是数据问题(该记
        rejected,零查询)。
        """
        if not isinstance(raw, dict):
            raise ScanError(f"hypothesis must be a mapping, got {type(raw).__name__}")
        claim = str(raw.get("claim") or "").strip()
        metric = str(raw.get("metric") or "").strip()
        if not claim:
            raise ScanError("hypothesis.claim must not be empty")
        if not metric:
            raise ScanError("hypothesis.metric must not be empty")
        direction = str(raw.get("direction") or "").strip().lower()
        if direction not in DIRECTIONS:
            raise ScanError(
                f"hypothesis.direction must be one of {', '.join(DIRECTIONS)} "
                f"(got {direction!r})")
        try:
            min_pct = float(raw.get("min_pct") or 0.0)
        except (TypeError, ValueError) as e:
            raise ScanError(f"hypothesis.min_pct must be numeric: {e}") from e
        if not math.isfinite(min_pct) or min_pct < 0 or min_pct > 1:
            raise ScanError(
                f"hypothesis.min_pct must be a fraction in [0, 1] (got {min_pct})")
        dimension = str(raw.get("dimension") or "").strip()
        return cls(
            claim=claim[:400], metric=metric, dimension=dimension,
            value=str(raw.get("value") or "").strip()[:200],
            direction=direction, min_pct=min_pct,
        )


@dataclass
class VerifiedHypothesis:
    """一条假设的裁决 —— 四态闭集,每条恰一行,绝不静默丢弃。"""

    hypothesis: Hypothesis
    status: str = "unverifiable"
    observed: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    queries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis.to_dict(),
            "status": self.status,
            "observed": dict(self.observed),
            "reason": self.reason,
            "queries": int(self.queries),
        }
