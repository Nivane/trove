"""RateLimiter 单元测试:令牌桶 + 日历日配额。"""

import time

from trove.services.ratelimit import RateLimiter, TokenBucket, reset_rate_limiter


def test_token_bucket_initial_burst_then_throttle():
    bucket = TokenBucket(capacity=3, refill_per_sec=3)
    assert bucket.allow() == (True, 0.0)
    assert bucket.allow() == (True, 0.0)
    assert bucket.allow() == (True, 0.0)
    ok, retry_after = bucket.allow()
    assert ok is False and retry_after >= 1.0


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(capacity=2, refill_per_sec=2)
    assert bucket.allow()[0] is True and bucket.allow()[0] is True
    assert bucket.allow()[0] is False
    time.sleep(1.0)
    assert bucket.allow()[0] is True


def test_allow_disabled_when_zero():
    limiter = RateLimiter()
    assert limiter.allow("k", 0) == (True, 0.0)
    assert limiter.allow_daily("k", 0) == (True, "")


def test_rate_limiter_daily_quota_resets_next_day():
    limiter = RateLimiter()
    quota = 2
    assert limiter.allow_daily("u1", quota) == (True, "")
    assert limiter.allow_daily("u1", quota) == (True, "")
    ok, _ = limiter.allow_daily("u1", quota)
    assert ok is False
    # 模拟次日:清掉后重新计数
    reset_rate_limiter(limiter)
    assert limiter.allow_daily("u1", quota) == (True, "")


def test_per_minute_then_daily_both_apply():
    limiter = RateLimiter()
    key = "u:1"
    # 每分钟 2 次:第 3 次被限
    assert limiter.allow(key, 2) == (True, 0.0)
    assert limiter.allow(key, 2) == (True, 0.0)
    ok, _ = limiter.allow(key, 2)
    assert ok is False


def test_bounded_eviction_keeps_working():
    limiter = RateLimiter(max_keys=3)
    for i in range(10):
        assert limiter.allow(f"user:{i}", 5)[0] is True
    assert len(limiter._buckets) <= 3
