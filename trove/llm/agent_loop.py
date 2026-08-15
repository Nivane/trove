"""Shared agent loop harness — model-driven termination with tool access.

Every agentic node (planner, gen_sql, reflect, …) runs this loop:
the model observes, calls tools, observes again, and the loop ends
when the model stops calling tools — i.e. when the model itself judges
its task done. max_rounds is a safety guard only, not a stopping rule.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from trove.core.logging import get_logger

logger = get_logger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


async def run_agent_loop(
    llm,
    model: str,
    system: str,
    user: str,
    tools: list[dict[str, Any]],
    tool_handlers: dict[str, ToolHandler],
    max_rounds: int = 8,
    metadata: dict[str, Any] | None = None,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Run a tool-calling loop until the model returns content without calls.

    Args:
        llm: Gateway with chat_full support.
        model: Model id.
        system: System prompt.
        user: User message.
        tools: Tool definitions (litellm format).
        tool_handlers: name → async handler(arguments) → observation text.
        max_rounds: Safety guard (not the stopping rule).
        metadata: Trace metadata passed to each LLM call.

    Returns:
        {"content": final model content, "rounds": N, "guard_hit": bool}
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    final_content = ""
    guard_hit = False
    tool_history: list[dict[str, Any]] = []

    for round_no in range(1, max_rounds + 1):
        response = await llm.chat_full(
            model=model, messages=messages, tools=tools, metadata=metadata,
            temperature=temperature,
        )
        content = response.get("content") or ""
        tool_calls = response.get("tool_calls") or []

        if not tool_calls:
            # 模型不再调用工具 = 模型认为任务完成
            final_content = content
            return {
                "content": final_content,
                "rounds": round_no,
                "guard_hit": False,
                "tool_history": tool_history,
            }

        messages.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            handler = tool_handlers.get(tc["name"])
            try:
                arguments = json.loads(tc["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            if handler is None:
                observation = f"Unknown tool: {tc['name']}"
            else:
                try:
                    observation = await handler(arguments)
                except Exception as e:
                    observation = f"Tool error: {e}"
            tool_history.append({"name": tc["name"], "arguments": arguments, "observation": observation})
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": observation,
            })

    # 护栏：模型持续调用工具未收敛——返回空内容并标记
    guard_hit = True
    logger.warning("Agent loop hit max_rounds guard (%d)", max_rounds)
    return {
        "content": final_content,
        "rounds": max_rounds,
        "guard_hit": True,
        "tool_history": tool_history,
    }
