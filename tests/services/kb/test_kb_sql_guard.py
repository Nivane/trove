"""SQL 护栏:语法 + 试执行(LIMIT 1,10s),失败一律 False 不抛。"""
from __future__ import annotations

import asyncio

from trove.services.kb.sql_guard import sqlglot_ok, trial_ok


class FakeRegistry:
    def __init__(self, *, fail=False, slow=False):
        self.fail, self.slow = fail, slow

    async def execute(self, sql, datasource):
        if self.slow:
            await asyncio.sleep(0.2)
        if self.fail:
            raise RuntimeError("bad sql")
        return type("R", (), {"rows": [[1]]})()


def test_sqlglot_ok():
    assert sqlglot_ok("SELECT 1")
    assert not sqlglot_ok("SELEC 1")
    assert not sqlglot_ok("")


async def test_trial_ok_true_and_false():
    assert await trial_ok(FakeRegistry(), "demo", "SELECT 1")
    assert not await trial_ok(FakeRegistry(fail=True), "demo", "SELECT bad")


async def test_trial_ok_timeout(monkeypatch):
    monkeypatch.setattr("trove.services.kb.sql_guard.TRIAL_TIMEOUT_S", 0.01)
    assert not await trial_ok(FakeRegistry(slow=True), "demo", "SELECT 1")
