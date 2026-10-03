"""主题域在会话/任务层的接线(session.ask / ask_stream / 缓存键)。

schema linking 内部的收敛在 ``tests/workflow/test_schema_linking_topics.py``
里钉过;这里守的是**最后一米**:topic 从调用方一路走到 ``WorkflowState``、
再走到图上,并且路上的每一处"会记住东西"的地方都带上它 ——

* **任务拆解不改变问数范围**:一个主题域下的多任务问题,子任务逐条收敛在
  同一个域里(否则第一条子任务问完,第二条就悄悄放开了范围);
* **结果缓存按主题域隔离**:缓存存的是 ``rows`` 本身,跨域复用等于把域外
  的数据端给一个"我只选了 loans 域"的用户,而且**不再过作用域收敛**
  (与 bypass 不写缓存同一条理由);
* **失败方向显式**:域不存在 / 域过期 / 问题在域外 —— 都是拒绝,不是回落全量。
"""
from __future__ import annotations

import yaml

from trove.core.config import AgentConfig
from trove.services.datasource.catalog import CatalogService
from trove.services.kb.semantic_gen import generate_semantic_document
from trove.services.semantic_layer.provider import SemanticLayerProvider
from trove.storage.session_store import SessionStore
from trove.workflow.graphs import GraphServices, build_graphs

from tests.conftest import ScriptedGateway, _test_terms

SQL = "```sql\nSELECT name FROM students;\n```"

#: 三个主题域:一个正常域、一个域内无数据(过期)、一个指不到任何数据集。
TOPIC_DOCS = [
    {"name": "learners", "description": "学生域", "datasets": ["students"]},
    {"name": "faculty", "description": "教师域", "datasets": ["teachers"]},
    {"name": "legacy", "description": "已拆掉的旧域", "datasets": ["dropped_table"]},
]


async def _topic_manager(sqlite_registry, tmp_path, tmp_home, *, result_cache=False):
    """两数据集(students/teachers)+ 三主题域的语义模型 → 真 graph 的 manager。"""
    adapter = await sqlite_registry.get("test_db")
    await adapter.execute(
        "CREATE TABLE IF NOT EXISTS teachers "
        "(id INTEGER PRIMARY KEY, name TEXT, subject TEXT)"
    )
    schema = await adapter.get_schema()
    doc = generate_semantic_document(
        schema, model_name="test_db", terms=_test_terms(schema))
    doc["semantic_model"][0]["topics"] = TOPIC_DOCS
    path = tmp_path / "semantic" / "test_db" / "semantics.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    config = AgentConfig(
        home=str(tmp_home), target="mock/model", language="zh",
        result_cache=result_cache,
    )
    provider = SemanticLayerProvider(
        tmp_path / "semantic", "test_db", kb_semantics_path=path, dialect="sqlite")
    services = GraphServices(
        llm=ScriptedGateway(["query", SQL, "OK"]),
        catalog=CatalogService(sqlite_registry),
        connectors=sqlite_registry,
        semantic_layer=provider,
        config=config,
    )
    graphs = build_graphs(
        services, multi_candidate=False, query_sketch=False, agentic=False)
    store = SessionStore(home_dir=str(tmp_home))
    from trove.agent.session import SessionManager

    manager = SessionManager(
        config=config, session_store=store, graphs=graphs,
        llm_gateway=ScriptedGateway(["query", SQL, "OK"]),
    )
    return manager, store


