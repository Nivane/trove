"""Workflow node implementation tests."""

import asyncio

import pytest

from trove.core.types import (
    Message,
    Session,
    WorkflowContext,
    NodeStatus,
)
from trove.core.config import AgentConfig
from trove.llm.gateway import LLMGateway
from trove.workflow.nodes.schema_linking import SchemaLinkingNode
from trove.workflow.nodes.gen_sql import GenSQLNode
from trove.workflow.nodes.execute_sql import ExecuteSQLNode
from trove.workflow.nodes.reflect import ReflectNode
from trove.workflow.nodes.output import OutputNode


def make_ctx(config=None):
    """Create a test workflow context."""
    return WorkflowContext(
        session=Session(),
        user_message=Message(role="user", content="Average grade by county"),
        config=config or AgentConfig(),
    )


class TestSchemaLinkingNode:
    async def test_no_catalog_graceful(self):
        node = SchemaLinkingNode()
        ctx = make_ctx()
        result = await node.execute(ctx)
        assert result.status == NodeStatus.SUCCESS
        assert result.data["matched_tables"] == []
        assert "No schema" in result.data["schema_context"]

    async def test_with_catalog(self, sqlite_registry):
        from trove.services.datasource.catalog import CatalogService
        config = AgentConfig()
        config._catalog_service = CatalogService(sqlite_registry)  # type: ignore[attr-defined]

        node = SchemaLinkingNode()
        ctx = make_ctx(config)
        result = await node.execute(ctx)

        assert result.status == NodeStatus.SUCCESS
        assert "students" in result.data["matched_tables"]
        assert "grade" in result.data["schema_context"]


class TestGenSQLNode:
    async def test_extract_sql_from_code_block(self):
        node = GenSQLNode()
        response = "Here is the query:\n```sql\nSELECT * FROM students;\n```\nHope it helps!"
        sql = node._extract_sql(response)
        assert sql == "SELECT * FROM students;"

    async def test_extract_sql_generic_block(self):
        node = GenSQLNode()
        response = "```\nSELECT 1\n```"
        sql = node._extract_sql(response)
        assert sql == "SELECT 1"

    async def test_extract_sql_raw(self):
        node = GenSQLNode()
        response = "SELECT county, AVG(grade) FROM students GROUP BY county"
        sql = node._extract_sql(response)
        assert sql.startswith("SELECT")

    async def test_extract_empty(self):
        node = GenSQLNode()
        assert node._extract_sql("") == ""
        assert node._extract_sql("I cannot generate SQL for this") != ""

    async def test_validate_valid_sql(self):
        node = GenSQLNode()
        ctx = make_ctx()
        valid, errors = await node._validate_sql("SELECT * FROM t", ctx)
        assert valid is True
        assert errors == []

    async def test_validate_invalid_sql(self):
        node = GenSQLNode()
        ctx = make_ctx()
        valid, errors = await node._validate_sql("SELEC * FROM", ctx)
        assert valid is False
        assert len(errors) > 0

    async def test_execute_with_valid_mock_llm(self):
        """Full gen_sql execution with mocked LLM returning valid SQL."""
        config = AgentConfig()
        config._llm_gateway = LLMGateway(  # type: ignore[attr-defined]
            mock_response="```sql\nSELECT county, AVG(grade) FROM students GROUP BY county;\n```"
        )

        node = GenSQLNode()
        ctx = make_ctx(config)
        result = await node.execute(ctx)

        assert result.status == NodeStatus.SUCCESS
        assert "students" in result.data["sql"]
        assert result.data["attempts"] == 1

    async def test_execute_retries_on_invalid_sql(self):
        """LLM returns invalid SQL first, valid SQL on retry."""
        config = AgentConfig()
        responses = iter([
            "```sql\nSELEC * FROM students;\n```",  # invalid
            "```sql\nSELECT * FROM students;\n```",  # valid
        ])

        class ScriptedLLM:
            async def chat(self, model, messages, **kwargs):
                return next(responses)

        config._llm_gateway = ScriptedLLM()  # type: ignore[attr-defined]

        node = GenSQLNode(max_retries=3)
        ctx = make_ctx(config)
        result = await node.execute(ctx)

        assert result.status == NodeStatus.SUCCESS
        assert result.data["attempts"] == 2
        assert result.data["sql"] == "SELECT * FROM students;"

    async def test_execute_fails_after_max_retries(self):
        config = AgentConfig()
        config._llm_gateway = LLMGateway(  # type: ignore[attr-defined]
            mock_response="```sql\nSELEC * FROM students;\n```"  # always invalid
        )

        node = GenSQLNode(max_retries=3)
        ctx = make_ctx(config)
        result = await node.execute(ctx)

        assert result.status == NodeStatus.ERROR
        assert result.error is not None

    async def test_execute_empty_llm_response(self):
        config = AgentConfig()
        config._llm_gateway = LLMGateway(mock_response="")  # type: ignore[attr-defined]

        node = GenSQLNode(max_retries=2)
        ctx = make_ctx(config)
        result = await node.execute(ctx)
        assert result.status == NodeStatus.ERROR


