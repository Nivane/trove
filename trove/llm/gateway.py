"""LLM Gateway — unified interface to LiteLLM.

Supports:
- chat/completion with any LiteLLM-supported provider
- Streaming responses via async generator
- Mock mode for testing (no real API calls)
- Automatic retry with exponential backoff
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any, AsyncIterator

from trove.core.config import ProviderConfig
from trove.core.errors import LLMError
from trove.core.logging import get_logger
from trove.services.errors import classify_error
from trove.llm.observability import get_client
from trove.llm.call_log import record_call

logger = get_logger(__name__)


def _extract_retry_after(exc: Exception | None) -> float | None:
    """Best-effort server retry-after hint (429/限流):秒数或 None。

    litellm 异常的 ``retry_after`` 字段、HTTP ``Retry-After`` 头(秒数或
    HTTP-date)、以及 ``status_code==429`` 无头时的保守等待值。失败静默
    返回 None(由调用方按指数退避兜底)。
    """
    if exc is None:
        return None
    try:
        ra = getattr(exc, "retry_after", None)
        if isinstance(ra, (int, float)) and ra > 0:
            return float(ra)
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None) or {}
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw and str(raw).isdecimal():
            return float(raw)
        if getattr(exc, "status_code", None) == 429:
            return 5.0
    except Exception:
        pass
    return None


class LLMGateway:
    """Unified gateway for LLM calls via LiteLLM.

    Usage:
        gateway = LLMGateway()
        response = await gateway.chat(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "Hello"}],
        )

        # Streaming:
        async for chunk in gateway.chat_stream(model=..., messages=...):
            print(chunk)

        # Mock mode for tests:
        gateway = LLMGateway(mock_response="SELECT * FROM district")
    """

    def __init__(
        self,
        mock_response: str | None = None,
        mock_stream_chunks: list[str] | None = None,
        mock_embedding: list[list[float]] | None = None,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        providers: list[ProviderConfig] | None = None,
    ):
        """Initialize the LLM gateway.

        Args:
            mock_response: If set, chat() returns this string without calling LLM.
            mock_stream_chunks: If set, chat_stream() yields these chunks.
            mock_embedding: If set, embedding() returns these vectors (per
                input, cycled) without calling the LLM provider.
            max_retries: Maximum retry attempts on failure.
            retry_base_delay: Base delay in seconds for exponential backoff.
            providers: Provider configs from agent.yml; litellm_params are
                merged into every litellm call whose model matches the
                provider name prefix (explicit api_key/api_base args win).
        """
        self._mock_response = mock_response
        self._mock_stream_chunks = mock_stream_chunks
        self._mock_embedding = mock_embedding
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._providers = providers or []

    def set_providers(self, providers: list[ProviderConfig] | None) -> None:
        """Hot-swap provider params (admin settings update — no restart)."""
        self._providers = providers or []

    # ── Public API ───────────────────────────────────────

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 16000,
        api_key: str | None = None,
        api_base: str | None = None,
        metadata: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Send a chat request and return the full response text.

        Args:
            model: LiteLLM model identifier (e.g. "openai/gpt-4o").
            messages: List of message dicts with "role" and "content".
            temperature: Sampling temperature (default 0 for deterministic).
            max_tokens: Maximum tokens in the response.
            api_key: Optional provider API key override.
            api_base: Optional provider API base override.
            response_format: Optional structured-output hint passed to the
                provider (e.g. {"type": "json_object"}) to replace fragile
                text parsing. Mock mode ignores it.

        Returns:
            The full response text from the LLM.

        Raises:
            LLMError: On any failure after retries are exhausted.
        """
        if self._mock_response is not None:
            logger.debug("Returning mock response (%d chars)", len(self._mock_response))
            return self._mock_response

        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                return await self._call_litellm(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    api_key=api_key,
                    api_base=api_base,
                    stream=False,
                    metadata=metadata,
                    response_format=response_format,
                )
            except Exception as e:
                last_error = e
                # 错误分类决定重试策略:瞬时(429/5xx/连接)才值得等退避重试;
                # 认证/模型不存在/上下文超窗等永久错误直接放弃,不烧重试次数。
                verdict = classify_error(str(e), exc=e, context="llm")
                if not verdict.retryable or attempt >= self.max_retries - 1:
                    break
                delay = (
                    _extract_retry_after(e)
                    if verdict.cls.retry_after else None
                )
                if delay is None:
                    delay = self.retry_base_delay * (2 ** attempt)
                logger.warning(
                    "LLM call attempt %d/%d failed: %s (%s). Retrying in %.1fs...",
                    attempt + 1, self.max_retries, e, verdict.describe(), delay,
                )
                await asyncio.sleep(delay)

        _record_failed_generation(model, messages, last_error, metadata)
        raise LLMError(
            message=f"LLM call failed after {self.max_retries} attempts: {last_error}",
            model=model,
            retry_after=_extract_retry_after(last_error),
        )

    async def chat_full(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 16000,
        metadata: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Chat with tool-calling support; returns the full message.

        Returns:
            {"content": str, "tool_calls": [{"id", "name", "arguments"}]}
        """
        if self._mock_response is not None:
            return {"content": self._mock_response, "tool_calls": []}

        import litellm
        start = time.monotonic()
        try:
            kwargs: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            if response_format:
                kwargs["response_format"] = response_format
            if metadata:
                kwargs["metadata"] = metadata

            response = await litellm.acompletion(**kwargs)
            message = response.choices[0].message
            tool_calls = []
            for tc in getattr(message, "tool_calls", None) or []:
                tool_calls.append({
                    "id": getattr(tc, "id", ""),
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                })
            elapsed_ms = int((time.monotonic() - start) * 1000)
            reasoning = getattr(message, "reasoning_content", "") or ""
            content = message.content or ""
            _record_generation(model, messages, response, metadata)
            _record_local_call(
                model, messages, content, metadata, elapsed_ms,
                temperature=temperature, reasoning=reasoning,
                usage=_usage_dict(response),
            )
            if not content and reasoning:
                # 与 _call_litellm 同策略:纯推理输出回退到 reasoning 正文
                finish = getattr(response.choices[0], "finish_reason", "") or ""
                logger.info(
                    "LLM returned empty content with reasoning (%d chars, "
                    "finish_reason=%s); falling back to reasoning text",
                    len(reasoning), finish,
                )
                content = reasoning
            return {
                "content": content,
                "tool_calls": tool_calls,
                "reasoning": reasoning,
                "usage": _usage_dict(response),
            }
        except Exception as e:
            _record_failed_generation(model, messages, e, metadata)
            raise LLMError(message=f"LLM call failed: {e}", model=model) from e

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 16000,
        api_key: str | None = None,
        api_base: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Send a chat request and stream response tokens.

        Args:
            model: LiteLLM model identifier.
            messages: List of message dicts.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens.
            api_key: Optional API key.
            api_base: Optional API base.

        Yields:
            Response text chunks as they arrive.

        Raises:
            LLMError: On failure.
        """
        if self._mock_stream_chunks is not None:
            for chunk in self._mock_stream_chunks:
                yield chunk
            return

        if self._mock_response is not None:
            yield self._mock_response
            return

        try:
            async for chunk in self._call_litellm_stream(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                api_key=api_key,
                api_base=api_base,
                metadata=metadata,
            ):
                yield chunk
        except Exception as e:
            raise LLMError(
                message=f"LLM streaming failed: {e}",
                model=model,
            ) from e

    async def embedding(
        self,
        model: str,
        inputs: list[str],
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> list[list[float]]:
        """Embed a batch of texts via litellm.aembedding.

        Mock mode (``mock_embedding`` set) returns the mocked vectors
        cycled per input, so test harnesses get a deterministic,
        network-free embedding path.

        Returns:
            One L2-normalized vector per input text.

        Raises:
            LLMError: On provider failure.
        """
        if self._mock_embedding is not None:
            n = len(self._mock_embedding)
            return [
                [float(x) for x in self._mock_embedding[i % n]]
                for i in range(len(inputs))
            ]

        import litellm

        kwargs = self._provider_kwargs(model)
        kwargs["model"] = model
        kwargs["input"] = inputs
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base
        try:
            resp = await litellm.aembedding(**kwargs)
        except Exception as e:
            raise LLMError(message=f"Embedding call failed: {e}", model=model) from e

        data = getattr(resp, "data", None)
        if data is None and isinstance(resp, dict):
            data = resp.get("data", [])
        out: list[list[float]] = []
        for item in data or []:
            vec = item.get("embedding") if isinstance(item, dict) else getattr(item, "embedding", None)
            if vec is None:
                continue
            norm = math.sqrt(sum(float(x) ** 2 for x in vec)) or 1.0
            out.append([float(x) / norm for x in vec])
        return out

    # ── LiteLLM integration ──────────────────────────────

    def _provider_kwargs(self, model: str) -> dict[str, Any]:
        """litellm_params for the provider matching the model prefix."""
        provider_name = model.split("/", 1)[0]
        for provider in self._providers:
            if provider.name == provider_name and provider.litellm_params:
                return dict(provider.litellm_params)
        return {}

    def _build_kwargs(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        api_key: str | None,
        api_base: str | None,
        stream: bool,
        metadata: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble litellm kwargs: provider params, then explicit overrides."""
        kwargs: dict[str, Any] = self._provider_kwargs(model)
        kwargs.update({
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        })
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base
        if metadata:
            kwargs["metadata"] = metadata
        if response_format:
            kwargs["response_format"] = response_format
        return kwargs

    async def _call_litellm(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        api_key: str | None,
        api_base: str | None,
        stream: bool,
        metadata: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        """Make a blocking call via litellm.acompletion."""
        import litellm

        kwargs = self._build_kwargs(
            model, messages, temperature, max_tokens, api_key, api_base, stream,
            metadata,
            response_format=response_format,
        )

        start = time.monotonic()

        # litellm.acompletion is async
        response = await litellm.acompletion(**kwargs)
        _record_generation(model, messages, response, metadata)
        message = response.choices[0].message
        reasoning = getattr(message, "reasoning_content", "") or ""
        content = message.content or ""
        finish = getattr(response.choices[0], "finish_reason", "") or ""
        if finish == "length" and content:
            # 正文非空但被截断:finish_reason=length(推理模型 CoT 挤占
            # max_tokens 预算,正文在中间被切)。调用方拿到的 YAML/SQL
            # 是残缺的(如 /kb init 草稿在引号中间切断)——显式告警便于
            # 定位,而不是等下游解析器报出莫名其妙的语法错误。
            logger.warning(
                "LLM response truncated (finish_reason=length): %d content chars, "
                "%d reasoning chars; output budget exhausted mid-generation",
                len(content), len(reasoning),
            )
        _record_local_call(
            model, messages, content,
            metadata, int((time.monotonic() - start) * 1000),
            temperature=temperature, reasoning=reasoning,
            usage=_usage_dict(response),
        )
        # 推理模型可能把全部输出放进 reasoning_content、content 为空
        # (DeepSeek 实测:38s 思考后 content="" 而 reasoning 有正文)。
        # content 为空时回退到 reasoning,否则调用方拿到空串,SQL 提取/
        # 裁决解析全部落空。
        if not content and reasoning:
            # finish_reason=length 说明预算被 CoT 耗尽(推理计入 max_tokens),
            # 与"正文落在 reasoning"是两种故障,日志里要能区分。
            finish = getattr(response.choices[0], "finish_reason", "") or ""
            logger.info(
                "LLM returned empty content with reasoning (%d chars, "
                "finish_reason=%s); falling back to reasoning text",
                len(reasoning), finish,
            )
            return reasoning
        return content

    async def _call_litellm_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        api_key: str | None,
        api_base: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Make a streaming call via litellm.acompletion."""
        import litellm

        kwargs = self._build_kwargs(
            model, messages, temperature, max_tokens, api_key, api_base,
            stream=True, metadata=metadata,
        )

        response = await litellm.acompletion(**kwargs)
        async for part in response:
            if part.choices and part.choices[0].delta.content:
                yield part.choices[0].delta.content


def _record_local_call(
    model: str,
    messages: list[dict[str, str]],
    output: str,
    metadata: dict[str, Any] | None,
    elapsed_ms: int,
    temperature: float = 0.0,
    reasoning: str = "",
    usage: dict[str, int] | None = None,
) -> None:
    """Zero-config local trace: one llm event into the run-trace store.

    Only recorded when the call carries a run_id (pipeline calls do);
    library users without a run stay silent. When an active RunTracer
    owns the run, the event routes through it (span parent linkage +
    run log + optional verbose echo); otherwise the flat llm event is
    written directly.

    Also feeds the per-run token accumulator (token_usage aggregated
    per question and surfaced in the run summary)."""
    run_id = (metadata or {}).get("run_id", "")
    if not run_id:
        return
    try:
        from trove.llm.token_accounting import add
        add(run_id, usage or {})
    except Exception:
        pass
    try:
        from trove.tracing.runlog import get_tracer
        tracer = get_tracer(run_id)
        if tracer is not None:
            tracer.llm(
                node=(metadata or {}).get("node", ""),
                model=model,
                messages=messages,
                output=output,
                elapsed_ms=elapsed_ms,
                temperature=temperature,
                reasoning=reasoning,
            )
            return
        from trove.tracing.local import add_event
        add_event(run_id, {
            "kind": "llm",
            "node": (metadata or {}).get("node", ""),
            "model": model,
            "messages": messages,
            "output": output,
            "elapsed_ms": elapsed_ms,
        })
    except Exception:
        pass


def _usage_dict(raw_response: Any) -> dict[str, int]:
    """Best-effort token usage from a litellm completion response.

    Returns {} when the provider omits usage (mock/legacy), so harness
    token budgets simply skip counting.

    Cache fields are surfaced best-effort across providers: Anthropic
    reports ``cache_read_input_tokens`` / ``cache_creation_input_tokens``,
    OpenAI-family reports ``prompt_tokens_details.cached_tokens``. 0 或
    缺失都算未命中——观测 prompt 缓存的真实命中率(cache_prefix_tokens
    只给"理论上可缓存多少")。
    """
    try:
        usage = getattr(raw_response, "usage", None)
        if usage is None:
            return {}
        out = {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        }
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            out["cached_tokens"] = int(getattr(details, "cached_tokens", 0) or 0)
        return out
    except Exception:
        return {}


def _record_generation(
    model: str,
    messages: list[dict[str, str]],
    response: Any,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record the LLM completion as a Langfuse generation (nested in the
    current node span via context propagation). Silent no-op when
    Langfuse is disabled."""
    client = get_client()
    if client is None:
        return
    try:
        message = response.choices[0].message
        output: dict[str, Any] = {"content": message.content or ""}
        reasoning = getattr(message, "reasoning_content", None)
        if reasoning:
            output["reasoning"] = reasoning
        # Langfuse SDK v4: generations are observations with an explicit type
        with client.start_as_current_observation(
            as_type="generation",
            name="llm",
            model=model,
            input={"messages": messages},
            output=output,
            metadata=metadata or {},
        ):
            pass
    except Exception as e:
        logger.debug("Generation recording failed: %s", e)


def _record_failed_generation(
    model: str,
    messages: list[dict[str, str]],
    error: Exception | str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a failed LLM completion as a langfuse generation (level=ERROR).

    Silent no-op when Langfuse is disabled; only the final failure after
    retries is recorded — individual attempts stay invisible (noise).
    The error text lands in both status_message and output so the failure
    reason is traceable from the UI and the trace API.
    """
    client = get_client()
    if client is None:
        return
    try:
        text = str(error)
        with client.start_as_current_observation(
            as_type="generation",
            name="llm",
            model=model,
            input={"messages": messages},
            output={"error": text},
            level="ERROR",
            status_message=text,
            metadata=metadata or {},
        ):
            pass
    except Exception as e:
        logger.debug("Failed-generation recording failed: %s", e)
