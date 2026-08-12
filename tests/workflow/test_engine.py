"""Workflow engine tests."""

import asyncio

import pytest

from trove.core.types import (
    Message,
    Session,
    WorkflowContext,
    NodeStatus,
)
from trove.workflow.engine import WorkflowEngine, WorkflowDefinition
from trove.workflow.node import Node, NodeResult
from trove.workflow.node_type import NodeType
from trove.workflow.registry import WorkflowRegistry


class SimpleNode(Node):
    """Test node that returns a fixed result."""

    node_type = NodeType.OUTPUT

    def __init__(self, name="simple", value="result"):
        super().__init__(name)
        self.value = value

    async def execute(self, ctx):
        return NodeResult(
            node_name=self.name,
            status=NodeStatus.SUCCESS,
            data={"value": self.value},
        )


class FailingNode(Node):
    """Test node that always fails."""

    node_type = NodeType.GEN_SQL

    def __init__(self, name="failing"):
        super().__init__(name)

    async def execute(self, ctx):
        return NodeResult(
            node_name=self.name,
            status=NodeStatus.ERROR,
            error=ValueError("intentional failure"),
        )


class SlowNode(Node):
    """Test node that sleeps."""

    node_type = NodeType.EXECUTE_SQL

    def __init__(self, name="slow", delay=0.1):
        super().__init__(name)
        self.delay = delay

    async def execute(self, ctx):
        await asyncio.sleep(self.delay)
        return NodeResult(
            node_name=self.name,
            status=NodeStatus.SUCCESS,
            data={"slept": self.delay},
        )


def make_ctx():
    return WorkflowContext(
        session=Session(),
        user_message=Message(role="user", content="test"),
        config=None,
    )


class TestWorkflowDefinition:
    def test_create(self):
        wf = WorkflowDefinition(name="test", nodes=[SimpleNode()])
        assert wf.name == "test"
        assert len(wf.nodes) == 1

    def test_default_flag(self):
        wf = WorkflowDefinition(name="x", default=True)
        assert wf.default is True


class TestEngineRegistration:
    def test_register_and_get(self):
        engine = WorkflowEngine()
        wf = WorkflowDefinition(name="w1", nodes=[SimpleNode()])
        engine.register(wf)
        assert engine.get("w1") is wf

    def test_get_unknown_raises(self):
        engine = WorkflowEngine()
        with pytest.raises(KeyError):
            engine.get("unknown")

    def test_list_names(self):
        engine = WorkflowEngine()
        engine.register(WorkflowDefinition(name="a"))
        engine.register(WorkflowDefinition(name="b"))
        assert sorted(engine.list_names()) == ["a", "b"]


