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

    async def test_set_pinned(self, session_manager):
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        assert await session_manager.set_pinned(
            session.session_id, True, "/tmp/p1"
        ) is True
        rows = await session_manager.list_sessions("/tmp/p1")
        assert rows[0]["pinned"] is True
        assert await session_manager.set_pinned("missing", True, "/tmp/p1") is False


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

    async def test_profile_backed_estimate_reaches_the_graph_end_to_end(
        self, session_manager,
    ):
        """§12 A2:估算依据**真的**从装配线流到了 state。

        sqlite 有 adapter 但**没有 EXPLAIN 解析器**(``row_guard._PARSERS`` 只覆盖
        postgres/duckdb/mysql/doris)→ 降级链退到第 2 档元数据画像 → 拿到
        ``students`` 的行数 → 干净放行,**不加 LIMIT**。

        这条跑的是**整图**,不是节点单测。单测能证明节点会记,只有整图能证明
        ``graphs._build_budget`` 把画像服务接上了 —— 而且这里断言的是
        ``source == "metadata"``:它同时证明了 **表名从 SQL 解出来了**、
        **画像查到了**、**适配器的行数传下来了** 三件事,比只断言「记了一笔」
        强得多。

        ⚠️ P2 之前这条断言的是相反的结果(``conservative`` + ``degraded=true``
        + ``limit_applied=1000``):那时没有第 2 档,这两个方言**每条查询**都落
        保守预算,``degraded`` 恒为真以至于失去信号意义。那条路径今天仍然存在
        ——「一个表名都解不出来」或「适配器报不出画像」时就会走到,
        由 ``tests/workflow/test_execute_sql_budget.py::TestDegradedExecution``
        与 ``TestMetadataTier::test_unparseable_sql_falls_through_to_conservative``
        覆盖。
        """
        session = await session_manager.start_session(project_cwd="/tmp/p1")
        state = await session_manager.ask(
            session=session,
            question="What students are in Alameda county?",
            workflow_name="reflection",
        )
        ev = state.execution_evidence
        assert ev is not None, "整图路径没装配成本轨 —— 接线断了"
        assert ev["source"] == "metadata"
        assert ev["degraded"] is False
        assert ev["limit_applied"] is None
        assert ev["estimated_rows"] and ev["estimated_rows"] > 0
        # 展示给用户的 SQL 仍是生成的那条:没降级就不该有任何改写
        assert state.sql == "SELECT name FROM students;"

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
        # 侧边栏扩展字段透传:改写痕迹(无改写为空)与数据源名(未指定时空)
        assert done_event["summary"]["rewritten_question"] == ""
        assert "datasource" in done_event["summary"]

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

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**(state if isinstance(state, dict) else state.model_dump()), "final_response": "answer"}

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
        )
        session = await manager.start_session(project_cwd="/tmp/p")

        await manager.ask(session=session, question="第一问")
        await manager.ask(session=session, question="第二问")

        assert captured[0]["history"] == ""  # 第一轮无历史
        assert "第一问" in captured[1]["history"]
        assert "answer" in captured[1]["history"]  # 含上一轮答案
        assert "第二问" not in captured[1]["history"]  # 当前问题不混入历史


