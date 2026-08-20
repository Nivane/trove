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
        "gen_sql": {"sql": "SELECT name FROM students;", "attempts": 1, "llm": None},
        "execute_sql": {"row_count": 5, "execution_time_ms": 1},
        "reflect": {"verdict": "OK", "reason": "", "llm": None},
        "output": {"final_response": final_response},
    }


class _StubManagerHarness:
    """SessionManager over a StubGraph with a scripted LLM gateway."""

    def __init__(self, tmp_home, llm_responses, graph):
        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore

        config = AgentConfig(home=str(tmp_home), language="zh", target="mock/model")
        self.manager = SessionManager(
            config=config,
            session_store=SessionStore(home_dir=str(tmp_home)),
            graphs={"reflection": graph},
            llm_gateway=ScriptedGateway(llm_responses),
        )

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
            [DECOMPOSE_JSON],
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

    async def test_task_failure_marks_failed_and_continues(self, tmp_home):
        """单任务失败 → failed 状态 + 错误事件,序列继续下一条。"""
        graph = StubGraph([RuntimeError("boom"), _ok_outcome("成绩答案")])
        h = _StubManagerHarness(
            tmp_home,
            [DECOMPOSE_JSON],
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


class TestCrossTurnFollowup:
    # 注:continue_next 真正执行 pending 任务的路径由 HITL 三选项测试覆盖
    # (approve 单任务后回复"继续" → 剩余任务执行)。

    async def test_redo_marks_pending_and_runs(self, tmp_home):
        """'重做第 2 个' → 任务2 pending → 重跑 → 成功。"""
        graph = StubGraph([_ok_outcome("名单答案"), _ok_outcome("成绩答案"), _ok_outcome("重做答案")])
        h = _StubManagerHarness(
            tmp_home,
            [DECOMPOSE_JSON, '{"action": "redo", "index": 2}'],
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
            [DECOMPOSE_JSON, '{"action": "skip", "index": 2}'],
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
            [DECOMPOSE_JSON, '{"action": "continue_next"}'],
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

    def _build(self, tmp_home, sqlite_registry, responses):
        from langgraph.checkpoint.memory import InMemorySaver

        from trove.agent.session import SessionManager
        from trove.core.config import AgentConfig
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs

        config = AgentConfig(home=str(tmp_home), target="mock/model", hitl=True)
        gateway = ScriptedGateway(responses)
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
