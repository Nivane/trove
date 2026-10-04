"""统计器械 —— 纯 stdlib 确定性统计(零 LLM,零 I/O,零 numpy)。

为什么不用 numpy:可复算承诺不该外包 —— 判定(verdict)是不可编辑的
审计记录,其统计结论必须复算得**同一结果**。``random.Random(seed)``
的行为是 Python 语言规范的一部分(跨版本稳定),而第三方采样实现
在历史上跨版本变过。量级也够:块序列 n ≤ 366,2000 次 bootstrap
纯 Python 约 100–300ms,全部发生在定时/异步路径。

三条纪律(每条都有对应测试):
  1. **seed 一律 ``sha256`` 派生**,绝不用 ``hash()``(PYTHONHASHSEED
     随机化会让同一输入在不同进程给出不同采样 —— 与 decompose 的
     保序修正同源的 bug);
  2. **算不出返回 ``None``,绝不返回 0/±inf** —— 0.0 是自信的
     「没触发」,None 是诚实的「判不了」(判定侧 Kleene 三值语义的
     同款要求);
  3. 每个结论带 ``method`` + 种子材料 + 样本量,证据可复算。
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

#: 稳健 z 阈值(Iglewicz-Hoaglin 推荐 3.5):判「超出噪声带」的默认门。
ROBUST_Z_THRESHOLD = 3.5

#: MAD → 正态一致尺度:1.4826 = 1/Φ⁻¹(0.75)。0.6745·(x−med)/MAD_raw
#: 等价于 (x−med)/(1.4826·MAD_raw) —— 本模块的 scale 一律取后者
#: (已缩放,MAD_SCALE 乘过),z 就是 (x−center)/scale。
MAD_SCALE = 1.4826

#: 块序列硬门:块数 < MIN_BLOCKS → 噪声带直接「判不了」(degraded)。
MIN_BLOCKS = 8

#: n < LOW_N 时强制打「样本不足」标 —— 结论可给,标不可省。
LOW_N = 12

#: bootstrap 默认:迭代次数与置信水平。
BOOTSTRAP_ITERS = 2000
BOOTSTRAP_LEVEL = 0.90


def _clean(values: Iterable[Any] | None) -> list[float]:
    """None / 非有限值过滤 → 纯 float 列表(保序,不排序)。"""
    out: list[float] = []
    for v in values or []:
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def _seed(material: str) -> int:
    """种子材料 → 64bit 整数种子(sha256 派生,跨进程稳定)。"""
    digest = hashlib.sha256(str(material).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def mean(values: Iterable[Any] | None) -> float | None:
    xs = _clean(values)
    return (sum(xs) / len(xs)) if xs else None


def median(values: Iterable[Any] | None) -> float | None:
    xs = _clean(values)
    if not xs:
        return None
    return float(statistics.median(xs))


def mad(values: Iterable[Any] | None) -> float | None:
    """中位绝对偏差 × 1.4826(正态一致化)。空 → None;全同 → 0.0。

    0.0 是**算得出**的值(序列确实没有散布),它的不可用发生在
    ``robust_z``(除以 0 尺度 → None)与 ``band``(→ degraded)——
    分工明确,别在这里把 0.0 偷换成 None。
    """
    xs = _clean(values)
    med = median(xs)
    if med is None:
        return None
    return float(statistics.median([abs(x - med) for x in xs])) * MAD_SCALE


def quantile(values: Iterable[Any] | None, q: float) -> float | None:
    """分位(线性插值,Hyndman-Fan type-7,与 numpy 默认一致)。"""
    xs = _clean(values)
    if not xs:
        return None
    xs.sort()
    if len(xs) == 1:
        return xs[0]
    q = min(max(float(q), 0.0), 1.0)
    h = (len(xs) - 1) * q
    lo = int(math.floor(h))
    hi = min(lo + 1, len(xs) - 1)
    frac = h - lo
    return float(xs[lo] + frac * (xs[hi] - xs[lo]))


def robust_z(x: float, values: Iterable[Any] | None) -> float | None:
    """稳健 z = (x − median) / (1.4826·MAD)。散布为 0 / 无数据 → None。

    判定「这次的值超出了历史噪声带吗」的主判据。返回 None 表示
    **判不了**(基线无散布),绝不返回一个假的 0 或 inf。
    """
    med = median(values)
    scale = mad(values)
    if med is None or scale is None or scale == 0.0:
        return None
    try:
        return (float(x) - med) / scale
    except (TypeError, ValueError):
        return None


@dataclass
class Band:
    """噪声带:center ± k·scale(robust 口径)。

    ``lo/hi`` 为 None ⇒ 带不可用(见 ``degraded`` 原因),``outside()``
    对不可用带返回 None(判不了),不是 False(判了「在带内」)。
    """

    center: float | None
    scale: float | None
    lo: float | None
    hi: float | None
    n: int
    method: str = "robust"
    degraded: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Band":
        """``to_dict`` 的逆(证据 JSON → 可再判的带)。

        判定侧要把存进证据的带重新拿去做 ``outside`` —— 逆变换存在的
        唯一理由是**同一份构造语义**:没有它,消费方会各写各的「从
        dict 拼一个带」,拼错了(漏掉 degraded 之类)外面看不出来。
        缺字段 → 带不可用(lo/hi None),绝不猜。
        """
        data = data if isinstance(data, dict) else {}
        degraded = data.get("degraded")
        n = data.get("n")
        return cls(
            center=data.get("center"),
            scale=data.get("scale"),
            lo=data.get("lo"),
            hi=data.get("hi"),
            n=int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 0,
            method=str(data.get("method") or "robust"),
            degraded=[str(d) for d in degraded] if isinstance(degraded, list) else [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": self.center,
            "scale": self.scale,
            "lo": self.lo,
            "hi": self.hi,
            "n": self.n,
            "method": self.method,
            "degraded": list(self.degraded),
        }


def band(
    values: Iterable[Any] | None,
    *,
    k: float = ROBUST_Z_THRESHOLD,
    min_n: int = MIN_BLOCKS,
) -> Band:
    """历史块 → 噪声带(median ± k·1.4826·MAD)。

    degraded 原因(全部记账,不静默):
      - ``insufficient_n``:块数 < min_n —— 带仍给(若 spread 可算),
        但调用方必须把「样本不足」传到结论旁;
      - ``no_data`` / ``zero_scale``:带不可用(lo/hi = None)。
    """
    xs = _clean(values)
    med = median(xs)
    scale = mad(xs)
    degraded: list[str] = []
    if len(xs) < int(min_n):
        degraded.append("insufficient_n")
    if med is None:
        degraded.append("no_data")
        return Band(center=None, scale=None, lo=None, hi=None,
                    n=0, degraded=degraded)
    if scale is None or scale == 0.0:
        degraded.append("zero_scale")
        return Band(center=med, scale=scale, lo=None, hi=None,
                    n=len(xs), degraded=degraded)
    half = float(k) * scale
    return Band(center=med, scale=scale, lo=med - half, hi=med + half,
                n=len(xs), degraded=degraded)


def outside(x: float, b: Band) -> bool | None:
    """x 是否落在带外;带不可用 → None(判不了)。"""
    if b is None or b.lo is None or b.hi is None:
        return None
    try:
        return float(x) < b.lo or float(x) > b.hi
    except (TypeError, ValueError):
        return None


def bootstrap_ci(
    values: Iterable[Any] | None,
    *,
    level: float = BOOTSTRAP_LEVEL,
    iters: int = BOOTSTRAP_ITERS,
    seed_material: str = "",
) -> dict[str, Any] | None:
    """中位数的 bootstrap 百分位区间(可复算)。

    ``seed_material`` 决定采样序列:同一材料 ⇒ 逐位相同的区间。
    材料留空仍是确定的(sha256("")),但调用方**应当**给材料
    (如 f"{datasource}|{metric}|{field}|{window}")把结论钉到语境上。
    """
    xs = _clean(values)
    if len(xs) < 2:
        return None
    rng = random.Random(_seed(seed_material))
    n = len(xs)
    stats_: list[float] = []
    for _ in range(max(int(iters), 1)):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        stats_.append(float(statistics.median(sample)))
    stats_.sort()
    alpha = (1.0 - min(max(float(level), 0.0), 1.0)) / 2.0
    return {
        "lo": quantile(stats_, alpha),
        "hi": quantile(stats_, 1.0 - alpha),
        "level": float(level),
        "iters": int(iters),
        "n": n,
        "statistic": "median",
        "method": "bootstrap_percentile",
        "seed_material": str(seed_material),
    }


def effective_n(values: Iterable[Any] | None) -> float | None:
    """自相关修正的有效样本量 n_eff = n·(1−ρ1)/(1+ρ1)。

    块序列有自相关(相邻月天然相似),裸 n 是伪精度 —— 报 n_eff。
    常数序列(无散布)ρ1 无定义 → None(判不了)。
    """
    xs = _clean(values)
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    den = sum((x - m) ** 2 for x in xs)
    if den == 0.0:
        return None
    num = sum((xs[i] - m) * (xs[i + 1] - m) for i in range(n - 1))
    rho = num / den
    rho = min(max(rho, -0.999), 0.999)
    n_eff = n * (1.0 - rho) / (1.0 + rho)
    return float(min(max(n_eff, 1.0), float(n)))


def welch_delta(a: Iterable[Any] | None, b: Iterable[Any] | None) -> dict[str, Any] | None:
    """两组位置差 + Welch t 统计量(不等方差;只作证据,不作判据)。

    刻意**不输出 p 值**:n≈8–12 上的 p 是伪精度,比没有更糟。
    返回 ``{delta, t, df, n_a, n_b, method}``;方差为 0 / 样本不足 → None。
    """
    xs, ys = _clean(a), _clean(b)
    if len(xs) < 2 or len(ys) < 2:
        return None
    nx, ny = len(xs), len(ys)
    vx = statistics.variance(xs)
    vy = statistics.variance(ys)
    se2 = vx / nx + vy / ny
    if se2 <= 0.0:
        return None
    delta = (sum(xs) / nx) - (sum(ys) / ny)
    t = delta / math.sqrt(se2)
    df_den = ((vx / nx) ** 2) / (nx - 1) + ((vy / ny) ** 2) / (ny - 1)
    df = (se2 ** 2) / df_den if df_den > 0.0 else None
    return {"delta": delta, "t": t, "df": df,
            "n_a": nx, "n_b": ny, "method": "welch"}


def low_n(n_or_values: Any, *, threshold: int = LOW_N) -> bool:
    """样本不足标:n < threshold(结论可给,「样本不足」标不可省)。"""
    if isinstance(n_or_values, int) and not isinstance(n_or_values, bool):
        n = n_or_values
    else:
        n = len(_clean(n_or_values))
    return n < int(threshold)
