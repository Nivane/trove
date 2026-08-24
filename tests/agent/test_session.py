"""SessionManager tests (LangGraph era).

ask() returns the final WorkflowState; ask_stream() emits graph-native
events whose payloads carry the node name.
"""

import pytest

from trove.core.types import Message
from trove.workflow.state import WorkflowState


class TestSessionLifecycle:
    async def test_start_session(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        assert session.session_id
        assert session.project_name == "p1"

    async def test_start_session_same_project_same_name(self, session_manager):
        s1 = await session_manager.start_session(project_cwd="/tmp/p1")
        s2 = await session_manager.start_session(project_cwd="/tmp/p1")
        assert s1.project_name == s2.project_name == "p1"
        assert s1.session_id != s2.session_id

    async def test_save_and_load_session(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        session.messages.append(Message(role="user", content="hi"))
        await session_manager.save_session(session)

        loaded = await session_manager.load_session(session.session_id, "/tmp/p1")
        assert loaded.messages[0].content == "hi"

    async def test_list_sessions(self, session_manager):
        await session_manager.start_session(project_cwd="/tmp/p1")
        await session_manager.start_session(project_cwd="/tmp/p1")
        sessions = await session_manager.list_sessions("/tmp/p1")
        assert len(sessions) == 2

    async def test_delete_session(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        assert await session_manager.delete_session(session.session_id, "/tmp/p1") is True
        assert await session_manager.delete_session(session.session_id, "/tmp/p1") is False


class TestAsk:
    async def test_ask_returns_final_state(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        state = await session_manager.ask(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        )
        assert isinstance(state, WorkflowState)
        assert state.final_response
        assert state.sql == "SELECT name FROM students;"
        assert state.row_count == 5
        assert state.verdict == "OK"
        assert state.error == ""

    async def test_ask_appends_messages(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        await session_manager.ask(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        )
        assert len(session.messages) == 2  # user + assistant
        assert session.messages[0].role == "user"
        assert session.messages[1].role == "assistant"
        assert session.messages[1].metadata["workflow"] == "reflection"
        assert session.messages[1].metadata["sql"] == "SELECT name FROM students;"

    async def test_ask_persists(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        await session_manager.ask(session=session, question="q", workflow_name="reflection")

        loaded = await session_manager.load_session(session.session_id, "/tmp/p1")
        assert len(loaded.messages) == 2

    async def test_ask_empty_workflow(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        state = await session_manager.ask(
            session=session,
            question="hello",
            workflow_name="empty",
        )
        assert "(未执行任何查询)" in state.final_response

    async def test_ask_unknown_workflow_raises(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        with pytest.raises(KeyError):
            await session_manager.ask(session=session, question="q", workflow_name="nope")


class TestAskStream:
    async def test_stream_yields_graph_events(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in session_manager.ask_stream(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        ):
            events.append(event)

        types = [e["type"] for e in events]
        assert types[0] == "thought"
        assert types[-1] == "done"
        assert "sql" in types
        assert "result" in types
        # graph-native payloads carry the producing node
        sql_event = next(e for e in events if e["type"] == "sql")
        assert sql_event["node"] == "gen_sql"
        done_event = events[-1]
        assert done_event["summary"]["sql"] == "SELECT name FROM students;"

    async def test_stream_emits_begin_events_before_steps(self, session_manager):
        """Node-start events let the UI show the currently-executing step."""
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in session_manager.ask_stream(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        ):
            events.append(event)

        begins = [e for e in events if e["type"] == "begin"]
        steps = [e for e in events if e["type"] == "step"]
        assert begins, "expected node-start events"
        # intel routing is the first executed node
        assert begins[0]["node"] == "route_intent"
        assert begins[0]["seq"] == 1
        # every begin for a top-level node precedes its step completion
        stream_pos = {
            (e["type"], e["node"]): i
            for i, e in enumerate(events)
            if e["type"] in ("begin", "step")
        }
        for node in {b["node"] for b in begins}:
            if ("step", node) in stream_pos:
                assert stream_pos[("begin", node)] < stream_pos[("step", node)]

    async def test_stream_records_exchange(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        async for _ in session_manager.ask_stream(
            session=session, question="q", workflow_name="reflection",
        ):
            pass
        assert len(session.messages) == 2

    async def test_stream_unknown_workflow_emits_error(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in session_manager.ask_stream(
            session=session,
            question="test",
            workflow_name="nonexistent_workflow",
        ):
            events.append(event)

        assert any(e["type"] == "error" for e in events)

    async def test_stream_degradation_emits_error_event(self, tmp_home, sqlite_registry):
        """Graceful degradation: error event replaces done, with the final state."""
        from trove.services.datasource.catalog import CatalogService
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig

        class ScriptedLLM:
            async def chat(self, model, messages, **kwargs):
                return "```sql\nSELEC * FROM students;\n```"  # always invalid

            async def chat_full(self, model, messages, tools=None, **kwargs):
                return {"content": "```sql\nSELEC * FROM students;\n```", "tool_calls": []}

        config = AgentConfig(home=str(tmp_home), target="mock/model")
        services = GraphServices(
            llm=ScriptedLLM(),
            catalog=CatalogService(sqlite_registry),
            connectors=sqlite_registry,
            semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
        )
        manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=build_graphs(services, agentic=False),
            llm_gateway=ScriptedLLM(),
        )
        session = await manager.start_session(project_cwd="/tmp/p1")
        events = []
        async for event in manager.ask_stream(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        ):
            events.append(event)

        assert events[-1]["type"] == "error"
        assert "3 attempts" in events[-1]["summary"]["error"]
        assert events[-1]["summary"]["final_response"]
        # exchange still recorded with the graceful explanation
        assert session.messages[-1].content == events[-1]["summary"]["final_response"]


class TestConversationHistory:
    async def test_ask_injects_prior_exchange_into_state(self, tmp_home):
        """第二次提问时，图收到的初始 state.history 含上一轮问答。"""
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager
        from trove.workflow.state import WorkflowState

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**state.model_dump(), "final_response": "answer"}

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
        )
        session = await manager.start_session(project_cwd="/tmp/p")

        await manager.ask(session=session, question="第一问")
        await manager.ask(session=session, question="第二问")

        assert captured[0].history == ""  # 第一轮无历史
        assert "第一问" in captured[1].history
        assert "answer" in captured[1].history  # 含上一轮答案
        assert "第二问" not in captured[1].history  # 当前问题不混入历史


class TestStructuredSteps:
    def _manager(self, tmp_home, sqlite_registry, responses, **build_kwargs):
        from trove.core.config import AgentConfig
        from trove.services.datasource.catalog import CatalogService
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager

        class Scripted:
            def __init__(self):
                self._it = iter(responses)

            async def chat(self, model, messages, **kwargs):
                return next(self._it)

            async def chat_full(self, model, messages, tools=None, **kwargs):
                return {"content": next(self._it), "tool_calls": []}

        config = AgentConfig(home=str(tmp_home), target="mock/model")
        llm = Scripted()
        services = GraphServices(
            llm=llm,
            catalog=CatalogService(sqlite_registry),
            connectors=sqlite_registry,
            semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
            config=config,
        )
        return SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=build_graphs(services, multi_candidate=False, **build_kwargs),
            llm_gateway=llm,
        )

    async def test_steps_are_numbered_and_timed(self, tmp_home, sqlite_registry):
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query", "```sql\nSELECT name FROM students;\n```", "OK"],
            planner=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        steps = []
        async for event in manager.ask_stream(
            session=session, question="What students are in Alameda county?",
        ):
            if event["type"] == "step":
                steps.append(event)

        # 序号连续递增 + 每步有耗时字段
        assert [s["seq"] for s in steps] == list(range(1, len(steps) + 1))
        assert all(s["elapsed_ms"] >= 0 for s in steps)
        nodes = [s["node"] for s in steps]
        assert "schema_linking" in nodes
        assert "execute_sql" in nodes

    async def test_step_details_carry_artifacts(self, tmp_home, sqlite_registry):
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query", "```sql\nSELECT name FROM students;\n```", "OK"],
            planner=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        steps = {}
        async for event in manager.ask_stream(
            session=session, question="What students are in Alameda county?",
        ):
            if event["type"] == "step":
                steps[event["node"]] = event

        assert steps["execute_sql"]["detail"]["row_count"] == 5
        assert "SELECT name" in " ".join(steps["gen_sql"]["detail"]["sql"].split())
        assert steps["reflect"]["detail"]["verdict"] == "OK"
        assert steps["schema_linking"]["detail"]["matched_tables"]

    async def test_correction_step_marks_retry(self, tmp_home, sqlite_registry):
        manager = self._manager(
            tmp_home, sqlite_registry,
            [
                "query",
                "```sql\nSELECT name FROM students;\n```",
                "```sql\nSELECT COUNT(*) FROM students;\n```",
                "OK",
            ],
            planner=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        steps = []
        async for event in manager.ask_stream(
            session=session, question="how many students are there",
        ):
            if event["type"] == "step":
                steps.append(event)

        # 第二次 gen_sql 带 retry 标记与修正原因
        gen_steps = [s for s in steps if s["node"] == "gen_sql"]
        assert len(gen_steps) == 2
        assert gen_steps[1]["detail"]["retry"] == 1
        assert "校验规则" in gen_steps[1]["detail"]["reason"]


class TestTrajectoryEvents:
    def _manager(self, tmp_home, sqlite_registry, responses, **build_kwargs):
        from trove.core.config import AgentConfig
        from trove.services.datasource.catalog import CatalogService
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager

        class Scripted:
            def __init__(self):
                self._it = iter(responses)

            async def chat(self, model, messages, **kwargs):
                return next(self._it)

            async def chat_full(self, model, messages, tools=None, **kwargs):
                return {"content": next(self._it), "tool_calls": []}

        config = AgentConfig(home=str(tmp_home), target="mock/model")
        llm = Scripted()
        services = GraphServices(
            llm=llm,
            catalog=CatalogService(sqlite_registry),
            connectors=sqlite_registry,
            semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
            config=config,
        )
        return SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=build_graphs(services, multi_candidate=False, **build_kwargs),
            llm_gateway=llm,
        )

    async def test_plan_and_verdict_events(self, tmp_home, sqlite_registry):
        """planner 计划与 reflect 裁决作为轨迹事件实时可见。"""
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query", "plan: use students, group by county", "```sql\nSELECT name FROM students;\n```", "OK"],
            planner=True,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        events = []
        async for event in manager.ask_stream(
            session=session,
            question="What students are in Alameda county?",
        ):
            events.append(event)

        types = [e["type"] for e in events]
        assert "plan" in types
        assert "verdict" in types
        plan_event = next(e for e in events if e["type"] == "plan")
        assert "use students" in plan_event["content"]
        verdict_event = next(e for e in events if e["type"] == "verdict")
        assert verdict_event["verdict"] == "OK"

    async def test_correction_event_on_rule_failure(self, tmp_home, sqlite_registry):
        """规则失败触发修正 → correction 事件实时可见。"""
        manager = self._manager(
            tmp_home, sqlite_registry,
            [
                "query",
                "```sql\nSELECT name FROM students;\n```",   # count 问题返回多行 → 规则失败
                "```sql\nSELECT COUNT(*) FROM students;\n```",  # 修正
                "OK",
            ],
            planner=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        events = []
        async for event in manager.ask_stream(
            session=session, question="how many students are there",
        ):
            events.append(event)

        types = [e["type"] for e in events]
        assert "correction" in types
        correction = next(e for e in events if e["type"] == "correction")
        assert "校验规则" in correction["content"]


class TestSelectCorrectionEvent:
    async def _manager(self, tmp_home, language="zh"):
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        class StubGraph:
            async def astream(self, state, config=None, stream_mode=None):
                yield {"select": {"consensus": False}}

        return SessionManager(
            config=AgentConfig(home=str(tmp_home), language=language),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
        )

    async def test_correction_follows_config_language(self, tmp_home):
        """correction 事件语言跟随配置(默认中文),不按问题语言检测,且不 NameError 中止。"""
        manager = await self._manager(tmp_home)
        session = await manager.start_session(project_cwd="/tmp/p")

        # 默认中文配置:即使英文问题,correction 也是中文
        events = []
        async for event in manager.ask_stream(
            session=session, question="Who traded the most?",
        ):
            events.append(event)
        assert events[-1]["type"] == "done"
        correction = next(e for e in events if e["type"] == "correction")
        assert "候选 SQL 结果不一致" in correction["content"]

        # lang=en 配置:中文问题也出英文 correction
        en_manager = await self._manager(tmp_home, language="en")
        session = await en_manager.start_session(project_cwd="/tmp/p")
        events = []
        async for event in en_manager.ask_stream(
            session=session, question="交易最多的账号是谁",
        ):
            events.append(event)
        correction = next(e for e in events if e["type"] == "correction")
        assert "Candidate SQLs disagreed" in correction["content"]


class TestHistorySummaryFusion:
    async def test_history_prefers_summary_over_old_turns(self, tmp_home):
        """有 compaction summary 时：历史 = 摘要 + 最近 1 轮原文。"""
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**state.model_dump(), "final_response": "answer"}

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        session.summary = "早期摘要：用户关心贷款数据"
        session.messages = [
            Message(role="user", content="旧问题1"),
            Message(role="assistant", content="旧答案1"),
            Message(role="user", content="旧问题2"),
            Message(role="assistant", content="旧答案2"),
        ]
        await manager.ask(session=session, question="新问题")

        history = captured[0].history
        assert "早期摘要" in history
        assert "旧答案2" in history          # 最近一轮保留原文
        assert "旧答案1" not in history      # 更早轮次由摘要替代
        assert "新问题" not in history

    async def test_history_without_summary_keeps_two_turns(self, tmp_home):
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**state.model_dump(), "final_response": "answer"}

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        session.messages = [
            Message(role="user", content="旧问题1"),
            Message(role="assistant", content="旧答案1"),
        ]
        await manager.ask(session=session, question="新问题")
        assert "旧问题1" in captured[0].history  # 无摘要 → 保留全部轮次


class TestLessonCapture:
    async def test_correction_captures_pending_lesson(self, tmp_home, sqlite_registry):
        """修正成功后，修正理由自动沉淀为待确认 lesson。"""
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager
        from trove.services.kb.service import KbService

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {
                    **state.model_dump(),
                    "sql": "SELECT * FROM loan",
                    "error": "",
                    "correction_history": ["no such table: loans"],
                    "final_response": "answer",
                }

        kb = KbService(tmp_home / "proj")
        kb.kb_dir.mkdir(parents=True)
        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
            kb=kb,
            connectors=sqlite_registry,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        await manager.ask(session=session, question="q")

        ds = sqlite_registry.default_name
        assert await kb.list_lessons(ds) == []  # 待确认，不注入
        all_lessons = await kb.list_lessons(ds, confirmed_only=False)
        assert any("loans" in l["pattern"] for l in all_lessons)


class TestTracingCallbacks:
    async def test_callbacks_forwarded_to_graph_config(self, tmp_home):
        """Langfuse CallbackHandler 通过 config["callbacks"] 传给图执行。"""
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(config)
                return {**state.model_dump(), "final_response": "answer"}

        handler = object()
        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
            callbacks=[handler],
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        await manager.ask(session=session, question="q")

        assert captured[0]["callbacks"] == [handler]


class TestCompaction:
    async def test_compact_short_session_noop(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        session.messages.append(Message(role="user", content="hi"))
        session.messages.append(Message(role="assistant", content="hello"))

        compacted = await session_manager.compact_session(session)
        # Too short to compact — unchanged
        assert len(compacted.messages) == 2

    async def test_compact_long_session(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        for i in range(4):
            session.messages.append(Message(role="user", content=f"q{i}"))
            session.messages.append(Message(role="assistant", content=f"a{i}"))

        compacted = await session_manager.compact_session(session, keep_recent=1)
        # summary + 2 recent messages
        assert len(compacted.messages) == 3
        assert compacted.messages[0].role == "system"
        assert compacted.messages[1].content == "q3"
        assert compacted.messages[2].content == "a3"

    async def test_compact_prompt_follows_config_language(self, tmp_home):
        """压缩提示词语言跟随配置:zh 中文 / en 英文(与 select correction 同模式)。"""
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        captured = {}

        class CapturingLLM:
            async def chat(self, model, messages, **kwargs):
                captured["messages"] = messages
                captured["kwargs"] = kwargs
                return "SUMMARY"

        async def make_manager(language):
            return SessionManager(
                config=AgentConfig(home=str(tmp_home), language=language),
                session_store=SessionStore(home_dir=str(tmp_home)),
                graphs={},
                llm_gateway=CapturingLLM(),
            )

        async def fill_and_compact(manager):
            session = await manager.start_session(project_cwd="/tmp/p")
            for i in range(4):
                session.messages.append(Message(role="user", content=f"q{i}"))
                session.messages.append(Message(role="assistant", content=f"a{i}"))
            await manager.compact_session(session, keep_recent=1)

        await fill_and_compact(await make_manager("zh"))
        assert "请压缩这段对话" in captured["messages"][0]["content"]
        assert "摘要：" in captured["messages"][0]["content"]

        await fill_and_compact(await make_manager("en"))
        assert "Summarize this conversation" in captured["messages"][0]["content"]
        assert "Summary:" in captured["messages"][0]["content"]


class TestTokenUsage:
    def test_get_context_usage(self, session_manager):
        session = type("S", (), {})()
        session.messages = [
            Message(role="user", content="hello world " * 10),
        ]
        usage = session_manager.get_context_usage(session)
        assert "token_count" in usage
        assert "usage_ratio" in usage
        assert usage["token_count"] > 0

    def test_should_compact_false_for_short(self, session_manager):
        session = type("S", (), {})()
        session.messages = [Message(role="user", content="short")]
        assert session_manager.should_compact(session) is False

    def test_should_compact_true_for_long(self, session_manager):
        session = type("S", (), {})()
        # ~1M chars ≈ 200k+ tokens, well above 90% of 128k context
        long_content = "word " * 200000
        session.messages = [Message(role="user", content=long_content)]
        assert session_manager.should_compact(session) is True


class TestAutoCompact:
    async def _manager(self, tmp_home, graph):
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        class SummaryLLM:
            async def chat(self, model, messages, **kwargs):
                return "SUMMARY"

        return SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": graph},
            llm_gateway=SummaryLLM(),
        )

    async def test_ask_auto_compacts_over_limit(self, tmp_home):
        """上下文超限时,ask 在构建历史前自动压缩并注入摘要。"""
        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**state.model_dump(), "final_response": "answer"}

        manager = await self._manager(tmp_home, StubGraph())
        session = await manager.start_session(project_cwd="/tmp/p")
        # ~1M chars >> 128k*0.9 → triggers should_compact
        session.messages = [Message(role="user", content="word " * 12000) for _ in range(10)]
        await manager.ask(session=session, question="新问题")

        assert "SUMMARY" in captured[0].history  # 自动压缩摘要进入历史
        assert session.summary == "SUMMARY"
        assert session.messages[0].role == "system"  # 摘要消息已持久化

    async def test_ask_skips_compact_when_short(self, tmp_home):
        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**state.model_dump(), "final_response": "answer"}

        manager = await self._manager(tmp_home, StubGraph())
        session = await manager.start_session(project_cwd="/tmp/p")
        session.messages = [Message(role="user", content="short")]
        await manager.ask(session=session, question="新问题")

        assert "SUMMARY" not in captured[0].history
        assert session.summary is None

    async def test_ask_stream_auto_compacts_over_limit(self, tmp_home):
        """ask_stream 同样在构建历史前自动压缩。"""
        captured = []

        class StubGraph:
            async def astream(self, state, config=None, stream_mode=None):
                captured.append(state)
                yield {"output": {"final_response": "answer"}}

        manager = await self._manager(tmp_home, StubGraph())
        session = await manager.start_session(project_cwd="/tmp/p")
        session.messages = [Message(role="user", content="word " * 12000) for _ in range(10)]
        events = []
        async for event in manager.ask_stream(session=session, question="新问题"):
            events.append(event)

        assert "SUMMARY" in captured[0].history
        assert session.summary == "SUMMARY"


class TestClearSession:
    async def test_clear_removes_messages_and_summary(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        for i in range(4):
            session.messages.append(Message(role="user", content=f"q{i}"))
            session.messages.append(Message(role="assistant", content=f"a{i}"))
        session.summary = "旧摘要"
        await session_manager.save_session(session)

        cleared = await session_manager.clear_session(session)
        assert len(cleared.messages) == 0
        assert cleared.summary is None

        loaded = await session_manager.load_session(session.session_id, "/tmp/p1")
        assert loaded.messages == []
        assert loaded.summary is None


class TestResultCache:
    """精确结果缓存:同会话同问句零 LLM 直接返回;错误不缓存;命中跳过 HITL。"""

    def _manager(self, tmp_home, sqlite_registry, responses, **build_kwargs):
        from trove.core.config import AgentConfig
        from trove.services.datasource.catalog import CatalogService
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager

        class Scripted:
            """循环响应:命中缓存时零调用(断言 calls 不增);未命中时重复提供脚本。"""

            def __init__(self):
                self._pool = list(responses)
                self._i = 0
                self.calls = 0

            async def chat(self, model, messages, **kwargs):
                self.calls += 1
                r = self._pool[self._i % len(self._pool)]
                self._i += 1
                return r

            async def chat_full(self, model, messages, tools=None, **kwargs):
                self.calls += 1
                r = self._pool[self._i % len(self._pool)]
                self._i += 1
                return {"content": r, "tool_calls": []}

        config = AgentConfig(home=str(tmp_home), target="mock/model", result_cache=True)
        llm = Scripted()
        services = GraphServices(
            llm=llm,
            catalog=CatalogService(sqlite_registry),
            connectors=sqlite_registry,
            semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
            config=config,
        )
        manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=build_graphs(services, multi_candidate=False, **build_kwargs),
            llm_gateway=llm,
            connectors=sqlite_registry,
        )
        return manager, llm

    Q = "What students are in Alameda county?"
    RESPONSES = ["query", "```sql\nSELECT name FROM students;\n```", "OK"]

    async def test_identical_question_hits_cache_zero_llm(self, tmp_home, sqlite_registry):
        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), planner=False)
        session = await manager.start_session(project_cwd="/tmp/p")
        first = await manager.ask(session=session, question=self.Q)
        assert first.sql and first.verdict == "OK"
        calls = llm.calls

        second = await manager.ask(session=session, question=self.Q)
        assert second.sql == first.sql
        assert second.verdict == "OK"
        assert second.error == ""
        assert llm.calls == calls  # 命中 → 零额外 LLM 调用
        assert len(session.messages) == 4  # user+assistant × 2(交换照常记录)

    async def test_normalization_equivalence(self, tmp_home, sqlite_registry):
        """标点/大小写/空白变体归一化后命中同一键。"""
        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), planner=False)
        session = await manager.start_session(project_cwd="/tmp/p")
        first = await manager.ask(session=session, question=self.Q)
        calls = llm.calls
        second = await manager.ask(session=session, question="  WHAT STUDENTS, ARE IN alameda county?? ")
        assert second.sql == first.sql
        assert llm.calls == calls

    async def test_session_isolation(self, tmp_home, sqlite_registry):
        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), planner=False)
        s1 = await manager.start_session(project_cwd="/tmp/p")
        s2 = await manager.start_session(project_cwd="/tmp/p")
        await manager.ask(session=s1, question=self.Q)
        calls = llm.calls
        await manager.ask(session=s2, question=self.Q)  # 不同会话 → 未命中
        assert llm.calls > calls

    async def test_datasource_isolation(self, tmp_home, sqlite_registry):
        """键含数据源分量:默认数据源变化后同问句不再命中。"""
        from trove.core.types import DatasourceConfig

        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), planner=False)
        session = await manager.start_session(project_cwd="/tmp/p")
        await manager.ask(session=session, question=self.Q)
        calls = llm.calls
        key_before = manager._cache_key(session, self.Q)
        await sqlite_registry.register(DatasourceConfig(
            name="other", type="sqlite", connection_params={"path": ":memory:"},
            default=True,
        ))
        key_after = manager._cache_key(session, self.Q)
        assert key_before != key_after  # 数据源分量变化
        await manager.ask(session=session, question=self.Q)  # 未命中 → 重新跑
        assert llm.calls > calls

    async def test_ttl_expiry(self, tmp_home, sqlite_registry):
        from trove.agent.session import RESULT_CACHE_TTL_S

        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), planner=False)
        session = await manager.start_session(project_cwd="/tmp/p")
        await manager.ask(session=session, question=self.Q)
        calls = llm.calls
        key = manager._cache_key(session, self.Q)
        old_cached_at = manager._result_cache[key]["cached_at"]
        manager._result_cache[key]["cached_at"] -= RESULT_CACHE_TTL_S + 1.0
        await manager.ask(session=session, question=self.Q)  # TTL 过期 → 惰性淘汰 → 重新跑
        assert llm.calls > calls
        # 重跑后条目被重新写入,但 cached_at 是新的(过期 → 淘汰 → 重写闭环)
        assert key in manager._result_cache
        assert manager._result_cache[key]["cached_at"] > old_cached_at

    async def test_error_run_not_cached(self, tmp_home, sqlite_registry):
        """错误/打回的结果不写缓存,下次同问照常重跑。"""
        manager, llm = self._manager(
            tmp_home, sqlite_registry,
            ["query", "```sql\nSELEC * FROM students;\n```", "OK"],
            planner=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        first = await manager.ask(session=session, question=self.Q)
        assert first.error
        assert manager._result_cache == {}  # 错误不写
        await manager.ask(session=session, question=self.Q)  # 再问仍重跑
        assert llm.calls > 3

    async def test_stream_hit_marks_cached(self, tmp_home, sqlite_registry):
        """流式命中:事件形状与实跑一致(sql → result → done),summary 带 cached 标记。"""
        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), planner=False)
        session = await manager.start_session(project_cwd="/tmp/p")
        async for _ in manager.ask_stream(session=session, question=self.Q):
            pass
        calls = llm.calls

        events = []
        async for e in manager.ask_stream(session=session, question=self.Q):
            events.append(e)
        assert llm.calls == calls
        types = [e["type"] for e in events]
        assert "sql" in types and "result" in types and "done" in types
        done = events[-1]
        assert done["type"] == "done"
        assert done["summary"].get("cached") is True
        assert done["summary"]["sql"] == "SELECT name FROM students;"
        assert done["summary"]["row_count"] == 5
        sql_event = next(e for e in events if e["type"] == "sql")
        assert "students" in sql_event["content"]  # format_sql 美化后的语句

    async def test_hitl_enabled_hit_skips_confirmation(self, tmp_home):
        """HITL 开启时缓存命中照常返回:读钩子在图执行之前,中断不触发。"""
        import time

        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        invoked = []

        class HitlGraph:
            async def ainvoke(self, state, config=None):
                invoked.append(state.question)  # 命中时绝不应被调
                return {**state.model_dump(), "sql": "SELECT 1", "row_count": 1,
                        "verdict": "OK", "final_response": "answer",
                        "__interrupt__": [type("I", (), {"value": {"kind": "confirm_sql"}})()]}

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home), result_cache=True, hitl=True),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": HitlGraph()},
            llm_gateway=None,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        q = "How many students?"
        # 首次运行已人工确认过 → 结果在缓存里
        manager._result_cache[manager._cache_key(session, q)] = {
            "summary": {
                "session_id": session.session_id, "question": q, "sql": "SELECT 1",
                "row_count": 1, "verdict": "OK", "reason": "", "error": "",
                "kb_hits": [], "semantics": "", "insights": [],
                "hitl_status": "ok", "final_response": "answer",
                "cached": True, "dialect": "sqlite",
            },
            "cached_at": time.time(),
        }
        final = await manager.ask(session=session, question=q)
        assert final.sql == "SELECT 1"
        assert final.verdict == "OK"
        assert not invoked  # 图从未执行 → HITL 中断未触发
        assert session.messages[-1].role == "assistant"  # 交换照常记录


