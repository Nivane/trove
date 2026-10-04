"""``measure_due`` —— 闭环验收的到期面(四道门,各自的语义都要看得见)。

  - ``outcome_after_days <= 0`` → 全关,不查库(未配置的部署逐字节不变);
  - 没满 N 天 → 跳过,**不算已测**(到期那次 sweep 再看);
  - verifier 返回 ``None`` → 期没滚过行动日,跳过、不写行(还没到时候 ≠ 测了);
  - verifier 缺席/异常/坏返回 → **一行 error outcome**(响亮),不重试。

另钉一条与直觉相反但刻意的:**``enabled=False`` 不是门** —— 测量是读,
不是外送;外送关掉之后,已经出去的那批更需要验收。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from trove.services.action.service import ActionService, _num, _tri
from trove.services.action.store import ActionStore

from tests.services.action.test_action_store import _proposal

NOW = datetime(2026, 10, 10, 3, 0, 0)
OBSERVED = {
    "method": "its", "delta": 49.5, "pct": 0.49, "z": 4.2,
    "outside_band": True, "confidence": 0.2, "metric": "loan_balance",
    "rule_rev": "rev-1", "window": ["2026-10-01", "2026-10-31"],
    "sql": "SELECT ...",
}


class FakeVerifier:
    def __init__(self, result=None, raises=None, not_a_dict=False):
        self.result = result
        self.raises = raises
        self.not_a_dict = not_a_dict
        self.seen: list = []

    async def __call__(self, proposal):
        self.seen.append(proposal.id)
        if self.raises is not None:
            raise self.raises
        if self.not_a_dict:
            return ["not", "a", "dict"]
        return self.result


@pytest.fixture()
async def env(tmp_path):
    """store + 一个手摆了 dispatched 提案的 service(测量面最小装置)。"""
    store = ActionStore(tmp_path)
    verifier = FakeVerifier(result=dict(OBSERVED))
    service = ActionService(store, None, None, enabled=False, verifier=verifier)
    service.outcome_after_days = 7
    try:
        yield service, store, verifier
    finally:
        await store.dispose()


async def _dispatch(store, pid="p-1", when="2026-10-01T09:00:00",
                    status="dispatched"):
    await store.create_proposal(_proposal(
        id=pid, idempotency_key="k-" + pid, dispatched_at=when))
    await store.update_proposal(pid, status=status, dispatched_at=when)


class TestGates:
    async def test_default_off_never_touches_the_store(self, env):
        service, store, verifier = env
        service.outcome_after_days = 0
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 0
        assert verifier.seen == []
        assert await store.list_outcomes("p-1") == []

    async def test_too_young_is_skipped_and_stays_a_candidate(self, env):
        service, store, verifier = env
        await _dispatch(store, when="2026-10-08T09:00:00")   # 才 2 天
        assert await service.measure_due(now=NOW) == 0
        assert verifier.seen == []
        # 到期后那次 sweep 会再看到它
        service.outcome_after_days = 1
        assert await service.measure_due(now=NOW) == 1

    async def test_boundary_exactly_n_days_is_due(self, env):
        service, store, verifier = env
        await _dispatch(store, when="2026-10-03T03:00:00")   # 恰 7 天
        assert await service.measure_due(now=NOW) == 1

    async def test_disabled_does_not_gate_measurement(self, env):
        """测量是读,不是外送 —— enabled=False 照测(见模块 docstring)。"""
        service, store, verifier = env
        assert service.enabled is False
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 1

    async def test_proposal_without_any_stamp_is_left_alone(self, env):
        service, store, verifier = env
        await store.create_proposal(_proposal(id="p-nostamp",
                                              idempotency_key="kn"))
        await store.update_proposal("p-nostamp", status="dispatched")
        assert await service.measure_due(now=NOW) == 0
        assert verifier.seen == []

    async def test_delivered_is_measured_too(self, env):
        service, store, verifier = env
        await _dispatch(store, status="delivered")
        assert await service.measure_due(now=NOW) == 1


class TestMeasurement:
    async def test_row_is_written_with_the_observed_fields(self, env):
        service, store, verifier = env
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 1

        (o,) = await store.list_outcomes("p-1")
        assert o.measured_at == "2026-10-10T03:00:00"
        assert (o.window_start, o.window_end) == ("2026-10-01", "2026-10-31")
        assert o.metric == "loan_balance" and o.rule_rev == "rev-1"
        assert o.outside_band is True and o.delta == pytest.approx(49.5)
        assert o.method == "its" and o.confidence == pytest.approx(0.2)
        assert o.observed["sql"] == "SELECT ..."
        assert o.error == ""

    async def test_one_shot_no_second_measurement(self, env):
        """一次测量一个结局:测过之后不再是候选,再 sweep 也不重测。"""
        service, store, verifier = env
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 1
        assert await service.measure_due(now=NOW) == 0
        assert verifier.seen == ["p-1"]

    async def test_limit_caps_one_sweep(self, env):
        service, store, verifier = env
        await _dispatch(store, "p-1", when="2026-10-01T09:00:00")
        await _dispatch(store, "p-2", when="2026-10-02T09:00:00")
        assert await service.measure_due(now=NOW, limit=1) == 1
        assert verifier.seen == ["p-1"]
        assert await service.measure_due(now=NOW) == 1

    async def test_verifier_none_is_not_yet_due_not_an_error(self, env):
        """``None`` = 期没滚过行动日 —— 跳过、不写行,下次再试。"""
        service, store, verifier = env
        verifier.result = None
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 0
        assert await store.list_outcomes("p-1") == []
        # 仍然在候选里(不是"已测")
        assert len(await store.list_unmeasured_proposals(
            ("dispatched", "delivered"))) == 1


class TestLoudFailures:
    async def test_no_verifier_writes_an_error_row(self, env):
        service, store, _ = env
        service.verifier = None
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 1
        (o,) = await store.list_outcomes("p-1")
        assert o.error == "no_verifier"
        assert o.outside_band is None

    async def test_verifier_exception_is_recorded_not_raised(self, env):
        service, store, verifier = env
        verifier.raises = RuntimeError("boom")
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 1
        (o,) = await store.list_outcomes("p-1")
        assert o.error.startswith("verifier_failed:")
        assert "boom" in o.error

    async def test_verifier_bad_result(self, env):
        service, store, verifier = env
        verifier.not_a_dict = True
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 1
        (o,) = await store.list_outcomes("p-1")
        assert o.error == "verifier_bad_result"

    async def test_error_row_is_also_one_shot(self, env):
        """失败也不重试:verifier 恢复后回填测的是另一个期间(见 outcome.py)。"""
        service, store, verifier = env
        verifier.raises = RuntimeError("boom")
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 1
        verifier.raises = None
        assert await service.measure_due(now=NOW) == 0


class TestDetails:
    async def test_get_envelope_carries_outcomes(self, env):
        service, store, verifier = env
        await _dispatch(store)
        await service.measure_due(now=NOW)
        detail = await service.get("p-1")
        assert len(detail["outcomes"]) == 1
        assert detail["outcomes"][0].outside_band is True

    async def test_window_garbage_leaves_columns_blank(self, env):
        service, store, verifier = env
        verifier.result = {**OBSERVED, "window": "上个月"}
        await _dispatch(store)
        assert await service.measure_due(now=NOW) == 1
        (o,) = await store.list_outcomes("p-1")
        assert (o.window_start, o.window_end) == ("", "")

    def test_num_and_tri_helpers(self):
        assert _num(True) is None and _num("x") is None and _num(None) is None
        assert _num("3.5") == 3.5
        assert _tri(True) is True and _tri(1) is True
        assert _tri(False) is False and _tri(0) is False
        assert _tri(None) is None and _tri("maybe") is None
