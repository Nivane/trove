"""``llm.egress`` 审计写入(会话侧汇总)。

每次 run 结束后 ``_audit_egress`` 把 state 上的 ``egress_report`` 汇总进
audit_log:回答「这一问里有没有声明字段的值被发给过模型」。钉死的边界:

- details 只含字段名与计数,**不含值本身**;工具名/字段名排序输出;
- ``egress.audit=False`` / 无 auth / 空报告 → 不写空行;
- 写审计失败被吞掉(审计绝不影响查询链路)。
"""

from __future__ import annotations

from types import SimpleNamespace

from trove.agent.session import SessionManager
from trove.core.config import AgentConfig, EgressConfig
from trove.core.types import Session
from trove.workflow.state import WorkflowState

RAW_VALUE = "13812348888"


class _Auth:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[dict] = []

    async def record_audit(self, action, *, user=None, details=None):
        if self.error is not None:
            raise self.error
        self.calls.append({"action": action, "user": user, "details": details})


def _owner(auth: _Auth, *, audit: bool = True):
    """只给 ``_audit_egress`` 依赖的属性(不构造完整 SessionManager)。"""

    async def _audit_user(session, final):
        return {"id": 7, "username": "alice"}

    cfg = AgentConfig(target="mock/model")
    cfg.egress = EgressConfig(audit=audit)
    return SimpleNamespace(_auth=auth, config=cfg, _audit_user=_audit_user)


def _final(report) -> WorkflowState:
    return WorkflowState(
        session_id="s1", run_id="r1", datasource="demo",
        question="q", egress_report=report,
    )


SESSION = Session(session_id="s1", user_id="7")

REPORT = {
    "probe_query": {"fields": {"phone": "partial", "name": "partial"},
                    "rows": 3, "values": 0},
    "prompt": {"fields": {"email": "hash"}, "rows": 0, "values": 4},
}


class TestEgressAudit:
    async def test_writes_sorted_report_without_values(self):
        auth = _Auth()
        owner = _owner(auth)
        await SessionManager._audit_egress(owner, SESSION, _final(REPORT))

        assert len(auth.calls) == 1
        call = auth.calls[0]
        assert call["action"] == "llm.egress"
        assert call["user"] == {"id": 7, "username": "alice"}
        details = call["details"]
        assert details["session_id"] == "s1"
        assert details["run_id"] == "r1"
        assert details["datasource"] == "demo"
        # 工具按名排序、字段按名排序(同一份报告两次审计逐字节一致)
        assert list(details["egress"]) == ["probe_query", "prompt"]
        assert list(details["egress"]["probe_query"]["fields"]) == ["name", "phone"]
        assert details["egress"]["probe_query"]["rows"] == 3
        assert details["egress"]["prompt"]["values"] == 4
        # 审计不含任何值本身
        assert RAW_VALUE not in str(details)

    async def test_report_of_merge_shape_is_echoed(self):
        """``merge_egress_reports`` 的产物(无 channel/tool 键、含 withheld)原样过。"""
        auth = _Auth()
        owner = _owner(auth)
        report = {"check_result": {"fields": {}, "rows": 0, "values": 0,
                                   "withheld": 2}}
        await SessionManager._audit_egress(owner, SESSION, _final(report))
        assert auth.calls[0]["details"]["egress"]["check_result"]["withheld"] == 2

    async def test_audit_disabled_writes_nothing(self):
        auth = _Auth()
        owner = _owner(auth, audit=False)
        await SessionManager._audit_egress(owner, SESSION, _final(REPORT))
        assert auth.calls == []

    async def test_no_auth_skips(self):
        owner = SimpleNamespace(_auth=None, config=AgentConfig())

        async def _audit_user(session, final):  # pragma: no cover - 不应被调用
            raise AssertionError("_audit_user must not run without auth")

        owner._audit_user = _audit_user
        await SessionManager._audit_egress(owner, SESSION, _final(REPORT))

    async def test_empty_report_writes_nothing(self):
        for report in (None, {}):
            auth = _Auth()
            owner = _owner(auth)
            await SessionManager._audit_egress(owner, SESSION, _final(report))
            assert auth.calls == []

    async def test_audit_failure_is_swallowed(self):
        owner = _owner(_Auth(error=RuntimeError("db down")))
        # 不抛:审计失败不能拖垮回答
        await SessionManager._audit_egress(owner, SESSION, _final(REPORT))
