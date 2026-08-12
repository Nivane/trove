"""LLM Gateway — unified interface to LiteLLM.

Supports:
- chat/completion with any LiteLLM-supported provider
- Streaming responses via async generator
- Mock mode for testing (no real API calls)
- Automatic retry with exponential backoff
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from trove.core.errors import LLMError
from trove.core.logging import get_logger

logger = get_logger(__name__)


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
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ):
        """Initialize the LLM gateway.

        Args:
            mock_response: If set, chat() returns this string without calling LLM.
            mock_stream_chunks: If set, chat_stream() yields these chunks.
            max_retries: Maximum retry attempts on failure.
            retry_base_delay: Base delay in seconds for exponential backoff.
        """
        self._mock_response = mock_response
        self._mock_stream_chunks = mock_stream_chunks
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay

    # ── Public API ───────────────────────────────────────

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> str:
        """Send a chat request and return the full response text.

        Args:
            model: LiteLLM model identifier (e.g. "openai/gpt-4o").
            messages: List of message dicts with "role" and "content".
            temperature: Sampling temperature (default 0 for deterministic).
            max_tokens: Maximum tokens in the response.
            api_key: Optional provider API key override.
            api_base: Optional provider API base override.

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
                )
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    delay = self.retry_base_delay * (2 ** attempt)
                    logger.warning(
                        "LLM call attempt %d/%d failed: %s. Retrying in %.1fs...",
                        attempt + 1, self.max_retries, e, delay,
                    )
                    await asyncio.sleep(delay)

        raise LLMError(
            message=f"LLM call failed after {self.max_retries} attempts: {last_error}",
            model=model,
        )

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 4096,
        api_key: str | None = None,
        api_base: str | None = None,
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
            ):
                yield chunk
        except Exception as e:
            raise LLMError(
                message=f"LLM streaming failed: {e}",
                model=model,
            ) from e

    # ── LiteLLM integration ──────────────────────────────

    async def _call_litellm(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        api_key: str | None,
        api_base: str | None,
        stream: bool,
    ) -> str:
        """Make a blocking call via litellm.acompletion."""
        import litellm

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base

        # litellm.acompletion is async
        response = await litellm.acompletion(**kwargs)
        return response.choices[0].message.content or ""

    async def _call_litellm_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        api_key: str | None,
        api_base: str | None,
    ) -> AsyncIterator[str]:
        """Make a streaming call via litellm.acompletion."""
        import litellm

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if api_key:
            kwargs["api_key"] = api_key
        if api_base:
            kwargs["api_base"] = api_base

        response = await litellm.acompletion(**kwargs)
        async for part in response:
            if part.choices and part.choices[0].delta.content:
                yield part.choices[0].delta.content
