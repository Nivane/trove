"""End-to-end integration tests (LangGraph era).

The core MVP closed loop:
  Natural language question → schema_linking → gen_sql → execute_sql
  → reflect → output

All LLM calls are scripted mocks (zero network, zero API keys).
"""

from __future__ import annotations

import pytest

from trove.core.config import AgentConfig
from trove.core.types import Message
from trove.services.datasource.catalog import CatalogService
from trove.storage.session_store import SessionStore
from trove.workflow.graphs import GraphServices, build_graphs
from trove.agent.session import SessionManager


class ScriptedLLM:
    """LLM mock that returns scripted responses based on prompt content.

    Maps prompt patterns to canned responses so the full workflow
    can run without any real LLM calls.
    """

    def __init__(self, sql: str, reflect_response: str = "OK", summarize_response: str = "summary"):
        self.sql = sql
        self.reflect_response = reflect_response
        self.summarize_response = summarize_response
        self.call_count = 0

    async def chat_full(self, model, messages, tools=None, **kwargs):
        self.call_count += 1
        content = self._content(messages)
        return {"content": content, "tool_calls": []}

    async def chat(self, model: str, messages: list[dict], **kwargs) -> str:
        self.call_count += 1
        return self._content(messages)

    def _content(self, messages):
        # Inspect the last user message to decide which canned response to return
        last_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_content = msg.get("content", "")
                break

        if "Summarize this conversation" in last_content or "请压缩这段对话" in last_content:
            return self.summarize_response
        if "Does this result correctly answer" in last_content:
            return self.reflect_response
        if "failed validation" in last_content or "校验错误" in last_content:
            return f"```sql\n{self.sql}\n```"
        # Default: SQL generation prompt
        return f"```sql\n{self.sql}\n```"


@pytest.fixture
async def full_stack(tmp_path, demo_registry):
    """A fully wired stack with demo data and scripted LLM."""
    store = SessionStore(home_dir=str(tmp_path / "home"))
    catalog = CatalogService(demo_registry)

    config = AgentConfig(home=str(tmp_path / "home"), target="mock/model")

    llm = ScriptedLLM(
        sql="SELECT d.A2 AS district_name, AVG(l.amount) AS avg_loan "
            "FROM loan l "
            "JOIN account a ON l.account_id = a.account_id "
            "JOIN district d ON a.district_id = d.district_id "
            "GROUP BY d.A2 ORDER BY avg_loan DESC",
    )

    # KB 术语解决中文问题匹配（真实使用路径：/kb init + 术语）
    from tests.helpers.kb import ossie_semantics_yaml
    from trove.services.kb.service import KbService
    kb = KbService(tmp_path / "proj")
    (kb.kb_dir / demo_registry.default_name).mkdir(parents=True, exist_ok=True)
    (kb.kb_dir / demo_registry.default_name / "semantics.yml").write_text(
        ossie_semantics_yaml([
            {"term": "平均贷款金额", "aliases": ["平均贷款"],
             "mapping": "AVG(loan.amount)", "tables": ["loan", "account", "district"],
             "definition": "按地区分组的贷款金额均值"},
        ]))

    services = GraphServices(
        llm=llm,
        catalog=catalog,
        connectors=demo_registry,semantic_layer=getattr(demo_registry, "_test_semantic_provider", None),
        config=config,
        kb=kb,
    )
    manager = SessionManager(
        config=config,
        session_store=store,
        graphs=build_graphs(services, agentic=False),
        llm_gateway=llm,
    )
    return manager


