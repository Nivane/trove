"""Workflow engine — DAG node executor.

Drives the execution of a WorkflowDefinition:
  - Resolves node order from DAG edges
  - Executes nodes sequentially (or in parallel for ControlNode)
  - Handles cancellation propagation
  - Aggregates results into WorkflowResult
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from trove.core.types import (
    NodeStatus,
    WorkflowContext,
    WorkflowResult,
)
from trove.core.errors import CancelledError
from trove.core.logging import get_logger
from trove.workflow.node import Node, NodeResult, ControlNode
from trove.workflow.node_type import NodeType

logger = get_logger(__name__)


@dataclass
class WorkflowDefinition:
    """A workflow is a named DAG of nodes."""

    name: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    default: bool = False


class WorkflowEngine:
    """Executes workflows by traversing node DAGs.

    Supports:
      - Sequential node execution via topological order
      - Cancellation via ctx.cancellation_event
      - Retry loops within agentic nodes (gen_sql → fix → gen_sql)
      - Per-node timing and error collection
    """

    def __init__(self):
        self._workflows: dict[str, WorkflowDefinition] = {}

    def register(self, wf: WorkflowDefinition) -> None:
        """Register a workflow definition."""
        self._workflows[wf.name] = wf
        logger.debug("Registered workflow: %s (%d nodes)", wf.name, len(wf.nodes))

    def get(self, name: str) -> WorkflowDefinition:
        """Get a workflow definition by name."""
        if name not in self._workflows:
            raise KeyError(f"Workflow '{name}' not found. Available: {list(self._workflows.keys())}")
        return self._workflows[name]

    def list_names(self) -> list[str]:
        """List all registered workflow names."""
        return list(self._workflows.keys())

    async def run(
        self,
        workflow_name: str,
        ctx: WorkflowContext,
    ) -> WorkflowResult:
        """Execute a workflow.

        Args:
            workflow_name: Name of the registered workflow to run.
            ctx: The execution context.

        Returns:
            WorkflowResult with all node results and final output.
        """
        wf = self.get(workflow_name)
        total_start = time.monotonic()

        result = WorkflowResult(workflow_name=wf.name, trace_id=ctx.trace_id)

        # Resolve execution order from edges (topological sort)
        execution_order = self._topological_sort(wf)

        for node in execution_order:
            # Check cancellation before each node
            if ctx.cancellation_event.is_set():
                logger.info("Workflow '%s' cancelled before node '%s'", wf.name, node.name)
                result.nodes.append(NodeResult(
                    node_name=node.name,
                    status=NodeStatus.SKIP,
                    data={"reason": "cancelled"},
                ))
                break

            logger.debug("Executing node: %s", node.name)
            node_start = time.monotonic()

            try:
                node_result = await node.execute(ctx)
            except asyncio.CancelledError:
                node_result = NodeResult(
                    node_name=node.name,
                    status=NodeStatus.ERROR,
                    error=CancelledError(f"Node '{node.name}' cancelled"),
                )

            elapsed = (time.monotonic() - node_start) * 1000
            node_result.metadata["elapsed_ms"] = round(elapsed, 2)
            result.nodes.append(node_result)

            # Merge node data into context for downstream nodes
            if node_result.status == NodeStatus.SUCCESS:
                self._merge_node_data(ctx, node_result)

            # Handle retry logic
            if node_result.status == NodeStatus.RETRY:
                retry_target = node_result.data.get("retry_target")
                if retry_target:
                    logger.info("Retry requested for node: %s", retry_target)
                    retry_node = self._find_node(wf, retry_target)
                    if retry_node:
                        retry_result = await retry_node.execute(ctx)
                        result.nodes.append(retry_result)

            # On fatal error, stop the workflow
            if node_result.status == NodeStatus.ERROR and not self._is_recoverable(node_result):
                logger.error(
                    "Workflow '%s' stopped at node '%s': %s",
                    wf.name, node.name, node_result.error,
                )
                break

        # Extract final output from the output node
        output_node = self._find_node_by_type(wf, NodeType.OUTPUT)
        if output_node:
            for nr in result.nodes:
                if nr.node_name == output_node.name and nr.status == NodeStatus.SUCCESS:
                    result.final_output = nr.data.get("response", "")
                    break

        total_elapsed = (time.monotonic() - total_start) * 1000
        logger.info(
            "Workflow '%s' completed in %.0fms: %d nodes",
            wf.name, total_elapsed, len(result.nodes),
        )
        return result

    async def cancel(self, trace_id: str) -> None:
        """Signal cancellation for a running workflow.

        Args:
            trace_id: The trace ID of the workflow to cancel.
        """
        logger.info("Cancellation requested for trace: %s", trace_id)
        # The actual cancellation is propagated via ctx.cancellation_event
        # which is set by the caller before calling this method.

    # ── Internal helpers ─────────────────────────────────

    def _topological_sort(self, wf: WorkflowDefinition) -> list[Node]:
        """Sort nodes in execution order based on edges.

        If no edges are defined, preserves the original list order.
        With edges, performs a topological sort.
        """
        if not wf.edges:
            return list(wf.nodes)

        # Build adjacency and in-degree
        node_map = {n.name: n for n in wf.nodes}
        in_degree: dict[str, int] = {n.name: 0 for n in wf.nodes}
        adj: dict[str, list[str]] = {n.name: [] for n in wf.nodes}

        for src, dst in wf.edges:
            if src in adj and dst in in_degree:
                adj[src].append(dst)
                in_degree[dst] += 1

        # Kahn's algorithm
        queue = [name for name, deg in in_degree.items() if deg == 0]
        order = []

        while queue:
            current = queue.pop(0)
            order.append(node_map[current])
            for neighbor in adj.get(current, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # Add any disconnected nodes
        for node in wf.nodes:
            if node not in order:
                order.append(node)

        return order

    def _find_node(self, wf: WorkflowDefinition, name: str) -> Node | None:
        for node in wf.nodes:
            if node.name == name:
                return node
        return None

    def _find_node_by_type(self, wf: WorkflowDefinition, node_type: NodeType) -> Node | None:
        for node in wf.nodes:
            if node.node_type == node_type:
                return node
        return None

    def _merge_node_data(self, ctx: WorkflowContext, result: NodeResult) -> None:
        """Merge node output data into the workflow context.

        This allows downstream nodes to access upstream results.
        """
        if not hasattr(ctx, '_node_data'):
            ctx._node_data = {}  # type: ignore[attr-defined]
        ctx._node_data[result.node_name] = result.data  # type: ignore[attr-defined]

    def _is_recoverable(self, result: NodeResult) -> bool:
        """Determine if a node failure should stop the workflow.

        ERROR results always stop the workflow — recovery happens via
        explicit RETRY status, which is handled before this check.
        """
        return result.status != NodeStatus.ERROR