class TestStructuredSteps:
    def _manager(self, tmp_home, sqlite_registry, responses,
                 multi_candidate=False, **build_kwargs):
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
            graphs=build_graphs(services, multi_candidate=multi_candidate,
                                **build_kwargs),
            llm_gateway=llm,
        )

    async def test_steps_are_numbered_and_timed(self, tmp_home, sqlite_registry):
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query", "```sql\nSELECT name FROM students;\n```", "OK"],
            query_sketch=False,
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
            query_sketch=False,
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

    async def test_step_details_carry_extended_artifacts(self, tmp_home, sqlite_registry):
        """侧边栏扩展字段:意图证据/规则链/投票/编译决策透传。"""
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query", "```sql\nSELECT name FROM students;\n```", "OK"],
            query_sketch=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        steps = {}
        async for event in manager.ask_stream(
            session=session, question="What students are in Alameda county?",
        ):
            if event["type"] == "step":
                steps[event["node"]] = event

        # route_intent: 证据链完整透传
        ev = steps["route_intent"]["detail"]["intent_evidence"]
        assert isinstance(ev, dict)
        assert "llm_verdict" in ev
        # validate: 确定性规则链全过信号
        assert steps["validate"]["detail"]["rules_passed"] is True
        # reflect: 重试计数与强制标记
        assert steps["reflect"]["detail"]["retry_count"] == 0
        assert steps["reflect"]["detail"]["forced"] is False
        # chart: 判定来源透传(本环境确定性回退)
        assert steps["chart"]["detail"]["chart_source"] == "deterministic"

    def test_step_event_carries_validator_hits(self):
        """validate 步的 detail 要带 **org validator 通道**,与 ``validation_hits``
        并列 —— 那条是规则链(eval 归因的判据),这条是管理员的质检信号。

        它是"判不了"唯一能被读到的地方(用户屏幕按设计不收 ``verdict: None``)。
        没有它,P3/P4 的质检统计只能回去翻 run log 的文本。``reason`` 必须一起
        透传:数分布靠原因码,不靠判词。
        """
        from trove.agent.session import SessionManager

        hit = {"name": "credit-guard", "verdict": None, "severity": "advisory",
               "reason": "truncated_rows", "message": "判不了", "mode": "deterministic"}
        detail = SessionManager._step_event(
            1, "validate", {"rules_passed": True, "validator_hits": [hit]},
            12, "", 0,
        )["detail"]
        assert detail["validator_hits"] == [hit]

        # 未接 SkillService 的图不带该键 —— 形状与 validation_hits 一致,总是列表
        empty = SessionManager._step_event(
            1, "validate", {"rules_passed": True}, 1, "", 0,
        )["detail"]
        assert empty["validator_hits"] == []

    async def test_correction_step_marks_retry(self, tmp_home, sqlite_registry):
        manager = self._manager(
            tmp_home, sqlite_registry,
            [
                "query",
                "```sql\nSELECT name FROM students;\n```",
                # 校验失败后 analyze_error 真的会问一次 LLM,脚本必须给它一轮。
                # 旧脚本漏了这轮:它拿到的下一句是 gen 的第二轮响应,凑成两个
                # gen step 靠的是散文("OK")被当 SQL 接受 —— 那个口子已被
                # gen 收尾解析门槛关上,脚本按真实调用序补齐。
                "TARGET: gen_sql\n修正: 按问题改为计数查询",
                "```sql\nSELECT COUNT(*) FROM students;\n```",
                "OK",
            ],
            query_sketch=False,
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


class TestHistoryPersistence:
    """对话历史补存(方案 ①②⑤):步骤/书签落盘 + resume 跨重启重建。

    恢复端(前端 ``restoreTurns``)与直播端读的是**同一形状** —— 落盘的就是
    SSE 事件原样(见 ``trove/agent/step_history.py`` 模块注释)。
    """

    _Q = "What students are in Alameda county?"
    _SQL = "```sql\nSELECT name FROM students;\n```"

    @staticmethod
    def _rig(tmp_home, sqlite_registry, responses, *, hitl=False, **cfg):
        from langgraph.checkpoint.memory import InMemorySaver

        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs

        class Scripted:
            def __init__(self):
                self._it = iter(responses)

            async def chat(self, model, messages, **kwargs):
                return next(self._it)

            async def chat_full(self, model, messages, tools=None, **kwargs):
                return {"content": next(self._it), "tool_calls": []}

        config = AgentConfig(home=str(tmp_home), target="mock/model", hitl=hitl, **cfg)
        llm = Scripted()
        graphs = build_graphs(
            GraphServices(
                llm=llm, connectors=sqlite_registry,
                semantic_layer=getattr(sqlite_registry, "_test_semantic_provider", None),
                config=config,
            ),
            checkpointer=InMemorySaver(),
            multi_candidate=False, query_sketch=False, agentic=False,
        )
        manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=graphs,
            llm_gateway=llm,
        )
        return manager, llm

    @staticmethod
    async def _drain(stream):
        return [ev async for ev in stream]

    async def test_stream_persists_steps_in_message_metadata(self, tmp_home, sqlite_registry):
        """跑完一问 → assistant 消息 metadata.steps = SSE step 事件(值相等)。

        直播端从 SSE 收步骤、恢复端从 metadata 读步骤,两边喂给同一个
        事件→卡片映射 —— 形状漂移会让历史卡片和直播卡片长得不一样,
        所以这里断言的是**逐字段相等**,不是"差不多"。
        """
        manager, _ = self._rig(
            tmp_home, sqlite_registry, ["query", self._SQL, "OK"],
        )
        try:
            session = await manager.start_session(project_cwd="/tmp/p")
            streamed = await self._drain(
                manager.ask_stream(session=session, question=self._Q)
            )
            sse_steps = [e for e in streamed if e["type"] == "step"]
            assert sse_steps

            stored = await manager.load_session(session.session_id, "/tmp/p")
            last = stored.messages[-1]
            assert last.role == "assistant"
            assert last.metadata["steps"] == sse_steps
            assert last.metadata["steps_truncated"] == 0
        finally:
            await manager.dispose()

    async def test_cache_hit_persists_no_steps(self, tmp_home, sqlite_registry):
        """缓存命中没跑图 → 无步骤可落,metadata 不带 steps 键(干净缺席)。"""
        manager, _ = self._rig(
            tmp_home, sqlite_registry, ["query", self._SQL, "OK"],
            result_cache=True,
        )
        try:
            session = await manager.start_session(project_cwd="/tmp/p")
            await self._drain(manager.ask_stream(session=session, question=self._Q))
            # 第二问命中结果缓存(脚本已耗尽,再问 LLM 会炸)
            await self._drain(manager.ask_stream(session=session, question=self._Q))

            stored = await manager.load_session(session.session_id, "/tmp/p")
            assert [m.role for m in stored.messages] == [
                "user", "assistant", "user", "assistant",
            ]
            assert stored.messages[1].metadata["steps"]
            assert "steps" not in stored.messages[3].metadata
            assert "steps_truncated" not in stored.messages[3].metadata
        finally:
            await manager.dispose()

    async def test_steps_truncation_lands_in_metadata(
        self, tmp_home, sqlite_registry, monkeypatch,
    ):
        """裁剪触顶 → steps_truncated 随 metadata 落盘(前端据此注记
        「其后 N 步未随历史保存」)。上限数值本身由 test_step_history 钉死。"""
        import trove.agent.session as session_mod
        from trove.agent.step_history import trim_steps as real_trim

        monkeypatch.setattr(
            session_mod, "trim_steps",
            lambda steps: real_trim(steps, max_steps=1),
        )
        manager, _ = self._rig(
            tmp_home, sqlite_registry, ["query", self._SQL, "OK"],
        )
        try:
            session = await manager.start_session(project_cwd="/tmp/p")
            streamed = await self._drain(
                manager.ask_stream(session=session, question=self._Q)
            )
            sse_steps = [e for e in streamed if e["type"] == "step"]
            assert len(sse_steps) > 1

            stored = await manager.load_session(session.session_id, "/tmp/p")
            meta = stored.messages[-1].metadata
            assert len(meta["steps"]) == 1
            assert meta["steps_truncated"] == len(sse_steps) - 1
        finally:
            await manager.dispose()

    async def test_interrupt_persists_pending_bookmark(self, tmp_home, sqlite_registry):
        """HITL 中断落「等待确认」assistant 消息(②)+ 提问(⑤)。

        书签是**中断时的事实快照**:status 恒为 pending(消息表 append-only,
        无更新通道);失效规则 = 「只认最后一条」—— 答案消息落在其后即失效。
        """
        manager, _ = self._rig(
            tmp_home, sqlite_registry, ["query", self._SQL], hitl=True,
        )
        try:
            session = await manager.start_session(project_cwd="/tmp/p")
            final = await manager.ask(session=session, question=self._Q)
            assert final.hitl_status == "pending"

            stored = await manager.load_session(session.session_id, "/tmp/p")
            assert [m.role for m in stored.messages] == ["user", "assistant"]
            assert stored.messages[0].content == self._Q
            meta = stored.messages[1].metadata
            assert meta["hitl"]["status"] == "pending"
            assert meta["hitl"]["run_id"] == final.run_id
            assert meta["hitl"]["workflow"] == "reflection"
            assert "SELECT name FROM students;" in " ".join(meta["sql"].split())
            assert meta["summary"]["hitl_status"] == "pending"
        finally:
            await manager.dispose()

    async def test_resume_rebuilds_bookmark_across_restart(self, tmp_home, sqlite_registry):
        """清空 _pending_runs(模拟服务重启)→ resume 从 store 末条重建书签并跑通。

        图状态本身在 checkpointer(thread = session_id)—— 重建后 resume 继续
        同一线程;跑完的答案消息落在书签之后,书签自然失效。
        """
        manager, _ = self._rig(
            tmp_home, sqlite_registry, ["query", self._SQL, "OK"], hitl=True,
        )
        try:
            session = await manager.start_session(project_cwd="/tmp/p")
            first = await self._drain(
                manager.ask_stream(session=session, question=self._Q)
            )
            assert first[-1]["type"] == "hitl"
            assert session.session_id in manager._pending_runs

            manager._pending_runs.clear()  # 重启:进程内簿记丢失
            loaded = await manager.load_session(session.session_id, "/tmp/p")
            resumed = await self._drain(manager.resume_stream(loaded, "yes"))
            assert resumed[-1]["type"] == "done"

            stored = await manager.load_session(session.session_id, "/tmp/p")
            assert [m.role for m in stored.messages] == [
                "user", "assistant", "assistant",
            ]
            assert stored.messages[-2].metadata["hitl"]["status"] == "pending"
            assert stored.messages[-1].metadata["row_count"] == 5
        finally:
            await manager.dispose()

    async def test_new_question_drops_stale_bookmark(self, tmp_home, sqlite_registry):
        """开新 run 丢弃进程内残留书签(失效规则的服务端一半)—— 中断后直接
        重问,旧的 resume 响亮报错而不是续错线程。"""
        manager, _ = self._rig(
            tmp_home, sqlite_registry,
            ["query", self._SQL, "query", self._SQL],  # 两问各自 route + gen
            hitl=True,
        )
        try:
            session = await manager.start_session(project_cwd="/tmp/p")
            first = await self._drain(
                manager.ask_stream(session=session, question=self._Q)
            )
            assert first[-1]["type"] == "hitl"
            assert session.session_id in manager._pending_runs

            # 不确认,直接重问:新 run 起始丢弃残留书签(这一问同样暂停)
            second = await self._drain(
                manager.ask_stream(session=session, question="how many students?")
            )
            assert second[-1]["type"] == "hitl"

            stored = await manager.load_session(session.session_id, "/tmp/p")
            bookmarks = [
                m.metadata["hitl"] for m in stored.messages
                if m.role == "assistant" and (m.metadata or {}).get("hitl")
            ]
            assert len(bookmarks) == 2  # append-only:两枚书签都在历史里
            assert bookmarks[0]["run_id"] != bookmarks[1]["run_id"]
            # 进程内簿记指向新书签 —— 旧书签不再可 resume
            assert manager._pending_runs[session.session_id]["run_id"] == \
                bookmarks[1]["run_id"]
        finally:
            await manager.dispose()

    async def test_resume_without_bookmark_is_loud_error(self, tmp_home, sqlite_registry):
        """没有待确认(无书签/已被消费)→ 响亮报错,不静默续一条不存在的线程。"""
        manager, _ = self._rig(tmp_home, sqlite_registry, [], hitl=True)
        try:
            session = await manager.start_session(project_cwd="/tmp/p")
            events = await self._drain(manager.resume_stream(session, "yes"))
            assert len(events) == 1
            assert events[0]["type"] == "error"
            assert events[0]["node"] == "hitl"
            assert (
                "没有待确认" in events[0]["content"]
                or "Nothing is pending" in events[0]["content"]
            )
        finally:
            await manager.dispose()


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
        """query_sketch 计划与 reflect 裁决作为轨迹事件实时可见。"""
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query", "plan: use students, group by county", "```sql\nSELECT name FROM students;\n```", "OK"],
            query_sketch=True,
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
            query_sketch=False,
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
        # 只准说过程事实。判分那半句(删掉的「——本答案置信度低」)已由
        # confidence.py 的票率折损接管:同一件事不两处说,而且两处会打架
        # —— 票率只是若干折扣之一,折完的分数可能并不低。
        assert "置信度" not in correction["content"]

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
        assert "confidence" not in correction["content"].lower()