class TestEndToEnd:
    async def test_full_question_loop(self, full_stack):
        """The complete MVP loop: question → SQL → result → formatted answer."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        state = await full_stack.ask(
            session=session,
            question="哪个地区的平均贷款金额最高？",
            workflow_name="reflection",
        )

        # All pipeline stages produced their artifacts.
        # NOTE: matched_tables is empty for Chinese questions — search_tables
        # tokenizes with ASCII \w only (pre-existing; semantic matching is
        # the v0.2 RAG roadmap).
        assert "loan" in state.sql
        assert "district" in state.sql
        assert state.row_count == 3
        assert state.verdict == "OK"
        assert state.error == ""

        # Execute returned the right answer (Benesov has the highest avg loan)
        assert "Benesov" in str(state.rows)

        # Final output contains the result
        assert "Benesov" in state.final_response
        # 问题/标题不再重复展示
        assert "**问题**" not in state.final_response

        # The exchange was recorded with graph metadata
        assert session.messages[-1].metadata["sql"] == state.sql
        assert session.messages[-1].metadata["verdict"] == "OK"

    async def test_year_grouped_query_generates_chart(self, tmp_path, demo_registry):
        """按年聚合(整数值年份)也必须自动出图——回归:年份列被判为数值度量
        导致无维度 → 无图表。"""
        from trove.services.datasource.catalog import CatalogService
        from trove.storage.session_store import SessionStore
        from trove.workflow.graphs import GraphServices, build_graphs
        from trove.agent.session import SessionManager

        store = SessionStore(home_dir=str(tmp_path / "home"))
        config = AgentConfig(home=str(tmp_path / "home"), target="mock/model")
        sql = (
            "SELECT strftime('%Y', date) AS year, COUNT(*) AS cnt "
            "FROM loan GROUP BY year ORDER BY year"
        )
        llm = ScriptedLLM(sql=sql)
        services = GraphServices(
            llm=llm,
            catalog=CatalogService(demo_registry),
            connectors=demo_registry,
            semantic_layer=getattr(demo_registry, "_test_semantic_provider", None),
            config=config,
        )
        manager = SessionManager(
            config=config,
            session_store=store,
            graphs=build_graphs(services, agentic=False),
            llm_gateway=llm,
        )
        session = await manager.start_session(project_cwd=str(tmp_path))
        state = await manager.ask(
            session=session, question="每年发放的贷款笔数", workflow_name="reflection",
        )
        assert state.error == ""
        assert state.chart is not None, (
            f"year-grouped query produced no chart (columns={state.columns})"
        )
        assert state.chart["type"] in ("line", "bar")
        assert state.chart["dimension"] == "year"
        assert state.chart["measures"] == ["cnt"]

    async def test_multi_turn_conversation(self, full_stack):
        """Multiple questions in the same session accumulate history."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        await full_stack.ask(session=session, question="第一问", workflow_name="reflection")
        await full_stack.ask(session=session, question="第二问", workflow_name="reflection")
        await full_stack.ask(session=session, question="第三问", workflow_name="reflection")

        assert len(session.messages) == 6  # 3 user + 3 assistant

        # History persists across "restarts" (reload from disk)
        loaded = await full_stack.load_session(session.session_id, "/tmp/integration")
        assert len(loaded.messages) == 6
        assert loaded.messages[0].content == "第一问"
        assert loaded.messages[5].content != ""  # last assistant message non-empty

    async def test_fixed_workflow(self, full_stack):
        """The 'fixed' workflow skips reflection."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        state = await full_stack.ask(
            session=session,
            question="List the loans per district",
            workflow_name="fixed",
        )
        assert state.verdict == ""  # reflect never ran
        assert state.row_count == 3

    async def test_session_compaction_flow(self, full_stack):
        """Full compaction flow: many messages → compact → still usable."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        for i in range(5):
            session.messages.append(Message(role="user", content=f"问题{i}"))
            session.messages.append(Message(role="assistant", content=f"回答{i}"))

        # Compact with scripted LLM summarization
        compacted = await full_stack.compact_session(session, keep_recent=1)
        assert len(compacted.messages) == 3
        assert compacted.messages[0].role == "system"

        # Session still usable after compaction
        state = await full_stack.ask(
            session=compacted,
            question="压缩后的新问题",
            workflow_name="reflection",
        )
        assert state.final_response

    async def test_question_with_no_matching_tables(self, full_stack):
        """语义优先(Phase B,决策 4):零命中 = 未覆盖 = 拒绝 + 反问,不生成。"""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        state = await full_stack.ask(
            session=session,
            question="zzz 不存在的表名 zzz",
            workflow_name="reflection",
        )
        assert state.matched_tables == []
        assert state.refusal is not None
        assert state.final_response
        assert "语义模型" in state.final_response


class TestWorkflowEdgeCases:
    async def test_scripted_llm_retry(self, tmp_path, demo_registry):
        """gen_sql subgraph retries when the first response is invalid."""
        store = SessionStore(home_dir=str(tmp_path / "home"))
        catalog = CatalogService(demo_registry)

        config = AgentConfig(home=str(tmp_path / "home"))

        class RetryLLM:
            def __init__(self):
                self.responses = iter([
                    "query",  # route_intent 意图分类
                    "```sql\nSELEC broken sql;\n```",  # 1st: invalid
                    "```sql\nSELECT COUNT(*) FROM client;\n```",  # 2nd: valid
                ])
                self.calls = 0

            async def chat(self, model, messages, **kwargs):
                self.calls += 1
                return next(self.responses)

            async def chat_full(self, model, messages, tools=None, **kwargs):
                self.calls += 1
                return {"content": next(self.responses), "tool_calls": []}

        llm = RetryLLM()
        manager = SessionManager(
            config=config,
            session_store=store,
            graphs=build_graphs(GraphServices(
                llm=llm,
                catalog=catalog,
                connectors=demo_registry,semantic_layer=getattr(demo_registry, "_test_semantic_provider", None),
                config=config,
            ), multi_candidate=False, agentic=False),
            llm_gateway=llm,
        )

        session = await manager.start_session(project_cwd="/tmp/retry")
        state = await manager.ask(
            session=session,
            question="How many client records are there",
            workflow_name="fixed",
        )

        # gen_sql succeeded on the second attempt
        assert llm.calls == 3  # 意图 + 初稿（非法）+ 修正稿
        assert state.sql == "SELECT COUNT(*) FROM client;"
        assert state.error == ""

        # Execution returned the row count
        assert state.row_count == 1
