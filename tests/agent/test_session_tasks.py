"""Task layer tests — decomposition, cross-turn follow-up, HITL batch options.

Three harnesses:
  - StubGraph: scripted graph outcomes (zero LLM in the graph), for
    decomposition / sequence / follow-up flows with exact event asserts.
  - Real graph + InMemorySaver + hitl=True: the HITL three-option batch.
  - Pure helper functions (tasks.py) with no fixtures.
"""

from __future__ import annotations

from tests.conftest import ScriptedGateway

SQL = "```sql\nSELECT name FROM students;\n```"
DECOMPOSE_JSON = '{"tasks": ["学生名单", "平均成绩"]}'
SYNTHESIS_TEXT = "综合回答:名单与成绩见上。"


class _UsageScriptedGateway(ScriptedGateway):
    """ScriptedGateway 变体:每次调用向 token_accounting 记账,驱动 done
    summary 的 token_usage(镜像 test_token_accounting 的 UsageGateway)。"""

    def __init__(self, responses):
        super().__init__(responses)
        self._n = 0

    def _record(self, model, messages, out, metadata):
        self._n += 1
        n = self._n
        from trove.llm.gateway import _record_local_call
        _record_local_call(
            model=model, messages=messages, output=out,
            metadata=metadata, elapsed_ms=10,
            usage={"prompt_tokens": n * 10, "completion_tokens": n * 2, "total_tokens": n * 12},
        )

    async def chat(self, model, messages, **kwargs):
        out = next(self._responses)
        self._record(model, messages, out, kwargs.get("metadata"))
        return out

    async def chat_full(self, model, messages, tools=None, **kwargs):
        out = next(self._responses)
        self._record(model, messages, out, kwargs.get("metadata"))
        return {"content": out, "tool_calls": []}


class StubGraph:
    """Scripted graph: one ``astream`` outcome per task (exact control)."""

    def __init__(self, outcomes):
        self._outcomes = iter(outcomes)
        self.captured = []

    async def astream(self, state, config=None, stream_mode=None):
        self.captured.append(state)
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome

    def run_states(self):
        return list(self.captured)


def _ok_outcome(final_response="答案"):
    return {
        "route_intent": {"intent": "query", "llm": None},
        "schema_linking": {"matched_tables": ["students"]},
        "gen_sql": {"sql": "SELECT name FROM students;", "attempts": 1, "llm": None},
        "execute_sql": {
            "row_count": 5,
            "rows": [[1, "Alice"], [2, "Bob"]],
            "columns": ["id", "name"],
            "execution_time_ms": 1,
        },
        "reflect": {"verdict": "OK", "reason": "", "llm": None},
        "output": {"final_response": final_response},
    }


class _RecordingScriptedGateway(ScriptedGateway):
    """ScriptedGateway 变体:记录每次调用的 (model, messages)。"""

    def __init__(self, responses):
        super().__init__(responses)
        self.calls: list[tuple] = []

    async def chat(self, model, messages, **kwargs):
        self.calls.append((model, messages))
        return await super().chat(model, messages, **kwargs)

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.calls.append((model, messages))
        return await super().chat_full(model, messages, tools=tools, **kwargs)


class _StubManagerHarness:
    """SessionManager over a StubGraph with a scripted LLM gateway."""

    def __init__(self, tmp_home, llm_responses, graph, config_kwargs=None, gateway=None):
        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore

        config = AgentConfig(home=str(tmp_home), language="zh", target="mock/model", **(config_kwargs or {}))
        gateway = gateway or ScriptedGateway(llm_responses)
        self.manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": graph},
            llm_gateway=gateway,
        )
        self.gateway = gateway

    async def session(self):
        return await self.manager.start_session(project_cwd="/tmp/p")

    async def stream(self, session, question):
        return [ev async for ev in self.manager.ask_stream(session=session, question=question)]

    def done_events(self, events):
        return [e for e in events if e["type"] == "done"]

    def task_events(self, events):
        return [e for e in events if e["type"] == "task"]


