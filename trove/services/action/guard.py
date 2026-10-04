"""外送护栏与重试退避 —— 全部是纯函数,零 I/O、零新列。

三件事,一个模块:

- **风险上限** ``risk_allowed``:提案的 ``risk`` 超过部署声明的 ``max_risk``
  时,dispatch 入口直接拒绝(写 failed + error,可审计,不静默)。
- **通道速率** ``channel_rate_ok``:同一通道一分钟内的外送尝试数上限 ——
  扫描/批量场景下这是"业务方被淹没"的第一道闸。
- **重试退避** ``backoff_delay`` / ``next_attempt_at``:退避时点是
  ``(attempts, 最近一次投递的 attempted_at)`` 的**纯派生** —— 零新 DB 列,
  换句话说是"重试时钟可以从回执 trail 里重算出来",与判定证据同一条纪律。

默认档全部是"关":``max_risk="high"``(等于不设上限)、``rate_limit=0``、
``base_s=0`` —— 未配置护栏的部署因此逐字节走老路径(见各函数 docstring)。

未知标签的处理口径(两端对称的一条):**未知不是护栏**。风险标签未知而
上限未声明 → 放行(与无护栏时一致);上限已声明而风险未知 → 拒绝(在声明
了天花板的前提下,没法归类的东西不放行);两边都未知 → 放行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

#: 风险等级(与 ``models.RISKS`` 同一序:低 < 中 < 高)。
_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}

#: 速率窗口:一分钟(护栏的语义单位;窗口写死在服务侧,函数保持窗口可注入)。
RATE_WINDOW_S = 60


@dataclass(frozen=True)
class ActionGuards:
    """护栏配置的运行时投影(``ActionConfig`` 里同名的那几个字段)。

    构造签名是姿态守卫钉死的(``test_action_service_takes_no_connectors``),
    所以护栏配置不走构造参数,而是由装配点(main.py)显式绑定到
    ``ActionService.guards`` —— 换句话说是"运行时绑定",不是"隐式默认"。
    全默认值 = 三道护栏全关,与没有这个模块时的行为逐字节一致。
    """

    max_risk: str = "high"
    rate_limit: int = 0
    retry_backoff_base_s: int = 0
    retry_backoff_factor: float = 2.0
    retry_backoff_max_s: int = 3600

    @classmethod
    def from_config(cls, action_cfg: Any) -> "ActionGuards":
        """从 ``AgentConfig.action`` 读出护栏(duck-typed,不 import 配置模块)。"""
        get = (lambda name, default: getattr(action_cfg, name, default)) \
            if action_cfg is not None else (lambda name, default: default)
        return cls(
            # 归一化到小写:risk_allowed 自己也会 lowercase,但那只是兜底 ——
            # 存进 dataclass 的值应当是规范形(日志/比较里只有一种写法)。
            max_risk=str(get("max_risk", "high") or "high").strip().lower(),
            rate_limit=max(0, int(get("rate_limit", 0) or 0)),
            retry_backoff_base_s=max(0, int(get("retry_backoff_base_s", 0) or 0)),
            retry_backoff_factor=float(get("retry_backoff_factor", 2.0) or 2.0),
            retry_backoff_max_s=int(get("retry_backoff_max_s", 3600) or 3600),
        )


def _risk_index(label: object) -> int | None:
    return _RISK_ORDER.get(str(label or "").strip().lower())


def risk_allowed(risk: str, max_risk: str) -> bool:
    """``risk`` 是否在 ``max_risk`` 天花板之内。

    ``max_risk`` 为 ``"high"``(默认)时恒真 —— 默认档因此与"没有这道护栏"
    逐字节一致。未知标签的口径见模块 docstring。
    """
    ceiling = _risk_index(max_risk)
    if ceiling is None:
        return True                      # 上限未声明 → 未知不是护栏
    level = _risk_index(risk)
    if level is None:
        return False                     # 有天花板,认不出的不放行
    return level <= ceiling


def channel_rate_ok(
    attempt_times: Iterable[str], *, now: datetime, limit: int,
    window_s: int = 60,
) -> bool:
    """滚动窗口内的外送尝试数是否仍在 ``limit`` 之内。

    Args:
        attempt_times: 该通道已发生的尝试时刻(ISO 字符串,来自 deliveries
            的 ``attempted_at`` —— 事实 trail,不是计数器)。
        now: 当前时刻(注入,纯函数)。
        limit: ``<= 0`` = 不设限(**默认档**)。
        window_s: 窗口长度(秒),``<= 0`` 同样视为不设限。

    解析不了的时间戳**不计入** —— 护栏宁可漏过一次,也不该把一条坏行读成
    "超限"(那会把外送永久卡死在一行坏数据上)。
    """
    if limit <= 0 or window_s <= 0:
        return True
    cutoff = now - timedelta(seconds=int(window_s))
    hits = 0
    for raw in attempt_times:
        try:
            stamp = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        if cutoff < stamp <= now:
            hits += 1
    return hits < int(limit)


def backoff_delay(
    attempts: int, *, base_s: int, factor: float = 2.0, max_s: int = 3600,
) -> int:
    """下一次尝试前的等待秒数:``min(base * factor^(attempts-1), max_s)``。

    ``attempts`` = **已经发生**的尝试次数(``proposal.attempts``)。``base_s
    <= 0`` → 0(自动重试未配置);``attempts < 1`` 按第一次算(``base_s``)。
    ``max_s <= 0`` 视为不设上限(只可能来自手改配置,不静默当作 0)。
    """
    base = max(0, int(base_s or 0))
    if base <= 0:
        return 0
    exp = max(0, int(attempts or 0) - 1)
    delay = float(base) * (float(factor or 1.0) ** exp)
    if delay < 0:                        # factor 为负的手改配置:回落到 base
        delay = float(base)
    cap = int(max_s or 0)
    if cap > 0:
        delay = min(delay, float(cap))
    return int(delay)


def next_attempt_at(
    attempts: int, last_attempted_at: str, *, base_s: int,
    factor: float = 2.0, max_s: int = 3600,
) -> datetime | None:
    """下一次尝试的时点 —— 纯派生,零新列。

    ``attempts`` + 最近一次投递的 ``attempted_at`` 就足以复算"什么时候才该
    再试一次";``deliveries`` 是 append-only 的事实 trail,所以这个时点永远
    可从库里重算,不需要"下次重试时间"这么一根会被漏更新的列。

    Returns:
        ``datetime``;算不出时返回 ``None``(绝不返回一个"看起来很安全"的
        假时点):退避未配置(``base_s <= 0``)、没有可依赖的上次尝试
        (``last_attempted_at`` 空或不是 ISO 时间)。
    """
    if int(base_s or 0) <= 0:
        return None
    raw = str(last_attempted_at or "").strip()
    if not raw:
        return None
    try:
        last = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return last + timedelta(
        seconds=backoff_delay(attempts, base_s=base_s, factor=factor, max_s=max_s))
