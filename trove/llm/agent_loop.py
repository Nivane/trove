"""Shared agent loop harness — model-driven termination with tool access.

Every agentic node (planner, gen_sql, reflect, …) runs this loop:
the model observes, calls tools, observes again, and the loop ends
when the model stops calling tools — i.e. when the model itself judges
its task done. max_rounds is a safety guard only, not a stopping rule.

Harness design (single place, configured per call-site):
  - ToolRegistry: centralized registration — defs + handlers + per-tool
    timeout / retry / parallel-eligibility, plus observer middleware
    hooks (tracing/cost/metrics are cross-cutting concerns, not loop code).
  - finish tool protocol: the model finalizes through an explicit
    ``finish(answer)`` tool whose payload is validated, instead of the
    implicit "empty tool_calls = done" convention (which lets the final
    answer be lost when the model never echoes it into content).
  - Parallel tool dispatch: read-only tools in one round run
    concurrently (asyncio.gather), while non-parallel tools run first.
  - Budgets: rounds / wall-clock time_budget_s / cumulative total tokens
    (LLM responses report usage). Exceeding one sets guard_hit with a
    budget_why; the caller decides how to degrade.
  - Loop steering: N identical consecutive tool calls get a steering
    message instead of silently spinning until the guard.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from trove.core.logging import get_logger

logger = get_logger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]
# (name, arguments, observation, elapsed_ms, error, run_id)
Observer = Callable[[str, dict[str, Any], str, float, str | None, str], None]

FINISH_TOOL = "finish"


# ── Tool spec & registry ──────────────────────────────────


class ToolSpec:
    """One registered tool: def (prompts to the model) + execution policy.

    parallel=False marks tools that must run before/after the parallel
    batch rather than inside it (ordering matters, e.g. ``finish``).
    """

    def __init__(
        self,
        name: str,
        func: ToolHandler,
        *,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        retries: int = 0,
        retry_base_delay: float = 0.5,
        parallel: bool = True,
    ):
        self.name = name
        self.func = func
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.timeout_s = timeout_s
        self.retries = retries
        self.retry_base_delay = retry_base_delay
        self.parallel = parallel

    def def_dict(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Center of tool matter for one agent round.

    register(): handler + def + per-tool timeout/retries/parallel policy.
    add_observer(): cross-cutting hook (metrics/cost/tracing); called with
        (name, arguments, observation, elapsed_ms, error, run_id) after each
        executed tool call. A default tracing observer is always present.
    finish protocol: constructed with finish=True, the registry exposes a
        built-in ``finish(answer)`` tool; the harness intercepts it, accepts
        the payload only when 'answer' is a non-empty string, and terminates.
    """

    def __init__(self, *, finish: bool = False):
        self._specs: dict[str, ToolSpec] = {}
        self._finish_spec: ToolSpec | None = None
        self._observers: list[Observer] = [_trace_observer]
        if finish:
            self.add_finish_tool()

    def register(
        self, name: str, func: ToolHandler, **kwargs: Any,
    ) -> ToolSpec:
        spec = ToolSpec(name, func, **kwargs)
        self._specs[name] = spec
        return spec

    def add_finish_tool(self) -> ToolSpec:
        spec = ToolSpec(
            FINISH_TOOL,
            _finish_handler,
            description=(
                "Stop exploring and submit your final answer. Call ONLY when you "
                "are ready to deliver: pass the final answer as plain text in "
                "'answer' (the SQL statement, plan JSON, or verdict text — exactly "
                "as it should be returned). Do not mix it with other tool calls."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "The final answer text to return.",
                    },
                },
                "required": ["answer"],
            },
            parallel=False,
        )
        # finish 定义置于工具列表末尾(注册序靠后),避免抢占注意力
        self._finish_spec = spec
        self._specs[FINISH_TOOL] = spec
        return spec

    def add_observer(self, fn: Observer) -> None:
        self._observers.append(fn)

    def observers(self) -> list[Observer]:
        return list(self._observers)

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def is_finish(self, name: str) -> bool:
        return self._finish_spec is not None and name == FINISH_TOOL

    @property
    def has_finish(self) -> bool:
        return self._finish_spec is not None

    @staticmethod
    def validate_finish(arguments: dict[str, Any]) -> tuple[bool, str]:
        answer = (arguments or {}).get("answer")
        if isinstance(answer, str) and answer.strip():
            return True, answer.strip()
        return False, (
            "finish requires a non-empty 'answer' string — put your final answer "
            "there and call finish alone (not mixed with other tools)."
        )

    def defs(self) -> list[dict[str, Any]]:
        ordered = [s for s in self._specs.values() if s is not self._finish_spec]
        if self._finish_spec is not None:
            ordered.append(self._finish_spec)
        return [s.def_dict() for s in ordered]

    def handlers(self) -> dict[str, ToolHandler]:
        return {n: s.func for n, s in self._specs.items()}