class TestDecompositionFlow:
    async def test_single_question_no_task_events(self, tmp_home):
        """单问题(不命中规则门)→ 零额外 LLM 调用,无 task 事件。"""
        graph = StubGraph([_ok_outcome()])
        h = _StubManagerHarness(tmp_home, [], graph)
        session = await h.session()

        events = await h.stream(session, "What students are in Alameda county?")
        assert h.task_events(events) == []
        assert not h.done_events(events)[-1]["summary"].get("batched")
        # 图被完整调用(route → gen_sql → reflect),没有多余的拆解/解释调用
        assert len(graph.run_states()) == 1

    async def test_multitask_sequences_tasks_with_closing_event(self, tmp_home):
        """多任务:逐任务 done + task 快照 + 收尾 batched done。"""
        graph = StubGraph([_ok_outcome("名单答案"), _ok_outcome("成绩答案")])
        h = _StubManagerHarness(
            tmp_home,
            [DECOMPOSE_JSON, SYNTHESIS_TEXT],
            graph,
        )
        session = await h.session()

        events = await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")
        dones = h.done_events(events)

        # 3 个 done:两个逐任务 + 一个收尾
        assert len(dones) == 3
        assert "**任务 1/2**" in dones[0]["content"]
        assert "**任务 2/2**" in dones[1]["content"]
        assert dones[2]["summary"]["batched"] is True
        assert "2/2" in dones[2]["content"]
        assert not dones[0]["summary"].get("batched")

        # task 快照:初始全 pending + 每任务 in_progress/终态 = 1 + 2N 个
        snapshots = [e["data"]["tasks"] for e in h.task_events(events)]
        assert len(snapshots) == 5
        assert [t["status"] for t in snapshots[0]] == ["pending", "pending"]
        assert snapshots[1][0]["status"] == "in_progress"
        assert snapshots[-1][0]["status"] == "done"
        assert snapshots[-1][1]["status"] == "done"

        # 图输入:子任务标题 + [tasks] 清单块注入 history + task_context
        states = graph.run_states()
        assert len(states) == 2
        assert states[0].question == "学生名单"
        assert states[1].question == "平均成绩"
        assert "[tasks] 当前任务清单:" in states[1].history
        assert states[0].task_context == {"index": 1, "total": 2, "remaining": 1}
        assert states[1].task_context == {"index": 2, "total": 2, "remaining": 0}

        # 落库消息带任务元数据
        assert all(m.metadata.get("task_id") for m in session.messages if m.role == "assistant")

    async def test_subtask_shares_previous_results(self, tmp_home):
        """步骤间共享:子任务2 的 history 带任务1 的结果包,matched_tables 锚点继承。"""
        graph = StubGraph([_ok_outcome("名单答案"), _ok_outcome("成绩答案")])
        h = _StubManagerHarness(tmp_home, [DECOMPOSE_JSON, SYNTHESIS_TEXT], graph)
        session = await h.session()

        await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")

        states = graph.run_states()
        assert len(states) == 2
        # 任务2 history 注入任务1 的 [previous results] 结果包
        assert "[previous results]" in states[1].history
        assert "学生名单" in states[1].history
        assert "SELECT name FROM students;" in states[1].history
        # matched_tables 锚点继承
        assert states[1].matched_tables == ["students"]
        # 任务1 落库 context 包(供后续步骤/跨轮复用)
        final = await h.manager.get_tasks(session)
        ctx = final[0]["metadata"]["context"]
        assert ctx["title"] == "学生名单"
        assert ctx["sql"] == "SELECT name FROM students;"
        assert ctx["row_count"] == 5
        assert ctx["rows_preview"] == [["1", "Alice"], ["2", "Bob"]]  # 单元格字符串化(截断预算)
        assert ctx["matched_tables"] == ["students"]

    async def test_task_failure_marks_failed_and_continues(self, tmp_home):
        """单任务失败 → failed 状态 + 错误事件,序列继续下一条。"""
        graph = StubGraph([RuntimeError("boom"), _ok_outcome("成绩答案")])
        h = _StubManagerHarness(
            tmp_home,
            [DECOMPOSE_JSON, SYNTHESIS_TEXT],
            graph,
        )
        session = await h.session()

        events = await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")
        types = [e["type"] for e in events]
        assert "error" in types

        final = await h.manager.get_tasks(session)
        assert [t["status"] for t in final] == ["failed", "done"]
        assert final[0]["metadata"]["error"] is not None
        assert h.done_events(events)[-1]["summary"]["batched"] is True

    async def test_judge_false_keeps_single_task_path(self, tmp_home):
        """规则未命中 + 疑似多步 → LLM 判断一次,返回 [] → 单任务路径。"""
        graph = StubGraph([_ok_outcome("直接答案")])
        gw = _RecordingScriptedGateway(['{"tasks": []}'])
        h = _StubManagerHarness(tmp_home, [], graph, gateway=gw)
        session = await h.session()

        question = "各银行的不良贷款情况如何,资产质量怎么样"
        events = await h.stream(session, question)
        assert h.task_events(events) == []
        assert len(h.done_events(events)) == 1
        assert len(graph.run_states()) == 1  # 原始问题直接跑图
        # 恰 1 次 LLM 调用 = 判断层;prompt 带原问题
        assert len(gw.calls) == 1
        assert question in gw.calls[0][1][0]["content"]

    async def test_judge_true_decomposes(self, tmp_home):
        """规则未命中 + judge 返回任务列表 → 走多任务执行。"""
        graph = StubGraph([_ok_outcome("A答"), _ok_outcome("B答")])
        h = _StubManagerHarness(
            tmp_home,
            ['{"tasks": ["任务A", "任务B"]}', SYNTHESIS_TEXT],
            graph,
        )
        session = await h.session()

        events = await h.stream(session, "各银行的不良贷款情况如何,资产质量怎么样")
        dones = h.done_events(events)
        assert len(dones) == 3  # 两个逐任务 + 收尾 batched
        assert dones[-1]["summary"]["batched"] is True
        assert [s.question for s in graph.run_states()] == ["任务A", "任务B"]

    async def test_judge_disabled_keeps_zero_llm(self, tmp_home):
        """decompose_llm_judge=false → 疑似多步也零 LLM,纯正则门控。"""
        graph = StubGraph([_ok_outcome("直接答案")])
        h = _StubManagerHarness(
            tmp_home, [], graph, config_kwargs={"decompose_llm_judge": False},
        )
        session = await h.session()

        events = await h.stream(session, "各银行的不良贷款情况如何,资产质量怎么样")
        assert h.task_events(events) == []
        assert len(graph.run_states()) == 1

    async def test_decompose_garbage_degrades_to_single_task(self, tmp_home):
        """拆解 LLM 返回不可解析内容 → 单任务路径(任务层绝不成为新失败源)。"""
        graph = StubGraph([_ok_outcome("单答案")])
        h = _StubManagerHarness(
            tmp_home,
            ["这不是 JSON"],
            graph,
        )
        session = await h.session()

        events = await h.stream(session, "分别查询学生名单和平均成绩")
        assert h.task_events(events) == []
        dones = h.done_events(events)
        assert len(dones) == 1
        assert dones[0]["content"] == "单答案"
        assert len(graph.run_states()) == 1  # 原始问题直接跑图

    async def test_batch_synthesizes_final_answer(self, tmp_home):
        """批收尾:≥2 任务结束 → 一次 fast 综合,进 batched done 的 final_response。"""
        graph = StubGraph([_ok_outcome("名单答案"), _ok_outcome("成绩答案")])
        gw = _RecordingScriptedGateway([DECOMPOSE_JSON, SYNTHESIS_TEXT])
        h = _StubManagerHarness(tmp_home, [], graph, gateway=gw)
        session = await h.session()

        events = await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")
        last = h.done_events(events)[-1]
        assert last["summary"]["batched"] is True
        assert last["summary"]["final_response"] == SYNTHESIS_TEXT
        # 汇总调用恰一次,输入带各任务标题/状态
        assert len(gw.calls) == 2  # 拆解 + 综合
        assert "学生名单" in gw.calls[1][1][0]["content"]
        assert "平均成绩" in gw.calls[1][1][0]["content"]

    async def test_batch_synthesis_failure_falls_back(self, tmp_home):
        """汇总 LLM 失败 → 不阻塞:summary 无 final_response,逐条答案兜底。"""
        graph = StubGraph([_ok_outcome("名单答案"), _ok_outcome("成绩答案")])
        h = _StubManagerHarness(tmp_home, [DECOMPOSE_JSON], graph)  # 综合调用无响应 → 异常
        session = await h.session()

        events = await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")
        last = h.done_events(events)[-1]
        assert last["summary"]["batched"] is True
        assert "final_response" not in last["summary"]
        assert "2/2" in last["content"]  # 收尾文案仍在

    async def test_partial_failure_still_synthesizes(self, tmp_home):
        """部分失败:综合仍执行,输入含失败任务的状态与错误。"""
        graph = StubGraph([RuntimeError("boom"), _ok_outcome("成绩答案")])
        gw = _RecordingScriptedGateway([DECOMPOSE_JSON, SYNTHESIS_TEXT])
        h = _StubManagerHarness(tmp_home, [], graph, gateway=gw)
        session = await h.session()

        events = await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")
        last = h.done_events(events)[-1]
        assert last["summary"]["batched"] is True
        assert last["summary"]["final_response"] == SYNTHESIS_TEXT
        assert "boom" in gw.calls[1][1][0]["content"]  # 失败任务错误带进综合输入

    async def test_all_failed_no_synthesis(self, tmp_home):
        """全部失败:不调用综合(零额外 LLM)。"""
        graph = StubGraph([RuntimeError("boom"), RuntimeError("boom2")])
        gw = _RecordingScriptedGateway([DECOMPOSE_JSON])
        h = _StubManagerHarness(tmp_home, [], graph, gateway=gw)
        session = await h.session()

        events = await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")
        last = h.done_events(events)[-1]
        assert last["summary"]["batched"] is True
        assert "final_response" not in last["summary"]
        assert len(gw.calls) == 1  # 只有拆解调用