class TestHistorySummaryFusion:
    async def test_history_prefers_summary_over_old_turns(self, tmp_home):
        """有 compaction summary 时:历史 = 摘要 + 最近 max_turns 轮原文(分层)。

        早期轮次被摘要承载并折叠出窗口;近若干轮保持逐字(比旧的只留
        1 轮更宽,支撑更长跨度的 follow-up 指代)。
        """
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**(state if isinstance(state, dict) else state.model_dump()), "final_response": "answer"}

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        session.summary = "早期摘要：用户关心贷款数据"
        session.messages = [
            Message(role="user", content="旧问题0"),
            Message(role="assistant", content="旧答案0"),
            Message(role="user", content="旧问题1"),
            Message(role="assistant", content="旧答案1"),
            Message(role="user", content="旧问题2"),
            Message(role="assistant", content="旧答案2"),
            Message(role="user", content="旧问题3"),
            Message(role="assistant", content="旧答案3"),
            Message(role="user", content="旧问题4"),
            Message(role="assistant", content="旧答案4"),
        ]
        await manager.ask(session=session, question="新问题")

        history = captured[0]["history"]
        assert "早期摘要" in history
        assert "旧答案4" in history          # 最近轮保留原文
        assert "旧答案3" in history          # 近 4 轮都在原文窗口内(分层)
        assert "旧答案1" in history
        assert "旧答案0" not in history      # 最早一轮由摘要承载并折叠出窗口
        assert "新问题" not in history

    async def test_history_without_summary_keeps_recent_turns(self, tmp_home):
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**(state if isinstance(state, dict) else state.model_dump()), "final_response": "answer"}

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
        assert "旧问题1" in captured[0]["history"]  # 无摘要 → 保留全部轮次


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
                    **(state if isinstance(state, dict) else state.model_dump()),
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


