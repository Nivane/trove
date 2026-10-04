"""查询账本单测:无上限恒真、让路记账、快照键序固定。"""

from __future__ import annotations

from trove.services.analysis.budget import QueryLedger


class TestQueryLedger:
    def test_no_limit_never_blocks_and_records(self):
        led = QueryLedger()
        assert led.can(1000) is True
        led.record("overall")
        led.record("overall")
        led.record("probe")
        assert led.used == 3
        assert led.by_stage == {"overall": 2, "probe": 1}
        assert led.remaining() is None
        assert led.snapshot() == {
            "limit": None, "used": 3,
            "by_stage": {"overall": 2, "probe": 1}, "yielded": [],
        }

    def test_limit_blocks_exactly_at_total(self):
        led = QueryLedger(total=4)
        assert led.can(4) is True
        led.record("overall", 2)
        led.record("probe", 2)
        assert led.can(1) is False          # 4 + 1 > 4
        assert led.remaining() == 0
        assert led.can(0) is True           # 零成本永远可以

    def test_yield_records_and_keeps_budget(self):
        led = QueryLedger(total=2)
        led.record("judge", 2)
        entry = led.yield_("significance", needed=2)
        assert entry == {"stage": "significance", "needed": 2,
                         "reason": "query_budget_exceeded", "remaining": 0}
        assert led.can(2) is False
        assert led.used == 2                # 让路不扣配额,只记账
        assert led.yielded == [entry]

    def test_stage_defaults_unknown(self):
        led = QueryLedger()
        led.record("")
        entry = led.yield_("", needed=1, reason="x")
        assert led.by_stage == {"unknown": 1}
        assert entry["stage"] == "unknown"