class TestCrossTurnFollowup:
    # 注:continue_next 真正执行 pending 任务的路径由 HITL 三选项测试覆盖
    # (approve 单任务后回复"继续" → 剩余任务执行)。

    async def test_redo_marks_pending_and_runs(self, tmp_home):
        """'重做第 2 个' → 任务2 pending → 重跑 → 成功。"""
        graph = StubGraph([_ok_outcome("名单答案"), _ok_outcome("成绩答案"), _ok_outcome("重做答案")])
        h = _StubManagerHarness(
            tmp_home,
            [DECOMPOSE_JSON, SYNTHESIS_TEXT, '{"action": "redo", "index": 2}'],
            graph,
        )
        session = await h.session()
        await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")

        events = await h.stream(session, "重做第 2 个")
        dones = h.done_events(events)
        assert len(dones) == 2
        assert "**任务 2/2**" in dones[0]["content"]
        assert dones[1]["summary"]["batched"] is True
        assert "重做答案" in dones[0]["content"]

    async def test_skip_marks_skipped_without_batch(self, tmp_home):
        """'跳过第 2 个' → skipped 状态 + 单个 done(无 batched)。"""
        graph = StubGraph([_ok_outcome(), _ok_outcome()])
        h = _StubManagerHarness(
            tmp_home,
            [DECOMPOSE_JSON, SYNTHESIS_TEXT, '{"action": "skip", "index": 2}'],
            graph,
        )
        session = await h.session()
        await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")

        events = await h.stream(session, "跳过第 2 个")
        dones = h.done_events(events)
        assert len(dones) == 1
        assert "已跳过任务 2" in dones[0]["content"]
        assert not (dones[0].get("summary") or {}).get("batched")

        final = await h.manager.get_tasks(session)
        assert [t["status"] for t in final] == ["done", "skipped"]
        assert len(graph.run_states()) == 2  # 没有为 skip 跑图

    async def test_continue_with_no_pending_tells_user(self, tmp_home):
        """全部完成后 '继续' → 提示,不跑图。"""
        graph = StubGraph([_ok_outcome(), _ok_outcome()])
        h = _StubManagerHarness(
            tmp_home,
            [DECOMPOSE_JSON, SYNTHESIS_TEXT, '{"action": "continue_next"}'],
            graph,
        )
        session = await h.session()
        await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")

        events = await h.stream(session, "继续")
        assert "没有待办任务。" in h.done_events(events)[0]["content"]
        assert len(graph.run_states()) == 2  # 未再跑图

    async def test_followup_without_tasks_goes_normal_path(self, tmp_home):
        """无任务清单时 '继续' → 规则门短路,零额外 LLM,走单任务路径。"""
        # 响应恰好只够单任务图(route/gen/reflect);若解释器被调 → StopIteration
        graph = StubGraph([_ok_outcome("直接答案")])
        h = _StubManagerHarness(tmp_home, [], graph)
        session = await h.session()

        events = await h.stream(session, "继续查询所有学生")
        assert h.task_events(events) == []
        assert h.done_events(events)[0]["content"] == "直接答案"

    async def test_add_appends_new_tasks(self, tmp_home):
        """'再加两个问题…' → add:新任务续排 position,不自动执行。"""
        graph = StubGraph([_ok_outcome(), _ok_outcome()])
        h = _StubManagerHarness(
            tmp_home,
            [
                DECOMPOSE_JSON,
                SYNTHESIS_TEXT,
                '{"action": "add"}',
                '{"tasks": ["新任务一", "新任务二"]}',
            ],
            graph,
        )
        session = await h.session()
        await h.stream(session, "分别查询 1. 学生名单 2. 平均成绩")

        events = await h.stream(session, "再加两个问题:1. 新任务一 2. 新任务二")
        done = h.done_events(events)[0]
        assert "已新增 2 个任务" in done["content"]

        final = await h.manager.get_tasks(session)
        assert len(final) == 4
        assert [t["position"] for t in final] == [0, 1, 2, 3]
        assert [t["title"] for t in final[2:]] == ["新任务一", "新任务二"]
        assert [t["status"] for t in final[2:]] == ["pending", "pending"]
        assert len(graph.run_states()) == 2  # add 不跑图