class TestCandidateCapture:
    """软 MISS → 确定性语义候选(pending)经 _record_exchange 收尾漏斗落库。"""

    @staticmethod
    def _kb_with_model(tmp_home, datasource: str):
        import yaml

        from trove.services.kb.service import KbService

        kb = KbService(tmp_home / "proj")
        ds_dir = kb.kb_dir / datasource
        ds_dir.mkdir(parents=True)
        (ds_dir / "semantics.yml").write_text(
            yaml.safe_dump({
                "version": "0.2.0.dev0",
                "semantic_model": [{
                    "name": datasource,
                    "datasets": [{
                        "name": "loan", "source": "loan",
                        "primary_key": ["loan_id"],
                        "fields": [{
                            "name": "amount", "datatype": "Decimal",
                            "expression": {"dialects": [{
                                "dialect": "ANSI_SQL",
                                "expression": "amount"}]}}],
                    }],
                    "metrics": [],
                }],
            }, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        return kb

    async def _ask(self, tmp_home, sqlite_registry, extra: dict):
        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore

        ds = sqlite_registry.default_name
        kb = self._kb_with_model(tmp_home, ds)

        class StubGraph:
            async def ainvoke(self, state, config=None):
                return {
                    **(state if isinstance(state, dict) else state.model_dump()),
                    "sql": "SELECT 1", "error": "", "final_response": "answer",
                    **extra,
                }

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": StubGraph()},
            llm_gateway=None,
            kb=kb,
            connectors=sqlite_registry,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        await manager.ask(session=session, question="贷款总额")
        return kb, ds

    async def test_soft_miss_lands_pending_candidate(self, tmp_home, sqlite_registry):
        from trove.services.semantic_layer.manage import SemanticManager

        kb, ds = await self._ask(tmp_home, sqlite_registry, {
            "compile_partial": True,
            "compile_misses": [
                {"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
        })
        pending = SemanticManager(kb).drafts(ds)["pending"]
        assert [d["name"] for d in pending] == ["avg_amount"]
        assert pending[0]["status"] == "pending"

    async def test_refusal_run_is_not_captured(self, tmp_home, sqlite_registry):
        """refusal 路径归 refuse 节点(带 LLM 草稿与验证门),捕获口不重复建。"""
        from trove.services.semantic_layer.manage import SemanticManager

        kb, ds = await self._ask(tmp_home, sqlite_registry, {
            "compile_partial": True,
            "compile_misses": [
                {"reason": "no_metric_match", "component": "AVG(loan.amount)"}],
            "refusal": {"reason": "uncovered", "question": "贷款总额"},
        })
        assert SemanticManager(kb).drafts(ds)["pending"] == []


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
                return {**(state if isinstance(state, dict) else state.model_dump()), "final_response": "answer"}

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
                return {**(state if isinstance(state, dict) else state.model_dump()), "final_response": "answer"}

        manager = await self._manager(tmp_home, StubGraph())
        session = await manager.start_session(project_cwd="/tmp/p")
        # ~1M chars >> 128k*0.9 → triggers should_compact
        session.messages = [Message(role="user", content="word " * 12000) for _ in range(10)]
        await manager.ask(session=session, question="新问题")

        assert "SUMMARY" in captured[0]["history"]  # 自动压缩摘要进入历史
        assert session.summary == "SUMMARY"
        assert session.messages[0].role == "system"  # 摘要消息已持久化

    async def test_ask_skips_compact_when_short(self, tmp_home):
        captured = []

        class StubGraph:
            async def ainvoke(self, state, config=None):
                captured.append(state)
                return {**(state if isinstance(state, dict) else state.model_dump()), "final_response": "answer"}

        manager = await self._manager(tmp_home, StubGraph())
        session = await manager.start_session(project_cwd="/tmp/p")
        session.messages = [Message(role="user", content="short")]
        await manager.ask(session=session, question="新问题")

        assert "SUMMARY" not in captured[0]["history"]
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

        assert "SUMMARY" in captured[0]["history"]
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
        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), query_sketch=False)
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
        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), query_sketch=False)
        session = await manager.start_session(project_cwd="/tmp/p")
        first = await manager.ask(session=session, question=self.Q)
        calls = llm.calls
        second = await manager.ask(session=session, question="  WHAT STUDENTS, ARE IN alameda county?? ")
        assert second.sql == first.sql
        assert llm.calls == calls

    async def test_session_isolation(self, tmp_home, sqlite_registry):
        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), query_sketch=False)
        s1 = await manager.start_session(project_cwd="/tmp/p")
        s2 = await manager.start_session(project_cwd="/tmp/p")
        await manager.ask(session=s1, question=self.Q)
        calls = llm.calls
        await manager.ask(session=s2, question=self.Q)  # 不同会话 → 未命中
        assert llm.calls > calls

    async def test_datasource_isolation(self, tmp_home, sqlite_registry):
        """键含数据源分量:默认数据源变化后同问句不再命中。"""
        from trove.core.types import DatasourceConfig

        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), query_sketch=False)
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

        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), query_sketch=False)
        session = await manager.start_session(project_cwd="/tmp/p")
        await manager.ask(session=session, question=self.Q)
        calls = llm.calls
        # 键里含「以谁的身份」(本条无 auth → 本机管理员),测试不手写键,
        # 直接取那一条 —— 意图是「把这条缓存变老」,不是复述键的形状。
        key = next(iter(manager._result_cache))
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
            query_sketch=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        first = await manager.ask(session=session, question=self.Q)
        assert first.error
        assert manager._result_cache == {}  # 错误不写
        await manager.ask(session=session, question=self.Q)  # 再问仍重跑
        assert llm.calls > 3

    async def test_stream_hit_marks_cached(self, tmp_home, sqlite_registry):
        """流式命中:事件形状与实跑一致(sql → result → done),summary 带 cached 标记。"""
        manager, llm = self._manager(tmp_home, sqlite_registry, list(self.RESPONSES), query_sketch=False)
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
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.agent.session import SessionManager

        invoked = []

        class HitlGraph:
            async def ainvoke(self, state, config=None):
                invoked.append(state.question)  # 命中时绝不应被调
                return {**(state if isinstance(state, dict) else state.model_dump()), "sql": "SELECT 1", "row_count": 1,
                        "verdict": "OK", "final_response": "answer",
                        "__interrupt__": [type("I", (), {"value": {"kind": "confirm_sql"}})()]}

        # store 必须 dispose:aiosqlite 的 worker 是**非 daemon 线程**,连接
        # 不关就活到解释器退出,而 CPython 关停时会一直等它 —— 表现为
        # 「测试跑完了,进程不退出」。本用例自建 store(不走 fixture),所以
        # 这份清理得自己写;图一旦真跑过(未命中),检查点连接会让它必现。
        store = SessionStore(home_dir=str(tmp_home))
        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home), result_cache=True, hitl=True),
            session_store=store,
            graphs={"reflection": HitlGraph()},
            llm_gateway=None,
        )
        try:
            session = await manager.start_session(project_cwd="/tmp/p")
            q = "How many students?"
            # 首次运行已人工确认过 → 结果在缓存里。走**生产写门**写进去,不手写
            # 键与 summary 形状:手写意味着测试复述一遍实现(键里有哪些分量、
            # summary 该带哪些字段),实现一改测试就假红,而它想验的是「命中不
            # 走图」。
            #
            # ``principal`` 要与本 manager 解析出来的那个一致 —— 键里带主体
            # (P5 加的视图分量),本 manager 无 auth → 解析结果是本机管理员。
            # 不填就是 None(空主体),写进去的键与查找时的键对不上,测试会从
            # 「命中」变成「实跑」,而那正是它要排除的路径。
            from trove.services.authz.policy import Policy, principal_to_wire
            from trove.workflow.state import WorkflowState

            seeded = WorkflowState(
                session_id=session.session_id, question=q, user_id=session.user_id,
                sql="SELECT 1", row_count=1, verdict="OK", dialect="sqlite",
                final_response="answer",
                principal=principal_to_wire(Policy.local_admin()),
            )
            manager._maybe_cache_exchange(session, seeded)
            assert manager._result_cache  # 种子里没有 → 下面「未命中」就白验了
            final = await manager.ask(session=session, question=q)
            assert final.sql == "SELECT 1"
            assert final.verdict == "OK"
            assert not invoked  # 图从未执行 → HITL 中断未触发
            assert session.messages[-1].role == "assistant"  # 交换照常记录
        finally:
            await store.dispose()


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


