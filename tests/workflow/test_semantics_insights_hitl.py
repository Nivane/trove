"""Tests for semantics explanation, HITL gate, and insights nodes."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from trove.core.config import AgentConfig
from trove.workflow.graphs import GraphServices, build_graphs
from trove.workflow.nodes.semantics import make_semantics
from trove.workflow.nodes.hitl import make_hitl, _normalize
from trove.workflow.nodes.insights import make_insights
from trove.workflow.state import WorkflowState


class RecordingLLM:
    """Scripted LLM (content-only); records call messages."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        return self._responses.pop(0)

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return {"content": self._responses.pop(0), "tool_calls": []}


def make_state(**kwargs):
    defaults = {
        "session_id": "s1",
        "run_id": "r1",
        "question": "What is the average loan amount?",
        "lang": "zh",
        "sql": "SELECT AVG(amount) FROM loan;",
    }
    defaults.update(kwargs)
    return WorkflowState(**defaults)


def on_config(**kwargs):
    cfg = dict(target="mock/model", explain_semantics=True, hitl=True, insights=True)
    cfg.update(kwargs)
    return AgentConfig(**cfg)


def make_services(llm, config=None, connectors=None):
    return GraphServices(
        llm=llm,
        connectors=connectors,
        config=config or AgentConfig(target="mock/model"),
    )


class TestSemanticsNode:
    async def test_explains_sql_when_enabled(self):
        llm = RecordingLLM(["这条 SQL 计算平均贷款金额"])
        node = make_semantics(llm, on_config())
        out = await node(make_state())
        assert out["semantics"] == "这条 SQL 计算平均贷款金额"
        assert len(llm.calls) == 1
        # prompt 含 SQL 与问题
        prompt = " ".join(str(m.get("content", "")) for m in llm.calls[0])
        assert "SELECT AVG(amount) FROM loan;" in prompt
        assert "What is the average loan amount?" in prompt

    async def test_passes_through_when_disabled(self):
        llm = RecordingLLM([])
        node = make_semantics(llm, on_config(explain_semantics=False))
        out = await node(make_state())
        assert out == {}

    async def test_passes_through_without_sql(self):
        llm = RecordingLLM([])
        node = make_semantics(llm, on_config())
        out = await node(make_state(sql=""))
        assert out == {}

    async def test_llm_failure_degrades_to_empty(self):
        class Boom:
            async def chat(self, model, messages, **kwargs):
                raise RuntimeError("llm down")

        node = make_semantics(Boom(), on_config())
        out = await node(make_state())
        assert out["semantics"] == ""

    async def test_skips_on_correction_round(self):
        """修正轮:semantics 仅供 HITL 确认展示,而 HITL 在修正轮直接放行
        (hitl.py in_correction pass-through)——修正轮再调 LLM 解释 SQL 是
        纯浪费,每次修正轮 +1 次 LLM 调用。"""
        llm = RecordingLLM([])
        node = make_semantics(llm, on_config())
        out = await node(make_state(error_feedback="execution failed"))
        assert out == {}
        assert len(llm.calls) == 0


class TestInsightsNode:
    ROWS = [[1000.0], [2000.0], [3000.0]]

    async def test_generates_insights_from_rows(self):
        llm = RecordingLLM(["- 最大贷款 3000\n- 平均 2000"])
        node = make_insights(llm, on_config())
        out = await node(make_state(
            columns=["amount"], rows=self.ROWS, row_count=3,
        ))
        assert out["insights"] == ["最大贷款 3000", "平均 2000"]
        prompt = " ".join(str(m.get("content", "")) for m in llm.calls[0])
        assert "3000.0" in prompt and "amount" in prompt

    async def test_skips_when_disabled(self):
        llm = RecordingLLM([])
        node = make_insights(llm, on_config(insights=False))
        out = await node(make_state(columns=["amount"], rows=self.ROWS, row_count=3))
        assert out == {}

    async def test_skips_when_no_rows(self):
        llm = RecordingLLM([])
        node = make_insights(llm, on_config())
        out = await node(make_state(columns=["amount"], rows=[], row_count=0))
        assert out == {}

    async def test_skips_when_not_executed(self):
        llm = RecordingLLM([])
        node = make_insights(llm, on_config())
        out = await node(make_state(columns=[], rows=[], row_count=-1))
        assert out == {}

    async def test_strips_bullets_and_truncates(self):
        llm = RecordingLLM(["\n".join(f"- 洞察 {i}" for i in range(10))])
        node = make_insights(llm, on_config())
        out = await node(make_state(
            columns=["amount"], rows=self.ROWS, row_count=3,
        ))
        assert len(out["insights"]) <= 6
        assert out["insights"][0] == "洞察 0"


