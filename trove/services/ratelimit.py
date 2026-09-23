"""API 速率限制与日配额(进程内令牌桶)——防 LLM 成本被单用户耗尽。

当前只有登录端点有限流(`services/auth/service.py` 的登录失败锁定);本模块
补上**业务端点**(/v1/chat、/v1/semantic/query 等)的:

- 每分钟请求数(按 user 的令牌桶,突发窗口 = 容量 = rpm);
- 每日请求总配额(按 user 的日历日计数)。

配置来自 ``AgentConfig.api_rate_per_minute`` / ``api_daily_quota``(0 = 关闭),
admin 设置热更新经 ``apply_overrides`` 落在同一 config,依赖在请求时读取 →
无需重启生效。

实现是**进程内**的:多副本部署下各实例独立计数(仍是合理护栏,不是严格
全局限额)。要严格全局,把 :class:`RateLimiter` 换成 Redis 计数器即可,接口不变。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


class TokenBucket:
    """滑动窗口令牌桶:容量 = 每秒填充率的整数倍,rpm 的突发上限。"""

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self.capacity = max(1.0, float(capacity))
        self.refill = max(0.001, float(refill_per_sec))
        self.tokens = self.capacity
        self.last = time.monotonic()

    def allow(self, tokens: float = 1.0) -> tuple[bool, float]:
        """尝试取 tokens;不足 → (False, Retry-After 秒)。"""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.refill)
        self.last = now
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True, 0.0
        deficit = tokens - self.tokens
        retry_after = max(1.0, int(deficit / self.refill))
        return False, retry_after


class RateLimiter:
    """按 key(user)的速率与日配额。进程内、有界(超限淘汰最旧桶)。"""

    def __init__(self, max_keys: int = 10_000) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._daily: dict[str, tuple[str, int]] = {}  # key → (date, count)
        self._max = max(1, max_keys)

    def _bounded_put(self, store: dict, key: str, value: Any) -> None:
        store[key] = value
        if len(store) > self._max:
            store.pop(next(iter(store)), None)

    def allow(self, key: str, per_minute: int) -> tuple[bool, float]:
        """每分钟令牌桶;per_minute <= 0 → 恒放行。"""
        if per_minute <= 0:
            return True, 0.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(per_minute, per_minute / 60.0)
            self._bounded_put(self._buckets, key, bucket)
        return bucket.allow()

    def allow_daily(self, key: str, quota: int) -> tuple[bool, str]:
        """日历日请求配额;quota <= 0 → 恒放行。超限 → (False, date)。"""
        if quota <= 0:
            return True, ""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        entry = self._daily.get(key)
        if entry is None or entry[0] != today:
            self._bounded_put(self._daily, key, (today, 1))
            return True, ""
        day, count = entry
        if count >= quota:
            return False, today
        self._daily[key] = (day, count + 1)
        return True, ""


def reset_rate_limiter(limiter: RateLimiter | None) -> None:
    """清空全部桶与日计数(测试隔离用)。"""
    if limiter is None:
        return
    limiter._buckets.clear()
    limiter._daily.clear()