class TestEngineExecution:
    async def test_run_single_node(self):
        engine = WorkflowEngine()
        engine.register(WorkflowDefinition(
            name="single",
            nodes=[SimpleNode(name="n1", value="hello")],
        ))

        result = await engine.run("single", make_ctx())
        assert result.workflow_name == "single"
        assert len(result.nodes) == 1
        assert result.nodes[0].status == NodeStatus.SUCCESS
        assert result.nodes[0].data["value"] == "hello"

    async def test_run_multiple_nodes_in_order(self):
        engine = WorkflowEngine()
        order = []

        class TrackingNode(Node):
            node_type = NodeType.OUTPUT

            def __init__(self, name, order_list):
                super().__init__(name)
                self.order_list = order_list

            async def execute(self, ctx):
                self.order_list.append(self.name)
                return NodeResult(
                    node_name=self.name,
                    status=NodeStatus.SUCCESS,
                    data={},
                )

        engine.register(WorkflowDefinition(
            name="seq",
            nodes=[
                TrackingNode("first", order),
                TrackingNode("second", order),
                TrackingNode("third", order),
            ],
            edges=[("first", "second"), ("second", "third")],
        ))

        await engine.run("seq", make_ctx())
        assert order == ["first", "second", "third"]

    async def test_topological_sort_with_edges(self):
        engine = WorkflowEngine()
        order = []

        class TNode(Node):
            node_type = NodeType.OUTPUT

            def __init__(self, name, order_list):
                super().__init__(name)
                self.order_list = order_list

            async def execute(self, ctx):
                self.order_list.append(self.name)
                return NodeResult(node_name=self.name, status=NodeStatus.SUCCESS, data={})

        # Nodes listed out of order but edges define execution order
        engine.register(WorkflowDefinition(
            name="topo",
            nodes=[
                TNode("c", order),
                TNode("a", order),
                TNode("b", order),
            ],
            edges=[("a", "b"), ("b", "c")],
        ))

        await engine.run("topo", make_ctx())
        assert order == ["a", "b", "c"]

    async def test_node_failure_stops_workflow(self):
        engine = WorkflowEngine()
        executed_after = []

        class TrackingNode(Node):
            node_type = NodeType.OUTPUT

            def __init__(self, name, tracker):
                super().__init__(name)
                self.tracker = tracker

            async def execute(self, ctx):
                self.tracker.append(self.name)
                return NodeResult(node_name=self.name, status=NodeStatus.SUCCESS, data={})

        engine.register(WorkflowDefinition(
            name="with_failure",
            nodes=[
                FailingNode(name="bad"),
                TrackingNode("after", executed_after),
            ],
            edges=[("bad", "after")],
        ))

        result = await engine.run("with_failure", make_ctx())
        assert result.nodes[0].status == NodeStatus.ERROR
        # 'after' node did not execute
        assert executed_after == []

    async def test_node_data_merged_into_ctx(self):
        """Node results should be accessible to downstream nodes via ctx._node_data."""
        engine = WorkflowEngine()

        class ReaderNode(Node):
            node_type = NodeType.OUTPUT

            async def execute(self, ctx):
                data = getattr(ctx, '_node_data', {}).get("producer", {})
                return NodeResult(
                    node_name=self.name,
                    status=NodeStatus.SUCCESS,
                    data={"read_value": data.get("value")},
                )

        engine.register(WorkflowDefinition(
            name="data_flow",
            nodes=[
                SimpleNode(name="producer", value="secret-payload"),
                ReaderNode(name="consumer"),
            ],
            edges=[("producer", "consumer")],
        ))

        result = await engine.run("data_flow", make_ctx())
        consumer_result = next(n for n in result.nodes if n.node_name == "consumer")
        assert consumer_result.data["read_value"] == "secret-payload"

    async def test_cancellation(self):
        engine = WorkflowEngine()
        ctx = make_ctx()

        engine.register(WorkflowDefinition(
            name="cancel_test",
            nodes=[
                SlowNode(name="slow1", delay=0.01),
                SlowNode(name="slow2", delay=0.01),
            ],
            edges=[("slow1", "slow2")],
        ))

        # Set cancellation after first node starts
        async def cancel_after_delay():
            await asyncio.sleep(0.005)
            ctx.cancellation_event.set()

        run_task = asyncio.create_task(engine.run("cancel_test", ctx))
        cancel_task = asyncio.create_task(cancel_after_delay())

        result = await run_task
        await cancel_task

        # At least one node executed; the second was skipped or the first completed
        statuses = [n.status for n in result.nodes]
        assert NodeStatus.SKIP in statuses or len(result.nodes) == 1


class TestWorkflowRegistry:
    def test_create_reflection(self):
        wf = WorkflowRegistry.create("reflection")
        assert wf.name == "reflection"
        assert wf.default is True
        assert len(wf.nodes) == 5
        node_names = [n.name for n in wf.nodes]
        assert node_names == [
            "schema_linking", "gen_sql", "execute_sql", "reflect", "output",
        ]

    def test_create_fixed(self):
        wf = WorkflowRegistry.create("fixed")
        assert wf.name == "fixed"
        assert len(wf.nodes) == 4

    def test_create_empty(self):
        wf = WorkflowRegistry.create("empty")
        assert wf.name == "empty"
        assert len(wf.nodes) == 1

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError):
            WorkflowRegistry.create("nonexistent")

    def test_list_available(self):
        assert "reflection" in WorkflowRegistry.list_available()
        assert "fixed" in WorkflowRegistry.list_available()
        assert "empty" in WorkflowRegistry.list_available()