async def _finish_handler(arguments: dict[str, Any]) -> str:
    ok, msg = ToolRegistry.validate_finish(arguments)
    return msg if ok else msg  # 常态由 harness 拦截,此兜底仅作非协议路径


def _trace_observer(
    name: str, arguments: dict[str, Any], observation: str,
    elapsed_ms: float, error: str | None, run_id: str,
) -> None:
    """Default observer: 工具调用挂到当前节点 span 下(详尽日志/诊断)。"""
    if not run_id:
        return
    try:
        from trove.tracing.runlog import get_tracer
        tracer = get_tracer(run_id)
        if tracer is not None:
            tracer.tool(name, arguments, observation)
    except Exception:
        pass


# ── Agent loop ────────────────────────────────────────────


async def run_agent_loop(
    llm,
    model: str,
    system: str,
    user: str,
    tools: list[dict[str, Any]] | None = None,
    tool_handlers: dict[str, ToolHandler] | None = None,
    registry: ToolRegistry | None = None,
    max_rounds: int = 8,
    metadata: dict[str, Any] | None = None,
    temperature: float = 0.0,
    tool_timeout_s: float | None = None,
    llm_timeout_s: float | None = None,
    time_budget_s: float | None = None,
    max_total_tokens: int | None = None,
    steering_window: int = 3,
) -> dict[str, Any]:
    """Run a tool-calling loop until the model returns content without calls.

    Args:
        llm: Gateway with chat_full support.
        model: Model id.
        system: System prompt.
        user: User message.
        tools: Tool definitions (litellm format) — legacy form; when
            ``registry`` is given the registry supplies the defs.
        tool_handlers: name → async handler(arguments) → observation text
            — legacy form; ignored when ``registry`` is given.
        registry: ToolRegistry with defs/handlers/timeouts/observers and
            optionally the finish protocol. Preferred over tools/handlers.
        max_rounds: Safety guard (not the stopping rule).
        metadata: Trace metadata passed to each LLM call.
        temperature: Sampling temperature.
        tool_timeout_s: Default timeout for tool handlers without one.
        llm_timeout_s: Per-LLM-call timeout (raises on expiry → caller
            decides degradation).
        time_budget_s: Total wall-clock budget; exceeded → guard_hit.
        max_total_tokens: Cumulative LLM output token budget (needs ``usage``
            in chat_full responses); exceeded → guard_hit.
        steering_window: N identical consecutive tool calls trigger a
            steering message instead of spinning until the guard.

    Returns:
        {"content", "rounds", "guard_hit", "finish_tool", "budget_why",
         "steering_hits", "tool_calls", "total_tokens", "tool_history",
         "transcript", "reasoning"}
    """
    own_registry = registry is None
    if registry is None:
        # 旧式 tools/handlers:包装成注册表,保留工具定义描述
        registry = ToolRegistry()
        details = _def_details(tools)
        for name, fn in (tool_handlers or {}).items():
            desc, params = details.get(name, ("", None))
            registry.register(name, fn, description=desc, parameters=params)
    tool_defs = tools if (own_registry and tools) else registry.defs()
    run_id = (metadata or {}).get("run_id", "")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    final_content = ""
    guard_hit = False
    budget_why: str | None = None
    tool_history: list[dict[str, Any]] = []
    # 思考痕迹:每轮的模型文本+工具调用+观测,供回退修正上下文使用
    transcript_parts: list[str] = []
    reasoning_parts: list[str] = []
    steering_hits: list[str] = []
    total_tokens = 0
    start_time = time.monotonic()
    recent_sigs: list[str] = []
    consumed_rounds = 0

    async def _chat_once() -> dict[str, Any]:
        return await llm.chat_full(
            model=model, messages=messages, tools=tool_defs,
            metadata=metadata, temperature=temperature,
        )

    async def _run_one(tc: dict[str, Any]) -> dict[str, Any]:
        """单个工具调用执行:参数解析、finish 拦截、超时/重试、错误折叠。"""
        name = tc["name"]
        try:
            arguments = json.loads(tc["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if registry.is_finish(name):
            ok, payload = registry.validate_finish(arguments)
            if ok:
                return {
                    "tc": tc, "arguments": arguments, "observation": payload,
                    "elapsed_ms": 0.0, "error": None, "finish_ok": True,
                }
            return {
                "tc": tc, "arguments": arguments, "observation": payload,
                "elapsed_ms": 0.0, "error": payload, "finish_ok": False,
            }
        spec = registry.spec(name)
        if spec is None:
            return {
                "tc": tc, "arguments": arguments,
                "observation": f"Unknown tool: {name}",
                "elapsed_ms": 0.0, "error": None, "finish_ok": False,
            }
        timeout = spec.timeout_s if spec.timeout_s else tool_timeout_s
        start = time.monotonic()
        attempt = 0
        while True:
            try:
                coro = spec.func(arguments)
                if timeout:
                    observation = await asyncio.wait_for(coro, timeout)
                else:
                    observation = await coro
                return {
                    "tc": tc, "arguments": arguments, "observation": observation,
                    "elapsed_ms": (time.monotonic() - start) * 1000,
                    "error": None, "finish_ok": False,
                }
            except TimeoutError:
                return {
                    "tc": tc, "arguments": arguments,
                    "observation": f"Tool timed out after {timeout}s: {name}",
                    "elapsed_ms": (time.monotonic() - start) * 1000,
                    "error": f"timed out after {timeout}s", "finish_ok": False,
                }
            except Exception as e:
                if attempt < spec.retries:
                    attempt += 1
                    await asyncio.sleep(spec.retry_base_delay * (2 ** (attempt - 1)))
                    continue
                return {
                    "tc": tc, "arguments": arguments,
                    "observation": f"Tool error: {e}",
                    "elapsed_ms": (time.monotonic() - start) * 1000,
                    "error": str(e), "finish_ok": False,
                }

    def _finish_result(rounds: int, answered: bool) -> dict[str, Any]:
        return {
            "content": final_content,
            "rounds": rounds,
            "guard_hit": guard_hit,
            "finish_tool": answered,
            "budget_why": budget_why,
            "steering_hits": steering_hits,
            "tool_calls": len(tool_history),
            "total_tokens": total_tokens or None,
            "tool_history": tool_history,
            "transcript": "\n".join(transcript_parts),
            "reasoning": "\n".join(reasoning_parts)[:1000],
        }

    for round_no in range(1, max_rounds + 1):
        consumed_rounds = round_no
        if time_budget_s is not None and (time.monotonic() - start_time) > time_budget_s:
            guard_hit = True
            budget_why = "time"
            logger.warning("Agent loop hit time budget (%.1fs)", time_budget_s)
            break

        chat_coro = _chat_once()
        if llm_timeout_s:
            response = await asyncio.wait_for(chat_coro, llm_timeout_s)
        else:
            response = await chat_coro
        content = response.get("content") or ""
        tool_calls = response.get("tool_calls") or []
        reasoning = response.get("reasoning") or ""
        usage = response.get("usage") or {}
        total_tokens += int(usage.get("total_tokens") or 0)
        if reasoning:
            reasoning_parts.append(reasoning)

        # 每轮累积非空文本：模型常在同一回复里既给 content 又调工具
        # （如 DeepSeek）。护栏命中时，最后说过的话比空串有用得多。
        if content:
            final_content = content
            transcript_parts.append(f"[assistant] {content[:300]}")

        if not tool_calls:
            # 模型不再调用工具 = 模型认为任务完成
            return _finish_result(round_no, answered=False)

        # token 预算已耗尽:不再执行更多工具,直接按护栏结束
        if max_total_tokens is not None and total_tokens >= max_total_tokens:
            guard_hit = True
            budget_why = "tokens"
            logger.warning(
                "Agent loop hit token budget (%d>=%d)", total_tokens, max_total_tokens,
            )
            break

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

        # 工具分发:finish 最先(finish_ok 即终止);并行批独立工具并发执行;
        # 非并行工具(parallel=False)串行等待,保证相对次序。
        finish_tcs = [tc for tc in tool_calls if registry.is_finish(tc["name"])]
        batch = [
            tc for tc in tool_calls
            if tc not in finish_tcs and registry.spec(tc["name"]) is not None
            and registry.spec(tc["name"]).parallel
        ]
        rest = [tc for tc in tool_calls if tc not in finish_tcs and tc not in batch]

        results: list[dict[str, Any]] = []
        if finish_tcs:
            results.append(await _run_one(finish_tcs[0]))
        for extra in finish_tcs[1:]:
            results.append({
                "tc": extra, "arguments": {}, "observation": "ignored (finish already called this round)",
                "elapsed_ms": 0.0, "error": None, "finish_ok": False,
            })
        if batch:
            results.extend(await asyncio.gather(*(_run_one(tc) for tc in batch)))
        for tc in rest:
            results.append(await _run_one(tc))

        finish_ok = next((r for r in results if r["finish_ok"]), None)
        if finish_ok is not None:
            # finish 定稿:载荷即为最终答案,立即终止,不再消耗下一轮
            final_content = finish_ok["observation"]
            transcript_parts.append(f"[tool:finish] {final_content[:300]}")
            tool_history.append({
                "name": FINISH_TOOL, "arguments": finish_ok["arguments"],
                "observation": final_content,
            })
            return _finish_result(round_no, answered=True)

        by_id = {id(r["tc"]): r for r in results}
        for tc in tool_calls:
            res = by_id.get(id(tc))
            if res is None:
                continue
            observation = res["observation"]
            tool_history.append({
                "name": tc["name"], "arguments": res["arguments"],
                "observation": observation,
            })
            transcript_parts.append(
                f"[tool:{tc['name']}] {str(res['arguments'])[:150]} -> {observation[:150]}"
            )
            for obs_fn in registry.observers():
                try:
                    obs_fn(tc["name"], res["arguments"], observation,
                           res["elapsed_ms"], res["error"], run_id)
                except Exception:
                    pass
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": observation,
            })
            recent_sigs.append(
                f"{tc['name']}|"
                + json.dumps(sorted(res["arguments"].items()), ensure_ascii=False, default=str)
            )

        # 循环转向:连续相同调用 → 注入转向消息,而不是干等到护栏
        if len(recent_sigs) >= steering_window:
            window = recent_sigs[-steering_window:]
            if all(s == window[0] for s in window):
                steer = (
                    f"[steering] You have executed the identical tool call "
                    f"`{window[0].split('|')[0]}` with identical arguments "
                    f"{steering_window} consecutive times without progress. Stop "
                    "repeating it. Change approach based on the observations above "
                    "— or finalize with the finish tool."
                )
                messages.append({"role": "user", "content": steer})
                steering_hits.append(steer)
                logger.info("Agent loop steering injected (%s)", window[0].split("|")[0])

    # 护栏:模型持续调用工具未收敛——返回累积内容并标记原因
    guard_hit = True
    budget_why = budget_why or "rounds"
    logger.warning("Agent loop guard hit (%s, %d rounds)", budget_why, max_rounds)
    return _finish_result(consumed_rounds or max_rounds, answered=False)


def _def_details(tools: list[dict[str, Any]] | None) -> dict[str, tuple[str, dict]]:
    """legacy tools 定义里按名抽取 description / parameters。"""
    out: dict[str, tuple[str, dict]] = {}
    for d in tools or []:
        fn = d.get("function") or {}
        name = fn.get("name", "")
        if not name:
            continue
        out[name] = (
            fn.get("description", ""),
            fn.get("parameters", {"type": "object", "properties": {}}),
        )
    return out