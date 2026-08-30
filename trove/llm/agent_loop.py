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
from trove.services.errors import classify_error, validate_arguments

logger = get_logger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]
# (name, arguments, observation, elapsed_ms, error, run_id)
Observer = Callable[[str, dict[str, Any], str, float, str | None, str], None]

FINISH_TOOL = "finish"

# 注入 messages 的上下文护栏:单条工具观测超长即截断、轮次过多即丢弃
# 最早往返轮(Claude Code 式窗口)——tracing/tool_history 仍保留完整观测,
# 只有喂回模型的 messages 被压缩,避免 ReAct 循环内上下文复利爆炸。
MAX_OBSERVATION_CHARS = 800   # 单条工具观测注入上限
MAX_TOOL_TURNS = 8            # messages 保留的最近往返轮数


def _truncate_observation(obs: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    """超长工具观测截断:保留头部 + 截断标记(注入版;tracing 用全文)。"""
    obs = obs or ""
    if len(obs) <= limit:
        return obs
    return obs[:limit] + f"\n…[truncated {len(obs) - limit} chars]"


def _prune_old_rounds(
    messages: list[dict[str, Any]],
    keep_rounds: int = MAX_TOOL_TURNS,
) -> list[dict[str, Any]]:
    """丢弃最早的完整往返轮,保留最近 ``keep_rounds`` 轮。

    system/user(前两条)永不丢弃。以 assistant 消息为轮起点:某轮被
    丢弃时,其后的 tool 消息(到下一个 assistant 为止)一并丢弃,
    保证不出现孤儿 tool_call_id 或无人认领的 tool 结果。steering
    (user)消息随其所在早轮一并丢弃。

    返回裁剪后的新列表(原地不修改)。
    """
    if len(messages) <= 2:
        return messages
    tail = messages[2:]
    starts = [i for i, m in enumerate(tail) if m["role"] == "assistant"]
    if len(starts) <= keep_rounds:
        return messages
    keep_from = starts[-keep_rounds]
    return messages[:2] + tail[keep_from:]


# ── Tool spec & registry ──────────────────────────────────


class ToolSpec:
    """One registered tool: def (prompts to the model) + execution policy.

    parallel=False marks tools that must run before/after the parallel
    batch rather than inside it (ordering matters, e.g. ``finish``).

    level/roles — tool governance (ACL): ``level`` is an operational
    tier (core/catalog/admin, observability + default gating), ``roles``
    is the list of user roles allowed to see this tool. ``roles=None``
    means unrestricted (visible to every user); an empty list means the
    tool is registered for internal/legacy use only. Filtering happens in
    ``ToolRegistry.defs()``/``handlers()`` against the registry's
    ``allowed_roles``.
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
        level: str = "core",
        roles: list[str] | None = None,
    ):
        self.name = name
        self.func = func
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.timeout_s = timeout_s
        self.retries = retries
        self.retry_base_delay = retry_base_delay
        self.parallel = parallel
        self.level = level
        self.roles = list(roles) if roles is not None else None

    @property
    def restricted(self) -> bool:
        """True when this tool is gated behind a role (ACL-relevant)."""
        return self.roles is not None and len(self.roles) > 0

    def visible_to(self, allowed_roles: list[str] | None) -> bool:
        """角色可见性判定:无角色限制 → 恒可见;未启用过滤 → 恒可见;
        否则注册 roles 与 allowed 有交集才可见。

        allowed_roles None = 未启用角色过滤(旧行为,全部可见)。
        """
        if self.roles is None or not self.roles:
            return True
        if allowed_roles is None:
            return True
        return bool(set(self.roles) & set(allowed_roles))

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

    def __init__(self, *, finish: bool = False, allowed_roles: list[str] | None = None):
        self._specs: dict[str, ToolSpec] = {}
        self._finish_spec: ToolSpec | None = None
        self._observers: list[Observer] = [_trace_observer]
        self.allowed_roles = list(allowed_roles) if allowed_roles is not None else None
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
                "Stop exploring and submit your final answer as plain text "
                "in 'answer' (the SQL statement, plan JSON, or verdict text "
                "— exactly as it should be returned). Use when: you are ready "
                "to deliver, after any probe/check finalization. Do NOT use "
                "when: you still need to verify or fix the draft (call "
                "probe_query/check_result first). "
                "Example: finish(answer=\"SELECT COUNT(*) FROM students\"). "
                "Do not mix it with other tool calls."
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
            level="core",  # finish 是协议终止工具,所有角色必须可用
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
        """工具定义(按 allowed_roles 裁剪;finish 恒可见、置末)。"""
        visible = [
            s for s in self._specs.values()
            if s is not self._finish_spec and s.visible_to(self.allowed_roles)
        ]
        if self._finish_spec is not None:
            visible.append(self._finish_spec)
        return [s.def_dict() for s in visible]

    def handlers(self) -> dict[str, ToolHandler]:
        """工具处理器映射(按 allowed_roles 裁剪;finish 恒保留供 harness 拦截)。"""
        out: dict[str, ToolHandler] = {}
        for n, s in self._specs.items():
            if s is self._finish_spec or s.visible_to(self.allowed_roles):
                out[n] = s.func
        return out


async def _finish_handler(arguments: dict[str, Any]) -> str:
    ok, msg = ToolRegistry.validate_finish(arguments)
    return msg if ok else msg  # 常态由 harness 拦截,此兜底仅作非协议路径


def _trace_observer(
    name: str, arguments: dict[str, Any], observation: str,
    elapsed_ms: float, error: str | None, run_id: str,
) -> None:
    """Default observer: 工具调用挂到当前节点 span 下(本地 runlog + langfuse)。

    两个通道共享同一插桩点:runlog 保真全量,langfuse 记截断的
    证据(嵌套进当前节点 span,contextvar 传播)。
    """
    try:
        from trove.tracing.runlog import get_tracer
        tracer = get_tracer(run_id) if run_id else None
        if tracer is not None:
            tracer.tool(name, arguments, observation)
    except Exception:
        pass
    try:
        from trove.llm.observability import record_tool_call
        record_tool_call(name, arguments, observation, error)
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
    cache_prefix: str | None = None,
    prompt_caching: bool = True,
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
        cache_prefix: Byte-stable prefix of ``user`` (e.g. dialect+schema).
            When given (and ``prompt_caching``), the system message becomes
            a content-block list, ``user`` splits into [prefix+cache_control,
            volatile remainder], and the last tool definition gets
            cache_control — Anthropic ephemeral breakpoints, so repeated
            calls with the same prefix skip re-processing the stable part.
            Callers without a stable prefix keep messages byte-identical
            (planner/reflect etc. pass None and are unaffected).
        prompt_caching: Master switch for the breakpoint markers above.
            Providers without explicit caching (OpenAI etc.) have the
            markers stripped by the gateway — behavior-equivalent, no
            caching benefit.

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

    # ① Prompt caching:cache_prefix 为 user 的真实字节前缀时,system 切成
    # 内容块列表打 ephemeral 断点,user 拆成 [稳定前缀块+断点, volatile
    # 剩余块],最后一个工具定义也打断点(Anthropic 工具级缓存)。前缀不
    # 匹配(模板演化/防御)或恰好覆盖全部 user(无剩余块)时保持原样——
    # 字节级不变,调用方无感。
    if cache_prefix and prompt_caching and user and user.startswith(cache_prefix) and user != cache_prefix:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": [
                {"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": cache_prefix,
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": user[len(cache_prefix):]},
            ]},
        ]
        if tool_defs:
            last = dict(tool_defs[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tool_defs = tool_defs[:-1] + [last]
    else:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    final_content = ""
    guard_hit = False
    budget_why: str | None = None
    tool_history: list[dict[str, Any]] = []
    # 思考��迹:每轮的模型文本+工具调用+观测,供回退修正上下文使用
    transcript_parts: list[str] = []
    reasoning_parts: list[str] = []
    steering_hits: list[str] = []
    total_tokens = 0
    completion_tokens = 0
    first_input_tokens = 0  # 首轮 prompt_tokens(调用方做估算校准用)
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
                "observation": f"[ERR:ARGS_SCHEMA] Unknown tool: {name}",
                "elapsed_ms": 0.0, "error": None, "finish_ok": False,
            }
        # 角色裁剪:模型不该调用不可见工具(不应出现在 defs);万一调用,
        # 按 unknown 折叠回喂,不执行。
        if not spec.visible_to(registry.allowed_roles):
            return {
                "tc": tc, "arguments": arguments,
                "observation": f"[ERR:ARGS_SCHEMA] Unknown tool: {name}",
                "elapsed_ms": 0.0, "error": None, "finish_ok": False,
            }
        # 参数校验防火墙(确定性,零执行):模型拼错参数重跑工具必死。
        # 分派前拦下,观测带 [ERR:ARGS_SCHEMA] 回喂模型修正参数本身。
        param_problems = validate_arguments(spec.parameters, arguments)
        if param_problems:
            issue = "; ".join(param_problems)
            return {
                "tc": tc, "arguments": arguments,
                "observation": f"[ERR:ARGS_SCHEMA] invalid arguments: {issue}",
                "elapsed_ms": 0.0,
                "error": f"ARGS_SCHEMA: {issue}", "finish_ok": False,
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
                    "observation": (
                        f"[ERR:TOOL_TIMEOUT] Tool timed out after {timeout}s: {name}"
                    ),
                    "elapsed_ms": (time.monotonic() - start) * 1000,
                    "error": f"TOOL_TIMEOUT: timed out after {timeout}s",
                    "finish_ok": False,
                }
            except Exception as e:
                # 错误分类决定是否值得重试:瞬时/连接类(exc 强信号或词典)才
                # 消耗 spec.retries;python bug / 永久错误直接折叠,不白烧预算。
                verdict = classify_error(str(e), exc=e, context="tool")
                if attempt < spec.retries and verdict.retryable:
                    attempt += 1
                    await asyncio.sleep(spec.retry_base_delay * (2 ** (attempt - 1)))
                    continue
                return {
                    "tc": tc, "arguments": arguments,
                    "observation": f"{verdict.tag()} Tool error: {e}",
                    "elapsed_ms": (time.monotonic() - start) * 1000,
                    "error": f"{verdict.cls.id}: {e}", "finish_ok": False,
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
            "first_input_tokens": first_input_tokens or None,
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
        completion_tokens += int(usage.get("completion_tokens") or 0)
        if round_no == 1:
            first_input_tokens = int(usage.get("prompt_tokens") or 0)
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
        # 预算针对"模型输出"计(completion),prompt 每轮回灌增长不计入,
        # 否则预算会在大上下文首轮即误触。
        if max_total_tokens is not None and completion_tokens >= max_total_tokens:
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

        # 自动定稿:本轮无显式 finish,但 check_result 已返回通过观测
        # ("OK (N rows)" —— 确定性规则链在真实数据上验证过草稿)。check_result
        # 的工具契约是"AFTER probe, BEFORE finalizing",模型调它就是要定稿;
        # harness 替模型按 finish 协议定稿,省掉"再调一轮 finish"的 LLM 调用。
        # 逆序只看到最近一次 check_result(决定性信号):它是 VIOLATION →
        # 模型仍在修正,违例观测回喂循环继续;是 "OK (" → 定稿。
        # 0 行也算通过(规则链放行;空结果由下游 EMPTY/reflect 兜底)。
        if finish_ok is None:
            for res in reversed(results):
                if res["tc"]["name"] != "check_result":
                    continue
                payload = str(res["arguments"].get("sql", ""))
                if (
                    not str(res["observation"]).startswith("OK (")
                    or not payload
                ):
                    break
                final_content = payload
                tool_history.append({
                    "name": "check_result", "arguments": res["arguments"],
                    "observation": res["observation"],
                })
                transcript_parts.append(
                    f"[tool:check_result] {str(res['arguments'])[:150]} -> {res['observation'][:150]}"
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": res["tc"]["id"],
                    "content": _truncate_observation(res["observation"]),
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
                "content": _truncate_observation(observation),
            })
            recent_sigs.append(
                f"{tc['name']}|"
                + json.dumps(sorted(res["arguments"].items()), ensure_ascii=False, default=str)
            )

        # 上下文窗口护栏:丢弃最早往返轮,防 ReAct 循环内消息复利爆炸
        messages = _prune_old_rounds(messages)

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