class TestTopicScopeThroughSession:
    async def _ask(self, sqlite_registry, tmp_path, tmp_home, question, **kw):
        manager, store = await _topic_manager(
            sqlite_registry, tmp_path, tmp_home)
        try:
            session = await manager.start_session(project_cwd="/tmp/p1")
            state = await manager.ask(
                session, question, workflow_name="reflection", **kw)
            return state
        finally:
            await store.dispose()

    async def test_topic_lands_on_state_and_narrows_the_run(
        self, sqlite_registry, tmp_path, tmp_home,
    ):
        """同一问句:带域只锚域内数据集,不带域两个都锚 —— 收敛发生在会话层。"""
        state = await self._ask(
            sqlite_registry, tmp_path, tmp_home,
            "What students and teachers are in Alameda county?", topic="learners",
        )
        assert state.topic == "learners"
        assert state.matched_tables == ["students"]
        assert state.sql == "SELECT name FROM students;"
        assert not state.refusal

    async def test_without_topic_the_same_question_is_unscoped(
        self, sqlite_registry, tmp_path, tmp_home,
    ):
        """对照组:不选主题域时行为与不启用主题域完全一致(两个数据集都进)。"""
        state = await self._ask(
            sqlite_registry, tmp_path, tmp_home,
            "What students and teachers are in Alameda county?",
        )
        assert state.topic == ""
        assert set(state.matched_tables) == {"students", "teachers"}

    async def test_unknown_topic_refuses_explicitly(
        self, sqlite_registry, tmp_path, tmp_home,
    ):
        state = await self._ask(
            sqlite_registry, tmp_path, tmp_home,
            "What students are there?", topic="ghost",
        )
        assert state.refusal["reason"] == "topic_not_found"
        assert state.refusal["topic"] == "ghost"
        assert set(state.refusal["available_topics"]) == {
            "learners", "faculty", "legacy"}
        assert "ghost" in state.final_response
        assert not state.sql  # 拒绝不带着一份"全量"的 SQL 出去

    async def test_stale_topic_refuses_explicitly(
        self, sqlite_registry, tmp_path, tmp_home,
    ):
        """域还在、声明的数据集全没了 → 过期,不是"不限制"。"""
        state = await self._ask(
            sqlite_registry, tmp_path, tmp_home,
            "What students are there?", topic="legacy",
        )
        assert state.refusal["reason"] == "topic_empty_scope"
        assert "legacy" in state.final_response

    async def test_out_of_scope_question_refuses_not_answers(
        self, sqlite_registry, tmp_path, tmp_home,
    ):
        """教师问题落在 learners 域外 → 拒绝(域内零锚定),不是答在更大的范围上。"""
        state = await self._ask(
            sqlite_registry, tmp_path, tmp_home,
            "What teachers teach math?", topic="learners",
        )
        assert state.matched_tables == []
        assert state.refusal["reason"] == "no_semantic_match"
        assert state.refusal["topic"] == "learners"
        assert "learners" in state.final_response
        assert not state.sql


class TestTopicCacheIsolation:
    async def test_cache_key_separates_topics(
        self, sqlite_registry, tmp_path, tmp_home,
    ):
        """topic 是缓存键的一个分量(大小写/空白归一后同键)。"""
        manager, store = await _topic_manager(sqlite_registry, tmp_path, tmp_home)
        try:
            session = await manager.start_session(project_cwd="/tmp/p1")
            key = lambda t: manager._cache_key(  # noqa: E731
                session, "What students are there?", None, None, t)
            assert key("") != key("learners")
            assert key("learners") != key("faculty")
            assert key("  LEARNERS ") == key("learners")
        finally:
            await store.dispose()

    async def test_cached_rows_are_not_replayed_across_topics(
        self, sqlite_registry, tmp_path, tmp_home,
    ):
        """缓存里是 rows 本身:域外提问不得复用域内缓存(那等于范围失效)。"""
        manager, store = await _topic_manager(
            sqlite_registry, tmp_path, tmp_home, result_cache=True)
        try:
            session = await manager.start_session(project_cwd="/tmp/p1")
            question = "What students and teachers are in Alameda county?"
            # 预置一条「learners 域缓存」:带域提问必须命中它,不带域必须绕过。
            # 键按 ask() 的同一算法算(数据源缺省解析 + 已解析主体),只差主题域。
            principal = await manager._principal_wire(session)
            manager._cache_put(
                manager._cache_key(
                    session, question, None, principal, "learners"),
                {
                    "session_id": session.session_id,
                    "question": question,
                    "datasource": "test_db",
                    "topic": "learners",
                    "sql": "SELECT 1;",
                    "row_count": 1,
                    "verdict": "OK",
                    "final_response": "域内缓存答案",
                    "matched_tables": ["students"],
                },
            )
            scoped = await manager.ask(
                session, question, workflow_name="reflection", topic="learners")
            assert scoped.final_response == "域内缓存答案"
            assert scoped.sql == "SELECT 1;"  # 全图未跑,直接回放

            unscoped = await manager.ask(
                session, question, workflow_name="reflection")
            assert unscoped.final_response != "域内缓存答案"
            assert unscoped.sql == "SELECT name FROM students;"  # 真跑了一遍
        finally:
            await store.dispose()


class TestTopicTaskPropagation:
    """任务拆解:子任务逐条落在同一个主题域里(拆解不改变问数范围)。"""

    async def test_subtasks_keep_the_topic(self, tmp_home):
        from tests.agent.test_session_tasks import (
            DECOMPOSE_JSON, SYNTHESIS_TEXT, StubGraph, _ok_outcome, _StubManagerHarness,
        )

        graph = StubGraph([_ok_outcome("名单答案"), _ok_outcome("成绩答案")])
        h = _StubManagerHarness(
            tmp_home, [DECOMPOSE_JSON, SYNTHESIS_TEXT], graph)
        session = await h.session()

        events = [ev async for ev in h.manager.ask_stream(
            session=session, question="分别查询 1. 学生名单 2. 平均成绩",
            topic="learners",
        )]

        states = graph.run_states()
        assert len(states) == 2
        assert [s["topic"] for s in states] == ["learners", "learners"]
        # 逐任务 done 的 summary 也带着域(前端/审计据它记账)
        dones = h.done_events(events)
        assert all(d["summary"]["topic"] == "learners" for d in dones[:2])