class TestHitlBatchOptions:
    """真图 + InMemorySaver + hitl=True:批内三选项。"""

    def _build(self, tmp_home, sqlite_registry, responses, gateway_cls=ScriptedGateway):
        from langgraph.checkpoint.memory import InMemorySaver

        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs

        config = AgentConfig(home=str(tmp_home), target="mock/model", hitl=True)
        gateway = gateway_cls(responses)
        graphs = build_graphs(
            GraphServices(llm=gateway, connectors=sqlite_registry, config=config),
            checkpointer=InMemorySaver(),
            multi_candidate=False, planner=False, agentic=False,
        )
        return SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs=graphs,
            llm_gateway=gateway,
        )

    @staticmethod
    async def _ask(manager, session, question):
        return [ev async for ev in manager.ask_stream(session=session, question=question)]

    async def test_batch_hitl_three_options_approve_all(self, tmp_home, sqlite_registry):
        """选项2 approve_all:完成被打断任务后 auto_approve 续跑剩余,收尾 batched done。"""
        manager = self._build(
            tmp_home, sqlite_registry,
            [
                DECOMPOSE_JSON,
                "query", SQL,                # 任务1:route → gen_sql → HITL 中断
                "OK",                        # resume:reflect
                "query", SQL, "OK",          # 任务2:auto_approve 全程
                SYNTHESIS_TEXT,              # 批收尾综合
            ],
        )
        session = await manager.start_session(project_cwd="/tmp/p")

        first = await self._ask(manager, session, "分别查询 1. 学生名单 2. 平均成绩")
        assert first[-1]["type"] == "hitl"
        payload = first[-1]["payload"]
        assert payload["kind"] == "confirm_sql"
        assert payload["task_context"]["total"] == 2

        resumed = [ev async for ev in manager.resume_stream(session, "approve_all")]
        types = [e["type"] for e in resumed]
        # 任务1 done → 继续提示 → 任务2 全程 → 收尾 batched done
        assert types.count("done") == 3
        assert resumed[-1]["summary"]["batched"] is True

        final = await manager.get_tasks(session)
        assert [t["status"] for t in final] == ["done", "done"]

    async def test_batch_approve_all_batched_done_carries_stats(self, tmp_home, sqlite_registry):
        """收尾 batched done 汇总各子任务耗时/token;hitl 事件带已累计统计。

        前端据此在确认气泡与收尾摘要上展示 token 数(逐任务 done 只是
        中间事件,汇总只落在最后的 batched done 上)。
        """
        manager = self._build(
            tmp_home, sqlite_registry,
            [
                DECOMPOSE_JSON,
                "query", SQL,            # 任务1:route → gen_sql → HITL 中断
                "OK",                    # resume:reflect
                "query", SQL, "OK",      # 任务2:auto_approve 全程
                SYNTHESIS_TEXT,          # 批收尾综合
            ],
            gateway_cls=_UsageScriptedGateway,
        )
        session = await manager.start_session(project_cwd="/tmp/p")

        first = await self._ask(manager, session, "分别查询 1. 学生名单 2. 平均成绩")
        assert first[-1]["type"] == "hitl"
        # 确认气泡即可展示已累计的耗时与 token(中断时只读不 pop)
        assert first[-1]["summary"]["total_elapsed_ms"] >= 0
        usage = first[-1]["summary"]["token_usage"]
        assert usage and usage["total"] > 0

        resumed = [ev async for ev in manager.resume_stream(session, "approve_all")]
        last = resumed[-1]
        assert last["type"] == "done"
        assert last["summary"]["batched"] is True
        # 聚合 2 个子任务:耗时与 token 都上收尾事件
        assert last["summary"]["total_elapsed_ms"] >= 0
        assert last["summary"]["token_usage"]["total"] > 0
        # 逐任务的中间 done 不带汇总统计(保持只追加语义)
        middles = [e for e in resumed if e.get("type") == "done" and not e["summary"].get("batched")]
        assert middles
        assert all("total_elapsed_ms" in m["summary"] for m in middles)

    async def test_batch_hitl_approve_single_leaves_rest_pending(self, tmp_home, sqlite_registry):
        """选项1 approve:只完成被打断任务,剩余保持 pending,无收尾事件。"""
        manager = self._build(
            tmp_home, sqlite_registry,
            [
                DECOMPOSE_JSON,
                "query", SQL,
                "OK",
                '{"action": "continue_next"}',  # "继续执行剩余任务" 的解释器
                "query", SQL, "OK",             # 任务2 全程
            ],
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        await self._ask(manager, session, "分别查询 1. 学生名单 2. 平均成绩")

        resumed = [ev async for ev in manager.resume_stream(session, "yes")]
        types = [e["type"] for e in resumed]
        assert types.count("done") == 1
        assert not resumed[-1]["summary"].get("batched")

        final = await manager.get_tasks(session)
        assert [t["status"] for t in final] == ["done", "pending"]

        # 提示明确:回复"继续"执行剩余任务 → 跨轮解释器接上;
        # 设计语义:选项1 = 逐个确认 —— 任务2 执行前再次 HITL 确认
        resumed_again = [ev async for ev in manager.ask_stream(
            session=session, question="继续执行剩余任务",
        )]
        assert resumed_again[-1]["type"] == "hitl"

        second = [ev async for ev in manager.resume_stream(session, "yes")]
        assert second[-1]["type"] == "done"
        assert not second[-1]["summary"].get("batched")
        assert [t["status"] for t in await manager.get_tasks(session)] == ["done", "done"]

    async def test_batch_hitl_reject_marks_failed_and_cancels(self, tmp_home, sqlite_registry):
        """选项3 不继续:当前任务 failed(user_cancelled),批终止。"""
        manager = self._build(
            tmp_home, sqlite_registry,
            [
                DECOMPOSE_JSON,
                "query", SQL,
                "OK",
            ],
        )
        session = await manager.start_session(project_cwd="/tmp/p")
        await self._ask(manager, session, "分别查询 1. 学生名单 2. 平均成绩")

        resumed = [ev async for ev in manager.resume_stream(session, "no")]
        types = [e["type"] for e in resumed]
        assert types.count("done") == 1
        assert "取消" in resumed[-1]["content"] or "cancel" in resumed[-1]["content"]

        final = await manager.get_tasks(session)
        assert [t["status"] for t in final] == ["failed", "pending"]
        assert final[0]["metadata"]["user_cancelled"] is True


class TestTaskHelpers:
    """tasks.py 纯函数(无 fixture,零 LLM)。"""

    def test_looks_multitask(self):
        from trove.agent.tasks import looks_multitask

        assert looks_multitask("分别查询 A 和 B")
        assert looks_multitask("依次列出 1. A 2. B")
        assert looks_multitask("先查 A,再查 B")
        assert not looks_multitask("哪个地区的平均贷款金额最高?")
        assert not looks_multitask("继续")

    def test_looks_multitask_strengthened_hints(self):
        """强化正则:隐式多步(及其/对比/TOP/排名/各行业/每个)命中。"""
        from trove.agent.tasks import looks_multitask

        assert looks_multitask("各行业贷款TOP3的银行及其坏账率")
        assert looks_multitask("2024 年各地区贷款总额对比")
        assert looks_multitask("排名前五的银行有哪些以及它们的坏账率")
        assert looks_multitask("每个地区的贷款总额和坏账率")
        # 纯单分析问题不误伤
        assert not looks_multitask("哪些地区的贷款总额最高")
        assert not looks_multitask("本月贷款金额是多少")

    def test_looks_likely_multitask(self):
        """弱提示词或长问句进入 LLM 判断层(慢路径)。"""
        from trove.agent.tasks import looks_likely_multitask

        assert looks_likely_multitask("各银行的不良贷款情况如何,资产质量怎么样")
        assert looks_likely_multitask(
            "我想详细了解一下这些银行在过去一年中的不良贷款率情况与资产质量的变化趋势情况如何"
        )
        # 短问句 + 无弱提示词:不浪费 LLM 调用
        assert not looks_likely_multitask("贷款总额是多少")
        assert not looks_likely_multitask("哪个地区的平均贷款金额最高?")

    def test_format_result_packet(self):
        """ContextPacket → [previous results] 文本块:SQL/行数/预览行/裁定。"""
        from trove.agent.tasks import format_result_packet

        text = format_result_packet({
            "title": "学生名单",
            "sql": "SELECT name FROM students;",
            "columns": ["id", "name"],
            "rows_preview": [[1, "Alice"], [2, "Bob"]],
            "row_count": 5,
            "verdict": "OK",
            "error": None,
            "matched_tables": ["students"],
        })
        assert "[previous results]" in text
        assert "学生名单" in text
        assert "SELECT name FROM students;" in text
        assert "5" in text  # row_count
        assert "Alice" in text  # 预览行内容
        # 失败包带错误说明
        text = format_result_packet({
            "title": "坏账率", "sql": None, "columns": [], "rows_preview": [],
            "row_count": 0, "verdict": None, "error": "boom", "matched_tables": [],
        })
        assert "boom" in text

    def test_looks_task_followup(self):
        from trove.agent.tasks import looks_task_followup

        assert looks_task_followup("继续")
        assert looks_task_followup("重做第 2 个")
        assert looks_task_followup("跳过第 3 个")
        assert not looks_task_followup("有多少学生?")

    def test_is_approve_all_and_reject(self):
        from trove.agent.tasks import is_approve_all, is_reject

        assert is_approve_all("approve_all")
        assert is_approve_all("2")
        assert is_approve_all("YA")
        assert not is_approve_all("yes")
        assert is_reject("no")
        assert is_reject("3")
        assert is_reject(False)
        assert not is_reject("yes")

    def test_parse_task_json_tolerates_fences_and_prose(self):
        from trove.agent.tasks import parse_task_json

        assert parse_task_json('```json\n{"tasks": ["A", "B"]}\n```') == ["A", "B"]
        assert parse_task_json('好的,已拆解:\n{"tasks": ["A", " B ", ""]}') == ["A", "B"]
        assert parse_task_json("这不是 JSON") == []
        assert parse_task_json('{"tasks": "A"}') == []

    def test_parse_action_json_degrades_to_none(self):
        from trove.agent.tasks import parse_action_json

        assert parse_action_json('{"action": "redo", "index": 2}') == {"action": "redo", "index": 2}
        assert parse_action_json('{"action": "none"}') == {"action": "none"}
        assert parse_action_json("垃圾") == {"action": "none"}
        assert parse_action_json('{"action": "explode"}') == {"action": "none"}