class TestCrossTurnStateReset:
    """回归:同会话跨轮不得残留上一轮的 refusal/no_model(带 checkpointer)。

    旧 bug:SessionManager 以 pydantic WorkflowState 作为图输入时,None 默认
    值不覆盖旧 checkpoint 通道,导致上一轮 refusal 跨轮残留——第二轮语义
    匹配成功仍被短路到 refuse。修复:传全量 state dict。
    """

    async def test_no_refusal_leak_between_turns(self, tmp_home):
        from langgraph.checkpoint.memory import MemorySaver

        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.services.kb.service import TermHit

        class LLM:
            async def chat(self, model, messages, **kw):
                return "query"

            async def chat_full(self, model, messages, tools=None, **kw):
                return {"content": "query", "tool_calls": []}

        class FakeDataset:
            name = "loan"
            source = "loan"
            fields = []
            synonyms = []
            description = "loan table"
            primary_key = []

        class FakeMetric:
            name = "average_loan_count_per_year"
            expression = "AVG(COUNT(loan.loan_id))"
            synonyms = ["每年平均贷款数量"]
            datasets = ["loan"]
            definition = ""
            metric_type = ""
            filter = ""
            agg_time_dimension = ""
            non_additive = False

        class FakeModel:
            name = "financial"
            datasets = [FakeDataset()]
            metrics = [FakeMetric()]
            relationships = []
            instructions = ""
            description = ""

        class ToggleLayer:
            def __init__(self):
                self.model_val = None

            enabled = True

            def model(self):
                return self.model_val

            def terms_for(self, question, tables=None, all_tables=None):
                if self.model_val is None:
                    return []
                return [TermHit(
                    term="average_loan_count_per_year",
                    aliases=["每年平均贷款数量"],
                    mapping=self.model_val.metrics[0].expression,
                    tables=["loan"],
                )]

            def field_hits(self, *a, **k):
                return []

        layer = ToggleLayer()
        services = GraphServices(
            llm=LLM(), catalog=None, connectors=None,
            config=AgentConfig(home=str(tmp_home)), kb=None,
            semantic_layer=layer, user_facts=None, lineage=None,
        )
        graphs = build_graphs(services, checkpointer=MemorySaver())

        manager = SessionManager(
            config=AgentConfig(home=str(tmp_home)),
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=graphs,
            llm_gateway=None,
        )
        session = await manager.start_session(project_cwd="/tmp/p")

        layer.model_val = None
        final1 = await manager.ask(session=session, question="zzz-uncovered", datasource="financial")
        assert final1.no_model is True
        assert final1.refusal is not None

        layer.model_val = FakeModel()
        final2 = await manager.ask(session=session, question="每年平均贷款数量", datasource="financial")
        assert final2.no_model is False
        assert final2.refusal is None, (
            f"turn2 不应携带上轮 refusal: {final2.refusal}"
        )
        assert "loan" in final2.matched_tables

    async def test_no_scored_answer_leak_between_turns(self, tmp_home, sqlite_registry):
        """同一条不变量的反面:第 1 轮真答案 → 第 2 轮拒绝,分数不得跟过来。

        ``output`` 的三处早退(clarification / intent_answer / error)都**不写**
        两个分数键 ⟹ ``confidence == 0.0`` 的含义「没有可披露的答案」,完全靠
        ``SessionManager.ask`` 每轮送一份**全新**的 ``WorkflowState``(整份
        ``state.model_dump()`` 才能把上一轮 checkpoint 里那些通道一起盖掉)。
        状态一旦跨轮复用,第 2 轮的澄清会渲染上一轮答案的分数,而没有用例会
        发现 —— 上面那条盯的是 refusal,这条盯的是分数。

        **两半都要真**:第 1 轮必须真的交付一条被打分的答案(``confidence > 0``
        且披露行真的渲染出来)。少了这半,第 2 轮的断言在一个从不打分的链路上
        也成立,整条用例什么都没证明。

        装配借 ``TestStructuredSteps._manager``(``sqlite_registry`` 真执行),
        只把语义层换成可开关的薄包装:``connectors=None`` 的那套装配走不到交付。
        ``multi_candidate=False`` 与它一致 —— 默认 ``True`` 会多跑 4 个备选
        子图,脚本就不够用了。
        """
        from trove.core.config import AgentConfig
        from trove.services.datasource.catalog import CatalogService
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager

        class ToggleLayer:
            """真 provider 的薄包装:只让 ``model()`` 可开关,其余原样委托。"""

            def __init__(self, inner):
                self._inner = inner
                self.on = True

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def model(self):
                return self._inner.model() if self.on else None

        class Scripted:
            def __init__(self, responses):
                self._it = iter(responses)

            async def chat(self, model, messages, **kwargs):
                return next(self._it)

            async def chat_full(self, model, messages, tools=None, **kwargs):
                return {"content": next(self._it), "tool_calls": []}

        layer = ToggleLayer(sqlite_registry._test_semantic_provider)
        config = AgentConfig(home=str(tmp_home), target="mock/model")
        llm = Scripted(["query", "```sql\nSELECT name FROM students;\n```", "OK"])
        services = GraphServices(
            llm=llm, catalog=CatalogService(sqlite_registry),
            connectors=sqlite_registry, semantic_layer=layer, config=config,
        )
        manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=build_graphs(services, multi_candidate=False, query_sketch=False),
            llm_gateway=llm,
        )
        session = await manager.start_session(project_cwd="/tmp/p")

        # 第 1 轮:语义层在位,链路一路跑到交付 —— 一条真被打分的答案。
        layer.on = True
        final1 = await manager.ask(
            session=session, question="What students are in Alameda county?")
        assert final1.confidence > 0, final1.final_response
        assert "置信度" in final1.final_response

        # 第 2 轮:同一会话摘掉语义层 → 拒绝。上一轮的分数不得跟过来。
        layer.on = False
        final2 = await manager.ask(session=session, question="zzz-whatever")
        assert final2.no_model is True
        assert final2.refusal is not None
        assert final2.confidence == 0.0, (
            f"第 2 轮是拒绝,不该带着上一轮的分数: {final2.confidence}"
        )
        assert "置信度" not in final2.final_response


