"""End-to-end integration tests.

The core MVP closed loop:
  Natural language question → schema_linking → gen_sql → execute_sql
  → reflect → output

All LLM calls are scripted mocks (zero network, zero API keys).
"""

from __future__ import annotations

import asyncio

import pytest

from trove.core.config import AgentConfig
from trove.core.types import Message
from trove.llm.gateway import LLMGateway
from trove.services.datasource.catalog import CatalogService
from trove.storage.session_store import SessionStore
from trove.workflow.engine import WorkflowEngine
from trove.workflow.registry import WorkflowRegistry
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

    async def chat(self, model: str, messages: list[dict], **kwargs) -> str:
        self.call_count += 1
        # Inspect the last user message to decide which canned response to return
        last_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                last_content = msg.get("content", "")
                break

        if "Summarize this conversation" in last_content:
            return self.summarize_response
        if "Does this result correctly answer" in last_content:
            return self.reflect_response
        if "Fix the following SQL" in last_content or "failed validation" in last_content:
            return f"```sql\n{self.sql}\n```"
        # Default: SQL generation prompt
        return f"```sql\n{self.sql}\n```"


@pytest.fixture
async def full_stack(tmp_path, demo_registry):
    """A fully wired stack with demo data and scripted LLM."""
    store = SessionStore(home_dir=str(tmp_path / "home"))
    catalog = CatalogService(demo_registry)

    engine = WorkflowEngine()
    for name in WorkflowRegistry.list_available():
        engine.register(WorkflowRegistry.create(name))

    config = AgentConfig(home=str(tmp_path / "home"), target="mock/model")

    llm = ScriptedLLM(
        sql="SELECT d.A2 AS district_name, AVG(l.amount) AS avg_loan "
            "FROM loan l "
            "JOIN account a ON l.account_id = a.account_id "
            "JOIN district d ON a.district_id = d.district_id "
            "GROUP BY d.A2 ORDER BY avg_loan DESC",
    )

    manager = SessionManager(
        config=config,
        session_store=store,
        workflow_engine=engine,
        llm_gateway=llm,
        catalog_service=catalog,
        connector_registry=demo_registry,
    )
    return manager


class TestEndToEnd:
    async def test_full_question_loop(self, full_stack):
        """The complete MVP loop: question → SQL → result → formatted answer."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        response, result = await full_stack.ask(
            session=session,
            question="哪个地区的平均贷款金额最高？",
            workflow_name="reflection",
        )

        # Workflow completed
        assert result.workflow_name == "reflection"
        assert len(result.nodes) == 5  # all 5 nodes ran

        # Check each node succeeded
        node_statuses = {n.node_name: n.status.value for n in result.nodes}
        assert node_statuses["schema_linking"] == "success"
        assert node_statuses["gen_sql"] == "success"
        assert node_statuses["execute_sql"] == "success"
        assert node_statuses["reflect"] == "success"
        assert node_statuses["output"] == "success"

        # SQL contains the expected joins
        sql = next(
            n.data["sql"] for n in result.nodes
            if n.node_name == "gen_sql" and "sql" in n.data
        )
        assert "loan" in sql
        assert "district" in sql

        # Execute returned the right answer (Benesov has the highest avg loan)
        exec_data = next(
            n.data for n in result.nodes
            if n.node_name == "execute_sql"
        )
        assert exec_data["row_count"] == 3
        assert "Benesov" in str(exec_data["rows"])

        # Final output contains the result
        assert "Benesov" in response
        assert "Question" in response

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
        """The 'fixed' workflow skips reflection (4 nodes)."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        _, result = await full_stack.ask(
            session=session,
            question="test",
            workflow_name="fixed",
        )
        assert len(result.nodes) == 4
        node_names = [n.node_name for n in result.nodes]
        assert "reflect" not in node_names

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
        _, result = await full_stack.ask(
            session=compacted,
            question="压缩后的新问题",
            workflow_name="reflection",
        )
        assert result.final_output

    async def test_question_with_no_matching_tables(self, full_stack):
        """Schema linking gracefully handles unmatched questions."""
        session = await full_stack.start_session(project_cwd="/tmp/integration")

        response, result = await full_stack.ask(
            session=session,
            question="zzz 不存在的表名 zzz",
            workflow_name="reflection",
        )
        # Workflow still completes (LLM scripted response is used)
        assert result.nodes[0].node_name == "schema_linking"
        # No tables matched
        assert result.nodes[0].data["matched_tables"] == []


class TestWorkflowEdgeCases:
    async def test_scripted_llm_retry(self, tmp_path, demo_registry):
        """gen_sql retries when the first response is invalid."""
        store = SessionStore(home_dir=str(tmp_path / "home"))
        catalog = CatalogService(demo_registry)

        engine = WorkflowEngine()
        engine.register(WorkflowRegistry.create("fixed"))

        config = AgentConfig(home=str(tmp_path / "home"))

        class RetryLLM:
            def __init__(self):
                self.responses = iter([
                    "```sql\nSELEC broken sql;\n```",  # 1st: invalid
                    "```sql\nSELECT COUNT(*) FROM client;\n```",  # 2nd: valid
                ])
                self.calls = 0

            async def chat(self, model, messages, **kwargs):
                self.calls += 1
                return next(self.responses)

        llm = RetryLLM()
        manager = SessionManager(
            config=config,
            session_store=store,
            workflow_engine=engine,
            llm_gateway=llm,
            catalog_service=catalog,
            connector_registry=demo_registry,
        )

        session = await manager.start_session(project_cwd="/tmp/retry")
        _, result = await manager.ask(
            session=session,
            question="有多少个客户？",
            workflow_name="fixed",
        )

        # gen_sql succeeded on second attempt
        gen_node = next(n for n in result.nodes if n.node_name == "gen_sql")
        assert gen_node.status.value == "success"
        assert gen_node.data["attempts"] == 2

        # Execution returned school count
        exec_node = next(n for n in result.nodes if n.node_name == "execute_sql")
        assert exec_node.data["row_count"] == 1