class TestExecuteSQLNode:
    async def test_no_sql_error(self):
        node = ExecuteSQLNode()
        ctx = make_ctx()
        result = await node.execute(ctx)
        assert result.status == NodeStatus.ERROR
        assert "No SQL" in str(result.error)

    async def test_execute_valid_sql(self, sqlite_registry):
        config = AgentConfig()
        config._connector_registry = sqlite_registry  # type: ignore[attr-defined]

        node = ExecuteSQLNode()
        ctx = make_ctx(config)
        ctx._node_data = {"gen_sql": {"sql": "SELECT name FROM students ORDER BY name"}}  # type: ignore[attr-defined]

        result = await node.execute(ctx)
        assert result.status == NodeStatus.SUCCESS
        assert result.data["row_count"] == 5
        assert result.data["columns"] == ["name"]

    async def test_execute_error_sql(self, sqlite_registry):
        config = AgentConfig()
        config._connector_registry = sqlite_registry  # type: ignore[attr-defined]

        node = ExecuteSQLNode()
        ctx = make_ctx(config)
        ctx._node_data = {"gen_sql": {"sql": "SELECT * FROM nonexistent"}}  # type: ignore[attr-defined]

        result = await node.execute(ctx)
        assert result.status == NodeStatus.ERROR

    async def test_cancelled_before_execution(self, sqlite_registry):
        config = AgentConfig()
        config._connector_registry = sqlite_registry  # type: ignore[attr-defined]

        node = ExecuteSQLNode()
        ctx = make_ctx(config)
        ctx.cancellation_event.set()  # pre-cancelled
        ctx._node_data = {"gen_sql": {"sql": "SELECT 1"}}  # type: ignore[attr-defined]

        from trove.core.errors import CancelledError
        with pytest.raises(CancelledError):
            await node.execute(ctx)


class TestReflectNode:
    async def test_empty_result_short_circuits(self):
        """Zero rows → EMPTY verdict without LLM call."""
        node = ReflectNode()
        ctx = make_ctx()
        ctx._node_data = {"execute_sql": {"row_count": 0, "columns": [], "rows": []}}  # type: ignore[attr-defined]

        result = await node.execute(ctx)
        assert result.status == NodeStatus.SUCCESS
        assert result.data["verdict"] == "EMPTY"

    async def test_ok_verdict(self):
        config = AgentConfig()
        config._llm_gateway = LLMGateway(mock_response="OK")  # type: ignore[attr-defined]

        node = ReflectNode()
        ctx = make_ctx(config)
        ctx._node_data = {"execute_sql": {  # type: ignore[attr-defined]
            "row_count": 3, "columns": ["county"], "rows": [["A"], ["B"], ["C"]],
        }}

        result = await node.execute(ctx)
        assert result.status == NodeStatus.SUCCESS
        assert result.data["verdict"] == "OK"

    async def test_retry_verdict(self):
        config = AgentConfig()
        config._llm_gateway = LLMGateway(  # type: ignore[attr-defined]
            mock_response="RETRY: wrong grouping"
        )

        node = ReflectNode()
        ctx = make_ctx(config)
        ctx._node_data = {"execute_sql": {  # type: ignore[attr-defined]
            "row_count": 3, "columns": ["x"], "rows": [[1], [2], [3]],
        }}

        result = await node.execute(ctx)
        assert result.status == NodeStatus.RETRY
        assert result.data["retry_target"] == "gen_sql"
        assert result.data["reason"] == "wrong grouping"

    async def test_llm_failure_assumes_ok(self):
        config = AgentConfig()
        config._llm_gateway = LLMGateway(mock_response="OK")  # type: ignore[attr-defined]

        # Break the gateway after instantiation
        config._llm_gateway.chat = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[attr-defined]
            RuntimeError("llm down")
        )

        node = ReflectNode()
        ctx = make_ctx(config)
        ctx._node_data = {"execute_sql": {  # type: ignore[attr-defined]
            "row_count": 2, "columns": ["x"], "rows": [[1], [2]],
        }}

        result = await node.execute(ctx)
        # Graceful: assume OK
        assert result.status == NodeStatus.SUCCESS
        assert result.data["verdict"] == "OK"


class TestOutputNode:
    async def test_format_with_full_data(self):
        node = OutputNode()
        ctx = make_ctx()
        ctx._node_data = {  # type: ignore[attr-defined]
            "gen_sql": {"sql": "SELECT county FROM students", "attempts": 1},
            "execute_sql": {
                "columns": ["county"],
                "rows": [["Alameda"], ["Orange"]],
                "row_count": 2,
                "execution_time_ms": 15.0,
            },
            "reflect": {"verdict": "OK"},
        }

        result = await node.execute(ctx)
        assert result.status == NodeStatus.SUCCESS

        response = result.data["response"]
        assert "Question" in response
        assert "SELECT county" in response
        assert "Alameda" in response
        assert "2 rows" in response

    async def test_format_empty_result(self):
        node = OutputNode()
        ctx = make_ctx()
        ctx._node_data = {  # type: ignore[attr-defined]
            "execute_sql": {"row_count": 0, "columns": [], "rows": []},
        }

        result = await node.execute(ctx)
        assert "zero rows" in result.data["response"]

    async def test_format_no_execution(self):
        """The 'empty' workflow has no execute_sql data."""
        node = OutputNode()
        ctx = make_ctx()
        result = await node.execute(ctx)
        assert result.status == NodeStatus.SUCCESS
        assert "(No query executed)" in result.data["response"]

    async def test_format_limits_table_rows(self):
        node = OutputNode()
        ctx = make_ctx()
        rows = [[f"row{i}"] for i in range(30)]
        ctx._node_data = {  # type: ignore[attr-defined]
            "execute_sql": {
                "columns": ["col"],
                "rows": rows,
                "row_count": 30,
            },
        }

        result = await node.execute(ctx)
        response = result.data["response"]
        assert "10 more rows" in response  # 30 - 20 displayed = 10 hidden