class TestQueryAudit:
    """查询执行审计:谁、问了什么、执行了什么 SQL、结果如何 → audit_log。"""

    def _manager_with_auth(self, auth):
        from trove.agent.session import SessionManager

        return SessionManager(None, None, None, None, auth=auth)

    async def test_audit_records_entry_with_user(self):
        class _Store:
            async def get_user_by_id(self, uid):
                return {"id": 7, "username": "bob"}

        class _Auth:
            def __init__(self):
                self.entries = []
                self.store = _Store()

            async def record_audit(self, action, user=None, method="",
                                   path="", status=None, details=None):
                self.entries.append({"action": action, "user": user, "details": details})

        auth = _Auth()
        manager = self._manager_with_auth(auth)
        final = WorkflowState(
            session_id="s1", run_id="r1", user_id="7",
            question="平均成绩?", sql="SELECT AVG(grade) FROM students",
            datasource="demo", verdict="OK", row_count=1, execution_time_ms=1.5,
        )
        session = type("S", (), {"user_id": "7"})()

        await manager._audit_query(session, final)

        assert len(auth.entries) == 1
        entry = auth.entries[0]
        assert entry["action"] == "query.execute"
        assert entry["user"] == {"id": 7, "username": "bob"}
        d = entry["details"]
        assert d["question"] == "平均成绩?"
        assert d["sql"] == "SELECT AVG(grade) FROM students"
        assert d["datasource"] == "demo"
        assert d["verdict"] == "OK"
        assert d["row_count"] == 1
        assert d["run_id"] == "r1"

    async def test_audit_skipped_without_auth(self):
        manager = self._manager_with_auth(None)
        final = WorkflowState(session_id="s1", question="q")
        await manager._audit_query(type("S", (), {"user_id": "7"})(), final)
        # 无 auth → 不调用任何东西,静默跳过

    async def test_audit_never_blocks_on_auth_failure(self):
        class _Auth:
            store = None

            async def record_audit(self, *a, **k):
                raise RuntimeError("audit db down")

        manager = self._manager_with_auth(_Auth())
        final = WorkflowState(session_id="s1", question="q", user_id="7")
        await manager._audit_query(type("S", (), {"user_id": "7"})(), final)
        # 审计写失败被吞,查询链路不受影响


