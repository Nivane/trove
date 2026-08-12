"""Node abstractions for the workflow engine.

Three categories:
  1. Node (ABC) — base for all nodes
  2. AgenticNode — nodes with an internal LLM loop (gen_sql, reflect)
  3. ControlNode — pure orchestration nodes (parallel, selection, subworkflow)
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from trove.core.types import NodeStatus, WorkflowContext
from trove.workflow.node_type import NodeType


# ── Node Result ──────────────────────────────────────────


@dataclass
class NodeResult:
    """Result produced by a single workflow node execution."""

    node_name: str
    status: NodeStatus
    data: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Common metadata keys: elapsed_ms, token_usage_prompt,
    # token_usage_completion, tool_calls_count


# ── Base Node ────────────────────────────────────────────


class Node(ABC):
    """Abstract base class for all workflow nodes.

    Every node has:
      - A unique node_type used for registration/factory lookup
      - A human-readable name for logging and tracing
      - An execute(ctx) method that processes a WorkflowContext
    """

    node_type: NodeType

    def __init__(self, name: str = ""):
        self.name = name or self.node_type.value

    @abstractmethod
    async def execute(self, ctx: WorkflowContext) -> NodeResult:
        """Execute the node logic.

        Args:
            ctx: The workflow execution context (session, config, trace_id, etc.)

        Returns:
            NodeResult indicating success, error, retry, or skip.
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"


# ── Agentic Node ─────────────────────────────────────────


@dataclass
class LLMLoopConfig:
    """Configuration for an AgenticNode's internal LLM loop."""

    max_rounds: int = 5
    tools: list[str] = field(default_factory=list)
    model_override: str = ""
    system_prompt: str = ""


class AgenticNode(Node):
    """A node that contains an internal LLM reasoning loop.

    Agentic nodes can:
      - Call the LLM multiple times
      - Use tools (database queries, knowledge retrieval)
      - Decide when to stop their internal loop

    Subclasses override _build_prompt() and _process_llm_response()
    to customize the LLM loop behavior.

    The base implementation provides a single-round LLM call.
    """

    def __init__(self, name: str = "", llm_loop_config: LLMLoopConfig | None = None):
        super().__init__(name)
        self.llm_config = llm_loop_config or LLMLoopConfig()

    async def execute(self, ctx: WorkflowContext) -> NodeResult:
        """Default agentic execution: one LLM round.

        Subclasses typically override this for multi-round logic.
        """
        try:
            prompt = self._build_prompt(ctx)
            response = await self._call_llm(ctx, prompt)
            return self._process_response(ctx, response)
        except asyncio.CancelledError:
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.ERROR,
                error=asyncio.CancelledError("Node cancelled"),
                data={"cancelled": True},
            )
        except Exception as e:
            return NodeResult(
                node_name=self.name,
                status=NodeStatus.ERROR,
                error=e,
            )

    def _build_prompt(self, ctx: WorkflowContext) -> str:
        """Build the prompt for the LLM call.

        Subclasses override this with domain-specific prompt construction.

        Args:
            ctx: The workflow context.

        Returns:
            The prompt string to send to the LLM.
        """
        return ctx.user_message.content

    async def _call_llm(self, ctx: WorkflowContext, prompt: str) -> str:
        """Make the actual LLM call.

        Uses the LLMGateway configured in the context.
        In test mode, this returns a mock response.

        Args:
            ctx: Workflow context.
            prompt: The prompt to send.

        Returns:
            LLM response text.
        """
        model = self.llm_config.model_override or ctx.config.target or "openai/gpt-4o"

        # Access the LLM gateway from the context config
        llm = getattr(ctx.config, "_llm_gateway", None)
        if llm is None:
            # Fallback: create a direct mock call
            return f"[LLM Response for: {prompt[:100]}...]"

        messages = [
            {"role": "system", "content": self.llm_config.system_prompt},
            {"role": "user", "content": prompt},
        ]
        return await llm.chat(model=model, messages=messages)

    def _process_response(self, ctx: WorkflowContext, response: str) -> NodeResult:
        """Process the LLM response into a NodeResult.

        Subclasses override this to parse domain-specific output.

        Args:
            ctx: Workflow context.
            response: Raw LLM response text.

        Returns:
            NodeResult.
        """
        return NodeResult(
            node_name=self.name,
            status=NodeStatus.SUCCESS,
            data={"response": response},
        )


# ── Control Node ─────────────────────────────────────────


class ControlNode(Node):
    """Pure orchestration node — no LLM calls.

    Control nodes manage the flow of child nodes:
      - Parallel: execute children concurrently
      - Selection: conditional branching
      - Subworkflow: delegate to another workflow
    """

    def __init__(
        self,
        name: str = "",
        children: list[Node] | None = None,
    ):
        super().__init__(name)
        self.children = children or []