class TestHITLNode:
    async def test_passes_through_when_disabled(self):
        node = make_hitl(on_config(hitl=False))
        out = await node(make_state())
        assert out == {}

    async def test_passes_through_on_correction_round(self):
        node = make_hitl(on_config())
        out = await node(make_state(error_feedback="execution failed"))
        assert out == {}

    async def test_interrupt_unavailable_passes_with_approve(self, monkeypatch):
        def boom(value):
            raise RuntimeError("no checkpointer")

        monkeypatch.setattr("trove.workflow.nodes.hitl.interrupt", boom)
        node = make_hitl(on_config())
        out = await node(make_state())
        assert out == {"hitl_status": "approved"}


class TestHITLNormalize:
    @pytest.mark.parametrize("decision,expected", [
        ("yes", "approved"),
        ("approve", "approved"),
        ("ok", "approved"),
        (True, "approved"),
        ({"decision": "yes"}, "approved"),
        ("no", "rejected"),
        ("reject", "rejected"),
        (False, "rejected"),
        ({"decision": "no"}, "rejected"),
        ({"decision": False}, "rejected"),
        ({"approve": False}, "rejected"),
    ])
    def test_maps_decision(self, decision, expected):
        assert _normalize(decision) == expected


class TestGraphHITLFlow:
    """端到端:reflection 图 + HITL 中断 → resume 批准 → 执行 → 洞察。"""

    @pytest.fixture
    def enabled_config(self):
        return AgentConfig(
            target="mock/model",
            explain_semantics=True,
            hitl=True,
            insights=True,
        )

    def _build(self, llm, config):
        return build_graphs(
            GraphServices(llm=llm, connectors=None, config=config),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )["reflection"]

    async def test_interrupt_pauses_before_execution(self, sqlite_registry, enabled_config):
        """SQL 生成后、执行前中断;返回 pending 状态并携带确认请求。"""
        llm = RecordingLLM([
            "query",  # route_intent
            "```sql\nSELECT name FROM students;\n```",  # gen_sql
            "这条 SQL 查询所有学生姓名",  # semantics
            # 无 reflect:执行前被 interrupt 打断,图在此暂停
        ])
        graph = build_graphs(
            GraphServices(
                llm=llm, connectors=sqlite_registry,
                config=enabled_config,
            ),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )["reflection"]
        cfg = {"configurable": {"thread_id": "hitl-1"}}
        result = await graph.ainvoke(make_state(session_id="hitl-1"), cfg)
        assert "__interrupt__" in result
        interrupts = result["__interrupt__"]
        payload = interrupts[0].value
        assert payload["kind"] == "confirm_sql"
        assert "SELECT name FROM students;" in payload["sql"]
        assert payload["semantics"] == "这条 SQL 查询所有学生姓名"
        # 执行未发生
        assert result["row_count"] == -1
        assert result["hitl_status"] == ""

    async def test_resume_approve_runs_query(self, sqlite_registry, enabled_config):
        """resume=yes → 批准 → 执行 SQL → reflect → insights → output。"""
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "这条 SQL 查询所有学生姓名",
            "OK",  # reflect
            "- 共返回 5 名学生",  # insights
        ])
        graph = build_graphs(
            GraphServices(
                llm=llm, connectors=sqlite_registry,
                config=enabled_config,
            ),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )["reflection"]
        cfg = {"configurable": {"thread_id": "hitl-2"}}
        result = await graph.ainvoke(make_state(session_id="hitl-2"), cfg)
        assert "__interrupt__" in result

        final = await graph.ainvoke(Command(resume="yes"), cfg)
        assert final["hitl_status"] == "approved"
        assert final["row_count"] == 5
        assert final["verdict"] == "OK"
        assert final["insights"] == ["共返回 5 名学生"]
        assert "共返回 5 名学生" in final["final_response"]

    async def test_resume_reject_aborts_without_execution(self, sqlite_registry, enabled_config):
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "这条 SQL 查询所有学生姓名",
        ])
        graph = build_graphs(
            GraphServices(
                llm=llm, connectors=sqlite_registry,
                config=enabled_config,
            ),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )["reflection"]
        cfg = {"configurable": {"thread_id": "hitl-3"}}
        result = await graph.ainvoke(make_state(session_id="hitl-3"), cfg)
        assert "__interrupt__" in result

        final = await graph.ainvoke(Command(resume="no"), cfg)
        assert final["hitl_status"] == "rejected"
        assert final["row_count"] == -1  # 未执行
        assert "取消" in final["intent_answer"]
        assert "取消" in final["final_response"]

    async def test_insights_disabled_skips(self, sqlite_registry, enabled_config):
        """insights 关闭 → 洞察节点透传,输出无洞察段。"""
        enabled_config.insights = False
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "OK",
        ])
        graph = build_graphs(
            GraphServices(
                llm=llm, connectors=sqlite_registry,
                config=enabled_config,
            ),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )["reflection"]
        cfg = {"configurable": {"thread_id": "hitl-4"}}
        final = await graph.ainvoke(make_state(session_id="hitl-4"), cfg)
        assert final["insights"] == []
        assert "Insights" not in final["final_response"]

    async def test_hitl_disabled_runs_through(self, sqlite_registry, enabled_config):
        """hitl 关闭 → 无中断,直接执行。"""
        enabled_config.hitl = False
        llm = RecordingLLM([
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "OK",
        ])
        graph = build_graphs(
            GraphServices(
                llm=llm, connectors=sqlite_registry,
                config=enabled_config,
            ),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )["reflection"]
        cfg = {"configurable": {"thread_id": "hitl-5"}}
        final = await graph.ainvoke(make_state(session_id="hitl-5"), cfg)
        assert "__interrupt__" not in final
        assert final["row_count"] == 5