async def _no_action(*args, **kwargs):
    return {"action": "none"}


async def _no_tasks(*args, **kwargs):
    return []


class TestDatasourceThreading:
    async def test_ask_stream_threads_datasource_into_state(
        self, session_manager, monkeypatch
    ):
        captured: dict = {}
        async def fake_stream(session, graph, state, workflow_name, run_id, *, task=None):
            captured["state"] = state
            yield {"type": "done", "content": "ok", "summary": {}}

        monkeypatch.setattr(session_manager, "_interpret_followup", _no_action)
        monkeypatch.setattr(session_manager, "_decompose_tasks", _no_tasks)
        monkeypatch.setattr(session_manager, "_stream_graph_run", fake_stream)

        session = await session_manager.start_session(user_id="t")
        events = [
            e async for e in session_manager.ask_stream(
                session, "hi", datasource="financial"
            )
        ]
        assert events[-1]["type"] == "done"
        assert captured["state"].datasource == "financial"

    async def test_ask_stream_defaults_datasource_to_empty(self, session_manager, monkeypatch):
        captured: dict = {}
        async def fake_stream(session, graph, state, workflow_name, run_id, *, task=None):
            captured["state"] = state
            yield {"type": "done", "content": "ok", "summary": {}}

        monkeypatch.setattr(session_manager, "_interpret_followup", _no_action)
        monkeypatch.setattr(session_manager, "_decompose_tasks", _no_tasks)
        monkeypatch.setattr(session_manager, "_stream_graph_run", fake_stream)

        session = await session_manager.start_session(user_id="t")
        events = [
            e async for e in session_manager.ask_stream(session, "hi")
        ]
        assert events[-1]["type"] == "done"
        assert captured["state"].datasource == ""

    async def test_cache_key_uses_explicit_datasource(self, session_manager):
        session = await session_manager.start_session(user_id="t")
        key = session_manager._cache_key(
            session, "how many rows?", datasource="financial"
        )
        assert key[0] == session.session_id
        assert key[1] == "financial"