class TestRefusalInSummary:
    """A1:拒绝出口活在 summary 里,不依赖 steps。

    历史回放(``restoreTurns``)读 ``meta.summary``,而步骤卡是有界裁剪的
    (``MAX_COLLECTED_STEPS``)—— 拒绝轮若只剩 steps 里的 refusal,裁掉后
    动作块就没了。与 ``error_info`` 同层同因。
    """

    def test_summary_carries_refusal_whole(self):
        from trove.agent.session import SessionManager
        from trove.workflow.state import WorkflowState

        refusal = {
            "reason": "no_model", "question": "q", "datasource": "demo",
            "message": "请管理员先 /kb init",
            "next_actions": [{
                "id": "datasource_init", "kind": "datasource_init",
                "label": "去初始化语义模型", "href": "/admin/kb?ds=demo",
                "admin_only": True,
            }],
        }
        state = WorkflowState(session_id="s1", question="q", refusal=refusal)
        assert SessionManager._state_summary(state)["refusal"] == refusal
        # 非拒绝轮 = None(三态:「没有拒绝」不冒充空 dict)
        plain = WorkflowState(session_id="s1", question="q")
        assert SessionManager._state_summary(plain)["refusal"] is None


class TestConfidenceInSummary:
    """SSE / 历史回放 / runlog 读的都是 ``_state_summary`` —— 落在这里,
    三个消费方一次全有(设计 §6.3)。

    复用 ``TestStructuredSteps`` 的 ``_manager`` 装配（内含 ``CatalogService`` /
    ``SessionStore`` / ``build_graphs`` / 脚本化 LLM）。

    **故意不写成 ``class TestConfidenceInSummary(TestStructuredSteps)``**：pytest 会把一个
    ``Test*`` 基类的用例在子类里**再收集一遍** —— 那 4 条断言会成对出现在报告里，
    跑两遍、计数翻倍，却没有任何一条多测到了什么。只取装配函数就够了。
    ``_manager`` 直接取基类那个函数：它在子类里被绑定后子类实例坐进它的 ``self``
    （该形参在 ``_manager`` 体内未被使用），其余形参位次不受影响。
    **不要**再套一层 ``staticmethod``：``staticmethod`` 只是不再自动填 ``self``，
    并不改变函数的参数表 —— 套上之后 ``self`` 落空、实参整体左移一格，最后报出
    ``TypeError: ... missing 1 required positional argument: 'responses'``，
    一个与真实原因毫不相干的错。
    """

    _manager = TestStructuredSteps._manager

    async def test_done_event_summary_carries_the_three_fields(
        self, tmp_home, sqlite_registry,
    ):
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query", "```sql\nSELECT name FROM students;\n```", "OK"],
            query_sketch=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        events = []
        async for event in manager.ask_stream(
            session=session, question="What students are in Alameda county?",
        ):
            events.append(event)

        summary = events[-1]["summary"]
        assert events[-1]["type"] == "done"
        assert summary["sql_confidence"] > 0
        assert summary["confidence"] > 0
        assert summary["confidence"] <= summary["sql_confidence"]
        assert isinstance(summary["confidence_evidence"], list)

    async def test_done_event_summary_carries_gen_model(
        self, tmp_home, sqlite_registry,
    ):
        """溯源条要的两行新依据:``model``(gen_sql 实际用的模型,此前全仓
        多处找得到、summary 里没有)与 ``execution_evidence``(数据截止/限额,
        与 output 的新鲜度行同源同读)。"""
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query", "```sql\nSELECT name FROM students;\n```", "OK"],
            query_sketch=False,
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        events = []
        async for event in manager.ask_stream(
            session=session, question="What students are in Alameda county?",
        ):
            events.append(event)

        summary = events[-1]["summary"]
        # AgentConfig(target="mock/model");这条答案真经过 gen_sql ⟹ 报模型名。
        assert summary["model"] == "mock/model"
        assert "execution_evidence" in summary

    def test_gen_model_reports_only_when_generated(self):
        """``_gen_model`` 的诚实口径:没有 SQL、复用(reused/certified)都不报
        模型 —— 把配置里的名字冒充成「这个模型写了这条 SQL」正是披露装置最
        不该说的话。compiled 仍报:编译契约之外,gen_sql 照样跑。"""
        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.workflow.state import WorkflowState

        config = AgentConfig(target="mock/model")
        gen = WorkflowState(session_id="s1", question="q", sql="SELECT 1", answer_source="generated")
        assert SessionManager._gen_model(config, gen) == "mock/model"
        compiled = WorkflowState(session_id="s1", question="q", sql="SELECT 1", answer_source="compiled")
        assert SessionManager._gen_model(config, compiled) == "mock/model"
        assert SessionManager._gen_model(
            config, WorkflowState(session_id="s1", question="q", answer_source="generated"),
        ) == ""
        for source in ("reused", "certified"):
            state = WorkflowState(session_id="s1", question="q", sql="SELECT 1", answer_source=source)
            assert SessionManager._gen_model(config, state) == ""
        # summary 的存与读:不传 = ""(三态里的「没有」),传了原样透出。
        assert SessionManager._state_summary(gen)["model"] == ""
        assert SessionManager._state_summary(gen, "m/x")["model"] == "m/x"

    async def test_select_step_detail_reads_the_nested_vote_share(
        self, tmp_home, sqlite_registry,
    ):
        """``detail['confidence']`` 今天恒为 ``0.0``:它读 ``delta['confidence']``,
        而票率写在 ``delta['selection']['confidence']`` 里(设计 §6.3)。改读嵌套键。

        **注意这不是一个显示 bug。** 实现期核实:渲染 select 置信度的那个 chip
        (``frontend/src/components/chat/StepCard.vue:126``)读的是
        ``view.selection.confidence`` ← ``payload.selection.confidence``,而
        ``get()``(``frontend/src/utils/steps.ts:90-97``)**先查 ``payload.detail``**,
        ``session.py`` 早就把 ``delta["selection"]`` 原样放进 ``detail["selection"]``(那一行按内容找,别按行号 —— 本任务自己的注释已把它往下推过) ——
        票率一直显示正确。恒 ``0.0`` 的是另一个字段,它的前端落点
        ``view.confidence``(``steps.ts`` 里 ``view.confidence = conf`` 那一行)**全仓库无读取方**。
        所以本行要做,但理由是**字段诚实**:它已随 SSE / 历史回放 / runlog
        外发,留着一个恒 ``0.0`` 会误导将来的消费方(前端那个死字段记入 P2)。

        **必须 ``multi_candidate=True`` + 7 条脚本**(本行原稿如此。Task 9 派发前
        曾据「单候选也记票型」把它改短过 —— 那次更正**是错的**,理由见下方)。

        票率恒为 ``confidence == 1.0``,走的是 ``select.py`` 的
        ``if len(ranked) == 1:``(全员一致那一档)。Task 2 正是把这条分支从
        ``return {}`` 改成返回完整的 ``selection`` 增量(``confidence: 1.0``)。

        > **为什么短脚本不够(Task 9 派发前的第二次更正)**:上一次更正的前提是
        > 「单候选也记票型 ⟹ 两种配置落在同一条分支」—— **这个前提是错的**。
        > ``select`` 开头有一句 ``if state.error or state.error_feedback or not
        > state.candidates: return {}``,而 ``candidates`` 只装**备选**池(主候选
        > 那一组是 select 自己从 ``state.sql`` 建的)。默认的
        > ``multi_candidate=False`` ⟹ ``graphs.py`` 里 ``alt_subgraphs = None``
        > ⟹ ``subgraph_alt is None`` ⟹ 池子**永远是空的** ⟹ select 每轮都在第一
        > 句 return,``selection`` **一次都没写过**。
        > 上一版推演说「跑不跑都是 1.0」。**两处都不对,而且是把两条原因叠成了一格**:
        > 池空 ⟹ ``select`` 早退 ``{}`` ⟹ ``delta`` 是空的 ⟹ ``session.py`` 的
        > ``if not delta: continue``(按内容找,在 select 那段处理**之前**)把整条
        > select 步骤跳过 —— 这个配置下**没有 select 步骤事件**,既不是 ``1.0`` 也
        > 不是 ``0.0``,是**根本没有那个 ``detail``**(用例会红在 ``KeyError``)。
        > 那个 ``0.0`` 是另一回事:那句赋值原先是 ``delta.get("confidence", 0.0)``,
        > 而 ``confidence`` 从来只写在 ``selection`` 字典**内部**,顶层没有这个键 ——
        > 所以**只要这一步跑得到**,读出来就是恒定 ``0.0``,与候选跑没跑无关(这才是
        > 本任务要修的那个字段)。**被替换的那一版**把「跑不到」与「读错键」认成了同一条
        > —— 它写的是「不跑就永远是 ``0.0``」(可在 ``git show e2d5331:tests/agent/test_session.py``
        > 上逐字核对)。
        >
        > ``len(ranked) == 1`` 的正确读法是「**主候选与全部备选一致**」,不是
        > 「只有一个候选」;两者只在**有备选**时才是一回事。``select`` 那个早退
        > 守卫还有个副作用值得记住:``graphs.py`` 每轮把 ``candidates`` 清成
        > ``[]``,所以**第 2 轮起 select 基本不投票** —— 同样的错读法在多轮场景
        > 里会得出「第 2 轮的 selection 是本轮的」,而它其实是第 1 轮留下的
        > (Task 5 为此给 ``update`` 补了 ``"selection": {}``)。
        """
        # 脚本给 7 条:意图 + 主候选生成 + 4 个备选生成 + reflect。**4 这个数是从
        # 代码上算出来的**:``graphs.py`` 的 ``alt_subgraphs`` 用
        # ``_candidate_schedule(max(scaling, 1) - 1)``,``scaling`` 默认 5 ⟹ 4 个备选
        # 子图。同形样本在 ``tests/workflow/test_graphs.py``:那里的
        # ``len(llm.calls) == 7`` 断言共**五处**,其中**同形的**两处自带形状注释 ——
        # ``# 意图 + 主 + 4 备 + reflect``(经典支)与
        # ``# 意图 + 主 + 4 subagent + reflect``(agentic 支)。**按注释搜,别按行号。**
        # ``Scripted`` 用 ``next(it)``:**短了是 StopIteration,长了只是没人取**
        # —— 两头不对称,所以按顺序给足。
        #
        # ⚠️ 备选那 4 条**必须与主候选文本不同**。入池前有一道去重:
        # ``key = " ".join(sql.split()).lower()``(折叠空白与大小写),而
        # ``seen`` 的初值是**主候选再加上前几轮的池**(``seen.update(...)`` ——
        # 是个超集,不止主候选) ⟹ ``if key in seen: continue`` 会把与主
        # 候选同文的备选**丢掉**。四条全写成主候选原句,池子会**空着**出这个
        # 节点;``select`` 开头的 ``not state.candidates`` 守卫随即早退 ``{}``,
        # ``session.py`` 又把空 delta 整条跳过(``if not delta: continue``),
        # 于是**连 select 步骤事件都没有**,用例红在 ``KeyError: 'select'`` ——
        # 一个看着像图坏了、其实是用例喂错的症状。限定名写法既可入池、结果集
        # 又与主候选一致:票型落「全员一致」那一档,票率 = 1.0。
        manager = self._manager(
            tmp_home, sqlite_registry,
            ["query",
             "```sql\nSELECT name FROM students;\n```",
             "```sql\nSELECT students.name FROM students;\n```",
             "```sql\nSELECT students.name FROM students;\n```",
             "```sql\nSELECT students.name FROM students;\n```",
             "```sql\nSELECT students.name FROM students;\n```",
             "OK"],
            query_sketch=False,
            multi_candidate=True,   # 没有备选就没有投票 —— 见本用例的 docstring
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        steps = {}
        async for event in manager.ask_stream(
            session=session, question="What students are in Alameda county?",
        ):
            if event["type"] == "step":
                steps[event["node"]] = event

        assert steps["select"]["detail"]["confidence"] == pytest.approx(1.0)