class ScriptedGateway:
    """Scripted LLM gateway for SessionManager-level tests."""

    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append(messages)
        return next(self._responses)

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append(messages)
        return {"content": next(self._responses), "tool_calls": []}

class TestSessionManagerHITL:
    """SessionManager.ask 中断 → resume 批准/否决的端到端流程。"""

    async def _make_manager(self, tmp_home, sqlite_registry, responses):
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        config = AgentConfig(
            home=str(tmp_home), target="mock/model",
            explain_semantics=True, hitl=True, insights=True,
        )
        gateway = ScriptedGateway(responses)
        services = GraphServices(
            llm=gateway,
            connectors=sqlite_registry,
            config=config,
        )
        graphs = build_graphs(
            services, checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )
        manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=graphs,
            llm_gateway=gateway,
        )
        return manager, gateway

    async def test_ask_pauses_and_resume_approve(self, tmp_home, sqlite_registry):
        manager, _ = await self._make_manager(tmp_home, sqlite_registry, [
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "这条 SQL 查询所有学生姓名",
            "OK",
            "- 共 5 名学生",
        ])
        session = await manager.start_session(project_cwd="/tmp/p1")

        paused = await manager.ask(
            session, "What is the average loan amount?", workflow_name="reflection",
        )
        assert paused.hitl_status == "pending"
        assert "执行确认" in paused.final_response
        assert "SELECT name FROM students;" in paused.final_response
        assert "这条 SQL 查询所有学生姓名" in paused.final_response

        final = await manager.resume(session, "yes")
        assert final.hitl_status == "approved"
        assert final.row_count == 5
        assert final.verdict == "OK"
        assert final.insights == ["共 5 名学生"]
        # 一次完整问答落库(user + assistant)
        assert len(session.messages) == 2

    async def test_ask_pauses_and_resume_reject(self, tmp_home, sqlite_registry):
        manager, _ = await self._make_manager(tmp_home, sqlite_registry, [
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "这条 SQL 查询所有学生姓名",
        ])
        session = await manager.start_session(project_cwd="/tmp/p1")

        paused = await manager.ask(
            session, "What is the average loan amount?", workflow_name="reflection",
        )
        assert paused.hitl_status == "pending"

        final = await manager.resume(session, "no")
        assert final.hitl_status == "rejected"
        assert final.row_count == -1
        assert "取消" in final.final_response

    async def test_ask_stream_emits_hitl_event(self, tmp_home, sqlite_registry):
        """ask_stream 在 HITL 门处发出 hitl 事件并停止,不带 done。"""
        manager, _ = await self._make_manager(tmp_home, sqlite_registry, [
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "这条 SQL 查询所有学生姓名",
        ])
        session = await manager.start_session(project_cwd="/tmp/p1")

        events = []
        async for event in manager.ask_stream(
            session, "What is the average loan amount?", workflow_name="reflection",
        ):
            events.append(event)

        types = [e["type"] for e in events]
        assert types[0] == "thought"
        assert "hitl" in types
        assert "done" not in types
        hitl_event = next(e for e in events if e["type"] == "hitl")
        assert hitl_event["node"] == "hitl"
        assert "SELECT name FROM students;" in hitl_event["content"]

        # 事件后可直接 resume 继续同一线程
        final = await manager.resume(session, "yes")
        assert final.hitl_status == "approved"
        assert final.row_count == 5

    async def test_stream_with_hitl_disabled_runs_to_done(self, tmp_home, sqlite_registry):
        manager, _ = await self._make_manager(tmp_home, sqlite_registry, [
            "query",
            "```sql\nSELECT name FROM students;\n```",
            "OK",
        ])
        manager.config.hitl = False
        session = await manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in manager.ask_stream(
            session, "What is the average loan amount?", workflow_name="reflection",
        ):
            events.append(event)
        assert events[-1]["type"] == "done"

